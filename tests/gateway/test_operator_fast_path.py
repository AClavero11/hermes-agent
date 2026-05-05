import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_type="dm",
        user_id="user-1",
        user_name="AC",
    )


def _event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_source(), message_id="msg-1")


def _minimal_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._is_user_authorized = lambda _source: True
    runner._session_key_for_source = lambda _source: "agent:main:telegram:dm:chat-1"
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._update_prompt_pending = {}
    runner._handle_message_with_agent = AsyncMock(side_effect=AssertionError("agent loop should not run"))
    return runner


def test_operator_capability_classifier_catches_regression_prompt():
    import gateway.run as gateway_run

    prompt = "i feel like youre regressed since march how are you potentially better"

    assert gateway_run._is_operator_capability_prompt(prompt)


@pytest.mark.asyncio
async def test_broad_operator_prompt_uses_fast_path_not_agent_loop(monkeypatch):
    import gateway.run as gateway_run

    async def fake_fast_path(message: str) -> str:
        assert "regressed" in message
        return "Hermes operator answer from model path. Approval required before customer sends, V11/Atlas writes, or destructive actions."

    monkeypatch.setattr(gateway_run, "_run_operator_capability_fast_path", fake_fast_path)
    runner = _minimal_runner()

    started = time.perf_counter()
    result = await runner._handle_message(_event("i feel like youre regressed since march how are you potentially better"))

    assert time.perf_counter() - started < 1.0
    assert "Received. Working" not in result
    assert "operator answer" in result
    runner._handle_message_with_agent.assert_not_called()


def test_operator_fast_path_is_model_backed_not_canned_menu(monkeypatch):
    import gateway.run as gateway_run

    def fake_post_json(*_args, **_kwargs):
        return {
            "output_text": (
                "Hermes is useful when you need grounded operator work, not a chat menu. "
                "I can turn V11/RFQ context into sourced drafts, tighten follow-ups, inspect runtime failures, "
                "and patch code with evidence. Approval required before customer sends, V11/Atlas writes, or destructive actions."
            )
        }

    monkeypatch.setenv("HERMES_FRONTIER_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_FRONTIER_MODEL", "gpt-test")
    monkeypatch.setenv("HERMES_OPERATOR_CAPABILITY_PREFER_RUNTIME", "0")
    monkeypatch.setattr(gateway_run, "_post_json", fake_post_json)

    prompt = gateway_run._build_operator_capability_prompt("what can you do")
    answer = gateway_run._build_operator_capability_model_answer_sync("what can you do")

    assert "Return exactly six plain hyphen bullets" not in prompt
    assert "- RFQ/quotes:" not in answer
    assert "Approval required" in answer


