"""Tests for agent/sequence_detector.py — Foundry self-crystallizing tools."""

import json
import os
from pathlib import Path

from agent.sequence_detector import (
    CRYSTALLIZE_THRESHOLD,
    MAX_SEQUENCE_LENGTH,
    SequenceDetector,
    _sanitize_tool_name,
    _sequence_hash,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_tool_calls(names, args_list=None):
    """Build a list of tool call dicts from names and optional args."""
    calls = []
    for i, name in enumerate(names):
        tc = {"name": name}
        if args_list and i < len(args_list):
            tc["arguments"] = args_list[i]
        calls.append(tc)
    return calls


# ---------------------------------------------------------------------------
# Hash + sanitize helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_sequence_hash_deterministic(self):
        h1 = _sequence_hash(("a", "b", "c"))
        h2 = _sequence_hash(("a", "b", "c"))
        assert h1 == h2

    def test_sequence_hash_different_for_different_sequences(self):
        h1 = _sequence_hash(("a", "b"))
        h2 = _sequence_hash(("b", "a"))
        assert h1 != h2

    def test_sequence_hash_length(self):
        h = _sequence_hash(("tool1", "tool2"))
        assert len(h) == 16

    def test_sanitize_tool_name(self):
        assert _sanitize_tool_name("foo-bar.baz qux") == "foo_bar_baz_qux"

    def test_sanitize_already_clean(self):
        assert _sanitize_tool_name("clean_name") == "clean_name"


# ---------------------------------------------------------------------------
# Init + table creation
# ---------------------------------------------------------------------------

class TestSequenceDetectorInit:
    def test_init_creates_db(self, tmp_path):
        db = tmp_path / "test.db"
        detector = SequenceDetector(db_path=db)
        assert db.exists()
        detector.close()

    def test_init_creates_table(self, tmp_path):
        db = tmp_path / "test.db"
        detector = SequenceDetector(db_path=db)
        row = detector._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tool_sequences'"
        ).fetchone()
        assert row is not None
        detector.close()

    def test_init_default_path(self):
        """Uses HERMES_HOME/state.db by default."""
        detector = SequenceDetector()
        hermes_home = Path(os.environ["HERMES_HOME"])
        assert detector.db_path == hermes_home / "state.db"
        detector.close()

    def test_init_creates_indexes(self, tmp_path):
        db = tmp_path / "test.db"
        detector = SequenceDetector(db_path=db)
        indexes = detector._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_tool_seq%'"
        ).fetchall()
        index_names = [r[0] for r in indexes]
        assert "idx_tool_seq_hash" in index_names
        assert "idx_tool_seq_count" in index_names
        detector.close()


# ---------------------------------------------------------------------------
# observe()
# ---------------------------------------------------------------------------

