#!/usr/bin/env python3
"""Hermes Workspace control-plane tool."""

from __future__ import annotations

import json
from typing import Any

from hermes_cli.workspace import WorkspaceStore, format_workspace_status
from tools.registry import registry, tool_error, tool_result


def check_workspace_requirements() -> bool:
    return True


def _json_metadata(value: Any) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("metadata must be a JSON object")


def workspace_tool(action: str, **kwargs: Any) -> str:
    """Manage the durable Hermes Workspace control plane."""
    action_name = (action or "").strip().lower()
    store = WorkspaceStore()
    try:
        if action_name in {"status", "summary"}:
            data = store.read()
            return tool_result(
                success=True,
                action="status",
                summary=format_workspace_status(data),
                workspace=data,
            )

        if action_name in {"create_task", "create", "add_task", "add"}:
            task = store.create_task(
                str(kwargs.get("title") or ""),
                owner=kwargs.get("owner"),
                priority=kwargs.get("priority"),
                project=kwargs.get("project"),
                source=kwargs.get("source"),
                next_action=kwargs.get("next_action"),
                status=kwargs.get("status", "open"),
                note=kwargs.get("note"),
            )
            return tool_result(success=True, action="create_task", task=task)

        if action_name in {"list_tasks", "list"}:
            tasks = store.list_tasks(
                status=kwargs.get("status"),
                include_done=bool(kwargs.get("include_done", False)),
                limit=kwargs.get("limit", 20),
            )
            return tool_result(
                success=True,
                action="list_tasks",
                tasks=tasks,
                count=len(tasks),
            )

        if action_name in {"get_task", "get", "read"}:
            task = store.get_task(str(kwargs.get("task_id") or ""))
            return tool_result(success=True, action="get_task", task=task)

        if action_name in {"update_task", "update", "start", "block", "done", "cancel"}:
            status = kwargs.get("status")
            if action_name == "start":
                status = "in_progress"
            elif action_name == "block":
                status = "blocked"
            elif action_name == "done":
                status = "done"
            elif action_name == "cancel":
                status = "cancelled"
            task = store.update_task(
                str(kwargs.get("task_id") or ""),
                status=status,
                owner=kwargs.get("owner"),
                priority=kwargs.get("priority"),
                project=kwargs.get("project"),
                next_action=kwargs.get("next_action"),
                note=kwargs.get("note"),
            )
            return tool_result(success=True, action="update_task", task=task)

        if action_name in {"add_evidence", "evidence"}:
            evidence = store.add_evidence(
                task_id=kwargs.get("task_id"),
                title=kwargs.get("title"),
                locator=kwargs.get("locator"),
                summary=kwargs.get("summary"),
                kind=kwargs.get("kind"),
                metadata=_json_metadata(kwargs.get("metadata")),
            )
            return tool_result(success=True, action="add_evidence", evidence=evidence)

        if action_name in {"add_inbox", "inbox"}:
            item = store.add_inbox_item(
                str(kwargs.get("text") or ""),
                source=kwargs.get("source"),
                title=kwargs.get("title"),
                metadata=_json_metadata(kwargs.get("metadata")),
            )
            return tool_result(success=True, action="add_inbox", item=item)

        if action_name in {"report", "create_report"}:
            report = store.create_report(title=kwargs.get("title"))
            return tool_result(success=True, action="report", report=report)

        return tool_error(f"unknown action: {action}", success=False)
    except Exception as exc:
        return tool_error(str(exc), success=False, action=action_name)


WORKSPACE_SCHEMA = {
    "name": "workspace",
    "description": (
        "Durable Hermes Workspace control plane. Use this for work that must "
        "survive chat/session resets: tasks, next actions, evidence records, "
        "inbox captures, and reports. Prefer this over the ephemeral todo tool "
        "for cross-session or org-level work."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "status",
                    "create_task",
                    "list_tasks",
                    "get_task",
                    "update_task",
                    "start",
                    "block",
                    "done",
                    "cancel",
                    "add_evidence",
                    "add_inbox",
                    "report",
                ],
            },
            "task_id": {
                "type": "string",
                "description": "Task id or unambiguous id prefix.",
            },
            "title": {
                "type": "string",
                "description": "Task, evidence, inbox, or report title.",
            },
            "status": {
                "type": "string",
                "enum": ["open", "pending", "in_progress", "blocked", "done", "cancelled"],
            },
            "owner": {"type": "string"},
            "priority": {"type": "string"},
            "project": {"type": "string"},
            "source": {"type": "string"},
            "next_action": {"type": "string"},
            "note": {"type": "string"},
            "locator": {
                "type": "string",
                "description": "URL, file path, command, or durable reference for evidence.",
            },
            "summary": {"type": "string"},
            "kind": {
                "type": "string",
                "enum": ["url", "file", "note", "command", "artifact", "other"],
            },
            "text": {"type": "string"},
            "metadata": {
                "type": "string",
                "description": "Optional JSON object string with additional metadata.",
            },
            "include_done": {"type": "boolean"},
            "limit": {"type": "integer", "default": 20},
        },
        "required": ["action"],
    },
}


registry.register(
    name="workspace",
    toolset="workspace",
    schema=WORKSPACE_SCHEMA,
    handler=lambda args, **kw: workspace_tool(**args),
    check_fn=check_workspace_requirements,
    description="Durable Workspace task/evidence/report control plane",
)
