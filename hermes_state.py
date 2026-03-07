#!/usr/bin/env python3
"""
SQLite State Store for Hermes Agent.

Provides persistent session storage with FTS5 full-text search, replacing
the per-session JSONL file approach. Stores session metadata, full message
history, and model configuration for CLI and gateway sessions.

Key design decisions:
- WAL mode for concurrent readers + one writer (gateway multi-platform)
- FTS5 virtual table for fast text search across all session messages
- Compression-triggered session splitting via parent_session_id chains
- Batch runner and RL trajectories are NOT stored here (separate systems)
- Session source tagging ('cli', 'telegram', 'discord', etc.) for filtering
"""

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Dict, Any, List, Optional


DEFAULT_DB_PATH = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes")) / "state.db"

SCHEMA_VERSION = 5

# Cost per model: input and output rates per 1M tokens (USD)
COST_PER_MODEL = {
    "claude-opus-4": {"input": 15.00, "output": 75.00},
    "claude-opus-4-20250805": {"input": 15.00, "output": 75.00},
    "claude-sonnet-4": {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5": {"input": 0.80, "output": 4.00},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    # OpenAI models (via openrouter)
    "o1": {"input": 15.00, "output": 60.00},
    "gpt-4": {"input": 0.03, "output": 0.06},
    "gpt-4-turbo": {"input": 0.01, "output": 0.03},
    "gpt-3.5-turbo": {"input": 0.0005, "output": 0.0015},
    # Nous models
    "nous-hermes-2-mixtral-8x7b-dpo": {"input": 0.54, "output": 0.81},
    "nous-hermes-2-vision": {"input": 0.54, "output": 0.81},
    "nous-hermes-2": {"input": 0.30, "output": 0.40},
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT
);

CREATE TABLE IF NOT EXISTS cost_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    model TEXT NOT NULL,
    workflow TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    cost_usd REAL NOT NULL,
    latency_ms INTEGER
);

CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_cost_log_session ON cost_log(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_cost_log_timestamp ON cost_log(timestamp DESC);

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
);

CREATE INDEX IF NOT EXISTS idx_batch_queue_status ON batch_queue(status);
CREATE INDEX IF NOT EXISTS idx_batch_queue_submitted ON batch_queue(submitted_at DESC);

CREATE TABLE IF NOT EXISTS tool_sequences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence_hash TEXT NOT NULL UNIQUE,
    tool_names TEXT NOT NULL,
    occurrence_count INTEGER DEFAULT 1,
    session_ids TEXT NOT NULL DEFAULT '[]',
    sample_args TEXT NOT NULL DEFAULT '[]',
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    crystallized INTEGER DEFAULT 0,
    generated_tool_path TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_seq_hash ON tool_sequences(sequence_hash);
