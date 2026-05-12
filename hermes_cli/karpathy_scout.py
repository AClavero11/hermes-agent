"""LLM-light Karpathy improvement scout for Hermes.

The scout looks for high-signal Hermes/business-ops improvement ideas from
GitHub and configured X.com URLs, scores them deterministically, runs cheap
local quality gates, writes an approval packet, and enqueues the best passing
candidate into the existing Auto-think queue.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from hermes_cli import auto_think


DEFAULT_GITHUB_RELEASE_REPOS: tuple[dict[str, Any], ...] = (
    {"repo": "openai/codex", "systems": ["codex_worker", "auth", "agent"]},
    {"repo": "modelcontextprotocol/python-sdk", "systems": ["mcp", "tools", "agent"]},
    {"repo": "microsoft/playwright", "systems": ["browser_harness", "canary", "tests"]},
    {"repo": "pytest-dev/pytest", "systems": ["tests", "canary", "reliability"]},
    {"repo": "aio-libs/aiohttp", "systems": ["gateway", "api_server", "reliability"]},
    {"repo": "astral-sh/uv", "systems": ["runtime", "packaging", "tests"]},
    {"repo": "getsentry/sentry-python", "systems": ["observability", "errors", "telemetry"]},
)

DEFAULT_GITHUB_SEARCHES: tuple[dict[str, Any], ...] = (
    {
        "query": "repo:openai/codex is:issue is:closed sandbox auth login",
        "systems": ["codex_worker", "auth", "safety"],
    },
    {
        "query": "repo:modelcontextprotocol/python-sdk is:issue is:closed timeout tool error",
        "systems": ["mcp", "tools", "reliability"],
    },
    {
        "query": "repo:microsoft/playwright is:issue is:closed trace timeout flaky",
        "systems": ["browser_harness", "canary", "tests"],
    },
)

HARD_STOP_GATES: tuple[str, ...] = (
    "customer/vendor sends require AC approval",
    "quotes require AC approval",
    "payments require AC approval",
    "orders require AC approval",
    "inventory/V11 mutations require AC approval",
    "public posts require AC approval",
    "destructive prod changes require AC approval",
    "paid signup require AC approval",
    "untrusted installs require AC approval",
)

KEYWORD_GROUPS: dict[str, tuple[str, ...]] = {
    "safety": (
        "prompt injection",
        "secret",
        "credential",
        "sandbox",
        "permission",
        "exploit",
        "vulnerability",
        "cve",
        "auth",
        "oauth",
    ),
    "reliability": (
        "timeout",
        "retry",
        "backoff",
        "idempot",
        "race",
        "deadlock",
        "flaky",
        "crash",
        "hang",
        "error",
    ),
    "metrics": (
        "eval",
        "benchmark",
        "metric",
        "score",
        "regression",
        "canary",
        "test",
        "coverage",
    ),
    "tokens": (
        "cache",
        "context",
        "compression",
        "token",
        "dedupe",
        "retrieval",
        "routing",
    ),
    "observability": (
        "trace",
        "telemetry",
        "log",
        "span",
        "diagnostic",
        "monitor",
        "alert",
    ),
    "business_ops": (
        "approval",
        "workflow",
        "queue",
        "scheduler",
        "cron",
        "inventory",
        "invoice",
        "quote",
        "customer",
    ),
}


@dataclass(frozen=True)
class ExternalItem:
    source_type: str
    locator: str
    title: str
    summary: str
    systems: list[str]
    fetched_at: str
    raw_metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CandidateScore:
    ev_score: int
    total: int
    relevance: int
    impact: int
    effort: int
    cost: int
    reliability: int
    privacy: int
    compounding: int
    lane: str
    rationale: str


@dataclass(frozen=True)
class TestResult:
    command: list[str]
    ok: bool
    exit_code: int
    elapsed_ms: int
    output_tail: str


@dataclass(frozen=True)
class ScoutReport:
    status: str
    generated_at: str
    candidate: dict[str, Any] | None
    score: dict[str, Any] | None
    tests: dict[str, Any]
    baseline: dict[str, Any]
    artifacts: dict[str, str]
    telegram: dict[str, Any]
    notes: list[str]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_source_config(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {
            "github_releases": list(DEFAULT_GITHUB_RELEASE_REPOS),
            "github_searches": list(DEFAULT_GITHUB_SEARCHES),
            "x_urls": [],
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"source config must be a JSON object: {path}")
    return {
        "github_releases": list(payload.get("github_releases") or DEFAULT_GITHUB_RELEASE_REPOS),
        "github_searches": list(payload.get("github_searches") or DEFAULT_GITHUB_SEARCHES),
        "x_urls": list(payload.get("x_urls") or []),
    }


def collect_external_items(
    *,
    source_config: dict[str, Any],
    limit: int = 40,
    timeout: float = 8.0,
) -> list[ExternalItem]:
    fetched_at = utc_now()
    items: list[ExternalItem] = []
    for entry in source_config.get("github_releases") or []:
        if len(items) >= limit:
            break
        items.extend(_collect_github_releases(entry, fetched_at=fetched_at, timeout=timeout))
    for entry in source_config.get("github_searches") or []:
        if len(items) >= limit:
            break
        items.extend(_collect_github_search(entry, fetched_at=fetched_at, timeout=timeout))
    for url in source_config.get("x_urls") or []:
        if len(items) >= limit:
            break
        item = _collect_x_url(str(url), fetched_at=fetched_at)
        if item is not None:
            items.append(item)
    return dedupe_items(items)[:limit]


def _collect_github_releases(entry: dict[str, Any], *, fetched_at: str, timeout: float) -> list[ExternalItem]:
    repo = str(entry.get("repo") or "").strip()
    if not repo or "/" not in repo:
        return []
    systems = _systems(entry.get("systems"), fallback=["github", "hermes"])
    url = f"https://api.github.com/repos/{repo}/releases?per_page=5"
    try:
        payload = _http_json(url, timeout=timeout)
    except Exception:
        payload = []
    if not isinstance(payload, list) or not payload:
        return _collect_github_commits(repo, systems=systems, fetched_at=fetched_at, timeout=timeout)
    items: list[ExternalItem] = []
    for release in payload[:5]:
        if not isinstance(release, dict):
            continue
        title = str(release.get("name") or release.get("tag_name") or repo).strip()
        body = _clean_text(str(release.get("body") or ""))
        locator = str(release.get("html_url") or f"https://github.com/{repo}/releases").strip()
        items.append(
            ExternalItem(
                source_type="github_release",
                locator=locator,
                title=f"{repo}: {title}",
                summary=body[:1200] or f"GitHub release from {repo}.",
                systems=systems,
                fetched_at=fetched_at,
                raw_metrics={"repo": repo, "tag": release.get("tag_name")},
            )
        )
    return items


def _collect_github_commits(
    repo: str,
    *,
    systems: list[str],
    fetched_at: str,
    timeout: float,
) -> list[ExternalItem]:
    url = f"https://api.github.com/repos/{repo}/commits?per_page=5"
    try:
        payload = _http_json(url, timeout=timeout)
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    items: list[ExternalItem] = []
    for commit in payload[:5]:
        if not isinstance(commit, dict):
            continue
        message = str(((commit.get("commit") or {}).get("message")) or "").strip()
        locator = str(commit.get("html_url") or f"https://github.com/{repo}/commits").strip()
        title = message.splitlines()[0][:160] if message else f"{repo}: commit"
        items.append(
            ExternalItem(
                source_type="github_commit",
                locator=locator,
                title=f"{repo}: {title}",
                summary=_clean_text(message)[:1200] or f"GitHub commit from {repo}.",
                systems=systems,
                fetched_at=fetched_at,
                raw_metrics={"repo": repo, "sha": str(commit.get("sha") or "")[:12]},
            )
        )
    return items


def _collect_github_search(entry: dict[str, Any], *, fetched_at: str, timeout: float) -> list[ExternalItem]:
    query = str(entry.get("query") or "").strip()
    if not query:
        return []
    systems = _systems(entry.get("systems"), fallback=["github", "hermes"])
    params = urllib.parse.urlencode({"q": query, "per_page": "5", "sort": "updated", "order": "desc"})
    url = f"https://api.github.com/search/issues?{params}"
    try:
        payload = _http_json(url, timeout=timeout)
    except Exception:
        return []
    raw_items = payload.get("items") if isinstance(payload, dict) else []
    if not isinstance(raw_items, list):
        return []
    items: list[ExternalItem] = []
    for issue in raw_items[:5]:
        if not isinstance(issue, dict):
            continue
        title = str(issue.get("title") or "GitHub issue").strip()
        summary = _clean_text(str(issue.get("body") or ""))[:1200]
        locator = str(issue.get("html_url") or "").strip()
        labels = [
            str(label.get("name") or "").strip().lower()
            for label in (issue.get("labels") or [])
            if isinstance(label, dict)
        ]
        items.append(
            ExternalItem(
                source_type="github_issue",
                locator=locator,
                title=title,
                summary=summary or f"GitHub search hit for: {query}",
                systems=systems,
                fetched_at=fetched_at,
                raw_metrics={
                    "query": query,
                    "state": issue.get("state"),
                    "comments": issue.get("comments", 0),
                    "labels": labels,
                },
            )
        )
    return items


def _collect_x_url(url: str, *, fetched_at: str) -> ExternalItem | None:
    text = ""
    try:
        from tools.x_scraper_tool import x_scrape_tool

        raw = x_scrape_tool([url])
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(parsed, dict):
            text = str(parsed.get("content") or parsed.get("text") or raw)
    except Exception:
        text = ""
    if not text.strip():
        return None
    first_line = _clean_text(text).splitlines()[0][:160] if text else "X.com source"
    return ExternalItem(
        source_type="x_link",
        locator=url,
        title=first_line or "X.com source",
        summary=_clean_text(text)[:1200],
        systems=["research", "agent", "hermes"],
        fetched_at=fetched_at,
        raw_metrics={"source": "x_scrape_tool"},
    )


def _http_json(url: str, *, timeout: float) -> Any:
    headers = {
        "Accept": "application/vnd.github+json, application/json",
        "User-Agent": "hermes-karpathy-scout",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=max(timeout, 1.0)) as response:
        raw = response.read(2_000_000).decode("utf-8", errors="replace")
    return json.loads(raw)


def dedupe_items(items: list[ExternalItem]) -> list[ExternalItem]:
    seen: set[str] = set()
    result: list[ExternalItem] = []
    for item in items:
        key = _normalize_key(item.locator or item.title)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def rank_items(items: list[ExternalItem]) -> list[tuple[ExternalItem, CandidateScore]]:
    ranked = [(item, score_item(item)) for item in items]
    ranked.sort(key=lambda pair: (pair[1].total, pair[1].ev_score, pair[0].fetched_at), reverse=True)
    return ranked


def score_item(item: ExternalItem) -> CandidateScore:
    text = f"{item.title}\n{item.summary}".lower()
    lane_hits = {lane: _keyword_hits(text, keywords) for lane, keywords in KEYWORD_GROUPS.items()}
    lane = max(lane_hits, key=lambda name: lane_hits[name])
    systems_text = " ".join(item.systems).lower()
    relevance = _clamp_score(4 + _keyword_hits(text + " " + systems_text, ("hermes", "agent", "codex", "mcp", "gateway", "canary", "test", "eval")))
    impact = _clamp_score(4 + lane_hits[lane] + _keyword_hits(text, ("regression", "security", "failure", "production", "reliability", "approval")))
    effort = _clamp_score(4 + _keyword_hits(text, ("migration", "rewrite", "major", "breaking", "redesign")) - _keyword_hits(text, ("small", "guard", "test", "config", "flag", "hook")))
    cost = _clamp_score(2 + _keyword_hits(text, ("paid", "enterprise", "api usage", "token", "expensive")))
    reliability = _clamp_score(5 + _keyword_hits(text, ("fixed", "closed", "release", "test", "regression", "deterministic", "idempotent")))
    privacy = _clamp_score(8 - _keyword_hits(text, ("upload", "third-party", "public", "external", "customer data")))
    compounding = _clamp_score(4 + _keyword_hits(text, ("eval", "metric", "canary", "test", "scaffold", "automation", "dashboard", "queue")))
    ev_score = auto_think.score_ev(
        relevance=relevance,
        impact=impact,
        effort=effort,
        cost=cost,
        reliability=reliability,
        privacy=privacy,
        compounding=compounding,
    )
    source_priority = _source_priority(item.systems)
    total = max(1, min(100, ev_score * 10 + min(9, sum(lane_hits.values())) + source_priority))
    rationale = (
        f"lane={lane}; hits={lane_hits[lane]}; "
        f"relevance={relevance}; impact={impact}; effort={effort}; "
        f"cost={cost}; reliability={reliability}; privacy={privacy}; "
        f"compounding={compounding}; source_priority={source_priority}"
    )
    return CandidateScore(
        ev_score=ev_score,
        total=total,
        relevance=relevance,
        impact=impact,
        effort=effort,
        cost=cost,
        reliability=reliability,
        privacy=privacy,
        compounding=compounding,
        lane=lane,
        rationale=rationale,
    )


def build_auto_think_payload(item: ExternalItem, score: CandidateScore) -> dict[str, Any]:
    source_type = "x_link" if item.source_type == "x_link" else "article"
    systems = sorted(set(_systems(item.systems, fallback=["hermes", score.lane]) + [score.lane]))
    title = _shorten(item.title, 120)
    return {
        "source_type": source_type,
        "source_locator": item.locator,
        "observed_at": item.fetched_at,
        "title": f"Karpathy scout: {title}",
        "core_idea": [
            f"External source suggests a {score.lane} improvement relevant to Hermes.",
            "Implement only the smallest measurable dry-run prototype, then re-measure before keeping it.",
        ],
        "evidence": [
            {
                "locator": item.locator,
                "summary": _shorten(item.summary or item.title, 360),
                "confidence": "high" if item.source_type.startswith("github") else "medium",
            }
        ],
        "affected_systems": systems,
        "risk_class": "prod_change",
        "approval_required": True,
        "ev": {
            "score": score.ev_score,
            "relevance": score.relevance,
            "impact": score.impact,
            "effort": score.effort,
            "cost": score.cost,
            "reliability": score.reliability,
            "privacy": score.privacy,
            "compounding": score.compounding,
            "rationale": score.rationale,
        },
        "smallest_safe_prototype": (
            "Make one scoped Hermes change with a before/after metric, focused tests, "
            "and no production/customer/business-system mutation before AC approval."
        ),
        "stop_gates": list(HARD_STOP_GATES),
        "acceptance_criteria": [
            "Starting metric and final metric are recorded in the report.",
            "Focused tests pass before approval is requested.",
            "No customer/vendor sends, quotes, V11/Atlas/Aeroxchange writes, deploys, or destructive commands run without AC approval.",
            "Implementation plan is small enough to revert in one commit.",
        ],
    }


def run_metric_tests(repo_root: Path, command: list[str] | None = None, timeout: int = 120) -> TestResult:
    if command is None:
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-o",
            "addopts=",
            "tests/hermes_cli/test_karpathy_scout.py",
            "tests/hermes_cli/test_auto_think.py",
            "-q",
        ]
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            command,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
        return TestResult(
            command=command,
            ok=proc.returncode == 0,
            exit_code=proc.returncode,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            output_tail=_tail(output),
        )
    except subprocess.TimeoutExpired as exc:
        output = "\n".join(str(part or "") for part in (exc.stdout, exc.stderr) if part)
        return TestResult(
            command=command,
            ok=False,
            exit_code=124,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            output_tail=_tail(output or f"timed out after {timeout}s"),
        )


def collect_baseline(*, hermes_home: Path, repo_root: Path) -> dict[str, Any]:
    latest = _read_latest_canary(hermes_home)
    return {
        "generated_at": utc_now(),
        "repo_root": str(repo_root),
        "git_sha": _git_output(repo_root, ["rev-parse", "--short", "HEAD"]),
        "git_branch": _git_output(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"]),
        "latest_canary_percent": latest.get("percent"),
        "latest_canary_score": latest.get("score"),
        "latest_canary_max_score": latest.get("max_score"),
        "latest_canary_path": latest.get("path"),
    }


def run_scout(
    *,
    hermes_home: Path | None = None,
    repo_root: Path | None = None,
    source_config_path: Path | None = None,
    source_limit: int = 40,
    min_score: int = 70,
    run_tests: bool = True,
    telegram: bool = False,
    dry_run: bool = False,
    timeout: float = 8.0,
) -> ScoutReport:
    home = Path(hermes_home) if hermes_home else get_hermes_home()
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parent.parent
    source_config = load_source_config(source_config_path or _default_source_config_path(home))
    baseline = collect_baseline(hermes_home=home, repo_root=root)
    items = collect_external_items(source_config=source_config, limit=source_limit, timeout=timeout)
    ranked = rank_items(items)
    selected: tuple[ExternalItem, CandidateScore] | None = None
    notes: list[str] = []
    for item, score in ranked:
        if score.total >= min_score:
            selected = (item, score)
            break
    if selected is None and ranked:
        selected = ranked[0]
        notes.append(f"best candidate below min_score={min_score}; approval packet marked blocked")
    if selected is None:
        report = _write_report(
            home=home,
            report=ScoutReport(
                status="no_candidate",
                generated_at=utc_now(),
                candidate=None,
                score=None,
                tests=asdict(TestResult([], True, 0, 0, "not run")),
                baseline=baseline,
                artifacts={},
                telegram={"sent": False, "reason": "no candidate"},
                notes=["No GitHub/X items were fetched."],
            ),
        )
        return report

    item, score = selected
    tests = run_metric_tests(root) if run_tests else TestResult([], True, 0, 0, "tests skipped by configuration")
    payload = build_auto_think_payload(item, score)
    passing_score = score.total >= min_score
    status = "ready_for_approval" if tests.ok and passing_score else "blocked"
    enqueue_result: dict[str, Any] | None = None
    if status == "ready_for_approval":
        enqueue_result = auto_think.enqueue_candidate(payload, hermes_home=home, dry_run=dry_run)
    else:
        notes.append("Candidate was not enqueued because score/test gates did not pass.")

    preliminary = ScoutReport(
        status=status,
        generated_at=utc_now(),
        candidate=(enqueue_result or {"candidate": payload}).get("candidate"),
        score=asdict(score),
        tests=asdict(tests),
        baseline=baseline,
        artifacts={},
        telegram={"sent": False},
        notes=notes,
    )
    report = _write_report(home=home, report=preliminary, append_history=False)
    telegram_result = maybe_send_telegram_packet(home=home, report=report, enabled=telegram)
    final = ScoutReport(
        status=report.status,
        generated_at=report.generated_at,
        candidate=report.candidate,
        score=report.score,
        tests=report.tests,
        baseline=report.baseline,
        artifacts=report.artifacts,
        telegram=telegram_result,
        notes=report.notes,
    )
    return _write_report(home=home, report=final)


def render_markdown_report(report: ScoutReport) -> str:
    candidate = report.candidate or {}
    score = report.score or {}
    tests = report.tests or {}
    baseline = report.baseline or {}
    lines = [
        "# Hermes Karpathy Scout",
        "",
        f"Status: `{report.status}`",
        f"Generated: `{report.generated_at}`",
        "",
        "## Candidate",
        f"- Title: {candidate.get('title', 'none')}",
        f"- Source: {candidate.get('source_locator', 'none')}",
        f"- Risk: {candidate.get('risk_class', 'none')}",
        f"- Approval required: {candidate.get('approval_required', False)}",
        "",
        "## Score",
        f"- Total: {score.get('total', 0)}/100",
        f"- EV: {score.get('ev_score', 0)}/10",
        f"- Lane: {score.get('lane', 'none')}",
        f"- Rationale: {score.get('rationale', '')}",
        "",
        "## Baseline",
        f"- Git: {baseline.get('git_branch', '')}@{baseline.get('git_sha', '')}",
        f"- Latest canary: {baseline.get('latest_canary_percent', 'unknown')}%",
        "",
        "## Tests",
        f"- Passed: {tests.get('ok', False)}",
        f"- Command: `{' '.join(tests.get('command') or [])}`",
        f"- Exit: {tests.get('exit_code')}",
        f"- Elapsed ms: {tests.get('elapsed_ms')}",
        "",
        "## Approval Packet",
        "- No implementation, deployment, customer send, quote, V11/Atlas/Aeroxchange write, or destructive command was executed.",
        "- Approve only if the candidate's source and metric gate justify a small implementation branch.",
        "",
        "## Test Output Tail",
        "```",
        str(tests.get("output_tail") or ""),
        "```",
    ]
    if report.notes:
        lines.extend(["", "## Notes", *[f"- {note}" for note in report.notes]])
    return "\n".join(lines) + "\n"


def render_telegram_packet(report: ScoutReport) -> str:
    candidate = report.candidate or {}
    score = report.score or {}
    tests = report.tests or {}
    baseline = report.baseline or {}
    title = str(candidate.get("title") or "No candidate")
    source = str(candidate.get("source_locator") or "")
    artifact = report.artifacts.get("markdown", "")
    return _shorten(
        "\n".join(
            [
                "Hermes Karpathy scout approval packet",
                f"Status: {report.status}",
                f"Candidate: {title}",
                f"Score: {score.get('total', 0)}/100 (EV {score.get('ev_score', 0)}/10, lane {score.get('lane', 'none')})",
                f"Baseline: canary {baseline.get('latest_canary_percent', 'unknown')}%, git {baseline.get('git_branch', '')}@{baseline.get('git_sha', '')}",
                f"Tests: {'PASS' if tests.get('ok') else 'FAIL'} ({tests.get('elapsed_ms')}ms)",
                f"Source: {source}",
                f"Report: {artifact}",
                "",
                "Approval required before implementation. No production code, customer send, quote, V11/Atlas/Aeroxchange write, deploy, or destructive command ran.",
            ]
        ),
        3800,
    )


def send_telegram_packet(message: str) -> dict[str, Any]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = (
        os.getenv("HERMES_KARPATHY_SCOUT_TELEGRAM_TARGET", "").strip()
        or os.getenv("TELEGRAM_HOME_CHANNEL", "").strip()
        or os.getenv("HERMES_TELEGRAM_HOME_CHANNEL", "").strip()
    )
    if not token or not chat_id:
        return {"sent": False, "reason": "missing TELEGRAM_BOT_TOKEN or TELEGRAM_HOME_CHANNEL"}
    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            payload = json.loads(response.read(1_000_000).decode("utf-8", errors="replace"))
        return {
            "sent": bool(payload.get("ok")),
            "message_id": ((payload.get("result") or {}).get("message_id") if isinstance(payload, dict) else None),
        }
    except Exception as exc:
        return {"sent": False, "reason": f"{type(exc).__name__}: {exc}"}


def maybe_send_telegram_packet(*, home: Path, report: ScoutReport, enabled: bool) -> dict[str, Any]:
    if not enabled:
        return {"sent": False, "reason": "telegram disabled"}
    signature = _telegram_signature(report)
    previous = _previous_telegram_delivery(home, signature)
    if previous is not None:
        return {
            "sent": False,
            "deduped": True,
            "reason": "telegram packet already sent for this candidate signature",
            "signature": signature,
            "previous_message_id": previous.get("message_id"),
            "previous_sent_at": previous.get("sent_at") or previous.get("generated_at"),
        }
    result = send_telegram_packet(render_telegram_packet(report))
    result["signature"] = signature
    if result.get("sent"):
        _record_telegram_delivery(home, report=report, signature=signature, result=result)
    return result


def _write_report(*, home: Path, report: ScoutReport, append_history: bool = True) -> ScoutReport:
    reports_dir = home / "karpathy_scout" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    existing_json_path = report.artifacts.get("json") if report.artifacts else ""
    existing_markdown_path = report.artifacts.get("markdown") if report.artifacts else ""
    if existing_json_path and existing_markdown_path:
        json_path = Path(existing_json_path)
        markdown_path = Path(existing_markdown_path)
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        json_path = reports_dir / f"karpathy-scout-{stamp}.json"
        markdown_path = reports_dir / f"karpathy-scout-{stamp}.md"
    final = ScoutReport(
        status=report.status,
        generated_at=report.generated_at,
        candidate=report.candidate,
        score=report.score,
        tests=report.tests,
        baseline=report.baseline,
        artifacts={"json": str(json_path), "markdown": str(markdown_path)},
        telegram=report.telegram,
        notes=report.notes,
    )
    json_payload = json.dumps(asdict(final), indent=2, sort_keys=True, ensure_ascii=False)
    json_path.write_text(json_payload + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown_report(final), encoding="utf-8")
    (reports_dir / "latest.json").write_text(json_payload + "\n", encoding="utf-8")
    (reports_dir / "latest.md").write_text(render_markdown_report(final), encoding="utf-8")
    if append_history:
        with (reports_dir / "history.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(final), sort_keys=True, ensure_ascii=False) + "\n")
    return final


def _telegram_signature(report: ScoutReport) -> str:
    candidate = report.candidate or {}
    score = report.score or {}
    tests = report.tests or {}
    raw = "|".join(
        [
            str(report.status),
            str(candidate.get("dedupe_key") or candidate.get("source_locator") or candidate.get("title") or ""),
            str(score.get("total") or ""),
            str(score.get("lane") or ""),
            str(tests.get("ok") or False),
        ]
    )
    return "v1:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _previous_telegram_delivery(home: Path, signature: str) -> dict[str, Any] | None:
    state = _read_json_dict(_telegram_state_path(home))
    for delivery in reversed(state.get("deliveries") or []):
        if isinstance(delivery, dict) and delivery.get("signature") == signature:
            return delivery
    for report in _recent_history_reports(home):
        if _telegram_signature(report) != signature:
            continue
        telegram = report.telegram or {}
        if telegram.get("sent"):
            return {
                "signature": signature,
                "message_id": telegram.get("message_id"),
                "sent_at": report.generated_at,
                "source": (report.candidate or {}).get("source_locator"),
            }
    return None


def _record_telegram_delivery(
    home: Path,
    *,
    report: ScoutReport,
    signature: str,
    result: dict[str, Any],
) -> None:
    path = _telegram_state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = _read_json_dict(path)
    deliveries = [delivery for delivery in (state.get("deliveries") or []) if isinstance(delivery, dict)]
    deliveries.append(
        {
            "signature": signature,
            "message_id": result.get("message_id"),
            "sent_at": utc_now(),
            "generated_at": report.generated_at,
            "status": report.status,
            "source": (report.candidate or {}).get("source_locator"),
            "title": (report.candidate or {}).get("title"),
        }
    )
    payload = {"updated_at": utc_now(), "deliveries": deliveries[-500:]}
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def _recent_history_reports(home: Path, limit: int = 200) -> list[ScoutReport]:
    path = home / "karpathy_scout" / "reports" / "history.jsonl"
    if not path.is_file():
        return []
    reports: list[ScoutReport] = []
    try:
        with path.open(encoding="utf-8") as handle:
            lines = deque(handle, maxlen=limit)
    except Exception:
        return reports
    for line in reversed(lines):
        try:
            payload = json.loads(line)
            reports.append(ScoutReport(**payload))
        except Exception:
            continue
    return reports


def _telegram_state_path(home: Path) -> Path:
    return home / "karpathy_scout" / "telegram_state.json"


def _read_json_dict(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_latest_canary(home: Path) -> dict[str, Any]:
    path = home / "canary" / "reports" / "latest.json"
    if not path.is_file():
        return {"path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"path": str(path)}
    if not isinstance(payload, dict):
        return {"path": str(path)}
    percent = payload.get("percent")
    score = payload.get("score")
    max_score = payload.get("max_score") or payload.get("effective_max_score")
    return {"path": str(path), "percent": percent, "score": score, "max_score": max_score}


def _git_output(repo_root: Path, args: list[str]) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return ""
    return (proc.stdout or "").strip()


def _keyword_hits(text: str, keywords: tuple[str, ...]) -> int:
    return sum(1 for keyword in keywords if keyword in text)


def _source_priority(systems: list[str]) -> int:
    normalized = {_normalize_key(str(system)) for system in systems}
    priority = 0
    if normalized & {"codex-worker", "mcp", "gateway", "api-server", "browser-harness", "auth", "tools"}:
        priority += 10
    if normalized & {"safety", "security", "canary", "observability"}:
        priority += 4
    if normalized and normalized.issubset({"tests", "canary", "reliability"}):
        priority -= 4
    return priority


def _clamp_score(value: int) -> int:
    return max(1, min(10, int(value)))


def _systems(value: Any, *, fallback: list[str]) -> list[str]:
    if not isinstance(value, list):
        return list(fallback)
    systems = [_normalize_key(str(item)) for item in value if str(item).strip()]
    return systems or list(fallback)


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _clean_text(value: str) -> str:
    text = re.sub(r"\r\n?", "\n", value or "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _shorten(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 15)].rstrip() + " [truncated]"


def _tail(value: str, limit: int = 6000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return "[truncated]\n" + text[-limit:]


def _default_source_config_path(home: Path) -> Path:
    return home / "karpathy_scout" / "sources.json"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Hermes Karpathy improvement scout")
    subparsers = parser.add_subparsers(dest="command")
    run_parser = subparsers.add_parser("run", help="Run one scout cycle")
    run_parser.add_argument("--hermes-home", type=Path, default=None)
    run_parser.add_argument("--repo-root", type=Path, default=None)
    run_parser.add_argument("--source-config", type=Path, default=None)
    run_parser.add_argument("--source-limit", type=int, default=int(os.getenv("HERMES_KARPATHY_SCOUT_SOURCE_LIMIT", "40")))
    run_parser.add_argument("--min-score", type=int, default=int(os.getenv("HERMES_KARPATHY_SCOUT_MIN_SCORE", "70")))
    run_parser.add_argument("--timeout", type=float, default=float(os.getenv("HERMES_KARPATHY_SCOUT_HTTP_TIMEOUT", "8") or 8))
    run_parser.add_argument("--telegram", action="store_true", default=os.getenv("HERMES_KARPATHY_SCOUT_TELEGRAM", "").lower() in {"1", "true", "yes", "on"})
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument("--skip-tests", action="store_true")
    run_parser.set_defaults(command="run")
    parser.set_defaults(command="run")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if not raw_args:
        raw_args = ["run"]
    args = parser.parse_args(raw_args)
    if args.command != "run":
        parser.error("unknown command")
    report = run_scout(
        hermes_home=args.hermes_home,
        repo_root=args.repo_root,
        source_config_path=args.source_config,
        source_limit=args.source_limit,
        min_score=args.min_score,
        run_tests=not args.skip_tests,
        telegram=args.telegram,
        dry_run=args.dry_run,
        timeout=args.timeout,
    )
    print(json.dumps(asdict(report), indent=2, sort_keys=True))
    return 0 if report.status in {"ready_for_approval", "blocked", "no_candidate"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
