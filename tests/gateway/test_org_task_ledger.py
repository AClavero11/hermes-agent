"""Tests for the durable org task ledger."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_state import SCHEMA_SQL, SessionDB
from tools.org_task_ledger_tool import org_task_ledger_tool


def _decode_tool_result(raw: str) -> dict:
    return json.loads(raw)


def _create_legacy_state_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (8,))
    conn.execute(
        """INSERT INTO sessions
           (id, source, user_id, model, started_at, message_count)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("legacy-session", "telegram", "user-1", "test/model", 1000.0, 1),
    )
    conn.execute(
        """INSERT INTO messages
           (session_id, role, content, timestamp)
           VALUES (?, ?, ?, ?)""",
        ("legacy-session", "user", "existing message survives", 1001.0),
    )
    conn.commit()
    conn.close()


def test_schema_migration_adds_org_tables_without_losing_sessions_messages(tmp_path):
    db_path = tmp_path / "state.db"
    _create_legacy_state_db(db_path)

    db = SessionDB(db_path=db_path)
    try:
        table_names = {
            row["name"]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "org_tasks" in table_names
        assert "org_events" in table_names

        session = db.get_session("legacy-session")
        assert session is not None
        assert session["source"] == "telegram"
        assert session["message_count"] == 1

        message = db._conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ?",
            ("legacy-session",),
        ).fetchone()
        assert dict(message) == {
            "role": "user",
            "content": "existing message survives",
        }

        task = db.create_org_task(
            source="migration-test",
            intent="prove new ledger works after migration",
            entities_json={"session": "legacy-session"},
            evidence_json={"message": "existing message survives"},
            owner="ops",
            next_action="verify",
        )
        assert task["source"] == "migration-test"
    finally:
        db.close()


