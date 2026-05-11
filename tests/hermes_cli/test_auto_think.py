from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import auto_think


def _candidate_payload(**overrides):
    payload = {
        "source_type": "x_link",
        "source_locator": "https://x.com/gkisokay/status/2046171501888516188?s=46",
        "title": "Auto-think queue for high-EV ideas",
        "core_idea": [
            "Capture high-EV ideas from fetched sources.",
            "Score, dedupe, and route only dry-run operator prototypes.",
        ],
        "evidence": [
            {
                "locator": "https://x.com/gkisokay/status/2046171501888516188?s=46",
                "summary": "Auto-think / Auto-build agent workflow source was fetched.",
                "confidence": "high",
            }
        ],
        "affected_systems": ["kanban", "canary"],
        "risk_class": "internal_write",
        "approval_required": True,
        "ev": {
            "score": 8,
            "relevance": 9,
            "impact": 8,
            "effort": 5,
            "cost": 2,
            "reliability": 7,
            "privacy": 8,
            "compounding": 9,
            "rationale": "High leverage and bounded to dry-run internals.",
        },
        "recommended_route": "operator_prototype",
        "smallest_safe_prototype": "Write a dry-run JSONL candidate and task handoff only.",
        "stop_gates": [
            "customer/vendor sends require AC approval",
            "quotes require AC approval",
            "payments require AC approval",
            "orders require AC approval",
            "inventory/V11 mutations require AC approval",
            "public posts require AC approval",
            "destructive prod changes require AC approval",
            "paid signup require AC approval",
            "untrusted installs require AC approval",
        ],
        "acceptance_criteria": ["pytest tests/hermes_cli/test_auto_think.py -q passes"],
    }
    payload.update(overrides)
    return payload


def test_candidate_schema_validation_requires_gate_fields():
    required_fields = [
        "source_locator",
        "dedupe_key",
        "approval_required",
        "stop_gates",
    ]
    for field in required_fields:
        payload = _candidate_payload(dedupe_key="x:2046171501888516188:canary-kanban")
        payload.pop(field)

        with pytest.raises(auto_think.CandidateValidationError, match=field):
            auto_think.AutoThinkCandidate.from_dict(payload)

    payload = _candidate_payload(dedupe_key="x:2046171501888516188:canary-kanban")
    payload["ev"].pop("score", None)

    with pytest.raises(auto_think.CandidateValidationError, match="ev.score"):
        auto_think.AutoThinkCandidate.from_dict(payload)


def test_ev_score_is_deterministic_and_matches_weighted_rubric():
    score = auto_think.score_ev(
        relevance=9,
        impact=8,
        effort=5,
        cost=2,
        reliability=7,
        privacy=8,
        compounding=9,
    )

    assert score == 8


def test_dedupe_key_normalizes_x_status_url_and_system_order():
    key = auto_think.normalize_dedupe_key(
        source_type="x_link",
        source_locator="https://x.com/gkisokay/status/2046171501888516188?s=46",
        affected_systems=["canary", "kanban", "canary"],
    )

    assert key == "x:2046171501888516188:canary-kanban"


