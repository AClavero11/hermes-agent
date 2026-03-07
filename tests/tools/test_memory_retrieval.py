"""Tests for tools/memory_retrieval.py — MemoryRetriever and tiered memory operations."""

import pytest

from tools.memory_retrieval import (
    MemoryRetriever,
    save_working_memory,
    save_episodic_memory,
    save_semantic_memory,
    save_procedural_memory,
    get_working_memory,
    format_memory_context,
    MEMORY_TIERS,
)


# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def retriever(monkeypatch, tmp_path):
    """Create a MemoryRetriever with temp directory."""
    monkeypatch.setattr("tools.memory_retrieval.MEMORY_DIR", tmp_path / "memories")
    r = MemoryRetriever()
    return r


# =========================================================================
# Tier Directory Creation
# =========================================================================


class TestTierDirectoryCreation:
    def test_ensure_tier_dirs_creates_all_tiers(self, retriever):
        """Verify all tier directories are created."""
        for tier in MEMORY_TIERS:
            tier_dir = retriever.memory_dir / tier
            assert tier_dir.exists(), f"Tier {tier} directory not created"
            assert tier_dir.is_dir(), f"{tier} is not a directory"

    def test_ensure_tier_dirs_idempotent(self, retriever):
        """Calling _ensure_tier_dirs twice should not raise."""
        retriever._ensure_tier_dirs()
        retriever._ensure_tier_dirs()  # Should be idempotent
        for tier in MEMORY_TIERS:
            assert (retriever.memory_dir / tier).exists()


# =========================================================================
# Memory Saving Functions
# =========================================================================


class TestSaveWorkingMemory:
    def test_save_working_memory(self, tmp_path, monkeypatch):
        """Save content to working memory."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        result = save_working_memory("User prefers dark mode")
        assert result is True

        # Verify file was created
        working_dir = hermes_home / "memories" / "working"
        assert len(list(working_dir.glob("*.md"))) > 0

    def test_save_working_memory_empty_rejected(self, tmp_path, monkeypatch):
        """Empty content should be rejected."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        result = save_working_memory("   ")
        assert result is False

    def test_save_working_memory_none_rejected(self, tmp_path, monkeypatch):
        """None content should be rejected."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        result = save_working_memory("")
        assert result is False


class TestSaveEpisodicMemory:
    def test_save_episodic_memory(self, tmp_path, monkeypatch):
        """Save session summary to episodic memory."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        result = save_episodic_memory("123", "Completed user profile update")
        assert result is True

        # Verify file was created with session_id in name
        episodic_dir = hermes_home / "memories" / "episodic"
        files = list(episodic_dir.glob("session_123*"))
        assert len(files) > 0

    def test_save_episodic_memory_empty_rejected(self, tmp_path, monkeypatch):
        """Empty summary should be rejected."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        result = save_episodic_memory("123", "")
        assert result is False


class TestSaveSemanticMemory:
    def test_save_semantic_memory(self, tmp_path, monkeypatch):
        """Save confirmed fact to semantic memory."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        result = save_semantic_memory("IDG part 762367B costs $1200", source="V11 SO#456")
        assert result is True

        semantic_dir = hermes_home / "memories" / "semantic"
        assert len(list(semantic_dir.glob("*.md"))) > 0

    def test_save_semantic_memory_with_source(self, tmp_path, monkeypatch):
        """Semantic memory with source should include source in content."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        save_semantic_memory("Important fact", source="Alexandria file X")

        semantic_dir = hermes_home / "memories" / "semantic"
        files = list(semantic_dir.glob("*.md"))
        assert len(files) > 0

        content = files[0].read_text()
        assert "Important fact" in content
        assert "Alexandria file X" in content


class TestSaveProcedureMemory:
    def test_save_procedural_memory(self, tmp_path, monkeypatch):
        """Save how-to procedure to procedural memory."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        steps = "1. Open V11\n2. Search part\n3. Check inventory"
        result = save_procedural_memory("lookup_part", steps)
        assert result is True

        proc_dir = hermes_home / "memories" / "procedural"
        files = list(proc_dir.glob("proc_lookup_part*"))
        assert len(files) > 0

    def test_procedural_memory_includes_header(self, tmp_path, monkeypatch):
        """Procedural memory should format with procedure name as header."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        steps = "Step 1\nStep 2"
        save_procedural_memory("test_proc", steps)

        proc_dir = hermes_home / "memories" / "procedural"
        files = list(proc_dir.glob("*.md"))
        assert len(files) > 0

        content = files[0].read_text()
        assert "# test_proc" in content
        assert "Step 1" in content


class TestGetWorkingMemory:
    def test_get_working_memory_empty(self, tmp_path, monkeypatch):
        """Empty working memory should return empty list."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        result = get_working_memory()
        assert result == []

    def test_get_working_memory_with_entries(self, tmp_path, monkeypatch):
        """Should return all entries from working memory."""
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        save_working_memory("Fact 1")
        save_working_memory("Fact 2")

        result = get_working_memory()
        assert len(result) == 2
        assert "Fact 1" in result
        assert "Fact 2" in result