def test_org_task_api_and_tool_crud_search_validate_json(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        api_task = db.create_org_task(
            source="telegram",
            intent="Quote 762367B for Turkish Technic",
            entities_json={"part": "762367B", "customer": "Turkish Technic"},
            evidence_json=[{"platform": "telegram", "message_id": "m-1"}],
            owner="ac",
            next_action="pull V11 pricing context",
        )
        assert api_task["status"] == "open"
        assert json.loads(api_task["entities_json"]) == {
            "customer": "Turkish Technic",
            "part": "762367B",
        }
        assert json.loads(api_task["evidence_json"]) == [
            {"message_id": "m-1", "platform": "telegram"}
        ]

        listed = db.list_org_tasks(status="open", owner="ac")
        assert [task["id"] for task in listed] == [api_task["id"]]

        updated = db.update_org_task(
            api_task["id"],
            status="in_progress",
            owner="sales",
            next_action="send quote draft for approval",
        )
        assert updated["status"] == "in_progress"
        assert updated["owner"] == "sales"

        searched = db.search_org_tasks("762367B")
        assert [task["id"] for task in searched] == [api_task["id"]]

        completed = db.complete_org_task(
            api_task["id"],
            next_action="quoted; wait for customer response",
        )
        assert completed["status"] == "completed"
        assert completed["completed_at"] is not None

        events = db.list_org_events(task_id=api_task["id"])
        assert [event["event_type"] for event in events] == [
            "created",
            "updated",
            "completed",
        ]

        with pytest.raises(ValueError):
            db.create_org_task(
                source="api",
                intent="bad entities payload",
                entities_json="{not valid json",
            )

        tool_created = _decode_tool_result(org_task_ledger_tool(
            action="create",
            db=db,
            source="api",
            intent="Route RFQ for 1234-5 to sales",
            entities_json={"part": "1234-5"},
            evidence_json={"request_id": "rfq-1"},
            owner="sales",
            next_action="triage RFQ",
        ))
        assert tool_created["success"] is True
        tool_task_id = tool_created["task"]["id"]

        tool_listed = _decode_tool_result(org_task_ledger_tool(
            action="list",
            db=db,
            status="open",
            owner="sales",
        ))
        assert tool_task_id in {task["id"] for task in tool_listed["tasks"]}

        tool_updated = _decode_tool_result(org_task_ledger_tool(
            action="update",
            db=db,
            task_id=tool_task_id,
            status="blocked",
            next_action="wait for buyer clarification",
        ))
        assert tool_updated["task"]["status"] == "blocked"

        tool_searched = _decode_tool_result(org_task_ledger_tool(
            action="search",
            db=db,
            query="1234-5",
        ))
        assert [task["id"] for task in tool_searched["tasks"]] == [tool_task_id]

        tool_completed = _decode_tool_result(org_task_ledger_tool(
            action="complete",
            db=db,
            task_id=tool_task_id,
            next_action="closed in RFQ system",
        ))
        assert tool_completed["task"]["status"] == "completed"

        tool_error = _decode_tool_result(org_task_ledger_tool(
            action="create",
            db=db,
            source="api",
            intent="bad evidence payload",
            evidence_json="{not valid json",
        ))
        assert tool_error["success"] is False
        assert "evidence_json must be valid JSON" in tool_error["error"]
    finally:
        db.close()


def test_state_machine_blocks_illegal_transitions(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        completed_task = db.create_org_task(
            source="test",
            intent="complete then reject reopening",
        )
        completed = db.complete_org_task(completed_task["id"])
        assert completed["status"] == "completed"

        with pytest.raises(ValueError):
            db.update_org_task(completed_task["id"], status="in_progress")

        completed_again = db.complete_org_task(completed_task["id"])
        assert completed_again == completed

        cancelled_task = db.create_org_task(
            source="test",
            intent="cancel then reject completion",
        )
        cancelled = db.update_org_task(cancelled_task["id"], status="cancelled")
        assert cancelled["status"] == "cancelled"

        with pytest.raises(ValueError):
            db.complete_org_task(cancelled_task["id"])
    finally:
        db.close()


def test_foreign_key_cascade_deletes_events(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db._conn.execute("PRAGMA foreign_keys=ON")
        task = db.create_org_task(
            source="test",
            intent="delete task and cascade events",
        )
        db.update_org_task(task["id"], status="pending")
        db.update_org_task(task["id"], next_action="verify cascade")

        db._conn.execute("DELETE FROM org_tasks WHERE id = ?", (task["id"],))
        event_count = db._conn.execute(
            "SELECT COUNT(*) FROM org_events WHERE task_id = ?",
            (task["id"],),
        ).fetchone()[0]
        assert event_count == 0
    finally:
        db.close()


def test_migration_idempotent(tmp_path):
    db_path = tmp_path / "state.db"

    first_db = SessionDB(db_path=db_path)
    first_db.close()

    second_db = SessionDB(db_path=db_path)
    second_db.close()


def test_concurrent_create_update_under_sqlite_wal(tmp_path):
    db_path = tmp_path / "state.db"
    bootstrap_db = SessionDB(db_path=db_path)
    try:
        journal_mode = bootstrap_db._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert journal_mode.lower() == "wal"
    finally:
        bootstrap_db.close()

    workers = 8
    tasks_per_worker = 12

    def worker(worker_id: int) -> list[str]:
        local_db = SessionDB(db_path=db_path)
        task_ids = []
        try:
            for index in range(tasks_per_worker):
                task = local_db.create_org_task(
                    source="thread",
                    intent=f"worker {worker_id} task {index}",
                    entities_json={"worker": worker_id, "index": index},
                    evidence_json={"concurrency": True},
                    owner=f"owner-{worker_id}",
                    next_action="created",
                )
                updated = local_db.update_org_task(
                    task["id"],
                    status="in_progress",
                    next_action=f"worker {worker_id} updated {index}",
                )
                assert updated["status"] == "in_progress"
                task_ids.append(task["id"])
            return task_ids
        finally:
            local_db.close()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        batches = list(executor.map(worker, range(workers)))

    task_ids = [task_id for batch in batches for task_id in batch]
    assert len(task_ids) == workers * tasks_per_worker
    assert len(set(task_ids)) == len(task_ids)

    final_db = SessionDB(db_path=db_path)
    try:
        tasks = final_db.list_org_tasks(status="in_progress", limit=200)
        assert len(tasks) == workers * tasks_per_worker
        assert {task["id"] for task in tasks} == set(task_ids)

        events = final_db.list_org_events(limit=500)
        assert len(events) == workers * tasks_per_worker * 2
    finally:
        final_db.close()
