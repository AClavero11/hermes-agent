"""AAC-specific Alexandria/V11/Atlas context retrieval.

Extracted from gateway.run to enable testing and to keep the gateway loop free
of domain-specific knowledge.
"""

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import List


logger = logging.getLogger(__name__)


CONTEXT_TERMS = (
    "alexandria",
    "atlas",
    "v11",
    "v 11",
    "aac",
    "advanced aerospace",
    "hermes",
    "czar",
    "telegram",
    "part number",
    "pricing",
    "quote",
    "rfq",
    "customer",
    "inventory",
    "sales order",
    "purchase order",
    "idg",
    "csd",
)

V11_TERMS = (
    "v11",
    "v 11",
    "inventory",
    "stock",
    "part number",
    "sales order",
    "purchase order",
)

HERMES_TERMS = (
    "hermes",
    "czar",
    "telegram",
    "gateway",
    "deepseek",
    "agent",
)

HERMES_RELEASE_TERMS = (
    "github",
    "release",
    "newest",
    "latest",
    "nous",
    "v2026.4.23",
    "v0.11.0",
)

PART_NUMBER_RE = re.compile(
    r"\b(?:\d{5,}[A-Z]?|\d{4,}-\d+[A-Z0-9-]*|\d{2,}[A-Z]-?\d+[A-Z0-9-]*|[A-Z]{1,4}\d{4,}[A-Z0-9-]*)\b",
    re.IGNORECASE,
)

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
HEALTH_PROBE_RE = re.compile(r"^(?:ping|pong|status\??|health\??|healthy|ok\??|okay\??)$", re.IGNORECASE)


_ALEXANDRIA_CONTEXT_TERMS = CONTEXT_TERMS
_ALEXANDRIA_V11_TERMS = V11_TERMS
_ALEXANDRIA_HERMES_TERMS = HERMES_TERMS
_HERMES_RELEASE_TERMS = HERMES_RELEASE_TERMS
_ALEXANDRIA_PART_NUMBER_RE = PART_NUMBER_RE
_URL_RE = URL_RE


def _strip_urls_for_routing(message: str) -> str:
    """Remove URLs before keyword/part-number routing decisions."""
    return URL_RE.sub(" ", message or "")


def _message_requests_alexandria_context(message: str) -> bool:
    """Return True when a gateway turn should be grounded in Alexandria/V11."""
    text = (message or "").strip()
    if not text:
        return False
    if text.startswith("/"):
        return False
    routing_text = _strip_urls_for_routing(text).strip()
    if not routing_text or len(routing_text) < 4:
        return False
    lowered = routing_text.lower()
    if "reply exactly" in lowered or "respond exactly" in lowered:
        return False
    if HEALTH_PROBE_RE.fullmatch(lowered):
        return False
    if (
        any(term in lowered for term in CONTEXT_TERMS)
        or any(term in lowered for term in V11_TERMS)
        or any(term in lowered for term in HERMES_TERMS)
    ):
        return True
    if "hermes" in lowered and any(term in lowered for term in HERMES_RELEASE_TERMS):
        return True
    return bool(PART_NUMBER_RE.search(routing_text))


def _alexandria_context_env() -> dict:
    """Environment for Alexandria search commands in non-interactive launchd shells."""
    home = Path.home()
    path_entries = [
        home / "alexandria" / "_system" / "scripts",
        home / "alexandria" / "advanced" / "operations" / "scripts",
        home / "bin",
        home / "bin" / "bin",
        home / ".bun" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/bin"),
    ]
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PATH"] = ":".join(str(p) for p in path_entries) + ":" + env.get("PATH", "")
    return env


