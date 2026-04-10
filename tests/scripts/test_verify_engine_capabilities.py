"""Unit tests for scripts/verify_engine_capabilities.py.

All probes are mocked; no network or V11 calls are made.
"""

from __future__ import annotations

import json
import os

import pytest

from scripts.verify_engine_capabilities import (
    Capability,
    CapabilityResult,
    VerificationReport,
    generate_addendum_stubs,
    probe_lifo_teardown,
    probe_sru_lru_compression,
    run_verification,
    write_report,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_price_probe(prices: dict):
    """Return a probe(pn, condition) -> {'price': ...} or None.

    prices is keyed by (pn, condition).
    """
    def _probe(pn, condition):
        value = prices.get((pn, condition))
        if value is None:
            return None
        return {"price": value}
    return _probe


def _make_lifo_probe(payload):
    def _probe(_pn):
        return payload
    return _probe


# ---------------------------------------------------------------------------
# TestSruLruProbe
# ---------------------------------------------------------------------------

class TestSruLruProbe:
    def test_compliant_when_sru_tight_lru_wide(self):
        prices = {
            ("SRU-1", "AR"): 96.0,
            ("SRU-1", "SV"): 100.0,
            ("LRU-1", "AR"): 88.0,
            ("LRU-1", "SV"): 100.0,
        }
        probe = _make_price_probe(prices)
        result = probe_sru_lru_compression(
            "SRU-1", "LRU-1", probe, probe, compression_threshold_pct=12.0
        )
        assert result.compliant is True
        assert result.capability is Capability.SRU_LRU_COMPRESSION
        assert result.measured["sru_delta_pct"] == pytest.approx(4.0)
        assert result.measured["lru_delta_pct"] == pytest.approx(12.0)

    def test_non_compliant_when_sru_wide(self):
        prices = {
            ("SRU-1", "AR"): 90.0,
            ("SRU-1", "SV"): 100.0,  # 10% delta, not < 12? 10 < 12 is True.
            ("LRU-1", "AR"): 90.0,
            ("LRU-1", "SV"): 100.0,  # 10% delta, LRU not wide enough
        }
        probe = _make_price_probe(prices)
        result = probe_sru_lru_compression(
            "SRU-1", "LRU-1", probe, probe, compression_threshold_pct=12.0
        )
        # SRU tight enough but LRU too tight -> non compliant
        assert result.compliant is False

    def test_non_compliant_when_sru_wide_explicit(self):
        # SRU spread 15% which is >= threshold 12 -> non compliant regardless
        prices = {
            ("SRU-1", "AR"): 85.0,
            ("SRU-1", "SV"): 100.0,
            ("LRU-1", "AR"): 85.0,
            ("LRU-1", "SV"): 100.0,
        }
        probe = _make_price_probe(prices)
        result = probe_sru_lru_compression(
            "SRU-1", "LRU-1", probe, probe, compression_threshold_pct=12.0
        )
        assert result.compliant is False
        assert "SRU spread" in result.detail

    def test_non_compliant_when_lru_tight(self):
        prices = {
            ("SRU-1", "AR"): 97.0,  # 3% delta, tight
            ("SRU-1", "SV"): 100.0,
            ("LRU-1", "AR"): 96.0,  # 4% delta, too tight for LRU
            ("LRU-1", "SV"): 100.0,
        }
        probe = _make_price_probe(prices)
        result = probe_sru_lru_compression(
            "SRU-1", "LRU-1", probe, probe, compression_threshold_pct=12.0
        )
        assert result.compliant is False
        assert "LRU spread" in result.detail

    def test_non_compliant_when_sru_probe_returns_none(self):
        def sru_probe(_pn, _cond):
            return None

        lru_probe = _make_price_probe(
            {("LRU-1", "AR"): 88.0, ("LRU-1", "SV"): 100.0}
        )
        result = probe_sru_lru_compression("SRU-1", "LRU-1", sru_probe, lru_probe)
        assert result.compliant is False
        assert "SRU probe" in result.detail

    def test_non_compliant_when_lru_probe_returns_none(self):
        sru_probe = _make_price_probe(
            {("SRU-1", "AR"): 96.0, ("SRU-1", "SV"): 100.0}
        )

        def lru_probe(_pn, _cond):
            return None

        result = probe_sru_lru_compression("SRU-1", "LRU-1", sru_probe, lru_probe)
        assert result.compliant is False
        assert "LRU probe" in result.detail


# ---------------------------------------------------------------------------
# TestLifoProbe
# ---------------------------------------------------------------------------

class TestLifoProbe:
    def _lots(self):
        return [
            {"lot_id": "OLD", "teardown_cost": 100.0, "in_date": "2024-01-01"},
            {"lot_id": "MID", "teardown_cost": 200.0, "in_date": "2025-06-01"},
            {"lot_id": "NEW", "teardown_cost": 150.0, "in_date": "2026-03-01"},
        ]

    def test_compliant_when_newest_teardown_selected(self):
        payload = {"selected_lot_id": "NEW", "lots_available": self._lots()}
        result = probe_lifo_teardown("PN-1", _make_lifo_probe(payload))
        assert result.compliant is True
        assert result.measured["selected"] == "NEW"
        assert result.measured["expected"] == "NEW"
        assert result.measured["teardown_lots_considered"] == 3

    def test_non_compliant_when_oldest_selected_fifo_default(self):
        payload = {"selected_lot_id": "OLD", "lots_available": self._lots()}
        result = probe_lifo_teardown("PN-1", _make_lifo_probe(payload))
        assert result.compliant is False
        assert result.measured["selected"] == "OLD"
        assert result.measured["expected"] == "NEW"
        assert "FIFO" in result.detail

    def test_non_compliant_when_no_teardown_lots(self):
        lots = [
            {"lot_id": "A", "teardown_cost": 0, "in_date": "2025-01-01"},
            {"lot_id": "B", "teardown_cost": 0, "in_date": "2026-01-01"},
        ]
        payload = {"selected_lot_id": "B", "lots_available": lots}
        result = probe_lifo_teardown("PN-1", _make_lifo_probe(payload))
        assert result.compliant is False
        assert "not enough teardown-bearing lots" in result.detail

    def test_non_compliant_when_probe_returns_none(self):
        result = probe_lifo_teardown("PN-1", lambda _pn: None)
        assert result.compliant is False
        assert result.detail == "probe returned no data"


# ---------------------------------------------------------------------------
# TestAddendumStubs
# ---------------------------------------------------------------------------

class TestAddendumStubs:
    def test_creates_srulru_stub_when_missing(self, tmp_path):
        created = generate_addendum_stubs(
            [Capability.SRU_LRU_COMPRESSION], output_dir=str(tmp_path)
        )
        assert len(created) == 1
        path = created[0]
        assert path.endswith("prd-atlas-addendum-srulru-compression.json")
        assert os.path.exists(path)

    def test_creates_lifo_stub_when_missing(self, tmp_path):
        created = generate_addendum_stubs(
            [Capability.LIFO_TEARDOWN], output_dir=str(tmp_path)
        )
        assert len(created) == 1
        assert created[0].endswith("prd-atlas-addendum-lifo-teardown.json")
        assert os.path.exists(created[0])

    def test_creates_both_when_both_missing(self, tmp_path):
        created = generate_addendum_stubs(
            [Capability.SRU_LRU_COMPRESSION, Capability.LIFO_TEARDOWN],
            output_dir=str(tmp_path),
        )
        assert len(created) == 2
        for path in created:
            assert os.path.exists(path)

    def test_skips_existing_file(self, tmp_path):
        target = tmp_path / "prd-atlas-addendum-lifo-teardown.json"
        target.write_text('{"original": true}', encoding="utf-8")
        original_content = target.read_text(encoding="utf-8")

        created = generate_addendum_stubs(
            [Capability.LIFO_TEARDOWN], output_dir=str(tmp_path)
        )
        assert len(created) == 1
        assert created[0].endswith("(existed, skipped)")
        # File content must be unchanged
        assert target.read_text(encoding="utf-8") == original_content

    def test_output_is_valid_json(self, tmp_path):
        created = generate_addendum_stubs(
            [Capability.SRU_LRU_COMPRESSION], output_dir=str(tmp_path)
        )
        with open(created[0], "r", encoding="utf-8") as handle:
            data = json.load(handle)
        assert isinstance(data, dict)

    def test_output_has_expected_story_shape(self, tmp_path):
        created = generate_addendum_stubs(
            [Capability.SRU_LRU_COMPRESSION, Capability.LIFO_TEARDOWN],
            output_dir=str(tmp_path),
        )
        for path in created:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            assert "project" in data
            assert data["project"].startswith("atlas-addendum-")
            assert "branchName" in data
            assert "userStories" in data
            stories = data["userStories"]
            assert len(stories) >= 1
            story = stories[0]
            assert story["id"] == "AA-001"
            assert isinstance(story["acceptanceCriteria"], list)
            assert len(story["acceptanceCriteria"]) > 0
            assert story["passes"] is False


# ---------------------------------------------------------------------------
# TestRunVerification
# ---------------------------------------------------------------------------

class TestRunVerification:
    def test_all_compliant_no_stubs(self, tmp_path):
        sru_prices = {
            ("SRU-1", "AR"): 96.0,
            ("SRU-1", "SV"): 100.0,
        }
        lru_prices = {
            ("LRU-1", "AR"): 88.0,
            ("LRU-1", "SV"): 100.0,
        }
        sru_probe = _make_price_probe(sru_prices)
        lru_probe = _make_price_probe(lru_prices)

        lifo_payload = {
            "selected_lot_id": "NEW",
            "lots_available": [
                {"lot_id": "OLD", "teardown_cost": 100.0, "in_date": "2024-01-01"},
                {"lot_id": "NEW", "teardown_cost": 200.0, "in_date": "2026-01-01"},
            ],
        }

        report = run_verification(
            sru_probe,
            lru_probe,
            _make_lifo_probe(lifo_payload),
            sru_pn="SRU-1",
            lru_pn="LRU-1",
            lifo_pn="PN-1",
            output_dir=str(tmp_path),
        )

        assert report.missing == []
        assert report.addendum_files_created == []
        # No addendum files should be written to tmp_path
        addendum_files = [
            p for p in tmp_path.iterdir()
            if p.name.startswith("prd-atlas-addendum-")
        ]
        assert addendum_files == []

    def test_all_missing_two_stubs(self, tmp_path):
        def null_probe(*_a, **_kw):
            return None

        report = run_verification(
            null_probe,
            null_probe,
            null_probe,
            output_dir=str(tmp_path),
        )

        assert len(report.missing) == 2
        assert Capability.SRU_LRU_COMPRESSION in report.missing
        assert Capability.LIFO_TEARDOWN in report.missing
        assert len(report.addendum_files_created) == 2
        for path in report.addendum_files_created:
            assert os.path.exists(path)


# ---------------------------------------------------------------------------
# TestWriteReport
# ---------------------------------------------------------------------------

class TestWriteReport:
    def test_report_roundtrips(self, tmp_path):
        report = VerificationReport(
            timestamp="2026-04-09T00:00:00+00:00",
            capabilities=[
                CapabilityResult(
                    capability=Capability.SRU_LRU_COMPRESSION,
                    compliant=True,
                    detail="ok",
                    measured={"sru_delta_pct": 4.0, "lru_delta_pct": 12.0},
                ),
                CapabilityResult(
                    capability=Capability.LIFO_TEARDOWN,
                    compliant=False,
                    detail="FIFO detected",
                    measured={"selected": "OLD", "expected": "NEW"},
                ),
            ],
            missing=[Capability.LIFO_TEARDOWN],
            addendum_files_created=[
                str(tmp_path / "prd-atlas-addendum-lifo-teardown.json")
            ],
        )

        path = str(tmp_path / "report.json")
        write_report(report, path)

        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)

        assert data["timestamp"] == "2026-04-09T00:00:00+00:00"
        assert len(data["capabilities"]) == 2
        assert data["capabilities"][0]["capability"] == "sru_lru_compression"
        assert data["capabilities"][0]["compliant"] is True
        assert data["capabilities"][1]["capability"] == "lifo_teardown"
        assert data["missing"] == ["lifo_teardown"]
        assert len(data["addendum_files_created"]) == 1
