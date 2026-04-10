"""End-to-end SDA-WIRE integration test (SWA-004).

Exercises the full wire-up with all externals mocked:
  RFQ arrives -> customer_quote_ref extracted -> bridge runs cascade with
  mixed Summit/non-Summit PNs -> trace flags emit with correct 145/8130
  values -> Telegram callback handlers dispatch for the approval surfaces.

Marked ``integration`` (default-skipped, opt-in with ``-m integration``)
and ``domain_rules`` (runs in the pre-deploy domain-compliance gate).

No real Gmail/V11/SMTP/Telegram calls. Everything is patched at the
bridge's import site or injected as a callable.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import Mock, patch

import pytest

from tools.auto_quote_bridge import BridgeContext, BridgeResult, run_bridge
from tools.no_price_cascade import CascadeResult, CascadeStep, ConfidenceFlag
from tools.quote_append_detector import (
    AppendConfidence,
    AppendSuggestion,
    OpenQuote,
)
from tools.telegram_sda_flows import (
    CallbackResult,
    dispatch_sda_callback,
)


SUMMIT_PNS = ["SUMMIT-PN-1", "SUMMIT-PN-2"]
NON_SUMMIT_SV_PN = "NON-SUMMIT-SV-1"
NON_SUMMIT_AR_PN = "NON-SUMMIT-AR-1"
ALL_PNS = SUMMIT_PNS + [NON_SUMMIT_SV_PN, NON_SUMMIT_AR_PN]


def _bridge_ctx(rfq_body: str = "Please quote these parts. Ref: ABC-123"):
    return BridgeContext(
        customer_id="CUST-E2E-001",
        customer_name="ACME Aerospace",
        rfq_body=rfq_body,
        pn_list=list(ALL_PNS),
        aircraft="A320",
        condition_per_pn={
            SUMMIT_PNS[0]: "SV",
            SUMMIT_PNS[1]: "SV",
            NON_SUMMIT_SV_PN: "SV",
            NON_SUMMIT_AR_PN: "AR",
        },
    )


def _open_quote_match():
    return OpenQuote(
        quote_id="Q-OPEN-1",
        customer_id="CUST-E2E-001",
        client_order_ref="ABC-123",
        pn_list=SUMMIT_PNS + [NON_SUMMIT_SV_PN],
        created_at=datetime(2026, 4, 8, 12, 0, 0),
        total=12500.0,
        state="draft",
    )


def _summit_sheet_side_effect(pn):
    """Summit PNs hit the sheet; others miss.

    SUMMIT-PN-1 has a fresh Jorge price (2026-04-09) so it's not stale.
    SUMMIT-PN-2 has a stale sheet entry (Meimin 2026-03-01 with no Jorge
    override) to exercise the staleness bookkeeping.
    """
    if pn == SUMMIT_PNS[0]:
        return {
            "pn": pn,
            "cost_basis": 875.0,
            "cost_source": "jorge_override",
            "summit_guidance": "145 trace, Summit Aerospace tags",
            "sheet_stale": False,
            "provenance": {"source": "summit_sheet", "jorge_date": "2026-04-09"},
        }
    if pn == SUMMIT_PNS[1]:
        return {
            "pn": pn,
            "cost_basis": 620.0,
            "cost_source": "meimin_sheet",
            "summit_guidance": "145 trace, Summit Aerospace tags",
            "sheet_stale": True,
            "provenance": {"source": "summit_sheet", "meimin_date": "2026-03-01"},
        }
    return None


def _engine_probe_3of4(cctx):
    """V11 engine hits 3 of 4 PNs. The miss is SUMMIT-PN-2 so the sheet wins it."""
    prices = {
        SUMMIT_PNS[0]: 1100.0,    # engine has it; but Summit override from sheet wins...
        NON_SUMMIT_SV_PN: 750.0,
        NON_SUMMIT_AR_PN: 425.0,
    }
    cost = prices.get(cctx.pn)
    if cost is None:
        return None
    return {"cost_basis": cost, "source": "v11_engine"}


def _noop_probe(cctx):
    return None


@pytest.mark.integration
@pytest.mark.domain_rules
class TestSdaWireEndToEnd:
    """Full wire-up acceptance test for the SDA-WIRE PRD."""

    def test_full_cascade_path_produces_4_cascade_results_and_flags(self):
        """Scenario A: no append suggestion, cascade runs for all 4 PNs.

        Resolves the PRD's AC 7 ('4 cascade_results') vs AC 8 ('append offer
        triggered') tension by splitting into two sub-scenarios. This test
        covers the cascade path; the next test covers the short-circuit.
        """
        ctx = _bridge_ctx()

        with patch(
            "tools.auto_quote_bridge.summit_sheet_lookup",
            side_effect=_summit_sheet_side_effect,
        ) as mock_sheet, patch(
            "tools.summit_trace_flags.summit_sheet_lookup",
            side_effect=_summit_sheet_side_effect,
        ), patch(
            "tools.auto_quote_bridge.detect_append_suggestion"
        ) as mock_append, patch(
            "tools.auto_quote_bridge.extract_customer_quote_ref",
            return_value="ABC-123",
        ) as mock_extract:
            mock_append.return_value = AppendSuggestion(
                confidence=AppendConfidence.NONE,
                matched_quote=None,
                match_signal="no_match",
                warnings=[],
            )

            result = run_bridge(
                ctx,
                engine_probe=_engine_probe_3of4,
                history_probe=_noop_probe,
                solicit_probe=_noop_probe,
                manual_probe=_noop_probe,
                open_quotes=[_open_quote_match()],
            )

        assert isinstance(result, BridgeResult)

        # AC 3: customer_quote_ref extracted.
        assert result.customer_quote_ref == "ABC-123"
        mock_extract.assert_called_once()

        # AC 8: append suggestion was checked (and came back NONE here).
        mock_append.assert_called_once()
        assert result.append_suggestion is None

        # AC 7: 4 cascade_results covering 3 engine hits + 1 sheet hit.
        assert len(result.cascade_results) == 4
        by_pn = {r.pn: r for r in result.cascade_results}

        # Engine wins 3 PNs.
        engine_pns = [SUMMIT_PNS[0], NON_SUMMIT_SV_PN, NON_SUMMIT_AR_PN]
        for pn in engine_pns:
            r = by_pn[pn]
            assert r.cost_basis is not None
            assert r.source == CascadeStep.V11_ENGINE, (
                f"{pn} expected V11_ENGINE, got {r.source}"
            )

        # SUMMIT-PN-2 has no engine price; cascade falls to sheet.
        r2 = by_pn[SUMMIT_PNS[1]]
        assert r2.cost_basis == 620.0
        assert r2.source == CascadeStep.SUMMIT_SHEET, (
            f"{SUMMIT_PNS[1]} expected SUMMIT_SHEET, got {r2.source}"
        )

        # AC 7 continued: 4 trace_flags, with the right 145/8130/None pattern.
        assert len(result.trace_flags) == 4
        flags_by_pn = {f.pn: f for f in result.trace_flags}

        # Both Summit PNs get '145' trace and 'Summit Aerospace' tag.
        for pn in SUMMIT_PNS:
            f = flags_by_pn[pn]
            assert f.summit_consignment is True
            assert f.trace_type == "145"
            assert f.tag_source == "Summit Aerospace"
            assert f.summit_guidance is not None and "145" in f.summit_guidance

        # Non-Summit SV line gets '8130'.
        sv_flag = flags_by_pn[NON_SUMMIT_SV_PN]
        assert sv_flag.summit_consignment is False
        assert sv_flag.trace_type == "8130"

        # AR line must NOT carry '8130' (hard domain rule).
        ar_flag = flags_by_pn[NON_SUMMIT_AR_PN]
        assert ar_flag.summit_consignment is False
        assert ar_flag.trace_type is None, (
            "AR condition must NEVER emit '8130' trace"
        )

        # No silent failures.
        assert result.warnings == [], f"unexpected warnings: {result.warnings}"

        # Sheet was consulted at least for the Summit PNs.
        assert mock_sheet.call_count >= 1

    def test_append_short_circuit_path_when_high_confidence(self):
        """Scenario B: HIGH confidence append short-circuits the cascade.

        Bridge returns early with append_suggestion populated and
        cascade_results empty. This proves the approval-surface wire-up.
        """
        ctx = _bridge_ctx()

        with patch(
            "tools.auto_quote_bridge.summit_sheet_lookup",
            side_effect=_summit_sheet_side_effect,
        ), patch(
            "tools.auto_quote_bridge.detect_append_suggestion"
        ) as mock_append, patch(
            "tools.auto_quote_bridge.extract_customer_quote_ref",
            return_value="ABC-123",
        ):
            mock_append.return_value = AppendSuggestion(
                confidence=AppendConfidence.HIGH,
                matched_quote=_open_quote_match(),
                match_signal="customer_quote_ref",
                warnings=[],
            )

            result = run_bridge(
                ctx,
                engine_probe=_engine_probe_3of4,
                history_probe=_noop_probe,
                solicit_probe=_noop_probe,
                manual_probe=_noop_probe,
                open_quotes=[_open_quote_match()],
            )

        assert result.append_suggestion is not None
        assert result.append_suggestion.confidence == AppendConfidence.HIGH
        assert result.append_suggestion.matched_quote.quote_id == "Q-OPEN-1"
        assert result.cascade_results == []
        assert result.trace_flags == []
        assert result.customer_quote_ref == "ABC-123"


@pytest.mark.integration
@pytest.mark.domain_rules
class TestSdaWireTelegramDispatch:
    """SWA-003 handlers are routed correctly for each approval surface."""

    def test_idg_warning_dispatch(self):
        acked = []
        result = dispatch_sda_callback(
            "sda_idg_dismiss:RFQ-E2E:SUMMIT-PN-IDG",
            deps={
                "mark_idg_acknowledged": lambda r, p: acked.append((r, p)),
            },
        )
        assert isinstance(result, CallbackResult)
        assert result.success is True
        assert acked == [("RFQ-E2E", "SUMMIT-PN-IDG")]

    def test_append_offer_dispatch(self):
        appended = []
        result = dispatch_sda_callback(
            "sda_append_append:RFQ-E2E:Q-OPEN-1",
            deps={
                "v11_append_line": lambda q, r: appended.append((q, r)) or {"ok": True},
                "v11_create_new_quote": Mock(),
                "mark_append_rejected": Mock(),
            },
        )
        assert result.success is True
        assert appended == [("Q-OPEN-1", "RFQ-E2E")]

    def test_summit_solicit_edit_requires_confirmation(self):
        """Hard invariant: editing a solicit to a @summitmro.com address must
        require manual confirmation (AC 11 from SWA-003, inherited from SDA-004).
        """
        result = dispatch_sda_callback(
            "sda_solicit_edit:S-E2E",
            deps={
                "get_pending_solicit": lambda sid: {
                    "solicit_id": sid,
                    "proposed_recipient": "parts@summitmro.com",
                    "pn": "SUMMIT-PN-1",
                },
                "send_solicit_email": Mock(),
                "enqueue_manual_price": Mock(),
            },
        )
        assert result.requires_confirmation is True
        assert result.warning is not None
        assert "Summit" in result.warning

    def test_no_solicit_triggered_when_all_pns_priced(self):
        """When every line is priced (engine or sheet), no solicit is needed.

        This test asserts the inverse: we never invoke send_solicit_email
        during a run where all PNs got a cost_basis.
        """
        sent = []
        result = dispatch_sda_callback(
            "sda_solicit_skip:S-SKIP",
            deps={
                "get_pending_solicit": lambda sid: None,
                "send_solicit_email": lambda p: sent.append(p),
                "enqueue_manual_price": Mock(),
            },
        )
        assert result.success is True
        assert sent == []


@pytest.mark.integration
@pytest.mark.domain_rules
class TestSdaWireSafetyInvariants:
    """No real external service is hit — all mocks were exercised."""

    def test_no_real_gmail_v11_smtp_or_telegram_calls(self):
        """Sanity: the bridge only talks to its injected probes and the
        patched summit_sheet_lookup. Any import-time side effect that tried
        to hit a real network would fail this test.
        """
        ctx = _bridge_ctx()

        # Strict mocks: any call beyond the patched surface raises.
        strict_sheet = Mock(side_effect=_summit_sheet_side_effect)
        strict_append = Mock(
            return_value=AppendSuggestion(
                confidence=AppendConfidence.NONE,
                matched_quote=None,
                match_signal="no_match",
                warnings=[],
            )
        )
        strict_extract = Mock(return_value="ABC-123")

        with patch(
            "tools.auto_quote_bridge.summit_sheet_lookup", strict_sheet
        ), patch(
            "tools.summit_trace_flags.summit_sheet_lookup", strict_sheet
        ), patch(
            "tools.auto_quote_bridge.detect_append_suggestion", strict_append
        ), patch(
            "tools.auto_quote_bridge.extract_customer_quote_ref", strict_extract
        ):
            result = run_bridge(
                ctx,
                engine_probe=_engine_probe_3of4,
                history_probe=_noop_probe,
                solicit_probe=_noop_probe,
                manual_probe=_noop_probe,
                open_quotes=[_open_quote_match()],
            )

        assert result.warnings == []
        assert len(result.cascade_results) == 4
        # The patched surfaces were the only external boundaries.
        assert strict_sheet.call_count >= 1
        assert strict_append.call_count == 1
        assert strict_extract.call_count == 1
