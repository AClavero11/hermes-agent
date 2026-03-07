#!/usr/bin/env python3
"""
Memory Retrieval Module - Tiered Memory Access with Similarity Scoring

Implements structured memory with 4 tiers:
  - working: short-term session memory (current context)
  - episodic: session summaries and completed actions
  - semantic: confirmed facts and domain knowledge
  - procedural: how-to guides and repeatable processes

Each tier is a directory under ~/.hermes/memories/{tier}/ with .md files.
Retrieval uses simple keyword/TF-IDF scoring for speed (<50ms latency).
"""

import logging
import os
import re
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Memory directory structure
MEMORY_DIR = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes")) / "memories"
MEMORY_TIERS = ["working", "episodic", "semantic", "procedural"]


class MemoryRetriever:
    """Retrieve relevant memories by similarity scoring."""

    def __init__(self):
        self._ensure_tier_dirs()

    def _get_memory_dir(self) -> Path:
        """Get current memory directory (supports test monkeypatching)."""
        return Path(os.getenv("HERMES_HOME", Path.home() / ".hermes")) / "memories"

    @property
    def memory_dir(self) -> Path:
        """Property to access memory_dir dynamically."""
        return self._get_memory_dir()

    def _ensure_tier_dirs(self):
        """Create tier directories if they don't exist."""
        for tier in MEMORY_TIERS:
            tier_dir = self.memory_dir / tier
            tier_dir.mkdir(parents=True, exist_ok=True)

    def retrieve_relevant_memories(
        self, query: str, top_k: int = 3, tiers: Optional[List[str]] = None
    ) -> List[Dict]:
        """
        Retrieve top-k most relevant memories by similarity to query.

        Args:
            query: Search query string
            top_k: Number of top results to return
            tiers: List of tiers to search. If None, search all tiers.

        Returns:
            List of dicts with keys: tier, filename, content, score
        """
        if not query or not query.strip():
            return []

        if tiers is None:
            tiers = MEMORY_TIERS

        # Collect all memories with scores
        scored_memories = []

        for tier in tiers:
            tier_path = self.memory_dir / tier
            if not tier_path.exists():
                continue

            for md_file in tier_path.glob("*.md"):
                try:
                    content = md_file.read_text(encoding="utf-8").strip()
                    if not content:
                        continue

                    score = self._score_similarity(query, content)
                    if score > 0:
                        scored_memories.append(
                            {
                                "tier": tier,
                                "filename": md_file.name,
                                "content": content,
                                "score": score,
                            }
                        )
                except (OSError, IOError):
                    logger.warning(f"Failed to read memory file: {md_file}")
                    continue

        # Sort by score (descending) and return top-k
        scored_memories.sort(key=lambda x: x["score"], reverse=True)
        return scored_memories[:top_k]

    def _score_similarity(self, query: str, text: str) -> float:
        """
        Score similarity between query and text using TF-IDF-like approach.

        Simple keyword overlap with frequency weighting. Fast and effective
        for memory-sized documents.
        """
        # Normalize: lowercase, extract words
        query_words = self._tokenize(query)
        text_words = self._tokenize(text)

        if not query_words or not text_words:
            return 0.0

        # Count word frequencies in text
        text_freq = Counter(text_words)

        # Score: sum of (query_word_frequency_in_text / text_length)
        score = 0.0
        for word in query_words:
            if word in text_freq:
                # Frequency-weighted: more matches = higher score
                score += text_freq[word]

        # Normalize by query length to avoid bias toward long queries
        score = score / len(query_words)

        return score

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """Tokenize text into lowercase words, filtering stop words."""
        # Common stop words to ignore
        stop_words = {
            "the", "a", "an", "and", "or", "is", "in", "to", "of", "for",
            "with", "by", "on", "at", "from", "as", "be", "have", "has",
            "was", "were", "are", "this", "that", "these", "those", "it",
            "which", "who", "what", "where", "when", "why", "how", "can",
            "will", "would", "should", "could", "do", "does", "did", "not",
            "no", "yes", "but", "so", "if", "then", "your", "my", "his",
            "her", "their", "our", "i", "you", "he", "she", "we", "they"
        }

        # Split on non-alphanumeric, lowercase
        words = re.findall(r"\b[a-z0-9_-]+\b", text.lower())

        # Filter stop words and short words
        return [w for w in words if w not in stop_words and len(w) > 2]


