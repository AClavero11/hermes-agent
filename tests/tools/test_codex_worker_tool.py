from __future__ import annotations

import json
import os
import subprocess

from tools import codex_worker_tool as cwt


def _reset_status_cache():
    cwt._STATUS_CACHE = None


def test_codex_worker_status_requires_chatgpt_login(monkeypatch):
    _reset_status_cache()
    monkeypatch.setattr(cwt.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(
        cwt,
        "_run_codex_login_status",
        lambda timeout=10.0: (0, "Logged in using ChatGPT"),
    )

    status = cwt.codex_worker_status(force=True)

    assert status["available"] is True
    assert status["auth_mode"] == "chatgpt"


def test_codex_worker_status_rejects_api_key_login(monkeypatch):
    _reset_status_cache()
    monkeypatch.setattr(cwt.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(
        cwt,
        "_run_codex_login_status",
        lambda timeout=10.0: (0, "Logged in using an API key - sk-proj-***"),
    )

    status = cwt.codex_worker_status(force=True)

    assert status["available"] is False
    assert status["auth_mode"] == "api_key"


def test_validate_codex_worker_task_blocks_external_quote_send():
    error = cwt.validate_codex_worker_task("send this quote to the customer by email")

    assert error
    assert "external-facing" in error


def test_validate_codex_worker_task_blocks_push_to_github():
    error = cwt.validate_codex_worker_task("push the repo to github")

    assert error
    assert "pushes and production deploys" in error


def test_validate_codex_worker_task_requires_coding_signal():
    error = cwt.validate_codex_worker_task("what should I cook tonight?")

    assert error
    assert "coding" in error


def test_codex_worker_dry_run_returns_command_preview(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cwt,
        "codex_worker_status",
        lambda **kwargs: {
            "available": True,
            "auth_mode": "chatgpt",
            "binary": "/usr/bin/codex",
            "summary": "ok",
        },
    )

    result = json.loads(
        cwt.codex_worker(
            {
                "task": "review the repository code for failing tests",
                "workdir": str(tmp_path),
                "sandbox": "read-only",
                "dry_run": True,
            }
        )
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["auth_mode"] == "chatgpt"
    assert result["sandbox"] == "read-only"
    assert result["command_preview"][:4] == ["/usr/bin/codex", "-a", "never", "exec"]


def test_codex_worker_exec_sanitizes_env_and_captures_final_message(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
    monkeypatch.setattr(
        cwt,
        "codex_worker_status",
        lambda **kwargs: {
            "available": True,
            "auth_mode": "chatgpt",
            "binary": "/usr/bin/codex",
            "summary": "ok",
        },
    )

    def fake_run(command, *, input, cwd, env, capture_output, text, timeout, check):
        captured["command"] = command
        captured["input"] = input
        captured["cwd"] = cwd
        captured["env"] = env
        output_path = command[command.index("--output-last-message") + 1]
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write("worker final summary")
        return subprocess.CompletedProcess(command, 0, stdout="stdout text", stderr="")

    monkeypatch.setattr(cwt.subprocess, "run", fake_run)

    result = json.loads(
        cwt.codex_worker(
            {
                "task": "fix failing tests in the repository",
                "workdir": str(tmp_path),
                "timeout_seconds": 60,
            }
        )
    )

    assert result["success"] is True
    assert result["final_message"] == "worker final summary"
    assert captured["command"][:4] == ["/usr/bin/codex", "-a", "never", "exec"]
    assert captured["command"][captured["command"].index("-m") + 1] == "gpt-5.5"
    assert captured["command"][captured["command"].index("-s") + 1] == "workspace-write"
    assert captured["cwd"] == str(tmp_path.resolve())
    assert "OPENAI_API_KEY" not in captured["env"]
    assert "OPENROUTER_API_KEY" not in captured["env"]
    assert "Hard safety rules:" in captured["input"]
    assert os.environ["OPENAI_API_KEY"] == "sk-test"