def test_operator_fast_path_prefers_gemini_planner_before_pro_synthesizer(monkeypatch):
    import gateway.run as gateway_run

    calls = []

    def fake_routes():
        return {
            "frontier_available": True,
            "frontier_source": "gemini",
            "frontier_verified": True,
            "routes": {
                "planner": {"provider": "gemini", "model": "gemini-2.5-flash"},
                "hard_task_planner": {"provider": "gemini", "model": "gemini-2.5-pro"},
                "executor": {"provider": "custom:office-deepseek-v4", "model": "deepseek-v4"},
                "verifier": {"provider": "gemini", "model": "gemini-2.5-pro"},
                "synthesizer": {"provider": "gemini", "model": "gemini-2.5-pro"},
            },
        }

    def fake_post_json(url, payload, **_kwargs):
        calls.append((url, payload))
        assert "gemini-2.5-flash" in url
        assert payload["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}
        return {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": (
                                    "Hosted planner and synthesizer routing is stronger now. "
                                    "RFQ drafts, V11 inventory context, follow-up handling, Hermes runtime repair, "
                                    "research links, and code patches can be handled without dropping into a full loop. "
                                    "Customer sends, V11 writes, Atlas writes, and destructive actions require approval."
                                )
                            }
                        ]
                    }
                }
            ]
        }

    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.delenv("HERMES_FRONTIER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(gateway_run, "resolve_model_routes", fake_routes)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {})
    monkeypatch.setattr(gateway_run, "_post_json", fake_post_json)

    answer = gateway_run._build_operator_capability_model_answer_sync(
        "i feel like youre regressed since march how are you potentially better"
    )

    assert len(calls) == 1
    assert "Hosted planner" in answer
    assert "approval" in answer.lower()


@pytest.mark.asyncio
async def test_operator_fast_path_timeout_reports_route(monkeypatch):
    import gateway.run as gateway_run

    async def slow_answer(_message: str) -> str:
        await asyncio.sleep(0.2)
        return "late"

    monkeypatch.setenv("HERMES_OPERATOR_FAST_PATH_TIMEOUT", "0.01")
    monkeypatch.setattr(gateway_run, "_build_operator_capability_model_answer", slow_answer)

    result = await gateway_run._run_operator_capability_fast_path("what can you do")

    assert "fast operator route failed" in result.lower()
    assert "Route:" in result
    assert "Elapsed:" in result
    assert "/runtime status" in result


def test_gateway_provider_timeout_cap_respects_lower_config(monkeypatch):
    import run_agent
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent.provider = "unit"
    agent.model = "unit-model"
    agent.request_timeout_seconds = 45.0

    monkeypatch.setattr(run_agent, "get_provider_request_timeout", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("HERMES_API_TIMEOUT", "1800")
    assert agent._resolved_api_call_timeout() == 45.0

    monkeypatch.setenv("HERMES_API_TIMEOUT", "30")
    assert agent._resolved_api_call_timeout() == 30.0


def test_provider_timeout_failure_message_has_route_elapsed_and_next_action():
    import gateway.run as gateway_run

    result = gateway_run._format_provider_timeout_failure(
        provider="custom:office-deepseek-v4",
        model="deepseek-v4",
        elapsed=45.2,
        error="ReadTimeout",
    )

    assert "custom:office-deepseek-v4:deepseek-v4" in result
    assert "Elapsed: 45.2s" in result
    assert "Next safe action" in result


def test_gateway_x_context_prompt_handles_eleven_links(monkeypatch):
    import gateway.run as gateway_run

    seen = []

    def fake_scrape(urls):
        seen.append(list(urls))
        return json.dumps({"content": "batched tweet content", "source": "unit"})

    monkeypatch.setattr("tools.x_scraper_tool.x_scrape_tool", fake_scrape)

    message = " ".join(f"https://x.com/u/status/{100 + i}" for i in range(11))
    prompt = gateway_run._build_x_link_context_prompt(message)

    assert len(seen[0]) == 11
    assert "batched tweet content" in prompt
    assert "https://x.com/u/status/110" in prompt


def test_goal_status_pause_resume_clear(tmp_path, monkeypatch):
    from hermes_cli.goals import GoalManager

    monkeypatch.setattr(GoalManager, "_judge_goal", lambda self, goal, response: (False, "continue"))
    manager = GoalManager(
        "agent:main:telegram:dm:chat-1",
        default_max_turns=2,
        store_path=tmp_path / "goals.json",
    )
    state = manager.set_goal("repair operator path")

    assert "repair operator path" in manager.status_message()
    assert manager.pause().paused is True
    assert "paused" in manager.status_message()
    assert manager.resume().paused is False
    assert manager.evaluate_after_turn("still working") is not None
    assert manager.evaluate_after_turn("still working") is None
    assert manager.load().paused is True
    manager.clear()
    assert manager.load() is None


def test_workspace_add_list_evidence_report(tmp_path):
    from hermes_cli.workspace import WorkspaceStore

    store = WorkspaceStore(tmp_path / "workspace" / "control_plane.json")
    task = store.create_task("fix operator path")
    store.update_task(task["id"], status="in_progress", note="started")
    evidence = store.add_evidence(task_id=task["id"], locator="/tmp/evidence.txt")
    report = store.create_report(title="Operator Report")

    assert store.list_tasks(status="in_progress")[0]["id"] == task["id"]
    assert evidence["id"] in store.get_task(task["id"])["evidence_ids"]
    assert report["path"].endswith(".md")


@pytest.mark.asyncio
async def test_workflow_kill_and_resume_commands_use_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WORKFLOW_STORE", str(tmp_path / "workflow_registry.json"))
    runner = _minimal_runner()

    killed = await runner._handle_kill_command(_event("/kill rfq-intake noisy extraction"))
    assert "Workflow disabled" in killed
    assert "rfq-intake" in killed

    resumed = await runner._handle_resume_command(_event("/resume rfq-intake"))
    assert "Workflow enabled" in resumed
    assert "rfq-intake" in resumed


@pytest.mark.asyncio
async def test_api_rfq_fast_path_does_not_hijack_operator_lane():
    from gateway.platforms.api_server import _rfq_fast_path_reply_text

    assert await _rfq_fast_path_reply_text("rfq") == ""


def test_runtime_status_format_includes_path_routes_and_counts():
    from hermes_cli.runtime_status import format_runtime_status

    status = {
        "host": "unit-host",
        "launchd": {"label": "ai.hermes.deepseek-gateway", "pid": "123", "state": "running", "working_directory": "/repo"},
        "wrapper": {"path": "/home/bin/hermes-env.sh", "selected_repo": "/repo"},
        "python": {"path": "/repo/.venv/bin/python3"},
        "model": {
            "provider": "kimi-coding",
            "name": "kimi-k2.6",
            "routing": {
                "frontier_available": False,
                "frontier_verified": False,
                "frontier_source": "",
                "routes": {
                    "planner": {"provider": "kimi-coding", "model": "kimi-k2.6"},
                    "hard_task_planner": {"provider": "kimi-coding", "model": "kimi-k2.6"},
                    "executor": {"provider": "custom:office-deepseek-v4", "model": "deepseek-v4"},
                    "verifier": {"provider": "kimi-coding", "model": "kimi-k2.6"},
                    "synthesizer": {"provider": "kimi-coding", "model": "kimi-k2.6"},
                },
            },
        },
        "health": {"ok": False, "url": "http://127.0.0.1:8642/health", "skipped": True},
        "git": {
            "repo_root": {"short_sha": "abc123", "branch": "main", "dirty": False},
            "runtime_repo": {"short_sha": "abc123", "branch": "main", "dirty": False},
        },
        "state": {"path": "/home/gateway_state.json", "active_agents_count": 0},
        "goals": {"active_count": 1, "total_count": 1},
        "workspace": {"active_task_count": 2, "task_count": 3},
    }

    text = format_runtime_status(status)

    assert "launchd cwd: /repo" in text
    assert "planner: kimi-coding:kimi-k2.6" in text
    assert "workspace: active_tasks=2 total_tasks=3" in text
