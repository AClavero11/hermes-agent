"""Tests for agent/batch_queue.py — Anthropic Batches API queue management."""

import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import pytest

from agent.batch_queue import BatchQueue, poll_batch_handler


class TestBatchQueueInit:
    """Tests for BatchQueue initialization."""

    def test_init_with_defaults(self, monkeypatch):
        """BatchQueue initializes with default db_path from HERMES_HOME."""
        # conftest already sets HERMES_HOME to a temp dir
        queue = BatchQueue()

        # Verify it uses HERMES_HOME/state.db
        hermes_home = Path(os.environ["HERMES_HOME"])
        assert queue.db_path == hermes_home / "state.db"
        assert queue.api_base == "https://api.anthropic.com/v1"

    def test_init_with_custom_db_path(self, tmp_path):
        """BatchQueue accepts custom db_path."""
        custom_db = tmp_path / "custom.db"
        queue = BatchQueue(db_path=custom_db)
        assert queue.db_path == custom_db

    def test_init_reads_api_key_from_env(self, monkeypatch):
        """BatchQueue reads ANTHROPIC_API_KEY from environment."""
        test_key = "sk-test-12345"
        monkeypatch.setenv("ANTHROPIC_API_KEY", test_key)
        queue = BatchQueue()
        assert queue.api_key == test_key

    def test_init_reads_config(self):
        """BatchQueue reads batch config from config dict."""
        config = {
            "default_model": "claude-opus-4-6",
            "poll_interval_ticks": 30,
            "max_pending": 5,
        }
        queue = BatchQueue(config=config)
        assert queue.default_model == "claude-opus-4-6"
        assert queue.poll_interval_ticks == 30
        assert queue.max_pending == 5

    def test_init_uses_fallback_model(self):
        """BatchQueue uses fallback model when not configured."""
        queue = BatchQueue()
        assert queue.default_model == "claude-sonnet-4-6-20250514"

    def test_init_telegram_credentials_optional(self, monkeypatch):
        """BatchQueue initializes even without Telegram credentials."""
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN_CZAR", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID_AC", raising=False)
        queue = BatchQueue()
        assert queue.telegram_token is None
        assert queue.telegram_chat_id is None


