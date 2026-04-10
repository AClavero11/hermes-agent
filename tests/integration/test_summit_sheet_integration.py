"""Integration tests for the Summit sheet tool against the real XLSX."""

from unittest.mock import patch

import pytest

from tools.summit_sheet_tool import (
    _get_cache,
    _reset_cache,
    summit_sheet_lookup,
)


@pytest.mark.integration
class TestRealSheetIntegration:
    def setup_method(self):
        _reset_cache()

    def teardown_method(self):
        _reset_cache()

    def test_all_six_jorge_email_pns_present(self):
        expected = {
            "3605812-17": 444.95,
            "273T1102-8": 4397.09,
            "273T6301-5": 520.51,
            "273T6101-9": 5383.94,
            "2206405-1": 5000.0,
            "2206407-1": 6500.0,
        }
        with patch(
            "tools.summit_sheet_tool._fetch_jorge_emails", return_value=[]
        ):
            for pn, cost in expected.items():
                result = summit_sheet_lookup(pn)
                assert result is not None, f"{pn} missing from sheet"
                assert result["kent_ext_cost"] == cost, (
                    f"{pn} kent_ext_cost mismatch: "
                    f"expected {cost}, got {result['kent_ext_cost']}"
                )

    def test_sheet_has_at_least_130_rows(self):
        with patch(
            "tools.summit_sheet_tool._fetch_jorge_emails", return_value=[]
        ):
            # Force a load through the public API.
            result = summit_sheet_lookup("3605812-17")
            assert result is not None
        cache = _get_cache()
        assert len(cache) >= 130