def test_candidate_jsonl_persistence_dedupes_existing_candidate(tmp_path):
    store = auto_think.CandidateStore(tmp_path)
    first = auto_think.AutoThinkCandidate.from_dict(
        _candidate_payload(dedupe_key="x:2046171501888516188:canary-kanban")
    )
    second = auto_think.AutoThinkCandidate.from_dict(
        _candidate_payload(
            dedupe_key="X:2046171501888516188:CANARY-KANBAN",
            title="Duplicate title",
        )
    )

    assert store.append(first) is True
    assert store.append(second) is False

    lines = (tmp_path / "auto_think" / "candidates.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["title"] == "Auto-think queue for high-EV ideas"


def test_generated_operator_task_body_contains_hard_approval_gates_and_rollback():
    candidate = auto_think.AutoThinkCandidate.from_dict(
        _candidate_payload(dedupe_key="x:2046171501888516188:canary-kanban")
    )

    body = auto_think.render_operator_task_body(candidate)

    for phrase in [
        "customer/vendor sends",
        "quotes",
        "payments",
        "orders",
        "inventory/V11 mutations",
        "public posts",
        "destructive prod changes",
        "paid signup",
        "untrusted installs",
        "Rollback",
        "dry-run",
    ]:
        assert phrase in body


def test_candidate_rejects_missing_paid_or_untrusted_install_gates():
    payload = _candidate_payload(
        dedupe_key="x:2046171501888516188:canary-kanban",
        stop_gates=[
            "customer/vendor sends require AC approval",
            "quotes require AC approval",
            "payments require AC approval",
            "orders require AC approval",
            "inventory/V11 mutations require AC approval",
            "public posts require AC approval",
            "destructive production changes require AC approval",
        ],
    )

    with pytest.raises(auto_think.CandidateValidationError, match="paid signup"):
        auto_think.AutoThinkCandidate.from_dict(payload)


def test_default_dry_run_does_not_write_candidates(tmp_path):
    payload = _candidate_payload()

    result = auto_think.enqueue_candidate(payload, hermes_home=tmp_path)

    assert result["dry_run"] is True
    assert result["written"] is False
    assert result["candidate"]["dedupe_key"] == "x:2046171501888516188:canary-kanban"
    assert not (tmp_path / "auto_think" / "candidates.jsonl").exists()


def test_explicit_write_persists_candidate(tmp_path):
    payload = _candidate_payload()

    result = auto_think.enqueue_candidate(payload, hermes_home=tmp_path, dry_run=False)

    assert result["dry_run"] is False
    assert result["written"] is True
    assert (tmp_path / "auto_think" / "candidates.jsonl").is_file()

def test_dashboard_artifact_renders_candidate_queue_with_gates_and_provenance(tmp_path):
    payload = _candidate_payload()
    auto_think.enqueue_candidate(payload, hermes_home=tmp_path, dry_run=False)

    output_path = auto_think.write_candidate_dashboard(
        hermes_home=tmp_path,
        generated_at="2026-05-10T12:00:00+00:00",
    )
    rendered = output_path.read_text(encoding="utf-8")

    assert output_path == tmp_path / "html_artifacts" / "auto-think-candidates-dashboard.html"
    assert "Auto-think candidate dashboard" in rendered
    assert "Status" in rendered
    assert "1 candidate" in rendered
    assert "Auto-think queue for high-EV ideas" in rendered
    assert "EV 8/10" in rendered
    assert "new" in rendered
    assert "Approval required" in rendered
    assert "customer/vendor sends require AC approval" in rendered
    assert "Source / provenance" in rendered
    assert "source: https://x.com/gkisokay/status/2046171501888516188?s=46" in rendered
    assert "Generated: 2026-05-10T12:00:00+00:00" in rendered
    assert "https://cdn" not in rendered
    assert "<script" not in rendered.lower()


def test_dashboard_artifact_handles_empty_candidate_queue(tmp_path):
    output_path = auto_think.write_candidate_dashboard(
        hermes_home=tmp_path,
        generated_at="2026-05-10T12:00:00+00:00",
    )
    rendered = output_path.read_text(encoding="utf-8")

    assert output_path == tmp_path / "html_artifacts" / "auto-think-candidates-dashboard.html"
    assert "empty_queue" in rendered
    assert "No Auto-think candidates found." in rendered
    assert "Generated: 2026-05-10T12:00:00+00:00" in rendered
    assert "HERMES_HOME/auto_think/candidates.jsonl" in rendered
    assert "https://cdn" not in rendered
    assert "<script" not in rendered.lower()


def test_dashboard_artifact_rejects_paths_outside_html_artifacts(tmp_path):
    outside_path = tmp_path / "outside.html"

    with pytest.raises(ValueError, match="html_artifacts"):
        auto_think.write_candidate_dashboard(
            hermes_home=tmp_path,
            output_path=outside_path,
            generated_at="2026-05-10T12:00:00+00:00",
        )


def test_dashboard_command_generates_dashboard_and_prints_path_without_opening_by_default(
    tmp_path, capsys, monkeypatch
):
    payload = _candidate_payload()
    auto_think.enqueue_candidate(payload, hermes_home=tmp_path, dry_run=False)
    opened = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: opened.append((args, kwargs)))

    result = auto_think.dashboard_command(SimpleNamespace(hermes_home=tmp_path, open=False))
    output = capsys.readouterr().out

    assert result == tmp_path / "html_artifacts" / "auto-think-candidates-dashboard.html"
    assert str(result) in output
    assert "Artifact:" in output
    assert result.is_file()
    assert opened == []


def test_dashboard_command_opens_only_when_explicitly_requested_on_macos(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(auto_think.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: opened.append((args, kwargs)))

    result = auto_think.dashboard_command(SimpleNamespace(hermes_home=tmp_path, open=True))

    assert result == tmp_path / "html_artifacts" / "auto-think-candidates-dashboard.html"
    assert opened == [((["open", str(result)],), {"check": False})]
