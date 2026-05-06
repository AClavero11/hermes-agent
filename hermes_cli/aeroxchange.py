"""Aeroxchange RFQ browser draft workflow.

This module is intentionally draft-only. It can parse Aeroxchange RFQ page
text or browser snapshots and create an evidence package, but it never
submits, sends, awards, declines, or writes back to any external system.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home


AEROXCHANGE_WORKFLOW_ID = "aeroxchange-rfq-browser-draft"
AEROXCHANGE_REPORT_ROOT = "aeroxchange/reports"
AEROXCHANGE_GUARDRAILS: tuple[str, ...] = (
    "Draft-only mode: navigate, snapshot, extract, and prepare response fields only.",
    "No Aeroxchange submit, send, award, decline, or portal write is executed without operator approval.",
    "No customer sends, vendor sends, V11 writes, Atlas writes, or price commits are executed.",
    "Capture a before-submit screenshot and approval record before any external action.",
)
AEROXCHANGE_BLOCKED_ACTIONS: tuple[str, ...] = (
    "submit_quote",
    "send_response",
    "award",
    "decline",
    "update_portal_record",
    "write_v11",
    "write_atlas",
)
REQUIRED_RFQ_FIELDS: tuple[str, ...] = ("customer", "part_number", "quantity", "condition")

_CONDITION_CODES = {
    "AR",
    "AS",
    "BER",
    "EX",
    "FN",
    "NE",
    "NS",
    "OH",
    "RP",
    "SV",
    "TEST",
}
_NEXT_LABEL_RE = re.compile(
    r"\s+\b(?:rfq|buyer|customer|company|requester|part|pn|p/n|qty|quantity|"
    r"condition|cond|description|desc|due|need by|priority|notes?)\b\s*[:=#]?",
    re.IGNORECASE,
)
_HTML_BREAK_RE = re.compile(r"</(?:tr|div|p|li|h[1-6])>|<br\s*/?>", re.IGNORECASE)
_HTML_CELL_RE = re.compile(r"</(?:td|th)>|<(?:td|th)\b[^>]*>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_RFQ_START_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:rfq|request\s+for\s+quote|quote\s+request)\b",
    re.IGNORECASE,
)
_LINE_RFX_ID_RE = re.compile(r"^\s*(?:[-*]\s*)?(?:AX|AEX|AEROX)[-_/ ]?\d{3,}\b", re.IGNORECASE)
_DATE_RE = re.compile(
    r"\b(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4}|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{2,4})\b",
    re.IGNORECASE,
)
_PART_TOKEN_RE = re.compile(r"^[A-Z0-9][A-Z0-9./_-]{2,24}$", re.IGNORECASE)
_QUANTITY_TOKEN_RE = re.compile(r"^\d+(?:\.\d+)?$")

_FIELD_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "rfq_id": (
        re.compile(
            r"\b(?:rfq|request\s+for\s+quote|quote\s+request)"
            r"(?:\s*(?:number|no\.?|id|#))?\s*[:#-]?\s*([A-Z0-9][A-Z0-9._/-]{2,})\b",
            re.IGNORECASE,
        ),
        re.compile(r"\b((?:AX|AEX|AEROX)[-_/ ]?\d{3,})\b", re.IGNORECASE),
    ),
    "customer": (
        re.compile(r"\b(?:buyer|customer|company|requester)\s*[:=#]\s*([^|\n;]+)", re.IGNORECASE),
    ),
    "part_number": (
        re.compile(
            r"\b(?:part(?:\s*(?:number|no\.?|#))?|pn|p/n)\s*[:=#]?\s*([A-Z0-9][A-Z0-9./_-]{1,})\b",
            re.IGNORECASE,
        ),
    ),
    "description": (
        re.compile(r"\b(?:description|desc)\s*[:=#]\s*([^|\n;]+)", re.IGNORECASE),
    ),
    "quantity": (
        re.compile(r"\b(?:qty|quantity|qnty|quan)\s*[:=#]?\s*(\d+(?:\.\d+)?)\b", re.IGNORECASE),
    ),
    "condition": (
        re.compile(r"\b(?:condition|cond)\s*[:=#]?\s*([A-Z]{1,4})\b", re.IGNORECASE),
    ),
    "due_date": (
        re.compile(
            r"\b(?:due(?:\s+date)?|response\s+due|need\s+by|required\s+by)\s*[:=#]\s*([^|\n;]+)",
            re.IGNORECASE,
        ),
    ),
    "priority": (
        re.compile(r"\bpriority\s*[:=#]\s*([^|\n;]+)", re.IGNORECASE),
    ),
    "notes": (
        re.compile(r"\bnotes?\s*[:=#]\s*([^|\n;]+)", re.IGNORECASE),
    ),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_snapshot_text(text: str) -> str:
    value = html.unescape(str(text or ""))
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    value = _HTML_BREAK_RE.sub("\n", value)
    value = _HTML_CELL_RE.sub(" | ", value)
    value = _HTML_TAG_RE.sub(" ", value)
    lines = [_WHITESPACE_RE.sub(" ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def _trim_at_next_label(value: str) -> str:
    match = _NEXT_LABEL_RE.search(value)
    if match:
        return value[: match.start()]
    return value


def _clean_value(value: str) -> str:
    cleaned = _trim_at_next_label(str(value or ""))
    cleaned = cleaned.strip().strip("`'\"[](){}")
    cleaned = cleaned.strip(" .,:;|-")
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


def _clean_part_number(value: str) -> str:
    cleaned = _clean_value(value).upper().replace(" ", "")
    cleaned = cleaned.strip(" .,:;|")
    return cleaned


def _extract_field(block: str, field: str) -> str:
    for pattern in _FIELD_PATTERNS[field]:
        match = pattern.search(block)
        if match:
            value = _clean_value(match.group(1))
            if field == "part_number":
                return _clean_part_number(value)
            if field == "condition":
                return value.upper()
            return value
    return ""


def _split_delimited_tokens(block: str) -> list[str]:
    tokens: list[str] = []
    for line in block.splitlines():
        if "|" in line or "\t" in line:
            parts = re.split(r"\s*\|\s*|\t+", line)
        else:
            parts = [line]
        for part in parts:
            token = _clean_value(part)
            if token:
                tokens.append(token)
    return tokens


def _looks_like_date(value: str) -> bool:
    if _DATE_RE.search(value):
        return True
    return bool(re.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", value.strip()))


def _looks_like_rfq_id(value: str) -> bool:
    cleaned = _clean_value(value)
    return bool(
        re.fullmatch(r"(?:RFQ[-_ ]*)?(?:AX|AEX|AEROX)[-_/ ]?\d{3,}", cleaned, re.IGNORECASE)
        or re.fullmatch(r"RFQ[-_/ ]?[A-Z0-9._/-]{3,}", cleaned, re.IGNORECASE)
    )


def _token_has_field_label(value: str) -> bool:
    return bool(_NEXT_LABEL_RE.search(f" {value}"))


def _first_part_like_token(tokens: list[str], *, rfq_id: str = "") -> str:
    rfq_clean = _clean_part_number(rfq_id)
    for token in tokens:
        cleaned = _clean_part_number(token)
        if not cleaned or cleaned == rfq_clean:
            continue
        if cleaned.upper() in _CONDITION_CODES:
            continue
        if _looks_like_date(cleaned) or _looks_like_rfq_id(cleaned):
            continue
        if _QUANTITY_TOKEN_RE.fullmatch(cleaned):
            continue
        if _PART_TOKEN_RE.fullmatch(cleaned) and any(char.isdigit() for char in cleaned):
            return cleaned
    return ""


def _first_condition_token(tokens: list[str]) -> str:
    for token in tokens:
        cleaned = _clean_value(token).upper()
        if cleaned in _CONDITION_CODES:
            return cleaned
    return ""


def _first_due_date(tokens: list[str]) -> str:
    for token in tokens:
        match = _DATE_RE.search(token)
        if match:
            return _clean_value(match.group(0))
    return ""


def _first_customer_like_token(tokens: list[str], *, rfq_id: str, part_number: str) -> str:
    blocked = {
        _clean_value(rfq_id).upper(),
        _clean_part_number(part_number),
        "RFQ",
        "QTY",
        "QUANTITY",
        "CONDITION",
        "COND",
        "PN",
        "P/N",
        "PART",
    }
    for token in tokens:
        cleaned = _clean_value(token)
        upper = cleaned.upper()
        if not cleaned or upper in blocked:
            continue
        if _token_has_field_label(cleaned):
            continue
        if _looks_like_rfq_id(cleaned) or _looks_like_date(cleaned):
            continue
        if _clean_part_number(cleaned) == _clean_part_number(part_number):
            continue
        if upper in _CONDITION_CODES or _QUANTITY_TOKEN_RE.fullmatch(cleaned):
            continue
        if any(char.isalpha() for char in cleaned) and len(cleaned) >= 3:
            return cleaned
    return ""


def _split_candidate_blocks(text: str) -> list[str]:
    normalized = _normalize_snapshot_text(text)
    if not normalized:
        return []
    lines = normalized.splitlines()
    blocks: list[str] = []
    current: list[str] = []
    for line in lines:
        starts_rfq = bool(_RFQ_START_RE.search(line) or _LINE_RFX_ID_RE.search(line))
        if current and starts_rfq:
            blocks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))

    if len(blocks) == 1:
        split = re.split(
            r"(?=\b(?:RFQ|Request\s+for\s+Quote|Quote\s+Request)\s*(?:#|No\.?|Number|ID|:)\s*[A-Z0-9])",
            normalized,
            flags=re.IGNORECASE,
        )
        split = [item.strip() for item in split if item.strip()]
        if len(split) > 1:
            blocks = split
    return blocks


def _rfq_from_block(block: str, index: int) -> dict[str, Any] | None:
    tokens = _split_delimited_tokens(block)
    rfq_id = _extract_field(block, "rfq_id")
    customer = _extract_field(block, "customer")
    part_number = _extract_field(block, "part_number")
    description = _extract_field(block, "description")
    quantity = _extract_field(block, "quantity")
    condition = _extract_field(block, "condition")
    due_date = _extract_field(block, "due_date")
    priority = _extract_field(block, "priority")
    notes = _extract_field(block, "notes")

    if not rfq_id:
        for token in tokens:
            if _looks_like_rfq_id(token):
                rfq_id = _clean_value(token)
                break
    if not part_number:
        part_number = _first_part_like_token(tokens, rfq_id=rfq_id)
    if not condition:
        condition = _first_condition_token(tokens)
    if not due_date:
        due_date = _first_due_date(tokens)
    if not quantity:
        for token in tokens:
            if re.search(r"\b(?:qty|quantity)\b", token, re.IGNORECASE):
                match = re.search(r"\d+(?:\.\d+)?", token)
                if match:
                    quantity = match.group(0)
                    break
    if not customer:
        customer = _first_customer_like_token(tokens, rfq_id=rfq_id, part_number=part_number)

    if not (rfq_id or part_number):
        return None

    rfq = {
        "index": index,
        "rfq_id": rfq_id,
        "customer": customer,
        "part_number": part_number,
        "description": description,
        "quantity": quantity,
        "condition": condition,
        "due_date": due_date,
        "priority": priority,
        "notes": notes,
        "source_excerpt": block[:800],
    }
    missing = [field for field in REQUIRED_RFQ_FIELDS if not str(rfq.get(field) or "").strip()]
    rfq["missing_fields"] = missing
    rfq["confidence"] = round((len(REQUIRED_RFQ_FIELDS) - len(missing)) / len(REQUIRED_RFQ_FIELDS), 2)
    return rfq


def parse_aeroxchange_rfq_text(text: str) -> list[dict[str, Any]]:
    """Parse RFQ records from Aeroxchange page text or browser snapshots."""
    rfqs: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for index, block in enumerate(_split_candidate_blocks(text), start=1):
        rfq = _rfq_from_block(block, index)
        if not rfq:
            continue
        key = (
            str(rfq.get("rfq_id") or "").upper(),
            str(rfq.get("customer") or "").upper(),
            str(rfq.get("part_number") or "").upper(),
            str(rfq.get("quantity") or ""),
            str(rfq.get("condition") or "").upper(),
        )
        if key in seen:
            continue
        seen.add(key)
        rfq["index"] = len(rfqs) + 1
        rfqs.append(rfq)
    return rfqs


def _missing_fields_summary(rfqs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "rfq_id": rfq.get("rfq_id") or f"row-{rfq.get('index')}",
            "part_number": rfq.get("part_number") or "",
            "missing_fields": list(rfq.get("missing_fields") or []),
        }
        for rfq in rfqs
        if rfq.get("missing_fields")
    ]


def build_aeroxchange_browser_actions(*, source_url: str = "") -> list[dict[str, Any]]:
    """Return the safe browser action plan without executing portal writes."""
    actions = [
        {
            "step": 1,
            "action": "navigate",
            "tool": "browser_navigate",
            "intent": "Open the Aeroxchange RFQ queue or the supplied RFQ URL.",
            "source_url": source_url,
            "approval_required": False,
            "executes_external_write": False,
        },
        {
            "step": 2,
            "action": "snapshot",
            "tool": "browser_snapshot",
            "intent": "Capture the RFQ list/detail page for deterministic parsing.",
            "approval_required": False,
            "executes_external_write": False,
        },
        {
            "step": 3,
            "action": "extract",
            "tool": "aeroxchange_parser",
            "intent": "Extract RFQ ID, customer, part number, quantity, condition, due date, and notes.",
            "approval_required": False,
            "executes_external_write": False,
        },
        {
            "step": 4,
            "action": "draft_fill",
            "tool": "browser_type",
            "intent": "Fill response fields only after quote support exists; do not submit.",
            "approval_required": True,
            "executes_external_write": False,
        },
        {
            "step": 5,
            "action": "screenshot_before_submit",
            "tool": "browser_screenshot",
            "intent": "Capture final portal state for operator approval before any external action.",
            "approval_required": True,
            "executes_external_write": False,
        },
    ]
    return actions


def build_aeroxchange_draft_package(
    snapshot_text: str,
    *,
    source_url: str = "",
    browser_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a draft-only Aeroxchange RFQ package from snapshot text."""
    rfqs = parse_aeroxchange_rfq_text(snapshot_text)
    return {
        "workflow_id": AEROXCHANGE_WORKFLOW_ID,
        "mode": "draft_only",
        "generated_at": _now_iso(),
        "source": {
            "source_url": source_url,
            "snapshot_chars": len(snapshot_text or ""),
            "browser_state": browser_state or {},
        },
        "summary": {
            "rfq_count": len(rfqs),
            "complete_rfq_count": sum(1 for rfq in rfqs if not rfq.get("missing_fields")),
            "missing_field_count": sum(len(rfq.get("missing_fields") or []) for rfq in rfqs),
        },
        "rfqs": rfqs,
        "missing_fields": _missing_fields_summary(rfqs),
        "browser_actions": build_aeroxchange_browser_actions(source_url=source_url),
        "blocked_actions": list(AEROXCHANGE_BLOCKED_ACTIONS),
        "guardrails": list(AEROXCHANGE_GUARDRAILS),
        "approval_required": {
            "before_submit": True,
            "before_customer_send": True,
            "before_v11_or_atlas_write": True,
        },
    }


