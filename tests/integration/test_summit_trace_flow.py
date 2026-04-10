"""Integration tests for the Summit trace flag emitter.

These hit the real Summit 8140 pricing XLSX on disk via the SDA-001
``summit_sheet_lookup`` tool. Marked ``integration`` so they only run
with ``-m integration``.
"""

from unittest.mock import patch

import pytest

from tools.summit_sheet_tool import _reset_cache
from tools.summit_trace_flags import QuoteLine, emit_trace_flags


@pytest.mark.integration
class TestRealSheetTraceFlow:
    def setup_method(self):
        _reset_cache()

    def teardown_method(self):
        _reset_cache()

    def test_real_sheet_summit_line_produces_145_trace(self):
        line = QuoteLine(
            pn="3605812-17", condition="SV", is_idg_piece_part=False
        )
        with patch(
            "tools.summit_sheet_tool._fetch_jorge_emails", return_value=[]
        ):
            flags = emit_trace_flags([line])
        assert len(flags) == 1
        assert flags[0].summit_consignment is True
        assert flags[0].trace_type == "145"
        assert flags[0].tag_source == "Summit Aerospace"

    def test_real_sheet_unknown_pn_with_sv_condition(self):
        line = QuoteLine(pn="NONEXISTENT999", condition="SV")
        with patch(
            "tools.summit_sheet_tool._fetch_jorge_emails", return_value=[]
        ):
            flags = emit_trace_flags([line])
        assert flags[0].summit_consignment is False
        assert flags[0].trace_type == "8130"

    def test_real_sheet_unknown_pn_with_ar_condition(self):
        line = QuoteLine(pn="NONEXISTENT999", condition="AR")
        with patch(
            "tools.summit_sheet_tool._fetch_jorge_emails", return_value=[]
        ):
            flags = emit_trace_flags([line])
        assert flags[0].summit_consignment is False
        assert flags[0].trace_type is None
