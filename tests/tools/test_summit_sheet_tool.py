"""Tests for the Summit 8140 sheet lookup tool."""

import os
from unittest.mock import patch

import pytest

from tools.summit_sheet_tool import (
    PrivacyWallViolation,
    _handle_summit_lookup,
    _reset_cache,
    check_summit_requirements,
    summit_sheet_lookup,
)


def _clear_cache():
    """Drop the module cache between tests so stale rows don't leak."""
    _reset_cache()


class TestSheetLookup:
    def setup_method(self):
        _clear_cache()

    def teardown_method(self):
        _clear_cache()

    def test_returns_none_for_unknown_pn(self):
        with patch(
            "tools.summit_sheet_tool._fetch_jorge_emails", return_value=[]
        ):
            result = summit_sheet_lookup("NONEXISTENT999")
        assert result is None

    def test_known_pn_3605812_17(self):
        with patch(
            "tools.summit_sheet_tool._fetch_jorge_emails", return_value=[]
        ):
            result = summit_sheet_lookup("3605812-17")
        assert result is not None
        assert result["kent_ext_cost"] == 444.95
        assert "STARTER" in (result["description"] or "")
        assert result["aircraft"] == "B737"

    def test_known_pn_273T1102_8(self):
        with patch(
            "tools.summit_sheet_tool._fetch_jorge_emails", return_value=[]
        ):
            result = summit_sheet_lookup("273T1102-8")
        assert result is not None
        assert result["kent_ext_cost"] == 4397.09
        assert result["may_buy_price"] == 32500.0

    def test_known_pn_273T6301_5_kent_cost(self):
        with patch(
            "tools.summit_sheet_tool._fetch_jorge_emails", return_value=[]
        ):
            result = summit_sheet_lookup("273T6301-5")
        assert result is not None
        assert result["kent_ext_cost"] == 520.51

    def test_known_pn_273T6101_9_kent_cost(self):
        with patch(
            "tools.summit_sheet_tool._fetch_jorge_emails", return_value=[]
        ):
            result = summit_sheet_lookup("273T6101-9")
        assert result is not None
        assert result["kent_ext_cost"] == 5383.94

    def test_known_pn_2206405_1_kent_cost(self):
        with patch(
            "tools.summit_sheet_tool._fetch_jorge_emails", return_value=[]
        ):
            result = summit_sheet_lookup("2206405-1")
        assert result is not None
        assert result["kent_ext_cost"] == 5000.0

    def test_known_pn_2206407_1_kent_cost(self):
        with patch(
            "tools.summit_sheet_tool._fetch_jorge_emails", return_value=[]
        ):
            result = summit_sheet_lookup("2206407-1")
        assert result is not None
        assert result["kent_ext_cost"] == 6500.0

    def test_compound_condition_expansion_273T6301_5(self):
        with patch(
            "tools.summit_sheet_tool._fetch_jorge_emails", return_value=[]
        ):
            result = summit_sheet_lookup("273T6301-5")
        assert result is not None
        assert result["conditions"] == ["AR", "OH"]

    def test_pn_767270_not_in_sheet(self):
        with patch(
            "tools.summit_sheet_tool._fetch_jorge_emails", return_value=[]
        ):
            result = summit_sheet_lookup("767270")
        assert result is None


class TestRequirementsCheck:
    def setup_method(self):
        _clear_cache()

    def teardown_method(self):
        _clear_cache()

    def test_check_summit_requirements_true_when_sheet_exists(self):
        assert check_summit_requirements() is True


class TestPrivacyWall:
    def setup_method(self):
        _clear_cache()
        self._prev = os.environ.get("HERMES_TOOL_REMOTE_EXEC")

    def teardown_method(self):
        _clear_cache()
        if self._prev is None:
            os.environ.pop("HERMES_TOOL_REMOTE_EXEC", None)
        else:
            os.environ["HERMES_TOOL_REMOTE_EXEC"] = self._prev

    def test_privacy_wall_violation_when_remote_env_set(self):
        os.environ["HERMES_TOOL_REMOTE_EXEC"] = "1"
        with pytest.raises(PrivacyWallViolation):
            _handle_summit_lookup({"pn": "273T1102-8"})