CREATE INDEX IF NOT EXISTS idx_tool_seq_count ON tool_sequences(occurrence_count DESC);
"""

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content=messages,
    content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


class SessionDB:
    """
    SQLite-backed session storage with FTS5 search.

    Thread-safe for the common gateway pattern (multiple reader threads,
    single writer via WAL mode). Each method opens its own cursor.
    """

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=10.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

        self._init_schema()

    def _init_schema(self):
        """Create tables and FTS if they don't exist, run migrations."""
        cursor = self._conn.cursor()

        cursor.executescript(SCHEMA_SQL)

        # Check schema version and run migrations
        cursor.execute("SELECT version FROM schema_version LIMIT 1")
        row = cursor.fetchone()
        if row is None:
            cursor.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        else:
            current_version = row["version"] if isinstance(row, sqlite3.Row) else row[0]
            if current_version < 2:
                # v2: add finish_reason column to messages
                try:
                    cursor.execute("ALTER TABLE messages ADD COLUMN finish_reason TEXT")
                except sqlite3.OperationalError:
                    pass  # Column already exists
                cursor.execute("UPDATE schema_version SET version = 2")
            if current_version < 3:
                # v3: add cost_log table with cache tracking
                try:
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS cost_log (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            timestamp REAL NOT NULL,
                            session_id TEXT NOT NULL REFERENCES sessions(id),
                            model TEXT NOT NULL,
                            workflow TEXT,
                            input_tokens INTEGER DEFAULT 0,
                            output_tokens INTEGER DEFAULT 0,
                            cache_read_tokens INTEGER DEFAULT 0,
                            cache_creation_tokens INTEGER DEFAULT 0,
                            cost_usd REAL NOT NULL,
                            latency_ms INTEGER
                        )
                    """)
                    cursor.execute("CREATE INDEX IF NOT EXISTS idx_cost_log_session ON cost_log(session_id, timestamp)")
                    cursor.execute("CREATE INDEX IF NOT EXISTS idx_cost_log_timestamp ON cost_log(timestamp DESC)")
                except sqlite3.OperationalError:
                    pass  # Table already exists
                cursor.execute("UPDATE schema_version SET version = 3")
            if current_version < 4:
                # v4: add batch_queue table for Anthropic Batches API
                try:
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
                    cursor.execute("CREATE INDEX IF NOT EXISTS idx_batch_queue_status ON batch_queue(status)")
                    cursor.execute("CREATE INDEX IF NOT EXISTS idx_batch_queue_submitted ON batch_queue(submitted_at DESC)")
                except sqlite3.OperationalError:
                    pass  # Table already exists
                cursor.execute("UPDATE schema_version SET version = 4")
            if current_version < 5:
                # v5: add tool_sequences table for Foundry sequence detection
                try:
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS tool_sequences (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            sequence_hash TEXT NOT NULL UNIQUE,
                            tool_names TEXT NOT NULL,
                            occurrence_count INTEGER DEFAULT 1,
                            session_ids TEXT NOT NULL DEFAULT '[]',
                            sample_args TEXT NOT NULL DEFAULT '[]',
                            first_seen REAL NOT NULL,
                            last_seen REAL NOT NULL,
                            crystallized INTEGER DEFAULT 0,
                            generated_tool_path TEXT
                        )
                    """)
                    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_seq_hash ON tool_sequences(sequence_hash)")
                    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tool_seq_count ON tool_sequences(occurrence_count DESC)")
                except sqlite3.OperationalError:
                    pass  # Table already exists
                cursor.execute("UPDATE schema_version SET version = 5")


        # FTS5 setup (separate because CREATE VIRTUAL TABLE can't be in executescript with IF NOT EXISTS reliably)
        try:
            cursor.execute("SELECT * FROM messages_fts LIMIT 0")
        except sqlite3.OperationalError:
            cursor.executescript(FTS_SQL)

        self._conn.commit()

    def close(self):
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    # =========================================================================
    # Session lifecycle
    # =========================================================================

    def create_session(
        self,
        session_id: str,
        source: str,
        model: str = None,
        model_config: Dict[str, Any] = None,
        system_prompt: str = None,
        user_id: str = None,
        parent_session_id: str = None,
    ) -> str:
        """Create a new session record. Returns the session_id."""
        self._conn.execute(
            """INSERT INTO sessions (id, source, user_id, model, model_config,
               system_prompt, parent_session_id, started_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                source,
                user_id,
                model,
                json.dumps(model_config) if model_config else None,
                system_prompt,
                parent_session_id,
                time.time(),
            ),
        )
        self._conn.commit()
        return session_id

    def end_session(self, session_id: str, end_reason: str) -> None:
        """Mark a session as ended."""
        self._conn.execute(
            "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
            (time.time(), end_reason, session_id),
        )
        self._conn.commit()

    def update_system_prompt(self, session_id: str, system_prompt: str) -> None:
        """Store the full assembled system prompt snapshot."""
        self._conn.execute(
            "UPDATE sessions SET system_prompt = ? WHERE id = ?",
            (system_prompt, session_id),
        )
        self._conn.commit()

    def update_token_counts(
        self, session_id: str, input_tokens: int = 0, output_tokens: int = 0
    ) -> None:
        """Increment token counters on a session."""
        self._conn.execute(
            """UPDATE sessions SET
               input_tokens = input_tokens + ?,
               output_tokens = output_tokens + ?
               WHERE id = ?""",
            (input_tokens, output_tokens, session_id),
        )
        self._conn.commit()

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get a session by ID."""
        cursor = self._conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    # =========================================================================
    # Message storage
    # =========================================================================

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str = None,
        tool_name: str = None,
        tool_calls: Any = None,
        tool_call_id: str = None,
        token_count: int = None,
        finish_reason: str = None,
    ) -> int:
        """
        Append a message to a session. Returns the message row ID.

        Also increments the session's message_count (and tool_call_count
        if role is 'tool' or tool_calls is present).
        """
        cursor = self._conn.execute(
            """INSERT INTO messages (session_id, role, content, tool_call_id,
               tool_calls, tool_name, timestamp, token_count, finish_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                role,
                content,
                tool_call_id,
                json.dumps(tool_calls) if tool_calls else None,
                tool_name,
                time.time(),
                token_count,
                finish_reason,
            ),
        )
        msg_id = cursor.lastrowid

        # Update counters
        is_tool_related = role == "tool" or tool_calls is not None
        if is_tool_related:
            self._conn.execute(
                """UPDATE sessions SET message_count = message_count + 1,
                   tool_call_count = tool_call_count + 1 WHERE id = ?""",
                (session_id,),
            )
        else:
            self._conn.execute(
                "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
                (session_id,),
            )

        self._conn.commit()
        return msg_id

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """Load all messages for a session, ordered by timestamp."""
        cursor = self._conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY timestamp, id",
            (session_id,),
        )
        rows = cursor.fetchall()
        result = []
        for row in rows:
            msg = dict(row)
            if msg.get("tool_calls"):
                try:
                    msg["tool_calls"] = json.loads(msg["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(msg)
        return result

    def get_messages_as_conversation(self, session_id: str) -> List[Dict[str, Any]]:
        """
        Load messages in the OpenAI conversation format (role + content dicts).
        Used by the gateway to restore conversation history.
        """
        cursor = self._conn.execute(
            "SELECT role, content, tool_call_id, tool_calls, tool_name "
            "FROM messages WHERE session_id = ? ORDER BY timestamp, id",
            (session_id,),
        )
        messages = []
        for row in cursor.fetchall():
            msg = {"role": row["role"], "content": row["content"]}
            if row["tool_call_id"]:
                msg["tool_call_id"] = row["tool_call_id"]
            if row["tool_name"]:
                msg["tool_name"] = row["tool_name"]
            if row["tool_calls"]:
                try:
                    msg["tool_calls"] = json.loads(row["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    pass
            messages.append(msg)
        return messages

    # =========================================================================
    # Search
    # =========================================================================

    def search_messages(
        self,
        query: str,
        source_filter: List[str] = None,
        role_filter: List[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        Full-text search across session messages using FTS5.

        Supports FTS5 query syntax:
          - Simple keywords: "docker deployment"
          - Phrases: '"exact phrase"'
          - Boolean: "docker OR kubernetes", "python NOT java"
          - Prefix: "deploy*"

        Returns matching messages with session metadata, content snippet,
        and surrounding context (1 message before and after the match).
        """
        if not query or not query.strip():
            return []

        if source_filter is None:
            source_filter = ["cli", "telegram", "discord", "whatsapp", "slack"]

        # Build WHERE clauses dynamically
        where_clauses = ["messages_fts MATCH ?"]
        params: list = [query]

        source_placeholders = ",".join("?" for _ in source_filter)
        where_clauses.append(f"s.source IN ({source_placeholders})")
        params.extend(source_filter)

        if role_filter:
            role_placeholders = ",".join("?" for _ in role_filter)
            where_clauses.append(f"m.role IN ({role_placeholders})")
            params.extend(role_filter)

        where_sql = " AND ".join(where_clauses)
        params.extend([limit, offset])

        sql = f"""
            SELECT
                m.id,
                m.session_id,
                m.role,
                snippet(messages_fts, 0, '>>>', '<<<', '...', 40) AS snippet,
                m.content,
                m.timestamp,
                m.tool_name,
                s.source,
                s.model,
                s.started_at AS session_started
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN sessions s ON s.id = m.session_id
            WHERE {where_sql}
            ORDER BY rank
            LIMIT ? OFFSET ?
        """

        cursor = self._conn.execute(sql, params)
        matches = [dict(row) for row in cursor.fetchall()]

        # Add surrounding context (1 message before + after each match)
        for match in matches:
            try:
                ctx_cursor = self._conn.execute(
                    """SELECT role, content FROM messages
                       WHERE session_id = ? AND id >= ? - 1 AND id <= ? + 1
                       ORDER BY id""",
                    (match["session_id"], match["id"], match["id"]),
                )
                context_msgs = [
                    {"role": r["role"], "content": (r["content"] or "")[:200]}
                    for r in ctx_cursor.fetchall()
                ]
                match["context"] = context_msgs
            except Exception:
                match["context"] = []

            # Remove full content from result (snippet is enough, saves tokens)
            match.pop("content", None)

        return matches

    def search_sessions(
        self,
        source: str = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List sessions, optionally filtered by source."""
        if source:
            cursor = self._conn.execute(
                "SELECT * FROM sessions WHERE source = ? ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (source, limit, offset),
            )
        else:
            cursor = self._conn.execute(
                "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        return [dict(row) for row in cursor.fetchall()]

    # =========================================================================
    # Utility
    # =========================================================================

    def session_count(self, source: str = None) -> int:
        """Count sessions, optionally filtered by source."""
        if source:
            cursor = self._conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE source = ?", (source,)
            )
        else:
            cursor = self._conn.execute("SELECT COUNT(*) FROM sessions")
        return cursor.fetchone()[0]

    def message_count(self, session_id: str = None) -> int:
        """Count messages, optionally for a specific session."""
        if session_id:
            cursor = self._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
            )
        else:
            cursor = self._conn.execute("SELECT COUNT(*) FROM messages")
        return cursor.fetchone()[0]

    # =========================================================================
    # Export and cleanup
    # =========================================================================

    def export_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Export a single session with all its messages as a dict."""
        session = self.get_session(session_id)
        if not session:
            return None
        messages = self.get_messages(session_id)
        return {**session, "messages": messages}

    def export_all(self, source: str = None) -> List[Dict[str, Any]]:
        """
        Export all sessions (with messages) as a list of dicts.
        Suitable for writing to a JSONL file for backup/analysis.
        """
        sessions = self.search_sessions(source=source, limit=100000)
        results = []
        for session in sessions:
            messages = self.get_messages(session["id"])
            results.append({**session, "messages": messages})
        return results

    def clear_messages(self, session_id: str) -> None:
        """Delete all messages for a session and reset its counters."""
        self._conn.execute(
            "DELETE FROM messages WHERE session_id = ?", (session_id,)
        )
        self._conn.execute(
            "UPDATE sessions SET message_count = 0, tool_call_count = 0 WHERE id = ?",
            (session_id,),
        )
        self._conn.commit()

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and all its messages. Returns True if found."""
        cursor = self._conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE id = ?", (session_id,)
        )
        if cursor.fetchone()[0] == 0:
            return False
        self._conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        self._conn.commit()
        return True

    def prune_sessions(self, older_than_days: int = 90, source: str = None) -> int:
        """
        Delete sessions older than N days. Returns count of deleted sessions.
        Only prunes ended sessions (not active ones).
        """
        import time as _time
        cutoff = _time.time() - (older_than_days * 86400)

        if source:
            cursor = self._conn.execute(
                """SELECT id FROM sessions
                   WHERE started_at < ? AND ended_at IS NOT NULL AND source = ?""",
                (cutoff, source),
            )
        else:
            cursor = self._conn.execute(
                "SELECT id FROM sessions WHERE started_at < ? AND ended_at IS NOT NULL",
                (cutoff,),
            )
        session_ids = [row["id"] for row in cursor.fetchall()]

        for sid in session_ids:
            self._conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            self._conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))

        self._conn.commit()
        return len(session_ids)

    # =========================================================================
    # Cost tracking
    # =========================================================================

    @staticmethod
    def calculate_cost(
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> float:
        """
        Calculate cost in USD for a given model and token counts.

        Cache tokens are typically cheaper than regular tokens:
        - cache_read_tokens: 10% of input cost (cached content is cheaper to read)
        - cache_creation_tokens: 25% of input cost (creating cache is cheaper than regular output)

        Args:
            model: Model identifier (exact or partial match against COST_PER_MODEL keys)
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
            cache_read_tokens: Number of cached tokens read
            cache_creation_tokens: Number of cache creation tokens

        Returns:
            Cost in USD as a float.
        """
        # Find matching model in COST_PER_MODEL (supports partial matches)
        model_rates = None
        for key, rates in COST_PER_MODEL.items():
            if key in model or model in key:
                model_rates = rates
                break

        if model_rates is None:
            # Default to claude-haiku rates if model not found
            model_rates = COST_PER_MODEL["claude-haiku-4-5"]

        input_rate = model_rates.get("input", 0.80) / 1_000_000
        output_rate = model_rates.get("output", 4.00) / 1_000_000

        # Regular token cost
        input_cost = input_tokens * input_rate
        output_cost = output_tokens * output_rate

        # Cache token cost (10% of input for reads, 25% of input for creation)
        cache_read_cost = cache_read_tokens * (input_rate * 0.10)
        cache_creation_cost = cache_creation_tokens * (input_rate * 0.25)

        total_cost = input_cost + output_cost + cache_read_cost + cache_creation_cost
        return round(total_cost, 6)

    def log_cost(
        self,
        session_id: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        latency_ms: int = None,
        workflow: str = None,
    ) -> int:
        """
        Log a single API call cost to the cost_log table.

        Args:
            session_id: Session ID for this API call
            model: Model used
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
            cache_read_tokens: Number of cached tokens read
            cache_creation_tokens: Number of cache creation tokens
            latency_ms: API call latency in milliseconds (optional)
            workflow: Workflow name (optional, e.g., "main_loop", "summarization")

        Returns:
            The row ID of the inserted cost_log entry.
        """
        cost_usd = self.calculate_cost(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
        )

        cursor = self._conn.execute(
            """INSERT INTO cost_log
               (timestamp, session_id, model, workflow, input_tokens, output_tokens,
                cache_read_tokens, cache_creation_tokens, cost_usd, latency_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                time.time(),
                session_id,
                model,
                workflow,
                input_tokens,
                output_tokens,
                cache_read_tokens,
                cache_creation_tokens,
                cost_usd,
                latency_ms,
            ),
        )
        cost_id = cursor.lastrowid
        self._conn.commit()
        return cost_id

    def get_cost_summary(self, days: int = 1, session_id: str = None) -> Dict[str, Any]:
        """
        Get cost summary for the last N days.

        Args:
            days: Number of days to summarize (default 1 = today)
            session_id: Optional session ID to filter by

        Returns:
            Dict with keys:
                - total_cost_usd: Total cost in USD
                - total_input_tokens: Sum of input tokens
                - total_output_tokens: Sum of output tokens
                - total_cache_read_tokens: Sum of cache read tokens
                - total_cache_creation_tokens: Sum of cache creation tokens
                - call_count: Number of API calls logged
                - avg_cost_per_call: Average cost per API call
                - models: Dict mapping model names to their cost
                - calls_by_workflow: Dict mapping workflow names to call counts
        """
        cutoff = time.time() - (days * 86400)

        if session_id:
            cursor = self._conn.execute(
                """SELECT
                    SUM(cost_usd) as total_cost,
                    SUM(input_tokens) as total_input,
                    SUM(output_tokens) as total_output,
                    SUM(cache_read_tokens) as total_cache_read,
                    SUM(cache_creation_tokens) as total_cache_create,
                    COUNT(*) as call_count
                   FROM cost_log
                   WHERE timestamp >= ? AND session_id = ?""",
                (cutoff, session_id),
            )
        else:
            cursor = self._conn.execute(
                """SELECT
                    SUM(cost_usd) as total_cost,
                    SUM(input_tokens) as total_input,
                    SUM(output_tokens) as total_output,
                    SUM(cache_read_tokens) as total_cache_read,
                    SUM(cache_creation_tokens) as total_cache_create,
                    COUNT(*) as call_count
                   FROM cost_log
                   WHERE timestamp >= ?""",
                (cutoff,),
            )

        row = cursor.fetchone()
        summary = {
            "total_cost_usd": row["total_cost"] or 0.0,
            "total_input_tokens": row["total_input"] or 0,
            "total_output_tokens": row["total_output"] or 0,
            "total_cache_read_tokens": row["total_cache_read"] or 0,
            "total_cache_creation_tokens": row["total_cache_create"] or 0,
            "call_count": row["call_count"] or 0,
        }

        if summary["call_count"] > 0:
            summary["avg_cost_per_call"] = round(
                summary["total_cost_usd"] / summary["call_count"], 6
            )
        else:
            summary["avg_cost_per_call"] = 0.0

        # Cost by model
        if session_id:
            cursor = self._conn.execute(
                """SELECT model, SUM(cost_usd) as cost
                   FROM cost_log
                   WHERE timestamp >= ? AND session_id = ?
                   GROUP BY model""",
                (cutoff, session_id),
            )
        else:
            cursor = self._conn.execute(
                """SELECT model, SUM(cost_usd) as cost
                   FROM cost_log
                   WHERE timestamp >= ?
                   GROUP BY model""",
                (cutoff,),
            )
        summary["models"] = {row["model"]: row["cost"] for row in cursor.fetchall()}

        # Call count by workflow
        if session_id:
            cursor = self._conn.execute(
                """SELECT workflow, COUNT(*) as count
                   FROM cost_log
                   WHERE timestamp >= ? AND session_id = ? AND workflow IS NOT NULL
                   GROUP BY workflow""",
                (cutoff, session_id),
            )
        else:
            cursor = self._conn.execute(
                """SELECT workflow, COUNT(*) as count
                   FROM cost_log
                   WHERE timestamp >= ? AND workflow IS NOT NULL
                   GROUP BY workflow""",
                (cutoff,),
            )
        summary["calls_by_workflow"] = {row["workflow"]: row["count"] for row in cursor.fetchall()}

        return summary
