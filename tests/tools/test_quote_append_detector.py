"""Tests for tools.quote_append_detector."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tools.quote_append_detector import (
    AppendConfidence,
    AppendSuggestion,
    IncomingRfq,
    OpenQuote,
    detect_append_suggestion,
    extract_pn_family,
)


NOW = datetime(2026, 4, 9, 12, 0, 0)


def _quote(
    quote_id: str = "Q1",
    customer_id: str = "cust-1",
    client_order_ref=None,
    pn_list=None,
    created_at=None,
    state: str = "draft",
) -> OpenQuote:
    return OpenQuote(
        quote_id=quote_id,
        customer_id=customer_id,
        client_order_ref=client_order_ref,
        pn_list=list(pn_list or []),
        created_at=created_at or (NOW - timedelta(days=1)),
        state=state,
    )


class TestPnFamily:
    def test_basic_family(self):
        assert extract_pn_family("273T1102-8") == "273T1102"

    def test_different_suffix_same_family(self):
        assert extract_pn_family("273T1102-5") == "273T1102"

    def test_no_dash(self):
        assert extract_pn_family("1234") == "1234"

    def test_multi_segment_strips_last(self):
        assert extract_pn_family("ABC-123-XYZ") == "ABC-123"

    def test_empty_string(self):
        assert extract_pn_family("") == ""


class TestHighConfidence:
    def test_exact_ref_match_same_customer(self):
        rfq = IncomingRfq(
            customer_id="cust-1", pn_list=["PN-1"], customer_quote_ref="ABC-123"
        )
        quote = _quote(client_order_ref="ABC-123", pn_list=["PN-2"])
        result = detect_append_suggestion(rfq, [quote], now=NOW)
        assert result.confidence == AppendConfidence.HIGH
        assert result.matched_quote is quote
        assert result.match_signal == "customer_quote_ref"

    def test_exact_ref_different_customer_suppressed(self):
        rfq = IncomingRfq(
            customer_id="cust-1", pn_list=["PN-1"], customer_quote_ref="ABC-123"
        )
        quote = _quote(
            customer_id="cust-2", client_order_ref="ABC-123", pn_list=["PN-9"]
        )
        result = detect_append_suggestion(rfq, [quote], now=NOW)
        assert result.confidence != AppendConfidence.HIGH
        assert result.confidence == AppendConfidence.NONE
        assert any("cross-customer" in warning for warning in result.warnings)

    def test_ref_match_outside_window_still_counts_for_ref_signal_only(self):
        """Window filter runs BEFORE any signal matching, so a stale ref
        match is excluded and nothing triggers."""
        rfq = IncomingRfq(
            customer_id="cust-1", pn_list=["PN-1"], customer_quote_ref="ABC-123"
        )
        quote = _quote(
            client_order_ref="ABC-123",
            pn_list=["PN-1"],
            created_at=NOW - timedelta(days=30),
        )
        result = detect_append_suggestion(rfq, [quote], now=NOW)
        assert result.confidence == AppendConfidence.NONE
        assert result.matched_quote is None


class TestMedConfidence:
    def test_same_customer_overlapping_pn_within_window(self):
        rfq = IncomingRfq(customer_id="cust-1", pn_list=["PN-1", "PN-2"])
        quote = _quote(pn_list=["PN-2", "PN-99"])
        result = detect_append_suggestion(rfq, [quote], now=NOW)
        assert result.confidence == AppendConfidence.MED
        assert result.matched_quote is quote
        assert result.match_signal == "pn_overlap"

    def test_overlap_but_different_customer_no_match(self):
        rfq = IncomingRfq(customer_id="cust-1", pn_list=["PN-1"])
        quote = _quote(customer_id="cust-2", pn_list=["PN-1"])
        result = detect_append_suggestion(rfq, [quote], now=NOW)
        assert result.confidence == AppendConfidence.NONE

    def test_overlap_outside_window_no_match(self):
        rfq = IncomingRfq(customer_id="cust-1", pn_list=["PN-1"])
        quote = _quote(pn_list=["PN-1"], created_at=NOW - timedelta(days=14))
        result = detect_append_suggestion(rfq, [quote], now=NOW)
        assert result.confidence == AppendConfidence.NONE

    def test_multiple_med_candidates_picks_newest(self):
        rfq = IncomingRfq(customer_id="cust-1", pn_list=["PN-1"])
        older = _quote(
            quote_id="Q-old",
            pn_list=["PN-1"],
            created_at=NOW - timedelta(days=5),
        )
        newer = _quote(
            quote_id="Q-new",
            pn_list=["PN-1"],
            created_at=NOW - timedelta(days=1),
        )
        result = detect_append_suggestion(rfq, [older, newer], now=NOW)
        assert result.confidence == AppendConfidence.MED
        assert result.matched_quote is newer


class TestLowConfidence:
    def test_pn_family_overlap_same_customer(self):
        rfq = IncomingRfq(customer_id="cust-1", pn_list=["273T1102-8"])
        quote = _quote(pn_list=["273T1102-5"])
        result = detect_append_suggestion(rfq, [quote], now=NOW)
        assert result.confidence == AppendConfidence.LOW
        assert result.matched_quote is quote
        assert result.match_signal == "pn_family_overlap"

    def test_family_mismatch(self):
        rfq = IncomingRfq(customer_id="cust-1", pn_list=["273T1102-8"])
        quote = _quote(pn_list=["999X9999-1"])
        result = detect_append_suggestion(rfq, [quote], now=NOW)
        assert result.confidence == AppendConfidence.NONE

    def test_family_overlap_different_customer_no_match(self):
        rfq = IncomingRfq(customer_id="cust-1", pn_list=["273T1102-8"])
        quote = _quote(customer_id="cust-2", pn_list=["273T1102-5"])
        result = detect_append_suggestion(rfq, [quote], now=NOW)
        assert result.confidence == AppendConfidence.NONE


class TestPriorityOrder:
    def test_high_beats_med(self):
        rfq = IncomingRfq(
            customer_id="cust-1",
            pn_list=["PN-1"],
            customer_quote_ref="REF-777",
        )
        ref_quote = _quote(
            quote_id="Q-ref", client_order_ref="REF-777", pn_list=["PN-99"]
        )
        pn_quote = _quote(quote_id="Q-pn", pn_list=["PN-1"])
        result = detect_append_suggestion(rfq, [pn_quote, ref_quote], now=NOW)
        assert result.confidence == AppendConfidence.HIGH
        assert result.matched_quote is ref_quote
        assert result.match_signal == "customer_quote_ref"

    def test_med_beats_low(self):
        rfq = IncomingRfq(customer_id="cust-1", pn_list=["273T1102-8"])
        pn_quote = _quote(quote_id="Q-pn", pn_list=["273T1102-8"])
        fam_quote = _quote(quote_id="Q-fam", pn_list=["273T1102-5"])
        result = detect_append_suggestion(rfq, [fam_quote, pn_quote], now=NOW)
        assert result.confidence == AppendConfidence.MED
        assert result.matched_quote is pn_quote
        assert result.match_signal == "pn_overlap"

    def test_no_signals_returns_none(self):
        rfq = IncomingRfq(customer_id="cust-1", pn_list=["PN-1"])
        quote = _quote(customer_id="cust-2", pn_list=["PN-99"])
        result = detect_append_suggestion(rfq, [quote], now=NOW)
        assert result.confidence == AppendConfidence.NONE
        assert result.matched_quote is None
        assert result.match_signal is None


class TestWindowFiltering:
    def test_quote_outside_window_excluded(self):
        rfq = IncomingRfq(customer_id="cust-1", pn_list=["PN-1"])
        quote = _quote(pn_list=["PN-1"], created_at=NOW - timedelta(days=14))
        result = detect_append_suggestion(rfq, [quote], now=NOW, window_days=7)
        assert result.confidence == AppendConfidence.NONE

    def test_quote_in_draft_state_included(self):
        rfq = IncomingRfq(customer_id="cust-1", pn_list=["PN-1"])
        quote = _quote(pn_list=["PN-1"], state="draft")
        result = detect_append_suggestion(rfq, [quote], now=NOW)
        assert result.confidence == AppendConfidence.MED

    def test_quote_in_sent_state_included(self):
        rfq = IncomingRfq(customer_id="cust-1", pn_list=["PN-1"])
        quote = _quote(pn_list=["PN-1"], state="sent")
        result = detect_append_suggestion(rfq, [quote], now=NOW)
        assert result.confidence == AppendConfidence.MED

    def test_quote_in_sale_state_excluded(self):
        rfq = IncomingRfq(customer_id="cust-1", pn_list=["PN-1"])
        quote = _quote(pn_list=["PN-1"], state="sale")
        result = detect_append_suggestion(rfq, [quote], now=NOW)
        assert result.confidence == AppendConfidence.NONE

    def test_quote_in_cancel_state_excluded(self):
        rfq = IncomingRfq(customer_id="cust-1", pn_list=["PN-1"])
        quote = _quote(pn_list=["PN-1"], state="cancel")
        result = detect_append_suggestion(rfq, [quote], now=NOW)
        assert result.confidence == AppendConfidence.NONE
