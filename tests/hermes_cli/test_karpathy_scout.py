from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from hermes_cli import karpathy_scout


def _release_payload():
    return [
        {
            "name": "v1.2.3",
            "tag_name": "v1.2.3",
            "html_url": "https://github.com/openai/codex/releases/tag/v1.2.3",
            "body": (
                "Security and reliability release: fixed sandbox auth timeout, "
                "added regression canary tests, improved deterministic eval metrics, "
                "and reduced token context waste."
            ),
        }
    ]


def test_collect_rank_and_build_auto_think_payload_from_github_release(monkeypatch):
    def fake_http_json(url: str, *, timeout: float):
        assert "api.github.com" in url
        return _release_payload()

    monkeypatch.setattr(karpathy_scout, "_http_json", fake_http_json)

    items = karpathy_scout.collect_external_items(
        source_config={
            "github_releases": [{"repo": "openai/codex", "systems": ["codex_worker", "safety"]}],
            "github_searches": [],
            "x_urls": [],
        },
        limit=5,
    )
    ranked = karpathy_scout.rank_items(items)
    item, score = ranked[0]
    payload = karpathy_scout.build_auto_think_payload(item, score)

    assert item.locator == "https://github.com/openai/codex/releases/tag/v1.2.3"
    assert score.total >= 70
    assert payload["source_type"] == "article"
    assert payload["approval_required"] is True
    assert "destructive prod changes require AC approval" in payload["stop_gates"]


def test_run_scout_writes_report_enqueues_candidate_and_sends_telegram(monkeypatch, tmp_path):
    item = karpathy_scout.ExternalItem(
        source_type="github_release",
        locator="https://github.com/openai/codex/releases/tag/v1.2.3",
        title="openai/codex: reliability release",
        summary="Fixed sandbox auth timeout with regression canary tests and eval metrics.",
        systems=["codex_worker", "safety"],
        fetched_at="2026-05-11T12:00:00+00:00",
    )

    monkeypatch.setattr(karpathy_scout, "collect_external_items", lambda **_kwargs: [item])
    monkeypatch.setattr(
        karpathy_scout,
        "run_metric_tests",
        lambda _repo_root: karpathy_scout.TestResult(
            command=["pytest", "tests/hermes_cli/test_karpathy_scout.py", "-q"],
            ok=True,
            exit_code=0,
            elapsed_ms=321,
            output_tail="1 passed",
        ),
    )
    sent_messages: list[str] = []
    monkeypatch.setattr(
        karpathy_scout,
        "send_telegram_packet",
        lambda message: sent_messages.append(message) or {"sent": True, "message_id": 123},
    )

    report = karpathy_scout.run_scout(
        hermes_home=tmp_path,
        repo_root=Path.cwd(),
        source_config_path=None,
        telegram=True,
    )

    assert report.status == "ready_for_approval"
    assert report.telegram["sent"] is True
    assert sent_messages and "Approval required before implementation" in sent_messages[0]
    assert Path(report.artifacts["json"]).is_file()
    assert Path(report.artifacts["markdown"]).is_file()
    assert (tmp_path / "karpathy_scout" / "reports" / "latest.json").is_file()
    queued = (tmp_path / "auto_think" / "candidates.jsonl").read_text(encoding="utf-8")
    assert "Karpathy scout" in queued


def test_run_scout_dedupes_repeated_telegram_candidate(monkeypatch, tmp_path):
    item = karpathy_scout.ExternalItem(
        source_type="github_release",
        locator="https://github.com/openai/codex/releases/tag/v1.2.3",
        title="openai/codex: reliability release",
        summary="Fixed sandbox auth timeout with regression canary tests and eval metrics.",
        systems=["codex_worker", "safety"],
        fetched_at="2026-05-11T12:00:00+00:00",
    )

    monkeypatch.setattr(karpathy_scout, "collect_external_items", lambda **_kwargs: [item])
    monkeypatch.setattr(
        karpathy_scout,
        "run_metric_tests",
        lambda _repo_root: karpathy_scout.TestResult(
            command=["pytest"],
            ok=True,
            exit_code=0,
            elapsed_ms=100,
            output_tail="passed",
        ),
    )
    sent_messages: list[str] = []
    monkeypatch.setattr(
        karpathy_scout,
        "send_telegram_packet",
        lambda message: sent_messages.append(message) or {"sent": True, "message_id": len(sent_messages)},
    )

    first = karpathy_scout.run_scout(
        hermes_home=tmp_path,
        repo_root=Path.cwd(),
        source_config_path=None,
        telegram=True,
    )
    second = karpathy_scout.run_scout(
        hermes_home=tmp_path,
        repo_root=Path.cwd(),
        source_config_path=None,
        telegram=True,
    )

    history = (tmp_path / "karpathy_scout" / "reports" / "history.jsonl").read_text(encoding="utf-8")
    assert first.telegram["sent"] is True
    assert second.telegram["deduped"] is True
    assert len(sent_messages) == 1
    assert len(history.strip().splitlines()) == 2


def test_run_scout_blocks_candidate_when_tests_fail(monkeypatch, tmp_path):
    item = karpathy_scout.ExternalItem(
        source_type="github_release",
        locator="https://github.com/openai/codex/releases/tag/v1.2.3",
        title="openai/codex: reliability release",
        summary="Fixed sandbox auth timeout with regression canary tests and eval metrics.",
        systems=["codex_worker", "safety"],
        fetched_at="2026-05-11T12:00:00+00:00",
    )

    monkeypatch.setattr(karpathy_scout, "collect_external_items", lambda **_kwargs: [item])
    monkeypatch.setattr(
        karpathy_scout,
        "run_metric_tests",
        lambda _repo_root: karpathy_scout.TestResult(
            command=["pytest"],
            ok=False,
            exit_code=1,
            elapsed_ms=100,
            output_tail="failed",
        ),
    )

    report = karpathy_scout.run_scout(
        hermes_home=tmp_path,
        repo_root=Path.cwd(),
        source_config_path=None,
        telegram=False,
    )

    assert report.status == "blocked"
    assert not (tmp_path / "auto_think" / "candidates.jsonl").exists()
    assert "not enqueued" in " ".join(report.notes)


def test_markdown_report_contains_metric_and_gate_context(tmp_path):
    report = karpathy_scout.ScoutReport(
        status="ready_for_approval",
        generated_at="2026-05-11T12:00:00+00:00",
        candidate={
            "title": "Karpathy scout: test",
            "source_locator": "https://github.com/example/repo",
            "risk_class": "prod_change",
            "approval_required": True,
        },
        score={
            "total": 88,
            "ev_score": 8,
            "lane": "reliability",
            "rationale": "deterministic",
        },
        tests=asdict(
            karpathy_scout.TestResult(
                command=["pytest", "-q"],
                ok=True,
                exit_code=0,
                elapsed_ms=10,
                output_tail="passed",
            )
        ),
        baseline={"git_branch": "main", "git_sha": "abc123", "latest_canary_percent": 100.0},
        artifacts={"json": str(tmp_path / "report.json"), "markdown": str(tmp_path / "report.md")},
        telegram={"sent": False},
        notes=[],
    )

    markdown = karpathy_scout.render_markdown_report(report)

    assert "Status: `ready_for_approval`" in markdown
    assert "Total: 88/100" in markdown
    assert "No implementation, deployment, customer send" in markdown
    assert "```" in markdown