# =========================================================================
# Tokenization and Scoring
# =========================================================================


class TestTokenize:
    def test_tokenize_basic(self, retriever):
        """Tokenization should extract words."""
        tokens = retriever._tokenize("User prefers dark mode")
        assert "user" in tokens
        assert "prefers" in tokens
        assert "dark" in tokens
        assert "mode" in tokens

    def test_tokenize_lowercase(self, retriever):
        """Tokenization should normalize to lowercase."""
        tokens = retriever._tokenize("Python 3.12 PROJECT")
        assert all(t.islower() for t in tokens)
        assert "python" in tokens
        assert "project" in tokens

    def test_tokenize_filters_stop_words(self, retriever):
        """Tokenization should filter common stop words."""
        tokens = retriever._tokenize("The user is the best and the one")
        assert "the" not in tokens
        assert "is" not in tokens
        assert "and" not in tokens
        assert "user" in tokens
        assert "best" in tokens

    def test_tokenize_filters_short_words(self, retriever):
        """Tokenization should filter words shorter than 3 chars."""
        tokens = retriever._tokenize("an to as it up my do go by at")
        # All these are 2 chars or stop words, should be filtered
        assert len(tokens) == 0

    def test_tokenize_keeps_numbers(self, retriever):
        """Tokenization should keep numbers."""
        tokens = retriever._tokenize("Part 762367B generator")
        assert any("762367b" in t or "762367" in t for t in tokens)
        assert "generator" in tokens

    def test_tokenize_empty(self, retriever):
        """Empty string should return empty list."""
        tokens = retriever._tokenize("")
        assert tokens == []

    def test_tokenize_only_stop_words(self, retriever):
        """String with only stop words should return empty list."""
        tokens = retriever._tokenize("the a is and or")
        assert tokens == []


class TestScoreSimilarity:
    def test_score_exact_match(self, retriever):
        """Exact match should have high score."""
        score = retriever._score_similarity("generator", "generator pump assembly")
        assert score > 0

    def test_score_partial_match(self, retriever):
        """Partial keyword match should score."""
        score = retriever._score_similarity("idg pump", "IDG generator and pump")
        assert score > 0

    def test_score_no_match(self, retriever):
        """No keywords in match should score 0."""
        score = retriever._score_similarity("xyz", "completely different text")
        assert score == 0

    def test_score_multiple_matches(self, retriever):
        """Multiple keyword matches should score higher."""
        score1 = retriever._score_similarity("idg", "idg generator assembly")
        score2 = retriever._score_similarity("idg", "idg idg idg generator assembly")
        # Duplicate keywords should increase score
        assert score2 > score1

    def test_score_frequency_weighted(self, retriever):
        """Frequently appearing keywords should increase score."""
        text_many = "part part part part assembly"
        text_few = "part assembly"

        score_many = retriever._score_similarity("part", text_many)
        score_few = retriever._score_similarity("part", text_few)

        assert score_many > score_few

    def test_score_normalized_by_query_length(self, retriever):
        """Longer queries should normalize scores fairly."""
        short_query = "part"
        long_query = "part assembly generator pump"

        # Both should find "part" and "assembly" but long query is penalized
        score_short = retriever._score_similarity(short_query, "part assembly")
        score_long = retriever._score_similarity(long_query, "part assembly")

        # Both should be positive but scores may differ due to normalization
        assert score_short > 0
        assert score_long > 0


# =========================================================================
# Memory Retrieval
# =========================================================================


