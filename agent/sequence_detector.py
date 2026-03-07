"""
Foundry — Self-Crystallizing Tools via Sequence Detection.

Detects repeated multi-step tool-call sequences across sessions and auto-generates
reusable composite MCP tools when a pattern appears 3+ times. Variable parts
of the sequence become parameters in the generated tool.

Sequence = ordered tuple of tool names. Each occurrence stores the arguments
used, allowing extraction of stable (constant) vs. variable parts.

Generated tools are saved to ~/.hermes/mcp_servers/generated/ as Python MCP
tool files and registered with the MCP pool at next startup.
"""

import hashlib
import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Minimum times a sequence must appear before crystallization
CRYSTALLIZE_THRESHOLD = 3

# Minimum sequence length to track (single tool calls aren't sequences)
MIN_SEQUENCE_LENGTH = 2

# Maximum sequence length to track (avoid noise from very long chains)
MAX_SEQUENCE_LENGTH = 10

# Where generated tools are saved
GENERATED_TOOLS_DIR = "mcp_servers/generated"


def _get_hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))


def _get_generated_tools_dir() -> Path:
    return _get_hermes_home() / GENERATED_TOOLS_DIR


def _sequence_hash(tool_names: Tuple[str, ...]) -> str:
    """Deterministic hash for a tool name sequence."""
    joined = "|".join(tool_names)
    return hashlib.sha256(joined.encode()).hexdigest()[:16]


def _sanitize_tool_name(name: str) -> str:
    """Convert a sequence hash + tool names into a valid Python identifier."""
    return name.replace("-", "_").replace(".", "_").replace(" ", "_")


