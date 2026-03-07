"""Anthropic prompt caching (system_and_3 strategy).

Reduces input token costs by ~75% on multi-turn conversations by caching
the conversation prefix. Uses 4 cache_control breakpoints (Anthropic max):
  1. System prompt (stable across all turns)
  2-4. Last 3 non-system messages (rolling window)

Pure functions -- no class state, no AIAgent dependency.
"""

import copy
from typing import Any, Dict, List


def _apply_cache_marker(msg: dict, cache_marker: dict) -> None:
    """Add cache_control to a single message, handling all format variations.

    Appends cache_control marker to the last content block to signal that
    Anthropic should cache everything up to and including this message.
    """
    role = msg.get("role", "")
    content = msg.get("content")

    if role == "tool":
        msg["cache_control"] = cache_marker
        return

    if content is None:
        msg["cache_control"] = cache_marker
        return

    if isinstance(content, str):
        # Convert string to structured format with cache_control
        msg["content"] = [{"type": "text", "text": content, "cache_control": cache_marker}]
        return

    if isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            # Append cache_control to the last content block
            last["cache_control"] = cache_marker
        else:
            # If last block is not a dict, append a new text block with cache_control
            msg["content"].append({"type": "text", "text": "", "cache_control": cache_marker})


def apply_anthropic_cache_control(
    api_messages: List[Dict[str, Any]],
    cache_ttl: str = "5m",
) -> List[Dict[str, Any]]:
    """Apply system_and_3 caching strategy to messages for Anthropic models.

    Places up to 4 cache_control breakpoints: system prompt + last 3 non-system messages.
    This maximizes cache hit rate on multi-turn conversations while respecting Anthropic's
    limit of 4 cache_control markers per request.

    Args:
        api_messages: List of message dicts with role, content, etc.
        cache_ttl: Cache TTL setting: "5m" (ephemeral, 1.25x write cost) or "1h" (1 hour, 2x write cost)

    Returns:
        Deep copy of messages with cache_control breakpoints injected at optimal positions.
    """
    messages = copy.deepcopy(api_messages)
    if not messages:
        return messages

    marker = {"type": "ephemeral"}
    if cache_ttl == "1h":
        marker["ttl"] = "1h"

    breakpoints_used = 0

    # Always cache the system prompt first (stable across all turns)
    if messages[0].get("role") == "system":
        _apply_cache_marker(messages[0], marker)
        breakpoints_used += 1

    # Cache the last 3 non-system messages (rolling window)
    # This captures recent conversation context while leaving room for growth
    remaining = 4 - breakpoints_used
    non_sys = [i for i in range(len(messages)) if messages[i].get("role") != "system"]
    for idx in non_sys[-remaining:]:
        _apply_cache_marker(messages[idx], marker)

    return messages
