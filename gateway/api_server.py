"""Hermes org task and approval HTTP API server."""

from __future__ import annotations

import asyncio
import functools
import hmac
import json
import logging
import sys
import time
from typing import Any, Callable, Dict, List, Optional

from aiohttp import web

from hermes_cli import __version__
from hermes_state import SessionDB

logger = logging.getLogger(__name__)

_SOURCE_REF_KEYS = {
    "source_ref",
    "sourceRef",
    "source_reference",
    "sourceReference",
}


def _json_error(message: str, status: int) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _missing_auth_response() -> web.Response:
    return _json_error("missing Authorization bearer token", 401)


def _check_authorization(request: web.Request) -> Optional[web.Response]:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header:
        return _missing_auth_response()

    prefix = "Bearer "
    if not auth_header.startswith(prefix):
        return _missing_auth_response()

    provided_key = auth_header[len(prefix):]
    if not provided_key:
        return _missing_auth_response()

    expected_key = request.app["service_key"]
    if not hmac.compare_digest(provided_key, expected_key):
        return _json_error("invalid service key", 403)
    return None


@web.middleware
async def _auth_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Any],
) -> web.StreamResponse:
    if request.path == "/health":
        return await handler(request)

    auth_response = _check_authorization(request)
    if auth_response is not None:
        return auth_response
    return await handler(request)


@web.middleware
async def _error_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Any],
) -> web.StreamResponse:
    try:
        return await handler(request)
    except ValueError as exc:
        return _json_error(str(exc), 400)
    except web.HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected Hermes API server error")
        return _json_error("internal server error", 500)


async def _db_call(method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    loop = asyncio.get_running_loop()
    call = functools.partial(method, *args, **kwargs)
    return await loop.run_in_executor(None, call)


async def _json_body(request: web.Request) -> Dict[str, Any]:
    raw_body = await request.text()
    if not raw_body.strip():
        return {}
    try:
        parsed = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise ValueError("malformed JSON body") from exc
    if not isinstance(parsed, dict):
        raise ValueError("JSON body must be an object")
    return parsed


def _require_field(data: Dict[str, Any], field_name: str) -> Any:
    value = data.get(field_name)
    if value is None or not str(value).strip():
        raise ValueError(f"{field_name} is required")
    return value


def _query_int(request: web.Request, field_name: str, default: int) -> int:
    raw_value = request.query.get(field_name)
    if raw_value is None or raw_value == "":
        return default
    try:
        return int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc


def _query_limit(request: web.Request, default: int = 50) -> int:
    return max(1, min(_query_int(request, "limit", default), 500))


def _query_offset(request: web.Request) -> int:
    return max(0, _query_int(request, "offset", 0))


def _load_json_field(value: Any, field_name: str) -> Any:
    if value is None:
        return {}
    if isinstance(value, str):
        raw_value = value.strip()
        if not raw_value:
            return {}
        try:
            return json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_name} must be valid JSON") from exc
    return value


def _merge_source_ref(evidence_json: Any, source_ref: Any) -> Any:
    if source_ref is None or not str(source_ref).strip():
        return evidence_json

    evidence = _load_json_field(evidence_json, "evidence_json")
    normalized_ref = str(source_ref).strip()
    if isinstance(evidence, dict):
        merged = dict(evidence)
        merged["source_ref"] = normalized_ref
        return merged
    if evidence in (None, ""):
        return {"source_ref": normalized_ref}
    return {"source_ref": normalized_ref, "evidence": evidence}


def _contains_source_ref(value: Any, source_ref: str) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _SOURCE_REF_KEYS and str(item).strip() == source_ref:
                return True
            if _contains_source_ref(item, source_ref):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_source_ref(item, source_ref) for item in value)
    return False


def _task_matches_source_ref(task: Dict[str, Any], source_ref: str) -> bool:
    for field_name in ("entities_json", "evidence_json"):
        try:
            value = _load_json_field(task.get(field_name), field_name)
        except ValueError:
            continue
        if _contains_source_ref(value, source_ref):
            return True
    return False


async def _handle_health(request: web.Request) -> web.Response:
    return web.json_response({
        "ok": True,
        "version": __version__,
        "started_at": request.app["started_at"],
    })


async def _handle_create_task(request: web.Request) -> web.Response:
    data = await _json_body(request)
    evidence_json = data.get("evidence_json")
    if "source_ref" in data:
        evidence_json = _merge_source_ref(evidence_json, data.get("source_ref"))

    task = await _db_call(
        request.app["db"].create_org_task,
        source=_require_field(data, "source"),
        intent=_require_field(data, "intent"),
        entities_json=data.get("entities_json"),
        evidence_json=evidence_json,
        owner=data.get("owner"),
        status=data.get("status", "open"),
        next_action=data.get("next_action"),
        task_id=data.get("task_id"),
    )
    return web.json_response({"task": task}, status=201)


async def _list_tasks_with_source_ref(
    request: web.Request,
    *,
    source_ref: str,
    status: Optional[str],
    owner: Optional[str],
    source: Optional[str],
    limit: int,
    offset: int,
) -> List[Dict[str, Any]]:
    matches: List[Dict[str, Any]] = []
    db_offset = 0
    page_limit = 500
    desired_count = offset + limit

    while len(matches) < desired_count:
        batch = await _db_call(
            request.app["db"].list_org_tasks,
            status=status,
            owner=owner,
            source=source,
            limit=page_limit,
            offset=db_offset,
        )
        if not batch:
            break
        matches.extend(
            task for task in batch if _task_matches_source_ref(task, source_ref)
        )
        if len(batch) < page_limit:
            break
        db_offset += page_limit

    return matches[offset:desired_count]


