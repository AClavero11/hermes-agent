"""Domain rules compliance suite.

Five end-to-end tests verifying the business rules that govern
AAC's RFQ autopilot. Plus one safety invariant covering the
Summit outbound-solicit exclusion.

Marked ``@pytest.mark.domain_rules`` so CI can selectively run this
as a pre-deploy gate: ``pytest -m domain_rules``.

The tests stitch together real SDA modules with mocked external
boundaries (Gmail, V11, Summit sheet) so the suite runs offline.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

FIXTURES = Path(__file__).parent.parent / "fixtures" / "domain_rules"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ---------------------------------------------------------------------------
# Rule 1: Condition-tier pricing compression (SRU tight, LRU wide)
# ---------------------------------------------------------------------------


@pytest.mark.domain_rules
class TestRule1ConditionCompression:
    """SRUs/piece parts compress AR/SV spread; LRUs/rotables keep it wide."""

    # A 6% threshold splits the fixture deltas: SRU 4% (tight) vs LRU 8% (wide).
    THRESHOLD = 6.0

    def _make_probe(self, key: str):
        resp = _load("mock_v11_responses.json")[key]

        def probe(_pn: str, cond: str):
            if cond == "AR":
                return {"price": resp["ar_price"]}
            if cond == "SV":
                return {"price": resp["sv_price"]}
            return None

        return probe

    def test_compliant_engine(self) -> None:
        from scripts.verify_engine_capabilities import probe_sru_lru_compression

        sru_probe = self._make_probe("sru_tight_compliant")
        lru_probe = self._make_probe("lru_wide_compliant")

        result = probe_sru_lru_compression(
            "TEST-SRU",
            "TEST-LRU",
            sru_probe,
            lru_probe,
            compression_threshold_pct=self.THRESHOLD,
        )

        assert result.compliant is True
        assert result.measured["sru_delta_pct"] == pytest.approx(4.0, abs=0.01)
        assert result.measured["lru_delta_pct"] == pytest.approx(8.0, abs=0.01)

    def test_non_compliant_wide_sru(self) -> None:
        """SRU at 12% delta is too wide: engine is not compressing SRUs."""
        from scripts.verify_engine_capabilities import probe_sru_lru_compression

        sru_probe = self._make_probe("sru_non_compliant")
        lru_probe = self._make_probe("lru_wide_compliant")

        result = probe_sru_lru_compression(
            "TEST-SRU",
            "TEST-LRU",
            sru_probe,
            lru_probe,
            compression_threshold_pct=self.THRESHOLD,
        )

        assert result.compliant is False
        assert "SRU" in result.detail
        assert result.measured["sru_delta_pct"] == pytest.approx(12.0, abs=0.01)


# ---------------------------------------------------------------------------
# Rule 2: LIFO Advanced ID selection with teardown cost recovery
# ---------------------------------------------------------------------------


@pytest.mark.domain_rules
class TestRule2LifoSelection:
    """Lot selection prefers the newest teardown-bearing lot (LIFO)."""

    def test_compliant_lifo_engine(self) -> None:
        from scripts.verify_engine_capabilities import probe_lifo_teardown

        payload = _load("mock_v11_responses.json")["lifo_compliant"]

        def engine_probe(_pn: str):
            return payload

        result = probe_lifo_teardown("TEST-LIFO", engine_probe)

        assert result.compliant is True
        assert result.measured["selected"] == "LOT-NEW"
        assert result.measured["expected"] == "LOT-NEW"

    def test_non_compliant_fifo_engine(self) -> None:
        from scripts.verify_engine_capabilities import probe_lifo_teardown

        payload = _load("mock_v11_responses.json")["lifo_non_compliant_fifo"]

        def engine_probe(_pn: str):
            return payload

        result = probe_lifo_teardown("TEST-LIFO", engine_probe)

        assert result.compliant is False
        assert result.measured["selected"] == "LOT-OLD"
        assert result.measured["expected"] == "LOT-NEW"
        assert "FIFO" in result.detail


# ---------------------------------------------------------------------------
# Rule 3: Summit 145 trace citation
# ---------------------------------------------------------------------------


@pytest.mark.domain_rules
class TestRule3SummitTrace:
    """Summit-sourced lines emit ``trace_type='145'``; AR non-Summit emits None."""

    def test_summit_line_produces_145_trace(self) -> None:
        from tools.summit_trace_flags import QuoteLine, emit_trace_flags

        # OH condition: gets 145 trace but NOT the Summit tag (SV-only)
        lines = [QuoteLine(pn="SUMMIT-PN", condition="OH")]

        def mock_lookup(pn: str):
            if pn == "SUMMIT-PN":
                return {"cost_basis": 500.0, "summit_guidance": "70_30"}
            return None

        with patch(
            "tools.summit_trace_flags.summit_sheet_lookup",
            side_effect=mock_lookup,
        ):
            flags = emit_trace_flags(lines)

        assert len(flags) == 1
        assert flags[0].trace_type == "145"
        assert flags[0].tag_source is None  # only SV gets Summit tag
        assert flags[0].summit_consignment is True
        assert flags[0].summit_cost_recent == 500.0

    def test_summit_sv_line_gets_tag_source(self) -> None:
        from tools.summit_trace_flags import QuoteLine, emit_trace_flags

        lines = [QuoteLine(pn="SUMMIT-PN", condition="SV")]

        def mock_lookup(pn: str):
            if pn == "SUMMIT-PN":
                return {"cost_basis": 500.0, "summit_guidance": "70_30"}
            return None

        with patch(
            "tools.summit_trace_flags.summit_sheet_lookup",
            side_effect=mock_lookup,
        ):
            flags = emit_trace_flags(lines)

        assert flags[0].trace_type == "145"
        assert flags[0].tag_source == "Summit Aerospace"
        assert flags[0].summit_consignment is True

    def test_non_summit_sv_line_gets_8130(self) -> None:
        from tools.summit_trace_flags import QuoteLine, emit_trace_flags

        lines = [QuoteLine(pn="RANDOM-PN", condition="SV")]

        with patch(
            "tools.summit_trace_flags.summit_sheet_lookup",
            return_value=None,
        ):
            flags = emit_trace_flags(lines)

        assert flags[0].trace_type == "8130"
        assert flags[0].tag_source is None

    def test_non_summit_ar_line_has_no_8130(self) -> None:
        """AR parts need trace documents, not an 8130 tag."""
        from tools.summit_trace_flags import QuoteLine, emit_trace_flags

        lines = [QuoteLine(pn="RANDOM-PN", condition="AR")]

        with patch(
            "tools.summit_trace_flags.summit_sheet_lookup",
            return_value=None,
        ):
            flags = emit_trace_flags(lines)

        assert flags[0].trace_type is None
        assert flags[0].tag_source is None

    def test_mixed_batch(self) -> None:
        from tools.summit_trace_flags import QuoteLine, emit_trace_flags

        lines = [
            QuoteLine(pn="SUMMIT-PN", condition="OH"),
            QuoteLine(pn="RANDOM-SV", condition="SV"),
            QuoteLine(pn="RANDOM-AR", condition="AR"),
        ]

        def mock_lookup(pn: str):
            if pn == "SUMMIT-PN":
                return {"cost_basis": 500.0, "summit_guidance": "70_30"}
            return None

        with patch(
            "tools.summit_trace_flags.summit_sheet_lookup",
            side_effect=mock_lookup,
        ):
            flags = emit_trace_flags(lines)

        assert [f.trace_type for f in flags] == ["145", "8130", None]
        assert flags[0].tag_source is None  # OH ≠ SV, no Summit tag
        assert flags[1].tag_source is None
        assert flags[2].tag_source is None


# ---------------------------------------------------------------------------
# Rule 4: Incremental quote append detection
# ---------------------------------------------------------------------------


@pytest.mark.domain_rules
class TestRule4IncrementalAppend:
    """Same-customer overlapping RFQs within 7 days surface as appends."""

    def test_exact_ref_match_high_confidence(self) -> None:
        from tools.quote_append_detector import (
            AppendConfidence,
            IncomingRfq,
            OpenQuote,
            detect_append_suggestion,
        )

        now = datetime(2026, 4, 10)
        rfq = IncomingRfq(
            customer_id="cust-123",
            pn_list=["P1"],
            customer_quote_ref="ABC-123",
        )
        quote = OpenQuote(
            quote_id="q1",
            customer_id="cust-123",
            client_order_ref="ABC-123",
            pn_list=["P2"],
            created_at=now - timedelta(days=2),
        )

        result = detect_append_suggestion(rfq, [quote], now=now)

        assert result.confidence == AppendConfidence.HIGH
        assert result.matched_quote is not None
        assert result.matched_quote.quote_id == "q1"
        assert result.match_signal == "customer_quote_ref"

    def test_pn_overlap_within_window_med_confidence(self) -> None:
        from tools.quote_append_detector import (
            AppendConfidence,
            IncomingRfq,
            OpenQuote,
            detect_append_suggestion,
        )

        now = datetime(2026, 4, 10)
        rfq = IncomingRfq(
            customer_id="cust-123",
            pn_list=["3605812-17", "273T1102-8"],
        )
        quote = OpenQuote(
            quote_id="q2",
            customer_id="cust-123",
            client_order_ref=None,
            pn_list=["3605812-17"],
            created_at=now - timedelta(days=3),
        )

        result = detect_append_suggestion(rfq, [quote], now=now)

        assert result.confidence == AppendConfidence.MED
        assert result.matched_quote is not None
        assert result.matched_quote.quote_id == "q2"
        assert result.match_signal == "pn_overlap"

    def test_cross_customer_ref_collision_suppressed(self) -> None:
        from tools.quote_append_detector import (
            AppendConfidence,
            IncomingRfq,
            OpenQuote,
            detect_append_suggestion,
        )

        now = datetime(2026, 4, 10)
        rfq = IncomingRfq(
            customer_id="cust-999",
            pn_list=["P1"],
            customer_quote_ref="ABC-123",
        )
        other_customer_quote = OpenQuote(
            quote_id="q3",
            customer_id="cust-123",
            client_order_ref="ABC-123",
            pn_list=["P9"],
            created_at=now - timedelta(days=1),
        )

        result = detect_append_suggestion(
            rfq, [other_customer_quote], now=now
        )

        assert result.confidence == AppendConfidence.NONE
        assert result.matched_quote is None
        assert any(
            "cross-customer" in w for w in result.warnings
        ), f"expected cross-customer warning, got {result.warnings}"


# ---------------------------------------------------------------------------
# Rule 5: Customer quote reference propagation
# ---------------------------------------------------------------------------


@pytest.mark.domain_rules
class TestRule5CustomerQuoteRefPropagation:
    """Extract a ref from RFQ body and use it as a match signal downstream."""

    def test_ref_extraction_and_append_roundtrip(self) -> None:
        from tools.customer_quote_ref import extract_customer_quote_ref
        from tools.quote_append_detector import (
            AppendConfidence,
            IncomingRfq,
            OpenQuote,
            detect_append_suggestion,
        )

        rfq_payload = _load("sample_rfqs.json")["rfq_with_ref"]
        extracted_ref = extract_customer_quote_ref(rfq_payload["body"])

        assert extracted_ref == "PROD-2026-0847"

        now = datetime(2026, 4, 10)
        rfq = IncomingRfq(
            customer_id=rfq_payload["customer_id"],
            pn_list=rfq_payload["pn_list"],
            customer_quote_ref=extracted_ref,
        )
        open_quote = OpenQuote(
            quote_id="q-round-1",
            customer_id=rfq_payload["customer_id"],
            client_order_ref="PROD-2026-0847",
            pn_list=["UNRELATED-PN"],
            created_at=now - timedelta(days=4),
        )

        result = detect_append_suggestion(rfq, [open_quote], now=now)

        assert result.confidence == AppendConfidence.HIGH
        assert result.match_signal == "customer_quote_ref"
        assert result.matched_quote is not None
        assert result.matched_quote.quote_id == "q-round-1"

    def test_rfq_without_ref_returns_none(self) -> None:
        from tools.customer_quote_ref import extract_customer_quote_ref

        rfq_payload = _load("sample_rfqs.json")["rfq_without_ref"]
        assert extract_customer_quote_ref(rfq_payload["body"]) is None


# ---------------------------------------------------------------------------
# Safety invariant: Summit outbound-solicit exclusion (2026-04-09 directive)
# ---------------------------------------------------------------------------


@pytest.mark.domain_rules
class TestSummitExclusionSafety:
    """Summit contacts must never receive an auto-drafted outbound solicit."""

    def test_cannot_build_draft_with_summit_recipient(self) -> None:
        from tools.outbound_solicit_tool import (
            HistoricalVendor,
            SolicitRequest,
            build_solicit_draft,
        )

        with pytest.raises(ValueError, match="Summit"):
            build_solicit_draft(
                SolicitRequest(pn_list=["P1"]),
                HistoricalVendor(
                    email="jorge.fernandez@summitmro.com",
                    last_contact_date="2026-04-09",
                ),
            )

    def test_select_non_summit_vendor_skips_summit(self) -> None:
        from tools.outbound_solicit_tool import (
            HistoricalVendor,
            select_non_summit_vendor,
        )

        candidates = [
            HistoricalVendor(
                email="kent@summitmro.com", last_contact_date="2026-04-01"
            ),
            HistoricalVendor(
                email="bob@othervendor.com", last_contact_date="2026-03-15"
            ),
        ]

        chosen = select_non_summit_vendor(candidates)

        assert chosen is not None
        assert chosen.email == "bob@othervendor.com"

    def test_manual_override_summit_prompts_confirmation(self) -> None:
        from tools.outbound_solicit_tool import check_manual_recipient_override

        needs_confirm, warning = check_manual_recipient_override(
            "Kent.Kendrick@summitmro.com"
        )

        assert needs_confirm is True
        assert warning is not None
        assert len(warning) > 0

    def test_manual_override_non_summit_passes_through(self) -> None:
        from tools.outbound_solicit_tool import check_manual_recipient_override

        needs_confirm, warning = check_manual_recipient_override(
            "pricing@othervendor.com"
        )

        assert needs_confirm is False
        assert warning is None
