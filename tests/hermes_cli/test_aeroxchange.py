from __future__ import annotations

from hermes_cli.aeroxchange import (
    AEROXCHANGE_WORKFLOW_ID,
    build_aeroxchange_browser_readiness,
    build_aeroxchange_draft_package,
    format_aeroxchange_draft_run,
    parse_aeroxchange_rfq_text,
    render_aeroxchange_draft_package,
    run_aeroxchange_draft_from_snapshot,
)
from hermes_cli.commands import resolve_command
from hermes_cli.workspace import WorkspaceStore
from hermes_cli.workflows import WorkflowRegistry


SNAPSHOT = """
RFQ # AX-1001
Buyer: Aero Accessories
Part Number: 5909891
Description: IDG accessory rotor
Qty: 1
Condition: AR
Due Date: 2026-05-08
Notes: quote from queue

RFQ # AX-1002
Customer: Advanced Aerospace Components
PN: 743502
Description: Pump liner
Quantity: 2
Cond: SV
Response Due: 2026-05-09
"""


def test_parse_aeroxchange_snapshot_extracts_multiple_rfqs():
    rfqs = parse_aeroxchange_rfq_text(SNAPSHOT)

    assert len(rfqs) == 2
    assert rfqs[0]["rfq_id"] == "AX-1001"
    assert rfqs[0]["customer"] == "Aero Accessories"
    assert rfqs[0]["part_number"] == "5909891"
    assert rfqs[0]["quantity"] == "1"
    assert rfqs[0]["condition"] == "AR"
    assert rfqs[0]["missing_fields"] == []
    assert rfqs[1]["part_number"] == "743502"
    assert rfqs[1]["condition"] == "SV"


def test_aeroxchange_package_is_draft_only_and_blocks_submit():
    package = build_aeroxchange_draft_package(SNAPSHOT, source_url="https://www.aeroxchange.com/rfq")
    markdown = render_aeroxchange_draft_package(package)

    assert package["workflow_id"] == AEROXCHANGE_WORKFLOW_ID
    assert package["mode"] == "draft_only"
    assert package["approval_required"]["before_submit"] is True
    assert package["summary"]["rfq_count"] == 2
    assert not any(
        action.get("action") in {"submit", "send", "award", "decline"}
        for action in package["browser_actions"]
    )
    assert "No Aeroxchange submit" in markdown
    assert "5909891" in markdown
    assert "743502" in markdown


def test_aeroxchange_run_writes_reports_and_workspace_evidence(tmp_path):
    workspace = WorkspaceStore(tmp_path / "workspace" / "control_plane.json")

    result = run_aeroxchange_draft_from_snapshot(
        SNAPSHOT,
        hermes_home=tmp_path,
        workspace_store=workspace,
        source="unit-test",
    )
    response = format_aeroxchange_draft_run(result)
    data = workspace.read()

    assert "Aeroxchange draft package created" in response
    assert "No Aeroxchange submit/send" in response
    assert (tmp_path / "aeroxchange" / "reports" / "latest.md").is_file()
    assert (tmp_path / "aeroxchange" / "reports" / "latest.json").is_file()
    assert len(data["tasks"]) == 1
    assert len(data["evidence"]) == 1
    assert next(iter(data["tasks"].values()))["status"] == "done"


def test_aeroxchange_command_and_workflow_are_registered(tmp_path):
    readiness = build_aeroxchange_browser_readiness()
    command = resolve_command("aeroxchange")
    registry = WorkflowRegistry(tmp_path / "workflows.json")

    assert readiness["status"] == "draft_ready"
    assert command is not None
    assert command.name == "aero"
    assert command.gateway_only is True
    assert registry.has_workflow(AEROXCHANGE_WORKFLOW_ID)
