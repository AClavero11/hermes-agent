# Implementation Spec: SWA-001 — Build auto-quote bridge module (pure logic, no side effects)

## Objective
Create `tools/auto_quote_bridge.py`: a pure orchestration function wiring the SDA tools (customer_quote_ref, quote_append_detector, no_price_cascade, summit_sheet_tool, summit_trace_flags) into one callable for the live auto-quote pipeline. Zero I/O, zero side effects, all external deps injected or imported and mockable at the bridge's import site. Every failure surfaces as a structured warning; nothing raises past the public function.

## Acceptance Criteria (MUST satisfy all)
1. `tools/auto_quote_bridge.py` exists.
2. Exports dataclass `BridgeContext(customer_id: str, customer_name: str, rfq_body: str, pn_list: List[str], aircraft: Optional[str] = None, condition_per_pn: dict = field(default_factory=dict))`.
3. Exports dataclass `BridgeResult(cascade_results: List[CascadeResult], trace_flags: List[TraceFlags], customer_quote_ref: Optional[str], append_suggestion: Optional[AppendSuggestion], warnings: List[str])` — all defaulted via `field(default_factory=...)` or `None`.
4. Exports `run_bridge(ctx: BridgeContext, engine_probe: Callable, history_probe: Callable, solicit_probe: Callable, manual_probe: Callable, open_quotes: Optional[List[OpenQuote]] = None) -> BridgeResult`.
5. Logic order:
   1. Extract `customer_quote_ref` from `ctx.rfq_body` via `tools.customer_quote_ref.extract_customer_quote_ref`.
   2. If `open_quotes` provided, call `tools.quote_append_detector.detect_append_suggestion(IncomingRfq(customer_id=ctx.customer_id, pn_list=list(ctx.pn_list), customer_quote_ref=result.customer_quote_ref), open_quotes)`. If result is `AppendConfidence.HIGH` or `AppendConfidence.MED`: populate `append_suggestion` and **return early** (cascade_results and trace_flags stay empty). If `LOW`: populate `append_suggestion` but continue to cascade. If `NONE`: leave `append_suggestion = None` and continue.
   3. For each `pn` in `ctx.pn_list`, call `tools.no_price_cascade.run_cascade(CascadeContext(pn=pn, customer=ctx.customer_name, condition=ctx.condition_per_pn.get(pn)), engine=engine_probe, sheet=lambda p: summit_sheet_lookup(p), history=history_probe, solicit=solicit_probe, manual=manual_probe)`. Append each result to `cascade_results`. **Critical:** `sheet` takes a bare `str`, all other probes take `CascadeContext` — the lambda bridges that asymmetry.
   4. For each PN whose cascade produced a non-None `cost_basis`, build a `QuoteLine(pn=pn, condition=ctx.condition_per_pn.get(pn) or "AR")` and call `emit_trace_flags([qline])` **per line** (not batched), extending `trace_flags`. Per-line isolation is required by test 7.
6. Failure isolation:
   - Each step (customer_quote_ref, append detector, per-PN cascade, per-PN trace flags) is wrapped in its own `try/except Exception as exc` that appends to `result.warnings` and continues. One PN failing never prevents another from being processed.
   - A top-level `try/except Exception` wraps the whole body as a safety net. If the safety net fires, `warnings` gets `"bridge top-level failure: {exc}"` and `cascade_results`/`trace_flags` are left as whatever was populated at point of failure (do NOT wipe them — per-step catches should prevent the top-level ever firing in tests 6 and 7).
   - **Never re-raise.** `run_bridge` always returns a `BridgeResult`.
7. Tests in `tests/tools/test_auto_quote_bridge.py`, ≥10 tests, organized in classes matching Scout 3's layout:
   - `TestBridgeHappyPath` — test 1
   - `TestAppendShortCircuit` — tests 2, 10
   - `TestCustomerRefExtraction` — tests 3, 9
   - `TestPerPnBehaviour` — tests 4, 5
   - `TestFailureIsolation` — tests 6, 7
   - `TestEdgeCases` — test 8
8. Quality gates: `python -c "import tools.auto_quote_bridge"` clean; `pytest tests/tools/test_auto_quote_bridge.py -q` green; no warnings (`-W error` optional).

## Files to Create
- `tools/auto_quote_bridge.py`
- `tests/tools/test_auto_quote_bridge.py`

## Files to Modify
None.

## Patterns to Follow (from Scout 2)

