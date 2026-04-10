"""Tests for the no-price cascade orchestrator (SDA-003)."""

from unittest.mock import Mock

from tools.no_price_cascade import (
    CascadeContext,
    CascadeResult,
    CascadeStep,
    ConfidenceFlag,
    run_cascade,
)


def _ctx(pn: str = "PN-123") -> CascadeContext:
    return CascadeContext(pn=pn, customer="ACME", condition="AR")


def _mocks(
    engine=None,
    sheet=None,
    history=None,
    solicit=None,
    manual=None,
):
    return {
        "engine": Mock(return_value=engine),
        "sheet": Mock(return_value=sheet),
        "history": Mock(return_value=history),
        "solicit": Mock(return_value=solicit),
        "manual": Mock(return_value=manual),
    }


class TestShortCircuiting:
    def test_engine_hit_returns_immediately(self):
        mocks = _mocks(
            engine={"cost_basis": 1234.56, "provenance": {"src": "v11"}},
        )
        result = run_cascade(_ctx(), **mocks)

        assert isinstance(result, CascadeResult)
        assert result.source == CascadeStep.V11_ENGINE
        assert result.confidence == ConfidenceFlag.GREEN
        assert result.cost_basis == 1234.56
        mocks["engine"].assert_called_once()
        mocks["sheet"].assert_not_called()
        mocks["history"].assert_not_called()
        mocks["solicit"].assert_not_called()
        mocks["manual"].assert_not_called()

    def test_sheet_hit_after_engine_miss(self):
        mocks = _mocks(
            engine=None,
            sheet={
                "cost_basis": 500.0,
                "sheet_stale": True,
                "provenance": {"sheet_row": 42},
            },
        )
        result = run_cascade(_ctx(), **mocks)

        assert result.source == CascadeStep.SUMMIT_SHEET
        assert result.confidence == ConfidenceFlag.GREEN
        assert result.cost_basis == 500.0
        assert result.sheet_stale is True
        mocks["engine"].assert_called_once()
        mocks["sheet"].assert_called_once()
        mocks["history"].assert_not_called()
        mocks["solicit"].assert_not_called()
        mocks["manual"].assert_not_called()

    def test_history_hit_after_sheet_miss(self):
        mocks = _mocks(
            engine=None,
            sheet=None,
            history={
                "weighted_median": 100.0,
                "match_count": 3,
                "provenance": {"threads": ["t1", "t2", "t3"]},
            },
        )
        result = run_cascade(_ctx(), **mocks)

        assert result.source == CascadeStep.GMAIL_HISTORY
        assert result.confidence == ConfidenceFlag.YELLOW
        assert result.cost_basis == 100.0
        mocks["history"].assert_called_once()
        mocks["solicit"].assert_not_called()
        mocks["manual"].assert_not_called()


class TestHistoryThreshold:
    def test_history_below_threshold_falls_through(self):
        mocks = _mocks(
            engine=None,
            sheet=None,
            history={"weighted_median": 99.0, "match_count": 1},
            solicit={"cost_basis": 222.0, "provenance": {"vendor": "v1"}},
        )
        result = run_cascade(_ctx(), **mocks)

        # History had match_count=1 which is under the default min of 2.
        assert result.source == CascadeStep.OUTBOUND_SOLICIT
        assert result.cost_basis == 222.0
        mocks["history"].assert_called_once()
        mocks["solicit"].assert_called_once()
        mocks["manual"].assert_not_called()

    def test_history_exactly_at_threshold_hits(self):
        mocks = _mocks(
            engine=None,
            sheet=None,
            history={"weighted_median": 77.0, "match_count": 2},
        )
        result = run_cascade(_ctx(), **mocks, history_min_matches=2)

        assert result.source == CascadeStep.GMAIL_HISTORY
        assert result.cost_basis == 77.0


