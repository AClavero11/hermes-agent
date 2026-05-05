"""Deterministic RFQ fast path for common quote requests."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
import json
import logging
from pathlib import Path
import re
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

CONDITION_CODES = {"SV", "OH", "AR", "NE", "FN", "NS"}
READ_ONLY_RFQ_TOOLS = {
    "customer": "mcp_v11_v11_customer_lookup",
    "inventory": "mcp_v11_v11_get_inventory",
    "pricing": "mcp_v11_v11_search_sales",
}

_CONDITION_RE = re.compile(r"\b(SV|OH|AR|NE|FN|NS)\b(?:\s+condition)?", re.IGNORECASE)
_QTY_RE = re.compile(r"\b(?:qty|quantity|qnty)\s*[:#-]?\s*(\d+(?:\.\d+)?)\b", re.IGNORECASE)
_PART_LABEL_RE = re.compile(
    r"\b(?:p/?n|pn|part(?:\s+(?:number|no\.?))?|part\s*#)\s*[:#-]?\s*([A-Z0-9][A-Z0-9._/-]{2,})\b",
    re.IGNORECASE,
)
_PART_TOKEN_RE = re.compile(r"\b(?=[A-Z0-9._/-]*\d)[A-Z0-9][A-Z0-9._/-]{2,}\b", re.IGNORECASE)
_TARGET_PRICE_RE = re.compile(
    r"\b(?:target(?:\s+price)?|price|at|offer)\s*[:$]?\s*\$?\s*(\d+(?:,\d{3})*(?:\.\d+)?)\b",
    re.IGNORECASE,
)
_RFQ_WORD_RE = re.compile(r"\b(?:rfq|quote|quoted|pricing)\b", re.IGNORECASE)
_CONTEXTUAL_RFQ_RE = re.compile(
    r"\b(?:we\s+sold\s+before|same\s+config|that\s+[a-z0-9._/-]+|again|previous|like\s+last\s+time|last\s+time)\b",
    re.IGNORECASE,
)
_PUSH_PRICE_RE = re.compile(r"\b(?:push(?:\s+the)?\s+price|aggressive)\b", re.IGNORECASE)
_HOLD_PRICE_RE = re.compile(r"\bhold\s+price\b", re.IGNORECASE)
_BEST_PRICE_RE = re.compile(r"\b(?:best\s+price|cheap|move\s+it)\b", re.IGNORECASE)
_BARE_NUMBER_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*$")
_FOLLOWUP_FILLER_RE = re.compile(r"\b(?:please|pls|thx|thanks)\b", re.IGNORECASE)

_CUSTOMER_STOP_WORDS = {
    "quote",
    "rfq",
    "from",
    "for",
    "need",
    "customer",
    "please",
    "pn",
    "part",
    "number",
    "wants",
    "want",
    "needs",
    "need",
}

_CONTEXT_CATEGORY_STOP_WORDS = {
    "one",
    "same",
    "config",
    "condition",
    "again",
    "previous",
    "last",
    "time",
}


@dataclass(frozen=True)
class ParsedRFQ:
    customer: str
    part_number: str
    qty: str
    condition: str
    target_price: str
    free_text: str

    @property
    def missing_fields(self) -> list[str]:
        missing: list[str] = []
        if not self.part_number:
            missing.append("part_number")
        if not self.qty:
            missing.append("qty")
        if not self.condition:
            missing.append("condition")
        return missing

    @property
    def sufficient_for_lookup(self) -> bool:
        return not self.missing_fields


@dataclass(frozen=True)
class RFQFastPathResult:
    response: str
    parsed: ParsedRFQ
    tool_calls: tuple[str, ...]
    elapsed_seconds: float


@dataclass(frozen=True)
class InventoryEvidence:
    summary: str
    covers: bool
    total_qty: float
    condition_qty: float


@dataclass(frozen=True)
class SaleEvidence:
    price: float
    order: str
    date: str
    customer: str
    recency: str
    weight: float


@dataclass(frozen=True)
class PricingEvidence:
    summary: str
    sales: tuple[SaleEvidence, ...]


@dataclass(frozen=True)
class PricingDecision:
    recommended_price: str
    confidence: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ContextualRFQ:
    customer: str
    category: str
    qty: str
    condition: str
    target_price: str
    pricing_modifier: str
    free_text: str

    @property
    def missing_fields(self) -> list[str]:
        missing: list[str] = []
        if not self.customer:
            missing.append("customer")
        if not self.category:
            missing.append("part_category")
        if not self.qty:
            missing.append("qty")
        if not self.condition:
            missing.append("condition")
        return missing


@dataclass(frozen=True)
class ContextualSalesMatch:
    part_number: str
    config: str
    price: float | None
    qty: float | None
    condition: str
    order: str
    date: str
    customer: str
    score: float


@dataclass(frozen=True)
class InferredField:
    name: str
    value: str
    source: str


def _clean_token(value: str) -> str:
    return re.sub(r"^[^\w]+|[^\w]+$", "", value or "").strip()


def _clean_customer(value: str) -> str:
    raw = re.sub(r"\s+", " ", value or "").strip(" :-,.")
    if not raw:
        return ""
    tokens = [
        token
        for token in raw.split()
        if token.lower() not in _CUSTOMER_STOP_WORDS
    ]
    cleaned = " ".join(tokens).strip(" :-,.")
    if not cleaned:
        return ""
    if _PART_TOKEN_RE.fullmatch(cleaned):
        return ""
    return cleaned


def _extract_part(text: str) -> str:
    label_match = _PART_LABEL_RE.search(text)
    if label_match:
        return _clean_token(label_match.group(1)).upper()

    qty_match = _QTY_RE.search(text)
    candidates: list[tuple[int, str]] = []
    for match in _PART_TOKEN_RE.finditer(text):
        token = _clean_token(match.group(0))
        if not token:
            continue
        lowered = token.lower()
        if lowered in {"rfq", "quote", "qty", "quantity"}:
            continue
        if token.upper() in CONDITION_CODES:
            continue
        if qty_match and match.start() >= qty_match.start() and match.end() <= qty_match.end():
            continue
        if token.replace(".", "", 1).isdigit() and qty_match and token == qty_match.group(1):
            continue
        candidates.append((match.start(), token.upper()))

    if not candidates:
        return ""
    if qty_match:
        before_qty = [item for item in candidates if item[0] < qty_match.start()]
        if before_qty:
            return before_qty[-1][1]
    return candidates[0][1]


def _extract_qty(text: str) -> str:
    match = _QTY_RE.search(text)
    if match:
        return match.group(1)
    x_match = re.search(r"\bx\s*(\d+(?:\.\d+)?)\b", text, re.IGNORECASE)
    return x_match.group(1) if x_match else ""


def _extract_contextual_qty(text: str) -> str:
    qty = _extract_qty(text)
    if qty:
        return qty
    condition_qty = re.search(
        r"\b(?:SV|OH|AR|NE|FN|NS)\b(?:\s+condition)?\s+(\d+(?:\.\d+)?)\b",
        text,
        re.IGNORECASE,
    )
    if condition_qty:
        return condition_qty.group(1)
    trailing_qty = re.search(r"\b(\d+(?:\.\d+)?)\s*$", text)
    return trailing_qty.group(1) if trailing_qty else ""


def _extract_condition(text: str) -> str:
    match = _CONDITION_RE.search(text)
    return match.group(1).upper() if match else ""


def _extract_target_price(text: str) -> str:
    match = _TARGET_PRICE_RE.search(text)
    return match.group(1).replace(",", "") if match else ""


def _extract_customer(text: str, part_number: str) -> str:
    part_pattern = re.escape(part_number) if part_number else r"(?:p/?n|pn|part|\d)"
    patterns = [
        rf"\bfrom\s+(.+?)\s+\b(?:for|p/?n|pn|part|qty|{part_pattern})\b",
        rf"\bcustomer\s+(.+?)\s+\b(?:for|p/?n|pn|part|qty|{part_pattern})\b",
        rf"\b(?:quote|rfq)\s+(.+?)\s+\b(?:for|p/?n|pn|part|qty|{part_pattern})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            customer = _clean_customer(match.group(1))
            if customer:
                return customer

    label_match = _PART_LABEL_RE.search(text)
    if label_match:
        customer = _clean_customer(text[:label_match.start()])
        if customer:
            return customer

    if part_number:
        part_match = re.search(re.escape(part_number), text, re.IGNORECASE)
        if part_match:
            prefix = text[:part_match.start()]
            prefix = re.sub(r"\b(?:quote|rfq|need|for|from)\b", " ", prefix, flags=re.IGNORECASE)
            customer = _clean_customer(prefix)
            if customer:
                return customer

    return ""


def _scrub_followup_text(text: str) -> str:
    scrubbed = text or ""
    scrubbed = _PART_LABEL_RE.sub(" ", scrubbed)
    scrubbed = _QTY_RE.sub(" ", scrubbed)
    scrubbed = _CONDITION_RE.sub(" ", scrubbed)
    scrubbed = _TARGET_PRICE_RE.sub(" ", scrubbed)
    scrubbed = _FOLLOWUP_FILLER_RE.sub(" ", scrubbed)
    return re.sub(r"[\s:#,$-]+", "", scrubbed).strip()


def _extract_rfq_followup_fields(text: str, previous: ParsedRFQ | None = None) -> dict[str, str]:
    raw = (text or "").strip()
    if not raw:
        return {}

    fields: dict[str, str] = {}
    labeled_part = _PART_LABEL_RE.search(raw)
    if labeled_part:
        fields["part_number"] = _clean_token(labeled_part.group(1)).upper()

    qty = _extract_qty(raw)
    condition = _extract_condition(raw)
    target_price = _extract_target_price(raw)
    if qty:
        fields["qty"] = qty
    if condition:
        fields["condition"] = condition
    if target_price:
        fields["target_price"] = target_price

    bare_number = _BARE_NUMBER_RE.fullmatch(raw)
    if bare_number and previous is not None:
        number = bare_number.group(1)
        if "qty" in previous.missing_fields:
            fields["qty"] = number
        elif "part_number" in previous.missing_fields and len(number.replace(".", "")) >= 3:
            fields["part_number"] = number.upper()

    if not fields:
        return {}

    if not bare_number and _scrub_followup_text(raw):
        return {}
    return fields


def is_rfq_field_followup(text: str) -> bool:
    """Return True for short RFQ field-only replies such as ``qty 1``."""
    raw = (text or "").strip()
    if len(raw.split()) > 6:
        return False
    fields = _extract_rfq_followup_fields(raw)
    return bool(fields) and not _RFQ_WORD_RE.search(raw)


def merge_rfq_followup_prompt(previous_text: str, followup_text: str) -> str:
    """Merge a short field-only reply into the previous incomplete RFQ prompt."""
    previous = parse_rfq_prompt(previous_text)
    if previous is None:
        return ""
    fields = _extract_rfq_followup_fields(followup_text, previous)
    if not fields:
        return ""

    customer = previous.customer
    part_number = fields.get("part_number") or previous.part_number
    qty = fields.get("qty") or previous.qty
    condition = fields.get("condition") or previous.condition
    target_price = fields.get("target_price") or previous.target_price

    parts = ["quote"]
    if customer:
        parts.append(customer)
    if part_number:
        parts.extend(["pn", part_number])
    if qty:
        parts.extend(["qty", qty])
    if condition:
        parts.extend([condition, "condition"])
    if target_price:
        parts.extend(["target", target_price])
    return " ".join(parts).strip()


def build_rfq_followup_without_context_response(text: str) -> str:
    """Return a concise reply for RFQ field-only text without active RFQ context."""
    fields = _extract_rfq_followup_fields(text)
    if not fields:
        return ""
    parsed_fields: list[str] = []
    if fields.get("part_number"):
        parsed_fields.append(f"PN {fields['part_number']}")
    if fields.get("qty"):
        parsed_fields.append(f"qty {fields['qty']}")
    if fields.get("condition"):
        parsed_fields.append(f"{fields['condition']} condition")
    if fields.get("target_price"):
        parsed_fields.append(f"target ${fields['target_price']}")
    received = ", ".join(parsed_fields) if parsed_fields else "RFQ field"
    return "\n".join([
        f"Received: {received}.",
        "No active incomplete RFQ found in this chat.",
        "Send customer, PN, condition, and qty in one message.",
    ])


def parse_rfq_prompt(text: str) -> ParsedRFQ | None:
    raw = (text or "").strip()
    if not raw:
        return None

    part_number = _extract_part(raw)
    qty = _extract_qty(raw)
    condition = _extract_condition(raw)
    customer = _extract_customer(raw, part_number)
    target_price = _extract_target_price(raw)

    rfq_like = bool(_RFQ_WORD_RE.search(raw)) or bool(_PART_LABEL_RE.search(raw))
    has_core = bool(part_number and qty and condition)
    if not (rfq_like or has_core):
        return None

    return ParsedRFQ(
        customer=customer,
        part_number=part_number,
        qty=qty,
        condition=condition,
        target_price=target_price,
        free_text=raw,
    )


def _extract_contextual_customer(text: str) -> str:
    patterns = [
        r"^\s*(.+?)\s+\b(?:wants?|needs?|asked|asks|requested|looking)\b",
        r"\b(?:for|from|customer)\s+(.+?)\s+\b(?:that|same|previous|last|again)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            customer = _clean_customer(match.group(1))
            if customer:
                return customer
    prefix = re.split(r"\b(?:that|same|previous|last|again|we\s+sold)\b", text, maxsplit=1, flags=re.IGNORECASE)[0]
    return _clean_customer(prefix)


def _extract_contextual_category(text: str) -> str:
    patterns = [
        r"\bthat\s+([a-z0-9._/-]+)\b",
        r"\b(?:same|previous|last)\s+([a-z0-9._/-]+)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        candidate = _clean_token(match.group(1)).lower()
        if candidate and candidate not in _CONTEXT_CATEGORY_STOP_WORDS and candidate.upper() not in CONDITION_CODES:
            return candidate
    tokens = [
        _clean_token(token).lower()
        for token in re.findall(r"\b[a-z][a-z0-9._/-]{2,}\b", text, re.IGNORECASE)
    ]
    for token in tokens:
        if token and token not in _CUSTOMER_STOP_WORDS and token not in _CONTEXT_CATEGORY_STOP_WORDS:
            if token not in {"turkish", "delta", "customer", "sold", "before", "wants", "needs"}:
                return token
    return ""


def _extract_pricing_modifier(text: str) -> str:
    if _PUSH_PRICE_RE.search(text):
        return "push"
    if _HOLD_PRICE_RE.search(text):
        return "hold"
    if _BEST_PRICE_RE.search(text):
        return "competitive"
    return ""


def parse_contextual_rfq_prompt(text: str) -> ContextualRFQ | None:
    raw = (text or "").strip()
    if not raw or not _CONTEXTUAL_RFQ_RE.search(raw):
        return None
    return ContextualRFQ(
        customer=_extract_contextual_customer(raw),
        category=_extract_contextual_category(raw),
        qty=_extract_contextual_qty(raw),
        condition=_extract_condition(raw),
        target_price=_extract_target_price(raw),
        pricing_modifier=_extract_pricing_modifier(raw),
        free_text=raw,
    )


def _load_json_payload(raw: str) -> Any:
    try:
        payload = json.loads(raw or "")
    except Exception:
        return raw
    if isinstance(payload, dict) and "result" in payload:
        inner = payload.get("result")
        if isinstance(inner, str):
            try:
                return json.loads(inner)
            except Exception:
                return inner
        return inner
    return payload


def _available_tool_names() -> set[str]:
    try:
        from model_tools import get_tool_definitions

        return {
            item.get("function", {}).get("name", "")
            for item in get_tool_definitions(quiet_mode=True)
            if isinstance(item, dict)
        }
    except Exception as exc:
        logger.info("RFQ fast path could not inspect tool definitions: %s", exc)
        return set()


def _call_read_only_tool(name: str, args: dict[str, Any], *, task_id: str) -> str:
    if name not in READ_ONLY_RFQ_TOOLS.values():
        raise ValueError(f"RFQ fast path refused non-read-only tool: {name}")
    from model_tools import handle_function_call

    return handle_function_call(name, args, task_id=task_id)


def _best_customer(customers: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    if not customers:
        return None
    normalized_query = re.sub(r"[^a-z0-9]+", "", query.lower())

    def score(customer: dict[str, Any]) -> tuple[int, str]:
        name = str(customer.get("name") or "")
        normalized_name = re.sub(r"[^a-z0-9]+", "", name.lower())
        value = 0
        if normalized_name == normalized_query:
            value += 100
        if normalized_name.startswith(normalized_query):
            value += 30
        if normalized_query and normalized_query in normalized_name:
            value += 20
        if customer.get("email"):
            value += 3
        if float(customer.get("credit") or 0) or float(customer.get("debit") or 0):
            value += 2
        if "technic" in normalized_name:
            value += 1
        return (value, name)

    return sorted(customers, key=score, reverse=True)[0]


def _summarize_customer(data: Any, query: str) -> tuple[str, str]:
    if not isinstance(data, dict):
        return ("Lookup returned non-JSON output.", "")
    customers = data.get("customers") if isinstance(data.get("customers"), list) else []
    if not customers:
        return (f"No V11 customer match for {query}.", "")
    best = _best_customer(customers, query) or customers[0]
    alternatives = [str(c.get("name") or "") for c in customers if c is not best and c.get("name")]
    line = str(best.get("name") or "unknown")
    details: list[str] = []
    if best.get("email"):
        details.append(f"email {best.get('email')}")
    if best.get("phone"):
        details.append(f"phone {best.get('phone')}")
    if details:
        line += " (" + ", ".join(details[:2]) + ")"
    if alternatives:
        line += "; other matches: " + ", ".join(alternatives[:3])
    return (line, str(best.get("name") or ""))


def _summarize_inventory(data: Any, requested_condition: str, requested_qty: str) -> InventoryEvidence:
    if not isinstance(data, dict):
        return InventoryEvidence("Lookup returned non-JSON output.", False, 0.0, 0.0)
    if data.get("error"):
        return InventoryEvidence(f"Lookup failed: {data.get('error')}", False, 0.0, 0.0)
    if not data.get("found", True):
        return InventoryEvidence(str(data.get("message") or "Part not found in V11."), False, 0.0, 0.0)
    lines = data.get("lines") if isinstance(data.get("lines"), list) else []
    total_qty = float(data.get("total_qty") or 0)
    condition_qty = 0.0
    for line in lines:
        if str(line.get("condition") or "").upper() == requested_condition.upper():
            try:
                condition_qty += float(line.get("quantity") or 0)
            except Exception:
                pass
    try:
        needed = float(requested_qty or 0)
    except Exception:
        needed = 0.0
    covers = condition_qty >= needed if needed else condition_qty > 0
    return InventoryEvidence(
        f"V11 total {total_qty:g}; {requested_condition.upper()} available {condition_qty:g}; requested qty {requested_qty}: {'covered' if covers else 'not covered'}",
        covers,
        total_qty,
        condition_qty,
    )


def _sale_recency(order_date: str) -> tuple[str, float]:
    try:
        parsed = datetime.fromisoformat(order_date[:10]).date()
    except Exception:
        return ("unknown date", 0.4)
    age_days = max((date.today() - parsed).days, 0)
    if age_days < 30:
        return ("recent", 1.0)
    if age_days <= 90:
        return ("30-90 days", 0.65)
    return (">90 days", 0.35)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except Exception:
        return None


def _format_money(value: float) -> str:
    rounded = int(round(value))
    if rounded >= 5000:
        rounded = int(round(rounded / 500.0) * 500)
    elif rounded >= 1000:
        rounded = int(round(rounded / 100.0) * 100)
    elif rounded >= 100:
        rounded = int(round(rounded / 50.0) * 50)
    return f"${rounded:,.0f}"


def _summarize_pricing(data: Any) -> PricingEvidence:
    if not isinstance(data, dict):
        return PricingEvidence("Lookup returned non-JSON output.", ())
    if data.get("error"):
        return PricingEvidence(f"Lookup failed: {data.get('error')}", ())
    orders = data.get("orders") if isinstance(data.get("orders"), list) else []
    if not orders:
        return PricingEvidence("No same-part V11 sales evidence found.", ())
    snippets: list[str] = []
    sales: list[SaleEvidence] = []
    for order in orders[:3]:
        order_name = str(order.get("order") or "")
        customer = str(order.get("customer") or "")
        date = str(order.get("date") or "")[:10]
        lines = order.get("matching_lines") if isinstance(order.get("matching_lines"), list) else []
        prices = []
        for line in lines:
            price = _as_float(line.get("unit_price"))
            if price is not None:
                prices.append(f"{price:g}")
                recency, weight = _sale_recency(date)
                sales.append(SaleEvidence(price, order_name, date, customer, recency, weight))
        price_text = ", ".join(prices) if prices else f"order total {order.get('total')}"
        snippets.append(f"{order_name} {date} {customer}: {price_text}")
    return PricingEvidence("; ".join(snippets), tuple(sales))


def _normalize_match_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _line_part_number(line: dict[str, Any]) -> str:
    for key in ("product", "part_number", "name", "default_code", "part"):
        value = str(line.get(key) or "").strip()
        if value:
            return _clean_token(value).upper()
    return ""


def _line_config(line: dict[str, Any]) -> str:
    fields = [
        str(line.get("description") or ""),
        str(line.get("name") or ""),
        str(line.get("product") or ""),
        str(line.get("condition") or ""),
    ]
    return " ".join(field for field in fields if field).strip()


def _order_lines(order: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("matching_lines", "lines", "order_lines"):
        value = order.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _score_contextual_match(
    context: ContextualRFQ,
    *,
    order: dict[str, Any],
    line: dict[str, Any],
    part_number: str,
    config: str,
) -> float:
    category = _normalize_match_text(context.category)
    searchable = _normalize_match_text(f"{part_number} {config}")
    score = 0.0
    if category and category in searchable:
        score += 5.0
    if context.condition:
        condition = str(line.get("condition") or "")
        if condition.upper() == context.condition.upper():
            score += 2.0
        elif context.condition.lower() in searchable:
            score += 1.0
    if context.customer:
        customer = _normalize_match_text(str(order.get("customer") or ""))
        query_customer = _normalize_match_text(context.customer)
        if query_customer and query_customer in customer:
            score += 1.0
    recency, weight = _sale_recency(str(order.get("date") or "")[:10])
    if recency == "recent":
        score += 2.0
    elif recency == "30-90 days":
        score += 1.0
    if _as_float(line.get("unit_price")) is not None:
        score += 1.0
    requested_qty = _parse_qty(context.qty)
    line_qty = _as_float(line.get("qty") or line.get("quantity") or line.get("product_uom_qty"))
    if requested_qty and line_qty == requested_qty:
        score += 0.5
    if weight <= 0.35:
        score -= 0.5
    return score


def _find_contextual_sales_matches(data: Any, context: ContextualRFQ) -> list[ContextualSalesMatch]:
    if not isinstance(data, dict):
        return []
    orders = data.get("orders") if isinstance(data.get("orders"), list) else []
    matches: dict[str, ContextualSalesMatch] = {}
    for order in orders:
        if not isinstance(order, dict):
            continue
        if context.customer:
            customer = _normalize_match_text(str(order.get("customer") or ""))
            query_customer = _normalize_match_text(context.customer)
            if query_customer and query_customer not in customer:
                continue
        for line in _order_lines(order):
            part_number = _line_part_number(line)
            if not part_number:
                continue
            config = _line_config(line)
            category = _normalize_match_text(context.category)
            searchable = _normalize_match_text(f"{part_number} {config}")
            if category and category not in searchable:
                continue
            score = _score_contextual_match(context, order=order, line=line, part_number=part_number, config=config)
            if score < 4.0:
                continue
            price = _as_float(line.get("unit_price") or line.get("price_unit"))
            qty = _as_float(line.get("qty") or line.get("quantity") or line.get("product_uom_qty"))
            candidate = ContextualSalesMatch(
                part_number=part_number,
                config=config or part_number,
                price=price,
                qty=qty,
                condition=str(line.get("condition") or context.condition or "").upper(),
                order=str(order.get("order") or ""),
                date=str(order.get("date") or "")[:10],
                customer=str(order.get("customer") or ""),
                score=score,
            )
            previous = matches.get(part_number)
            if previous is None or candidate.score > previous.score:
                matches[part_number] = candidate
    return sorted(matches.values(), key=lambda item: (item.score, item.date), reverse=True)


def _sales_payload_has_line_details(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    orders = data.get("orders") if isinstance(data.get("orders"), list) else []
    for order in orders:
        if isinstance(order, dict) and _order_lines(order):
            return True
    return False


def _pricing_root() -> Path:
    return Path.home() / "alexandria" / "advanced" / "pricing"


def _category_part_numbers(category: str) -> set[str]:
    normalized = _normalize_match_text(category)
    if not normalized:
        return set()
    root = _pricing_root()
    parts: set[str] = set()
    if normalized == "idg":
        for path in [root / "PRICING_SERVICE.md", root / "IDG_PIECE_PARTS_HOT_LIST.md"]:
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for token in re.findall(r"\b[A-Z0-9][A-Z0-9-]{4,}\b", content.upper()):
                if any(ch.isdigit() for ch in token):
                    parts.add(token.strip("-"))

    oem_path = root / "oem_master_2026.csv"
    try:
        with oem_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                haystack = _normalize_match_text(" ".join(str(value or "") for value in row.values()))
                part = _clean_token(str(row.get("PN") or row.get("part_number") or "")).upper()
                if part and normalized in haystack:
                    parts.add(part)
    except OSError:
        pass
    return parts


def _contextual_sales_matches_from_pricing_export(context: ContextualRFQ) -> list[ContextualSalesMatch]:
    parts = _category_part_numbers(context.category)
    if not parts:
        return []
    sales_path = _pricing_root() / "data" / "v11_completed_sales.csv"
    matches: dict[str, ContextualSalesMatch] = {}
    customer_query = _normalize_match_text(context.customer)
    requested_qty = _parse_qty(context.qty)
    try:
        with sales_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                customer = str(row.get("customer_name") or "")
                if customer_query and customer_query not in _normalize_match_text(customer):
                    continue
                part_number = _clean_token(str(row.get("part_number") or "")).upper()
                if part_number not in parts:
                    continue
                date_text = str(row.get("date_order") or "")[:10]
                recency, weight = _sale_recency(date_text)
                price = _as_float(row.get("unit_price"))
                qty = _as_float(row.get("quantity"))
                score = 5.0
                if customer_query:
                    score += 1.0
                if requested_qty and qty == requested_qty:
                    score += 0.5
                if price is not None:
                    score += 1.0
                if recency == "recent":
                    score += 2.0
                elif recency == "30-90 days":
                    score += 1.0
                elif weight <= 0.35:
                    score -= 0.5
                candidate = ContextualSalesMatch(
                    part_number=part_number,
                    config=f"{context.category.upper()} category from V11 completed sales export",
                    price=price,
                    qty=qty,
                    condition=context.condition or "SV",
                    order=str(row.get("order_number") or ""),
                    date=date_text,
                    customer=customer,
                    score=score,
                )
                previous = matches.get(part_number)
                if previous is None or (candidate.date, candidate.score) > (previous.date, previous.score):
                    matches[part_number] = candidate
    except OSError:
        return []

    ranked = sorted(matches.values(), key=lambda item: (item.date, item.score), reverse=True)
    if ranked and (len(ranked) == 1 or ranked[0].date > ranked[1].date):
        newest = ranked[0]
        ranked[0] = ContextualSalesMatch(
            part_number=newest.part_number,
            config=newest.config,
            price=newest.price,
            qty=newest.qty,
            condition=newest.condition,
            order=newest.order,
            date=newest.date,
            customer=newest.customer,
            score=newest.score + 2.0,
        )
    return sorted(ranked, key=lambda item: (item.score, item.date), reverse=True)


def _sales_match_line(match: ContextualSalesMatch) -> str:
    price = f", last sale {_format_money(match.price)}" if match.price is not None else ""
    qty = f", qty {match.qty:g}" if match.qty is not None else ""
    condition = f", {match.condition}" if match.condition else ""
    order = f"{match.order} " if match.order else ""
    date_text = f"{match.date} " if match.date else ""
    return f"{match.part_number} ({order}{date_text}{match.customer}{qty}{condition}{price}; {match.config})"


def _context_missing_response(context: ContextualRFQ) -> str:
    return "\n".join([
        "Contextual RFQ fast path",
        "",
        "Parsed context:",
        f"- Customer: {context.customer or 'missing'}",
        f"- Part category: {context.category or 'missing'}",
        f"- Qty: {context.qty or 'missing'}",
        f"- Condition: {context.condition or 'missing'}",
        "",
        "Missing fields: " + ", ".join(context.missing_fields),
        "Next available action: send the missing RFQ detail or the PN.",
        "No customer send, V11 write, Atlas write, or destructive shell action was performed.",
    ])


def _context_tools_missing_response(context: ContextualRFQ, missing_tool: str) -> str:
    return "\n".join([
        "Contextual RFQ fast path",
        "",
        "Parsed context:",
        f"- Customer: {context.customer or 'missing'}",
        f"- Part category: {context.category or 'missing'}",
        f"- Qty: {context.qty or 'missing'}",
        f"- Condition: {context.condition or 'missing'}",
        "",
        f"Sales lookup unavailable: {missing_tool}",
        "Next available action: retry when read-only sales lookup is available, or send the PN.",
        "No customer send, V11 write, Atlas write, or destructive shell action was performed.",
    ])


def _context_no_match_response(context: ContextualRFQ) -> str:
    return "\n".join([
        "Contextual RFQ fast path",
        "",
        "Parsed context:",
        f"- Customer: {context.customer}",
        f"- Part category: {context.category}",
        f"- Qty: {context.qty}",
        f"- Condition: {context.condition}",
        "",
        "Prior sales match: none strong enough to identify a PN.",
        "Next available action: send the PN, or add aircraft/config detail.",
        "No customer send, V11 write, Atlas write, or destructive shell action was performed.",
    ])


def _context_ambiguous_response(context: ContextualRFQ, matches: list[ContextualSalesMatch]) -> str:
    lines = [
        "Contextual RFQ fast path",
        "",
        "Parsed context:",
        f"- Customer: {context.customer}",
        f"- Part category: {context.category}",
        f"- Qty: {context.qty}",
        f"- Condition: {context.condition}",
        "",
        "Multiple plausible prior-sale matches:",
    ]
    lines.extend(f"- {item}" for item in (_sales_match_line(match) for match in matches[:2]))
    lines.extend([
        "",
        "Next available action: reply with the PN or choose option 1 or 2.",
        "No customer send, V11 write, Atlas write, or destructive shell action was performed.",
    ])
    return "\n".join(lines)


def _inferred_field_lines(fields: tuple[InferredField, ...]) -> list[str]:
    if not fields:
        return []
    lines = ["Inferred fields:"]
    lines.extend(f"- {field.name}: {field.value} ({field.source})" for field in fields)
    lines.append("")
    return lines


def _parse_qty(value: str) -> float:
    try:
        return max(float(value or 0), 0.0)
    except Exception:
        return 0.0


def _condition_adjustment(condition: str) -> tuple[float, str]:
    condition = condition.upper()
    adjustments = {
        "OH": (0.15, "OH condition carries a premium over serviceable sales."),
        "NE": (0.20, "NE condition carries the highest condition premium."),
        "FN": (0.18, "FN condition carries a new/unused premium."),
        "SV": (0.08, "SV condition supports a moderate premium."),
        "NS": (0.05, "NS condition supports a small premium."),
        "AR": (-0.08, "AR condition needs a discount against serviceable/OH evidence."),
    }
    return adjustments.get(condition, (0.0, f"{condition} condition has no configured adjustment."))


def _inventory_adjustment(inventory: InventoryEvidence, qty: float) -> tuple[float, str]:
    if not inventory.covers:
        return (0.0, "inventory does not cover the requested condition/qty.")
    if inventory.condition_qty <= max(qty * 1.5, qty + 1):
        return (0.10, "low condition stock increases price pressure.")
    if inventory.condition_qty <= max(qty * 3, 3):
        return (0.05, "limited condition stock supports a firmer price.")
    if inventory.total_qty >= 10 or inventory.condition_qty >= max(qty * 5, 5):
        return (-0.02, "inventory is available enough to stay competitive.")
    return (0.0, "inventory is available without strong stock pressure.")


def _qty_adjustment(qty: float) -> tuple[float, str]:
    if qty <= 1:
        return (0.03, "single unit order supports higher margin.")
    if qty >= 5:
        return (-0.07, "multi-unit order warrants a volume discount.")
    if qty >= 2:
        return (-0.03, "small multi-unit order gets a modest discount.")
    return (0.0, "quantity has no price adjustment.")


def _make_pricing_decision(
    parsed: ParsedRFQ,
    *,
    inventory: InventoryEvidence,
    pricing: PricingEvidence,
    pricing_modifier: str = "",
) -> PricingDecision:
    qty = _parse_qty(parsed.qty)
    if not pricing.sales:
        reasons = [
            "no same-part sales price was available.",
            inventory.summary,
            f"{parsed.condition} condition requested.",
        ]
        return PricingDecision("manual pricing required", "low", tuple(reasons))

    weighted_total = sum(sale.price * sale.weight for sale in pricing.sales)
    weight = sum(sale.weight for sale in pricing.sales) or 1.0
    baseline = weighted_total / weight
    most_recent = max(pricing.sales, key=lambda sale: sale.weight)

    condition_delta, condition_reason = _condition_adjustment(parsed.condition)
    inventory_delta, inventory_reason = _inventory_adjustment(inventory, qty)
    qty_delta, qty_reason = _qty_adjustment(qty)
    adjustment = condition_delta + inventory_delta + qty_delta
    recommended = baseline * (1 + adjustment)
    modifier_reason = ""
    if pricing_modifier == "push":
        pushed = baseline * 1.10
        if pushed > recommended:
            recommended = pushed
        formatted_base = _format_money(baseline * (1 + adjustment))
        formatted_pushed = _format_money(recommended)
        if formatted_pushed == formatted_base and recommended > baseline:
            recommended += 500 if recommended >= 5000 else max(recommended * 0.03, 50)
        modifier_reason = "price increased due to push price request."
    elif pricing_modifier == "hold":
        recommended = baseline
        modifier_reason = "price held at prior-sale baseline by request."
    elif pricing_modifier == "competitive":
        recommended = min(recommended, baseline * 0.95)
        modifier_reason = "price decreased for best-price/competitive request."

    has_recent = any(sale.recency == "recent" for sale in pricing.sales)
    has_medium = any(sale.recency == "30-90 days" for sale in pricing.sales)
    if has_recent and inventory.covers:
        confidence = "high"
    elif (has_recent or has_medium) and pricing.sales:
        confidence = "medium"
    else:
        confidence = "low"

    reasons = [
        f"last sale {most_recent.price:g} ({most_recent.recency} signal).",
        condition_reason,
        inventory_reason,
        qty_reason,
    ]
    if modifier_reason:
        reasons.append(modifier_reason)
    return PricingDecision(_format_money(recommended), confidence, tuple(reasons))


def _missing_response(parsed: ParsedRFQ) -> str:
    return "\n".join([
        "RFQ fast path",
        "",
        "Parsed RFQ:",
        f"- Customer: {parsed.customer or 'missing'}",
        f"- Part number: {parsed.part_number or 'missing'}",
        f"- Qty: {parsed.qty or 'missing'}",
        f"- Condition: {parsed.condition or 'missing'}",
        f"- Target price: {parsed.target_price or 'not provided'}",
        "",
        "Missing fields: " + ", ".join(parsed.missing_fields),
        "No lookup tools executed.",
        "Next available action: send the missing RFQ fields.",
        "Approval required before customer send, V11 write, or Atlas write.",
    ])


def _tools_missing_response(parsed: ParsedRFQ, missing_tools: list[str]) -> str:
    return "\n".join([
        "RFQ fast path",
        "",
        "Parsed RFQ:",
        f"- Customer: {parsed.customer or 'missing'}",
        f"- Part number: {parsed.part_number}",
        f"- Qty: {parsed.qty}",
        f"- Condition: {parsed.condition}",
        f"- Target price: {parsed.target_price or 'not provided'}",
        "",
        "Lookup tools unavailable: " + ", ".join(missing_tools),
        "No lookup tools executed.",
        "Next available action: retry when read-only V11 lookup tools are available.",
        "Approval required before customer send, V11 write, or Atlas write.",
    ])


def _format_quote_packet(
    parsed: ParsedRFQ,
    *,
    customer_summary: str,
    customer_name: str,
    inventory: InventoryEvidence,
    pricing: PricingEvidence,
    decision: PricingDecision,
    tool_errors: list[str],
    context_summary: str = "",
    inferred_fields: tuple[InferredField, ...] = (),
) -> str:
    draft = "Not enough evidence for a customer-ready draft."
    if customer_name and inventory.covers and decision.recommended_price.startswith("$"):
        draft = (
            f"{customer_name},\n"
            f"We can support PN {parsed.part_number}, qty {parsed.qty}, {parsed.condition} condition"
            f" at {decision.recommended_price}, subject to final availability."
        )

    lines = [
        "RFQ fast path",
        "",
        "Parsed RFQ:",
        f"- Customer: {parsed.customer or 'missing'}",
        f"- Part number: {parsed.part_number}",
        f"- Qty: {parsed.qty}",
        f"- Condition: {parsed.condition}",
        f"- Target price: {('$' + parsed.target_price) if parsed.target_price else 'not provided'}",
        "",
    ]
    if context_summary:
        lines.extend(["Context resolution:", f"- {context_summary}", ""])
    lines.extend(_inferred_field_lines(inferred_fields))
    lines.extend([
        "Customer match:",
        f"- {customer_summary}",
        "",
        "Inventory result:",
        f"- {inventory.summary}",
        "",
        "Pricing evidence:",
        f"- {pricing.summary}",
        "",
        "Recommended price:",
        f"- {decision.recommended_price}",
        "Confidence:",
        f"- {decision.confidence}",
        "Reason:",
        *[f"- {reason}" for reason in decision.reasons],
        "",
        "Customer-ready draft:",
        draft,
        "",
        "Approval required before customer send, V11 write, or Atlas write.",
    ])
    if tool_errors:
        lines.extend(["", "Lookup issues:"])
        lines.extend(f"- {item}" for item in tool_errors)
    return "\n".join(lines)


def _run_standard_rfq_lookup(
    parsed: ParsedRFQ,
    *,
    tools: set[str],
    caller: Callable[[str, dict[str, Any]], str],
    called: list[str],
    errors: list[str],
    started: float,
    context_summary: str = "",
    context_sale: ContextualSalesMatch | None = None,
    inferred_fields: tuple[InferredField, ...] = (),
    pricing_modifier: str = "",
) -> RFQFastPathResult:
    customer_summary = "Customer not provided."
    customer_name = ""
    if parsed.customer and READ_ONLY_RFQ_TOOLS["customer"] in tools:
        tool_name = READ_ONLY_RFQ_TOOLS["customer"]
        called.append(tool_name)
        try:
            customer_summary, customer_name = _summarize_customer(
                _load_json_payload(caller(tool_name, {"name": parsed.customer, "limit": 5})),
                parsed.customer,
            )
        except Exception as exc:
            errors.append(f"customer lookup failed: {exc}")
            customer_summary = f"Lookup failed: {exc}"

    inventory = InventoryEvidence("Inventory lookup tool unavailable.", False, 0.0, 0.0)
    if READ_ONLY_RFQ_TOOLS["inventory"] in tools:
        tool_name = READ_ONLY_RFQ_TOOLS["inventory"]
        called.append(tool_name)
        try:
            inventory = _summarize_inventory(
                _load_json_payload(caller(tool_name, {"part_number": parsed.part_number})),
                parsed.condition,
                parsed.qty,
            )
        except Exception as exc:
            errors.append(f"inventory lookup failed: {exc}")
            inventory = InventoryEvidence(f"Lookup failed: {exc}", False, 0.0, 0.0)

    pricing = PricingEvidence("Pricing lookup tool unavailable.", ())
    if READ_ONLY_RFQ_TOOLS["pricing"] in tools:
        tool_name = READ_ONLY_RFQ_TOOLS["pricing"]
        called.append(tool_name)
        try:
            pricing = _summarize_pricing(
                _load_json_payload(caller(tool_name, {"part_number": parsed.part_number, "limit": 8}))
            )
        except Exception as exc:
            errors.append(f"pricing lookup failed: {exc}")
            pricing = PricingEvidence(f"Lookup failed: {exc}", ())
    if not pricing.sales and context_sale and context_sale.price is not None:
        recency, weight = _sale_recency(context_sale.date)
        pricing = PricingEvidence(
            f"Context prior sale {context_sale.order} {context_sale.date} {context_sale.customer}: {context_sale.price:g}",
            (
                SaleEvidence(
                    context_sale.price,
                    context_sale.order,
                    context_sale.date,
                    context_sale.customer,
                    recency,
                    weight,
                ),
            ),
        )

    decision = _make_pricing_decision(
        parsed,
        inventory=inventory,
        pricing=pricing,
        pricing_modifier=pricing_modifier,
    )

    return RFQFastPathResult(
        response=_format_quote_packet(
            parsed,
            customer_summary=customer_summary,
            customer_name=customer_name,
            inventory=inventory,
            pricing=pricing,
            decision=decision,
            tool_errors=errors,
            context_summary=context_summary,
            inferred_fields=inferred_fields,
        ),
        parsed=parsed,
        tool_calls=tuple(called),
        elapsed_seconds=time.perf_counter() - started,
    )


def _build_contextual_rfq_response(
    context: ContextualRFQ,
    *,
    tools: set[str],
    caller: Callable[[str, dict[str, Any]], str],
    called: list[str],
    errors: list[str],
    started: float,
) -> RFQFastPathResult:
    placeholder = ParsedRFQ(
        customer=context.customer,
        part_number="",
        qty=context.qty,
        condition=context.condition,
        target_price=context.target_price,
        free_text=context.free_text,
    )
    required_missing = [field for field in context.missing_fields if field in {"customer", "part_category"}]
    if required_missing:
        return RFQFastPathResult(
            response=_context_missing_response(context),
            parsed=placeholder,
            tool_calls=tuple(called),
            elapsed_seconds=time.perf_counter() - started,
        )

    sales_tool = READ_ONLY_RFQ_TOOLS["pricing"]
    if sales_tool not in tools:
        return RFQFastPathResult(
            response=_context_tools_missing_response(context, sales_tool),
            parsed=placeholder,
            tool_calls=tuple(called),
            elapsed_seconds=time.perf_counter() - started,
        )

    called.append(sales_tool)
    try:
        sales_payload = _load_json_payload(caller(sales_tool, {"customer": context.customer, "limit": 12}))
    except Exception as exc:
        errors.append(f"context sales lookup failed: {exc}")
        return RFQFastPathResult(
            response="\n".join([
                "Contextual RFQ fast path",
                "",
                f"Sales lookup failed: {exc}",
                "Next available action: send the PN, or retry after sales lookup is healthy.",
                "No customer send, V11 write, Atlas write, or destructive shell action was performed.",
            ]),
            parsed=placeholder,
            tool_calls=tuple(called),
            elapsed_seconds=time.perf_counter() - started,
        )

    matches = _find_contextual_sales_matches(sales_payload, context)
    if not matches and not _sales_payload_has_line_details(sales_payload):
        matches = _contextual_sales_matches_from_pricing_export(context)
    if not matches:
        return RFQFastPathResult(
            response=_context_no_match_response(context),
            parsed=placeholder,
            tool_calls=tuple(called),
            elapsed_seconds=time.perf_counter() - started,
        )
    if len(matches) > 1 and matches[0].score - matches[1].score < 2.0:
        return RFQFastPathResult(
            response=_context_ambiguous_response(context, matches),
            parsed=placeholder,
            tool_calls=tuple(called),
            elapsed_seconds=time.perf_counter() - started,
        )

    match = matches[0]
    resolved_qty = context.qty or "1"
    resolved_condition = context.condition or match.condition
    inferred_fields = [InferredField("part_number", match.part_number, "from prior sale")]
    if not context.qty:
        inferred_fields.append(InferredField("qty", resolved_qty, "default"))
    if not context.condition:
        if not resolved_condition:
            return RFQFastPathResult(
                response=_context_missing_response(context),
                parsed=placeholder,
                tool_calls=tuple(called),
                elapsed_seconds=time.perf_counter() - started,
            )
        inferred_fields.append(InferredField("condition", resolved_condition, "from last sale"))
    resolved = ParsedRFQ(
        customer=context.customer,
        part_number=match.part_number,
        qty=resolved_qty,
        condition=resolved_condition,
        target_price=context.target_price,
        free_text=context.free_text,
    )
    context_summary = f"Matched prior sale {_sales_match_line(match)}"
    return _run_standard_rfq_lookup(
        resolved,
        tools=tools,
        caller=caller,
        called=called,
        errors=errors,
        started=started,
        context_summary=context_summary,
        context_sale=match,
        inferred_fields=tuple(inferred_fields),
        pricing_modifier=context.pricing_modifier,
    )


def build_rfq_fast_path_response(
    text: str,
    *,
    task_id: str = "rfq-fast-path",
    available_tools: set[str] | None = None,
    tool_caller: Callable[[str, dict[str, Any]], str] | None = None,
) -> RFQFastPathResult | None:
    started = time.perf_counter()
    parsed = parse_rfq_prompt(text)
    contextual = parse_contextual_rfq_prompt(text)
    tools = available_tools if available_tools is not None else None
    caller = tool_caller or (lambda name, args: _call_read_only_tool(name, args, task_id=task_id))
    called: list[str] = []
    errors: list[str] = []

    if contextual is not None and (parsed is None or "part_number" in parsed.missing_fields):
        if tools is None:
            tools = _available_tool_names()
        return _build_contextual_rfq_response(
            contextual,
            tools=tools,
            caller=caller,
            called=called,
            errors=errors,
            started=started,
        )

    if parsed is None:
        return None
    if not parsed.sufficient_for_lookup:
        return RFQFastPathResult(
            response=_missing_response(parsed),
            parsed=parsed,
            tool_calls=(),
            elapsed_seconds=time.perf_counter() - started,
        )

    if tools is None:
        tools = _available_tool_names()
    required_tool_names = list(READ_ONLY_RFQ_TOOLS.values())
    missing_tools = [name for name in required_tool_names if name not in tools]
    if len(missing_tools) == len(required_tool_names):
        return RFQFastPathResult(
            response=_tools_missing_response(parsed, missing_tools),
            parsed=parsed,
            tool_calls=(),
            elapsed_seconds=time.perf_counter() - started,
        )

    return _run_standard_rfq_lookup(
        parsed,
        tools=tools,
        caller=caller,
        called=called,
        errors=errors,
        started=started,
        pricing_modifier=contextual.pricing_modifier if contextual is not None else "",
    )
