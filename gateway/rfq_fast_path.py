"""Deterministic RFQ fast path for common quote requests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
import logging
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
    ]
    if tool_errors:
        lines.extend(["", "Lookup issues:"])
        lines.extend(f"- {item}" for item in tool_errors)
    return "\n".join(lines)


def build_rfq_fast_path_response(
    text: str,
    *,
    task_id: str = "rfq-fast-path",
    available_tools: set[str] | None = None,
    tool_caller: Callable[[str, dict[str, Any]], str] | None = None,
) -> RFQFastPathResult | None:
    started = time.perf_counter()
    parsed = parse_rfq_prompt(text)
    if parsed is None:
        return None
    if not parsed.sufficient_for_lookup:
        return RFQFastPathResult(
            response=_missing_response(parsed),
            parsed=parsed,
            tool_calls=(),
            elapsed_seconds=time.perf_counter() - started,
        )

    tools = available_tools if available_tools is not None else _available_tool_names()
    required_tool_names = list(READ_ONLY_RFQ_TOOLS.values())
    missing_tools = [name for name in required_tool_names if name not in tools]
    if len(missing_tools) == len(required_tool_names):
        return RFQFastPathResult(
            response=_tools_missing_response(parsed, missing_tools),
            parsed=parsed,
            tool_calls=(),
            elapsed_seconds=time.perf_counter() - started,
        )

    caller = tool_caller or (lambda name, args: _call_read_only_tool(name, args, task_id=task_id))
    called: list[str] = []
    errors: list[str] = []

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

    decision = _make_pricing_decision(parsed, inventory=inventory, pricing=pricing)

    return RFQFastPathResult(
        response=_format_quote_packet(
            parsed,
            customer_summary=customer_summary,
            customer_name=customer_name,
            inventory=inventory,
            pricing=pricing,
            decision=decision,
            tool_errors=errors,
        ),
        parsed=parsed,
        tool_calls=tuple(called),
        elapsed_seconds=time.perf_counter() - started,
    )
