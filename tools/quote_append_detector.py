"""Incremental quote append detector.

Given a newly parsed RFQ and a set of open quotes for the same customer,
decide whether the RFQ should be offered as an append to an existing open
quote rather than drafted fresh. Three signals, in priority order:

1. Exact customer_quote_ref match (HIGH)
2. Same customer + overlapping PN within a configurable window (MED)
3. Same customer + PN family overlap within the window (LOW)

Cross-customer customer_quote_ref collisions are suppressed with a warning.
Pure utility module with no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import List, Optional

# Quote states that still allow append. Anything beyond these is closed.
_OPEN_STATES = {"draft", "sent"}


class AppendConfidence(str, Enum):
    HIGH = "high"
    MED = "med"
    LOW = "low"
    NONE = "none"


@dataclass
class OpenQuote:
    quote_id: str
    customer_id: str
    client_order_ref: Optional[str]
    pn_list: List[str]
    created_at: datetime
    total: float = 0.0
    state: str = "draft"  # draft | sent


@dataclass
class IncomingRfq:
    customer_id: str
    pn_list: List[str]
    customer_quote_ref: Optional[str] = None


@dataclass
class AppendSuggestion:
    confidence: AppendConfidence
    matched_quote: Optional[OpenQuote]
    match_signal: Optional[str]  # "customer_quote_ref" | "pn_overlap" | "pn_family_overlap" | None
    warnings: List[str] = field(default_factory=list)


def extract_pn_family(pn: str) -> str:
    """Return the family prefix for a PN: strip any suffix after the LAST '-'.

    Examples:
        '273T1102-8'   -> '273T1102'
        '273T1102-5'   -> '273T1102'
        '1234'         -> '1234'          (no dash)
        'ABC-123-XYZ'  -> 'ABC-123'       (strip only the last segment)
        ''             -> ''
    """
    if not pn:
        return pn
    last_dash = pn.rfind("-")
    if last_dash == -1:
        return pn
    return pn[:last_dash]


def _pn_families(pn_list: List[str]) -> set:
    """Return the set of family prefixes for a list of PNs, skipping empties."""
    return {extract_pn_family(pn) for pn in pn_list if pn}


def _pn_set(pn_list: List[str]) -> set:
    """Return the set of non-empty PNs."""
    return {pn for pn in pn_list if pn}


def detect_append_suggestion(
    rfq: IncomingRfq,
    open_quotes: List[OpenQuote],
    now: Optional[datetime] = None,
    window_days: int = 7,
) -> AppendSuggestion:
    """Find the strongest append signal across open_quotes.

    Filters to open states within the window, then walks signals in priority
    order. Returns the strongest match found. When multiple quotes match at
    the same confidence, picks the one with the newest created_at.
    """
    if now is None:
        now = datetime.utcnow()

    warnings: List[str] = []
    cutoff = now - timedelta(days=window_days)

    # Filter: open state + created_at within the window.
    candidates = [
        quote
        for quote in open_quotes
        if quote.state in _OPEN_STATES and quote.created_at >= cutoff
    ]

    # Signal 1 (HIGH): exact customer_quote_ref match.
    if rfq.customer_quote_ref:
        ref_matches = [
            quote
            for quote in candidates
            if quote.client_order_ref == rfq.customer_quote_ref
        ]
        same_customer_ref_matches = [
            quote for quote in ref_matches if quote.customer_id == rfq.customer_id
        ]
        cross_customer_ref_matches = [
            quote for quote in ref_matches if quote.customer_id != rfq.customer_id
        ]

        if cross_customer_ref_matches and not same_customer_ref_matches:
            warnings.append(
                f"cross-customer client_order_ref collision suppressed for "
                f"rfq.customer_id={rfq.customer_id}"
            )

        if same_customer_ref_matches:
            best = max(same_customer_ref_matches, key=lambda quote: quote.created_at)
            return AppendSuggestion(
                confidence=AppendConfidence.HIGH,
                matched_quote=best,
                match_signal="customer_quote_ref",
                warnings=warnings,
            )

    rfq_pns = _pn_set(rfq.pn_list)
    rfq_families = _pn_families(rfq.pn_list)

    # Signal 2 (MED): same customer + PN overlap.
    med_candidates = []
    for quote in candidates:
        if quote.customer_id != rfq.customer_id:
            continue
        if rfq_pns & _pn_set(quote.pn_list):
            med_candidates.append(quote)

    if med_candidates:
        best = max(med_candidates, key=lambda quote: quote.created_at)
        return AppendSuggestion(
            confidence=AppendConfidence.MED,
            matched_quote=best,
            match_signal="pn_overlap",
            warnings=warnings,
        )

    # Signal 3 (LOW): same customer + PN family overlap.
    low_candidates = []
    for quote in candidates:
        if quote.customer_id != rfq.customer_id:
            continue
        if rfq_families & _pn_families(quote.pn_list):
            low_candidates.append(quote)

    if low_candidates:
        best = max(low_candidates, key=lambda quote: quote.created_at)
        return AppendSuggestion(
            confidence=AppendConfidence.LOW,
            matched_quote=best,
            match_signal="pn_family_overlap",
            warnings=warnings,
        )

    return AppendSuggestion(
        confidence=AppendConfidence.NONE,
        matched_quote=None,
        match_signal=None,
        warnings=warnings,
    )