### Module header
```python
"""Auto-quote bridge module (SWA-001).

<1-2 line purpose>

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
```

### Dataclass style
- Plain `@dataclass` (no frozen, no slots).
- `Optional[X]` (Python 3.9 compat), NOT `X | None`.
- `field(default_factory=list)` / `field(default_factory=dict)` for mutable defaults.
- No pydantic, no TypedDict.

### Error handling
- Per-step `try/except Exception as exc:` with `result.warnings.append(...)`.
- No logging (pure tool).
- Never raise from `run_bridge`.

### No logging
Pure tool — no `import logging`, no loggers.

## Test Patterns (from Scout 3)

### Test file skeleton
```python
"""Tests for tools.auto_quote_bridge (SWA-001)."""

from __future__ import annotations

from unittest.mock import Mock, patch
from datetime import datetime

import pytest

from tools.auto_quote_bridge import BridgeContext, BridgeResult, run_bridge
from tools.no_price_cascade import CascadeResult, ConfidenceFlag, CascadeStep
from tools.quote_append_detector import (
    AppendConfidence,
    AppendSuggestion,
    OpenQuote,
)
from tools.summit_trace_flags import TraceFlags


def _ctx(
    pn_list=None,
    rfq_body="Please quote these parts.",
    customer_id="CUST-1",
    customer_name="ACME",
    condition_per_pn=None,
    aircraft=None,
):
    return BridgeContext(
        customer_id=customer_id,
        customer_name=customer_name,
        rfq_body=rfq_body,
        pn_list=pn_list if pn_list is not None else ["PN-1"],
        aircraft=aircraft,
        condition_per_pn=condition_per_pn or {},
    )


def _engine_hit(pn="PN-1", cost=100.0):
    """Helper to build a CascadeResult that looks like an engine hit."""
    return CascadeResult(
        pn=pn,
        cost_basis=cost,
        source=CascadeStep.ENGINE,
        confidence=ConfidenceFlag.GREEN,
        provenance={"source": "engine"},
        sheet_stale=False,
    )


def _probes(engine=None, history=None, solicit=None, manual=None):
    return {
        "engine_probe": Mock(return_value=engine),
        "history_probe": Mock(return_value=history),
        "solicit_probe": Mock(return_value=solicit),
        "manual_probe": Mock(return_value=manual),
    }
```

### Mocking strategy
- `run_cascade`, `emit_trace_flags`, `detect_append_suggestion`, `summit_sheet_lookup`, `extract_customer_quote_ref` are all imported by name in `tools.auto_quote_bridge` — patch at `tools.auto_quote_bridge.<name>`.
- `engine_probe`/`history_probe`/`solicit_probe`/`manual_probe` are just `Mock(return_value=...)` — they're passed through to `run_cascade`. Since we patch `run_cascade` entirely, the probes are rarely inspected in most tests — tests assert on `run_cascade`'s call args.
- For test 6 (cascade failure), patch `tools.auto_quote_bridge.run_cascade` with `side_effect=RuntimeError("down")`; assert no raise, warnings populated.
- For test 7 (trace flags isolation), patch `tools.auto_quote_bridge.emit_trace_flags` with a `side_effect` function that raises for a specific PN but returns valid flags for others. Requires bridge to call `emit_trace_flags` per-line.

