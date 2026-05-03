"""Tests for gateway /status behavior and token persistence."""

import json
from datetime import datetime
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=_make_source(),
        message_id="m1",
    )


def _make_runner(session_entry: SessionEntry):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    return runner


@pytest.mark.asyncio
async def test_status_command_reports_running_agent_without_interrupt(monkeypatch):
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)
    running_agent = MagicMock()
    runner._running_agents[build_session_key(_make_source())] = running_agent

    result = await runner._handle_message(_make_event("/status"))

    assert "**Session ID:** `sess-1`" in result
    assert "**Tokens:** 321" in result
    assert "**Agent Running:** Yes ⚡" in result
    assert "**Title:**" not in result
    running_agent.interrupt.assert_not_called()
    assert runner._pending_messages == {}


@pytest.mark.asyncio
async def test_status_command_includes_session_title_when_present():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=321,
    )
    runner = _make_runner(session_entry)
    runner._session_db.get_session_title.return_value = "My titled session"

    result = await runner._handle_message(_make_event("/status"))

    assert "**Session ID:** `sess-1`" in result
    assert "**Title:** My titled session" in result


@pytest.mark.asyncio
async def test_status_command_includes_studio_inference_state(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_LOCAL_BASE_URL", "http://studio.local:8090/v1")
    monkeypatch.setenv("DEEPSEEK_LOCAL_MODEL", "deepseek-coder")
    monkeypatch.setenv("DEEPSEEK_V4_BASE_URL", "http://studio.local:8091/v1")
    monkeypatch.setenv("DEEPSEEK_V4_MODEL", "deepseek-v4")
    monkeypatch.setenv("HERMES_STATUS_INCLUDE_LAUNCHD", "0")
    eval_state_file = tmp_path / "eval_last.json"
    eval_state_file.write_text(json.dumps({
        "ok": True,
        "completed_at": "2026-04-25T18:27:00+00:00",
        "checks": [
            {"name": "models", "ok": True},
            {"name": "exact_string", "ok": True},
            {"name": "json_contract", "ok": True},
            {"name": "code_expression", "ok": True},
        ],
    }))
    monkeypatch.setenv("HERMES_EVAL_STATE_FILE", str(eval_state_file))
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )
    runner = _make_runner(session_entry)

    result = await runner._handle_message(_make_event("/status"))

    assert "**Studio Inference:**" in result
    assert "**Production:** `deepseek-coder` @ `http://studio.local:8090/v1`" in result
    assert "**Experimental V4:** `deepseek-v4` @ `http://studio.local:8091/v1`" in result
    assert "**Last Eval:** PASS `4/4` at `2026-04-25T18:27:00+00:00`" in result


def test_gateway_model_resolution_expands_env_placeholders(monkeypatch):
    from gateway.run import _resolve_gateway_model

    monkeypatch.setenv("HERMES_PLANNER_MODEL", "deepseek-coder")

    result = _resolve_gateway_model({
        "model": {
            "default": "${HERMES_PLANNER_MODEL}",
            "provider": "${HERMES_PLANNER_PROVIDER}",
        },
    })

    assert result == "deepseek-coder"


def test_message_requests_alexandria_context():
    from gateway import context_router

    assert context_router.message_requests_alexandria_context("Look in Alexandria for Hermes status")
    assert context_router.message_requests_alexandria_context("Check stock for 762367B")
    assert not context_router.message_requests_alexandria_context(
        "planner live diagnostic: respond exactly HERMES_V4_64K_OK"
    )
    assert not context_router.message_requests_alexandria_context(
        "https://x.com/steipete/status/2047982647264059734?s=46"
    )
    assert not context_router.message_requests_alexandria_context("hello")


def test_extract_alexandria_rel_paths_from_search_outputs():
    from gateway import context_router

    qmd_output = "qmd://alexandria/advanced/czar/CONTEXT.md:77 #abc123"
    json_output = json.dumps({
        "results": [
            {"rel_path": "advanced/pricing/CONTEXT.md"},
            {"path": "/Users/ac/alexandria/advanced/products/CONTEXT.md"},
        ]
    })

    assert context_router.extract_alexandria_rel_paths(qmd_output) == ["advanced/czar/CONTEXT.md"]
    assert context_router.extract_alexandria_rel_paths(json_output) == [
        "advanced/pricing/CONTEXT.md",
        "advanced/products/CONTEXT.md",
    ]


