"""Integration test for SWA-003 — simulates a full Telegram callback round-trip
for each of the four SDA flows using mock Telegram objects. No real bot.

Marked ``integration`` so it only runs with ``-m integration``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from tools.telegram_sda_flows import CallbackResult, dispatch_sda_callback


def _fake_query(callback_data):
    q = MagicMock()
    q.data = callback_data
    q.answer = AsyncMock()
    q.message = MagicMock()
    q.message.reply_text = AsyncMock()
    return q


@pytest.mark.integration
class TestSdaCallbackRoundTrip:
    def test_noprice_round_trip(self):
        resolved = []
        deps = {"mark_noprice_resolved": lambda r, p, a: resolved.append((r, p, a))}
        result = dispatch_sda_callback("sda_noprice_skip:RFQ1:PN-A", deps=deps)
        assert result.success
        assert resolved == [("RFQ1", "PN-A", "noprice_skip")]

    def test_solicit_round_trip(self):
        sent = []
        deps = {
            "get_pending_solicit": lambda sid: {
                "solicit_id": sid,
                "proposed_recipient": "v@normal.com",
                "pn": "PN-A",
            },
            "send_solicit_email": lambda p: sent.append(p),
            "enqueue_manual_price": Mock(),
            "check_override": lambda e: (False, None),
        }
        result = dispatch_sda_callback("sda_solicit_send:S1", deps=deps)
        assert result.success
        assert len(sent) == 1

    def test_summit_guard_round_trip(self):
        deps = {
            "get_pending_solicit": lambda sid: {
                "solicit_id": sid,
                "proposed_recipient": "x@summitmro.com",
                "pn": "PN-A",
            },
            "send_solicit_email": Mock(),
            "enqueue_manual_price": Mock(),
        }
        result = dispatch_sda_callback("sda_solicit_edit:S1", deps=deps)
        assert result.requires_confirmation is True
        assert "Summit" in (result.warning or "")

    def test_append_round_trip(self):
        appended = []
        deps = {
            "v11_append_line": lambda q, r: appended.append((q, r)) or {"ok": True},
            "v11_create_new_quote": Mock(),
            "mark_append_rejected": Mock(),
        }
        result = dispatch_sda_callback("sda_append_append:RFQ1:Q42", deps=deps)
        assert result.success
        assert appended == [("Q42", "RFQ1")]

    def test_idg_round_trip(self):
        acked = []
        deps = {"mark_idg_acknowledged": lambda r, p: acked.append((r, p))}
        result = dispatch_sda_callback("sda_idg_dismiss:RFQ1:PN-IDG", deps=deps)
        assert result.success
        assert acked == [("RFQ1", "PN-IDG")]
