"""AAC business-ops brief for Hermes operator mode."""

from __future__ import annotations

import json
import os
import time
from datetime import date
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home


BUSINESS_OS_WORKFLOW_IDS: tuple[str, ...] = (
    "finance-admin-daily-brief",
    "purchasing-vendor-followup",
    "repair-stuck-units",
    "inventory-hot-parts-review",
)

REPORT_LANE_TITLES: dict[str, str] = {
    "finance-admin": "Finance/Admin",
    "purchasing": "Purchasing",
    "repairs": "Repairs",
    "inventory": "Inventory",
}

READ_ONLY_GUARDRAILS: tuple[str, ...] = (
    "V11 access is search_read only.",
    "No customer sends, vendor sends, V11 writes, Atlas writes, or bank actions are executed.",
    "Any external action still requires operator approval.",
)

BUSINESS_OS_LANES: tuple[dict[str, str], ...] = (
    {
        "id": "finance-admin",
        "owner": "finance-admin",
        "proof": "cash, receivables, payables, deposits, invoices",
        "next_action": "daily cash/AR/AP brief with V11/bank evidence",
    },
    {
        "id": "purchasing",
        "owner": "operator",
        "proof": "open POs, vendor quotes, late vendor replies",
        "next_action": "vendor follow-up queue with stale-age sorting",
    },
    {
        "id": "repairs",
        "owner": "operator",
        "proof": "stuck repairs, teardown blockers, cert/document gaps",
        "next_action": "blocked repair digest with owner and unblock step",
    },
    {
        "id": "inventory",
        "owner": "sales-rfq",
        "proof": "hot stock, no-sale inventory, repricing candidates",
        "next_action": "hot-parts and stale-stock review outside RFQ intake",
    },
)


def _load_latest_canary(hermes_home: Path) -> dict[str, Any]:
    path = hermes_home / "canary" / "reports" / "latest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"path": str(path), "available": False}
    if not isinstance(payload, dict):
        return {"path": str(path), "available": False}
    payload["path"] = str(path)
    payload["available"] = True
    return payload


def _score_summary(latest: dict[str, Any]) -> str:
    if not latest.get("available"):
        return "Score: latest canary unavailable"
    quality = latest.get("overall_quality") if isinstance(latest.get("overall_quality"), dict) else {}
    readiness = latest.get("readiness") if isinstance(latest.get("readiness"), dict) else {}
    quality_score = quality.get("score")
    percent = latest.get("percent")
    status = str(latest.get("status") or "unknown").upper()
    readiness_status = str(readiness.get("status") or "unknown")
    parts = [f"Score: {quality_score}/10" if quality_score is not None else "Score: unknown"]
    if percent is not None:
        parts.append(f"coverage {float(percent):.1f}%")
    parts.append(f"status {status}")
    parts.append(f"readiness {readiness_status}")
    return " | ".join(parts)


def _workspace_summary(store: Any) -> tuple[str, dict[str, int], list[dict[str, Any]]]:
    try:
        from hermes_cli.workspace import ACTIVE_TASK_STATUSES, summarize_counts

        data = store.read()
        counts = summarize_counts(data)
        active = [
            task for task in data.get("tasks", {}).values()
            if task.get("status") in ACTIVE_TASK_STATUSES
        ]
        active.sort(key=lambda item: float(item.get("updated_at") or 0), reverse=True)
    except Exception as exc:
        return f"Workspace: unavailable ({type(exc).__name__})", {}, []
    line = (
        f"Workspace: {counts.get('active', 0)} active / {counts.get('tasks', 0)} total "
        f"| blocked {counts.get('blocked', 0)} | evidence {counts.get('evidence', 0)}"
    )
    return line, counts, active[:5]


def _workflow_summary(registry: Any) -> tuple[str, list[dict[str, Any]], list[str]]:
    try:
        workflows = registry.list_workflows(include_disabled=True, limit=100)
    except Exception as exc:
        return f"Workflow coverage: unavailable ({type(exc).__name__})", [], list(BUSINESS_OS_WORKFLOW_IDS)
    workflow_ids = {str(item.get("id") or "") for item in workflows}
    missing = [workflow_id for workflow_id in BUSINESS_OS_WORKFLOW_IDS if workflow_id not in workflow_ids]
    enabled = [item for item in workflows if item.get("status") == "enabled"]
    disabled = [item for item in workflows if item.get("status") == "disabled"]
    line = (
        f"Workflow coverage: {len(workflows)} registered | {len(enabled)} enabled "
        f"| {len(disabled)} disabled | missing non-RFQ lanes {len(missing)}"
    )
    return line, workflows, missing


