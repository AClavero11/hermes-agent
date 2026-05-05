import json

from hermes_cli.business_ops import (
    BUSINESS_OS_WORKFLOW_IDS,
    build_business_ops_brief,
    build_business_ops_score_line,
)
from hermes_cli.workspace import WorkspaceStore
from hermes_cli.workflows import WorkflowRegistry


def test_business_ops_brief_covers_non_rfq_lanes(tmp_path):
    latest_dir = tmp_path / "canary" / "reports"
    latest_dir.mkdir(parents=True)
    (latest_dir / "latest.json").write_text(
        json.dumps({
            "status": "warn",
            "percent": 100.0,
            "overall_quality": {"score": 8.8},
            "readiness": {"status": "not_frontier_ready"},
        }),
        encoding="utf-8",
    )
    workspace = WorkspaceStore(tmp_path / "workspace" / "control_plane.json")
    workspace.create_task(
        "Stuck repair blocker review",
        status="blocked",
        next_action="Find missing cert package.",
    )
    registry = WorkflowRegistry(tmp_path / "workflows" / "registry.json")

    brief = build_business_ops_brief(
        hermes_home=tmp_path,
        workspace_store=workspace,
        workflow_registry=registry,
        include_kanban=False,
    )

    assert "Hermes Business OS Brief" in brief
    assert "Score: 8.8/10" in brief
    assert "Workflow coverage:" in brief
    assert "finance-admin" in brief
    assert "purchasing" in brief
    assert "repairs" in brief
    assert "inventory" in brief
    assert "Stuck repair blocker review" in brief
    assert "approval-gated" in brief
    assert all(workflow_id in {item["id"] for item in registry.list_workflows()} for workflow_id in BUSINESS_OS_WORKFLOW_IDS)


def test_business_ops_score_line_handles_missing_canary(tmp_path):
    assert build_business_ops_score_line(hermes_home=tmp_path) == "Score: latest canary unavailable"
