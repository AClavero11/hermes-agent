from __future__ import annotations

from pathlib import Path

from hermes_cli import runtime_status


def test_runtime_status_uses_wrapper_exported_home(monkeypatch, tmp_path):
    runtime_home = tmp_path / ".hermes-deepseek"
    repo_root = tmp_path / "repo"
    wrapper = runtime_home / "bin" / "hermes-env.sh"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("# test wrapper\n", encoding="utf-8")
    (runtime_home / "gateway_state.json").write_text(
        '{"active_agents": {}, "pid": 123, "platforms": []}',
        encoding="utf-8",
    )

    def fake_env_snapshot(*, env_wrapper, hermes_home, timeout):
        return (
            {
                "ok": True,
                "HERMES_HOME": str(runtime_home),
                "AAC_HERMES_DEEPSEEK_HOME": str(runtime_home),
                "AAC_HERMES_DEEPSEEK_REPO": str(repo_root),
                "AAC_HERMES_DEEPSEEK_PYTHON": "/tmp/python",
                "HERMES_PLANNER_PROVIDER": "custom:office-deepseek-v4",
                "HERMES_PLANNER_MODEL": "deepseek-v4",
            },
            [wrapper],
            wrapper,
        )

    monkeypatch.setattr(runtime_status, "_env_snapshot", fake_env_snapshot)
    monkeypatch.setattr(
        runtime_status,
        "_launchd_snapshot",
        lambda label, timeout: {"ok": True, "label": label, "pid": "1", "state": "running"},
    )
    monkeypatch.setattr(
        runtime_status,
        "_health_probe",
        lambda url, timeout: {"ok": True, "url": url, "status": 200, "duration_ms": 1.0},
    )
    monkeypatch.setattr(
        runtime_status,
        "_git_info",
        lambda repo, timeout: {
            "ok": True,
            "path": str(Path(repo)),
            "short_sha": "abc123",
            "branch": "test",
            "dirty": False,
        },
    )

    status = runtime_status.collect_runtime_status(repo_root=repo_root, timeout=0.1)

    assert status["hermes_home"] == str(runtime_home)
    assert status["state"]["path"] == str(runtime_home / "gateway_state.json")