def test_build_alexandria_context_prompt_includes_guardrails(monkeypatch):
    import gateway.run as gateway_run
    from gateway import context_router

    monkeypatch.setattr(
        context_router,
        "_run_alexandria_context_command",
        lambda args, timeout=20.0: "qmd://alexandria/advanced/czar/CONTEXT.md:1\nStudio target",
    )
    monkeypatch.setattr(
        context_router,
        "_read_alexandria_source_snippets",
        lambda paths: "Source: advanced/czar/CONTEXT.md\nArchitecture target: office Studio powers Hermes.",
    )

    result = gateway_run._build_alexandria_context_prompt(
        "Look in Alexandria for Hermes architecture"
    )

    assert "Never answer with generic chatbot disclaimers" in result
    assert "Architecture target: office Studio powers Hermes." in result
    assert "QMD semantic search" in result


def test_build_alexandria_context_prompt_includes_release_context(monkeypatch):
    import gateway.run as gateway_run
    from gateway import context_router

    monkeypatch.setattr(
        context_router,
        "_run_alexandria_context_command",
        lambda args, timeout=20.0: "",
    )
    monkeypatch.setattr(
        context_router,
        "_read_alexandria_source_snippets",
        lambda paths, max_chars=9000: "Source: advanced/czar/CONTEXT.md\nStudio target",
    )
    monkeypatch.setattr(
        context_router,
        "_read_hermes_release_context_snippet",
        lambda message, max_chars=5000: "Source: RELEASE_v0.11.0.md\nInk TUI and pluggable transports",
    )

    result = gateway_run._build_alexandria_context_prompt(
        "Read the Hermes agent newest GitHub release"
    )

    assert "Retrieved Hermes GitHub release notes" in result
    assert "Ink TUI and pluggable transports" in result
    assert "If asked for Hermes status or a 1-100 score" in result


