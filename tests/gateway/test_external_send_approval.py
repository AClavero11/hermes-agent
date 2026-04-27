"""Tests for durable external-send approval gates."""

import asyncio
import hashlib
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform
from hermes_state import SessionDB
from tools.send_message_tool import send_message_tool


def _run_async_immediately(coro):
    return asyncio.run(coro)


def _make_config():
    telegram_cfg = SimpleNamespace(enabled=True, token="***", extra={})
    return SimpleNamespace(
        platforms={Platform.TELEGRAM: telegram_cfg},
        get_home_channel=lambda _platform: None,
    ), telegram_cfg


@pytest.fixture
def approval_db(tmp_path, monkeypatch):
    from tools import approval

    db = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(approval, "_get_session_db", lambda: db)
    monkeypatch.delenv("HERMES_EXTERNAL_APPROVAL_DISABLED", raising=False)
    monkeypatch.delenv("HERMES_EXTERNAL_APPROVAL_AUTO", raising=False)
    monkeypatch.delenv("HERMES_EXTERNAL_APPROVAL_TIMEOUT", raising=False)
    with approval._lock:
        approval._external_action_events.clear()
        approval._external_action_entries.clear()
        approval._gateway_queues.clear()
        approval._gateway_notify_cbs.clear()
    yield db
    db.close()


def _send_args(message="hello"):
    return {"action": "send", "target": "telegram:-1001", "message": message}


def _expected_payload(message="hello"):
    return {
        "raw_target": "telegram:-1001",
        "channel": "telegram",
        "target": "-1001",
        "target_ref": "-1001",
        "thread_id": None,
        "message": message,
        "media_files": [],
    }