def _kanban_summary(limit: int = 5) -> str:
    try:
        from hermes_cli import kanban_db as kb

        with kb.connect() as conn:
            kb.recompute_ready(conn)
            brief = kb.chief_of_staff_brief(conn, limit=limit)
        stats = brief.get("stats") or {}
        by_status = stats.get("by_status") or {}
        return (
            "Kanban: "
            + ", ".join(
                f"{status}={by_status.get(status, 0)}"
                for status in ("blocked", "running", "ready", "triage", "todo")
            )
        )
    except Exception as exc:
        return f"Kanban: unavailable ({type(exc).__name__})"


def build_business_ops_brief(
    *,
    hermes_home: Path | None = None,
    workspace_store: Any | None = None,
    workflow_registry: Any | None = None,
    include_kanban: bool = True,
) -> str:
    """Build a concise global operating brief without customer sends or writes."""
    resolved_home = Path(hermes_home) if hermes_home is not None else get_hermes_home()
    if workspace_store is None:
        from hermes_cli.workspace import WorkspaceStore

        workspace_store = WorkspaceStore(resolved_home / "workspace" / "control_plane.json")
    if workflow_registry is None:
        from hermes_cli.workflows import WorkflowRegistry

        workflow_registry = WorkflowRegistry(resolved_home / "workflows" / "registry.json")

    latest = _load_latest_canary(resolved_home)
    workspace_line, _counts, active_tasks = _workspace_summary(workspace_store)
    workflow_line, workflows, missing_workflows = _workflow_summary(workflow_registry)
    workflow_lookup = {str(item.get("id") or ""): item for item in workflows}

    lines = [
        "Hermes Business OS Brief",
        _score_summary(latest),
        workspace_line,
        workflow_line,
    ]
    if include_kanban:
        lines.append(_kanban_summary())

    lines.extend(["", "Catch-up lanes:"])
    for lane in BUSINESS_OS_LANES:
        workflow = workflow_lookup.get(f"{lane['id']}-daily-brief") or workflow_lookup.get(
            {
                "finance-admin": "finance-admin-daily-brief",
                "purchasing": "purchasing-vendor-followup",
                "repairs": "repair-stuck-units",
                "inventory": "inventory-hot-parts-review",
            }[lane["id"]]
        )
        status = str((workflow or {}).get("status") or "missing")
        lines.append(
            f"- {lane['id']}: {status} | owner {lane['owner']} | next {lane['next_action']}"
        )

    lines.extend(["", "Blocked/active work:"])
    if active_tasks:
        for task in active_tasks:
            title = str(task.get("title") or "").strip()
            status = str(task.get("status") or "open")
            next_action = str(task.get("next_action") or "").strip()
            suffix = f" | next {next_action}" if next_action else ""
            lines.append(f"- {status}: {title}{suffix}")
    else:
        lines.append("- None in Workspace.")

    lines.extend(["", "Memory/V11 grounding:"])
    lines.append("- Routes finance/admin, purchasing, repairs, inventory, V11, Hermes, and customer work to Alexandria/V11 context.")
    lines.append("- Customer sends, V11 writes, Atlas writes, and external messages remain approval-gated.")
    if missing_workflows:
        lines.append("")
        lines.append("Missing workflow registrations: " + ", ".join(missing_workflows))
    return "\n".join(lines)


def build_business_ops_score_line(*, hermes_home: Path | None = None) -> str:
    """Return the latest global Hermes score line."""
    resolved_home = Path(hermes_home) if hermes_home is not None else get_hermes_home()
    return _score_summary(_load_latest_canary(resolved_home))


def _load_dotenv_for_v11(hermes_home: Path) -> None:
    try:
        from hermes_cli.env_loader import load_hermes_dotenv

        load_hermes_dotenv(hermes_home=hermes_home)
    except Exception:
        pass
    _load_v11_fallback_env_files(hermes_home)