async def _handle_list_tasks(request: web.Request) -> web.Response:
    limit = _query_limit(request)
    offset = _query_offset(request)
    status = request.query.get("status") or None
    owner = request.query.get("owner") or None
    source = request.query.get("source") or None
    source_ref = request.query.get("source_ref") or None

    if source_ref:
        tasks = await _list_tasks_with_source_ref(
            request,
            source_ref=source_ref,
            status=status,
            owner=owner,
            source=source,
            limit=limit,
            offset=offset,
        )
    else:
        tasks = await _db_call(
            request.app["db"].list_org_tasks,
            status=status,
            owner=owner,
            source=source,
            limit=limit,
            offset=offset,
        )
    return web.json_response({"tasks": tasks, "count": len(tasks)})


async def _handle_get_task(request: web.Request) -> web.Response:
    task = await _db_call(
        request.app["db"].get_org_task,
        request.match_info["task_id"],
    )
    if task is None:
        return _json_error("task not found", 404)
    return web.json_response({"task": task})


async def _handle_update_task(request: web.Request) -> web.Response:
    data = await _json_body(request)
    task_id = request.match_info["task_id"]
    allowed_fields = {
        "source",
        "intent",
        "entities_json",
        "evidence_json",
        "owner",
        "status",
        "next_action",
    }
    updates = {
        field_name: data[field_name]
        for field_name in allowed_fields
        if field_name in data
    }

    if "source_ref" in data:
        evidence_json = updates.get("evidence_json")
        if evidence_json is None:
            existing = await _db_call(request.app["db"].get_org_task, task_id)
            if existing is None:
                return _json_error("task not found", 404)
            evidence_json = existing.get("evidence_json")
        updates["evidence_json"] = _merge_source_ref(
            evidence_json,
            data.get("source_ref"),
        )

    task = await _db_call(
        request.app["db"].update_org_task,
        task_id,
        **updates,
    )
    if task is None:
        return _json_error("task not found", 404)
    return web.json_response({"task": task})


async def _handle_complete_task(request: web.Request) -> web.Response:
    data = await _json_body(request)
    task_id = request.match_info["task_id"]
    if "next_action" in data:
        task = await _db_call(
            request.app["db"].complete_org_task,
            task_id,
            next_action=data.get("next_action"),
        )
    else:
        task = await _db_call(request.app["db"].complete_org_task, task_id)
    if task is None:
        return _json_error("task not found", 404)
    return web.json_response({"task": task})


async def _handle_list_events(request: web.Request) -> web.Response:
    task = await _db_call(
        request.app["db"].get_org_task,
        request.match_info["task_id"],
    )
    if task is None:
        return _json_error("task not found", 404)

    events = await _db_call(
        request.app["db"].list_org_events,
        task_id=request.match_info["task_id"],
        limit=_query_limit(request),
        offset=_query_offset(request),
    )
    return web.json_response({"events": events, "count": len(events)})


async def _handle_request_external_action_approval(
    request: web.Request,
) -> web.Response:
    data = await _json_body(request)
    org_task_id = data.get("org_task_id")
    if org_task_id:
        task = await _db_call(request.app["db"].get_org_task, org_task_id)
        if task is None:
            return _json_error("task not found", 404)

    row = await _db_call(
        request.app["db"].create_approval_request,
        action_type=_require_field(data, "action_type"),
        channel=_require_field(data, "channel"),
        target=_require_field(data, "target"),
        payload=_require_field(data, "payload"),
        requested_by=data.get("requested_by"),
        org_task_id=org_task_id,
        metadata=data.get("metadata"),
    )
    return web.json_response(
        {
            "approval_id": row["id"],
            "payload_hash": row["payload_hash"],
            "status": row["status"],
            "created_at": row["created_at"],
        },
        status=201,
    )


async def create_api_app(
    db: SessionDB,
    *,
    service_key: str,
) -> web.Application:
    """Create the aiohttp app for Hermes org task and approval endpoints."""
    if not service_key:
        raise ValueError("service_key is required")

    app = web.Application(middlewares=[_error_middleware, _auth_middleware])
    app["db"] = db
    app["service_key"] = service_key
    app["started_at"] = time.time()
    app.router.add_get("/health", _handle_health)
    app.router.add_post("/api/org/tasks", _handle_create_task)
    app.router.add_get("/api/org/tasks", _handle_list_tasks)
    app.router.add_get("/api/org/tasks/{task_id}", _handle_get_task)
    app.router.add_patch("/api/org/tasks/{task_id}", _handle_update_task)
    app.router.add_put("/api/org/tasks/{task_id}/complete", _handle_complete_task)
    app.router.add_get("/api/org/tasks/{task_id}/events", _handle_list_events)
    app.router.add_post(
        "/api/org/external-actions/request_external_action_approval",
        _handle_request_external_action_approval,
    )
    return app


async def run_api_server(
    db: SessionDB,
    *,
    host: str = "localhost",
    port: int = 8642,
    service_key: str,
) -> None:
    """Run the Hermes HTTP API server forever."""
    if not service_key:
        logger.warning("HERMES_SERVICE_KEY is required; refusing to start API server")
        raise SystemExit(1)

    app = await create_api_app(db, service_key=service_key)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, int(port))
    try:
        await site.start()
        logger.info("Hermes HTTP API server listening on %s:%s", host, port)
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        raise
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    logger.warning(
        "gateway.api_server is started by gateway.run and requires a gateway-owned SessionDB"
    )
    sys.exit(1)
