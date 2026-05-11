import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent, has_explicit_execution_intent


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _tool_call(name: str, arguments: dict | None = None):
    return SimpleNamespace(
        id=f"call_{uuid.uuid4().hex[:8]}",
        type="function",
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments or {}),
        ),
    )


def _response(*, content: str | None = None, tool_calls: list | None = None, finish_reason: str = "stop"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="unit-test")


def _agent(*tools: str) -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs(*tools)),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://example.test/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    return agent


def test_planning_does_not_execute(monkeypatch):
    agent = _agent("web_search", "memory")
    tool_starts = []
    agent.tool_start_callback = lambda *args: tool_starts.append(args)
    agent.client.chat.completions.create.side_effect = AssertionError("planning mode must not call model/tools")

    with (
        patch("run_agent.handle_function_call", side_effect=AssertionError("tool dispatch must not run")),
        patch("tools.memory_tool.memory_tool", side_effect=AssertionError("memory write must not run")),
    ):
        result = agent.run_conversation("what can you work on for me right now")

    assert result["completed"] is True
    assert result["api_calls"] == 0
    assert result["planning_mode"] is True
    assert tool_starts == []
    assert all(message.get("role") != "tool" for message in result["messages"])
    assert all(not message.get("tool_calls") for message in result["messages"] if isinstance(message, dict))
    assert "Prioritized tasks:" in result["final_response"]
    assert "Next available action:" in result["final_response"]
    assert "Execute? (yes/no)" in result["final_response"]


def test_tool_call_without_execution_intent_returns_plan(monkeypatch):
    agent = _agent("web_search")
    agent.client.chat.completions.create.return_value = _response(
        tool_calls=[_tool_call("web_search", {"query": "current status"})],
        finish_reason="tool_calls",
    )

    with patch("run_agent.handle_function_call", side_effect=AssertionError("tool dispatch must not run")):
        result = agent.run_conversation("could you look into this for me")

    assert result["completed"] is True
    assert "Execution blocked: web_search not run without explicit execution intent." in result["final_response"]
    assert "Execute? (yes/no)" in result["final_response"]
    assert all(message.get("role") != "tool" for message in result["messages"])


def test_learn_from_links_executes_skill_tool(monkeypatch):
    agent = _agent("skill_view")
    agent.client.chat.completions.create.side_effect = [
        _response(
            tool_calls=[_tool_call("skill_view", {"skill": "browser-harness"})],
            finish_reason="tool_calls",
        ),
        _response(content="Learned from the linked source."),
    ]

    with patch("run_agent.handle_function_call", return_value='{"ok": true}') as handle_tool:
        result = agent.run_conversation("Learn from these links")

    assert result["completed"] is True
    assert result["final_response"] == "Learned from the linked source."
    handle_tool.assert_called_once()
    assert handle_tool.call_args.args[:2] == (
        "skill_view",
        {"skill": "browser-harness"},
    )
    assert any(message.get("role") == "tool" for message in result["messages"])


def test_kanban_work_command_executes_terminal_tool(monkeypatch):
    agent = _agent("terminal")
    agent.client.chat.completions.create.side_effect = [
        _response(
            tool_calls=[_tool_call("terminal", {"command": "kanban_show"})],
            finish_reason="tool_calls",
        ),
        _response(content="Worker handoff complete."),
    ]

    with patch("run_agent.handle_function_call", return_value='{"ok": true}') as handle_tool:
        result = agent.run_conversation("work kanban task t_be60fcd7")

    assert result["completed"] is True
    assert result["final_response"] == "Worker handoff complete."
    handle_tool.assert_called_once()
    assert handle_tool.call_args.args[:2] == (
        "terminal",
        {"command": "kanban_show"},
    )
    assert any(message.get("role") == "tool" for message in result["messages"])


def test_planning_prompt_still_overrides_work_word(monkeypatch):
    assert has_explicit_execution_intent("what can you work on for me right now") is True

    agent = _agent("terminal")
    agent.client.chat.completions.create.side_effect = AssertionError("planning mode must not call model/tools")

    result = agent.run_conversation("what can you work on for me right now")

    assert result["completed"] is True
    assert result["planning_mode"] is True
    assert "Prioritized tasks:" in result["final_response"]
    assert all(message.get("role") != "tool" for message in result["messages"])


def test_memory_write_failure_stops_execution_chain(monkeypatch):
    agent = _agent("memory", "web_search")
    agent.client.chat.completions.create.return_value = _response(
        tool_calls=[
            _tool_call("memory", {"action": "add", "target": "memory", "content": "durable fact"}),
            _tool_call("web_search", {"query": "must not run"}),
        ],
        finish_reason="tool_calls",
    )

    with patch("run_agent.handle_function_call", side_effect=AssertionError("next tool must not run")):
        result = agent.run_conversation("update memory with this durable fact")

    assert result["completed"] is True
    assert result["final_response"].startswith("Memory write failed:")
    assert "No further actions executed" in result["final_response"]
    assert not any(
        message.get("role") == "tool" and "must not run" in str(message.get("content"))
        for message in result["messages"]
    )