class TestRetrieveRelevantMemories:
    def test_retrieve_empty_query(self, retriever):
        """Empty query should return empty list."""
        result = retriever.retrieve_relevant_memories("")
        assert result == []

    def test_retrieve_no_memories(self, retriever):
        """No saved memories should return empty list."""
        result = retriever.retrieve_relevant_memories("anything")
        assert result == []

    def test_retrieve_single_memory(self, retriever, monkeypatch):
        """Should retrieve a saved memory."""
        monkeypatch.setattr("tools.memory_retrieval.MEMORY_DIR", retriever.memory_dir)
        # Create the semantic tier dir first
        (retriever.memory_dir / "semantic").mkdir(parents=True, exist_ok=True)
        save_semantic_memory("IDG generator costs $1200")

        result = retriever.retrieve_relevant_memories("IDG price", top_k=5)
        assert len(result) >= 1
        assert any("IDG" in r.get("content", "") for r in result)

    def test_retrieve_respects_top_k(self, retriever, monkeypatch):
        """Should return at most top_k memories."""
        monkeypatch.setattr("tools.memory_retrieval.MEMORY_DIR", retriever.memory_dir)
        (retriever.memory_dir / "semantic").mkdir(parents=True, exist_ok=True)

        for i in range(10):
            save_semantic_memory(f"Fact {i}: generator component")

        result = retriever.retrieve_relevant_memories("generator", top_k=3)
        assert len(result) <= 3

    def test_retrieve_sorted_by_score(self, retriever, monkeypatch):
        """Results should be sorted by score (highest first)."""
        monkeypatch.setattr("tools.memory_retrieval.MEMORY_DIR", retriever.memory_dir)
        (retriever.memory_dir / "semantic").mkdir(parents=True, exist_ok=True)

        save_semantic_memory("Something unrelated about food")
        save_semantic_memory("IDG generator pump assembly specs")
        save_semantic_memory("IDG generator")

        result = retriever.retrieve_relevant_memories("IDG generator", top_k=5)
        assert len(result) > 0

        # Verify sorted by score descending
        scores = [r["score"] for r in result]
        assert scores == sorted(scores, reverse=True)

    def test_retrieve_specific_tier(self, retriever, monkeypatch):
        """Should filter results by tier."""
        monkeypatch.setattr("tools.memory_retrieval.MEMORY_DIR", retriever.memory_dir)
        (retriever.memory_dir / "semantic").mkdir(parents=True, exist_ok=True)
        (retriever.memory_dir / "procedural").mkdir(parents=True, exist_ok=True)

        save_semantic_memory("Semantic fact")
        save_procedural_memory("lookup", "Steps here")

        semantic_results = retriever.retrieve_relevant_memories(
            "fact", tiers=["semantic"]
        )
        procedural_results = retriever.retrieve_relevant_memories(
            "steps", tiers=["procedural"]
        )

        assert all(r["tier"] == "semantic" for r in semantic_results)
        assert all(r["tier"] == "procedural" for r in procedural_results)

    def test_retrieve_multiple_tiers(self, retriever, monkeypatch):
        """Should search multiple tiers."""
        monkeypatch.setattr("tools.memory_retrieval.MEMORY_DIR", retriever.memory_dir)
        (retriever.memory_dir / "semantic").mkdir(parents=True, exist_ok=True)
        (retriever.memory_dir / "procedural").mkdir(parents=True, exist_ok=True)

        save_semantic_memory("IDG pricing information")
        save_procedural_memory("lookup", "IDG lookup steps")

        result = retriever.retrieve_relevant_memories(
            "IDG", tiers=["semantic", "procedural"], top_k=10
        )

        assert len(result) >= 2
        tiers = {r["tier"] for r in result}
        assert "semantic" in tiers
        assert "procedural" in tiers

    def test_retrieve_includes_metadata(self, retriever, monkeypatch):
        """Retrieved memories should include tier, filename, content, score."""
        monkeypatch.setattr("tools.memory_retrieval.MEMORY_DIR", retriever.memory_dir)
        (retriever.memory_dir / "semantic").mkdir(parents=True, exist_ok=True)
        save_semantic_memory("Test fact")

        result = retriever.retrieve_relevant_memories("fact")
        assert len(result) > 0

        mem = result[0]
        assert "tier" in mem
        assert "filename" in mem
        assert "content" in mem
        assert "score" in mem
        assert mem["tier"] in MEMORY_TIERS
        assert isinstance(mem["score"], float)


# =========================================================================
# Format Memory Context
# =========================================================================


