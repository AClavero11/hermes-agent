"""Durable Hermes Workspace control plane.

The Workspace is intentionally file-backed so it works across the current
gateway runtimes without requiring a state.db migration.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None


WORKSPACE_SCHEMA_VERSION = 1
VALID_TASK_STATUSES = {
    "open",
    "pending",
    "in_progress",
    "blocked",
    "done",
    "cancelled",
}
ACTIVE_TASK_STATUSES = {"open", "pending", "in_progress", "blocked"}
_THREAD_LOCK = threading.RLock()


def workspace_root() -> Path:
    override = os.getenv("HERMES_WORKSPACE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return get_hermes_home() / "workspace"


def workspace_store_path() -> Path:
    override = os.getenv("HERMES_WORKSPACE_STORE", "").strip()
    if override:
        return Path(override).expanduser()
    return workspace_root() / "control_plane.json"


def _now() -> float:
    return time.time()


def _iso(ts: float | None) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))


def _slug(value: str, *, default: str = "item", max_len: int = 48) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower()).strip("-")
    text = text or default
    return text[:max_len].strip("-") or default


def _new_id(prefix: str, title: str = "") -> str:
    stamp = time.strftime("%Y%m%d")
    slug = _slug(title, default=prefix, max_len=36)
    return f"{prefix}_{stamp}_{slug}_{uuid.uuid4().hex[:6]}"


def _coerce_limit(value: Any, default: int = 20, maximum: int = 100) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = default
    return max(1, min(limit, maximum))


def _normalize_status(status: str | None, *, default: str = "open") -> str:
    normalized = (status or default).strip().lower().replace("-", "_")
    if normalized in {"complete", "completed"}:
        normalized = "done"
    if normalized in {"doing", "progress"}:
        normalized = "in_progress"
    if normalized not in VALID_TASK_STATUSES:
        raise ValueError(f"invalid status: {status}")
    return normalized


def _empty_store() -> dict[str, Any]:
    now = _now()
    return {
        "version": WORKSPACE_SCHEMA_VERSION,
        "created_at": now,
        "updated_at": now,
        "tasks": {},
        "evidence": {},
        "inbox": {},
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


class WorkspaceStore:
    """Persistent task, evidence, inbox, and report store."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else workspace_store_path()

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _empty_store()
        except json.JSONDecodeError as exc:
            raise ValueError(f"workspace store is not valid JSON: {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            return _empty_store()
        raw.setdefault("version", WORKSPACE_SCHEMA_VERSION)
        raw.setdefault("created_at", _now())
        raw.setdefault("updated_at", raw.get("created_at") or _now())
        raw.setdefault("tasks", {})
        raw.setdefault("evidence", {})
        raw.setdefault("inbox", {})
        raw.setdefault("reports", {})
        return raw

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        data["version"] = WORKSPACE_SCHEMA_VERSION
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

    def read(self) -> dict[str, Any]:
        with _locked_store(self.path):
            return self._read_unlocked()

    def create_task(
        self,
        title: str,
        *,
        owner: str | None = None,
        priority: str | None = None,
        project: str | None = None,
        source: str | None = None,
        next_action: str | None = None,
        status: str = "open",
        note: str | None = None,
    ) -> dict[str, Any]:
        title = (title or "").strip()
        if not title:
            raise ValueError("task title is required")
        now = _now()
        task_id = _new_id("task", title)
        task = {
            "id": task_id,
            "title": title,
            "status": _normalize_status(status),
            "owner": (owner or "").strip(),
            "priority": (priority or "").strip(),
            "project": (project or "").strip(),
            "source": (source or "").strip(),
            "next_action": (next_action or "").strip(),
            "notes": [],
            "evidence_ids": [],
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        if note:
            task["notes"].append({"at": now, "text": str(note).strip()})
        with _locked_store(self.path):
            data = self._read_unlocked()
            data["tasks"][task_id] = task
            self._write_unlocked(data)
        return task

    def resolve_task_id(self, task_id_or_prefix: str) -> str:
        raw = (task_id_or_prefix or "").strip()
        if not raw:
            raise ValueError("task_id is required")
        data = self.read()
        tasks = data.get("tasks", {})
        if raw in tasks:
            return raw
        matches = [task_id for task_id in tasks if task_id.startswith(raw)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(f"task not found: {raw}")
        raise ValueError(f"ambiguous task id prefix: {raw}")

    def get_task(self, task_id_or_prefix: str) -> dict[str, Any]:
        task_id = self.resolve_task_id(task_id_or_prefix)
        task = self.read()["tasks"].get(task_id)
        if not task:
            raise ValueError(f"task not found: {task_id_or_prefix}")
        return task

    def list_tasks(
        self,
        *,
        status: str | None = None,
        include_done: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        data = self.read()
        tasks = list(data.get("tasks", {}).values())
        if status:
            normalized = _normalize_status(status)
            tasks = [task for task in tasks if task.get("status") == normalized]
        elif not include_done:
            tasks = [
                task for task in tasks
                if task.get("status") in ACTIVE_TASK_STATUSES
            ]
        tasks.sort(key=lambda item: float(item.get("updated_at") or 0), reverse=True)
        return tasks[:_coerce_limit(limit)]

    def update_task(
        self,
        task_id_or_prefix: str,
        *,
        status: str | None = None,
        owner: str | None = None,
        priority: str | None = None,
        project: str | None = None,
        next_action: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        task_id = self.resolve_task_id(task_id_or_prefix)
        now = _now()
        with _locked_store(self.path):
            data = self._read_unlocked()
            task = data["tasks"].get(task_id)
            if not task:
                raise ValueError(f"task not found: {task_id_or_prefix}")
            if status is not None:
                normalized = _normalize_status(status)
                task["status"] = normalized
                task["completed_at"] = now if normalized == "done" else None
            if owner is not None:
                task["owner"] = str(owner).strip()
            if priority is not None:
                task["priority"] = str(priority).strip()
            if project is not None:
                task["project"] = str(project).strip()
            if next_action is not None:
                task["next_action"] = str(next_action).strip()
            if note:
                task.setdefault("notes", []).append({"at": now, "text": str(note).strip()})
            task["updated_at"] = now
            self._write_unlocked(data)
            return task

    def add_evidence(
        self,
        *,
        task_id: str | None = None,
        title: str | None = None,
        locator: str | None = None,
        summary: str | None = None,
        kind: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved_task_id = self.resolve_task_id(task_id) if task_id else ""
        label = title or locator or summary or "evidence"
        evidence_id = _new_id("ev", label)
        now = _now()
        evidence = {
            "id": evidence_id,
            "task_id": resolved_task_id,
            "title": (title or "").strip(),
            "locator": (locator or "").strip(),
            "summary": (summary or "").strip(),
            "kind": (kind or _infer_evidence_kind(locator or summary or "")).strip(),
            "metadata": metadata or {},
            "created_at": now,
        }
        with _locked_store(self.path):
            data = self._read_unlocked()
            data["evidence"][evidence_id] = evidence
            if resolved_task_id:
                task = data["tasks"].get(resolved_task_id)
                if not task:
                    raise ValueError(f"task not found: {resolved_task_id}")
                task.setdefault("evidence_ids", []).append(evidence_id)
                task["updated_at"] = now
            self._write_unlocked(data)
        return evidence

    def add_inbox_item(
        self,
        text: str,
        *,
        source: str | None = None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            raise ValueError("inbox text is required")
        item_id = _new_id("inbox", title or text)
        now = _now()
        item = {
            "id": item_id,
            "title": (title or text[:80]).strip(),
            "text": text,
            "source": (source or "").strip(),
            "status": "new",
            "metadata": metadata or {},
            "created_at": now,
            "updated_at": now,
        }
        with _locked_store(self.path):
            data = self._read_unlocked()
            data["inbox"][item_id] = item
            self._write_unlocked(data)
        return item

    def create_report(self, *, title: str | None = None) -> dict[str, Any]:
        data = self.read()
        report_title = (title or "Hermes Workspace Report").strip()
        report_id = _new_id("report", report_title)
        reports_dir = self.path.parent / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_path = reports_dir / f"{report_id}.md"
        content = render_report(data, title=report_title)
        report_path.write_text(content, encoding="utf-8")
        now = _now()
        report = {
            "id": report_id,
            "title": report_title,
            "path": str(report_path),
            "created_at": now,
            "summary": summarize_counts(data),
        }
        with _locked_store(self.path):
            latest = self._read_unlocked()
            latest["reports"][report_id] = report
            self._write_unlocked(latest)
        return report


def _infer_evidence_kind(text: str) -> str:
    value = (text or "").strip().lower()
    if value.startswith(("http://", "https://")):
        return "url"
    if value.startswith("/") or value.startswith("~"):
        return "file"
    return "note"


def summarize_counts(data: dict[str, Any]) -> dict[str, int]:
    tasks = data.get("tasks", {})
    counts = {status: 0 for status in sorted(VALID_TASK_STATUSES)}
    for task in tasks.values():
        status = task.get("status") or "open"
        counts[status] = counts.get(status, 0) + 1
    counts["active"] = sum(counts.get(status, 0) for status in ACTIVE_TASK_STATUSES)
    counts["tasks"] = len(tasks)
    counts["evidence"] = len(data.get("evidence", {}))
    counts["inbox"] = len(data.get("inbox", {}))
    counts["reports"] = len(data.get("reports", {}))
    return counts


def format_task(task: dict[str, Any]) -> str:
    parts = [
        task.get("id", ""),
        task.get("status", "open"),
        task.get("title", ""),
    ]
    line = " | ".join(part for part in parts if part)
    next_action = (task.get("next_action") or "").strip()
    if next_action:
        line += f"\n  next: {next_action}"
    evidence_count = len(task.get("evidence_ids") or [])
    if evidence_count:
        line += f"\n  evidence: {evidence_count}"
    updated = _iso(task.get("updated_at"))
    if updated:
        line += f"\n  updated: {updated}"
    return line


def format_workspace_status(
    data: dict[str, Any],
    *,
    swarm: dict[str, Any] | None = None,
    limit: int = 10,
) -> str:
    counts = summarize_counts(data)
    lines = [
        "Workspace",
        (
            f"Tasks: {counts['active']} active / {counts['tasks']} total "
            f"(open {counts.get('open', 0)}, in_progress {counts.get('in_progress', 0)}, "
            f"blocked {counts.get('blocked', 0)}, done {counts.get('done', 0)})"
        ),
        f"Evidence: {counts['evidence']} | Inbox: {counts['inbox']} | Reports: {counts['reports']}",
    ]
    if swarm:
        swarm_parts = [
            f"agents {int(swarm.get('running_agents') or 0)}",
            f"processes {int(swarm.get('running_processes') or 0)}",
            f"goals {int(swarm.get('goal_continuations') or 0)}",
        ]
        lines.append("Swarm: " + " | ".join(swarm_parts))
    active_tasks = [
        task for task in data.get("tasks", {}).values()
        if task.get("status") in ACTIVE_TASK_STATUSES
    ]
    active_tasks.sort(key=lambda item: float(item.get("updated_at") or 0), reverse=True)
    if active_tasks:
        lines.append("")
        lines.append("Active tasks:")
        for task in active_tasks[:_coerce_limit(limit)]:
            lines.append("- " + format_task(task).replace("\n", "\n  "))
    else:
        lines.append("")
        lines.append("No active workspace tasks.")
    return "\n".join(lines)


def render_report(data: dict[str, Any], *, title: str) -> str:
    counts = summarize_counts(data)
    lines = [
        f"# {title}",
        "",
        f"Generated: {_iso(_now())}",
        "",
        "## Counts",
        "",
        f"- Active tasks: {counts['active']}",
        f"- Total tasks: {counts['tasks']}",
        f"- Evidence records: {counts['evidence']}",
        f"- Inbox items: {counts['inbox']}",
        "",
        "## Active Tasks",
        "",
    ]
    active = [
        task for task in data.get("tasks", {}).values()
        if task.get("status") in ACTIVE_TASK_STATUSES
    ]
    active.sort(key=lambda item: float(item.get("updated_at") or 0), reverse=True)
    if not active:
        lines.append("- None")
    for task in active:
        lines.append(f"- `{task.get('id')}` [{task.get('status')}] {task.get('title')}")
        if task.get("next_action"):
            lines.append(f"  Next: {task.get('next_action')}")
    lines.extend(["", "## Blocked", ""])
    blocked = [task for task in active if task.get("status") == "blocked"]
    if not blocked:
        lines.append("- None")
    for task in blocked:
        lines.append(f"- `{task.get('id')}` {task.get('title')}")
        if task.get("next_action"):
            lines.append(f"  Blocker/next: {task.get('next_action')}")
    lines.extend(["", "## Recent Evidence", ""])
    evidence = list(data.get("evidence", {}).values())
    evidence.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
    if not evidence:
        lines.append("- None")
    for item in evidence[:25]:
        label = item.get("title") or item.get("locator") or item.get("summary") or item.get("id")
        lines.append(f"- `{item.get('id')}` {label}")
        if item.get("task_id"):
            lines.append(f"  Task: `{item.get('task_id')}`")
    return "\n".join(lines) + "\n"
