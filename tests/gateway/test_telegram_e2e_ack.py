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
                "repo_sha": "abc123",
                "runtime_sha": "abc123",
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
    assert evidence["repo_sha"] == "abc123"
    assert evidence["runtime_sha"] == "abc123"
    assert evidence["created_at"] > 0


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


def test_operator_response_evidence_preserves_sha_binding(tmp_path, monkeypatch):
    pending_path = tmp_path / "telegram_operator_response_pending.json"
    evidence_path = tmp_path / "telegram_operator_response_last.json"
    pending_path.write_text(
        json.dumps(
            {
                "status": "pending",
                "mode": "signed_operator_response_probe",
                "chat_id": "496461229",
                "sent_at": time.time() - 1,
                "latency_budget_ms": 10000,
                "nonce": "op-test",
                "prompt": "rfq",
                "expected_substrings": ["RFQ mode", "No customer sends"],
                "inbound_message_id": 101,
                "repo_sha": "def456",
                "runtime_sha": "def456",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_TELEGRAM_OPERATOR_PENDING_PATH", str(pending_path))
    monkeypatch.setenv("HERMES_TELEGRAM_OPERATOR_EVIDENCE_PATH", str(evidence_path))

    _adapter()._maybe_record_telegram_operator_response(
        chat_id="496461229",
        content="RFQ mode.\nGuardrail: No customer sends.",
        reply_to="101",
        message_id="202",
        success=True,
    )

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["status"] == "pass"
    assert evidence["prompt"] == "rfq"
    assert evidence["repo_sha"] == "def456"
    assert evidence["runtime_sha"] == "def456"
    assert evidence["created_at"] > 0


def test_operator_response_evidence_fails_for_forbidden_planner_scaffold(tmp_path, monkeypatch):
    pending_path = tmp_path / "telegram_operator_response_pending.json"
    evidence_path = tmp_path / "telegram_operator_response_last.json"
    pending_path.write_text(
        json.dumps(
            {
                "status": "pending",
                "mode": "signed_operator_response_probe",
                "chat_id": "496461229",
                "sent_at": time.time() - 1,
                "latency_budget_ms": 10000,
                "nonce": "op-qty",
                "prompt": "qty 1",
                "expected_substrings": ["Received: qty 1"],
                "forbidden_substrings": ["Prioritized tasks", "Execution blocked"],
                "inbound_message_id": 101,
                "repo_sha": "def456",
                "runtime_sha": "def456",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_TELEGRAM_OPERATOR_PENDING_PATH", str(pending_path))
    monkeypatch.setenv("HERMES_TELEGRAM_OPERATOR_EVIDENCE_PATH", str(evidence_path))

    _adapter()._maybe_record_telegram_operator_response(
        chat_id="496461229",
        content="Received: qty 1.\n\nPrioritized tasks:\n1. Clarify.",
        reply_to="101",
        message_id="202",
        success=True,
    )

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["status"] == "fail"
    assert evidence["content_match"] is False
    assert evidence["forbidden_hits"] == ["Prioritized tasks"]
