"""Tests for tools.auto_quote_bridge (SWA-001)."""

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
    """Build a CascadeResult that looks like a V11 engine hit."""
    return CascadeResult(
        pn=pn,
        cost_basis=cost,
        source=CascadeStep.V11_ENGINE,
        confidence=ConfidenceFlag.GREEN,
        provenance={"source": "engine"},
        sheet_stale=False,
    )


def _trace_flag(pn="PN-1"):
    """Build a TraceFlags instance for assertions."""
    return TraceFlags(
        pn=pn,
        summit_consignment=False,
        trace_type="8130",
        tag_source=None,
        summit_cost_recent=None,
        summit_guidance=None,
        idg_piece_part_warning=False,
        per_line_check_required=None,
    )


def _append_suggestion(confidence=AppendConfidence.HIGH):
    return AppendSuggestion(
        confidence=confidence,
        matched_quote=OpenQuote(
            quote_id="Q-001",
            customer_id="CUST-1",
            client_order_ref="ABC-123",
            pn_list=["PN-1"],
            created_at=datetime(2026, 4, 9, 12, 0, 0),
            total=500.0,
            state="draft",
        ),
        match_signal="customer_quote_ref",
        warnings=[],
    )


def _probes(engine=None, history=None, solicit=None, manual=None):
    return {
        "engine_probe": Mock(return_value=engine),
        "history_probe": Mock(return_value=history),
        "solicit_probe": Mock(return_value=solicit),
        "manual_probe": Mock(return_value=manual),
    }


class TestBridgeHappyPath:
    def test_bridge_happy_path(self):
        ctx = _ctx(pn_list=["PN-A", "PN-B"])

        def cascade_side_effect(cctx, **kwargs):
            return _engine_hit(pn=cctx.pn)

        def trace_side_effect(lines):
            return [_trace_flag(pn=lines[0].pn)]

        with patch(
            "tools.auto_quote_bridge.run_cascade",
            side_effect=cascade_side_effect,
        ) as mock_cascade, patch(
            "tools.auto_quote_bridge.emit_trace_flags",
            side_effect=trace_side_effect,
        ) as mock_emit, patch(
            "tools.auto_quote_bridge.extract_customer_quote_ref",
            return_value=None,
        ):
            result = run_bridge(ctx, **_probes())

        assert isinstance(result, BridgeResult)
        assert len(result.cascade_results) == 2
        assert {r.pn for r in result.cascade_results} == {"PN-A", "PN-B"}
        assert len(result.trace_flags) == 2
        assert result.warnings == []
        assert result.customer_quote_ref is None
        assert mock_cascade.call_count == 2
        assert mock_emit.call_count == 2


class TestAppendShortCircuit:
    def test_bridge_with_append_short_circuits(self):
        ctx = _ctx(pn_list=["PN-A"])
        open_quotes = [
            OpenQuote(
                quote_id="Q-1",
                customer_id="CUST-1",
                client_order_ref="ABC-123",
                pn_list=["PN-A"],
                created_at=datetime(2026, 4, 9),
            )
        ]

        with patch(
            "tools.auto_quote_bridge.detect_append_suggestion",
            return_value=_append_suggestion(AppendConfidence.HIGH),
        ) as mock_detect, patch(
            "tools.auto_quote_bridge.run_cascade"
        ) as mock_cascade, patch(
            "tools.auto_quote_bridge.emit_trace_flags"
        ) as mock_emit, patch(
            "tools.auto_quote_bridge.extract_customer_quote_ref",
            return_value=None,
        ):
            result = run_bridge(ctx, **_probes(), open_quotes=open_quotes)

        assert result.append_suggestion is not None
        assert result.append_suggestion.confidence == AppendConfidence.HIGH
        assert result.cascade_results == []
        assert result.trace_flags == []
        assert result.warnings == []
        mock_detect.assert_called_once()
        mock_cascade.assert_not_called()
        mock_emit.assert_not_called()

    def test_bridge_append_suggestion_low_confidence_does_not_short_circuit(self):
        ctx = _ctx(pn_list=["PN-A"])
        open_quotes = [
            OpenQuote(
                quote_id="Q-1",
                customer_id="CUST-1",
                client_order_ref=None,
                pn_list=["PN-A"],
                created_at=datetime(2026, 4, 9),
            )
        ]

        with patch(
            "tools.auto_quote_bridge.detect_append_suggestion",
            return_value=_append_suggestion(AppendConfidence.LOW),
        ), patch(
            "tools.auto_quote_bridge.run_cascade",
            return_value=_engine_hit(pn="PN-A"),
        ) as mock_cascade, patch(
            "tools.auto_quote_bridge.emit_trace_flags",
            return_value=[_trace_flag(pn="PN-A")],
        ), patch(
            "tools.auto_quote_bridge.extract_customer_quote_ref",
            return_value=None,
        ):
            result = run_bridge(ctx, **_probes(), open_quotes=open_quotes)

        mock_cascade.assert_called()
        assert len(result.cascade_results) == 1
        assert result.append_suggestion is not None
        assert result.append_suggestion.confidence == AppendConfidence.LOW


