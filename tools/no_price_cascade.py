"""No-price cascade orchestrator (SDA-003).

Pure function that walks every pricing source in order for a given part
number context and returns the first successful hit with provenance.
Sources are injected as callables so this module has no network, no global
state, and no side effects — making it trivially unit-testable.

Cascade order:
    1. V11 pricing engine      (GREEN)
    2. Summit consignment sheet (GREEN)
    3. Gmail quote history      (YELLOW, requires >= history_min_matches)
    4. Outbound solicit         (YELLOW, SKIPPED when PN is Summit-associated)
    5. Manual Telegram ask      (YELLOW)
    6. Total miss               (RED, source=None)

A PN is "Summit-associated" iff step 2 returned a non-None dict, even if
its ``cost_basis`` was None — the sheet knowing about the PN means Summit
owns the customer relationship, so we never solicit externally.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


class CascadeStep(str, Enum):
    V11_ENGINE = "v11_engine"
    SUMMIT_SHEET = "summit_sheet"
    GMAIL_HISTORY = "gmail_history"
    OUTBOUND_SOLICIT = "outbound_solicit"
    MANUAL_TELEGRAM = "manual_telegram"


class ConfidenceFlag(str, Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


@dataclass
class CascadeResult:
    pn: str
    cost_basis: Optional[float]
    source: Optional[CascadeStep]
    confidence: ConfidenceFlag
    provenance: dict = field(default_factory=dict)
    sheet_stale: bool = False


@dataclass
class CascadeContext:
    pn: str
    customer: Optional[str] = None
    condition: Optional[str] = None


# Source callable signatures. Each returns Optional[dict] where None means
# "miss, try the next source".
EngineSource = Callable[["CascadeContext"], Optional[dict]]
SheetSource = Callable[[str], Optional[dict]]
HistorySource = Callable[["CascadeContext"], Optional[dict]]
SolicitSource = Callable[["CascadeContext"], Optional[dict]]
ManualSource = Callable[["CascadeContext"], Optional[dict]]


def _provenance_with_step(base: Optional[dict], step: CascadeStep) -> dict:
    merged = dict(base) if base else {}
    merged["step"] = step.value
    return merged


def run_cascade(
    ctx: CascadeContext,
    engine: EngineSource,
    sheet: SheetSource,
    history: HistorySource,
    solicit: SolicitSource,
    manual: ManualSource,
    history_min_matches: int = 2,
) -> CascadeResult:
    """Run the no-price cascade for a single part lookup.

    Short-circuits on the first successful source. Summit-associated PNs
    (those where ``sheet`` returned any non-None dict) skip the outbound
    solicit step and fall through to manual Telegram instead.
    """

    # Step 1 — V11 pricing engine
    engine_result = engine(ctx)
    if engine_result is not None and engine_result.get("cost_basis") is not None:
        return CascadeResult(
            pn=ctx.pn,
            cost_basis=engine_result["cost_basis"],
            source=CascadeStep.V11_ENGINE,
            confidence=ConfidenceFlag.GREEN,
            provenance=_provenance_with_step(
                engine_result.get("provenance"), CascadeStep.V11_ENGINE
            ),
        )

    # Step 2 — Summit consignment sheet
    sheet_result = sheet(ctx.pn)
    summit_associated = sheet_result is not None
    if summit_associated and sheet_result.get("cost_basis") is not None:
        return CascadeResult(
            pn=ctx.pn,
            cost_basis=sheet_result["cost_basis"],
            source=CascadeStep.SUMMIT_SHEET,
            confidence=ConfidenceFlag.GREEN,
            provenance=_provenance_with_step(
                sheet_result.get("provenance"), CascadeStep.SUMMIT_SHEET
            ),
            sheet_stale=bool(sheet_result.get("sheet_stale", False)),
        )

    # Step 3 — Gmail quote history (needs enough matches to trust)
    history_result = history(ctx)
    if (
        history_result is not None
        and history_result.get("match_count", 0) >= history_min_matches
    ):
        return CascadeResult(
            pn=ctx.pn,
            cost_basis=history_result.get("weighted_median"),
            source=CascadeStep.GMAIL_HISTORY,
            confidence=ConfidenceFlag.YELLOW,
            provenance=_provenance_with_step(
                history_result.get("provenance"), CascadeStep.GMAIL_HISTORY
            ),
        )

    # Step 4 — Outbound solicit (SKIPPED for Summit-associated PNs)
    if not summit_associated:
        solicit_result = solicit(ctx)
        if solicit_result is not None:
            return CascadeResult(
                pn=ctx.pn,
                cost_basis=solicit_result.get("cost_basis"),
                source=CascadeStep.OUTBOUND_SOLICIT,
                confidence=ConfidenceFlag.YELLOW,
                provenance=_provenance_with_step(
                    solicit_result.get("provenance"), CascadeStep.OUTBOUND_SOLICIT
                ),
            )

    # Step 5 — Manual Telegram ask
    manual_result = manual(ctx)
    if manual_result is not None:
        return CascadeResult(
            pn=ctx.pn,
            cost_basis=manual_result.get("cost_basis"),
            source=CascadeStep.MANUAL_TELEGRAM,
            confidence=ConfidenceFlag.YELLOW,
            provenance=_provenance_with_step(
                manual_result.get("provenance"), CascadeStep.MANUAL_TELEGRAM
            ),
        )

    # Step 6 — Total miss
    return CascadeResult(
        pn=ctx.pn,
        cost_basis=None,
        source=None,
        confidence=ConfidenceFlag.RED,
        provenance={"error": "all sources returned None"},
    )
