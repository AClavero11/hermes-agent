#!/usr/bin/env python3
"""Hermes workflow registry tool."""

from __future__ import annotations

from typing import Any

from hermes_cli.workflows import (
    WorkflowRegistry,
    format_workflow,
    format_workflow_status,
)
from tools.registry import registry, tool_error, tool_result


def check_workflow_requirements() -> bool:
    return True


def workflow_tool(action: str, **kwargs: Any) -> str:
    action_name = (action or "").strip().lower()
    store = WorkflowRegistry()
    try:
        if action_name in {"status", "summary"}:
            data = store.read()
            return tool_result(
                success=True,
                action="status",
                summary=format_workflow_status(data),
                registry=data,
            )

        if action_name in {"list", "list_workflows"}:
            workflows = store.list_workflows(
                include_disabled=bool(kwargs.get("include_disabled", True)),
                limit=int(kwargs.get("limit") or 50),
            )
            return tool_result(
                success=True,
                action="list",
                workflows=workflows,
                count=len(workflows),
            )

        if action_name in {"show", "get"}:
            workflow = store.get_workflow(str(kwargs.get("workflow_id") or ""))
            return tool_result(
                success=True,
                action="show",
                workflow=workflow,
                summary=format_workflow(workflow),
            )

        if action_name in {"kill", "disable"}:
            workflow = store.kill_workflow(
                str(kwargs.get("workflow_id") or ""),
                actor=kwargs.get("actor"),
                reason=kwargs.get("reason"),
            )
            return tool_result(
                success=True,
                action="kill",
                workflow=workflow,
                summary=format_workflow(workflow),
            )

        if action_name in {"resume", "enable"}:
            workflow = store.resume_workflow(
                str(kwargs.get("workflow_id") or ""),
                actor=kwargs.get("actor"),
                reason=kwargs.get("reason"),
            )
            return tool_result(
                success=True,
                action="resume",
                workflow=workflow,
                summary=format_workflow(workflow),
            )

        if action_name in {"is_enabled", "enabled"}:
            workflow_id = str(kwargs.get("workflow_id") or "")
            return tool_result(
                success=True,
                action="is_enabled",
                workflow_id=workflow_id,
                enabled=store.is_enabled(workflow_id),
            )

        if action_name in {"register", "create"}:
            workflow = store.register_workflow(
                str(kwargs.get("title") or ""),
                workflow_id=kwargs.get("workflow_id"),
                category=kwargs.get("category"),
                owner=kwargs.get("owner"),
                schedule=kwargs.get("schedule"),
                kill_switch_env=kwargs.get("kill_switch_env"),
                max_candidates=kwargs.get("max_candidates"),
                dedupe_key=kwargs.get("dedupe_key"),
                health_check=kwargs.get("health_check"),
                rollback=kwargs.get("rollback"),
                status=str(kwargs.get("status") or "unknown"),
                note=kwargs.get("note"),
            )
            return tool_result(
                success=True,
                action="register",
                workflow=workflow,
            )

        if action_name in {"report", "create_report"}:
            report = store.create_report(title=kwargs.get("title"))
            return tool_result(success=True, action="report", report=report)

        return tool_error(f"unknown action: {action}", success=False)
    except Exception as exc:
        return tool_error(str(exc), success=False, action=action_name)


WORKFLOW_SCHEMA = {
    "name": "workflow",
    "description": (
        "Durable Hermes workflow registry and operator kill-switch control. "
        "Use this to list production workflows, kill or resume a named workflow, "
        "and inspect each workflow's schedule, env gate, dedupe key, health check, "
        "and rollback note."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "status",
                    "list",
                    "show",
                    "kill",
                    "resume",
                    "is_enabled",
                    "register",
                    "report",
                ],
            },
            "workflow_id": {
                "type": "string",
                "description": "Workflow id or unambiguous id prefix.",
            },
            "title": {"type": "string"},
            "category": {"type": "string"},
            "owner": {"type": "string"},
            "schedule": {"type": "string"},
            "kill_switch_env": {"type": "string"},
            "max_candidates": {"type": "string"},
            "dedupe_key": {"type": "string"},
            "health_check": {"type": "string"},
            "rollback": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["enabled", "disabled", "paused", "unknown"],
            },
            "reason": {"type": "string"},
            "actor": {"type": "string"},
            "note": {"type": "string"},
            "include_disabled": {"type": "boolean"},
            "limit": {"type": "integer", "default": 50},
        },
        "required": ["action"],
    },
}


registry.register(
    name="workflow",
    toolset="workflow",
    schema=WORKFLOW_SCHEMA,
    handler=lambda args, **kw: workflow_tool(**args),
    check_fn=check_workflow_requirements,
    description="Durable workflow registry and kill-switch control",
)