### 10+ test roster
1. `TestBridgeHappyPath::test_bridge_happy_path` — patch `run_cascade` to return `_engine_hit(pn)` for 2 PNs, patch `emit_trace_flags` to return a 1-element list. Assert `len(cascade_results) == 2`, `len(trace_flags) == 2`, no warnings, `customer_quote_ref is None`.
2. `TestAppendShortCircuit::test_bridge_with_append_short_circuits` — patch `detect_append_suggestion` to return `AppendSuggestion(confidence=AppendConfidence.HIGH, ...)`, pass `open_quotes=[...]`. Assert `append_suggestion is not None`, `cascade_results == []`, `trace_flags == []`. Also verify `run_cascade` was NOT called.
3. `TestCustomerRefExtraction::test_bridge_extracts_customer_ref_from_body` — `rfq_body="Ref: ABC-123 please quote"`. Patch `extract_customer_quote_ref` to return `"ABC-123"`. Assert `result.customer_quote_ref == "ABC-123"`.
4. `TestPerPnBehaviour::test_bridge_each_pn_gets_own_cascade_result` — 3 PNs, patch `run_cascade` with side_effect that returns a distinct `_engine_hit(ctx.pn)` per call. Assert `len(cascade_results) == 3` and each has the right PN.
5. `TestPerPnBehaviour::test_bridge_each_pn_gets_own_trace_flags` — 2 PNs priced, patch `emit_trace_flags` with side_effect that returns distinct TraceFlags per call. Assert `len(trace_flags) == 2` and each matches its PN.
6. `TestFailureIsolation::test_bridge_failure_in_cascade_returns_warning_not_raise` — patch `run_cascade` to `side_effect=RuntimeError("engine down")`. Assert no exception, `result.warnings` non-empty and mentions the failure, `isinstance(result, BridgeResult)`.
7. `TestFailureIsolation::test_bridge_failure_in_trace_flags_isolated` — 2 priced PNs. Patch `emit_trace_flags` with a side_effect: `lambda lines: (_ for _ in ()).throw(RuntimeError()) if lines[0].pn == "PN-BAD" else [TraceFlags(pn=lines[0].pn, ...)]`. Assert `len(trace_flags) == 1` (the good PN), warnings mentions the bad PN.
8. `TestEdgeCases::test_bridge_no_pns_returns_empty_but_valid_result` — `pn_list=[]`. Assert `result.cascade_results == []`, `result.trace_flags == []`, `result.warnings == []`, `isinstance(result, BridgeResult)`.
9. `TestCustomerRefExtraction::test_bridge_no_customer_ref_is_none` — `rfq_body="no ref here"`. Patch `extract_customer_quote_ref` to return `None`. Assert `result.customer_quote_ref is None`.
10. `TestAppendShortCircuit::test_bridge_append_suggestion_low_confidence_does_not_short_circuit` — patch `detect_append_suggestion` to return `AppendSuggestion(confidence=AppendConfidence.LOW, ...)`, 1 PN. Assert cascade was called (`run_cascade.assert_called()`) and `cascade_results` has 1 entry; `append_suggestion` is still populated (not None).

## Gotchas (critical)

1. **sheet asymmetry.** `run_cascade`'s `sheet` callable takes `str` (bare PN) while the others take `CascadeContext`. Bridge MUST wrap: `sheet=lambda p: summit_sheet_lookup(p)`.
2. **Patch at import site.** Tests patch `tools.auto_quote_bridge.run_cascade`, `tools.auto_quote_bridge.emit_trace_flags`, `tools.auto_quote_bridge.detect_append_suggestion`, `tools.auto_quote_bridge.summit_sheet_lookup`, `tools.auto_quote_bridge.extract_customer_quote_ref` — NOT at the source modules.
3. **QuoteLine construction.** `emit_trace_flags` needs `List[QuoteLine]`, not raw dicts. Import `QuoteLine` from `tools.summit_trace_flags`.
4. **Per-line trace_flags.** Must call `emit_trace_flags` once per priced line (not batched) so test 7 can inject a per-PN failure.
5. **Append detector signature.** `detect_append_suggestion(rfq, open_quotes, now=None, window_days=7)` — don't pass `now` in production (let it default); in tests just patch the function.
6. **IncomingRfq construction.** Must use `list(ctx.pn_list)` (defensive copy) when building `IncomingRfq`.
7. **`AppendConfidence.LOW`** — populates `append_suggestion` but does NOT short-circuit (cascade still runs).
8. **Never mutate `ctx`.** `BridgeContext` is the caller's — defensive-copy anything you pass through.

## Out of Scope
- Integration with `~/.hermes/services/ils_auto_quote.py` (that's SWA-002).
- Telegram callbacks (SWA-003).
- End-to-end integration tests with real externals (SWA-004).
- Narrowing `except Exception` or Jorge cost sanity fallback (SWA-005).
- Docs/runbook (SWA-006).
- Registry wiring in `tools/__init__.py` — this is a bridge module, not an LLM-invokable tool. Do NOT add to `_HERMES_CORE_TOOLS`. Do NOT import from `tools/registry.py`.
- Logging — this is a pure tool; no logger.

## Definition of Done
- `python -c "from tools.auto_quote_bridge import BridgeContext, BridgeResult, run_bridge"` works.
- `pytest tests/tools/test_auto_quote_bridge.py -q` passes with ≥10 tests.
- No changes to any file outside `tools/auto_quote_bridge.py` and `tests/tools/test_auto_quote_bridge.py`.
