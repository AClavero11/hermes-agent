"""Integration test for SWA-002: verify the SDA bridge is wired into
services/ils_auto_quote.py and that bridge failures fall through cleanly
to the existing pricing logic.

The loose file ~/.hermes/services/ils_auto_quote.py is loaded by absolute
path because it lives outside the hermes-agent repo.

Marked ``integration`` so it only runs with ``-m integration``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


ILS_AUTO_QUOTE_PATH = Path.home() / ".hermes" / "services" / "ils_auto_quote.py"


@pytest.fixture(scope="module")
def ils_module():
    """Load ils_auto_quote.py from its absolute loose-file path."""
    sys.path.insert(0, str(Path.home() / ".hermes" / "hermes-agent"))
    sys.path.insert(0, str(Path.home() / ".hermes"))
    spec = importlib.util.spec_from_file_location(
        "services.ils_auto_quote", str(ILS_AUTO_QUOTE_PATH)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["services.ils_auto_quote"] = module
    spec.loader.exec_module(module)
    return module


def _make_mock_rfq(ils_mod):
    part = ils_mod.RFQPart(
        part_number="PN-123",
        quantity=1,
        description="Test widget",
    )
    return ils_mod.ILSRfq(
        rfq_id="TEST-RFQ-1",
        company="Test Co",
        contact_name="Test Contact",
        contact_email="test@example.com",
        parts=[part],
        ils_company_id="42",
    )


def _make_stock_match(ils_mod, pn="PN-123"):
    return ils_mod.StockMatch(
        part_number=pn,
        product_id=1,
        template_id=1,
        qty_available=5,
        condition="AR",
        unit_cost=100.0,
        suggested_price=250.0,
        description="Test widget",
        pricing_method="cost+markup",
        platform="",
        oem="",
        is_idg=False,
        confidence=80,
    )


@pytest.mark.integration
class TestBridgeWire:
    def test_bridge_is_invoked_during_process_rfq(self, ils_module):
        """run_bridge must be called once with a BridgeContext matching the RFQ."""
        engine = ils_module.AutoQuoteEngine.__new__(ils_module.AutoQuoteEngine)
        engine.v11 = MagicMock()
        engine.ils = MagicMock()
        engine.db = MagicMock()
        engine.db.is_rfq_seen.return_value = False
        engine.v11.find_or_create_customer.return_value = (7, "Test Co", False)
        engine.v11.create_draft_quote.return_value = {"id": 100, "name": "SO100"}
        engine.db.save_quote.return_value = None

        rfq = _make_mock_rfq(ils_module)
        match = _make_stock_match(ils_module)

        with patch.object(
            engine, "_check_stock", return_value=match
        ), patch(
            "services.ils_auto_quote.run_bridge"
        ) as mock_run_bridge:
            mock_result = MagicMock()
            mock_result.warnings = []
            mock_result.cascade_results = []
            mock_result.trace_flags = []
            mock_run_bridge.return_value = mock_result

            result = engine._process_rfq(rfq)

        mock_run_bridge.assert_called_once()
        call_args = mock_run_bridge.call_args
        bridge_ctx = call_args.args[0]
        assert bridge_ctx.customer_id == "42"
        assert bridge_ctx.customer_name == "Test Co"
        assert bridge_ctx.pn_list == ["PN-123"]
        assert bridge_ctx.condition_per_pn == {"PN-123": "AR"}
        assert isinstance(result, ils_module.QuoteResult)

    def test_bridge_exception_falls_through_to_existing_pricing(self, ils_module):
        """If run_bridge raises, _process_rfq must still produce a QuoteResult."""
        engine = ils_module.AutoQuoteEngine.__new__(ils_module.AutoQuoteEngine)
        engine.v11 = MagicMock()
        engine.ils = MagicMock()
        engine.db = MagicMock()
        engine.v11.find_or_create_customer.return_value = (7, "Test Co", False)
        engine.v11.create_draft_quote.return_value = {"id": 101, "name": "SO101"}
        engine.db.save_quote.return_value = None

        rfq = _make_mock_rfq(ils_module)
        match = _make_stock_match(ils_module)

        with patch.object(
            engine, "_check_stock", return_value=match
        ), patch(
            "services.ils_auto_quote.run_bridge",
            side_effect=RuntimeError("bridge boom"),
        ):
            result = engine._process_rfq(rfq)

        # Existing pricing path must still produce a successful quote.
        assert isinstance(result, ils_module.QuoteResult)
        assert result.success is True
        assert result.order_name == "SO101"

    def test_bridge_warnings_are_logged_not_raised(self, ils_module, caplog):
        """If run_bridge returns warnings, they are logged but don't break the flow."""
        import logging

        engine = ils_module.AutoQuoteEngine.__new__(ils_module.AutoQuoteEngine)
        engine.v11 = MagicMock()
        engine.ils = MagicMock()
        engine.db = MagicMock()
        engine.v11.find_or_create_customer.return_value = (7, "Test Co", False)
        engine.v11.create_draft_quote.return_value = {"id": 102, "name": "SO102"}
        engine.db.save_quote.return_value = None

        rfq = _make_mock_rfq(ils_module)
        match = _make_stock_match(ils_module)

        with patch.object(
            engine, "_check_stock", return_value=match
        ), patch(
            "services.ils_auto_quote.run_bridge"
        ) as mock_run_bridge, caplog.at_level(logging.WARNING, logger="ils_auto_quote"):
            mock_result = MagicMock()
            mock_result.warnings = ["cascade failed for pn=PN-123: simulated"]
            mock_result.cascade_results = []
            mock_result.trace_flags = []
            mock_run_bridge.return_value = mock_result

            result = engine._process_rfq(rfq)

        assert isinstance(result, ils_module.QuoteResult)
        assert result.success is True
        assert any("SDA bridge warnings" in msg for msg in caplog.messages)


@pytest.mark.integration
class TestBridgeImportSmoke:
    def test_module_has_run_bridge_symbol(self, ils_module):
        """SWA-002 AC: the import line must bind run_bridge and BridgeContext."""
        assert hasattr(ils_module, "run_bridge")
        assert hasattr(ils_module, "BridgeContext")

    def test_bridge_context_dataclass_fields(self, ils_module):
        """Verify BridgeContext is the real class from tools.auto_quote_bridge."""
        from tools.auto_quote_bridge import BridgeContext as RealBridgeContext
        assert ils_module.BridgeContext is RealBridgeContext
