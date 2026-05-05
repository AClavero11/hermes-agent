"""AAC business-ops brief for Hermes operator mode."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home


BUSINESS_OS_WORKFLOW_IDS: tuple[str, ...] = (
    "finance-admin-daily-brief",
    "purchasing-vendor-followup",
    "repair-stuck-units",
    "inventory-hot-parts-review",
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
