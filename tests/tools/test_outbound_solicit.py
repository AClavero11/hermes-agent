"""Tests for the outbound solicit draft builder (SDA-004).

Hard invariant: Summit contacts (@summitmro.com) are excluded from the
default vendor selection path. Manual override flow requires explicit
confirmation.
"""

import pytest

from tools.outbound_solicit_tool import (
    HistoricalVendor,
    SolicitDraft,
    SolicitOutcome,
    SolicitRequest,
    build_solicit_draft,
    check_manual_recipient_override,
    select_non_summit_vendor,
    verify_price_free,
)


class TestVendorSelection:
    def test_skips_summit_vendor(self):
        candidates = [
            HistoricalVendor(
                email="jorge@summitmro.com", last_contact_date="2026-04-01"
            ),
            HistoricalVendor(
                email="other@x.com", last_contact_date="2026-03-15"
            ),
        ]
        result = select_non_summit_vendor(candidates)
        assert result is not None
        assert result.email == "other@x.com"

    def test_all_summit_returns_none(self):
        candidates = [
            HistoricalVendor(
                email="jorge@summitmro.com", last_contact_date="2026-04-01"
            ),
            HistoricalVendor(
                email="kent@summitmro.com", last_contact_date="2026-03-15"
            ),
        ]
        assert select_non_summit_vendor(candidates) is None

    def test_case_insensitive_summit_match(self):
        candidates = [
            HistoricalVendor(
                email="JORGE@SUMMITMRO.COM", last_contact_date="2026-04-01"
            ),
            HistoricalVendor(
                email="other@x.com", last_contact_date="2026-03-15"
            ),
        ]
        result = select_non_summit_vendor(candidates)
        assert result is not None
        assert result.email == "other@x.com"

    def test_empty_list_returns_none(self):
        assert select_non_summit_vendor([]) is None

    def test_first_non_summit_wins(self):
        candidates = [
            HistoricalVendor(
                email="first@x.com", last_contact_date="2026-04-01"
            ),
            HistoricalVendor(
                email="second@y.com", last_contact_date="2026-03-15"
            ),
        ]
        result = select_non_summit_vendor(candidates)
        assert result is not None
        assert result.email == "first@x.com"


class TestDraftBuilder:
    def _request(self, pn_list=None):
        return SolicitRequest(
            pn_list=pn_list or ["273T1102-8", "3605812-17"],
            customer_name="ACME Airlines",
            aircraft="B737",
        )

    def _vendor(self, email="vendor@trusted.com"):
        return HistoricalVendor(email=email, last_contact_date="2026-03-01")

    def test_builds_valid_draft_for_non_summit(self):
        draft = build_solicit_draft(self._request(), self._vendor())
        assert isinstance(draft, SolicitDraft)
        assert draft.outcome == SolicitOutcome.DRAFTED
        assert draft.recipient == "vendor@trusted.com"

    def test_rejects_summit_recipient(self):
        summit_vendor = self._vendor(email="jorge@summitmro.com")
        with pytest.raises(ValueError):
            build_solicit_draft(self._request(), summit_vendor)

    def test_rejects_mixed_case_summit_recipient(self):
        summit_vendor = self._vendor(email="JORGE@SUMMITMRO.COM")
        with pytest.raises(ValueError):
            build_solicit_draft(self._request(), summit_vendor)

    def test_subject_format(self):
        draft = build_solicit_draft(self._request(), self._vendor())
        assert draft.subject.startswith("Need pricing on")

    def test_subject_truncates_long_pn_list(self):
        many_pns = [f"PN-{i:03d}" for i in range(20)]
        draft = build_solicit_draft(
            SolicitRequest(pn_list=many_pns), self._vendor()
        )
        assert len(draft.subject) <= 100
        assert draft.subject.startswith("Need pricing on")

    def test_body_contains_all_pns(self):
        pn_list = ["ABC-1", "XYZ-99", "DEF-42"]
        draft = build_solicit_draft(
            SolicitRequest(pn_list=pn_list), self._vendor()
        )
        for pn in pn_list:
            assert pn in draft.body

    def test_body_no_dollar_sign(self):
        draft = build_solicit_draft(self._request(), self._vendor())
        assert "$" not in draft.body

    def test_body_no_cost_or_price_word(self):
        draft = build_solicit_draft(self._request(), self._vendor())
        is_clean, offenses = verify_price_free(draft.body)
        assert is_clean, f"body had price leakage: {offenses}"

    def test_body_includes_signature(self):
        draft = build_solicit_draft(self._request(), self._vendor())
        assert "Anthony Clavero" in draft.body
        assert "President" in draft.body

    def test_empty_pn_list_raises(self):
        with pytest.raises(ValueError):
            build_solicit_draft(
                SolicitRequest(pn_list=[]), self._vendor()
            )


class TestPriceFreeCheck:
    def test_clean_body(self):
        is_clean, offenses = verify_price_free("Need pricing on these parts")
        assert is_clean is True
        assert offenses == []

    def test_dollar_sign_flagged(self):
        is_clean, offenses = verify_price_free("it cost $500")
        assert is_clean is False
        assert any("dollar" in o for o in offenses)

    def test_cost_statement_flagged(self):
        is_clean, offenses = verify_price_free("The cost is high")
        assert is_clean is False
        assert len(offenses) >= 1

    def test_price_statement_flagged(self):
        is_clean, offenses = verify_price_free("The price is $500")
        assert is_clean is False

    def test_cost_question_allowed(self):
        is_clean, offenses = verify_price_free("What's the cost?")
        assert is_clean is True
        assert offenses == []

    def test_how_much_allowed(self):
        is_clean, offenses = verify_price_free("how much does it cost")
        assert is_clean is True
        assert offenses == []

    def test_what_prefix_allowed(self):
        is_clean, offenses = verify_price_free("what cost do you see")
        assert is_clean is True


class TestManualOverride:
    def test_non_summit_no_confirmation(self):
        requires, warning = check_manual_recipient_override("jane@x.com")
        assert requires is False
        assert warning is None

    def test_summit_requires_confirmation(self):
        requires, warning = check_manual_recipient_override(
            "jorge.fernandez@summitmro.com"
        )
        assert requires is True
        assert warning is not None
        assert len(warning) > 0

    def test_kent_summit_requires_confirmation(self):
        requires, warning = check_manual_recipient_override(
            "kent.kendrick@summitmro.com"
        )
        assert requires is True
        assert warning is not None
        assert len(warning) > 0

    def test_mixed_case_summit_caught(self):
        requires, warning = check_manual_recipient_override(
            "JORGE@SUMMITMRO.COM"
        )
        assert requires is True
        assert warning is not None