def _markdown_cell(value: Any) -> str:
    text = str(value or "").replace("\n", " ").replace("|", "\\|")
    return _WHITESPACE_RE.sub(" ", text).strip()


def render_aeroxchange_draft_package(package: dict[str, Any]) -> str:
    """Render a draft package as operator-readable markdown."""
    source = package.get("source") if isinstance(package.get("source"), dict) else {}
    summary = package.get("summary") if isinstance(package.get("summary"), dict) else {}
    rfqs = package.get("rfqs") if isinstance(package.get("rfqs"), list) else []
    lines = [
        "# Aeroxchange RFQ Draft Package",
        "",
        f"Generated: {package.get('generated_at') or _now_iso()}",
        f"Mode: {package.get('mode') or 'draft_only'}",
        f"Workflow: {package.get('workflow_id') or AEROXCHANGE_WORKFLOW_ID}",
        f"Source URL: {source.get('source_url') or 'not provided'}",
        "",
        "## Summary",
        "",
        f"- RFQs parsed: {summary.get('rfq_count', len(rfqs))}",
        f"- Complete RFQs: {summary.get('complete_rfq_count', 0)}",
        f"- Missing fields: {summary.get('missing_field_count', 0)}",
        "",
        "## Guardrails",
        "",
    ]
    for guardrail in package.get("guardrails") or AEROXCHANGE_GUARDRAILS:
        lines.append(f"- {guardrail}")

    lines.extend([
        "",
        "## RFQs",
        "",
        "| # | RFQ | Customer | Part | Qty | Cond | Due | Missing |",
        "|---|-----|----------|------|-----|------|-----|---------|",
    ])
    if not rfqs:
        lines.append("| - | - | - | - | - | - | - | no RFQs parsed |")
    for rfq in rfqs:
        missing = ", ".join(rfq.get("missing_fields") or []) or "-"
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown_cell(rfq.get("index")),
                    _markdown_cell(rfq.get("rfq_id") or "-"),
                    _markdown_cell(rfq.get("customer") or "-"),
                    _markdown_cell(rfq.get("part_number") or "-"),
                    _markdown_cell(rfq.get("quantity") or "-"),
                    _markdown_cell(rfq.get("condition") or "-"),
                    _markdown_cell(rfq.get("due_date") or "-"),
                    _markdown_cell(missing),
                ]
            )
            + " |"
        )

    lines.extend(["", "## Browser Action Plan", ""])
    for action in package.get("browser_actions") or []:
        approval = "approval required" if action.get("approval_required") else "no approval required"
        lines.append(
            f"- {action.get('step')}. {action.get('action')} via {action.get('tool')}: "
            f"{action.get('intent')} ({approval}; external write: no)"
        )

    lines.extend(["", "## Blocked Actions", ""])
    for action in package.get("blocked_actions") or AEROXCHANGE_BLOCKED_ACTIONS:
        lines.append(f"- {action}")
    return "\n".join(lines) + "\n"


