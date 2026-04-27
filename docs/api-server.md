# Hermes HTTP API Server

Hermes starts a small aiohttp server from the gateway process when
`HERMES_SERVICE_KEY` is set. It shares the gateway-owned `SessionDB` and exposes
org task and external-action approval endpoints for internal AAC services.

## Environment

- `HERMES_SERVICE_KEY`: required bearer token for every endpoint except `/health`.
- `HERMES_API_PORT`: optional port, defaults to `8642`.

Restart the gateway after changing either value. If `HERMES_SERVICE_KEY` is not
set, the gateway logs a warning and skips the HTTP API server while keeping the
messaging adapters alive.

## Auth

Send the service key as a bearer token:

```bash
Authorization: Bearer $HERMES_SERVICE_KEY
```

Missing auth returns `401`. A mismatched token returns `403`.

## Health

```http
GET /health
```

```json
{
  "ok": true,
  "version": "0.11.0",
  "started_at": 1777315200.0
}
```

## Tasks

```http
POST /api/org/tasks
Content-Type: application/json
Authorization: Bearer $HERMES_SERVICE_KEY
```

```json
{
  "source": "advanced-parts",
  "intent": "Review RFQ 123",
  "source_ref": "rfq:123",
  "owner": "sales",
  "status": "open",
  "next_action": "triage request",
  "entities_json": {"customer": "Example Air"},
  "evidence_json": {"url": "https://example.test/rfq/123"}
}
```

Response:

```json
{
  "task": {
    "id": "orgtask_abc",
    "source": "advanced-parts",
    "intent": "Review RFQ 123",
    "entities_json": "{\"customer\":\"Example Air\"}",
    "evidence_json": "{\"source_ref\":\"rfq:123\",\"url\":\"https://example.test/rfq/123\"}",
    "owner": "sales",
    "status": "open",
    "next_action": "triage request",
    "created_at": 1777315200.0,
    "updated_at": 1777315200.0,
    "completed_at": null
  }
}
```

```http
GET /api/org/tasks?status=&owner=&source=&source_ref=&limit=&offset=
GET /api/org/tasks/{id}
PATCH /api/org/tasks/{id}
PUT /api/org/tasks/{id}/complete
GET /api/org/tasks/{id}/events
```

List responses use `{"tasks": [...], "count": N}`. Event responses use
`{"events": [...], "count": N}`. Single-task responses use `{"task": {...}}`.

`PATCH /api/org/tasks/{id}` accepts any subset of `source`, `intent`,
`entities_json`, `evidence_json`, `owner`, `status`, `next_action`, and
`source_ref`. Illegal task status transitions return `400`.

`PUT /api/org/tasks/{id}/complete` accepts an optional JSON body:

```json
{"next_action": "closed in advanced-parts"}
```

## Approvals

```http
POST /api/org/external-actions/request_external_action_approval
Content-Type: application/json
Authorization: Bearer $HERMES_SERVICE_KEY
```

```json
{
  "action_type": "external_send",
  "channel": "telegram",
  "target": "-1001",
  "payload": {"message": "Send quote"},
  "requested_by": "advanced-parts",
  "metadata": {"source": "advanced-parts"}
}
```

Response:

```json
{
  "approval_id": "appr_abc",
  "payload_hash": "64 hex chars",
  "status": "pending",
  "created_at": 1777315200.0
}
```

## Smoke Test

```bash
curl -H "Authorization: Bearer $HERMES_SERVICE_KEY" http://localhost:8642/health
curl -H "Authorization: Bearer $HERMES_SERVICE_KEY" \
  -H "Content-Type: application/json" \
  -d '{"source":"test","intent":"first task"}' \
  http://localhost:8642/api/org/tasks
```