def test_build_sonnet_era_continuity_prompt_includes_alexandria_bridge(monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.setattr(
        gateway_run,
        "_read_alexandria_source_snippets",
        lambda paths, max_chars=9000: "Source: _system/ACTIVE_CONTEXT.md\nCurrent priority: Hermes.",
    )

    result = gateway_run._build_sonnet_era_continuity_prompt("what should we do now?")

    assert "Sonnet-era Hermes continuity bridge" in result
    assert "not a fresh generic chat" in result
    assert "Current priority: Hermes." in result


def test_build_sonnet_era_continuity_prompt_skips_exact_diagnostics():
    import gateway.run as gateway_run

    result = gateway_run._build_sonnet_era_continuity_prompt(
        "reply exactly HERMES_V4_64K_OK"
    )

    assert result == ""


def test_build_hermes_direct_answer_for_docs_access(monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.setattr(
        gateway_run,
        "_read_alexandria_source_snippets",
        lambda paths, max_chars=4500: "Source: advanced/czar/CONTEXT.md\nStudio target",
    )

    result = gateway_run._build_hermes_direct_answer(
        "Are you able to view Alexandria docs?"
    )

    assert result.startswith("Yes. I pulled Alexandria source context")
    assert "advanced/czar/CONTEXT.md" in result
    assert "browse paths manually" in result


def test_build_hermes_direct_answer_for_release_status(monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.setattr(
        gateway_run,
        "_read_alexandria_source_snippets",
        lambda paths, max_chars=4500: "Source: advanced/czar/CONTEXT.md\nStudio target",
    )
    monkeypatch.setattr(
        gateway_run,
        "_read_hermes_release_context_snippet",
        lambda message, max_chars=3500: "Source: RELEASE_v0.11.0.md\n/steer and orchestrator delegation",
    )

    result = gateway_run._build_hermes_direct_answer(
        "hows your hermes agent status 1-100 if i read the hermes newest github?"
    )

    assert "Hermes agent status: 94/100." in result
    assert "v2026.4.23 / v0.11.0" in result
    assert "Blockers to 100/100" in result
    assert "V4 planner is active" in result


def test_build_hermes_direct_answer_for_test_probe():
    import gateway.run as gateway_run

    result = gateway_run._build_hermes_direct_answer("Test")

    assert result.startswith("Hermes online.")
    assert "custom:office-deepseek-v4" in result
    assert "operator mode" in result


def test_build_hermes_direct_answer_for_testing_probe():
    import gateway.run as gateway_run

    result = gateway_run._build_hermes_direct_answer("testing")

    assert result.startswith("Hermes online.")
    assert "operator mode" in result


def test_build_hermes_direct_answer_for_operator_menu():
    import gateway.run as gateway_run

    result = gateway_run._build_hermes_direct_answer("what can we do")

    assert result.startswith("Immediate AAC moves:")
    assert "`rfq`" in result
    assert "`inventory`" in result
    assert "Reply with one word" in result
    assert "roleplay" not in result.lower()


def test_build_hermes_direct_answer_for_operator_menu_choices():
    import gateway.run as gateway_run

    cases = {
        "rfq": ["RFQ mode", "No customer sends"],
        "inventory": ["Inventory mode", "read-only"],
        "followups": ["Follow-up mode", "drafts only"],
        "hermes": ["Hermes mode", "runtime path"],
    }
    for prompt, expected_terms in cases.items():
        result = gateway_run._build_hermes_direct_answer(prompt)
        for term in expected_terms:
            assert term in result
        assert "sorry" not in result.lower()


def test_classify_malformed_hermes_response_blocks_repeated_quote_refusal():
    import gateway.run as gateway_run

    bad = (
        "I'm sorry, but I cannot provide a quote for this request. "
        "Please contact me if you have any other questions.\n\n"
    ) * 4

    assert (
        gateway_run._classify_malformed_hermes_response(bad)
        == "repeated quote refusal loop"
    )


def test_classify_malformed_hermes_response_blocks_provider_alias_command():
    import gateway.run as gateway_run

    bad = (
        "I'm sorry, I cannot execute commands on your system. "
        "The error you're seeing indicates that \"custom:office-deepseek-v4\" "
        "is not a valid command."
    )

    assert (
        gateway_run._classify_malformed_hermes_response(bad)
        == "provider alias treated as a shell command"
    )


def test_build_hermes_direct_answer_for_ack_probe():
    import gateway.run as gateway_run

    result = gateway_run._build_hermes_direct_answer("ack")

    assert result == "Ack received. No task started."


def test_build_hermes_direct_answer_for_finish_it_probe():
    import gateway.run as gateway_run

    result = gateway_run._build_hermes_direct_answer("Finish it")

    assert result.startswith("Hermes will not run an unbounded")
    assert "2026-04-25-hermes-deepseek-openai-telegram.md" in result
    assert "operator mode" in result


def test_build_hermes_direct_answer_for_quality_score(monkeypatch, tmp_path):
    import gateway.run as gateway_run

    reports_dir = tmp_path / "canary" / "reports"
    reports_dir.mkdir(parents=True)
    (reports_dir / "latest.json").write_text(
        json.dumps(
            {
                "status": "warn",
                "score": 190.0,
                "effective_max_score": 210.0,
                "percent": 90.48,
                "overall_quality": {"score": 8.5},
                "readiness": {
                    "status": "not_frontier_ready",
                    "passed": 6,
                    "total": 10,
                    "open_gates": [
                        {"name": "local_model_reasoning"},
                        {"name": "hermes_reasoning_eval"},
                        {"name": "frontier_wrapper"},
                        {"name": "telegram_e2e"},
                    ],
                },
                "results": [
                    {
                        "name": "live.telegram_e2e",
                        "status": "warn",
                        "summary": "No live Telegram E2E latency/restart evidence",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    result = gateway_run._build_hermes_direct_answer(
        "Score your quality score we measure your performance by"
    )

    assert result.startswith("Latest Hermes metrics: WARN.")
    assert "Frontier readiness: NOT_FRONTIER_READY (6/10 gates passed)." in result
    assert "Open gates: local_model_reasoning, hermes_reasoning_eval, frontier_wrapper, telegram_e2e" in result
    assert "Telegram E2E gate: WARN" in result
    assert "no single quality score claim" in result
    assert str(reports_dir / "latest.json") in result


def test_build_hermes_direct_answer_for_social_link_only():
    import gateway.run as gateway_run

    result = gateway_run._build_hermes_direct_answer(
        "https://x.com/steipete/status/2047982647264059734?s=46"
    )

    assert result == ""


def test_build_x_link_context_prompt_uses_scraper(monkeypatch):
    import tools.x_scraper_tool as x_scraper_tool
    import gateway.run as gateway_run

    monkeypatch.setattr(
        x_scraper_tool,
        "x_scrape_tool",
        lambda urls: json.dumps({
            "content": "Built clawsweeper, which runs 50 codex in parallel.",
            "source": "test",
        }),
    )

    result = gateway_run._build_x_link_context_prompt(
        "https://x.com/steipete/status/2047982647264059734?s=46"
    )

    assert "Fetched X/Twitter context" in result
    assert "Built clawsweeper" in result
    assert "Do not say you cannot access the link" in result


def test_build_hermes_direct_answer_for_self_upgrade_prompt(monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.setattr(
        gateway_run,
        "_read_alexandria_source_snippets",
        lambda paths, max_chars=4500: "Source: advanced/czar/CONTEXT.md\nStudio target",
    )
    monkeypatch.setattr(
        gateway_run,
        "_read_hermes_release_context_snippet",
        lambda message, max_chars=3500: "Source: RELEASE_v0.11.0.md\n/steer and orchestrator delegation",
    )

    result = gateway_run._build_hermes_direct_answer("Bring yourself to 100/100")

    assert result.startswith("Hermes self-heal status: 94/100.")
    assert "Prevented this prompt class from reaching terminal/tool execution." in result
    assert "V4 planner active" in result
    assert "Current operating mode: local-first Studio Hermes" in result


def test_build_hermes_direct_answer_for_self_heal_prompt(monkeypatch):
    import gateway.run as gateway_run

    monkeypatch.setattr(
        gateway_run,
        "_read_alexandria_source_snippets",
        lambda paths, max_chars=4500: "Source: advanced/czar/CONTEXT.md\nStudio target",
    )
    monkeypatch.setattr(
        gateway_run,
        "_read_hermes_release_context_snippet",
        lambda message, max_chars=3500: "Source: RELEASE_v0.11.0.md\n/steer and orchestrator delegation",
    )

    result = gateway_run._build_hermes_direct_answer("Can you self heal?")

    assert result.startswith("Hermes self-heal status: 94/100.")
    assert "AC Telegram operator mode enabled" in result


@pytest.mark.asyncio
async def test_agents_command_reports_active_agents_and_processes(monkeypatch):
    session_key = build_session_key(_make_source())
    session_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )
    runner = _make_runner(session_entry)
    running_agent = SimpleNamespace(
        session_id="sess-running",
        model="openrouter/test-model",
        interrupt=MagicMock(),
        get_activity_summary=lambda: {"seconds_since_activity": 0},
    )
    runner._running_agents[session_key] = running_agent
    runner._running_agents_ts = {session_key: time.time() - 8}
    runner._background_tasks = set()

    class _FakeRegistry:
        def list_sessions(self):
            return [
                {
                    "session_id": "proc-1",
                    "status": "running",
                    "uptime_seconds": 17,
                    "command": "sleep 30",
                }
            ]

    monkeypatch.setattr("tools.process_registry.process_registry", _FakeRegistry())

    result = await runner._handle_message(_make_event("/agents"))

    assert "**Active agents:** 1" in result
    assert "**Running background processes:** 1" in result
    assert "proc-1" in result
    running_agent.interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_tasks_alias_routes_to_agents_command(monkeypatch):
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )
    runner = _make_runner(session_entry)
    runner._background_tasks = set()

    class _FakeRegistry:
        def list_sessions(self):
            return []

    monkeypatch.setattr("tools.process_registry.process_registry", _FakeRegistry())

    result = await runner._handle_message(_make_event("/tasks"))

    assert "Active Agents & Tasks" in result


@pytest.mark.asyncio
async def test_handle_message_persists_agent_token_counts(monkeypatch):
    import gateway.run as gateway_run

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    runner.session_store.load_transcript.return_value = [{"role": "user", "content": "earlier"}]
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ok",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 80,
            "input_tokens": 120,
            "output_tokens": 45,
            "model": "openai/test-model",
        }
    )

    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100000,
    )

    result = await runner._handle_message(_make_event("hello"))

    assert result == "ok"
    runner.session_store.update_session.assert_called_once_with(
        session_entry.session_key,
        last_prompt_tokens=80,
    )


@pytest.mark.asyncio
async def test_handle_message_discards_stale_result_after_session_invalidation(monkeypatch):
    import gateway.run as gateway_run

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    runner.session_store.load_transcript.return_value = [{"role": "user", "content": "earlier"}]
    session_key = session_entry.session_key
    runner.adapters[Platform.TELEGRAM]._post_delivery_callbacks = {session_key: object()}

    async def _stale_result(**kwargs):
        runner._invalidate_session_run_generation(kwargs["session_key"], reason="test_stale_result")
        return {
            "final_response": "late reply",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 80,
            "input_tokens": 120,
            "output_tokens": 45,
            "model": "openai/test-model",
        }

    runner._run_agent = AsyncMock(side_effect=_stale_result)

    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100000,
    )

    result = await runner._handle_message(_make_event("hello"))

    assert result is None
    runner.session_store.append_to_transcript.assert_not_called()
    runner.session_store.update_session.assert_not_called()
    assert session_key not in runner.adapters[Platform.TELEGRAM]._post_delivery_callbacks


@pytest.mark.asyncio
async def test_handle_message_stale_result_keeps_newer_generation_callback(monkeypatch):
    import gateway.run as gateway_run

    class _Adapter:
        def __init__(self):
            self._post_delivery_callbacks = {}

        async def send(self, *args, **kwargs):
            return None

        def pop_post_delivery_callback(self, session_key, *, generation=None):
            entry = self._post_delivery_callbacks.get(session_key)
            if entry is None:
                return None
            if isinstance(entry, tuple):
                entry_generation, callback = entry
                if generation is not None and entry_generation != generation:
                    return None
                self._post_delivery_callbacks.pop(session_key, None)
                return callback
            if generation is not None:
                return None
            return self._post_delivery_callbacks.pop(session_key, None)

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    runner.session_store.load_transcript.return_value = [{"role": "user", "content": "earlier"}]
    session_key = session_entry.session_key
    adapter = _Adapter()
    runner.adapters[Platform.TELEGRAM] = adapter

    async def _stale_result(**kwargs):
        # Simulate a newer run claiming the callback slot before the stale run unwinds.
        runner._session_run_generation[session_key] = 2
        adapter._post_delivery_callbacks[session_key] = (2, lambda: None)
        return {
            "final_response": "late reply",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 80,
            "input_tokens": 120,
            "output_tokens": 45,
            "model": "openai/test-model",
        }

    runner._run_agent = AsyncMock(side_effect=_stale_result)

    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100000,
    )

    result = await runner._handle_message(_make_event("hello"))

    assert result is None
    assert session_key in adapter._post_delivery_callbacks
    assert adapter._post_delivery_callbacks[session_key][0] == 2



@pytest.mark.asyncio
async def test_status_command_bypasses_active_session_guard():
    """When an agent is running, /status must be dispatched immediately via
    base.handle_message — not queued or treated as an interrupt (#5046)."""
    import asyncio
    from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType
    from gateway.session import build_session_key
    from gateway.config import Platform, PlatformConfig

    source = _make_source()
    session_key = build_session_key(source)

    handler_called_with = []

    async def fake_handler(event):
        handler_called_with.append(event)
        return "📊 **Hermes Gateway Status**\n**Agent Running:** Yes ⚡"

    # Concrete subclass to avoid abstract method errors
    class _ConcreteAdapter(BasePlatformAdapter):
        platform = Platform.TELEGRAM

        async def connect(self): pass
        async def disconnect(self): pass
        async def send(self, chat_id, content, **kwargs): pass
        async def get_chat_info(self, chat_id): return {}

    platform_config = PlatformConfig(enabled=True, token="***")
    adapter = _ConcreteAdapter(platform_config, Platform.TELEGRAM)
    adapter.set_message_handler(fake_handler)

    sent = []

    async def fake_send_with_retry(chat_id, content, reply_to=None, metadata=None):
        sent.append(content)

    adapter._send_with_retry = fake_send_with_retry

    # Simulate an active session
    interrupt_event = asyncio.Event()
    adapter._active_sessions[session_key] = interrupt_event

    event = MessageEvent(
        text="/status",
        source=source,
        message_id="m1",
        message_type=MessageType.COMMAND,
    )
    await adapter.handle_message(event)

    assert handler_called_with, "/status handler was never called (event was queued or dropped)"
    assert sent, "/status response was never sent"
    assert "Agent Running" in sent[0]
    assert not interrupt_event.is_set(), "/status incorrectly triggered an agent interrupt"
    assert session_key not in adapter._pending_messages, "/status was incorrectly queued"


@pytest.mark.asyncio
async def test_profile_command_reports_custom_root_profile(monkeypatch, tmp_path):
    """Gateway /profile detects custom-root profiles (not under ~/.hermes)."""
    from pathlib import Path

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    profile_home = tmp_path / "profiles" / "coder"

    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "unrelated-home")

    result = await runner._handle_profile_command(_make_event("/profile"))

    assert "**Profile:** `coder`" in result
    assert f"**Home:** `{profile_home}`" in result
