#!/usr/bin/env python3
"""Focused Hermes operator-path canary.

This script avoids live customer/customer-system writes. It exercises the
operator fast path, timeout reporting, X-link batching, /goal, Workspace, and
runtime status formatting with local fakes/temp stores.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _failures() -> list[str]:
    return []


def _assert(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def canary_broad_operator_fast_path(failures: list[str]) -> None:
    import gateway.run as gateway_run

    prompt = "i feel like youre regressed since march how are you potentially better"
    _assert(gateway_run._is_operator_capability_prompt(prompt), "broad regression prompt was not classified", failures)

    original_post_json = gateway_run._post_json
    previous_key = os.environ.get("HERMES_FRONTIER_API_KEY")
    previous_model = os.environ.get("OPENAI_FRONTIER_MODEL")
    previous_prefer_runtime = os.environ.get("HERMES_OPERATOR_CAPABILITY_PREFER_RUNTIME")
    os.environ["HERMES_FRONTIER_API_KEY"] = "canary-key"
    os.environ["OPENAI_FRONTIER_MODEL"] = "canary-model"
    os.environ["HERMES_OPERATOR_CAPABILITY_PREFER_RUNTIME"] = "0"

    def fake_post_json(*_args, **_kwargs):
        return {
            "output_text": (
                "Hermes is better when it stays in operator mode: RFQ and V11 context become sourced drafts, "
                "follow-ups stay approval-ready, runtime issues get inspected with evidence, and code/file work "
                "gets patched instead of discussed. Approval required before customer sends, V11/Atlas writes, "
                "or destructive actions."
            )
        }

    try:
        gateway_run._post_json = fake_post_json
        started = time.perf_counter()
        answer = gateway_run._build_operator_capability_model_answer_sync(prompt)
        elapsed = time.perf_counter() - started
    finally:
        gateway_run._post_json = original_post_json
        if previous_key is None:
            os.environ.pop("HERMES_FRONTIER_API_KEY", None)
        else:
            os.environ["HERMES_FRONTIER_API_KEY"] = previous_key
        if previous_model is None:
            os.environ.pop("OPENAI_FRONTIER_MODEL", None)
        else:
            os.environ["OPENAI_FRONTIER_MODEL"] = previous_model
        if previous_prefer_runtime is None:
            os.environ.pop("HERMES_OPERATOR_CAPABILITY_PREFER_RUNTIME", None)
        else:
            os.environ["HERMES_OPERATOR_CAPABILITY_PREFER_RUNTIME"] = previous_prefer_runtime

    _assert(elapsed < 12.0, f"broad fast path took {elapsed:.2f}s", failures)
    _assert("Received. Working" not in answer, "broad fast path returned receipt text", failures)
    _assert("Approval required" in answer, "broad fast path omitted approval gate", failures)
    _assert("- RFQ/quotes:" not in answer, "broad fast path emitted canned menu labels", failures)


def canary_operator_timeout(failures: list[str]) -> None:
    import gateway.run as gateway_run

    old_timeout = os.environ.get("HERMES_OPERATOR_FAST_PATH_TIMEOUT")
    original = gateway_run._build_operator_capability_model_answer

    async def slow_answer(_message: str) -> str:
        await asyncio.sleep(0.2)
        return "late"

    async def run_probe() -> str:
        os.environ["HERMES_OPERATOR_FAST_PATH_TIMEOUT"] = "0.01"
        gateway_run._build_operator_capability_model_answer = slow_answer
        try:
            return await gateway_run._run_operator_capability_fast_path("what can you do")
        finally:
            gateway_run._build_operator_capability_model_answer = original
            if old_timeout is None:
                os.environ.pop("HERMES_OPERATOR_FAST_PATH_TIMEOUT", None)
            else:
                os.environ["HERMES_OPERATOR_FAST_PATH_TIMEOUT"] = old_timeout

    result = asyncio.run(run_probe())
    _assert("Route:" in result, "timeout report missing route", failures)
    _assert("Elapsed:" in result, "timeout report missing elapsed time", failures)
    _assert("/runtime status" in result, "timeout report missing next safe action", failures)


def canary_x_links(failures: list[str]) -> None:
    import gateway.run as gateway_run
    import tools.x_scraper_tool as x_scraper_tool

    original = x_scraper_tool.x_scrape_tool
    seen: list[list[str]] = []

    def fake_scrape(urls):
        seen.append(list(urls))
        return json.dumps({"content": "batched tweet content", "source": "canary"})

    try:
        x_scraper_tool.x_scrape_tool = fake_scrape
        message = " ".join(f"https://x.com/u/status/{100 + i}" for i in range(11))
        prompt = gateway_run._build_x_link_context_prompt(message)
    finally:
        x_scraper_tool.x_scrape_tool = original

    _assert(seen and len(seen[0]) == 11, "X context did not pass all 11 links to x_scrape", failures)
    _assert("https://x.com/u/status/110" in prompt, "X context omitted the 11th link", failures)


def canary_goal_and_workspace(failures: list[str]) -> None:
    from hermes_cli.goals import GoalManager
    from hermes_cli.workspace import WorkspaceStore

    with tempfile.TemporaryDirectory(prefix="hermes-operator-canary-") as tmp:
        tmp_path = Path(tmp)
        original_judge = GoalManager._judge_goal
        GoalManager._judge_goal = lambda self, goal, response: (False, "continue")
        goal = GoalManager("agent:main:telegram:dm:canary", default_max_turns=2, store_path=tmp_path / "goals.json")
        try:
            goal.set_goal("repair operator path")
            _assert("repair operator path" in goal.status_message(), "/goal status missing goal text", failures)
            _assert(goal.pause() is not None and "paused" in goal.status_message(), "/goal pause failed", failures)
            _assert(goal.resume() is not None and "running" in goal.status_message(), "/goal resume failed", failures)
            _assert(goal.evaluate_after_turn("still working") is not None, "/goal first continuation did not schedule", failures)
            _assert(goal.evaluate_after_turn("still working") is None, "/goal max-turn pause failed", failures)
            _assert(goal.load() is not None and goal.load().paused, "/goal budget did not pause state", failures)
            goal.clear()
            _assert(goal.load() is None, "/goal clear failed", failures)
        finally:
            GoalManager._judge_goal = original_judge

        store = WorkspaceStore(tmp_path / "workspace" / "control_plane.json")
        task = store.create_task("operator canary task")
        store.update_task(task["id"], status="in_progress", note="started")
        evidence = store.add_evidence(task_id=task["id"], locator="/tmp/hermes-canary.txt")
        report = store.create_report(title="Operator Canary")
        _assert(store.list_tasks(status="in_progress")[0]["id"] == task["id"], "workspace list/start failed", failures)
        _assert(evidence["id"] in store.get_task(task["id"])["evidence_ids"], "workspace evidence failed", failures)
        _assert(Path(report["path"]).is_file(), "workspace report file was not created", failures)


def canary_provider_timeout_and_runtime_status(failures: list[str]) -> None:
    import run_agent
    from hermes_cli.runtime_status import format_runtime_status
    from run_agent import AIAgent

    original_timeout_fn = run_agent.get_provider_request_timeout
    try:
        run_agent.get_provider_request_timeout = lambda *_args, **_kwargs: None
        old_api_timeout = os.environ.get("HERMES_API_TIMEOUT")
        os.environ["HERMES_API_TIMEOUT"] = "1800"
        agent = object.__new__(AIAgent)
        agent.provider = "canary"
        agent.model = "canary-model"
        agent.request_timeout_seconds = 45.0
        _assert(agent._resolved_api_call_timeout() == 45.0, "normal provider timeout cap failed", failures)
    finally:
        run_agent.get_provider_request_timeout = original_timeout_fn
        if old_api_timeout is None:
            os.environ.pop("HERMES_API_TIMEOUT", None)
        else:
            os.environ["HERMES_API_TIMEOUT"] = old_api_timeout

    rendered = format_runtime_status(
        {
            "host": "canary",
            "launchd": {"label": "ai.hermes.deepseek-gateway", "pid": "1", "state": "running", "working_directory": str(ROOT)},
            "wrapper": {"path": "/tmp/hermes-env.sh", "selected_repo": str(ROOT)},
            "python": {"path": sys.executable},
            "model": {
                "provider": "canary",
                "name": "canary-model",
                "routing": {
                    "frontier_available": False,
                    "frontier_verified": False,
                    "frontier_source": "",
                    "routes": {
                        "planner": {"provider": "canary", "model": "planner"},
                        "hard_task_planner": {"provider": "canary", "model": "planner"},
                        "executor": {"provider": "canary", "model": "executor"},
                        "verifier": {"provider": "canary", "model": "judge"},
                        "synthesizer": {"provider": "canary", "model": "synth"},
                    },
                },
            },
            "health": {"ok": False, "url": "http://127.0.0.1:8642/health", "skipped": True},
            "git": {
                "repo_root": {"short_sha": "abc", "branch": "main", "dirty": False},
                "runtime_repo": {"short_sha": "abc", "branch": "main", "dirty": False},
            },
            "state": {"path": "/tmp/gateway_state.json", "active_agents_count": 0},
            "goals": {"active_count": 0, "total_count": 0},
            "workspace": {"active_task_count": 0, "task_count": 0},
        }
    )
    _assert("launchd cwd:" in rendered, "runtime status missing launchd cwd", failures)
    _assert("planner: canary:planner" in rendered, "runtime status missing model route", failures)


def main() -> int:
    failures = _failures()
    checks = [
        canary_broad_operator_fast_path,
        canary_operator_timeout,
        canary_x_links,
        canary_goal_and_workspace,
        canary_provider_timeout_and_runtime_status,
    ]
    for check in checks:
        try:
            check(failures)
        except Exception as exc:
            failures.append(f"{check.__name__} raised {type(exc).__name__}: {exc}")

    if failures:
        print("Hermes operator canary: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("Hermes operator canary: PASS")
    print("- broad operator prompts hit the model-backed fast path")
    print("- provider timeout reports include route, elapsed time, and next action")
    print("- X link batching, /goal, Workspace, and runtime status canaries passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