def save_working_memory(content: str) -> bool:
    """Save content to working memory (short-term, current session)."""
    return _save_to_tier("working", content)


def save_episodic_memory(session_id: str, summary: str) -> bool:
    """
    Save session summary to episodic memory.

    Args:
        session_id: Unique session identifier
        summary: Session summary/completion notes

    Returns:
        True if saved successfully
    """
    filename = f"session_{session_id}.md"
    return _save_to_tier("episodic", summary, filename)


def save_semantic_memory(fact: str, source: str = "") -> bool:
    """
    Save confirmed fact to semantic memory.

    Args:
        fact: The fact to save
        source: Where the fact came from (optional)

    Returns:
        True if saved successfully
    """
    content = fact
    if source:
        content = f"{fact}\n(source: {source})"
    return _save_to_tier("semantic", content)


def save_procedural_memory(procedure_name: str, steps: str) -> bool:
    """
    Save how-to procedure to procedural memory.

    Args:
        procedure_name: Name of the procedure
        steps: Step-by-step instructions

    Returns:
        True if saved successfully
    """
    filename = f"proc_{procedure_name}.md"
    content = f"# {procedure_name}\n\n{steps}"
    return _save_to_tier("procedural", content, filename)


def get_working_memory() -> List[str]:
    """Get all entries from working memory (current session)."""
    return _read_tier("working")


def format_memory_context(memories: List[Dict]) -> str:
    """
    Format retrieved memories for system prompt injection.

    Args:
        memories: List of memory dicts from retrieve_relevant_memories()

    Returns:
        Formatted string ready for system prompt insertion
    """
    if not memories:
        return ""

    lines = ["═" * 50, "RETRIEVED MEMORIES", "═" * 50]

    for mem in memories:
        tier = mem.get("tier", "unknown")
        filename = mem.get("filename", "untitled")
        content = mem.get("content", "")
        score = mem.get("score", 0)

        lines.append(f"\n[{tier.upper()}] {filename} (relevance: {score:.2f})")
        lines.append("─" * 50)
        lines.append(content)

    lines.append("─" * 50)
    return "\n".join(lines)


# -- Internal helpers --


def _save_to_tier(tier: str, content: str, filename: Optional[str] = None) -> bool:
    """
    Save content to a memory tier.

    Args:
        tier: Memory tier name (working, episodic, semantic, procedural)
        content: Content to save
        filename: Optional filename. If not provided, auto-generates from content.

    Returns:
        True if saved successfully
    """
    if tier not in MEMORY_TIERS:
        logger.error(f"Invalid memory tier: {tier}")
        return False

    if not content or not content.strip():
        logger.warning(f"Empty content for {tier} memory")
        return False

    # Re-read MEMORY_DIR each time to support test monkeypatching
    mem_dir = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes")) / "memories"
    tier_dir = mem_dir / tier
    tier_dir.mkdir(parents=True, exist_ok=True)

    # Auto-generate filename if not provided
    if filename is None:
        # Use first 40 chars of content as filename
        safe_name = re.sub(r"[^\w\s-]", "", content[:40])
        safe_name = "_".join(safe_name.split())[:30]
        filename = f"{safe_name}.md"

    file_path = tier_dir / filename

    try:
        file_path.write_text(content, encoding="utf-8")
        logger.debug(f"Saved to {tier}/{filename}")
        return True
    except (OSError, IOError) as e:
        logger.error(f"Failed to save to {tier}/{filename}: {e}")
        return False


def _read_tier(tier: str) -> List[str]:
    """Read all entries from a memory tier."""
    if tier not in MEMORY_TIERS:
        return []

    # Re-read MEMORY_DIR each time to support test monkeypatching
    mem_dir = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes")) / "memories"
    tier_dir = mem_dir / tier
    if not tier_dir.exists():
        return []

    entries = []
    for md_file in tier_dir.glob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8").strip()
            if content:
                entries.append(content)
        except (OSError, IOError):
            logger.warning(f"Failed to read {md_file}")
            continue

    return entries
