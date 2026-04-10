"""SWA-005: Jorge cost sanity fallback + narrowed exception handling.

Tests the two polish improvements from Gemini 2.5 Pro's audit:
  1. Jorge email cost override rejects wildly out-of-band values compared
     to the Kent Ext Cost baseline (default 3x ratio).
  2. _fetch_jorge_emails narrows its catch-all to named exception types so
     unexpected errors surface at WARNING level instead of being swallowed.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from tools.summit_sheet_tool import (
    SummitSheetHit,
    _fetch_jorge_emails,
    _jorge_cost_sanity_ratio,
    _merge_jorge_override,
    _reset_cache,
    summit_sheet_lookup,
)


def _hit(kent_cost: float, pn: str = "TEST-PN", meimin: str = "2026-01-01") -> SummitSheetHit:
    return SummitSheetHit(
        pn=pn,
        description="Test part",
        aircraft=None,
        conditions=["AR"],
        quantity=1.0,
        kent_ext_cost=kent_cost,
        may_buy_price=None,
        meimin_date=meimin,
        our_quote_sell=None,
        num_quotes=None,
        ils_sellers=None,
        ils_qty=None,
        aac_on_ils=None,
        summit_on_ils=None,
        ils_price_range=None,
        jf_price=None,
        sheet_row_number=42,
        cost_basis=kent_cost,
        cost_source="kent_ext_cost",
        summit_guidance=None,
        guidance_source=None,
        sheet_stale=False,
        provenance={},
    )


class TestSanityRatioEnvVar:
    def test_default_ratio_is_3(self):
        prev = os.environ.pop("HERMES_JORGE_COST_SANITY_RATIO", None)
        try:
            assert _jorge_cost_sanity_ratio() == 3.0
        finally:
            if prev is not None:
                os.environ["HERMES_JORGE_COST_SANITY_RATIO"] = prev

    def test_env_var_overrides_default(self):
        os.environ["HERMES_JORGE_COST_SANITY_RATIO"] = "10.0"
        try:
            assert _jorge_cost_sanity_ratio() == 10.0
        finally:
            os.environ.pop("HERMES_JORGE_COST_SANITY_RATIO", None)

    def test_invalid_env_var_falls_back_to_default(self):
        os.environ["HERMES_JORGE_COST_SANITY_RATIO"] = "not-a-number"
        try:
            assert _jorge_cost_sanity_ratio() == 3.0
        finally:
            os.environ.pop("HERMES_JORGE_COST_SANITY_RATIO", None)


class TestJorgeCostSanityCheck:
    def setup_method(self):
        _reset_cache()
        os.environ.pop("HERMES_JORGE_COST_SANITY_RATIO", None)

    def teardown_method(self):
        _reset_cache()
        os.environ.pop("HERMES_JORGE_COST_SANITY_RATIO", None)

    def test_override_rejected_when_jorge_cost_is_10x_kent(self):
        """AC: mock Jorge email returns cost 10x Kent, override rejected,
        provenance carries sanity_check_rejected_jorge_cost."""
        hit = _hit(kent_cost=100.0)
        emails = [
            {
                "message_id": "jorge-1",
                "date": "2026-02-01",
                "body": "for TEST-PN Summit Cost $ 1000.00 try to max the sale",
            }
        ]
        _merge_jorge_override(hit, emails)
        # Override rejected: cost_basis stays at Kent value.
        assert hit.cost_basis == 100.0
        assert hit.cost_source == "kent_ext_cost"
        assert "sanity_check_rejected_jorge_cost" in hit.provenance
        rejected = hit.provenance["sanity_check_rejected_jorge_cost"]
        assert rejected["jorge_cost"] == 1000.0
        assert rejected["kent_ext_cost"] == 100.0
        assert rejected["ratio_threshold"] == 3.0

    def test_override_accepted_when_jorge_cost_within_threshold(self):
        """Jorge cost 2x Kent (under 3x threshold) is accepted normally."""
        hit = _hit(kent_cost=100.0)
        emails = [
            {
                "message_id": "jorge-2",
                "date": "2026-02-01",
                "body": "for TEST-PN Summit Cost $ 200.00 try to max the sale",
            }
        ]
        _merge_jorge_override(hit, emails)
        assert hit.cost_basis == 200.0
        assert hit.cost_source == "jorge_email"
        assert "sanity_check_rejected_jorge_cost" not in hit.provenance
        assert hit.provenance.get("jorge_email_id") == "jorge-2"

    def test_env_var_raises_threshold_to_allow_5x_deviation(self):
        """HERMES_JORGE_COST_SANITY_RATIO=10.0 allows a 5x deviation that
        would have been rejected under the default."""
        os.environ["HERMES_JORGE_COST_SANITY_RATIO"] = "10.0"
        hit = _hit(kent_cost=100.0)
        emails = [
            {
                "message_id": "jorge-3",
                "date": "2026-02-01",
                "body": "for TEST-PN Summit Cost $ 500.00 try to max the sale",
            }
        ]
        _merge_jorge_override(hit, emails)
        assert hit.cost_basis == 500.0
        assert hit.cost_source == "jorge_email"
        assert "sanity_check_rejected_jorge_cost" not in hit.provenance

    def test_override_rejected_when_jorge_cost_below_1_third_kent(self):
        """A Jorge cost 1/5 of Kent (below 1/3 floor) is also rejected."""
        hit = _hit(kent_cost=1000.0)
        emails = [
            {
                "message_id": "jorge-4",
                "date": "2026-02-01",
                "body": "for TEST-PN Summit Cost $ 200.00 try to max the sale",
            }
        ]
        _merge_jorge_override(hit, emails)
        assert hit.cost_basis == 1000.0
        assert hit.cost_source == "kent_ext_cost"
        assert "sanity_check_rejected_jorge_cost" in hit.provenance

    def test_no_kent_baseline_skips_sanity_check(self):
        """If Kent Ext Cost is None, the sanity check cannot fire; accept."""
        hit = _hit(kent_cost=0.0)
        hit.kent_ext_cost = None
        emails = [
            {
                "message_id": "jorge-5",
                "date": "2026-02-01",
                "body": "for TEST-PN Summit Cost $ 9999.00 try to max the sale",
            }
        ]
        _merge_jorge_override(hit, emails)
        assert hit.cost_basis == 9999.0
        assert hit.cost_source == "jorge_email"


class _FakeTokenPath:
    """Stand-in for JORGE_GMAIL_TOKEN_PATH — Path attrs are read-only."""

    def __init__(self, exists: bool):
        self._exists = exists

    def exists(self) -> bool:
        return self._exists

    def __fspath__(self) -> str:
        return "/tmp/fake-jorge-token.json"

    def __str__(self) -> str:
        return self.__fspath__()


class TestNarrowedExceptionHandling:
    """SWA-005: _fetch_jorge_emails catches specific exception types with
    DEBUG logs, and uses a WARNING-level safety net for anything unexpected.
    """

    def test_unexpected_exception_returns_empty_list_with_warning(self, caplog):
        """A random RuntimeError is caught by the safety net and logged
        at WARNING level so it's visible but doesn't crash the lookup."""
        import logging

        with patch(
            "tools.summit_sheet_tool.JORGE_GMAIL_TOKEN_PATH", _FakeTokenPath(True)
        ), patch(
            "google.oauth2.credentials.Credentials.from_authorized_user_file",
            side_effect=RuntimeError("unexpected boom"),
        ), caplog.at_level(logging.WARNING, logger="tools.summit_sheet_tool"):
            result = _fetch_jorge_emails("TEST-PN")
        assert result == []
        assert any("UNEXPECTED" in msg for msg in caplog.messages)
        assert any("RuntimeError" in msg for msg in caplog.messages)

    def test_file_not_found_returns_empty_without_warning(self, caplog):
        """FileNotFoundError is a named failure mode and logs at DEBUG only."""
        import logging

        with patch(
            "tools.summit_sheet_tool.JORGE_GMAIL_TOKEN_PATH", _FakeTokenPath(True)
        ), patch(
            "google.oauth2.credentials.Credentials.from_authorized_user_file",
            side_effect=FileNotFoundError("creds missing"),
        ), caplog.at_level(logging.WARNING, logger="tools.summit_sheet_tool"):
            result = _fetch_jorge_emails("TEST-PN")
        assert result == []
        # No WARNING-level message — FileNotFoundError is a recognized mode.
        assert not any("UNEXPECTED" in msg for msg in caplog.messages)

    def test_timeout_error_returns_empty_without_warning(self, caplog):
        """TimeoutError is also a named failure mode."""
        import logging

        with patch(
            "tools.summit_sheet_tool.JORGE_GMAIL_TOKEN_PATH", _FakeTokenPath(True)
        ), patch(
            "google.oauth2.credentials.Credentials.from_authorized_user_file",
            side_effect=TimeoutError("gmail timed out"),
        ), caplog.at_level(logging.WARNING, logger="tools.summit_sheet_tool"):
            result = _fetch_jorge_emails("TEST-PN")
        assert result == []
        assert not any("UNEXPECTED" in msg for msg in caplog.messages)

    def test_missing_token_returns_empty_without_touching_google(self):
        """When the Jorge token file doesn't exist, return [] immediately."""
        with patch(
            "tools.summit_sheet_tool.JORGE_GMAIL_TOKEN_PATH", _FakeTokenPath(False)
        ):
            assert _fetch_jorge_emails("TEST-PN") == []