def build_aeroxchange_browser_readiness() -> dict[str, Any]:
    """Describe what Hermes can safely do with Aeroxchange browser control."""
    return {
        "workflow_id": AEROXCHANGE_WORKFLOW_ID,
        "status": "draft_ready",
        "control_surface": "browser harness/CDP when connected; pasted snapshot text otherwise",
        "trusted_for": [
            "navigate",
            "snapshot",
            "parse RFQ details",
            "prepare draft package",
            "capture before-submit evidence",
        ],
        "not_trusted_for_without_approval": list(AEROXCHANGE_BLOCKED_ACTIONS),
        "guardrails": list(AEROXCHANGE_GUARDRAILS),
        "next_action": "Use /aero draft <Aeroxchange RFQ text or browser snapshot>; submit remains approval-gated.",
    }


def format_aeroxchange_readiness(readiness: dict[str, Any] | None = None) -> str:
    data = readiness or build_aeroxchange_browser_readiness()
    lines = [
        "Aeroxchange Browser Harness",
        f"Status: {data.get('status')}",
        f"Workflow: {data.get('workflow_id')}",
        f"Control: {data.get('control_surface')}",
        "",
        "Trusted for:",
    ]
    for item in data.get("trusted_for") or []:
        lines.append(f"- {item}")
    lines.extend(["", "Approval-gated actions:"])
    for item in data.get("not_trusted_for_without_approval") or []:
        lines.append(f"- {item}")
    lines.extend(["", "Guardrails:"])
    for item in data.get("guardrails") or []:
        lines.append(f"- {item}")
    lines.extend(["", f"Next: {data.get('next_action')}"])
    return "\n".join(lines)


