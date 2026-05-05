"""Durable Hermes workflow registry and operator kill switches."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None


WORKFLOW_SCHEMA_VERSION = 1
VALID_WORKFLOW_STATUSES = {"enabled", "disabled", "paused", "unknown"}
ACTIVE_WORKFLOW_STATUSES = {"enabled", "unknown"}
_THREAD_LOCK = threading.RLock()


DEFAULT_WORKFLOWS: tuple[dict[str, Any], ...] = (
    {
        "id": "rfq-intake",
        "title": "advanced-parts Gmail RFQ intake",
        "category": "rfq",
        "owner": "aac-ops",
        "status": "disabled",
        "schedule": "Vercel cron /api/cron/rfq-intake, every 5 minutes",
        "kill_switch_env": "HERMES_RFQ_GMAIL_ENABLED",
        "max_candidates": "bounded summary required before re-enable",
        "dedupe_key": "gmail_message_id/body_hash",
        "health_check": "GET https://advanced.parts/api/cron/rfq-intake should prove enabled=false and created_tasks=[] when disabled",
        "rollback": "Set HERMES_RFQ_GMAIL_ENABLED=true and redeploy only after dedupe/batching canary passes",
        "notes": [
            {
                "at": 1777996800.0,
                "text": "Disabled on 2026-05-05 after extracted RFQ Telegram noise.",
            }
        ],
    },
    {
        "id": "hermes-canary-daily",
        "title": "Hermes daily readiness canary",
        "category": "health",
        "owner": "hermes",
        "status": "enabled",
        "schedule": "Studio launchd daily canary around 06:20",
        "kill_switch_env": "",
        "max_candidates": "n/a",
        "dedupe_key": "report timestamp",
        "health_check": "latest.json plus history.jsonl under HERMES_HOME/canary/reports",
        "rollback": "Disable the launchd canary job only if it is noisy or failing open",
        "notes": [],
    },
    {
        "id": "kanban-daily-brief",
        "title": "Hermes Kanban daily operator brief",
        "category": "briefing",
        "owner": "chief-of-staff",
        "status": "enabled",
        "schedule": "Hermes cron 0 8 * * *",
        "kill_switch_env": "",
        "max_candidates": "one summary per run",
        "dedupe_key": "run date",
        "health_check": "Hermes cron list plus Telegram send-path evidence",
        "rollback": "Pause the Hermes cron job before editing the brief workflow",
        "notes": [],
    },
    {
        "id": "finance-admin-daily-brief",
        "title": "AAC finance/admin daily operating brief",
        "category": "finance",
        "owner": "finance-admin",
        "status": "enabled",
        "schedule": "daily operator brief; no bank/V11 writes without approval",
        "kill_switch_env": "",
        "max_candidates": "one sourced cash/AR/AP summary per run",
        "dedupe_key": "run date + account snapshot timestamp",
        "health_check": "/ops brief must show finance-admin lane and current canary score",
        "rollback": "Disable workflow and keep finance/admin work manual",
        "notes": [],
    },
    {
        "id": "purchasing-vendor-followup",
        "title": "AAC purchasing and vendor follow-up queue",
        "category": "purchasing",
        "owner": "operator",
        "status": "enabled",
        "schedule": "daily stale PO/vendor quote review",
        "kill_switch_env": "",
        "max_candidates": "bounded stale vendor list with owner and next action",
        "dedupe_key": "vendor + PO/RFQ/reference + follow-up date",
        "health_check": "/ops brief must show purchasing lane and blocked/active work",
        "rollback": "Disable workflow and keep vendor follow-ups manual",
        "notes": [],
    },
    {
        "id": "repair-stuck-units",
        "title": "AAC stuck repair and teardown blocker review",
        "category": "repairs",
        "owner": "operator",
        "status": "enabled",
        "schedule": "daily blocked repair/teardown digest",
        "kill_switch_env": "",
        "max_candidates": "bounded blocked-unit list with evidence and unblock step",
        "dedupe_key": "repair/order/unit + blocker category",
        "health_check": "/ops brief must show repairs lane and Workspace blockers",
        "rollback": "Disable workflow and keep repair blocker review manual",
        "notes": [],
    },
    {
        "id": "inventory-hot-parts-review",
        "title": "AAC hot-parts and stale-stock inventory review",
        "category": "inventory",
        "owner": "sales-rfq",
        "status": "enabled",
        "schedule": "daily hot stock / stale stock / repricing review",
        "kill_switch_env": "",
        "max_candidates": "bounded inventory opportunities with no customer sends",
        "dedupe_key": "part number + condition + location + run date",
        "health_check": "/ops brief must show inventory lane and V11 grounding",
        "rollback": "Disable workflow and keep inventory review manual",
        "notes": [],
    },
)


def workflows_root() -> Path:
    override = os.getenv("HERMES_WORKFLOW_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return get_hermes_home() / "workflows"


def workflow_store_path() -> Path:
    override = os.getenv("HERMES_WORKFLOW_STORE", "").strip()
    if override:
        return Path(override).expanduser()
    return workflows_root() / "registry.json"


def _now() -> float:
    return time.time()


def _iso(ts: float | None) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))


def _slug(value: str, *, default: str = "workflow", max_len: int = 56) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower()).strip("-")
    text = text or default
    return text[:max_len].strip("-") or default


def _normalize_status(status: str | None, *, default: str = "unknown") -> str:
    normalized = (status or default).strip().lower().replace("-", "_")
    if normalized in {"on", "active", "running"}:
        normalized = "enabled"
    if normalized in {"off", "stopped", "killed", "disabled"}:
        normalized = "disabled"
    if normalized not in VALID_WORKFLOW_STATUSES:
        raise ValueError(f"invalid workflow status: {status}")
    return normalized


def _empty_store() -> dict[str, Any]:
    now = _now()
    return {
        "version": WORKFLOW_SCHEMA_VERSION,
        "created_at": now,
        "updated_at": now,
        "workflows": {},
        "events": {},
        "reports": {},
    }


@contextmanager
def _locked_store(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _THREAD_LOCK:
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class WorkflowRegistry:
    """File-backed registry of production workflows and kill-switch state."""

    def __init__(self, path: Path | None = None, *, seed_defaults: bool = True):
        self.path = Path(path) if path is not None else workflow_store_path()
        self.seed_defaults = seed_defaults

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = _empty_store()
        except json.JSONDecodeError as exc:
            raise ValueError(f"workflow registry is not valid JSON: {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raw = _empty_store()
        raw.setdefault("version", WORKFLOW_SCHEMA_VERSION)
        raw.setdefault("created_at", _now())
        raw.setdefault("updated_at", raw.get("created_at") or _now())
        raw.setdefault("workflows", {})
        raw.setdefault("events", {})
        raw.setdefault("reports", {})
        if self.seed_defaults:
            raw["_seed_defaults_changed"] = self._seed_defaults_unlocked(raw)
        return raw

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        data.pop("_seed_defaults_changed", None)
        data["version"] = WORKFLOW_SCHEMA_VERSION
        data["updated_at"] = _now()
        payload = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=str(self.path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                tmp.write(payload)
                tmp.write("\n")
            Path(tmp_name).replace(self.path)
        except Exception:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except Exception:
                pass
            raise

    def _seed_defaults_unlocked(self, data: dict[str, Any]) -> bool:
        workflows = data.setdefault("workflows", {})
        now = _now()
        changed = False
        for item in DEFAULT_WORKFLOWS:
            workflow_id = str(item["id"])
            if workflow_id in workflows:
                continue
            workflow = dict(item)
            workflow.setdefault("created_at", now)
            workflow.setdefault("updated_at", now)
            workflow.setdefault("last_changed_at", now)
            workflow.setdefault("last_changed_by", "seed")
            workflow["status"] = _normalize_status(workflow.get("status"))
            workflows[workflow_id] = workflow
            changed = True
        if changed:
            data["updated_at"] = now
        return changed

    def read(self) -> dict[str, Any]:
        with _locked_store(self.path):
            data = self._read_unlocked()
            changed = bool(data.pop("_seed_defaults_changed", False))
            if self.seed_defaults and (changed or not self.path.exists()):
                self._write_unlocked(data)
            return data

    def register_workflow(
        self,
        title: str,
        *,
        workflow_id: str | None = None,
        category: str | None = None,
        owner: str | None = None,
        schedule: str | None = None,
        kill_switch_env: str | None = None,
        max_candidates: str | None = None,
        dedupe_key: str | None = None,
        health_check: str | None = None,
        rollback: str | None = None,
        status: str = "unknown",
        note: str | None = None,
    ) -> dict[str, Any]:
        title = (title or "").strip()
        if not title:
            raise ValueError("workflow title is required")
        workflow_id = _slug(workflow_id or title)
        now = _now()
        workflow = {
            "id": workflow_id,
            "title": title,
            "category": (category or "").strip(),
            "owner": (owner or "").strip(),
            "status": _normalize_status(status),
            "schedule": (schedule or "").strip(),
            "kill_switch_env": (kill_switch_env or "").strip(),
            "max_candidates": (max_candidates or "").strip(),
            "dedupe_key": (dedupe_key or "").strip(),
            "health_check": (health_check or "").strip(),
            "rollback": (rollback or "").strip(),
            "notes": [],
            "created_at": now,
            "updated_at": now,
            "last_changed_at": now,
            "last_changed_by": "register",
        }
        if note:
            workflow["notes"].append({"at": now, "text": str(note).strip()})
        with _locked_store(self.path):
            data = self._read_unlocked()
            if workflow_id in data["workflows"]:
                raise ValueError(f"workflow already exists: {workflow_id}")
            data["workflows"][workflow_id] = workflow
            self._append_event_unlocked(
                data,
                workflow_id,
                "register",
                actor="register",
                reason=note or "registered workflow",
            )
            self._write_unlocked(data)
        return workflow

    def resolve_workflow_id(self, workflow_id_or_prefix: str) -> str:
        raw = _slug(workflow_id_or_prefix or "")
        if not raw:
            raise ValueError("workflow id is required")
        data = self.read()
        workflows = data.get("workflows", {})
        if raw in workflows:
            return raw
        matches = [
            workflow_id
            for workflow_id, workflow in workflows.items()
            if workflow_id.startswith(raw)
            or _slug(str(workflow.get("title") or "")).startswith(raw)
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(f"workflow not found: {workflow_id_or_prefix}")
        raise ValueError(f"ambiguous workflow id prefix: {workflow_id_or_prefix}")

    def has_workflow(self, workflow_id_or_prefix: str) -> bool:
        try:
            self.resolve_workflow_id(workflow_id_or_prefix)
            return True
        except ValueError:
            return False

    def get_workflow(self, workflow_id_or_prefix: str) -> dict[str, Any]:
        workflow_id = self.resolve_workflow_id(workflow_id_or_prefix)
        workflow = self.read()["workflows"].get(workflow_id)
        if not workflow:
            raise ValueError(f"workflow not found: {workflow_id_or_prefix}")
        return workflow

    def list_workflows(
        self,
        *,
        include_disabled: bool = True,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        data = self.read()
        workflows = list(data.get("workflows", {}).values())
        if not include_disabled:
            workflows = [
                workflow for workflow in workflows
                if workflow.get("status") in ACTIVE_WORKFLOW_STATUSES
            ]
        workflows.sort(key=lambda item: (str(item.get("category") or ""), str(item.get("id") or "")))
        return workflows[: max(1, min(int(limit or 50), 200))]

    def _append_event_unlocked(
        self,
        data: dict[str, Any],
        workflow_id: str,
        action: str,
        *,
        actor: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        event_id = f"wf_event_{time.strftime('%Y%m%d')}_{_slug(workflow_id, max_len=32)}_{len(data.get('events', {})) + 1:04d}"
        event = {
            "id": event_id,
            "workflow_id": workflow_id,
            "action": action,
            "actor": (actor or "").strip(),
            "reason": (reason or "").strip(),
            "created_at": now,
        }
        data.setdefault("events", {})[event_id] = event
        return event

    def set_status(
        self,
        workflow_id_or_prefix: str,
        status: str,
        *,
        actor: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        workflow_id = self.resolve_workflow_id(workflow_id_or_prefix)
        normalized = _normalize_status(status)
        now = _now()
        with _locked_store(self.path):
            data = self._read_unlocked()
            workflow = data["workflows"].get(workflow_id)
            if not workflow:
                raise ValueError(f"workflow not found: {workflow_id_or_prefix}")
            workflow["status"] = normalized
            workflow["updated_at"] = now
            workflow["last_changed_at"] = now
            workflow["last_changed_by"] = (actor or "").strip()
            if reason:
                workflow.setdefault("notes", []).append({"at": now, "text": str(reason).strip()})
            self._append_event_unlocked(
                data,
                workflow_id,
                "resume" if normalized == "enabled" else "kill" if normalized == "disabled" else normalized,
                actor=actor,
                reason=reason,
            )
            self._write_unlocked(data)
            return dict(workflow)

    def kill_workflow(
        self,
        workflow_id_or_prefix: str,
        *,
        actor: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return self.set_status(
            workflow_id_or_prefix,
            "disabled",
            actor=actor,
            reason=reason or "operator kill switch",
        )

    def resume_workflow(
        self,
        workflow_id_or_prefix: str,
        *,
        actor: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return self.set_status(
            workflow_id_or_prefix,
            "enabled",
            actor=actor,
            reason=reason or "operator resume",
        )

    def is_enabled(self, workflow_id_or_prefix: str) -> bool:
        return self.get_workflow(workflow_id_or_prefix).get("status") == "enabled"

    def create_report(self, *, title: str | None = None) -> dict[str, Any]:
        data = self.read()
        report_title = (title or "Hermes Workflow Registry").strip()
        report_id = f"workflow_report_{time.strftime('%Y%m%d')}_{_slug(report_title, max_len=36)}"
        reports_dir = self.path.parent / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_path = reports_dir / f"{report_id}.md"
        report_path.write_text(render_workflow_report(data, title=report_title), encoding="utf-8")
        report = {
            "id": report_id,
            "title": report_title,
            "path": str(report_path),
            "created_at": _now(),
        }
        with _locked_store(self.path):
            latest = self._read_unlocked()
            latest.setdefault("reports", {})[report_id] = report
            self._write_unlocked(latest)
        return report


def format_workflow(workflow: dict[str, Any]) -> str:
    status = str(workflow.get("status") or "unknown")
    title = str(workflow.get("title") or workflow.get("id") or "")
    line = f"{workflow.get('id', '')} | {status} | {title}"
    schedule = str(workflow.get("schedule") or "").strip()
    if schedule:
        line += f"\n  schedule: {schedule}"
    kill_switch = str(workflow.get("kill_switch_env") or "").strip()
    if kill_switch:
        line += f"\n  kill switch: {kill_switch}"
    health_check = str(workflow.get("health_check") or "").strip()
    if health_check:
        line += f"\n  health: {health_check}"
    changed = _iso(workflow.get("last_changed_at") or workflow.get("updated_at"))
    if changed:
        line += f"\n  changed: {changed}"
    return line


def summarize_workflows(data: dict[str, Any]) -> dict[str, int]:
    counts = {status: 0 for status in sorted(VALID_WORKFLOW_STATUSES)}
    workflows = data.get("workflows", {})
    for workflow in workflows.values():
        status = str(workflow.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    counts["workflows"] = len(workflows)
    counts["events"] = len(data.get("events", {}))
    counts["reports"] = len(data.get("reports", {}))
    return counts


def format_workflow_status(data: dict[str, Any], *, limit: int = 20) -> str:
    counts = summarize_workflows(data)
    lines = [
        "Workflow Registry",
        (
            f"Workflows: {counts['workflows']} total "
            f"(enabled {counts.get('enabled', 0)}, disabled {counts.get('disabled', 0)}, "
            f"paused {counts.get('paused', 0)}, unknown {counts.get('unknown', 0)})"
        ),
        f"Events: {counts['events']} | Reports: {counts['reports']}",
    ]
    workflows = list(data.get("workflows", {}).values())
    workflows.sort(key=lambda item: (str(item.get("category") or ""), str(item.get("id") or "")))
    if workflows:
        lines.append("")
        lines.append("Tracked workflows:")
        for workflow in workflows[: max(1, min(int(limit or 20), 100))]:
            lines.append("- " + format_workflow(workflow).replace("\n", "\n  "))
    return "\n".join(lines)


def render_workflow_report(data: dict[str, Any], *, title: str) -> str:
    counts = summarize_workflows(data)
    lines = [
        f"# {title}",
        "",
        f"Generated: {_iso(_now())}",
        "",
        "## Counts",
        "",
        f"- Workflows: {counts['workflows']}",
        f"- Enabled: {counts.get('enabled', 0)}",
        f"- Disabled: {counts.get('disabled', 0)}",
        f"- Events: {counts['events']}",
        "",
        "## Workflows",
        "",
    ]
    workflows = list(data.get("workflows", {}).values())
    workflows.sort(key=lambda item: (str(item.get("category") or ""), str(item.get("id") or "")))
    for workflow in workflows:
        lines.append(f"- `{workflow.get('id')}` [{workflow.get('status')}] {workflow.get('title')}")
        if workflow.get("kill_switch_env"):
            lines.append(f"  Kill switch: `{workflow.get('kill_switch_env')}`")
        if workflow.get("health_check"):
            lines.append(f"  Health: {workflow.get('health_check')}")
    lines.extend(["", "## Recent Events", ""])
    events = list(data.get("events", {}).values())
    events.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
    if not events:
        lines.append("- None")
    for event in events[:25]:
        lines.append(
            f"- `{event.get('id')}` {event.get('workflow_id')} {event.get('action')} "
            f"by {event.get('actor') or 'unknown'}"
        )
        if event.get("reason"):
            lines.append(f"  Reason: {event.get('reason')}")
    return "\n".join(lines) + "\n"