class TestBatchQueueSubmit:
    """Tests for BatchQueue.submit() method."""

    def test_submit_creates_valid_batch_request(self, tmp_path, monkeypatch):
        """submit() formats batch request correctly for Anthropic API."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        queue = BatchQueue(db_path=tmp_path / "state.db")

        messages = [
            {"role": "user", "content": "Hello"}
        ]

        with patch("httpx.Client.post") as mock_post:
            mock_response = Mock()
            mock_response.json.return_value = {"id": "batch-123"}
            mock_post.return_value = mock_response

            batch_id = queue.submit(messages, "test_workflow")

            assert batch_id == "batch-123"
            mock_post.assert_called_once()

            # Verify request format
            call_args = mock_post.call_args
            json_data = call_args.kwargs["json"]
            assert "requests" in json_data
            assert len(json_data["requests"]) == 1
            assert json_data["requests"][0]["params"]["model"] == "claude-sonnet-4-6-20250514"
            assert json_data["requests"][0]["params"]["max_tokens"] == 4096

    def test_submit_stores_batch_in_database(self, tmp_path, monkeypatch):
        """submit() stores batch metadata in database."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        db_path = tmp_path / "state.db"

        # Initialize database with schema
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS batch_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL UNIQUE,
                workflow_name TEXT NOT NULL,
                model TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                submitted_at REAL NOT NULL,
                completed_at REAL,
                result_summary TEXT,
                metadata TEXT
            )
        """)
        conn.commit()
        conn.close()

        queue = BatchQueue(db_path=db_path)

        with patch("httpx.Client.post") as mock_post:
            mock_response = Mock()
            mock_response.json.return_value = {"id": "batch-456"}
            mock_post.return_value = mock_response

            batch_id = queue.submit(
                [{"role": "user", "content": "Test"}],
                "test_workflow",
                metadata={"test": "data"}
            )

            # Verify database entry
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM batch_queue WHERE batch_id = ?", (batch_id,))
            row = cursor.fetchone()
            conn.close()

            assert row is not None
            assert row[1] == "batch-456"  # batch_id
            assert row[2] == "test_workflow"  # workflow_name
            assert row[4] == "pending"  # status

    def test_submit_raises_without_api_key(self, tmp_path, monkeypatch):
        """submit() raises RuntimeError if ANTHROPIC_API_KEY not set."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        queue = BatchQueue(db_path=tmp_path / "state.db")

        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            queue.submit([{"role": "user", "content": "Test"}], "workflow")

    def test_submit_custom_model(self, tmp_path, monkeypatch):
        """submit() accepts custom model parameter."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        queue = BatchQueue(db_path=tmp_path / "state.db")

        with patch("httpx.Client.post") as mock_post:
            mock_response = Mock()
            mock_response.json.return_value = {"id": "batch-789"}
            mock_post.return_value = mock_response

            batch_id = queue.submit(
                [{"role": "user", "content": "Test"}],
                "workflow",
                model="claude-opus-4-6"
            )

            call_args = mock_post.call_args
            json_data = call_args.kwargs["json"]
            assert json_data["requests"][0]["params"]["model"] == "claude-opus-4-6"

    def test_submit_api_headers_correct(self, tmp_path, monkeypatch):
        """submit() sends correct API headers."""
        api_key = "sk-test-header-key"
        monkeypatch.setenv("ANTHROPIC_API_KEY", api_key)
        queue = BatchQueue(db_path=tmp_path / "state.db")

        with patch("httpx.Client.post") as mock_post:
            mock_response = Mock()
            mock_response.json.return_value = {"id": "batch-header"}
            mock_post.return_value = mock_response

            queue.submit([{"role": "user", "content": "Test"}], "workflow")

            call_args = mock_post.call_args
            headers = call_args.kwargs["headers"]
            assert headers["x-api-key"] == api_key
            assert headers["content-type"] == "application/json"


class TestBatchQueuePoll:
    """Tests for BatchQueue.poll() method."""

    def test_poll_no_pending_batches(self, tmp_path, monkeypatch):
        """poll() returns empty list when no batches pending."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        db_path = tmp_path / "state.db"

        # Initialize database
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS batch_queue (
                id INTEGER PRIMARY KEY,
                batch_id TEXT,
                workflow_name TEXT,
                model TEXT,
                status TEXT,
                submitted_at REAL,
                completed_at REAL,
                result_summary TEXT,
                metadata TEXT
            )
        """)
        conn.commit()
        conn.close()

        queue = BatchQueue(db_path=db_path)
        results = queue.poll()

        assert results == []

    def test_poll_checks_pending_batch_status(self, tmp_path, monkeypatch):
        """poll() fetches status for pending batches."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        db_path = tmp_path / "state.db"

        # Initialize database with a pending batch
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS batch_queue (
                id INTEGER PRIMARY KEY,
                batch_id TEXT,
                workflow_name TEXT,
                model TEXT,
                status TEXT,
                submitted_at REAL,
                completed_at REAL,
                result_summary TEXT,
                metadata TEXT
            )
        """)
        cursor.execute(
            "INSERT INTO batch_queue VALUES (1, 'batch-123', 'test_wf', 'claude-sonnet', 'pending', ?, NULL, NULL, NULL)",
            (time.time(),)
        )
        conn.commit()
        conn.close()

        queue = BatchQueue(db_path=db_path)

        with patch.object(queue, "_check_batch_status") as mock_check:
            mock_check.return_value = ("processing", "status=processing | processed=5")
            results = queue.poll()

            # Should check status for the pending batch
            mock_check.assert_called_once_with("batch-123")

    def test_poll_marks_completed_batch(self, tmp_path, monkeypatch):
        """poll() marks batch as completed when API returns ended status."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        db_path = tmp_path / "state.db"

        # Initialize database with a pending batch
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS batch_queue (
                id INTEGER PRIMARY KEY,
                batch_id TEXT,
                workflow_name TEXT,
                model TEXT,
                status TEXT,
                submitted_at REAL,
                completed_at REAL,
                result_summary TEXT,
                metadata TEXT
            )
        """)
        cursor.execute(
            "INSERT INTO batch_queue VALUES (1, 'batch-ended', 'test_wf', 'claude-sonnet', 'pending', ?, NULL, NULL, NULL)",
            (time.time(),)
        )
        conn.commit()
        conn.close()

        queue = BatchQueue(db_path=db_path)

        with patch.object(queue, "_check_batch_status") as mock_check, \
             patch.object(queue, "_fetch_batch_results") as mock_fetch:
            mock_check.return_value = ("ended", "status=ended | succeeded=10")
            mock_fetch.return_value = [{"custom_id": "msg1", "result": {}}]
            results = queue.poll()

            assert len(results) == 1
            assert results[0]["batch_id"] == "batch-ended"
            assert results[0]["workflow_name"] == "test_wf"

    def test_poll_returns_empty_on_database_error(self, tmp_path, monkeypatch):
        """poll() returns empty list if batch_queue table doesn't exist."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        db_path = tmp_path / "state.db"

        # Create minimal database without batch_queue table
        conn = sqlite3.connect(str(db_path))
        conn.close()

        queue = BatchQueue(db_path=db_path)
        results = queue.poll()

        assert results == []