def _load_v11_fallback_env_files(hermes_home: Path) -> None:
    alias_groups = (
        {"ODOO_URL", "V11_WEB_URL", "V11_URL"},
        {"ODOO_DB", "V11_DB", "V11_DB_NAME"},
        {"ODOO_USER", "V11_WEB_USER", "V11_USER"},
        {"ODOO_PASSWORD", "V11_WEB_PASS", "ODOO_API_KEY"},
    )
    allowed_keys = set().union(*alias_groups)
    key_to_group = {
        key: group
        for group in alias_groups
        for key in group
    }
    candidates = [
        hermes_home / ".env",
        Path.home() / ".hermes" / ".env",
        Path.home() / ".secrets" / "alexandria.env",
    ]
    seen: set[Path] = set()
    for path in candidates:
        path = path.expanduser()
        if path in seen:
            continue
        seen.add(path)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            group = key_to_group.get(key)
            group_loaded = bool(group and any(os.getenv(alias, "").strip() for alias in group))
            if key in allowed_keys and value and key not in os.environ and not group_loaded:
                os.environ[key] = value


def _env_value(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


def _xmlrpc_client_module() -> Any:
    try:
        import xmlrpc.client as xmlrpc_client
    except Exception as exc:
        raise RuntimeError(f"XML-RPC client unavailable: {exc}") from exc
    return xmlrpc_client


class ReadOnlyV11Client:
    """Small XML-RPC client that exposes only search_read."""

    def __init__(self, *, url: str, db: str, user: str, password: str):
        self.url = url.rstrip("/")
        self.db = db
        self.user = user
        self.password = password
        self._uid: int | None = None
        self._transport = ""
        self._models: Any | None = None
        self._json_opener: Any | None = None

    @classmethod
    def from_env(cls, *, hermes_home: Path) -> "ReadOnlyV11Client | None":
        _load_dotenv_for_v11(hermes_home)
        password = _env_value("ODOO_PASSWORD", "V11_WEB_PASS", "ODOO_API_KEY")
        if not password:
            return None
        return cls(
            url=_env_value("ODOO_URL", "V11_WEB_URL", "V11_URL", default="https://v11.advanced.aero"),
            db=_env_value("ODOO_DB", "V11_DB", "V11_DB_NAME", default="advancedaero"),
            user=_env_value("ODOO_USER", "V11_WEB_USER", "V11_USER", default="ac@advanced.aero"),
            password=password,
        )

    def authenticate(self) -> int:
        if self._uid is not None:
            return self._uid
        if self._transport == "json":
            return self._authenticate_json()
        try:
            return self._authenticate_xmlrpc()
        except Exception as xmlrpc_exc:
            try:
                return self._authenticate_json()
            except Exception as json_exc:
                raise RuntimeError(
                    f"V11 authentication failed via XML-RPC ({xmlrpc_exc}) "
                    f"and JSON-RPC ({json_exc})"
                ) from json_exc

    def _authenticate_xmlrpc(self) -> int:
        xmlrpc_client = _xmlrpc_client_module()
        common = xmlrpc_client.ServerProxy(f"{self.url}/xmlrpc/2/common", allow_none=True)
        uid = common.authenticate(self.db, self.user, self.password, {})
        if not uid:
            raise RuntimeError("V11 authentication failed")
        self._uid = int(uid)
        self._transport = "xmlrpc"
        return self._uid

    def _json_rpc(self, path: str, params: dict[str, Any]) -> Any:
        import http.cookiejar
        import urllib.request

        if self._json_opener is None:
            cookie_jar = http.cookiejar.CookieJar()
            self._json_opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(cookie_jar)
            )
        payload = json.dumps({
            "jsonrpc": "2.0",
            "method": "call",
            "params": params,
            "id": int(time.time() * 1000),
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self.url}{path}",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self._json_opener.open(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("invalid JSON-RPC response")
        if data.get("error"):
            error = data.get("error")
            if isinstance(error, dict):
                message = error.get("message") or error.get("data") or error
            else:
                message = error
            raise RuntimeError(f"JSON-RPC error: {message}")
        return data.get("result")

    def _authenticate_json(self) -> int:
        result = self._json_rpc(
            "/web/session/authenticate",
            {"db": self.db, "login": self.user, "password": self.password},
        )
        if not isinstance(result, dict) or not result.get("uid"):
            raise RuntimeError("V11 JSON-RPC authentication failed")
        self._uid = int(result["uid"])
        self._transport = "json"
        return self._uid

    def _json_search_read(
        self,
        model: str,
        domain: list[Any],
        *,
        kwargs: dict[str, Any],
    ) -> list[dict[str, Any]]:
        result = self._json_rpc(
            f"/web/dataset/call_kw/{model}/search_read",
            {
                "model": model,
                "method": "search_read",
                "args": [domain],
                "kwargs": kwargs,
            },
        )
        return result if isinstance(result, list) else []

    def search_read(
        self,
        model: str,
        domain: list[Any],
        *,
        fields: list[str],
        limit: int = 50,
        order: str = "",
    ) -> list[dict[str, Any]]:
        uid = self.authenticate()
        kwargs: dict[str, Any] = {"fields": fields, "limit": limit}
        if order:
            kwargs["order"] = order
        if self._transport == "json":
            return self._json_search_read(model, domain, kwargs=kwargs)
        if self._models is None:
            xmlrpc_client = _xmlrpc_client_module()
            self._models = xmlrpc_client.ServerProxy(f"{self.url}/xmlrpc/2/object", allow_none=True)
        rows = self._models.execute_kw(
            self.db,
            uid,
            self.password,
            model,
            "search_read",
            [domain],
            kwargs,
        )
        return rows if isinstance(rows, list) else []


def _now_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _now_display() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %Z")


def _money(value: Any) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0.0
    return f"${number:,.2f}"


def _m2o_name(value: Any) -> str:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return str(value[1] or "").strip()
    if value in (False, None):
        return ""
    return str(value).strip()


def _date_value(value: Any) -> str:
    return str(value or "").split(" ", 1)[0]


def _is_overdue(value: Any) -> bool:
    due_date = _date_value(value)
    if not due_date:
        return False
    return due_date < date.today().isoformat()


def _lane_unavailable(lane_id: str, reason: str) -> dict[str, Any]:
    return {
        "id": lane_id,
        "title": REPORT_LANE_TITLES[lane_id],
        "status": "unavailable",
        "source": "V11 search_read",
        "summary": reason,
        "items": [],
        "errors": [reason],
    }


def _lane_ok(
    lane_id: str,
    *,
    summary: str,
    items: list[str],
    source: str,
    status: str = "ok",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": lane_id,
        "title": REPORT_LANE_TITLES[lane_id],
        "status": status,
        "source": source,
        "summary": summary,
        "items": items,
        "errors": [],
        "details": details or {},
    }


def _safe_lane(lane_id: str, fn: Any) -> dict[str, Any]:
    try:
        return fn()
    except Exception as exc:
        return _lane_unavailable(lane_id, f"{type(exc).__name__}: {exc}")


def _collect_finance_admin(client: Any, *, limit: int) -> dict[str, Any]:
    fields = ["number", "partner_id", "date_due", "amount_total", "residual", "state", "type"]
    open_ar = client.search_read(
        "account.invoice",
        [["type", "=", "out_invoice"], ["state", "=", "open"]],
        fields=fields,
        limit=limit,
        order="date_due asc, date_invoice asc, id asc",
    )
    open_ap = client.search_read(
        "account.invoice",
        [["type", "=", "in_invoice"], ["state", "=", "open"]],
        fields=fields,
        limit=limit,
        order="date_due asc, date_invoice asc, id asc",
    )
    ar_total = sum(float(row.get("residual") or 0) for row in open_ar)
    ap_total = sum(float(row.get("residual") or 0) for row in open_ap)
    ar_overdue = sum(1 for row in open_ar if _is_overdue(row.get("date_due")))
    ap_overdue = sum(1 for row in open_ap if _is_overdue(row.get("date_due")))
    items: list[str] = []
    for row in open_ar[:5]:
        items.append(
            "AR "
            f"{row.get('number') or 'unnumbered'} | {_m2o_name(row.get('partner_id')) or 'unknown'} "
            f"| due {_date_value(row.get('date_due')) or 'missing'} | {_money(row.get('residual'))}"
        )
    for row in open_ap[:5]:
        items.append(
            "AP "
            f"{row.get('number') or 'unnumbered'} | {_m2o_name(row.get('partner_id')) or 'unknown'} "
            f"| due {_date_value(row.get('date_due')) or 'missing'} | {_money(row.get('residual'))}"
        )
    return _lane_ok(
        "finance-admin",
        source="V11 account.invoice search_read",
        summary=(
            f"Open AR {len(open_ar)} ({_money(ar_total)}, overdue {ar_overdue}); "
            f"open AP {len(open_ap)} ({_money(ap_total)}, overdue {ap_overdue})."
        ),
        items=items,
        status="warn" if ar_overdue or ap_overdue else "ok",
        details={
            "open_ar_count": len(open_ar),
            "open_ap_count": len(open_ap),
            "open_ar_total": ar_total,
            "open_ap_total": ap_total,
            "open_ar_overdue": ar_overdue,
            "open_ap_overdue": ap_overdue,
        },
    )


def _collect_purchasing(client: Any, *, limit: int) -> dict[str, Any]:
    rows = client.search_read(
        "purchase.order",
        [["state", "in", ["draft", "sent", "to approve", "purchase"]]],
        fields=["name", "partner_id", "date_order", "amount_total", "state"],
        limit=limit,
        order="date_order asc, id asc",
    )
    late_or_open = [row for row in rows if row.get("state") in {"draft", "sent", "to approve"}]
    items = [
        (
            f"{row.get('name') or 'PO'} | {_m2o_name(row.get('partner_id')) or 'unknown vendor'} "
            f"| {row.get('state') or 'unknown'} | {_date_value(row.get('date_order')) or 'missing date'} "
            f"| {_money(row.get('amount_total'))}"
        )
        for row in rows[:10]
    ]
    return _lane_ok(
        "purchasing",
        source="V11 purchase.order search_read",
        summary=f"Open purchase orders {len(rows)}; draft/sent/to-approve queue {len(late_or_open)}.",
        items=items,
        status="warn" if late_or_open else "ok",
        details={"open_purchase_orders": len(rows), "needs_followup": len(late_or_open)},
    )


def _collect_repairs(client: Any, *, limit: int) -> dict[str, Any]:
    try:
        rows = client.search_read(
            "repair.order",
            [["state", "not in", ["done", "cancel"]]],
            fields=["name", "partner_id", "product_id", "state", "create_date"],
            limit=limit,
            order="create_date asc, id asc",
        )
    except Exception:
        return _collect_purchase_repair_orders(client, limit=limit)
    items = [
        (
            f"{row.get('name') or 'repair'} | {_m2o_name(row.get('product_id')) or 'unknown part'} "
            f"| {_m2o_name(row.get('partner_id')) or 'unknown customer'} "
            f"| {row.get('state') or 'unknown'} | opened {_date_value(row.get('create_date')) or 'unknown'}"
        )
        for row in rows[:10]
    ]
    return _lane_ok(
        "repairs",
        source="V11 repair.order search_read",
        summary=f"Open repair orders {len(rows)} for stuck-unit review.",
        items=items,
        status="warn" if rows else "ok",
        details={"open_repairs": len(rows)},
    )


def _collect_purchase_repair_orders(client: Any, *, limit: int) -> dict[str, Any]:
    rows = client.search_read(
        "purchase.order",
        [["repair_order_type", "!=", False], ["state", "not in", ["done", "cancel"]]],
        fields=["name", "partner_id", "repair_order_type", "date_order", "amount_total", "state"],
        limit=limit,
        order="date_order asc, id asc",
    )
    items = [
        (
            f"{row.get('name') or 'repair PO'} | {_m2o_name(row.get('repair_order_type')) or 'repair'} "
            f"| {_m2o_name(row.get('partner_id')) or 'unknown vendor'} "
            f"| {row.get('state') or 'unknown'} | {_date_value(row.get('date_order')) or 'unknown'} "
            f"| {_money(row.get('amount_total'))}"
        )
        for row in rows[:10]
    ]
    return _lane_ok(
        "repairs",
        source="V11 purchase.order repair_order_type search_read",
        summary=f"Open repair-scope purchase orders {len(rows)} for stuck-unit/vendor review.",
        items=items,
        status="warn" if rows else "ok",
        details={"open_repair_purchase_orders": len(rows)},
    )


def _collect_inventory(client: Any, *, limit: int) -> dict[str, Any]:
    rows = client.search_read(
        "stock.quant",
        [["quantity", ">", 0], ["location_id.usage", "=", "internal"]],
        fields=["product_id", "location_id", "lot_id", "quantity"],
        limit=limit,
        order="quantity desc, id asc",
    )
    items = [
        (
            f"{_m2o_name(row.get('product_id')) or 'unknown part'} "
            f"| qty {row.get('quantity') or 0} | {_m2o_name(row.get('location_id')) or 'unknown location'} "
            f"| lot {_m2o_name(row.get('lot_id')) or 'none'}"
        )
        for row in rows[:10]
    ]
    return _lane_ok(
        "inventory",
        source="V11 stock.quant search_read",
        summary=f"Internal stock quants with quantity on hand: {len(rows)} shown for hot/stale review.",
        items=items,
        status="ok",
        details={"positive_internal_quants": len(rows)},
    )


def _collect_business_ops_lanes(client: Any | None, *, limit: int) -> list[dict[str, Any]]:
    if client is None:
        reason = "V11 credentials unavailable; no live business data collected."
        return [_lane_unavailable(lane["id"], reason) for lane in BUSINESS_OS_LANES]
    return [
        _safe_lane("finance-admin", lambda: _collect_finance_admin(client, limit=limit)),
        _safe_lane("purchasing", lambda: _collect_purchasing(client, limit=limit)),
        _safe_lane("repairs", lambda: _collect_repairs(client, limit=limit)),
        _safe_lane("inventory", lambda: _collect_inventory(client, limit=limit)),
    ]


def _daily_report_summary(lanes: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    for lane in lanes:
        status = str(lane.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    available = len([lane for lane in lanes if lane.get("status") != "unavailable"])
    warnings = len([lane for lane in lanes if lane.get("status") == "warn"])
    item_count = sum(len(lane.get("items") or []) for lane in lanes)
    return {
        "lanes": len(lanes),
        "available_lanes": available,
        "warning_lanes": warnings,
        "item_count": item_count,
        "status_counts": status_counts,
        "approval_boundary": "external actions and V11/Atlas writes require operator approval",
    }


def build_business_ops_daily_report(
    *,
    hermes_home: Path | None = None,
    v11_client: Any | None = None,
    collect_live: bool = True,
    limit: int = 40,
) -> dict[str, Any]:
    """Collect the read-only AAC Business OS daily report."""
    resolved_home = Path(hermes_home) if hermes_home is not None else get_hermes_home()
    try:
        resolved_limit = int(limit)
    except (TypeError, ValueError):
        resolved_limit = 40
    client = v11_client
    if client is None and collect_live:
        client = ReadOnlyV11Client.from_env(hermes_home=resolved_home)
    lanes = _collect_business_ops_lanes(client, limit=max(1, min(resolved_limit, 100)))
    return {
        "title": "Hermes Business OS Daily Report",
        "generated_at": _now_display(),
        "mode": "read_only",
        "hermes_home": str(resolved_home),
        "lanes": lanes,
        "summary": _daily_report_summary(lanes),
        "guardrails": list(READ_ONLY_GUARDRAILS),
    }


def render_business_ops_daily_report(report: dict[str, Any]) -> str:
    """Render a Business OS daily report as Markdown."""
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    lines = [
        f"# {report.get('title') or 'Hermes Business OS Daily Report'}",
        "",
        f"Generated: {report.get('generated_at') or ''}",
        "Mode: read-only",
        "",
        "## Summary",
        "",
        f"- Lanes available: {summary.get('available_lanes', 0)}/{summary.get('lanes', 0)}",
        f"- Warning lanes: {summary.get('warning_lanes', 0)}",
        f"- Evidence items surfaced: {summary.get('item_count', 0)}",
        f"- Approval boundary: {summary.get('approval_boundary', '')}",
        "",
        "## Guardrails",
        "",
    ]
    for guardrail in report.get("guardrails") or READ_ONLY_GUARDRAILS:
        lines.append(f"- {guardrail}")
    for lane in report.get("lanes") or []:
        title = lane.get("title") or lane.get("id") or "Lane"
        lines.extend([
            "",
            f"## {title}",
            "",
            f"Status: {lane.get('status') or 'unknown'}",
            f"Source: {lane.get('source') or 'unknown'}",
            "",
            str(lane.get("summary") or "No summary."),
            "",
        ])
        items = lane.get("items") or []
        if items:
            lines.append("Top items:")
            for item in items:
                lines.append(f"- {item}")
        else:
            lines.append("- No items surfaced.")
        errors = lane.get("errors") or []
        if errors:
            lines.append("")
            lines.append("Errors:")
            for error in errors:
                lines.append(f"- {error}")
    return "\n".join(lines).rstrip() + "\n"


def write_business_ops_daily_report(
    report: dict[str, Any],
    *,
    hermes_home: Path | None = None,
) -> dict[str, str]:
    """Write Markdown and JSON copies of the report under HERMES_HOME."""
    resolved_home = Path(hermes_home or report.get("hermes_home") or get_hermes_home()).expanduser()
    reports_dir = resolved_home / "business_ops" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = _now_stamp()
    markdown_path = reports_dir / f"business-ops-daily-{stamp}.md"
    json_path = reports_dir / f"business-ops-daily-{stamp}.json"
    markdown = render_business_ops_daily_report(report)
    markdown_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    (reports_dir / "latest.md").write_text(markdown, encoding="utf-8")
    (reports_dir / "latest.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return {"markdown": str(markdown_path), "json": str(json_path)}


def _workspace_report_summary(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    return (
        f"{summary.get('available_lanes', 0)}/{summary.get('lanes', 0)} lanes available; "
        f"{summary.get('warning_lanes', 0)} warning lanes; "
        f"{summary.get('item_count', 0)} evidence items surfaced."
    )


def record_business_ops_daily_report(
    report: dict[str, Any],
    *,
    paths: dict[str, str],
    hermes_home: Path | None = None,
    workspace_store: Any | None = None,
    source: str = "ops-daily-report",
) -> dict[str, Any]:
    """Record the report path as local Workspace evidence."""
    resolved_home = Path(hermes_home or report.get("hermes_home") or get_hermes_home()).expanduser()
    if workspace_store is None:
        from hermes_cli.workspace import WorkspaceStore

        workspace_store = WorkspaceStore(resolved_home / "workspace" / "control_plane.json")
    task = workspace_store.create_task(
        "Business OS daily report",
        owner="chief-of-staff",
        project="hermes-business-os",
        source=source,
        status="open",
        next_action="Review warning/unavailable lanes before any external action.",
        note=render_business_ops_daily_report(report),
    )
    evidence = workspace_store.add_evidence(
        task_id=task["id"],
        title="Business OS daily report",
        locator=paths.get("markdown", ""),
        summary=_workspace_report_summary(report),
        kind="file",
        metadata={
            "json": paths.get("json", ""),
            "mode": report.get("mode"),
            "summary": report.get("summary", {}),
        },
    )
    task = workspace_store.update_task(
        task["id"],
        status="done",
        next_action="Report captured; act only after explicit operator approval.",
    )
    return {"task": task, "evidence": evidence}


def run_business_ops_daily_report(
    *,
    hermes_home: Path | None = None,
    v11_client: Any | None = None,
    collect_live: bool = True,
    write_workspace: bool = True,
    workspace_store: Any | None = None,
    source: str = "ops-daily-report",
    limit: int = 40,
) -> dict[str, Any]:
    """Collect, write, and optionally record the daily Business OS report."""
    resolved_home = Path(hermes_home) if hermes_home is not None else get_hermes_home()
    report = build_business_ops_daily_report(
        hermes_home=resolved_home,
        v11_client=v11_client,
        collect_live=collect_live,
        limit=limit,
    )
    paths = write_business_ops_daily_report(report, hermes_home=resolved_home)
    workspace = None
    if write_workspace:
        workspace = record_business_ops_daily_report(
            report,
            paths=paths,
            hermes_home=resolved_home,
            workspace_store=workspace_store,
            source=source,
        )
    return {"report": report, "paths": paths, "workspace": workspace}


def format_business_ops_daily_run(result: dict[str, Any]) -> str:
    """Format the operator response for /ops run."""
    report = result.get("report") if isinstance(result.get("report"), dict) else {}
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    paths = result.get("paths") if isinstance(result.get("paths"), dict) else {}
    workspace = result.get("workspace") if isinstance(result.get("workspace"), dict) else {}
    task = workspace.get("task") if isinstance(workspace.get("task"), dict) else {}
    evidence = workspace.get("evidence") if isinstance(workspace.get("evidence"), dict) else {}
    lines = [
        "Business OS daily report created",
        (
            f"Lanes: {summary.get('available_lanes', 0)}/{summary.get('lanes', 0)} available "
            f"| warnings {summary.get('warning_lanes', 0)} | items {summary.get('item_count', 0)}"
        ),
        f"Report: {paths.get('markdown') or 'not written'}",
    ]
    if task:
        lines.append(f"Workspace task: `{task.get('id')}`")
    if evidence:
        lines.append(f"Evidence: `{evidence.get('id')}`")
    lines.append("No customer sends, V11 writes, Atlas writes, or bank actions executed.")
    return "\n".join(lines)
