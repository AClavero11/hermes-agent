from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource


def _telegram_event(text: str, message_type: MessageType = MessageType.TEXT) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=message_type,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="496461229",
            chat_type="dm",
            user_id="496461229",
        ),
        message_id="123",
    )


def test_telegram_operator_prompt_gets_fast_receipt(monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.delenv("HERMES_TELEGRAM_FAST_RECEIPT", raising=False)

    event = _telegram_event("i need help building out my hermes auto quoting can we work on that")

    assert gateway_run._should_send_telegram_turn_receipt(event) is True


def test_telegram_ack_probe_does_not_get_fast_receipt(monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.delenv("HERMES_TELEGRAM_FAST_RECEIPT", raising=False)

    event = _telegram_event("ack")

    assert gateway_run._should_send_telegram_turn_receipt(event) is False


def test_telegram_testing_probe_does_not_get_fast_receipt(monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.delenv("HERMES_TELEGRAM_FAST_RECEIPT", raising=False)

    event = _telegram_event("testing")

    assert gateway_run._should_send_telegram_turn_receipt(event) is False


@pytest.mark.asyncio
async def test_busy_testing_probe_returns_direct_answer_without_interrupt():
    from gateway.run import GatewayRunner

    adapter = SimpleNamespace(_send_with_retry=AsyncMock())
    runner = object.__new__(GatewayRunner)
    runner._draining = False
    runner.adapters = {Platform.TELEGRAM: adapter}

    event = _telegram_event("testing")

    handled = await runner._handle_active_session_busy_message(event, "telegram:496461229")

    assert handled is True
    adapter._send_with_retry.assert_awaited_once()
    assert adapter._send_with_retry.await_args.kwargs["content"].startswith("Hermes online.")


def test_telegram_notify_interval_defaults_to_early_first_update(monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.delenv("HERMES_AGENT_NOTIFY_INTERVAL", raising=False)
    monkeypatch.delenv("HERMES_TELEGRAM_NOTIFY_INTERVAL", raising=False)
    monkeypatch.delenv("HERMES_TELEGRAM_NOTIFY_FIRST_INTERVAL", raising=False)

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="496461229",
        chat_type="dm",
        user_id="496461229",
    )

    first, recurring = gateway_run._resolve_agent_notify_interval(source)

    assert first == 20.0
    assert recurring == 60.0
