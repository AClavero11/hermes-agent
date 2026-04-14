"""Summit consignment trace flag emitter.

Pure-function pipeline stage that, given a list of priced RFQ lines,
consults :func:`tools.summit_sheet_tool.summit_sheet_lookup` and emits
soft trace signals for downstream ``draft_quote`` and Telegram approval
steps.

Design notes
------------
- No hard approval locks. Jorge's 2026-04-09 standing green light on
  Summit consignment parts removes the ``requires_summit_approval``
  gate. Flags here are advisory only.
- Pure function: no network calls, no side effects, no globals beyond
  imports. All external I/O happens inside ``summit_sheet_lookup``.
- Not a registered tool. This is internal pipeline logic, invoked from
  the broader RFQ -> quote flow, not from the LLM tool-use loop.

Flag semantics
--------------
``trace_type``
    - ``"145"`` when the PN is found in the Summit sheet, regardless of
      condition. Summit is an FAA Repair Station 145.
    - ``"8130"`` when the PN is NOT in the sheet and condition is one
      of ``{SV, OH, NE, NS, New}``.
    - ``None`` when the PN is NOT in the sheet and condition is ``AR``
      (AR parts need trace documents, NOT an 8130 tag) or when the
      condition is unknown.

``idg_piece_part_warning``
    Soft warning only. Set when the PN is Summit-sourced AND the
    upstream pipeline flagged the line as an IDG piece part. The quote
    still proceeds; this surfaces for human review in Telegram.

``per_line_check_required``
    Passthrough of any ``gmail_caveat`` string that upstream
    ``mine_gmail`` attached to the line (e.g. "check w/ Sergio before
    quoting"). Downstream approval UI renders it as-is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from tools.summit_sheet_tool import summit_sheet_lookup

__all__ = [
    "QuoteLine",
    "TraceFlags",
    "emit_trace_flags",
]


# Conditions that require an 8130-3 airworthiness tag when the part is
# sourced outside the Summit consignment (i.e. no Summit 145 trace).
_EIGHT_THIRTY_CONDITIONS = frozenset({"SV", "OH", "NE", "NS", "New"})


@dataclass
class TraceFlags:
    """Soft trace signals for one priced RFQ line."""

    pn: str
    summit_consignment: bool
    trace_type: Optional[str]
    tag_source: Optional[str]
    summit_cost_recent: Optional[float]
    summit_guidance: Optional[str]
    idg_piece_part_warning: bool
    per_line_check_required: Optional[str]


@dataclass
class QuoteLine:
    """A priced RFQ line arriving into the trace-flag stage."""

    pn: str
    condition: str
    is_idg_piece_part: bool = False
    gmail_caveat: Optional[str] = None


def _classify_trace_type(
    summit_consignment: bool, condition: str
) -> Optional[str]:
    """Return the trace type label for one line."""
    if summit_consignment:
        return "145"
    if condition in _EIGHT_THIRTY_CONDITIONS:
        return "8130"
    return None


def _emit_one(line: QuoteLine) -> TraceFlags:
    """Produce trace flags for a single line."""
    lookup_result = summit_sheet_lookup(line.pn)
    summit_consignment = lookup_result is not None

    if summit_consignment:
        # Customer-facing "Summit Aerospace" tag only applies to SV
        # condition. Other conditions (AR, OH, etc.) still get the 145
        # trace and internal consignment flag, but no Summit branding
        # on the quote form.
        tag_source: Optional[str] = (
            "Summit Aerospace" if line.condition == "SV" else None
        )
        summit_cost_recent = lookup_result.get("cost_basis")
        summit_guidance = lookup_result.get("summit_guidance")
    else:
        tag_source = None
        summit_cost_recent = None
        summit_guidance = None

    trace_type = _classify_trace_type(summit_consignment, line.condition)

    return TraceFlags(
        pn=line.pn,
        summit_consignment=summit_consignment,
        trace_type=trace_type,
        tag_source=tag_source,
        summit_cost_recent=summit_cost_recent,
        summit_guidance=summit_guidance,
        idg_piece_part_warning=summit_consignment and line.is_idg_piece_part,
        per_line_check_required=line.gmail_caveat,
    )


def emit_trace_flags(lines: List[QuoteLine]) -> List[TraceFlags]:
    """For each quote line, produce trace flags by consulting Summit.

    Parameters
    ----------
    lines:
        Priced RFQ lines from the upstream pricing stage.

    Returns
    -------
    list[TraceFlags]
        One :class:`TraceFlags` per input line, in the same order.
    """
    return [_emit_one(line) for line in lines]