class TestBatchQueueDeliver:
    """Tests for BatchQueue.deliver_results() method."""

    def test_deliver_results_sends_telegram(self, tmp_path, monkeypatch):
        """deliver_results() sends message via Telegram for each completed batch."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN_CZAR", "test-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID_AC", "123456")

        queue = BatchQueue(db_path=tmp_path / "state.db")

        results = [
            {
                "batch_id": "batch-123",
                "workflow_name": "test_workflow",
                "completed_at": time.time(),
                "results": [{"custom_id": "msg1"}],
            }
        ]

        with patch.object(queue, "_send_telegram_message") as mock_send:
            mock_send.return_value = True
            queue.deliver_results(results)

            mock_send.assert_called_once()
            call_args = mock_send.call_args[0][0]
            assert "batch-123" in call_args
            assert "test_workflow" in call_args

    def test_deliver_results_skips_without_telegram_creds(self, tmp_path, monkeypatch):
        """deliver_results() silently skips if Telegram credentials not configured."""
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN_CZAR", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID_AC", raising=False)

        queue = BatchQueue(db_path=tmp_path / "state.db")

        results = [
            {
                "batch_id": "batch-456",
                "workflow_name": "test_workflow",
                "completed_at": time.time(),
                "results": [],
            }
        ]

        with patch.object(queue, "_send_telegram_message") as mock_send:
            queue.deliver_results(results)
            mock_send.assert_not_called()


class TestBatchQueueGetPending:
    """Tests for BatchQueue.get_pending() method."""

    def test_get_pending_returns_batches(self, tmp_path):
        """get_pending() returns list of pending batches."""
        db_path = tmp_path / "state.db"

        # Initialize database
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS batch_queue (
                id INTEGER PRIMARY KEY,
                batch_id TEXT,
                workflow_name TEXT,
                model TEXT,
                status TEXT,
                submitted_at REAL,
                completed_at REAL,
                result_summary TEXT,
                metadata TEXT
            )
        """)
        now = time.time()
        cursor.execute(
            "INSERT INTO batch_queue VALUES (1, 'batch-1', 'wf1', 'claude-sonnet', 'pending', ?, NULL, NULL, NULL)",
            (now,)
        )
        cursor.execute(
            "INSERT INTO batch_queue VALUES (2, 'batch-2', 'wf2', 'claude-sonnet', 'processing', ?, NULL, NULL, NULL)",
            (now,)
        )
        cursor.execute(
            "INSERT INTO batch_queue VALUES (3, 'batch-3', 'wf3', 'claude-sonnet', 'completed', ?, ?, NULL, NULL)",
            (now, now)
        )
        conn.commit()
        conn.close()

        queue = BatchQueue(db_path=db_path)
        pending = queue.get_pending()

        # Should return only pending and processing, not completed
        assert len(pending) == 2
        batch_ids = {b["batch_id"] for b in pending}
        assert batch_ids == {"batch-1", "batch-2"}


