"""Tests for tools.customer_quote_ref."""

from __future__ import annotations

from tools.customer_quote_ref import extract_all_refs, extract_customer_quote_ref


class TestSinglePatternMatch:
    def test_ref_colon(self) -> None:
        assert extract_customer_quote_ref("Ref: ABC-123") == "ABC-123"

    def test_ref_space(self) -> None:
        assert extract_customer_quote_ref("Ref ABC-123") == "ABC-123"

    def test_ref_no_space_after_colon(self) -> None:
        assert extract_customer_quote_ref("Ref:ABC-123") == "ABC-123"

    def test_reference_colon(self) -> None:
        assert extract_customer_quote_ref("Reference: XYZ-9") == "XYZ-9"

    def test_q_hash_no_space(self) -> None:
        assert extract_customer_quote_ref("Q#4567") == "4567"

    def test_q_hash_space(self) -> None:
        assert extract_customer_quote_ref("Q# 4567") == "4567"

    def test_q_colon(self) -> None:
        assert extract_customer_quote_ref("Q: 89-AB") == "89-AB"

    def test_your_quote_plain(self) -> None:
        assert extract_customer_quote_ref("Your quote ABC-001") == "ABC-001"

    def test_your_quote_hash(self) -> None:
        assert extract_customer_quote_ref("Your quote #DEF-002") == "DEF-002"

    def test_quote_number(self) -> None:
        assert extract_customer_quote_ref("Quote number GHI-003") == "GHI-003"

    def test_quote_no(self) -> None:
        assert extract_customer_quote_ref("Quote no. JKL-4") == "JKL-4"

    def test_po_hash(self) -> None:
        assert extract_customer_quote_ref("PO#MNO-5") == "MNO-5"

    def test_po_colon(self) -> None:
        assert extract_customer_quote_ref("PO: PQR-6") == "PQR-6"

    def test_case_insensitive_ref(self) -> None:
        assert extract_customer_quote_ref("ref: ABC-123") == "ABC-123"

    def test_case_insensitive_q_hash(self) -> None:
        assert extract_customer_quote_ref("q# 4567") == "4567"


class TestNoMatch:
    def test_plain_text_no_ref(self) -> None:
        assert extract_customer_quote_ref(
            "Hello, we need a price on some parts please."
        ) is None

    def test_empty_string(self) -> None:
        assert extract_customer_quote_ref("") is None

    def test_none_body(self) -> None:
        assert extract_customer_quote_ref(None) is None

    def test_whitespace_only(self) -> None:
        assert extract_customer_quote_ref("   \n\t  ") is None


class TestFalsePositives:
    def test_skips_tbd_token(self) -> None:
        assert extract_customer_quote_ref("Ref: TBD") is None

    def test_skips_na_token(self) -> None:
        # "N/A" contains a slash which is not in the token char class,
        # so the token match will only capture "N" (length 1, rejected)
        # or the pattern will fail altogether.
        assert extract_customer_quote_ref("Q#: N/A") is None

    def test_skips_unknown(self) -> None:
        assert extract_customer_quote_ref("Ref: UNKNOWN") is None


class TestRealRFQBodies:
    def test_rfq_with_ref_in_middle(self) -> None:
        body = (
            "Hello team,\n"
            "\n"
            "We are sourcing the following components for our next build.\n"
            "Ref: PROD-2026-0847\n"
            "\n"
            "Please quote availability and pricing at your earliest convenience.\n"
            "Thanks,\n"
            "Procurement\n"
        )
        assert extract_customer_quote_ref(body) == "PROD-2026-0847"

    def test_rfq_with_trailing_punct(self) -> None:
        assert extract_customer_quote_ref("Ref: ABC-123.") == "ABC-123"

    def test_rfq_with_parens(self) -> None:
        assert extract_customer_quote_ref("quote (Ref: ABC-123)") == "ABC-123"


class TestExtractAll:
    def test_single_match(self) -> None:
        assert extract_all_refs("Ref: ABC-123") == ["ABC-123"]

    def test_multiple_refs(self) -> None:
        body = "Please see Ref: AAA and also Q# BBB for context."
        assert extract_all_refs(body) == ["AAA", "BBB"]

    def test_dedup_exact_duplicates(self) -> None:
        body = "First Ref: SAME then later Ref: SAME again."
        assert extract_all_refs(body) == ["SAME"]

    def test_order_preserved(self) -> None:
        body = "Start with Q# FIRST, then PO: SECOND, finally Ref: THIRD."
        assert extract_all_refs(body) == ["FIRST", "SECOND", "THIRD"]