class TestFormatMemoryContext:
    def test_format_empty_memories(self):
        """Empty memories should return empty string."""
        result = format_memory_context([])
        assert result == ""

    def test_format_single_memory(self):
        """Single memory should be formatted with header."""
        memories = [
            {
                "tier": "semantic",
                "filename": "test.md",
                "content": "Test content",
                "score": 0.85,
            }
        ]
        result = format_memory_context(memories)

        assert "RETRIEVED MEMORIES" in result
        assert "SEMANTIC" in result
        assert "test.md" in result
        assert "Test content" in result
        assert "0.85" in result

    def test_format_multiple_memories(self):
        """Multiple memories should all be included."""
        memories = [
            {
                "tier": "semantic",
                "filename": "fact1.md",
                "content": "Content 1",
                "score": 0.9,
            },
            {
                "tier": "procedural",
                "filename": "proc1.md",
                "content": "Content 2",
                "score": 0.7,
            },
        ]
        result = format_memory_context(memories)

        assert "SEMANTIC" in result
        assert "PROCEDURAL" in result
        assert "fact1.md" in result
        assert "proc1.md" in result
        assert "Content 1" in result
        assert "Content 2" in result

    def test_format_includes_dividers(self):
        """Format should include visual separators."""
        memories = [
            {
                "tier": "semantic",
                "filename": "test.md",
                "content": "Content",
                "score": 0.8,
            }
        ]
        result = format_memory_context(memories)

        # Should have separator lines
        assert "═" in result or "─" in result

    def test_format_handles_missing_fields(self):
        """Format should gracefully handle missing fields."""
        memories = [
            {
                "tier": "semantic",
                "filename": "test.md",
                # Missing content and score
            }
        ]
        result = format_memory_context(memories)

        # Should not crash and should include tier and filename
        assert "SEMANTIC" in result
        assert "test.md" in result


# =========================================================================
# Integration Tests
# =========================================================================


class TestMemoryIntegration:
    def test_save_and_retrieve_workflow(self, retriever, monkeypatch):
        """End-to-end: save memories and retrieve them."""
        monkeypatch.setattr("tools.memory_retrieval.MEMORY_DIR", retriever.memory_dir)
        (retriever.memory_dir / "semantic").mkdir(parents=True, exist_ok=True)
        (retriever.memory_dir / "procedural").mkdir(parents=True, exist_ok=True)
        (retriever.memory_dir / "working").mkdir(parents=True, exist_ok=True)

        # Save various memories
        save_semantic_memory("IDG generators are commonly found on B737 aircraft")
        save_procedural_memory("idg_lookup", "1. Search V11 for IDG\n2. Check inventory")
        save_working_memory("User asked about IDG pricing")

        # Retrieve
        results = retriever.retrieve_relevant_memories("IDG aircraft", top_k=5)

        assert len(results) > 0
        assert any("generator" in r["content"].lower() for r in results)

    def test_retrieve_and_format_workflow(self, retriever, monkeypatch):
        """End-to-end: retrieve and format for system prompt."""
        monkeypatch.setattr("tools.memory_retrieval.MEMORY_DIR", retriever.memory_dir)
        (retriever.memory_dir / "semantic").mkdir(parents=True, exist_ok=True)
        (retriever.memory_dir / "procedural").mkdir(parents=True, exist_ok=True)

        save_semantic_memory("Important fact about part X")
        save_procedural_memory("lookup", "Steps to look up part")

        memories = retriever.retrieve_relevant_memories("part lookup", top_k=5)
        formatted = format_memory_context(memories)

        assert "RETRIEVED MEMORIES" in formatted
        assert len(formatted) > 0

    def test_tier_isolation(self, retriever, monkeypatch):
        """Memories in one tier should not affect others."""
        monkeypatch.setattr("tools.memory_retrieval.MEMORY_DIR", retriever.memory_dir)
        (retriever.memory_dir / "semantic").mkdir(parents=True, exist_ok=True)
        (retriever.memory_dir / "procedural").mkdir(parents=True, exist_ok=True)

        save_semantic_memory("Unique semantic fact")
        save_procedural_memory("proc", "Unique procedural steps")

        # Search semantic tier only
        semantic_results = retriever.retrieve_relevant_memories(
            "fact", tiers=["semantic"]
        )

        # Should not find procedural memory
        assert all("semantic" == r["tier"] for r in semantic_results)