def _run_alexandria_context_command(args: List[str], timeout: float = 20.0) -> str:
    """Run a bounded Alexandria retrieval command and return stdout or a short error."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_alexandria_context_env(),
        )
    except FileNotFoundError:
        return ""
    except subprocess.TimeoutExpired:
        logger.debug("Alexandria context command timed out: %s", args[0])
        raise
    except Exception as exc:
        logger.debug("Alexandria context command failed: %s", exc)
        return ""

    output = (result.stdout or "").strip()
    if result.returncode == 0:
        return output
    stderr = (result.stderr or "").strip()
    return stderr[:800] if stderr else output


def _truncate_context_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 40].rstrip() + "\n[truncated]"


def _extract_alexandria_rel_paths(search_output: str) -> List[str]:
    """Extract Alexandria relative paths from qmd/alex-search output."""
    if not search_output:
        return []

    paths: List[str] = []

    try:
        data = json.loads(search_output)
    except Exception:
        data = None

    if isinstance(data, dict):
        items = data.get("results", []) or []
    elif isinstance(data, list):
        items = data
    else:
        items = []

    for item in items:
        if not isinstance(item, dict):
            continue
        rel_path = item.get("rel_path")
        if isinstance(rel_path, str) and rel_path:
            paths.append(rel_path)
        else:
            raw_path = item.get("path")
            if isinstance(raw_path, str) and "/alexandria/" in raw_path:
                paths.append(raw_path.split("/alexandria/", 1)[1])

    for match in re.finditer(r"qmd://alexandria/([^:\s#]+)(?::\d+)?", search_output):
        paths.append(match.group(1))
    for match in re.finditer(r"^\s*\d+\.\s+([^ \[]+\.md)\s+\[", search_output, re.MULTILINE):
        paths.append(match.group(1))

    unique: List[str] = []
    seen = set()
    for path in paths:
        normalized = path.strip().lstrip("/")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique


def _direct_alexandria_context_paths(message: str) -> List[str]:
    """Route high-value gateway prompts to known Alexandria context files."""
    routing_text = _strip_urls_for_routing(message or "")
    lowered = routing_text.lower()
    paths = ["_system/ROUTING.md"]

    if any(term in lowered for term in HERMES_TERMS) or "alexandria" in lowered:
        paths.append("advanced/czar/CONTEXT.md")
    if "pricing" in lowered or "quote" in lowered or "rfq" in lowered:
        paths.append("advanced/pricing/CONTEXT.md")
        paths.append("advanced/customers/CONTEXT.md")
    if "customer" in lowered:
        paths.append("advanced/customers/CONTEXT.md")
    if any(term in lowered for term in V11_TERMS) or PART_NUMBER_RE.search(routing_text):
        paths.append("advanced/operations/CONTEXT.md")
        paths.append("advanced/products/CONTEXT.md")
    if "idg" in lowered or "csd" in lowered:
        paths.append("advanced/idg/IDG_PLATFORM_MAPPING.md")

    unique: List[str] = []
    seen = set()
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _read_alexandria_source_snippets(rel_paths: List[str], max_chars: int = 9000) -> str:
    """Read bounded snippets from Alexandria files without allowing path traversal."""
    root = (Path.home() / "alexandria").resolve()
    parts: List[str] = []
    total = 0

    for rel_path in rel_paths:
        if total >= max_chars:
            break
        candidate = (root / rel_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            continue
        if not text:
            continue
        remaining = max_chars - total
        per_file_limit = 6000 if rel_path == "advanced/czar/CONTEXT.md" else 3000
        snippet = _truncate_context_text(text, min(per_file_limit, remaining))
        parts.append(f"Source: {rel_path}\n{snippet}")
        total += len(snippet)

    return "\n\n".join(parts)


def _read_hermes_release_context_snippet(message: str, max_chars: int = 5000) -> str:
    """Return local GitHub-release notes when the user asks about Hermes releases."""
    lowered = (message or "").lower()
    if not ("hermes" in lowered and any(term in lowered for term in HERMES_RELEASE_TERMS)):
        return ""

    repo_root = Path(__file__).resolve().parents[1]
    candidates = [
        repo_root / "RELEASE_v0.11.0.md",
        Path.home() / ".hermes" / "hermes-agent-v2026.4.23" / "RELEASE_v0.11.0.md",
        Path.home() / ".hermes" / "hermes-agent" / "RELEASE_v0.11.0.md",
    ]

    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            continue
        if not text:
            continue
        return (
            "Source: NousResearch/hermes-agent RELEASE_v0.11.0.md "
            "(GitHub tag v2026.4.23)\n"
            + _truncate_context_text(text, max_chars)
        )
    return ""


def _run_retrieval_step(label: str, args: List[str], timeout: float, errors: List[str]) -> str:
    try:
        return _run_alexandria_context_command(args, timeout=timeout)
    except subprocess.TimeoutExpired:
        errors.append(f"{label} timed out after {timeout:g}s")
        logger.debug("Alexandria retrieval step timed out: %s", label)
    except Exception as exc:
        errors.append(f"{label} failed: {type(exc).__name__}: {exc}")
        logger.debug("Alexandria retrieval step failed: %s", label, exc_info=True)
    return ""


def _unique_paths(paths: List[str]) -> List[str]:
    unique: List[str] = []
    seen = set()
    for path in paths:
        normalized = path.strip().lstrip("/") if isinstance(path, str) else ""
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique


def collect_alexandria_context(
    message: str,
    *,
    qmd_timeout: float = 12.0,
    alex_timeout: float = 14.0,
    v11_timeout: float = 14.0,
) -> dict:
    """Collect Alexandria/V11 source context and return a prompt-ready block."""
    result = {
        "context_text": "",
        "source_paths": [],
        "retrieval_succeeded": False,
        "errors": [],
    }
    if not _message_requests_alexandria_context(message):
        return result

    query = (message or "").strip()
    routing_text = _strip_urls_for_routing(query).strip()
    lowered = routing_text.lower()
    errors: List[str] = []
    raw_search_outputs: List[str] = []
    search_outputs: List[tuple[str, str, int]] = []

    qmd_output = _run_retrieval_step(
        "qmd semantic search",
        ["qmd", "query", query, "-n", "4"],
        qmd_timeout,
        errors,
    )
    if qmd_output:
        raw_search_outputs.append(qmd_output)
        search_outputs.append(("QMD semantic search", qmd_output, 6000))
    else:
        alex_output = _run_retrieval_step(
            "alex-search",
            ["alex-search", query, "-n", "5", "--format", "json"],
            alex_timeout,
            errors,
        )
        if alex_output:
            raw_search_outputs.append(alex_output)
            search_outputs.append(("Alexandria LSH search", alex_output, 5000))

    if any(term in lowered for term in V11_TERMS) or PART_NUMBER_RE.search(routing_text):
        v11_output = _run_retrieval_step(
            "v11-search",
            ["v11-search", query, "-n", "5", "--format", "json"],
            v11_timeout,
            errors,
        )
        if v11_output:
            raw_search_outputs.append(v11_output)
            search_outputs.append(("V11 search", v11_output, 4000))

    rel_paths = _direct_alexandria_context_paths(query)
    for output in raw_search_outputs:
        rel_paths.extend(_extract_alexandria_rel_paths(output))
    source_paths = _unique_paths(rel_paths)

    source_snippets = _read_alexandria_source_snippets(source_paths)
    release_snippet = _read_hermes_release_context_snippet(query)

    context_parts: List[str] = []
    if source_snippets:
        context_parts.append("Retrieved Alexandria source files:\n" + source_snippets)
    if release_snippet:
        context_parts.append("Retrieved Hermes GitHub release notes:\n" + release_snippet)
        source_paths = _unique_paths(source_paths + ["NousResearch/hermes-agent RELEASE_v0.11.0.md"])
    for label, output, max_chars in search_outputs:
        context_parts.append(f"{label}:\n" + _truncate_context_text(output, max_chars))

    retrieval_succeeded = bool(source_snippets or release_snippet or raw_search_outputs)
    if not context_parts:
        context_parts.append(
            "Alexandria retrieval was attempted, but no source output was returned. "
            "State that retrieval returned no results instead of claiming no access."
        )

    if retrieval_succeeded:
        docs_access_instruction = (
            "If asked whether you can view or pull Alexandria docs, say that the docs were "
            "pulled and cite the source paths."
        )
    else:
        docs_access_instruction = (
            "If asked whether you can view or pull Alexandria docs, say retrieval was attempted "
            "but returned no local source output."
        )

    source_path_block = ""
    if source_paths:
        source_path_block = "Source paths:\n" + "\n".join(f"- {path}" for path in source_paths) + "\n\n"

    result["context_text"] = (
        "[System note: Alexandria/V11 retrieval for this turn]\n"
        "Answer as Hermes for AAC using the source material below. Never answer with "
        "generic chatbot disclaimers, missing-access claims about Alexandria, "
        "or instructions to navigate to specified paths. "
        + docs_access_instruction
        + " If asked for Hermes status or a 1-100 score, give the score first, then blockers. "
        "If the material is insufficient, say exactly what was searched and what remains unknown.\n"
        f"User query: {query}\n\n"
        + source_path_block
        + "\n\n".join(context_parts)
        + "\n[End Alexandria/V11 retrieval]"
    )
    result["source_paths"] = source_paths
    result["retrieval_succeeded"] = retrieval_succeeded
    result["errors"] = errors
    return result


def build_alexandria_context_prompt(message: str) -> str:
    """Backward-compatible prompt builder for callers that only need text."""
    return str(collect_alexandria_context(message).get("context_text") or "")


strip_urls_for_routing = _strip_urls_for_routing
message_requests_alexandria_context = _message_requests_alexandria_context
alexandria_context_env = _alexandria_context_env
run_alexandria_context_command = _run_alexandria_context_command
extract_alexandria_rel_paths = _extract_alexandria_rel_paths
direct_alexandria_context_paths = _direct_alexandria_context_paths
read_alexandria_source_snippets = _read_alexandria_source_snippets
read_hermes_release_context_snippet = _read_hermes_release_context_snippet
truncate_context_text = _truncate_context_text
