"""Unit tests for the Summit trace flag emitter."""

from dataclasses import asdict
from unittest.mock import patch

from tools.summit_trace_flags import QuoteLine, TraceFlags, emit_trace_flags


_SUMMIT_HIT_BASE = {
    "pn": "3605812-17",
    "cost_basis": 444.95,
    "summit_guidance": None,
    "cost_source": "kent_ext_cost",
    "sheet_stale": False,
}


def _summit_hit(**overrides):
    out = dict(_SUMMIT_HIT_BASE)
    out.update(overrides)
    return out


class TestSummitMembership:
    def test_summit_non_idg_line(self):
        line = QuoteLine(pn="3605812-17", condition="AR", is_idg_piece_part=False)
        with patch(
            "tools.summit_trace_flags.summit_sheet_lookup",
            return_value=_summit_hit(),
        ):
            flags = emit_trace_flags([line])
        assert len(flags) == 1
        assert flags[0].summit_consignment is True
        assert flags[0].trace_type == "145"
        assert flags[0].tag_source == "Summit Aerospace"
        assert flags[0].idg_piece_part_warning is False

    def test_summit_idg_piece_part_line(self):
        line = QuoteLine(
            pn="3605812-17", condition="SV", is_idg_piece_part=True
        )
        with patch(
            "tools.summit_trace_flags.summit_sheet_lookup",
            return_value=_summit_hit(),
        ):
            flags = emit_trace_flags([line])
        assert flags[0].idg_piece_part_warning is True
        assert flags[0].trace_type == "145"
        # Hard lock field must not exist on output.
        assert "requires_summit_approval" not in asdict(flags[0])

    def test_summit_cost_and_guidance_populated_when_summit(self):
        line = QuoteLine(pn="273T6301-5", condition="SV")
        with patch(
            "tools.summit_trace_flags.summit_sheet_lookup",
            return_value=_summit_hit(
                pn="273T6301-5", cost_basis=520.0, summit_guidance="70_30"
            ),
        ):
            flags = emit_trace_flags([line])
        assert flags[0].summit_cost_recent == 520.0
        assert flags[0].summit_guidance == "70_30"


class TestNonSummitTrace:
    def test_non_summit_sv_line(self):
        line = QuoteLine(pn="UNKNOWN1", condition="SV")
        with patch(
            "tools.summit_trace_flags.summit_sheet_lookup", return_value=None
        ):
            flags = emit_trace_flags([line])
        assert flags[0].summit_consignment is False
        assert flags[0].trace_type == "8130"
        assert flags[0].tag_source is None
        assert flags[0].summit_cost_recent is None
        assert flags[0].summit_guidance is None

    def test_non_summit_ar_line(self):
        line = QuoteLine(pn="UNKNOWN2", condition="AR")
        with patch(
            "tools.summit_trace_flags.summit_sheet_lookup", return_value=None
        ):
            flags = emit_trace_flags([line])
        assert flags[0].trace_type is None
        assert flags[0].summit_consignment is False

    def test_non_summit_new_line(self):
        line = QuoteLine(pn="UNKNOWN3", condition="New")
        with patch(
            "tools.summit_trace_flags.summit_sheet_lookup", return_value=None
        ):
            flags = emit_trace_flags([line])
        assert flags[0].trace_type == "8130"

    def test_non_summit_oh_line(self):
        line = QuoteLine(pn="UNKNOWN4", condition="OH")
        with patch(
            "tools.summit_trace_flags.summit_sheet_lookup", return_value=None
        ):
            flags = emit_trace_flags([line])
        assert flags[0].trace_type == "8130"

    def test_non_summit_ne_line(self):
        line = QuoteLine(pn="UNKNOWN5", condition="NE")
        with patch(
            "tools.summit_trace_flags.summit_sheet_lookup", return_value=None
        ):
            flags = emit_trace_flags([line])
        assert flags[0].trace_type == "8130"

    def test_non_summit_ns_line(self):
        line = QuoteLine(pn="UNKNOWN6", condition="NS")
        with patch(
            "tools.summit_trace_flags.summit_sheet_lookup", return_value=None
        ):
            flags = emit_trace_flags([line])
        assert flags[0].trace_type == "8130"

    def test_non_summit_unknown_condition(self):
        line = QuoteLine(pn="UNKNOWN7", condition="XYZ")
        with patch(
            "tools.summit_trace_flags.summit_sheet_lookup", return_value=None
        ):
            flags = emit_trace_flags([line])
        assert flags[0].trace_type is None


class TestPassthroughs:
    def test_per_line_check_required_passthrough(self):
        line = QuoteLine(
            pn="UNKNOWN8",
            condition="SV",
            gmail_caveat="check w/ Sergio",
        )
        with patch(
            "tools.summit_trace_flags.summit_sheet_lookup", return_value=None
        ):
            flags = emit_trace_flags([line])
        assert flags[0].per_line_check_required == "check w/ Sergio"


class TestNoHardLock:
    def test_no_requires_summit_approval_field_ever(self):
        lines = [
            QuoteLine(pn="3605812-17", condition="AR", is_idg_piece_part=True),
            QuoteLine(pn="3605812-17", condition="SV", is_idg_piece_part=True),
            QuoteLine(pn="UNKNOWN_A", condition="AR"),
            QuoteLine(pn="UNKNOWN_B", condition="SV"),
            QuoteLine(pn="UNKNOWN_C", condition="New"),
            QuoteLine(pn="UNKNOWN_D", condition="XYZ"),
        ]

        def fake_lookup(pn):
            if pn == "3605812-17":
                return _summit_hit()
            return None

        with patch(
            "tools.summit_trace_flags.summit_sheet_lookup",
            side_effect=fake_lookup,
        ):
            flags = emit_trace_flags(lines)

        assert len(flags) == len(lines)
        for flag in flags:
            assert isinstance(flag, TraceFlags)
            assert "requires_summit_approval" not in asdict(flag)