def _wait_for_pending(db, count=1, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = db.list_pending_approvals(action_type="external_send")
        if len(rows) >= count:
            return rows
        time.sleep(0.02)
    raise AssertionError("timed out waiting for pending approval")


def _start_send_thread(holder, args=None):
    def _target():
        holder["result"] = json.loads(send_message_tool(args or _send_args()))

    thread = threading.Thread(target=_target)
    thread.start()
    return thread


def _join_thread(thread):
    thread.join(timeout=5)
    assert not thread.is_alive()


def _send_patches(send_mock):
    return patch.multiple(
        "tools.send_message_tool",
        _send_to_platform=send_mock,
    )


def test_external_send_creates_pending_approval_with_payload_hash(approval_db):
    from tools.approval import decide_external_action

    config, _telegram_cfg = _make_config()
    send_mock = AsyncMock(return_value={"success": True})
    holder = {}

    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("tools.interrupt.is_interrupted", return_value=False), \
         patch("model_tools._run_async", side_effect=_run_async_immediately), \
         _send_patches(send_mock), \
         patch("gateway.mirror.mirror_to_session", return_value=True):
        thread = _start_send_thread(holder)
        pending = _wait_for_pending(approval_db)[0]
        assert pending["status"] == "pending"
        assert pending["payload_hash"] == SessionDB._approval_payload_hash(
            _expected_payload()
        )
        assert pending["payload_preview"]
        send_mock.assert_not_awaited()

        decide_external_action(pending["id"], "denied", "tester")
        _join_thread(thread)

    assert holder["result"]["reason"] == "denied_by_approver"
    send_mock.assert_not_awaited()


def test_send_blocks_until_approval_decided(approval_db):
    from tools.approval import decide_external_action

    config, _telegram_cfg = _make_config()
    send_mock = AsyncMock(return_value={"success": True})
    holder = {}

    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("tools.interrupt.is_interrupted", return_value=False), \
         patch("model_tools._run_async", side_effect=_run_async_immediately), \
         _send_patches(send_mock), \
         patch("gateway.mirror.mirror_to_session", return_value=True):
        thread = _start_send_thread(holder)
        pending = _wait_for_pending(approval_db)[0]
        time.sleep(0.1)
        assert thread.is_alive()
        send_mock.assert_not_awaited()

        decide_external_action(pending["id"], "approved", "tester")
        _join_thread(thread)

    assert holder["result"]["success"] is True


def test_send_proceeds_on_approval(approval_db):
    from tools.approval import decide_external_action

    config, telegram_cfg = _make_config()
    send_mock = AsyncMock(return_value={"success": True})
    holder = {}

    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("tools.interrupt.is_interrupted", return_value=False), \
         patch("model_tools._run_async", side_effect=_run_async_immediately), \
         _send_patches(send_mock), \
         patch("gateway.mirror.mirror_to_session", return_value=True):
        thread = _start_send_thread(holder)
        pending = _wait_for_pending(approval_db)[0]
        decide_external_action(pending["id"], "approved", "tester")
        _join_thread(thread)

    assert holder["result"]["success"] is True
    send_mock.assert_awaited_once_with(
        Platform.TELEGRAM,
        telegram_cfg,
        "-1001",
        "hello",
        thread_id=None,
        media_files=[],
    )


def test_send_aborts_on_denial(approval_db):
    from tools.approval import decide_external_action

    config, _telegram_cfg = _make_config()
    send_mock = AsyncMock(return_value={"success": True})
    holder = {}

    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("tools.interrupt.is_interrupted", return_value=False), \
         patch("model_tools._run_async", side_effect=_run_async_immediately), \
         _send_patches(send_mock), \
         patch("gateway.mirror.mirror_to_session", return_value=True):
        thread = _start_send_thread(holder)
        pending = _wait_for_pending(approval_db)[0]
        decide_external_action(pending["id"], "denied", "tester")
        _join_thread(thread)

    assert holder["result"]["ok"] is False
    assert holder["result"]["reason"] == "denied_by_approver"
    send_mock.assert_not_awaited()


def test_payload_hash_is_deterministic_canonical_json(approval_db):
    payload_a = {"message": "hello", "nested": {"b": 2, "a": 1}}
    payload_b = {"nested": {"a": 1, "b": 2}, "message": "hello"}

    row_a = approval_db.create_approval_request(
        "external_send", "telegram", "-1001", payload_a
    )
    row_b = approval_db.create_approval_request(
        "external_send", "telegram", "-1001", payload_b
    )
    canonical = json.dumps(
        payload_a,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    assert row_a["payload_hash"] == expected_hash
    assert row_b["payload_hash"] == expected_hash


def test_org_event_audit_row_written_on_approval_and_denial(approval_db):
    approved = approval_db.create_approval_request(
        "external_send", "telegram", "-1001", {"message": "approved"}
    )
    denied = approval_db.create_approval_request(
        "external_send", "telegram", "-1002", {"message": "denied"}
    )

    approved_row = approval_db.decide_approval_request(
        approved["id"], "approved", "tester", "looks ok"
    )
    denied_row = approval_db.decide_approval_request(
        denied["id"], "denied", "tester", "wrong target"
    )

    approved_events = approval_db.list_org_events(task_id=approved_row["org_task_id"])
    denied_events = approval_db.list_org_events(task_id=denied_row["org_task_id"])
    approved_audit = [e for e in approved_events if e["event_type"] == "approval.approved"]
    denied_audit = [e for e in denied_events if e["event_type"] == "approval.denied"]

    assert len(approved_audit) == 1
    assert len(denied_audit) == 1
    approved_payload = json.loads(approved_audit[0]["payload_json"])
    denied_payload = json.loads(denied_audit[0]["payload_json"])
    assert approved_payload["approval_id"] == approved["id"]
    assert approved_payload["payload_hash"] == approved["payload_hash"]
    assert denied_payload["approval_id"] == denied["id"]
    assert denied_payload["reason"] == "wrong target"


def test_internal_destinations_bypass_approval(approval_db):
    from tools.send_message_tool import _requires_external_approval

    config, _telegram_cfg = _make_config()
    assert _requires_external_approval("cli") is False
    assert _requires_external_approval("log") is False

    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("tools.interrupt.is_interrupted", return_value=False):
        result = json.loads(
            send_message_tool({"action": "send", "target": "cli", "message": "hello"})
        )

    assert "error" in result
    assert approval_db.list_pending_approvals() == []


def test_approval_disabled_env_var_bypasses_gate(approval_db, monkeypatch):
    monkeypatch.setenv("HERMES_EXTERNAL_APPROVAL_DISABLED", "1")
    config, telegram_cfg = _make_config()
    send_mock = AsyncMock(return_value={"success": True})

    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("tools.interrupt.is_interrupted", return_value=False), \
         patch("model_tools._run_async", side_effect=_run_async_immediately), \
         _send_patches(send_mock), \
         patch("gateway.mirror.mirror_to_session", return_value=True):
        result = json.loads(send_message_tool(_send_args()))

    assert result["success"] is True
    assert approval_db.list_pending_approvals() == []
    send_mock.assert_awaited_once_with(
        Platform.TELEGRAM,
        telegram_cfg,
        "-1001",
        "hello",
        thread_id=None,
        media_files=[],
    )


def test_approval_auto_env_var_auto_approves(approval_db, monkeypatch):
    monkeypatch.setenv("HERMES_EXTERNAL_APPROVAL_AUTO", "1")
    config, _telegram_cfg = _make_config()
    send_mock = AsyncMock(return_value={"success": True})

    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("tools.interrupt.is_interrupted", return_value=False), \
         patch("model_tools._run_async", side_effect=_run_async_immediately), \
         _send_patches(send_mock), \
         patch("gateway.mirror.mirror_to_session", return_value=True):
        result = json.loads(send_message_tool(_send_args()))

    assert result["success"] is True
    approval = approval_db.get_approval_request(result["approval_id"])
    assert approval["status"] == "approved"
    assert approval_db.list_pending_approvals() == []
    send_mock.assert_awaited_once()


def test_decide_approval_state_machine_rejects_double_decision(approval_db):
    row = approval_db.create_approval_request(
        "external_send", "telegram", "-1001", {"message": "hello"}
    )

    approval_db.decide_approval_request(row["id"], "approved", "tester")
    with pytest.raises(ValueError):
        approval_db.decide_approval_request(row["id"], "denied", "tester")


def test_expired_approval_does_not_send(approval_db, monkeypatch):
    monkeypatch.setenv("HERMES_EXTERNAL_APPROVAL_TIMEOUT", "0.05")
    config, _telegram_cfg = _make_config()
    send_mock = AsyncMock(return_value={"success": True})

    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("tools.interrupt.is_interrupted", return_value=False), \
         patch("model_tools._run_async", side_effect=_run_async_immediately), \
         _send_patches(send_mock), \
         patch("gateway.mirror.mirror_to_session", return_value=True):
        result = json.loads(send_message_tool(_send_args()))

    assert result["ok"] is False
    assert result["status"] == "expired"
    assert result["reason"] == "approval_expired"
    send_mock.assert_not_awaited()