class TestObserve:
    def test_observe_too_short(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        result = detector.observe(make_tool_calls(["single"]), session_id="s1")
        assert result == []
        detector.close()

    def test_observe_empty_list(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        result = detector.observe([], session_id="s1")
        assert result == []
        detector.close()

    def test_observe_records_sequence(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        calls = make_tool_calls(["search", "format"])
        detector.observe(calls, session_id="s1")

        row = detector._conn.execute("SELECT * FROM tool_sequences LIMIT 1").fetchone()
        assert row is not None
        assert json.loads(row["tool_names"]) == ["search", "format"]
        assert row["occurrence_count"] == 1
        detector.close()

    def test_observe_increments_count(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        calls = make_tool_calls(["search", "format"])

        detector.observe(calls, session_id="s1")
        detector.observe(calls, session_id="s2")

        row = detector._conn.execute(
            "SELECT occurrence_count FROM tool_sequences WHERE tool_names = ?",
            (json.dumps(["search", "format"]),),
        ).fetchone()
        assert row["occurrence_count"] == 2
        detector.close()

    def test_observe_tracks_session_ids(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        calls = make_tool_calls(["search", "format"])

        detector.observe(calls, session_id="s1")
        detector.observe(calls, session_id="s2")

        row = detector._conn.execute(
            "SELECT session_ids FROM tool_sequences WHERE tool_names = ?",
            (json.dumps(["search", "format"]),),
        ).fetchone()
        sessions = json.loads(row["session_ids"])
        assert "s1" in sessions
        assert "s2" in sessions
        detector.close()

    def test_observe_deduplicates_session_ids(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        calls = make_tool_calls(["search", "format"])

        detector.observe(calls, session_id="s1")
        detector.observe(calls, session_id="s1")

        row = detector._conn.execute(
            "SELECT session_ids FROM tool_sequences WHERE tool_names = ?",
            (json.dumps(["search", "format"]),),
        ).fetchone()
        sessions = json.loads(row["session_ids"])
        assert sessions.count("s1") == 1
        detector.close()

    def test_observe_returns_ready_hashes_at_threshold(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        calls = make_tool_calls(["search", "format"])

        for i in range(CRYSTALLIZE_THRESHOLD - 1):
            result = detector.observe(calls, session_id=f"s{i}")
            assert result == []

        # This one should trigger threshold
        result = detector.observe(calls, session_id=f"s{CRYSTALLIZE_THRESHOLD}")
        assert len(result) > 0
        detector.close()

    def test_observe_extracts_subsequences(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        calls = make_tool_calls(["a", "b", "c"])
        detector.observe(calls, session_id="s1")

        count = detector._conn.execute("SELECT COUNT(*) FROM tool_sequences").fetchone()[0]
        # 3 tools => subsequences of len 2: (a,b), (b,c) and len 3: (a,b,c) = 3
        assert count == 3
        detector.close()

    def test_observe_stores_sample_args(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        calls = make_tool_calls(
            ["search", "format"],
            args_list=[{"query": "hello"}, {"style": "json"}],
        )
        detector.observe(calls, session_id="s1")

        row = detector._conn.execute(
            "SELECT sample_args FROM tool_sequences WHERE tool_names = ?",
            (json.dumps(["search", "format"]),),
        ).fetchone()
        samples = json.loads(row["sample_args"])
        assert len(samples) == 1
        assert samples[0][0] == {"query": "hello"}
        assert samples[0][1] == {"style": "json"}
        detector.close()

    def test_observe_limits_sample_args_to_5(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        calls = make_tool_calls(["search", "format"])

        for i in range(7):
            detector.observe(calls, session_id=f"s{i}")

        row = detector._conn.execute(
            "SELECT sample_args FROM tool_sequences WHERE tool_names = ?",
            (json.dumps(["search", "format"]),),
        ).fetchone()
        samples = json.loads(row["sample_args"])
        assert len(samples) <= 5
        detector.close()

    def test_observe_skips_calls_without_name(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        calls = [{"name": "search"}, {"arguments": {"x": 1}}, {"name": "format"}]
        detector.observe(calls, session_id="s1")

        row = detector._conn.execute(
            "SELECT * FROM tool_sequences WHERE tool_names = ?",
            (json.dumps(["search", "format"]),),
        ).fetchone()
        assert row is not None
        detector.close()


# ---------------------------------------------------------------------------
# crystallize()
# ---------------------------------------------------------------------------

class TestCrystallize:
    def _build_ready_detector(self, tmp_path, tool_names=None, args_list=None):
        """Create a detector with a sequence at threshold."""
        if tool_names is None:
            tool_names = ["search", "format", "send"]
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        for i in range(CRYSTALLIZE_THRESHOLD):
            calls = make_tool_calls(tool_names, args_list)
            detector.observe(calls, session_id=f"session_{i}")
        return detector

    def test_crystallize_generates_file(self, tmp_path):
        detector = self._build_ready_detector(tmp_path)
        paths = detector.crystallize()
        assert len(paths) > 0
        for p in paths:
            assert Path(p).exists()
        detector.close()

    def test_crystallize_marks_as_crystallized(self, tmp_path):
        detector = self._build_ready_detector(tmp_path)
        detector.crystallize()

        row = detector._conn.execute(
            "SELECT crystallized, generated_tool_path FROM tool_sequences WHERE crystallized = 1"
        ).fetchone()
        assert row is not None
        assert row["generated_tool_path"] is not None
        detector.close()

    def test_crystallize_idempotent(self, tmp_path):
        detector = self._build_ready_detector(tmp_path)
        detector.crystallize()
        paths2 = detector.crystallize()
        assert len(paths2) == 0  # Already crystallized
        detector.close()

    def test_crystallize_specific_hash(self, tmp_path):
        detector = self._build_ready_detector(tmp_path)

        # Get one of the ready hashes
        candidates = detector.get_candidates()
        target_hash = candidates[0]["sequence_hash"]

        paths = detector.crystallize(sequence_hash=target_hash)
        assert len(paths) == 1
        detector.close()

    def test_crystallize_below_threshold_ignored(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        calls = make_tool_calls(["a", "b"])
        detector.observe(calls, session_id="s1")

        paths = detector.crystallize()
        assert paths == []
        detector.close()

    def test_generated_tool_contains_source_attribution(self, tmp_path):
        detector = self._build_ready_detector(tmp_path)
        paths = detector.crystallize()

        content = Path(paths[0]).read_text()
        assert "session_0" in content
        assert "Source sessions" in content
        detector.close()

    def test_generated_tool_has_function(self, tmp_path):
        detector = self._build_ready_detector(tmp_path)
        paths = detector.crystallize()

        content = Path(paths[0]).read_text()
        assert "def composite_" in content
        assert "-> dict:" in content
        detector.close()

    def test_generated_tool_includes_hash(self, tmp_path):
        detector = self._build_ready_detector(tmp_path)
        paths = detector.crystallize()

        content = Path(paths[0]).read_text()
        assert "sequence_hash" in content
        detector.close()


# ---------------------------------------------------------------------------
# Parameter extraction (variable vs stable)
# ---------------------------------------------------------------------------

class TestParamExtraction:
    def test_stable_params_detected(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        tool_names = ["search", "format"]
        sample_args = [
            [{"query": "hello"}, {"style": "json"}],
            [{"query": "hello"}, {"style": "json"}],
            [{"query": "hello"}, {"style": "json"}],
        ]
        _variable, stable = detector._extract_params(tool_names, sample_args)
        # Both are stable (same value across all samples)
        assert "0.query" in stable
        assert stable["0.query"]["value"] == "hello"
        assert "1.style" in stable
        detector.close()

    def test_variable_params_detected(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        tool_names = ["search", "format"]
        sample_args = [
            [{"query": "hello"}, {"style": "json"}],
            [{"query": "world"}, {"style": "json"}],
            [{"query": "test"}, {"style": "json"}],
        ]
        variable, stable = detector._extract_params(tool_names, sample_args)
        assert "0.query" in variable
        assert "1.style" in stable
        detector.close()

    def test_empty_sample_args(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        empty_variable, empty_stable = detector._extract_params(["a", "b"], [])
        assert empty_variable == {}
        assert empty_stable == {}
        detector.close()

    def test_single_sample_no_stable(self, tmp_path):
        """With only 1 sample, nothing can be confirmed as stable (needs >= 2)."""
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        tool_names = ["search"]
        sample_args = [[{"query": "hello"}]]
        _var, stable = detector._extract_params(tool_names, sample_args)
        # Only 1 sample, can't confirm stability
        assert stable == {}
        detector.close()


# ---------------------------------------------------------------------------
# get_candidates()
# ---------------------------------------------------------------------------

class TestGetCandidates:
    def test_no_candidates_initially(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        assert detector.get_candidates() == []
        detector.close()

    def test_returns_candidates_at_threshold(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        calls = make_tool_calls(["a", "b"])
        for i in range(CRYSTALLIZE_THRESHOLD):
            detector.observe(calls, session_id=f"s{i}")

        candidates = detector.get_candidates()
        assert len(candidates) > 0
        assert candidates[0]["occurrence_count"] >= CRYSTALLIZE_THRESHOLD
        detector.close()

    def test_candidates_ordered_by_count(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")

        # Sequence 1: 5 occurrences
        calls1 = make_tool_calls(["x", "y"])
        for i in range(5):
            detector.observe(calls1, session_id=f"s1_{i}")

        # Sequence 2: 3 occurrences
        calls2 = make_tool_calls(["p", "q"])
        for i in range(3):
            detector.observe(calls2, session_id=f"s2_{i}")

        candidates = detector.get_candidates()
        counts = [c["occurrence_count"] for c in candidates]
        assert counts == sorted(counts, reverse=True)
        detector.close()

    def test_custom_min_count(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        calls = make_tool_calls(["a", "b"])
        detector.observe(calls, session_id="s1")

        assert detector.get_candidates(min_count=1) != []
        assert detector.get_candidates(min_count=5) == []
        detector.close()


# ---------------------------------------------------------------------------
# get_generated_tools()
# ---------------------------------------------------------------------------

class TestGetGeneratedTools:
    def test_empty_initially(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        assert detector.get_generated_tools() == []
        detector.close()

    def test_shows_crystallized_tools(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        calls = make_tool_calls(["a", "b"])
        for i in range(CRYSTALLIZE_THRESHOLD):
            detector.observe(calls, session_id=f"s{i}")
        detector.crystallize()

        tools = detector.get_generated_tools()
        assert len(tools) > 0
        assert tools[0]["generated_tool_path"] is not None
        assert tools[0]["exists"] is True
        detector.close()

    def test_exists_false_when_file_deleted(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        calls = make_tool_calls(["a", "b"])
        for i in range(CRYSTALLIZE_THRESHOLD):
            detector.observe(calls, session_id=f"s{i}")
        paths = detector.crystallize()

        # Delete the generated file
        Path(paths[0]).unlink()

        tools = detector.get_generated_tools()
        # At least one tool should show exists=False
        missing = [t for t in tools if not t["exists"]]
        assert len(missing) > 0
        detector.close()


# ---------------------------------------------------------------------------
# get_stats()
# ---------------------------------------------------------------------------

class TestGetStats:
    def test_empty_stats(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        stats = detector.get_stats()
        assert stats["total_sequences_tracked"] == 0
        assert stats["crystallization_candidates"] == 0
        assert stats["crystallized_tools"] == 0
        assert stats["threshold"] == CRYSTALLIZE_THRESHOLD
        detector.close()

    def test_stats_after_observations(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        calls = make_tool_calls(["a", "b"])
        for i in range(CRYSTALLIZE_THRESHOLD):
            detector.observe(calls, session_id=f"s{i}")

        stats = detector.get_stats()
        assert stats["total_sequences_tracked"] > 0
        assert stats["crystallization_candidates"] > 0
        assert stats["crystallized_tools"] == 0
        detector.close()

    def test_stats_after_crystallize(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        calls = make_tool_calls(["a", "b"])
        for i in range(CRYSTALLIZE_THRESHOLD):
            detector.observe(calls, session_id=f"s{i}")
        detector.crystallize()

        stats = detector.get_stats()
        assert stats["crystallized_tools"] > 0
        # Candidates should decrease after crystallization
        assert stats["crystallization_candidates"] < stats["total_sequences_tracked"]
        detector.close()


# ---------------------------------------------------------------------------
# Generated tool code quality
# ---------------------------------------------------------------------------

class TestGeneratedToolCode:
    def test_generated_code_is_valid_python(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        calls = make_tool_calls(["search", "format", "send"])
        for i in range(CRYSTALLIZE_THRESHOLD):
            detector.observe(calls, session_id=f"s{i}")
        paths = detector.crystallize()

        for p in paths:
            code = Path(p).read_text()
            compile(code, p, "exec")  # Raises SyntaxError if invalid
        detector.close()

    def test_generated_function_callable(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        calls = make_tool_calls(["search", "format"])
        for i in range(CRYSTALLIZE_THRESHOLD):
            detector.observe(calls, session_id=f"s{i}")
        paths = detector.crystallize()

        # Execute the generated code and call the function
        for p in paths:
            code = Path(p).read_text()
            namespace = {}
            exec(code, namespace)

            # Find the composite function
            func_names = [k for k in namespace if k.startswith("composite_")]
            assert len(func_names) > 0

            result = namespace[func_names[0]]()
            assert isinstance(result, dict)
            assert "steps" in result
            assert "sequence_hash" in result
        detector.close()

    def test_variable_params_become_function_args(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        tool_names = ["search", "format"]

        for i in range(CRYSTALLIZE_THRESHOLD):
            calls = make_tool_calls(
                tool_names,
                args_list=[{"query": f"query_{i}"}, {"style": "json"}],
            )
            detector.observe(calls, session_id=f"s{i}")

        paths = detector.crystallize()
        # Find the one for the exact 2-step sequence
        target_hash = _sequence_hash(("search", "format"))
        matching = [p for p in paths if target_hash in p]

        if matching:
            code = Path(matching[0]).read_text()
            # query varies -> should become a parameter
            assert "query" in code
        detector.close()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_close_idempotent(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        detector.close()
        detector.close()  # Should not raise

    def test_generated_tools_dir_created(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        calls = make_tool_calls(["a", "b"])
        for i in range(CRYSTALLIZE_THRESHOLD):
            detector.observe(calls, session_id=f"s{i}")
        paths = detector.crystallize()

        if paths:
            assert Path(paths[0]).parent.exists()
        detector.close()

    def test_long_sequence_capped(self, tmp_path):
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        # Create a sequence longer than MAX_SEQUENCE_LENGTH
        names = [f"tool_{i}" for i in range(MAX_SEQUENCE_LENGTH + 5)]
        calls = make_tool_calls(names)
        detector.observe(calls, session_id="s1")

        # No sequence should be longer than MAX_SEQUENCE_LENGTH
        rows = detector._conn.execute("SELECT tool_names FROM tool_sequences").fetchall()
        for row in rows:
            tool_list = json.loads(row["tool_names"])
            assert len(tool_list) <= MAX_SEQUENCE_LENGTH
        detector.close()

    def test_concurrent_sessions(self, tmp_path):
        """Multiple sessions can observe without conflicts."""
        detector = SequenceDetector(db_path=tmp_path / "test.db")
        calls = make_tool_calls(["x", "y"])

        for i in range(10):
            detector.observe(calls, session_id=f"concurrent_{i}")

        row = detector._conn.execute(
            "SELECT occurrence_count FROM tool_sequences WHERE tool_names = ?",
            (json.dumps(["x", "y"]),),
        ).fetchone()
        assert row["occurrence_count"] == 10
        detector.close()


# ---------------------------------------------------------------------------
# Schema migration in hermes_state.py
# ---------------------------------------------------------------------------

class TestSchemaMigration:
    def test_schema_version_is_5(self):
        from hermes_state import SCHEMA_VERSION
        assert SCHEMA_VERSION == 5

    def test_schema_sql_includes_tool_sequences(self):
        from hermes_state import SCHEMA_SQL
        assert "tool_sequences" in SCHEMA_SQL
        assert "sequence_hash" in SCHEMA_SQL
        assert "crystallized" in SCHEMA_SQL