class SequenceDetector:
    """Detects repeated tool-call sequences and crystallizes composite tools.

    Tracks tool-call sequences in SQLite (tool_sequences table). When a
    sequence appears CRYSTALLIZE_THRESHOLD+ times, generates a composite
    MCP tool that wraps the sequence into a single call.

    Args:
        db_path: Path to state.db. Defaults to HERMES_HOME/state.db.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or (_get_hermes_home() / "state.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._ensure_table()

    def _ensure_table(self):
        """Create tool_sequences table if it doesn't exist."""
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS tool_sequences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sequence_hash TEXT NOT NULL,
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
        self._conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_seq_hash
            ON tool_sequences(sequence_hash)
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tool_seq_count
            ON tool_sequences(occurrence_count DESC)
        """)
        self._conn.commit()

    def observe(self, tool_calls: List[Dict[str, Any]], session_id: str = "unknown") -> List[str]:
        """Track tool-call sequences from a session.

        Extracts all contiguous subsequences of length MIN_SEQUENCE_LENGTH to
        MAX_SEQUENCE_LENGTH from the tool_calls list, and records each one.

        Args:
            tool_calls: List of tool call dicts, each with at least 'name' key.
                        Optionally 'arguments' dict for parameter extraction.
            session_id: Current session ID for attribution.

        Returns:
            List of sequence hashes that reached the crystallization threshold.
        """
        if len(tool_calls) < MIN_SEQUENCE_LENGTH:
            return []

        tool_names = [tc.get("name", "") for tc in tool_calls if tc.get("name")]
        if len(tool_names) < MIN_SEQUENCE_LENGTH:
            return []

        ready_hashes = []
        now = time.time()

        for length in range(MIN_SEQUENCE_LENGTH, min(len(tool_names) + 1, MAX_SEQUENCE_LENGTH + 1)):
            for start in range(len(tool_names) - length + 1):
                subsequence = tuple(tool_names[start:start + length])
                seq_hash = _sequence_hash(subsequence)

                # Extract args for this subsequence window
                window_args = []
                for i in range(start, start + length):
                    args = tool_calls[i].get("arguments", {})
                    window_args.append(args)

                # Upsert into tool_sequences
                row = self._conn.execute(
                    "SELECT id, occurrence_count, session_ids, sample_args FROM tool_sequences WHERE sequence_hash = ?",
                    (seq_hash,),
                ).fetchone()

                if row is None:
                    session_ids_json = json.dumps([session_id])
                    sample_args_json = json.dumps([window_args])
                    self._conn.execute(
                        """INSERT INTO tool_sequences
                           (sequence_hash, tool_names, occurrence_count, session_ids, sample_args, first_seen, last_seen)
                           VALUES (?, ?, 1, ?, ?, ?, ?)""",
                        (seq_hash, json.dumps(list(subsequence)), session_ids_json, sample_args_json, now, now),
                    )
                else:
                    count = row["occurrence_count"] + 1
                    existing_sessions = json.loads(row["session_ids"])
                    if session_id not in existing_sessions:
                        existing_sessions.append(session_id)

                    existing_args = json.loads(row["sample_args"])
                    # Keep at most 5 samples to avoid unbounded growth
                    if len(existing_args) < 5:
                        existing_args.append(window_args)

                    self._conn.execute(
                        """UPDATE tool_sequences
                           SET occurrence_count = ?, session_ids = ?, sample_args = ?, last_seen = ?
                           WHERE id = ?""",
                        (count, json.dumps(existing_sessions), json.dumps(existing_args), now, row["id"]),
                    )

                    if count >= CRYSTALLIZE_THRESHOLD:
                        ready_hashes.append(seq_hash)

                self._conn.commit()

        return ready_hashes

    def crystallize(self, sequence_hash: Optional[str] = None) -> List[str]:
        """Generate composite MCP tools for sequences that hit the threshold.

        If sequence_hash is provided, crystallizes only that sequence.
        Otherwise, crystallizes all sequences with count >= CRYSTALLIZE_THRESHOLD
        that haven't been crystallized yet.

        Args:
            sequence_hash: Optional specific sequence to crystallize.

        Returns:
            List of file paths to generated tool files.
        """
        if sequence_hash:
            rows = self._conn.execute(
                """SELECT * FROM tool_sequences
                   WHERE sequence_hash = ? AND crystallized = 0 AND occurrence_count >= ?""",
                (sequence_hash, CRYSTALLIZE_THRESHOLD),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT * FROM tool_sequences
                   WHERE crystallized = 0 AND occurrence_count >= ?
                   ORDER BY occurrence_count DESC""",
                (CRYSTALLIZE_THRESHOLD,),
            ).fetchall()

        generated_paths = []
        for row in rows:
            path = self._generate_tool(row)
            if path:
                generated_paths.append(path)
                self._conn.execute(
                    "UPDATE tool_sequences SET crystallized = 1, generated_tool_path = ? WHERE id = ?",
                    (path, row["id"]),
                )
                self._conn.commit()

        return generated_paths

    def _generate_tool(self, row: sqlite3.Row) -> Optional[str]:
        """Generate a Python MCP tool file from a detected sequence.

        Analyzes sample_args across occurrences to find:
        - Stable args (same value every time) -> hardcoded in tool
        - Variable args (different values) -> become tool parameters

        Args:
            row: tool_sequences row with sequence data.

        Returns:
            Path to the generated tool file, or None on failure.
        """
        tool_names = json.loads(row["tool_names"])
        sample_args = json.loads(row["sample_args"])
        session_ids = json.loads(row["session_ids"])
        seq_hash = row["sequence_hash"]

        # Build a readable tool name from the sequence
        short_names = [n.split(".")[-1] for n in tool_names]
        tool_func_name = _sanitize_tool_name("_".join(short_names[:3]))
        if len(short_names) > 3:
            tool_func_name += f"_plus{len(short_names) - 3}"
        tool_func_name = f"composite_{tool_func_name}"

        # Analyze variable vs stable args across samples
        variable_params, stable_params = self._extract_params(tool_names, sample_args)

        # Generate tool file
        generated_dir = _get_generated_tools_dir()
        generated_dir.mkdir(parents=True, exist_ok=True)

        tool_filename = f"{tool_func_name}_{seq_hash}.py"
        tool_path = generated_dir / tool_filename

        tool_code = self._render_tool_code(
            func_name=tool_func_name,
            tool_names=tool_names,
            variable_params=variable_params,
            stable_params=stable_params,
            session_ids=session_ids,
            seq_hash=seq_hash,
        )

        tool_path.write_text(tool_code)
        logger.info("Crystallized tool: %s (%d steps, %d occurrences)", tool_path, len(tool_names), row["occurrence_count"])

        return str(tool_path)

    def _extract_params(
        self,
        tool_names: List[str],
        sample_args: List[List[Dict[str, Any]]],
    ) -> Tuple[Dict[str, List[str]], Dict[str, Dict[str, Any]]]:
        """Analyze sample arguments to find variable vs stable parameters.

        Args:
            tool_names: Ordered tool names in the sequence.
            sample_args: List of argument samples (each sample = list of arg dicts per step).

        Returns:
            Tuple of (variable_params, stable_params).
            variable_params: {step_index.param_name: [sample_values]}
            stable_params: {step_index.param_name: constant_value}
        """
        variable_params: Dict[str, List[str]] = {}
        stable_params: Dict[str, Dict[str, Any]] = {}

        if not sample_args:
            return variable_params, stable_params

        num_steps = len(tool_names)

        for step_idx in range(num_steps):
            # Collect all param keys seen across samples for this step
            all_keys: set = set()
            for sample in sample_args:
                if step_idx < len(sample):
                    all_keys.update(sample[step_idx].keys())

            for key in all_keys:
                param_id = f"{step_idx}.{key}"
                values = []
                for sample in sample_args:
                    if step_idx < len(sample) and key in sample[step_idx]:
                        values.append(sample[step_idx][key])

                if not values:
                    continue

                # Check if all values are identical (stable)
                first = values[0]
                all_same = all(v == first for v in values)

                if all_same and len(values) >= 2:
                    stable_params[param_id] = {"value": first, "tool": tool_names[step_idx], "param": key}
                else:
                    variable_params[param_id] = [str(v) for v in values]

        return variable_params, stable_params

    def _render_tool_code(
        self,
        func_name: str,
        tool_names: List[str],
        variable_params: Dict[str, List[str]],
        stable_params: Dict[str, Dict[str, Any]],
        session_ids: List[str],
        seq_hash: str,
    ) -> str:
        """Render a Python MCP tool file.

        Args:
            func_name: Function name for the composite tool.
            tool_names: Ordered tool names in the sequence.
            variable_params: Parameters that vary across occurrences.
            stable_params: Parameters constant across occurrences.
            session_ids: Sessions that triggered crystallization.
            seq_hash: Unique hash of the sequence.

        Returns:
            Python source code string.
        """
        # Build parameter list for the function signature
        param_lines = []
        param_docs = []
        for param_id, sample_values in variable_params.items():
            step_idx, param_name = param_id.split(".", 1)
            safe_name = _sanitize_tool_name(f"{param_name}_step{step_idx}")
            param_lines.append(f"    {safe_name}: str = \"\"")
            param_docs.append(f"        {safe_name}: Parameter for {tool_names[int(step_idx)]}.{param_name} (examples: {', '.join(sample_values[:3])})")

        params_str = ",\n".join(param_lines) if param_lines else "    # No variable parameters detected"
        docs_str = "\n".join(param_docs) if param_docs else "        No variable parameters."

        steps_code = []
        for i, tool_name in enumerate(tool_names):
            steps_code.append(f'    # Step {i + 1}: {tool_name}')
            steps_code.append(f'    steps.append({{"tool": "{tool_name}", "step": {i + 1}}})')

        steps_str = "\n".join(steps_code)

        stable_str = json.dumps(
            {k: v["value"] for k, v in stable_params.items()},
            indent=4,
        )

        return f'''"""
Auto-generated composite tool: {func_name}
Crystallized from sequence detected {len(session_ids)} time(s).

Sequence: {' -> '.join(tool_names)}
Hash: {seq_hash}
Source sessions: {json.dumps(session_ids)}
Generated: {datetime.now().isoformat()}
"""


def {func_name}(
{params_str}
) -> dict:
    """Composite tool wrapping a {len(tool_names)}-step sequence.

    Auto-crystallized by Foundry when this tool sequence was detected
    {len(session_ids)}+ times across sessions.

    Args:
{docs_str}

    Returns:
        Dict with step results and execution summary.
    """
    steps = []

{steps_str}

    return {{
        "tool": "{func_name}",
        "sequence_hash": "{seq_hash}",
        "steps_planned": {len(tool_names)},
        "steps": steps,
        "stable_params": {stable_str},
    }}
'''

    def get_candidates(self, min_count: int = CRYSTALLIZE_THRESHOLD) -> List[Dict[str, Any]]:
        """Get sequences that are candidates for crystallization.

        Args:
            min_count: Minimum occurrence count.

        Returns:
            List of candidate dicts with sequence info.
        """
        rows = self._conn.execute(
            """SELECT sequence_hash, tool_names, occurrence_count, session_ids,
                      first_seen, last_seen, crystallized, generated_tool_path
               FROM tool_sequences
               WHERE occurrence_count >= ?
               ORDER BY occurrence_count DESC""",
            (min_count,),
        ).fetchall()

        return [
            {
                "sequence_hash": row["sequence_hash"],
                "tool_names": json.loads(row["tool_names"]),
                "occurrence_count": row["occurrence_count"],
                "session_ids": json.loads(row["session_ids"]),
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "crystallized": bool(row["crystallized"]),
                "generated_tool_path": row["generated_tool_path"],
            }
            for row in rows
        ]

    def get_generated_tools(self) -> List[Dict[str, Any]]:
        """Get all crystallized tools.

        Returns:
            List of dicts with tool info (path, sequence, sessions).
        """
        rows = self._conn.execute(
            """SELECT sequence_hash, tool_names, occurrence_count, session_ids,
                      generated_tool_path, last_seen
               FROM tool_sequences
               WHERE crystallized = 1 AND generated_tool_path IS NOT NULL
               ORDER BY last_seen DESC""",
        ).fetchall()

        return [
            {
                "sequence_hash": row["sequence_hash"],
                "tool_names": json.loads(row["tool_names"]),
                "occurrence_count": row["occurrence_count"],
                "session_ids": json.loads(row["session_ids"]),
                "generated_tool_path": row["generated_tool_path"],
                "exists": Path(row["generated_tool_path"]).exists() if row["generated_tool_path"] else False,
            }
            for row in rows
        ]

    def get_stats(self) -> Dict[str, Any]:
        """Get sequence detection statistics.

        Returns:
            Dict with total tracked, candidates, crystallized counts.
        """
        total = self._conn.execute("SELECT COUNT(*) FROM tool_sequences").fetchone()[0]
        candidates = self._conn.execute(
            "SELECT COUNT(*) FROM tool_sequences WHERE occurrence_count >= ? AND crystallized = 0",
            (CRYSTALLIZE_THRESHOLD,),
        ).fetchone()[0]
        crystallized = self._conn.execute(
            "SELECT COUNT(*) FROM tool_sequences WHERE crystallized = 1",
        ).fetchone()[0]

        return {
            "total_sequences_tracked": total,
            "crystallization_candidates": candidates,
            "crystallized_tools": crystallized,
            "threshold": CRYSTALLIZE_THRESHOLD,
        }

    def close(self):
        """Close the database connection."""
        if self._conn:
            self._conn.close()