class TestSummitBranch:
    def test_summit_associated_skips_solicit(self):
        # Sheet returns a dict (PN is Summit-associated) but no price.
        mocks = _mocks(
            engine=None,
            sheet={"cost_basis": None, "sheet_stale": False},
            history=None,
            solicit={"cost_basis": 999.0},  # should NOT be called
            manual={"cost_basis": 50.0, "provenance": {"asker": "ac"}},
        )
        result = run_cascade(_ctx(), **mocks)

        assert result.source == CascadeStep.MANUAL_TELEGRAM
        assert result.cost_basis == 50.0
        mocks["solicit"].assert_not_called()
        mocks["manual"].assert_called_once()

    def test_non_summit_pn_calls_solicit(self):
        mocks = _mocks(
            engine=None,
            sheet=None,  # not Summit-associated
            history=None,
            solicit={"cost_basis": 321.0, "provenance": {"vendor": "pmi"}},
        )
        result = run_cascade(_ctx(), **mocks)

        assert result.source == CascadeStep.OUTBOUND_SOLICIT
        assert result.cost_basis == 321.0
        mocks["solicit"].assert_called_once()
        mocks["manual"].assert_not_called()


class TestTotalMiss:
    def test_all_sources_miss_returns_red(self):
        mocks = _mocks()  # everything returns None
        result = run_cascade(_ctx(), **mocks)

        assert result.source is None
        assert result.confidence == ConfidenceFlag.RED
        assert result.cost_basis is None
        assert "error" in result.provenance

    def test_summit_associated_total_miss_skips_solicit(self):
        mocks = _mocks(
            engine=None,
            sheet={"cost_basis": None},
            history=None,
            solicit={"cost_basis": 1.0},  # must not be called
            manual=None,
        )
        result = run_cascade(_ctx(), **mocks)

        assert result.source is None
        assert result.confidence == ConfidenceFlag.RED
        mocks["solicit"].assert_not_called()
        mocks["manual"].assert_called_once()


class TestProvenance:
    def test_provenance_includes_source_step(self):
        mocks = _mocks(
            engine={"cost_basis": 10.0, "provenance": {"rule": "R1"}},
        )
        result = run_cascade(_ctx(), **mocks)

        assert result.provenance.get("step") == CascadeStep.V11_ENGINE.value
        assert result.provenance.get("rule") == "R1"

    def test_provenance_preserved_for_sheet(self):
        mocks = _mocks(
            engine=None,
            sheet={
                "cost_basis": 5.0,
                "provenance": {"row": 7, "sheet_id": "abc"},
                "sheet_stale": False,
            },
        )
        result = run_cascade(_ctx(), **mocks)

        assert result.provenance["step"] == "summit_sheet"
        assert result.provenance["row"] == 7
        assert result.provenance["sheet_id"] == "abc"


class TestConfidenceMapping:
    def test_confidence_mapping(self):
        # engine -> GREEN
        r = run_cascade(_ctx(), **_mocks(engine={"cost_basis": 1.0}))
        assert r.confidence == ConfidenceFlag.GREEN

        # sheet -> GREEN
        r = run_cascade(_ctx(), **_mocks(sheet={"cost_basis": 1.0}))
        assert r.confidence == ConfidenceFlag.GREEN

        # history -> YELLOW
        r = run_cascade(
            _ctx(),
            **_mocks(history={"weighted_median": 1.0, "match_count": 5}),
        )
        assert r.confidence == ConfidenceFlag.YELLOW

        # solicit -> YELLOW
        r = run_cascade(_ctx(), **_mocks(solicit={"cost_basis": 1.0}))
        assert r.confidence == ConfidenceFlag.YELLOW

        # manual -> YELLOW
        r = run_cascade(_ctx(), **_mocks(manual={"cost_basis": 1.0}))
        assert r.confidence == ConfidenceFlag.YELLOW

        # total miss -> RED
        r = run_cascade(_ctx(), **_mocks())
        assert r.confidence == ConfidenceFlag.RED