class TestStalenessOverride:
    def setup_method(self):
        _clear_cache()

    def teardown_method(self):
        _clear_cache()

    def test_staleness_override_with_mock_jorge_email(self):
        """Jorge email newer than Meimin Date must override cost basis."""
        fake_emails = [
            {
                "message_id": "msg-abc-123",
                "date": "2026-04-08",  # newer than 2026-03-17 Meimin Date
                "body": (
                    "Hi team, for 273T1102-8 I'd try to sale over your "
                    "cost 22,500.00 to be safe. Summit's holding firm."
                ),
            }
        ]
        with patch(
            "tools.summit_sheet_tool._fetch_jorge_emails",
            return_value=fake_emails,
        ):
            result = summit_sheet_lookup("273T1102-8")

        assert result is not None
        assert result["cost_basis"] == 22500.0
        assert result["cost_source"] == "jorge_email"
        assert result["sheet_stale"] is True
        assert result["summit_guidance"] == "over_cost"
        assert result["guidance_source"] == "msg-abc-123"
        assert result["provenance"]["jorge_email_id"] == "msg-abc-123"
        assert result["provenance"]["jorge_email_date"] == "2026-04-08"

    def test_no_jorge_email_falls_through_to_kent(self):
        with patch(
            "tools.summit_sheet_tool._fetch_jorge_emails", return_value=[]
        ):
            result = summit_sheet_lookup("273T1102-8")

        assert result is not None
        assert result["cost_basis"] == result["kent_ext_cost"]
        assert result["cost_source"] == "kent_ext_cost"
        assert result["sheet_stale"] is False

    def test_provenance_resets_between_calls(self):
        """Regression: provenance/guidance must not leak across sequential lookups on the same PN.

        A Jorge override on call #1 must NOT leave stale jorge_email_id on
        a subsequent call #2 that has no matching emails. The module-level
        cache returns the same SummitSheetHit reference, so every merge
        pass must fully reset the computed fields.
        """
        fake_emails = [
            {
                "message_id": "msg-stale-test",
                "date": "2026-04-08",
                "body": (
                    "Hi team, for 273T1102-8 try to sale over your cost "
                    "22,500.00 to be safe."
                ),
            }
        ]

        # Call 1: with Jorge override
        with patch(
            "tools.summit_sheet_tool._fetch_jorge_emails",
            return_value=fake_emails,
        ):
            first = summit_sheet_lookup("273T1102-8")

        assert first is not None
        assert first["cost_source"] == "jorge_email"
        assert first["provenance"].get("jorge_email_id") == "msg-stale-test"
        assert first["summit_guidance"] == "over_cost"

        # Call 2: no emails — must fall back cleanly with NO stale state
        with patch(
            "tools.summit_sheet_tool._fetch_jorge_emails", return_value=[]
        ):
            second = summit_sheet_lookup("273T1102-8")

        assert second is not None
        assert second["cost_source"] == "kent_ext_cost"
        assert second["cost_basis"] == second["kent_ext_cost"]
        assert second["sheet_stale"] is False
        assert second["summit_guidance"] is None
        assert second["guidance_source"] is None
        assert second["provenance"] == {}


class TestMultiPnEmailParsing:
    """Regression tests for PN-scoped parsing of multi-PN Jorge emails."""

    def setup_method(self):
        _clear_cache()

    def teardown_method(self):
        _clear_cache()

    MULTI_PN_BODY = (
        "3605812-17   Summit Cost $ 444.00 see what you can max the sale. 70/30\n"
        "\n"
        "273T1102-8  try to sale over your cost 22,500.00\n"
        "\n"
        "273T6301-5 Summit Cost $ 520.00 try to max the sale 70/30\n"
        "\n"
        "273T6101-9,  Summit cost, $ 5,400.00 try to max the sale 70/30\n"
        "\n"
        "2206405-1 ,  Summit cost $ 0 one the other one 5K try to max the sale 70/30\n"
    )

    def _mock_email(self):
        return [
            {
                "message_id": "msg-multi-pn",
                "date": "2026-04-09",
                "body": self.MULTI_PN_BODY,
            }
        ]

    def test_3605812_17_gets_70_30_not_over_cost(self):
        """Regression: 3605812-17's 70/30 guidance must not be overridden
        by the 'over your cost' phrase that belongs to 273T1102-8 later
        in the same email body."""
        with patch(
            "tools.summit_sheet_tool._fetch_jorge_emails",
            return_value=self._mock_email(),
        ):
            result = summit_sheet_lookup("3605812-17")
        assert result is not None
        assert result["cost_basis"] == 444.0
        assert result["summit_guidance"] == "70_30"
        assert result["cost_source"] == "jorge_email"

    def test_273T1102_8_gets_over_cost_22500(self):
        """273T1102-8 must pick up its own 'over your cost 22,500' and NOT
        the earlier 'Summit Cost $ 444' from 3605812-17."""
        with patch(
            "tools.summit_sheet_tool._fetch_jorge_emails",
            return_value=self._mock_email(),
        ):
            result = summit_sheet_lookup("273T1102-8")
        assert result is not None
        assert result["cost_basis"] == 22500.0
        assert result["summit_guidance"] == "over_cost"

    def test_273T6301_5_gets_own_520_cost(self):
        """273T6301-5 must pick up its own Summit Cost $520, not 3605812-17's $444."""
        with patch(
            "tools.summit_sheet_tool._fetch_jorge_emails",
            return_value=self._mock_email(),
        ):
            result = summit_sheet_lookup("273T6301-5")
        assert result is not None
        assert result["cost_basis"] == 520.0
        assert result["summit_guidance"] == "70_30"

    def test_273T6101_9_gets_own_5400_cost(self):
        """273T6101-9's cost $5,400 must be its own, not neighbors'."""
        with patch(
            "tools.summit_sheet_tool._fetch_jorge_emails",
            return_value=self._mock_email(),
        ):
            result = summit_sheet_lookup("273T6101-9")
        assert result is not None
        assert result["cost_basis"] == 5400.0
        assert result["summit_guidance"] == "70_30"
