"""Auto-quote bridge module (SWA-001).

Pure orchestration wiring the SDA tools (customer_quote_ref,
quote_append_detector, no_price_cascade, summit_sheet_tool,
summit_trace_flags) into one callable for the live auto-quote pipeline.

Design contract:
- Pure: no network, no SMTP, no Telegram, no V11 writes, no disk I/O.
- Injected deps: engine/history/solicit/manual probes passed by caller.
- Graceful failure: all exceptions caught, surfaced as warnings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

from tools.customer_quote_ref import extract_customer_quote_ref
from tools.no_price_cascade import CascadeContext, CascadeResult, run_cascade
from tools.quote_append_detector import (
    AppendConfidence,
    AppendSuggestion,
    IncomingRfq,
    OpenQuote,
    detect_append_suggestion,
)
from tools.summit_sheet_tool import summit_sheet_lookup
from tools.summit_trace_flags import QuoteLine, TraceFlags, emit_trace_flags


@dataclass
class BridgeContext:
    customer_id: str
    customer_name: str
    rfq_body: str
    pn_list: List[str]
    aircraft: Optional[str] = None
    condition_per_pn: dict = field(default_factory=dict)


@dataclass
class BridgeResult:
    cascade_results: List[CascadeResult] = field(default_factory=list)
    trace_flags: List[TraceFlags] = field(default_factory=list)
    customer_quote_ref: Optional[str] = None
    append_suggestion: Optional[AppendSuggestion] = None
    warnings: List[str] = field(default_factory=list)


def run_bridge(
    ctx: BridgeContext,
    engine_probe: Callable,
    history_probe: Callable,
    solicit_probe: Callable,
    manual_probe: Callable,
    open_quotes: Optional[List[OpenQuote]] = None,
) -> BridgeResult:
    """Orchestrate the SDA tools for one RFQ.

    Never raises. All failures surface as ``result.warnings`` entries.
    """
    result = BridgeResult()

    try:
        # Step 1: customer quote ref extraction.
        try:
            result.customer_quote_ref = extract_customer_quote_ref(ctx.rfq_body)
        except Exception as exc:  # noqa: BLE001
            result.warnings.append(
                f"customer_quote_ref extraction failed: {exc}"
            )

        # Step 2: append-suggestion short-circuit.
        if open_quotes:
            try:
                rfq = IncomingRfq(
                    customer_id=ctx.customer_id,
                    pn_list=list(ctx.pn_list),
                    customer_quote_ref=result.customer_quote_ref,
                )
                suggestion = detect_append_suggestion(rfq, open_quotes)
            except Exception as exc:  # noqa: BLE001
                result.warnings.append(
                    f"append detector failed: {exc}"
                )
                suggestion = None

            if suggestion is not None:
                if suggestion.confidence in (
                    AppendConfidence.HIGH,
                    AppendConfidence.MED,
                ):
                    result.append_suggestion = suggestion
                    return result
                if suggestion.confidence == AppendConfidence.LOW:
                    result.append_suggestion = suggestion
                # NONE: leave append_suggestion as None and continue.

        # Step 3: per-PN cascade. Deduplicate — duplicate PNs in an RFQ are
        # almost always a caller bug, and a double-entry would corrupt
        # downstream pricing.
        seen_pns: set = set()
        for pn in ctx.pn_list:
            if pn in seen_pns:
                result.warnings.append(f"duplicate pn skipped: {pn}")
                continue
            seen_pns.add(pn)
            try:
                cascade_ctx = CascadeContext(
                    pn=pn,
                    customer=ctx.customer_name,
                    condition=ctx.condition_per_pn.get(pn),
                )
                cascade_result = run_cascade(
                    cascade_ctx,
                    engine=engine_probe,
                    sheet=summit_sheet_lookup,
                    history=history_probe,
                    solicit=solicit_probe,
                    manual=manual_probe,
                )
                result.cascade_results.append(cascade_result)
            except Exception as exc:  # noqa: BLE001
                result.warnings.append(
                    f"cascade failed for pn={pn}: {exc}"
                )

        # Step 4: per-line trace flags (only for priced lines).
        for cascade_result in result.cascade_results:
            if cascade_result.cost_basis is None:
                continue
            pn = cascade_result.pn
            try:
                qline = QuoteLine(
                    pn=pn,
                    condition=ctx.condition_per_pn.get(pn) or "AR",
                )
                flags = emit_trace_flags([qline])
                result.trace_flags.extend(flags)
            except Exception as exc:  # noqa: BLE001
                result.warnings.append(
                    f"trace flags failed for pn={pn}: {exc}"
                )

    except Exception as exc:  # noqa: BLE001
        result.warnings.append(f"bridge top-level failure: {exc}")

    return result
