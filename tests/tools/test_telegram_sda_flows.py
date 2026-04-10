"""Unit tests for tools.telegram_sda_flows (SWA-003)."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from tools.telegram_sda_flows import (
    CallbackResult,
    dispatch_sda_callback,
    handle_noprice_manual_reply,
    handle_solicit_callback,
    handle_append_callback,
    handle_idg_warning_dismissed,
)


class TestNoPriceHandler:
    def test_happy_path_skip(self):
        mock_resolve = Mock()
        r = handle_noprice_manual_reply(
            "sda_noprice_skip:RFQ1:PN-A",
            mark_noprice_resolved=mock_resolve,
        )
        assert r.success and r.action == "noprice_skip"
        mock_resolve.assert_called_once_with("RFQ1", "PN-A", "noprice_skip")

    def test_malformed_callback(self):
        r = handle_noprice_manual_reply(
            "sda_noprice_skip",
            mark_noprice_resolved=Mock(),
        )
        assert r.success is False
        assert "expected rfq_id" in r.error

    def test_downstream_failure_returns_error_result(self):
        mock_resolve = Mock(side_effect=RuntimeError("db down"))
        r = handle_noprice_manual_reply(
            "sda_noprice_cancel:RFQ1:PN-A",
            mark_noprice_resolved=mock_resolve,
        )
        assert r.success is False and "db down" in r.error


class TestSolicitHandler:
    def _pending(self, recipient="vendor@example.com", pn="PN-A"):
        return {"solicit_id": "S1", "proposed_recipient": recipient, "pn": pn}

    def test_happy_path_send(self):
        mock_send = Mock()
        r = handle_solicit_callback(
            "sda_solicit_send:S1",
            get_pending_solicit=lambda sid: self._pending(),
            send_solicit_email=mock_send,
            enqueue_manual_price=Mock(),
            check_override=lambda e: (False, None),
        )
        assert r.success
        mock_send.assert_called_once()

    def test_skip_is_a_noop_success(self):
        r = handle_solicit_callback(
            "sda_solicit_skip:S1",
            get_pending_solicit=lambda sid: None,
            send_solicit_email=Mock(),
            enqueue_manual_price=Mock(),
            check_override=lambda e: (False, None),
        )
        assert r.success and r.action == "solicit_skip"

    def test_edit_recipient_summit_requires_confirmation(self):
        """AC 11: Summit email triggers check_manual_recipient_override."""
        guard_calls = []

        def fake_check(email):
            guard_calls.append(email)
            return (True, "Summit is excluded from auto-solicits. Send anyway?")

        r = handle_solicit_callback(
            "sda_solicit_edit:S1",
            get_pending_solicit=lambda sid: self._pending(recipient="x@summitmro.com"),
            send_solicit_email=Mock(),
            enqueue_manual_price=Mock(),
            check_override=fake_check,
        )
        assert r.requires_confirmation is True
        assert "Summit" in r.warning
        assert guard_calls == ["x@summitmro.com"]

    def test_edit_recipient_non_summit_passes_through(self):
        r = handle_solicit_callback(
            "sda_solicit_edit:S1",
            get_pending_solicit=lambda sid: self._pending(recipient="bob@normal.com"),
            send_solicit_email=Mock(),
            enqueue_manual_price=Mock(),
            check_override=lambda e: (False, None),
        )
        assert r.success and r.requires_confirmation is False

    def test_manual_price_happy_path(self):
        mock_enqueue = Mock()
        r = handle_solicit_callback(
            "sda_solicit_manual_price:S1",
            get_pending_solicit=lambda sid: self._pending(),
            send_solicit_email=Mock(),
            enqueue_manual_price=mock_enqueue,
            check_override=lambda e: (False, None),
        )
        assert r.success
        mock_enqueue.assert_called_once_with("S1", "PN-A")

    def test_pending_solicit_missing_returns_error(self):
        r = handle_solicit_callback(
            "sda_solicit_send:MISSING",
            get_pending_solicit=lambda sid: None,
            send_solicit_email=Mock(),
            enqueue_manual_price=Mock(),
            check_override=lambda e: (False, None),
        )
        assert r.success is False and "no pending solicit" in r.error


class TestAppendHandler:
    def test_append_to_existing_quote(self):
        mock_append = Mock(return_value={"ok": True})
        r = handle_append_callback(
            "sda_append_append:RFQ1:Q42",
            v11_append_line=mock_append,
            v11_create_new_quote=Mock(),
            mark_append_rejected=Mock(),
        )
        assert r.success
        mock_append.assert_called_once_with("Q42", "RFQ1")

    def test_new_quote_path(self):
        mock_new = Mock(return_value={"id": 99})
        r = handle_append_callback(
            "sda_append_new:RFQ1",
            v11_append_line=Mock(),
            v11_create_new_quote=mock_new,
            mark_append_rejected=Mock(),
        )
        assert r.success
        mock_new.assert_called_once_with("RFQ1")

    def test_reject_marks_rejection(self):
        mock_reject = Mock()
        r = handle_append_callback(
            "sda_append_reject:RFQ1",
            v11_append_line=Mock(),
            v11_create_new_quote=Mock(),
            mark_append_rejected=mock_reject,
        )
        assert r.success
        mock_reject.assert_called_once_with("RFQ1")

    def test_append_missing_quote_id(self):
        r = handle_append_callback(
            "sda_append_append:RFQ1",
            v11_append_line=Mock(),
            v11_create_new_quote=Mock(),
            mark_append_rejected=Mock(),
        )
        assert r.success is False and "quote_id" in r.error


class TestIdgHandler:
    def test_dismiss_happy_path(self):
        mock_ack = Mock()
        r = handle_idg_warning_dismissed(
            "sda_idg_dismiss:RFQ1:PN-IDG",
            mark_idg_acknowledged=mock_ack,
        )
        assert r.success
        mock_ack.assert_called_once_with("RFQ1", "PN-IDG")

    def test_dismiss_missing_pn(self):
        r = handle_idg_warning_dismissed(
            "sda_idg_dismiss:RFQ1",
            mark_idg_acknowledged=Mock(),
        )
        assert r.success is False

    def test_unknown_idg_action(self):
        r = handle_idg_warning_dismissed(
            "sda_idg_escalate:RFQ1:PN-A",
            mark_idg_acknowledged=Mock(),
        )
        assert r.success is False


class TestDispatcher:
    def test_routes_to_correct_handler(self):
        mock_resolve = Mock()
        r = dispatch_sda_callback(
            "sda_noprice_skip:RFQ1:PN-A",
            deps={"mark_noprice_resolved": mock_resolve},
        )
        assert r.success
        mock_resolve.assert_called_once()

    def test_unknown_prefix_returns_error(self):
        r = dispatch_sda_callback("sda_other_foo:X", deps={})
        assert r.success is False and "unknown" in r.action.lower()

    def test_missing_dependency_surfaces_as_error(self):
        r = dispatch_sda_callback("sda_noprice_skip:RFQ1:PN-A", deps={})
        assert r.success is False
        assert r.action == "missing_dep"