class TestCustomerRefExtraction:
    def test_bridge_extracts_customer_ref_from_body(self):
        ctx = _ctx(
            pn_list=["PN-A"],
            rfq_body="Ref: ABC-123 please quote",
        )

        with patch(
            "tools.auto_quote_bridge.extract_customer_quote_ref",
            return_value="ABC-123",
        ) as mock_extract, patch(
            "tools.auto_quote_bridge.run_cascade",
            return_value=_engine_hit(pn="PN-A"),
        ), patch(
            "tools.auto_quote_bridge.emit_trace_flags",
            return_value=[_trace_flag(pn="PN-A")],
        ):
            result = run_bridge(ctx, **_probes())

        assert result.customer_quote_ref == "ABC-123"
        mock_extract.assert_called_once_with("Ref: ABC-123 please quote")

    def test_bridge_no_customer_ref_is_none(self):
        ctx = _ctx(pn_list=["PN-A"], rfq_body="no ref here")

        with patch(
            "tools.auto_quote_bridge.extract_customer_quote_ref",
            return_value=None,
        ), patch(
            "tools.auto_quote_bridge.run_cascade",
            return_value=_engine_hit(pn="PN-A"),
        ), patch(
            "tools.auto_quote_bridge.emit_trace_flags",
            return_value=[_trace_flag(pn="PN-A")],
        ):
            result = run_bridge(ctx, **_probes())

        assert result.customer_quote_ref is None


class TestPerPnBehaviour:
    def test_bridge_each_pn_gets_own_cascade_result(self):
        ctx = _ctx(pn_list=["PN-A", "PN-B", "PN-C"])

        def cascade_side_effect(cctx, **kwargs):
            return _engine_hit(pn=cctx.pn, cost=10.0)

        with patch(
            "tools.auto_quote_bridge.run_cascade",
            side_effect=cascade_side_effect,
        ), patch(
            "tools.auto_quote_bridge.emit_trace_flags",
            side_effect=lambda lines: [_trace_flag(pn=lines[0].pn)],
        ), patch(
            "tools.auto_quote_bridge.extract_customer_quote_ref",
            return_value=None,
        ):
            result = run_bridge(ctx, **_probes())

        assert len(result.cascade_results) == 3
        assert [r.pn for r in result.cascade_results] == ["PN-A", "PN-B", "PN-C"]

    def test_bridge_each_pn_gets_own_trace_flags(self):
        ctx = _ctx(pn_list=["PN-A", "PN-B"])

        def cascade_side_effect(cctx, **kwargs):
            return _engine_hit(pn=cctx.pn)

        call_log = []

        def trace_side_effect(lines):
            assert len(lines) == 1, "emit_trace_flags must be called per-line"
            call_log.append(lines[0].pn)
            return [_trace_flag(pn=lines[0].pn)]

        with patch(
            "tools.auto_quote_bridge.run_cascade",
            side_effect=cascade_side_effect,
        ), patch(
            "tools.auto_quote_bridge.emit_trace_flags",
            side_effect=trace_side_effect,
        ), patch(
            "tools.auto_quote_bridge.extract_customer_quote_ref",
            return_value=None,
        ):
            result = run_bridge(ctx, **_probes())

        assert len(result.trace_flags) == 2
        assert [f.pn for f in result.trace_flags] == ["PN-A", "PN-B"]
        assert call_log == ["PN-A", "PN-B"]