def _write_report_files(root: Path, package: dict[str, Any]) -> dict[str, str]:
    reports_dir = root / AEROXCHANGE_REPORT_ROOT
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = f"aeroxchange-rfq-draft-{stamp}"
    json_path = reports_dir / f"{base}.json"
    markdown_path = reports_dir / f"{base}.md"
    json_path.write_text(
        json.dumps(package, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_aeroxchange_draft_package(package), encoding="utf-8")
    shutil.copyfile(json_path, reports_dir / "latest.json")
    shutil.copyfile(markdown_path, reports_dir / "latest.md")
    return {
        "json": str(json_path),
        "markdown": str(markdown_path),
        "latest_json": str(reports_dir / "latest.json"),
        "latest_markdown": str(reports_dir / "latest.md"),
    }


def run_aeroxchange_draft_from_snapshot(
    snapshot_text: str,
    *,
    hermes_home: Path | None = None,
    workspace_store: Any | None = None,
    source: str = "aeroxchange-draft",
    source_url: str = "",
    browser_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create Aeroxchange draft report files and Workspace evidence."""
    root = Path(hermes_home) if hermes_home is not None else get_hermes_home()
    package = build_aeroxchange_draft_package(
        snapshot_text,
        source_url=source_url,
        browser_state=browser_state,
    )
    paths = _write_report_files(root, package)

    if workspace_store is None:
        from hermes_cli.workspace import WorkspaceStore

        workspace_store = WorkspaceStore(root / "workspace" / "control_plane.json")

    task = workspace_store.create_task(
        f"Aeroxchange RFQ draft package ({package['summary']['rfq_count']} RFQs)",
        owner="sales-rfq",
        priority="normal",
        project="rfq",
        source=source,
        status="in_progress",
        next_action="Operator approval required before Aeroxchange submit, send, V11 write, or Atlas write.",
        note="Draft-only browser workflow evidence created.",
    )
    evidence = workspace_store.add_evidence(
        task_id=task["id"],
        title="Aeroxchange RFQ draft package",
        locator=paths["markdown"],
        summary=(
            f"Parsed {package['summary']['rfq_count']} RFQs; "
            f"{package['summary']['missing_field_count']} missing fields; no submit/send executed."
        ),
        kind="report",
        metadata={
            "workflow_id": AEROXCHANGE_WORKFLOW_ID,
            "mode": "draft_only",
            "json_path": paths["json"],
            "rfq_count": package["summary"]["rfq_count"],
            "blocked_actions": list(AEROXCHANGE_BLOCKED_ACTIONS),
        },
    )
    task = workspace_store.update_task(
        task["id"],
        status="done",
        next_action="Review draft package and approve explicitly before any Aeroxchange submit/send.",
    )
    return {
        "package": package,
        "paths": paths,
        "workspace": {
            "task": task,
            "evidence": evidence,
        },
    }


def format_aeroxchange_draft_run(result: dict[str, Any]) -> str:
    package = result.get("package") if isinstance(result.get("package"), dict) else {}
    summary = package.get("summary") if isinstance(package.get("summary"), dict) else {}
    paths = result.get("paths") if isinstance(result.get("paths"), dict) else {}
    workspace = result.get("workspace") if isinstance(result.get("workspace"), dict) else {}
    task = workspace.get("task") if isinstance(workspace.get("task"), dict) else {}
    lines = [
        "Aeroxchange draft package created",
        f"RFQs parsed: {summary.get('rfq_count', 0)}",
        f"Complete RFQs: {summary.get('complete_rfq_count', 0)}",
        f"Missing fields: {summary.get('missing_field_count', 0)}",
        f"Markdown: {paths.get('markdown', '')}",
        f"JSON: {paths.get('json', '')}",
        f"Workspace task: {task.get('id', '')}",
        "",
        "No Aeroxchange submit/send, V11 write, or Atlas write executed.",
        "Next: review the draft package and approve explicitly before any external action.",
    ]
    return "\n".join(lines)
