from __future__ import annotations

from hermes_cli import canary
from gateway import context_router


def test_canary_brain_retrieval_passes_with_fresh_lsh_qmd_contract(monkeypatch, tmp_path):
    monkeypatch.setattr(
        context_router,
        "brain_retrieval_health",
        lambda max_age_days=14: {
            "commands": {
                "alex-search": "/bin/alex-search",
                "v11-search": "/bin/v11-search",
                "qmd": "/bin/qmd",
            },
            "missing_commands": [],
            "indexes": {
                "alexandria_lsh": {"fresh": True},
                "v11_lsh": {"fresh": True},
                "qmd": {"fresh": True},
            },
            "daemon": {"running": True, "responsive": True},
            "stale_indexes": [],
            "qmd_mode": "support",
            "qmd_command": ["qmd", "search", "health", "-n", "4", "--json"],
            "retrieval_order": ["alex-search", "qmd-search", "v11-search"],
            "lsh_first": True,
            "qmd_expansion_default": False,
        },
    )
    monkeypatch.setattr(
        context_router,
        "collect_alexandria_context",
        lambda message, **kwargs: {
            "retrieval_succeeded": True,
            "source_paths": ["_system/ROUTING.md", "advanced/czar/CONTEXT.md"],
            "retrieval_plan": {"order": ["alex-search", "qmd-search"]},
            "errors": [],
        },
    )
    options = canary.CanaryOptions(repo_root=tmp_path, hermes_home=tmp_path)

    result = canary._canary_brain_retrieval(options)

    assert result.status == canary.PASS
    assert result.score == 20
    assert "fresh_indexes" in result.details["passed"]
    assert "lsh_daemon" in result.details["passed"]


def test_canary_brain_retrieval_warns_on_stale_indexes(monkeypatch, tmp_path):
    monkeypatch.setattr(
        context_router,
        "brain_retrieval_health",
        lambda max_age_days=14: {
            "commands": {
                "alex-search": "/bin/alex-search",
                "v11-search": "/bin/v11-search",
                "qmd": "/bin/qmd",
            },
            "missing_commands": [],
            "indexes": {"qmd": {"fresh": False, "age_days": 30}},
            "daemon": {"running": True, "responsive": True},
            "stale_indexes": ["qmd"],
            "qmd_mode": "support",
            "qmd_command": ["qmd", "search", "health", "-n", "4", "--json"],
            "retrieval_order": ["alex-search", "qmd-search", "v11-search"],
            "lsh_first": True,
            "qmd_expansion_default": False,
        },
    )
    monkeypatch.setattr(
        context_router,
        "collect_alexandria_context",
        lambda message, **kwargs: {
            "retrieval_succeeded": True,
            "source_paths": ["_system/ROUTING.md"],
            "retrieval_plan": {"order": ["alex-search", "qmd-search"]},
            "errors": [],
        },
    )
    options = canary.CanaryOptions(repo_root=tmp_path, hermes_home=tmp_path)

    result = canary._canary_brain_retrieval(options)

    assert result.status == canary.WARN
    assert result.details["warned"]["stale_indexes"]["stale"] == ["qmd"]


def test_canary_brain_retrieval_fails_when_lsh_daemon_is_unresponsive(monkeypatch, tmp_path):
    monkeypatch.setattr(
        context_router,
        "brain_retrieval_health",
        lambda max_age_days=14: {
            "commands": {
                "alex-search": "/bin/alex-search",
                "v11-search": "/bin/v11-search",
                "qmd": "/bin/qmd",
            },
            "missing_commands": [],
            "indexes": {
                "alexandria_lsh": {"fresh": True},
                "v11_lsh": {"fresh": True},
                "qmd": {"fresh": True},
            },
            "daemon": {"running": True, "responsive": False, "error": "socket missing"},
            "stale_indexes": [],
            "qmd_mode": "support",
            "qmd_command": ["qmd", "search", "health", "-n", "4", "--json"],
            "retrieval_order": ["alex-search", "qmd-search", "v11-search"],
            "lsh_first": True,
            "qmd_expansion_default": False,
        },
    )
    monkeypatch.setattr(
        context_router,
        "collect_alexandria_context",
        lambda message, **kwargs: {
            "retrieval_succeeded": True,
            "source_paths": ["_system/ROUTING.md"],
            "retrieval_plan": {"order": ["alex-search", "qmd-search"]},
            "errors": [],
        },
    )
    options = canary.CanaryOptions(repo_root=tmp_path, hermes_home=tmp_path)

    result = canary._canary_brain_retrieval(options)

    assert result.status == canary.FAIL
    assert result.details["failed"]["lsh_daemon"]["responsive"] is False
