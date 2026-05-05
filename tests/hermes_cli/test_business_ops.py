import json

import hermes_cli.business_ops as business_ops_module
from hermes_cli.business_ops import (
    BUSINESS_OS_WORKFLOW_IDS,
    ReadOnlyV11Client,
    build_business_ops_brief,
    build_business_ops_daily_report,
    build_business_ops_score_line,
    format_business_ops_daily_run,
    render_business_ops_daily_report,
    run_business_ops_daily_report,
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


def test_read_only_v11_client_loads_web_env_aliases(monkeypatch, tmp_path):
    for key in (
        "ODOO_URL",
        "ODOO_DB",
        "ODOO_USER",
        "ODOO_PASSWORD",
        "V11_WEB_URL",
        "V11_WEB_USER",
        "V11_WEB_PASS",
        "V11_DB_NAME",
    ):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        "\n".join([
            "V11_WEB_URL=https://v11.example.test",
            "V11_DB_NAME=advanced-test",
            "V11_WEB_USER=ops@example.test",
            "V11_WEB_PASS=secret-test",
        ]),
        encoding="utf-8",
    )

    client = ReadOnlyV11Client.from_env(hermes_home=tmp_path)

    assert client is not None
    assert client.url == "https://v11.example.test"
    assert client.db == "advanced-test"
    assert client.user == "ops@example.test"
    assert client.password == "secret-test"


def test_read_only_v11_client_falls_back_to_json_rpc(monkeypatch):
    def broken_xmlrpc():
        raise RuntimeError("broken XML parser")

    client = ReadOnlyV11Client(
        url="https://v11.example.test",
        db="advanced-test",
        user="ops@example.test",
        password="secret-test",
    )
    calls = []

    def fake_json_rpc(path, params):
        calls.append((path, params))
        if path == "/web/session/authenticate":
            return {"uid": 42}
        return [{"name": "PO1"}]

    monkeypatch.setattr(business_ops_module, "_xmlrpc_client_module", broken_xmlrpc)
    monkeypatch.setattr(client, "_json_rpc", fake_json_rpc)

    rows = client.search_read(
        "purchase.order",
        [["state", "=", "sent"]],
        fields=["name"],
        limit=1,
    )

    assert rows == [{"name": "PO1"}]
    assert client._transport == "json"
    assert calls[0][0] == "/web/session/authenticate"
    assert calls[1][0] == "/web/dataset/call_kw/purchase.order/search_read"


class FakeReadOnlyV11:
    def search_read(self, model, domain, *, fields, limit=50, order=""):
        del fields, limit, order
        if model == "account.invoice":
            invoice_type = ""
            for item in domain:
                if isinstance(item, list) and item[:2] == ["type", "="]:
                    invoice_type = str(item[2])
            if invoice_type == "out_invoice":
                return [{
                    "number": "INV/1",
                    "partner_id": [1, "Aero Accessories"],
                    "date_due": "2026-01-01",
                    "amount_total": 1000.0,
                    "residual": 1000.0,
                    "state": "open",
                    "type": "out_invoice",
                }]
            if invoice_type == "in_invoice":
                return [{
                    "number": "BILL/1",
                    "partner_id": [2, "Vendor Co"],
                    "date_due": "2026-01-02",
                    "amount_total": 500.0,
                    "residual": 500.0,
                    "state": "open",
                    "type": "in_invoice",
                }]
        if model == "purchase.order":
            return [{
                "name": "PO1",
                "partner_id": [2, "Vendor Co"],
                "date_order": "2026-01-03",
                "amount_total": 500.0,
                "state": "sent",
            }]
        if model == "repair.order":
            return [{
                "name": "RO1",
                "partner_id": [1, "Aero Accessories"],
                "product_id": [3, "5909891"],
                "state": "under_repair",
                "create_date": "2026-01-04",
            }]
        if model == "stock.quant":
            return [{
                "product_id": [4, "743502 PUMP LINER"],
                "location_id": [5, "WH/Stock"],
                "lot_id": [6, "LOT-1"],
                "quantity": 2.0,
            }]
        return []


class FakePurchaseRepairV11(FakeReadOnlyV11):
    def search_read(self, model, domain, *, fields, limit=50, order=""):
        if model == "repair.order":
            raise RuntimeError("missing repair.order")
        if model == "purchase.order" and any(
            isinstance(item, list) and item[:2] == ["repair_order_type", "!="]
            for item in domain
        ):
            return [{
                "name": "PO-REPAIR-1",
                "partner_id": [2, "Repair Vendor"],
                "repair_order_type": [8, "Bench Repair"],
                "date_order": "2026-01-06",
                "amount_total": 750.0,
                "state": "purchase",
            }]
        return super().search_read(model, domain, fields=fields, limit=limit, order=order)


def test_business_ops_daily_report_collects_and_renders_read_only_lanes(tmp_path):
    report = build_business_ops_daily_report(
        hermes_home=tmp_path,
        v11_client=FakeReadOnlyV11(),
        collect_live=False,
    )
    markdown = render_business_ops_daily_report(report)

    assert report["mode"] == "read_only"
    assert report["summary"]["available_lanes"] == 4
    assert {lane["id"] for lane in report["lanes"]} == {
        "finance-admin",
        "purchasing",
        "repairs",
        "inventory",
    }
    assert "Mode: read-only" in markdown
    assert "No customer sends" in markdown
    assert "743502 PUMP LINER" in markdown


def test_business_ops_daily_report_falls_back_to_repair_purchase_orders(tmp_path):
    report = build_business_ops_daily_report(
        hermes_home=tmp_path,
        v11_client=FakePurchaseRepairV11(),
        collect_live=False,
    )
    repair_lane = next(lane for lane in report["lanes"] if lane["id"] == "repairs")

    assert repair_lane["status"] == "warn"
    assert repair_lane["source"] == "V11 purchase.order repair_order_type search_read"
    assert "PO-REPAIR-1" in "\n".join(repair_lane["items"])


def test_business_ops_daily_run_writes_report_and_workspace_evidence(tmp_path):
    workspace = WorkspaceStore(tmp_path / "workspace" / "control_plane.json")

    result = run_business_ops_daily_report(
        hermes_home=tmp_path,
        v11_client=FakeReadOnlyV11(),
        collect_live=False,
        workspace_store=workspace,
        source="unit-test",
    )
    response = format_business_ops_daily_run(result)
    paths = result["paths"]
    data = workspace.read()

    assert "Business OS daily report created" in response
    assert "No customer sends" in response
    assert paths["markdown"].endswith(".md")
    assert paths["json"].endswith(".json")
    assert (tmp_path / "business_ops" / "reports" / "latest.md").is_file()
    assert (tmp_path / "business_ops" / "reports" / "latest.json").is_file()
    assert len(data["tasks"]) == 1
    assert len(data["evidence"]) == 1
    assert next(iter(data["tasks"].values()))["status"] == "done"
