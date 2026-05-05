from __future__ import annotations

import json


def test_workflow_registry_seeds_kill_resume_and_report(tmp_path):
    from hermes_cli.workflows import WorkflowRegistry, format_workflow_status

    store = WorkflowRegistry(tmp_path / "workflow_registry.json")
    workflows = {workflow["id"]: workflow for workflow in store.list_workflows()}

    assert "rfq-intake" in workflows
    assert workflows["rfq-intake"]["kill_switch_env"] == "HERMES_RFQ_GMAIL_ENABLED"

    killed = store.kill_workflow("rfq", actor="unit", reason="stop noisy intake")
    assert killed["id"] == "rfq-intake"
    assert killed["status"] == "disabled"
    assert store.is_enabled("rfq-intake") is False

    resumed = store.resume_workflow("rfq-intake", actor="unit", reason="verified")
    assert resumed["status"] == "enabled"
    assert store.is_enabled("rfq-intake") is True

    report = store.create_report(title="Unit Workflow Report")
    assert report["path"].endswith(".md")
    assert "rfq-intake" in format_workflow_status(store.read())

    data = json.loads((tmp_path / "workflow_registry.json").read_text(encoding="utf-8"))
    actions = {event["action"] for event in data["events"].values()}
    assert {"kill", "resume"}.issubset(actions)


def test_workflow_tool_controls_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_WORKFLOW_STORE", str(tmp_path / "registry.json"))

    from tools.workflow_tool import workflow_tool

    killed = json.loads(workflow_tool("kill", workflow_id="rfq-intake", actor="unit"))
    assert killed["success"] is True
    assert killed["workflow"]["status"] == "disabled"

    enabled = json.loads(workflow_tool("is_enabled", workflow_id="rfq-intake"))
    assert enabled["enabled"] is False

    resumed = json.loads(workflow_tool("resume", workflow_id="rfq-intake", actor="unit"))
    assert resumed["success"] is True
    assert resumed["workflow"]["status"] == "enabled"