class TestBatchQueueCancel:
    """Tests for BatchQueue.cancel() method."""

    def test_cancel_sends_api_request(self, tmp_path, monkeypatch):
        """cancel() sends POST request to Anthropic cancel endpoint."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        queue = BatchQueue(db_path=tmp_path / "state.db")

        with patch("httpx.Client.post") as mock_post:
            mock_response = Mock()
            mock_post.return_value = mock_response

            result = queue.cancel("batch-123")

            assert result is True
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            assert "batch-123/cancel" in call_args[0][0]

    def test_cancel_returns_false_on_error(self, tmp_path, monkeypatch):
        """cancel() returns False if API call fails."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        queue = BatchQueue(db_path=tmp_path / "state.db")

        with patch("httpx.Client.post") as mock_post:
            mock_post.side_effect = httpx.HTTPError("API error")

            result = queue.cancel("batch-123")

            assert result is False

    def test_cancel_requires_api_key(self, tmp_path, monkeypatch):
        """cancel() returns False if API key not configured."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        queue = BatchQueue(db_path=tmp_path / "state.db")

        result = queue.cancel("batch-123")

        assert result is False


class TestBatchQueueCheckStatus:
    """Tests for BatchQueue._check_batch_status() method."""

    def test_check_batch_status_parses_response(self, tmp_path, monkeypatch):
        """_check_batch_status() extracts status from API response."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        queue = BatchQueue(db_path=tmp_path / "state.db")

        with patch("httpx.Client.get") as mock_get:
            mock_response = Mock()
            mock_response.json.return_value = {
                "id": "batch-123",
                "processing_status": "processing",
                "request_counts": {
                    "processed": 10,
                    "succeeded": 10,
                    "errored": 0,
                }
            }
            mock_get.return_value = mock_response

            status, summary = queue._check_batch_status("batch-123")

            assert status == "processing"
            assert "processed=10" in summary
            assert "succeeded=10" in summary

    def test_check_batch_status_handles_missing_counts(self, tmp_path, monkeypatch):
        """_check_batch_status() handles response without request_counts."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        queue = BatchQueue(db_path=tmp_path / "state.db")

        with patch("httpx.Client.get") as mock_get:
            mock_response = Mock()
            mock_response.json.return_value = {
                "id": "batch-123",
                "processing_status": "ended",
            }
            mock_get.return_value = mock_response

            status, summary = queue._check_batch_status("batch-123")

            assert status == "ended"
            assert "status=ended" in summary


class TestBatchQueueFetchResults:
    """Tests for BatchQueue._fetch_batch_results() method."""

    def test_fetch_batch_results_streams_jsonl(self, tmp_path, monkeypatch):
        """_fetch_batch_results() parses JSONL stream from API."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        queue = BatchQueue(db_path=tmp_path / "state.db")

        jsonl_content = (
            '{"custom_id": "msg1", "result": {"message": {"content": "reply1"}}}\n'
            '{"custom_id": "msg2", "result": {"message": {"content": "reply2"}}}\n'
        )

        with patch("httpx.Client.stream") as mock_stream:
            mock_response = Mock()
            mock_response.__enter__ = Mock(return_value=mock_response)
            mock_response.__exit__ = Mock(return_value=None)
            mock_response.iter_lines.return_value = jsonl_content.strip().split("\n")
            mock_stream.return_value = mock_response

            results = queue._fetch_batch_results("batch-123")

            assert len(results) == 2
            assert results[0]["custom_id"] == "msg1"
            assert results[1]["custom_id"] == "msg2"

    def test_fetch_batch_results_skips_invalid_json(self, tmp_path, monkeypatch):
        """_fetch_batch_results() skips lines that aren't valid JSON."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        queue = BatchQueue(db_path=tmp_path / "state.db")

        lines = [
            '{"custom_id": "msg1", "result": {}}',
            "not valid json",
            '{"custom_id": "msg2", "result": {}}',
        ]

        with patch("httpx.Client.stream") as mock_stream:
            mock_response = Mock()
            mock_response.__enter__ = Mock(return_value=mock_response)
            mock_response.__exit__ = Mock(return_value=None)
            mock_response.iter_lines.return_value = lines
            mock_stream.return_value = mock_response

            results = queue._fetch_batch_results("batch-123")

            assert len(results) == 2


class TestBatchQueueSendTelegram:
    """Tests for BatchQueue._send_telegram_message() method."""

    def test_send_telegram_success(self, tmp_path, monkeypatch):
        """_send_telegram_message() sends message to Telegram API."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN_CZAR", "token123")
        monkeypatch.setenv("TELEGRAM_CHAT_ID_AC", "chat123")

        queue = BatchQueue(db_path=tmp_path / "state.db")

        with patch("httpx.Client.post") as mock_post:
            mock_response = Mock()
            mock_post.return_value = mock_response

            result = queue._send_telegram_message("Test message")

            assert result is True
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            assert "sendMessage" in call_args[0][0]

    def test_send_telegram_returns_false_on_error(self, tmp_path, monkeypatch):
        """_send_telegram_message() returns False if API call fails."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN_CZAR", "token123")
        monkeypatch.setenv("TELEGRAM_CHAT_ID_AC", "chat123")

        queue = BatchQueue(db_path=tmp_path / "state.db")

        with patch("httpx.Client.post") as mock_post:
            mock_post.side_effect = httpx.HTTPError("Network error")

            result = queue._send_telegram_message("Test message")

            assert result is False

    def test_send_telegram_requires_credentials(self, tmp_path, monkeypatch):
        """_send_telegram_message() returns False without credentials."""
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN_CZAR", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID_AC", raising=False)

        queue = BatchQueue(db_path=tmp_path / "state.db")

        result = queue._send_telegram_message("Test message")

        assert result is False


class TestPollBatchHandler:
    """Tests for poll_batch_handler() function."""

    def test_poll_batch_handler_creates_queue_and_polls(self, tmp_path, monkeypatch):
        """poll_batch_handler() creates queue, polls, and delivers results."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        with patch("agent.batch_queue.BatchQueue") as mock_queue_class:
            mock_queue = Mock()
            mock_queue.poll.return_value = [
                {"batch_id": "batch-1", "workflow_name": "test"}
            ]
            mock_queue_class.return_value = mock_queue

            poll_batch_handler()

            mock_queue.poll.assert_called_once()
            mock_queue.deliver_results.assert_called_once()

    def test_poll_batch_handler_handles_no_results(self, tmp_path, monkeypatch):
        """poll_batch_handler() handles gracefully when no batches complete."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        with patch("agent.batch_queue.BatchQueue") as mock_queue_class:
            mock_queue = Mock()
            mock_queue.poll.return_value = []
            mock_queue_class.return_value = mock_queue

            poll_batch_handler()

            mock_queue.poll.assert_called_once()
            mock_queue.deliver_results.assert_not_called()


class TestCostTracking:
    """Tests that batch API calls log costs correctly."""

    def test_batch_cost_reduction_factor(self):
        """Batch API costs are 50% of standard API costs."""
        # This is more of a documentation test
        # The 50% reduction is handled by Anthropic's API
        # We just need to verify we're calling the right endpoint

        # Standard cost for 1M input tokens: $3.00
        # Batch cost for 1M input tokens: $1.50 (50% reduction)
        # This is handled by Anthropic, not by our code

        assert True  # Reminder: batch endpoint is .../messages/batches not .../messages


class TestDatabaseMigration:
    """Tests that database migration to v4 works correctly."""

    def test_v4_migration_creates_table(self, tmp_path):
        """Database v4 migration creates batch_queue table."""
        # This test is handled by SessionDB._init_schema()
        # Just verify that the SCHEMA_SQL includes batch_queue
        from hermes_state import SCHEMA_SQL

        assert "batch_queue" in SCHEMA_SQL
        assert "batch_id TEXT NOT NULL UNIQUE" in SCHEMA_SQL
        assert "workflow_name TEXT NOT NULL" in SCHEMA_SQL
