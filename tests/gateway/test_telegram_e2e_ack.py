from __future__ import annotations

import json
import time
from types import SimpleNamespace

from gateway.config import Platform
from gateway.platforms.telegram import TelegramAdapter


def _adapter() -> TelegramAdapter:
    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    return adapter


def _write_pending(tmp_path, *, mode: str = "real_visible_delivery_probe") -> tuple:
    pending_path = tmp_path / "telegram_e2e_pending.json"
    evidence_path = tmp_path / "telegram_e2e_last.json"
    pending_path.write_text(
        json.dumps(
            {
                "status": "sent",
                "mode": mode,
                "chat_id": "496461229",
                "sent_at": time.time() - 1,
                "awaiting_reply": "ack real-test",
                "latency_budget_ms": 60000,
                "nonce": "real-test",
                "message_id": 1649,
            }
        ),
        encoding="utf-8",
    )
    return pending_path, evidence_path


def test_visible_e2e_accepts_plain_ack_from_same_chat(tmp_path, monkeypatch):
    pending_path, evidence_path = _write_pending(tmp_path)
    monkeypatch.setenv("HERMES_TELEGRAM_E2E_PENDING_PATH", str(pending_path))
    monkeypatch.setenv("HERMES_TELEGRAM_E2E_EVIDENCE_PATH", str(evidence_path))

    message = SimpleNamespace(text="ack", chat_id="496461229", message_id=1700)

    assert _adapter()._maybe_record_telegram_e2e_ack(message, update_id=42) is True

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["status"] == "pass"
    assert evidence["mode"] == "real_visible_delivery_probe"
    assert evidence["ack_match"] == "loose"
    assert evidence["nonce"] == "real-test"


def test_signed_webhook_simulation_requires_exact_ack(tmp_path, monkeypatch):
    pending_path, evidence_path = _write_pending(
        tmp_path,
        mode="signed_webhook_simulation",
    )
    monkeypatch.setenv("HERMES_TELEGRAM_E2E_PENDING_PATH", str(pending_path))
    monkeypatch.setenv("HERMES_TELEGRAM_E2E_EVIDENCE_PATH", str(evidence_path))

    message = SimpleNamespace(text="ack", chat_id="496461229", message_id=1700)

    assert _adapter()._maybe_record_telegram_e2e_ack(message, update_id=42) is False
    assert not evidence_path.exists()