class TestFailureIsolation:
    def test_bridge_failure_in_cascade_returns_warning_not_raise(self):
        ctx = _ctx(pn_list=["PN-A"])

        with patch(
            "tools.auto_quote_bridge.run_cascade",
            side_effect=RuntimeError("engine down"),
        ), patch(
            "tools.auto_quote_bridge.extract_customer_quote_ref",
            return_value=None,
        ):
            result = run_bridge(ctx, **_probes())

        assert isinstance(result, BridgeResult)
        assert result.cascade_results == []
        assert result.trace_flags == []
        assert len(result.warnings) >= 1
        assert any("engine down" in w for w in result.warnings)
        assert any("PN-A" in w for w in result.warnings)

    def test_bridge_failure_in_trace_flags_isolated(self):
        ctx = _ctx(pn_list=["PN-GOOD", "PN-BAD"])

        def cascade_side_effect(cctx, **kwargs):
            return _engine_hit(pn=cctx.pn)

        def trace_side_effect(lines):
            if lines[0].pn == "PN-BAD":
                raise RuntimeError("trace flag kaboom")
            return [_trace_flag(pn=lines[0].pn)]

        with patch(
            "tools.auto_quote_bridge.run_cascade",
            side_effect=cascade_side_effect,
        ), patch(
            "tools.auto_quote_bridge.emit_trace_flags",
            side_effect=trace_side_effect,
        ), patch(
            "tools.auto_quote_bridge.extract_customer_quote_ref",
            return_value=None,
        ):
            result = run_bridge(ctx, **_probes())

        assert isinstance(result, BridgeResult)
        assert len(result.cascade_results) == 2
        assert len(result.trace_flags) == 1
        assert result.trace_flags[0].pn == "PN-GOOD"
        assert any("PN-BAD" in w for w in result.warnings)
        assert any("kaboom" in w for w in result.warnings)


class TestEdgeCases:
    def test_bridge_no_pns_returns_empty_but_valid_result(self):
        ctx = _ctx(pn_list=[])

        with patch(
            "tools.auto_quote_bridge.extract_customer_quote_ref",
            return_value=None,
        ), patch(
            "tools.auto_quote_bridge.run_cascade"
        ) as mock_cascade, patch(
            "tools.auto_quote_bridge.emit_trace_flags"
        ) as mock_emit:
            result = run_bridge(ctx, **_probes())

        assert isinstance(result, BridgeResult)
        assert result.cascade_results == []
        assert result.trace_flags == []
        assert result.warnings == []
        mock_cascade.assert_not_called()
        mock_emit.assert_not_called()

    def test_bridge_duplicate_pn_is_skipped_with_warning(self):
        ctx = _ctx(pn_list=["PN-A", "PN-A", "PN-B"])

        def cascade_side_effect(cctx, **kwargs):
            return _engine_hit(pn=cctx.pn)

        with patch(
            "tools.auto_quote_bridge.run_cascade",
            side_effect=cascade_side_effect,
        ) as mock_cascade, patch(
            "tools.auto_quote_bridge.emit_trace_flags",
            side_effect=lambda lines: [_trace_flag(pn=lines[0].pn)],
        ), patch(
            "tools.auto_quote_bridge.extract_customer_quote_ref",
            return_value=None,
        ):
            result = run_bridge(ctx, **_probes())

        assert len(result.cascade_results) == 2
        assert [r.pn for r in result.cascade_results] == ["PN-A", "PN-B"]
        assert mock_cascade.call_count == 2
        assert any("duplicate pn skipped: PN-A" in w for w in result.warnings)


class TestAppendConfidenceMatrix:
    """Cover all four AppendConfidence branches explicitly."""

    def _run(self, confidence):
        ctx = _ctx(pn_list=["PN-A"])
        open_quotes = [
            OpenQuote(
                quote_id="Q-1",
                customer_id="CUST-1",
                client_order_ref=None,
                pn_list=["PN-A"],
                created_at=datetime(2026, 4, 9),
            )
        ]
        with patch(
            "tools.auto_quote_bridge.detect_append_suggestion",
            return_value=_append_suggestion(confidence),
        ), patch(
            "tools.auto_quote_bridge.run_cascade",
            return_value=_engine_hit(pn="PN-A"),
        ) as mock_cascade, patch(
            "tools.auto_quote_bridge.emit_trace_flags",
            return_value=[_trace_flag(pn="PN-A")],
        ), patch(
            "tools.auto_quote_bridge.extract_customer_quote_ref",
            return_value=None,
        ):
            result = run_bridge(ctx, **_probes(), open_quotes=open_quotes)
        return result, mock_cascade

    def test_med_confidence_short_circuits(self):
        result, mock_cascade = self._run(AppendConfidence.MED)
        assert result.append_suggestion is not None
        assert result.append_suggestion.confidence == AppendConfidence.MED
        assert result.cascade_results == []
        assert result.trace_flags == []
        mock_cascade.assert_not_called()

    def test_none_confidence_continues_to_cascade(self):
        result, mock_cascade = self._run(AppendConfidence.NONE)
        assert result.append_suggestion is None
        assert len(result.cascade_results) == 1
        mock_cascade.assert_called_once()
