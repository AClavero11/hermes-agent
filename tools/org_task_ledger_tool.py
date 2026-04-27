#!/usr/bin/env python3
"""
Durable org task ledger tool.

Stores company-level tasks in ``state.db`` so operational work survives
agent/session resets. The database layer owns concurrency, WAL mode, and
audit events; this tool is a thin action dispatcher over ``SessionDB``.
"""

from typing import Any, Dict, Optional

from hermes_state import SessionDB
from tools.registry import registry, tool_error, tool_result


def check_org_task_ledger_requirements() -> bool:
    """The org task ledger uses local SQLite only."""
    return True


def _require_task_id(kwargs: Dict[str, Any]) -> Optional[str]:
    task_id = kwargs.get("task_id")
    if task_id is None or not str(task_id).strip():
        return None
    return str(task_id).strip()


def org_task_ledger_tool(
    action: str,
    db: Optional[SessionDB] = None,
    **kwargs: Any,
) -> str:
    """Create, list, update, complete, or search durable org tasks."""
    if action is None:
        return tool_error("action is required", success=False)

    action_name = str(action).strip().lower()
    owns_db = db is None
    ledger_db = db or SessionDB()

    try:
        if action_name == "create":
            task = ledger_db.create_org_task(
                source=kwargs.get("source"),
                intent=kwargs.get("intent"),
                entities_json=kwargs.get("entities_json"),
                evidence_json=kwargs.get("evidence_json"),
                owner=kwargs.get("owner"),
                status=kwargs.get("status", "open"),
                next_action=kwargs.get("next_action"),
                task_id=kwargs.get("task_id"),
            )
            return tool_result(success=True, action="create", task=task)

        if action_name in {"get", "read"}:
            task_id = _require_task_id(kwargs)
            if not task_id:
                return tool_error("task_id is required", success=False)
            task = ledger_db.get_org_task(task_id)
            if not task:
                return tool_error("task not found", success=False, task_id=task_id)
            return tool_result(success=True, action="get", task=task)

        if action_name == "list":
            tasks = ledger_db.list_org_tasks(
                status=kwargs.get("status"),
                owner=kwargs.get("owner"),
                source=kwargs.get("source"),
                limit=kwargs.get("limit", 50),
                offset=kwargs.get("offset", 0),
            )
            return tool_result(
                success=True,
                action="list",
                tasks=tasks,
                count=len(tasks),
            )

        if action_name == "update":
            task_id = _require_task_id(kwargs)
            if not task_id:
                return tool_error("task_id is required", success=False)
            updates = {
                field: kwargs[field]
                for field in (
                    "source",
                    "intent",
                    "entities_json",
                    "evidence_json",
                    "owner",
                    "status",
                    "next_action",
                )
                if field in kwargs
            }
            task = ledger_db.update_org_task(task_id, **updates)
            if not task:
                return tool_error("task not found", success=False, task_id=task_id)
            return tool_result(success=True, action="update", task=task)

        if action_name == "complete":
            task_id = _require_task_id(kwargs)
            if not task_id:
                return tool_error("task_id is required", success=False)
            if "next_action" in kwargs:
                task = ledger_db.complete_org_task(
                    task_id,
                    next_action=kwargs.get("next_action"),
                )
            else:
                task = ledger_db.complete_org_task(task_id)
            if not task:
                return tool_error("task not found", success=False, task_id=task_id)
            return tool_result(success=True, action="complete", task=task)

        if action_name == "search":
            query = kwargs.get("query")
            if query is None or not str(query).strip():
                return tool_error("query is required", success=False)
            tasks = ledger_db.search_org_tasks(
                str(query),
                status=kwargs.get("status"),
                owner=kwargs.get("owner"),
                limit=kwargs.get("limit", 20),
                offset=kwargs.get("offset", 0),
            )
            return tool_result(
                success=True,
                action="search",
                tasks=tasks,
                count=len(tasks),
            )

        if action_name in {"events", "list_events"}:
            events = ledger_db.list_org_events(
                task_id=kwargs.get("task_id"),
                limit=kwargs.get("limit", 50),
                offset=kwargs.get("offset", 0),
            )
            return tool_result(
                success=True,
                action="events",
                events=events,
                count=len(events),
            )

        return tool_error(f"unknown action: {action}", success=False)
    except ValueError as exc:
        return tool_error(str(exc), success=False)
    finally:
        if owns_db:
            ledger_db.close()


ORG_TASK_LEDGER_SCHEMA = {
    "name": "org_task_ledger",
    "description": (
        "Manage durable company task records in Hermes state.db. "
        "Actions: create, get, list, update, complete, search, events. "
        "Use this for org-level operational tasks that must survive agent "
        "or session resets."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "create",
                    "get",
                    "list",
                    "update",
                    "complete",
                    "search",
                    "events",
                ],
                "description": "Ledger action to perform.",
            },
            "task_id": {
                "type": "string",
                "description": "Task ID for get/update/complete/events, or optional ID for create.",
            },
            "source": {
                "type": "string",
                "description": "Origin of the task, such as telegram, api, cli, or atlas.",
            },
            "intent": {
                "type": "string",
                "description": "User or business intent this task captures.",
            },
            "entities_json": {
                "type": "string",
                "description": "JSON entities payload. Stored as validated JSON text.",
            },
            "evidence_json": {
                "type": "string",
                "description": "JSON evidence payload. Stored as validated JSON text.",
            },
            "owner": {
                "type": "string",
                "description": "Human, agent, or system responsible for the next step.",
            },
            "status": {
                "type": "string",
                "enum": [
                    "open",
                    "pending",
                    "in_progress",
                    "blocked",
                    "completed",
                    "cancelled",
                ],
                "description": "Task status.",
            },
            "next_action": {
                "type": "string",
                "description": "Concrete next step for this task.",
            },
            "query": {
                "type": "string",
                "description": "Search query for org task search.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum rows to return.",
                "default": 50,
            },
            "offset": {
                "type": "integer",
                "description": "Rows to skip for pagination.",
                "default": 0,
            },
        },
        "required": ["action"],
    },
}


registry.register(
    name="org_task_ledger",
    toolset="org_tasks",
    schema=ORG_TASK_LEDGER_SCHEMA,
    handler=lambda args, **kw: org_task_ledger_tool(
        db=kw.get("db"),
        **args,
    ),
    check_fn=check_org_task_ledger_requirements,
    description="Durable company task ledger",
)
