"""Customer quote reference extractor.

Finds customer-supplied quote references (e.g. "Ref: ABC-123", "Q# 4567")
in inbound RFQ text and returns the captured token. Pure utility module
with no side effects.
"""

from __future__ import annotations

import re
from typing import List, Optional

# Token definition: starts with alphanumeric, followed by 1+ alphanumeric/dash.
# Minimum total length is 2 characters.
_TOKEN = r"[A-Za-z0-9][A-Za-z0-9\-]{1,}"

# Patterns are tried in order. First match wins for extract_customer_quote_ref.
# Each pattern captures the token in group 1.
_PATTERNS: List[re.Pattern[str]] = [
    # Reference: / Reference #<token>  (must come before "Ref" to avoid partial match)
    re.compile(r"\bReference\s*[:#]\s*(" + _TOKEN + r")", re.IGNORECASE),
    # Ref: / Ref:<token> / Ref <token>
    re.compile(r"\bRef\s*:\s*(" + _TOKEN + r")", re.IGNORECASE),
    re.compile(r"\bRef\s+(" + _TOKEN + r")", re.IGNORECASE),
    # Q#<token> / Q# <token> / Q #<token>
    re.compile(r"\bQ\s*#\s*(" + _TOKEN + r")", re.IGNORECASE),
    # Q: <token>
    re.compile(r"\bQ\s*:\s*(" + _TOKEN + r")", re.IGNORECASE),
    # Your quote <token> / Your quote #<token> / Your quote # <token>
    re.compile(r"\bYour\s+quote\s*#?\s*(" + _TOKEN + r")", re.IGNORECASE),
    # Quote #<token> / Quote # <token> / Quote number <token> / Quote no. <token> / Quote no <token>
    re.compile(r"\bQuote\s*#\s*(" + _TOKEN + r")", re.IGNORECASE),
    re.compile(r"\bQuote\s+number\s+(" + _TOKEN + r")", re.IGNORECASE),
    re.compile(r"\bQuote\s+no\.?\s+(" + _TOKEN + r")", re.IGNORECASE),
    # PO#<token> / PO# <token> / PO: <token> / PO <token>
    re.compile(r"\bPO\s*#\s*(" + _TOKEN + r")", re.IGNORECASE),
    re.compile(r"\bPO\s*:\s*(" + _TOKEN + r")", re.IGNORECASE),
    re.compile(r"\bPO\s+(" + _TOKEN + r")", re.IGNORECASE),
]

# Tokens that look like placeholders and should not be returned.
_FALSE_POSITIVES = {"TBD", "TBA", "N/A", "NA", "NONE", "UNKNOWN"}

# Punctuation to strip from the tail of a matched token.
_TRAILING_PUNCT = ",.;)("


def _clean_token(token: str) -> str:
    """Strip whitespace and trailing punctuation from a matched token."""
    token = token.strip()
    while token and token[-1] in _TRAILING_PUNCT:
        token = token[:-1]
    return token


def _is_false_positive(token: str) -> bool:
    """Return True if the token is a known placeholder value."""
    return token.upper() in _FALSE_POSITIVES


def extract_customer_quote_ref(body: Optional[str]) -> Optional[str]:
    """Return the first customer quote reference token found, or None."""
    if not body or not isinstance(body, str):
        return None

    # Find the earliest match across all patterns (by position in body).
    earliest_pos = -1
    earliest_token: Optional[str] = None

    for pattern in _PATTERNS:
        for match in pattern.finditer(body):
            token = _clean_token(match.group(1))
            if not token or _is_false_positive(token):
                continue
            if len(token) < 2:
                continue
            pos = match.start()
            if earliest_pos == -1 or pos < earliest_pos:
                earliest_pos = pos
                earliest_token = token
            # Only need the first match per pattern for position comparison.
            break

    return earliest_token


def extract_all_refs(body: Optional[str]) -> List[str]:
    """Return every matching ref token in order, deduped."""
    if not body or not isinstance(body, str):
        return []

    # Collect (position, token) tuples from all patterns.
    found: List[tuple[int, str]] = []
    for pattern in _PATTERNS:
        for match in pattern.finditer(body):
            token = _clean_token(match.group(1))
            if not token or _is_false_positive(token):
                continue
            if len(token) < 2:
                continue
            found.append((match.start(), token))

    # Sort by position to preserve order of appearance.
    found.sort(key=lambda item: item[0])

    # Dedupe while preserving first occurrence order.
    seen: set[str] = set()
    result: List[str] = []
    for _, token in found:
        if token not in seen:
            seen.add(token)
            result.append(token)

    return result
