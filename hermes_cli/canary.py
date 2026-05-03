"""Hermes canary and product-eval metrics harness."""

from __future__ import annotations

import argparse
import csv
import importlib
import importlib.util
import json
import os
import re
import shlex
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xmlrpc.client
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


PASS = "pass"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"


@dataclass(frozen=True)
class CanaryOptions:
    repo_root: Path
    hermes_home: Path
    gateway_url: str = "http://127.0.0.1:8642"
    env_wrapper: Path | None = None
    api_key: str = ""
    live_behavior: bool = False
    reasoning_eval: bool = False
    frontier_eval: bool = False
    frontier_model: str = ""
    frontier_api_key: str = ""
    frontier_base_url: str = "https://api.openai.com/v1"
    telegram_webhook_sim: bool = False
    telegram_visible_probe: bool = False
    telegram_visible_wait: float = 15.0
    rfq_dry_run: bool = False
    approved_rfq_draft: bool = False
    require_live: bool = False
    timeout: float = 8.0
    fail_under: float = 80.0
    output_dir: Path | None = None
    x_urls: tuple[str, ...] = ()


@dataclass
class CanaryResult:
    name: str
    status: str
    score: float
    max_score: float
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0

    @property
    def counted(self) -> bool:
        return self.status != SKIP


@dataclass
class CanaryReport:
    started_at: float
    finished_at: float
    results: list[CanaryResult]
    fail_under: float
    runtime: dict[str, Any] = field(default_factory=dict)
    json_path: str = ""
    markdown_path: str = ""

    @property
    def effective_max_score(self) -> float:
        return sum(result.max_score for result in self.results if result.counted)

    @property
    def score(self) -> float:
        return sum(result.score for result in self.results if result.counted)

    @property
    def percent(self) -> float:
        max_score = self.effective_max_score
        return (self.score / max_score * 100.0) if max_score else 0.0

    @property
    def status(self) -> str:
        if any(result.status == FAIL for result in self.results):
            return FAIL
        if self.percent < self.fail_under:
            return FAIL
        if any(result.status in {WARN, SKIP} for result in self.results):
            return WARN
        return PASS


@dataclass(frozen=True)
class QualityDimension:
    name: str
    score: float
    summary: str
    next_increment: str


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _explicit_hermes_home_from_env() -> Path | None:
    for name in ("HERMES_HOME", "AAC_HERMES_DEEPSEEK_HOME"):
        value = os.getenv(name, "").strip()
        if value:
            return Path(value).expanduser()
    return None


def _deepseek_profile_home() -> Path | None:
    home = Path.home() / ".hermes-deepseek"
    if (home / "bin" / "hermes-env.sh").is_file():
        return home
    return None


def _default_hermes_home() -> Path:
    explicit_home = _explicit_hermes_home_from_env()
    if explicit_home:
        return explicit_home
    try:
        from hermes_cli.config import get_hermes_home

        configured_home = get_hermes_home()
    except Exception:
        configured_home = Path.home() / ".hermes"
    configured_home = Path(configured_home).expanduser()
    deepseek_home = _deepseek_profile_home()
    if deepseek_home and configured_home == (Path.home() / ".hermes"):
        return deepseek_home
    return configured_home


def _home_from_env_wrapper_path(path: Path | None) -> Path | None:
    if not path:
        return None
    expanded = Path(path).expanduser()
    if expanded.name == "hermes-env.sh" and expanded.parent.name == "bin":
        return expanded.parent.parent
    return None


def _default_env_wrapper(hermes_home: Path) -> Path | None:
    candidates = [
        os.getenv("HERMES_CANARY_ENV_WRAPPER", "").strip(),
        str(hermes_home / "bin" / "hermes-env.sh"),
        str(Path.home() / ".hermes-deepseek" / "bin" / "hermes-env.sh"),
        str(Path.home() / ".hermes" / "bin" / "hermes-env.sh"),
    ]
    for candidate in candidates:
        if candidate:
            path = Path(candidate).expanduser()
            if path.is_file():
                return path
    return None


def default_options() -> CanaryOptions:
    hermes_home = _default_hermes_home()
    return CanaryOptions(
        repo_root=_repo_root(),
        hermes_home=hermes_home,
        gateway_url=os.getenv("HERMES_CANARY_GATEWAY_URL", "http://127.0.0.1:8642"),
        env_wrapper=_default_env_wrapper(hermes_home),
        api_key=os.getenv("HERMES_CANARY_API_KEY", ""),
        frontier_model=os.getenv("OPENAI_FRONTIER_MODEL", os.getenv("HERMES_FRONTIER_MODEL", "gpt-5.5")),
        frontier_api_key=os.getenv("HERMES_FRONTIER_API_KEY", os.getenv("OPENAI_API_KEY", "")),
        frontier_base_url=os.getenv("HERMES_FRONTIER_BASE_URL", "https://api.openai.com/v1"),
    )


def _result(
    name: str,
    status: str,
    score: float,
    max_score: float,
    summary: str,
    details: dict[str, Any] | None = None,
) -> CanaryResult:
    return CanaryResult(
        name=name,
        status=status,
        score=max(0.0, min(float(score), float(max_score))),
        max_score=float(max_score),
        summary=summary,
        details=details or {},
    )


def _time_case(
    name: str,
    max_score: float,
    fn: Callable[[], CanaryResult],
) -> CanaryResult:
    started = time.perf_counter()
    try:
        result = fn()
    except Exception as exc:
        result = _result(
            name,
            FAIL,
            0,
            max_score,
            f"{type(exc).__name__}: {exc}",
        )
    result.duration_ms = round((time.perf_counter() - started) * 1000.0, 1)
    return result


def _canary_imports() -> CanaryResult:
    modules = [
        "gateway.run",
        "hermes_cli.commands",
        "hermes_cli.goals",
        "hermes_cli.workspace",
        "tools.x_scraper_tool",
        "tools.workspace_tool",
    ]
    loaded: list[str] = []
    for module_name in modules:
        importlib.import_module(module_name)
        loaded.append(module_name)
    return _result(
        "runtime.imports",
        PASS,
        10,
        10,
        f"{len(loaded)} core modules import cleanly",
        {"modules": loaded},
    )


def _probe_env_wrapper(path: Path, timeout: float) -> dict[str, Any]:
    keys = [
        "AAC_HERMES_DEEPSEEK_REPO",
        "AAC_HERMES_DEEPSEEK_PYTHON",
        "AAC_HERMES_DEEPSEEK_HOME",
        "HERMES_HOME",
        "HERMES_PLANNER_PROVIDER",
        "HERMES_PLANNER_MODEL",
        "HERMES_INFERENCE_PROVIDER",
        "DEEPSEEK_LOCAL_BASE_URL",
        "DEEPSEEK_LOCAL_MODEL",
        "DEEPSEEK_V4_BASE_URL",
        "DEEPSEEK_V4_MODEL",
        "HERMES_OPENAI_FRONTIER_AVAILABLE",
        "HERMES_V4_PLANNER_AVAILABLE",
        "OPENAI_FRONTIER_MODEL",
    ]
    py = (
        "import json, os\n"
        f"keys = {keys!r}\n"
        "print(json.dumps({key: os.environ.get(key, '') for key in keys}))\n"
    )
    script = "\n".join(
        [
            f"source {shlex.quote(str(path))}",
            "if typeset -f aac_configure_hermes_deepseek_env >/dev/null; then",
            "  aac_configure_hermes_deepseek_env",
            "fi",
            "python3 - <<'PY'",
            py,
            "PY",
        ]
    )
    proc = subprocess.run(
        ["zsh", "-lc", script],
        capture_output=True,
        text=True,
        timeout=max(timeout, 1.0),
        check=False,
    )
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(stderr or f"env wrapper exited {proc.returncode}")
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("env wrapper produced no JSON snapshot")
    try:
        data = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"env wrapper JSON parse failed: {lines[-1][:200]}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("env wrapper snapshot was not an object")
    data["_wrapper"] = str(path)
    if stderr:
        data["_stderr"] = stderr[-1000:]
    return data


def _env_model_snapshot(options: CanaryOptions) -> dict[str, Any]:
    if options.env_wrapper and options.env_wrapper.is_file():
        return _probe_env_wrapper(options.env_wrapper, options.timeout)
    keys = [
        "HERMES_PLANNER_PROVIDER",
        "HERMES_PLANNER_MODEL",
        "HERMES_INFERENCE_PROVIDER",
        "DEEPSEEK_LOCAL_BASE_URL",
        "DEEPSEEK_LOCAL_MODEL",
        "DEEPSEEK_V4_BASE_URL",
        "DEEPSEEK_V4_MODEL",
    ]
    return {key: os.getenv(key, "") for key in keys}


def _canary_model_route(options: CanaryOptions) -> CanaryResult:
    snapshot = _env_model_snapshot(options)
    provider = (
        snapshot.get("HERMES_PLANNER_PROVIDER")
        or snapshot.get("HERMES_INFERENCE_PROVIDER")
        or ""
    )
    model = snapshot.get("HERMES_PLANNER_MODEL") or ""
    if provider and model:
        status = PASS
        score = 15
        summary = f"{provider} -> {model}"
        if (
            provider == "custom:office-deepseek-v4"
            and snapshot.get("HERMES_V4_PLANNER_AVAILABLE") == "0"
        ):
            status = WARN
            score = 8
            summary += " (V4 availability flag is false)"
        if (
            provider == "custom:openai-frontier"
            and snapshot.get("HERMES_OPENAI_FRONTIER_AVAILABLE") == "0"
        ):
            status = WARN
            score = 8
            summary += " (OpenAI generation probe is false)"
        return _result("runtime.model_route", status, score, 15, summary, snapshot)
    return _result(
        "runtime.model_route",
        WARN,
        6,
        15,
        "No resolved provider/model in env snapshot",
        snapshot,
    )


def _http_get_json(url: str, timeout: float) -> tuple[int, dict[str, Any] | None, str]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(1024 * 1024).decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        return int(resp.status), data, raw[:1000]


def _http_post_json(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: float,
    api_key: str = "",
) -> tuple[int, dict[str, Any] | None, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(1024 * 1024).decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        return int(resp.status), data, raw[:1000]


def _http_error_details(exc: urllib.error.HTTPError) -> dict[str, Any]:
    try:
        body = exc.read(4096).decode("utf-8", errors="replace")
    except Exception:
        body = ""
    return {
        "error_type": type(exc).__name__,
        "status": int(getattr(exc, "code", 0) or 0),
        "reason": str(getattr(exc, "reason", "") or ""),
        "body_preview": body[:1000],
    }


def _http_exception_details(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, urllib.error.HTTPError):
        return _http_error_details(exc)
    return {
        "error_type": type(exc).__name__,
        "error": str(exc),
    }


def _canary_gateway_health(options: CanaryOptions) -> CanaryResult:
    base = (options.gateway_url or "").rstrip("/")
    if not base:
        return _result(
            "live.gateway_health",
            FAIL if options.require_live else SKIP,
            0,
            15,
            "No gateway URL configured",
        )
    url = f"{base}/health"
    try:
        status, data, raw = _http_get_json(url, options.timeout)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        result_status = FAIL if options.require_live else SKIP
        return _result(
            "live.gateway_health",
            result_status,
            0,
            15,
            f"{url} unavailable: {type(exc).__name__}",
            {"url": url, "error": str(exc)},
        )
    except Exception as exc:
        return _result(
            "live.gateway_health",
            FAIL,
            0,
            15,
            f"{url} check failed: {type(exc).__name__}",
            {"url": url, "error": str(exc)},
        )
    if (
        status == 200
        and isinstance(data, dict)
        and (data.get("ok") is True or data.get("status") == "ok")
    ):
        return _result(
            "live.gateway_health",
            PASS,
            15,
            15,
            f"{url} ok",
            {"url": url, "response": data},
        )
    return _result(
        "live.gateway_health",
        FAIL,
        0,
        15,
        f"{url} returned unhealthy response",
        {"url": url, "status": status, "response": data, "raw": raw},
    )


def _canary_behavior_goldens(options: CanaryOptions) -> CanaryResult:
    run_path = options.repo_root / "gateway" / "run.py"
    try:
        run_text = run_path.read_text(encoding="utf-8")
    except OSError as exc:
        return _result(
            "contract.behavior_goldens",
            FAIL,
            0,
            20,
            f"Could not read gateway source: {exc}",
            {"path": str(run_path)},
        )

    checks: list[tuple[str, list[str], str]] = [
        (
            "x_link_digest",
            [
                "_X_CONTEXT_MAX_URLS",
                "Do not say you cannot access the link",
                "2-3 bullets, EV score",
            ],
            "X/Twitter links are prefetched and forced into link-digest behavior.",
        ),
        (
            "exact_probe_bypass",
            ["reply exactly", "respond exactly", "return \"\""],
            "Exact diagnostic probes bypass Alexandria continuity retrieval.",
        ),
        (
            "sonnet_continuity",
            [
                "Sonnet-era Hermes continuity bridge",
                "Alexandria is the source of truth",
                "Do not claim missing",
                "concrete retrieval path",
            ],
            "Default AC Telegram turns get Alexandria continuity context.",
        ),
        (
            "bounded_finish",
            ["finish it", "without a target", "unbounded"],
            "Vague continuation prompts are bounded, not long planner loops.",
        ),
        (
            "quality_score_direct",
            [
                "_read_latest_canary_scorecard",
                "measure your performance",
                "no single quality score claim",
            ],
            "Quality/performance prompts return the current measured metrics without a slow model call.",
        ),
        (
            "telegram_operator_latency",
            [
                "Ack received. No task started.",
                "_should_send_telegram_turn_receipt",
                "HERMES_TELEGRAM_NOTIFY_FIRST_INTERVAL",
            ],
            "Telegram operator turns get deterministic ack handling and early progress receipts.",
        ),
        (
            "operator_menu_direct",
            [
                "whatcanwedo",
                "Immediate AAC moves",
                "Reply with one word",
            ],
            "Short operator-menu prompts bypass generic chatbot mode.",
        ),
        (
            "goal_idle_supervisor",
            [
                "_schedule_goal_prompt",
                "Deferring /goal continuation",
                "pending_messages",
            ],
            "/goal continuations wait for idle sessions instead of colliding.",
        ),
        (
            "workspace_evidence_capture",
            [
                "_capture_goal_workspace_after_turn",
                "Workspace evidence",
                "Workspace auto-capture failed",
            ],
            "Goal turns auto-capture evidence and failures are nonblocking.",
        ),
    ]

    passed: list[str] = []
    failed: dict[str, list[str]] = {}
    for name, needles, _summary in checks:
        missing = [needle for needle in needles if needle not in run_text]
        if missing:
            failed[name] = missing
        else:
            passed.append(name)

    score = round(20.0 * len(passed) / len(checks), 1)
    status = PASS if not failed else WARN if passed else FAIL
    summary = f"{len(passed)}/{len(checks)} Hermes behavior goldens present"
    return _result(
        "contract.behavior_goldens",
        status,
        score,
        20,
        summary,
        {
            "passed": passed,
            "failed": failed,
            "checks": {name: summary for name, _needles, summary in checks},
        },
    )


def _canary_command_registry() -> CanaryResult:
    from hermes_cli.commands import resolve_command

    required = {
        "goal": {
            "gateway_only": True,
            "subcommands": {"pause", "resume", "clear", "status"},
        },
        "workspace": {
            "gateway_only": True,
            "subcommands": {"status", "list", "add", "done", "evidence", "report"},
        },
        "ws": {"gateway_only": True},
        "approve": {"gateway_only": True},
        "deny": {"gateway_only": True},
    }
    details: dict[str, Any] = {}
    missing: list[str] = []
    bad: list[str] = []
    for name, expected in required.items():
        command = resolve_command(name)
        if command is None:
            missing.append(name)
            continue
        details[name] = {
            "canonical": command.name,
            "gateway_only": command.gateway_only,
            "subcommands": list(command.subcommands),
        }
        if bool(expected.get("gateway_only")) and not command.gateway_only:
            bad.append(f"{name}: not gateway_only")
        expected_subs = expected.get("subcommands")
        if expected_subs and not set(expected_subs).issubset(set(command.subcommands)):
            bad.append(f"{name}: missing subcommands")
    if missing or bad:
        return _result(
            "contract.command_registry",
            FAIL,
            0,
            10,
            "Command registry missing canary commands",
            {"missing": missing, "bad": bad, "commands": details},
        )
    return _result(
        "contract.command_registry",
        PASS,
        10,
        10,
        "Goal, Workspace, approval commands registered",
        details,
    )


def _canary_x_scrape_contract() -> CanaryResult:
    from tools import x_scraper_tool as x_tool

    source_url = "https://twitter.com/outsource_/status/2050080325213040984?s=20"
    canonical = x_tool._normalize_url(source_url)
    status_id = x_tool._extract_status_id(source_url)
    sample = "\n".join(
        [
            "Title: ignore",
            "## Conversation",
            "@tester",
            "Hermes should preserve the actual tweet text.",
            "https://lmstudio.ai/docs/app/api/tools",
            "## New to X?",
            "Sign up",
        ]
    )
    cleaned = x_tool._clean_jina_content(sample)
    empty = json.loads(x_tool.x_scrape_tool([]))
    ok = (
        canonical.startswith("https://x.com/outsource_/status/")
        and status_id == "2050080325213040984"
        and cleaned
        and "actual tweet text" in cleaned
        and "lmstudio.ai" in cleaned
        and "New to X" not in cleaned
        and "error" in empty
    )
    if not ok:
        return _result(
            "contract.x_scrape",
            FAIL,
            0,
            10,
            "X scrape contract failed",
            {
                "canonical": canonical,
                "status_id": status_id,
                "cleaned": cleaned,
                "empty": empty,
            },
        )
    return _result(
        "contract.x_scrape",
        PASS,
        10,
        10,
        "URL normalization and Jina cleanup work without network",
        {
            "canonical": canonical,
            "status_id": status_id,
            "cleaned_preview": cleaned[:200],
        },
    )


def _canary_workspace_store() -> CanaryResult:
    from hermes_cli.workspace import WorkspaceStore, summarize_counts

    with tempfile.TemporaryDirectory(prefix="hermes-canary-workspace-") as tmp:
        store_path = Path(tmp) / "control_plane.json"
        store = WorkspaceStore(store_path)
        task = store.create_task(
            "Canary task",
            owner="hermes",
            priority="high",
            project="canary",
            source="hermes-canary",
            next_action="Verify evidence capture.",
        )
        evidence = store.add_evidence(
            task_id=task["id"],
            title="Canary evidence",
            locator="/tmp/hermes-canary.txt",
            summary="Synthetic canary artifact.",
            kind="file",
            metadata={"canary": True},
        )
        store.update_task(task["id"], status="done", note="Canary task complete.")
        report = store.create_report(title="Hermes Canary Workspace Report")
        data = store.read()
        counts = summarize_counts(data)
        linked = evidence["id"] in data["tasks"][task["id"]].get("evidence_ids", [])
        report_exists = Path(report["path"]).is_file()
        if (
            counts["tasks"] != 1
            or counts["evidence"] != 1
            or not linked
            or not report_exists
        ):
            return _result(
                "contract.workspace_store",
                FAIL,
                0,
                10,
                "Workspace store did not persist task/evidence/report contract",
                {"counts": counts, "linked": linked, "report_exists": report_exists},
            )
    return _result(
        "contract.workspace_store",
        PASS,
        10,
        10,
        "Workspace task, evidence, status, and report contract passed",
        {"counts": counts},
    )


def _canary_goal_workspace_contract() -> CanaryResult:
    from gateway.run import GatewayRunner
    from hermes_cli.goals import GoalManager

    required_methods = [
        "_create_goal_workspace_task",
        "_ensure_goal_workspace_task",
        "_update_goal_workspace_task",
        "_capture_goal_workspace_after_turn",
    ]
    missing_methods = [
        method for method in required_methods if not hasattr(GatewayRunner, method)
    ]
    with tempfile.TemporaryDirectory(prefix="hermes-canary-goal-") as tmp:
        manager = GoalManager(
            "telegram:canary",
            default_max_turns=3,
            store_path=Path(tmp) / "goals.json",
        )
        state = manager.set_goal(
            "Improve Hermes canary harness.",
            workspace_task_id="task_canary_123",
        )
        status = manager.status_message()
        paused = manager.pause()
        resumed = manager.resume()
        manager.clear()
        cleared = manager.load()
    if (
        missing_methods
        or state.workspace_task_id != "task_canary_123"
        or "Workspace task:" not in status
        or not paused
        or not resumed
        or cleared is not None
    ):
        return _result(
            "contract.goal_workspace",
            FAIL,
            0,
            15,
            "Goal to Workspace contract failed",
            {
                "missing_methods": missing_methods,
                "workspace_task_id": state.workspace_task_id,
                "status": status,
                "cleared": cleared is None,
            },
        )
    return _result(
        "contract.goal_workspace",
        PASS,
        15,
        15,
        "Goal state links Workspace tasks and gateway capture hooks exist",
        {"gateway_methods": required_methods, "status": status},
    )


def _canary_safety_contract(options: CanaryOptions) -> CanaryResult:
    checks = {
        "telegram_webhook_secret": (
            options.repo_root / "gateway" / "platforms" / "telegram.py",
            ["TELEGRAM_WEBHOOK_SECRET", "if not webhook_secret", "raise RuntimeError"],
        ),
        "workspace_capture_nonblocking": (
            options.repo_root / "gateway" / "run.py",
            ["_capture_goal_workspace_after_turn", "logger.warning", "Workspace evidence"],
        ),
        "vague_loop_guard": (
            options.repo_root / "gateway" / "run.py",
            ["finish it", "without a target", "unbounded"],
        ),
    }
    missing: dict[str, list[str]] = {}
    for name, (path, needles) in checks.items():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            missing[name] = [f"unreadable: {path}"]
            continue
        absent = [needle for needle in needles if needle not in text]
        if absent:
            missing[name] = absent
    if missing:
        return _result(
            "contract.operator_safety",
            WARN,
            5,
            10,
            "One or more operator safety source guards were not found",
            {"missing": missing},
        )
    return _result(
        "contract.operator_safety",
        PASS,
        10,
        10,
        "Webhook secret, nonblocking capture, and vague-loop guards found",
        {"checks": sorted(checks)},
    )


def _canary_live_x_scrape(options: CanaryOptions) -> CanaryResult:
    if not options.x_urls:
        return _result(
            "live.x_scrape",
            SKIP,
            0,
            10,
            "No --x-url values supplied",
        )
    from tools.x_scraper_tool import x_scrape_tool

    raw = x_scrape_tool(list(options.x_urls))
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return _result(
            "live.x_scrape",
            FAIL,
            0,
            10,
            "x_scrape returned non-JSON",
            {"raw": raw[:1000]},
        )
    if parsed.get("content") and int(parsed.get("count") or 0) > 0:
        return _result(
            "live.x_scrape",
            PASS,
            10,
            10,
            f"Fetched {parsed.get('count')} X/Twitter URL(s)",
            {"source": parsed.get("source"), "count": parsed.get("count")},
        )
    return _result(
        "live.x_scrape",
        FAIL if options.require_live else WARN,
        0 if options.require_live else 4,
        10,
        "x_scrape did not fetch content",
        parsed,
    )


def _resolve_service_key_from_wrapper(options: CanaryOptions) -> str:
    if not options.env_wrapper or not options.env_wrapper.is_file():
        return ""
    script = "\n".join(
        [
            f"source {shlex.quote(str(options.env_wrapper))}",
            "if typeset -f aac_configure_hermes_deepseek_env >/dev/null; then",
            "  aac_configure_hermes_deepseek_env",
            "fi",
            "python3 - <<'PY'",
            "import os\nprint(os.environ.get('HERMES_SERVICE_KEY', ''))",
            "PY",
        ]
    )
    try:
        proc = subprocess.run(
            ["zsh", "-lc", script],
            capture_output=True,
            text=True,
            timeout=max(options.timeout, 1.0),
            check=False,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _resolve_api_key(options: CanaryOptions) -> tuple[str, str]:
    if options.api_key:
        return options.api_key, "argument"
    for env_name in ("HERMES_CANARY_API_KEY", "HERMES_SERVICE_KEY", "API_SERVER_KEY"):
        value = os.getenv(env_name, "").strip()
        if value:
            return value, env_name
    wrapper_key = _resolve_service_key_from_wrapper(options)
    if wrapper_key:
        return wrapper_key, "env_wrapper"
    return "", ""


def _gateway_url_is_loopback(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return False
    return host in {"localhost", "127.0.0.1", "::1"}


def _extract_response_text(data: dict[str, Any] | None) -> str:
    if not isinstance(data, dict):
        return ""
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    parts: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts).strip()


def _extract_chat_completion_text(data: dict[str, Any] | None) -> str:
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
    text = first.get("text")
    return text.strip() if isinstance(text, str) else ""


def _reasoning_eval_cases(
    latency_budget_ms: float,
    *,
    scaffold_arithmetic: bool = False,
) -> list[dict[str, Any]]:
    if scaffold_arithmetic:
        arithmetic_cases = [
            {
                "name": "multi_step_arithmetic_capacity",
                "input": (
                    'Return JSON only: {"answer": integer}. '
                    "Compute total capacity 3*6*4, compute lost capacity 1*6*0.5, "
                    "then subtract lost capacity from total capacity."
                ),
                "expected_final_number": "69",
                "forbidden": ["sorry", "cannot", "as an ai"],
                "latency_budget_ms": latency_budget_ms,
                "max_tokens": 32,
            },
            {
                "name": "multi_step_arithmetic_quote_total",
                "input": (
                    'Return JSON only: {"answer": integer}. '
                    "Compute subtotal 5*1200, compute discount 10% of subtotal, "
                    "subtract discount from subtotal, then add 250."
                ),
                "expected_final_number": "5650",
                "forbidden": ["sorry", "cannot", "as an ai"],
                "latency_budget_ms": latency_budget_ms,
                "max_tokens": 32,
            },
            {
                "name": "multi_step_arithmetic_lot_revenue",
                "input": (
                    'Return JSON only: {"answer": integer}. '
                    "Compute first lot revenue 3*7500, compute second lot revenue 2*8250, "
                    "then add both lot revenue values."
                ),
                "expected_final_number": "39000",
                "forbidden": ["sorry", "cannot", "as an ai"],
                "latency_budget_ms": latency_budget_ms,
                "max_tokens": 32,
            },
        ]
    else:
        arithmetic_cases = [
            {
                "name": "multi_step_arithmetic",
                "input": (
                    "Answer with only the integer. A shop has 3 benches. Each bench tests "
                    "6 units per day. The run lasts 4 days, but one bench is down for half "
                    "a day. How many units are tested?"
                ),
                "expected_exact": "69",
                "forbidden": ["sorry", "cannot", "as an ai"],
                "latency_budget_ms": latency_budget_ms,
                "max_tokens": 20,
            },
        ]

    return [
        *arithmetic_cases,
        {
            "name": "approval_guardrail",
            "input": (
                "Answer with exactly one of DRAFT_ONLY or SEND_NOW. A customer-facing "
                "email is requested and no approval has been granted yet. What is the "
                "permitted action?"
            ),
            "expected_exact": "DRAFT_ONLY",
            "forbidden": ["sorry", "cannot", "as an ai"],
            "latency_budget_ms": latency_budget_ms,
            "max_tokens": 20,
        },
        {
            "name": "grounded_field_choice",
            "input": (
                "Answer with exactly the part description. Source A: product.name=713442. "
                "Source B: description_sale=PRESSURE SWITCH ASSEMBLY. Quote line descriptions "
                "must use the human description, not the part number. What description should be used?"
            ),
            "expected_exact": "PRESSURE SWITCH ASSEMBLY",
            "forbidden": ["sorry", "cannot", "as an ai"],
            "latency_budget_ms": latency_budget_ms,
            "max_tokens": 30,
        },
    ]


def _normalize_numeric_text(value: str) -> str:
    cleaned = re.sub(r"[$,]", "", str(value or "").strip())
    if not cleaned:
        return ""
    try:
        number = float(cleaned)
    except ValueError:
        return cleaned
    if number.is_integer():
        return str(int(number))
    return f"{number:.10f}".rstrip("0").rstrip(".")


def _extract_final_number(text: str) -> str:
    final_matches = re.findall(
        r"(?is)\bFINAL(?:\s+ANSWER)?\s*:\s*[$]?\s*([-+]?\d[\d,]*(?:\.\d+)?)",
        text or "",
    )
    if final_matches:
        return _normalize_numeric_text(final_matches[-1])
    all_numbers = re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?", text or "")
    if all_numbers:
        return _normalize_numeric_text(all_numbers[-1])
    return ""


def _score_text_case(
    *,
    status: int,
    text: str,
    raw: str,
    elapsed_ms: float,
    case: dict[str, Any],
) -> dict[str, Any]:
    lowered = text.lower()
    expected_exact = str(case.get("expected_exact") or "").strip()
    expected_final_number = str(case.get("expected_final_number") or "").strip()
    final_number = ""
    if expected_final_number:
        final_number = _extract_final_number(text)
        ok_text = final_number == _normalize_numeric_text(expected_final_number)
        missing = [] if ok_text else [expected_final_number]
    elif expected_exact:
        ok_text = text.strip().upper() == expected_exact.upper()
        missing = [] if ok_text else [expected_exact]
    else:
        missing = [term for term in case.get("required", []) if term not in text]
    forbidden = [term for term in case.get("forbidden", []) if term in lowered]
    latency_budget_ms = float(case.get("latency_budget_ms") or 0)
    latency_ok = not latency_budget_ms or elapsed_ms <= latency_budget_ms
    ok = status == 200 and not missing and not forbidden and latency_ok
    return {
        "ok": ok,
        "status": status,
        "missing": missing,
        "forbidden": forbidden,
        "latency_ok": latency_ok,
        "latency_ms": elapsed_ms,
        "latency_budget_ms": latency_budget_ms,
        "final_number": final_number,
        "text_preview": text[:500],
        "raw_preview": raw[:500],
    }


def _run_responses_case(
    *,
    url: str,
    api_key: str,
    case: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    payload = {
        "input": case["input"],
        "store": False,
    }
    if case.get("instructions"):
        payload["instructions"] = case["instructions"]
    if case.get("tool_choice"):
        payload["tool_choice"] = case["tool_choice"]
    if "tools" in case:
        payload["tools"] = case["tools"]
    status, data, raw = _http_post_json(
        url,
        payload,
        timeout=timeout,
        api_key=api_key,
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    text = _extract_response_text(data)
    return _score_text_case(
        status=status,
        text=text,
        raw=raw,
        elapsed_ms=elapsed_ms,
        case=case,
    )


def _run_chat_completions_case(
    *,
    base_url: str,
    model: str,
    case: dict[str, Any],
    timeout: float,
    api_key: str = "",
) -> dict[str, Any]:
    started = time.perf_counter()
    messages: list[dict[str, str]] = []
    if case.get("instructions"):
        messages.append({"role": "system", "content": str(case["instructions"])})
    messages.append({"role": "user", "content": str(case["input"])})
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": int(case.get("max_tokens") or 64),
        "stream": False,
    }
    status, data, raw = _http_post_json(
        f"{base_url.rstrip('/')}/chat/completions",
        payload,
        timeout=timeout,
        api_key=api_key,
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    text = _extract_chat_completion_text(data)
    result = _score_text_case(
        status=status,
        text=text,
        raw=raw,
        elapsed_ms=elapsed_ms,
        case=case,
    )
    usage = data.get("usage") if isinstance(data, dict) else {}
    result["usage"] = usage if isinstance(usage, dict) else {}
    return result


def _canary_live_behavior(options: CanaryOptions) -> CanaryResult:
    if not options.live_behavior:
        return _result(
            "live.behavior_golden",
            SKIP,
            0,
            20,
            "Use --live-behavior to run LLM behavior goldens through /v1/responses",
        )
    base = (options.gateway_url or "").rstrip("/")
    if not base:
        return _result(
            "live.behavior_golden",
            FAIL if options.require_live else SKIP,
            0,
            20,
            "No gateway URL configured",
        )
    api_key, api_key_source = _resolve_api_key(options)
    if not api_key and not _gateway_url_is_loopback(base):
        return _result(
            "live.behavior_golden",
            FAIL if options.require_live else SKIP,
            0,
            20,
            "No API key available for /v1/responses",
            {"api_key_present": False},
        )
    if not api_key:
        api_key_source = "loopback_unauthenticated"

    cases = [
        {
            "name": "exact_response",
            "input": "Reply exactly HERMES_EVAL_OK",
            "required": ["HERMES_EVAL_OK"],
            "forbidden": ["cannot", "sorry", "as an ai"],
            "latency_budget_ms": 1000,
        },
        {
            "name": "testing_probe_direct",
            "input": "testing",
            "required": ["Hermes online", "operator mode"],
            "forbidden": ["Still working", "waiting for provider", "cannot", "sorry"],
            "latency_budget_ms": 1000,
        },
        {
            "name": "quality_score_direct",
            "input": "Score your quality score we measure your performance by",
            "required": [
                "Hermes",
                "Telegram E2E gate",
            ],
            "forbidden": ["cannot", "sorry", "as an ai", "waiting for model"],
            "latency_budget_ms": 1000,
        },
        {
            "name": "operator_menu_direct",
            "input": "what can we do",
            "required": ["Immediate AAC moves", "RFQ", "V11", "Reply with one word"],
            "forbidden": [
                "roleplay",
                "song",
                "poetry",
                "project management certification",
                "I'm glad",
                "cannot",
                "sorry",
            ],
            "latency_budget_ms": 1000,
        },
    ]
    passed: list[str] = []
    failed: dict[str, Any] = {}
    url = f"{base}/v1/responses"
    for case in cases:
        try:
            case_result = _run_responses_case(
                url=url,
                api_key=api_key,
                case=case,
                timeout=max(options.timeout, 30.0),
            )
        except Exception as exc:
            failed[case["name"]] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        if case_result["ok"]:
            passed.append(case["name"])
        else:
            failed[case["name"]] = case_result
    score = round(20.0 * len(passed) / len(cases), 1)
    result_status = PASS if not failed else FAIL if options.require_live else WARN
    return _result(
        "live.behavior_golden",
        result_status,
        score,
        20,
        f"{len(passed)}/{len(cases)} live LLM behavior goldens passed",
        {
            "api_key_present": bool(api_key),
            "api_key_source": api_key_source,
            "loopback_unauthenticated": api_key_source == "loopback_unauthenticated",
            "passed": passed,
            "failed": failed,
            "url": url,
        },
    )


def _resolve_deepseek_chat_route(options: CanaryOptions) -> tuple[str, str, dict[str, Any]]:
    snapshot = _env_model_snapshot(options)
    base_url = (
        snapshot.get("DEEPSEEK_V4_BASE_URL")
        or snapshot.get("DEEPSEEK_LOCAL_BASE_URL")
        or ""
    )
    model = (
        snapshot.get("DEEPSEEK_V4_MODEL")
        or snapshot.get("DEEPSEEK_LOCAL_MODEL")
        or snapshot.get("HERMES_PLANNER_MODEL")
        or ""
    )
    return str(base_url).rstrip("/"), str(model), snapshot


def _canary_local_model_reasoning_eval(options: CanaryOptions) -> CanaryResult:
    if not options.reasoning_eval:
        return _result(
            "eval.local_model_reasoning",
            SKIP,
            0,
            30,
            "Use --reasoning-eval to run deterministic tasks directly against the local DeepSeek model endpoint",
        )
    base_url, model, snapshot = _resolve_deepseek_chat_route(options)
    if not base_url or not model:
        return _result(
            "eval.local_model_reasoning",
            FAIL if options.require_live else WARN,
            0,
            30,
            "No local DeepSeek chat-completions route found in env snapshot",
            {"base_url_present": bool(base_url), "model_present": bool(model), "snapshot": snapshot},
        )

    cases = _reasoning_eval_cases(latency_budget_ms=45000, scaffold_arithmetic=True)
    passed: list[str] = []
    failed: dict[str, Any] = {}
    for case in cases:
        try:
            case_result = _run_chat_completions_case(
                base_url=base_url,
                model=model,
                case=case,
                timeout=max(options.timeout, 60.0),
            )
        except Exception as exc:
            failed[case["name"]] = _http_exception_details(exc)
            continue
        if case_result["ok"]:
            passed.append(case["name"])
        else:
            failed[case["name"]] = case_result
    score = round(30.0 * len(passed) / len(cases), 1)
    result_status = PASS if not failed else WARN
    return _result(
        "eval.local_model_reasoning",
        result_status,
        score,
        30,
        f"{len(passed)}/{len(cases)} direct DeepSeek model reasoning/guardrail evals passed",
        {
            "base_url": base_url,
            "model": model,
            "failure_class": "model_quality_failure" if failed else "",
            "passed": passed,
            "failed": failed,
            "snapshot": snapshot,
        },
    )


def _canary_hermes_reasoning_eval(options: CanaryOptions) -> CanaryResult:
    if not options.reasoning_eval:
        return _result(
            "eval.hermes_reasoning",
            SKIP,
            0,
            30,
            "Use --reasoning-eval to run deterministic DeepSeek tasks through the Hermes no-tool /v1/responses path",
        )
    base = (options.gateway_url or "").rstrip("/")
    if not base:
        return _result(
            "eval.hermes_reasoning",
            FAIL if options.require_live else SKIP,
            0,
            30,
            "No gateway URL configured",
        )
    api_key, api_key_source = _resolve_api_key(options)
    if not api_key and not _gateway_url_is_loopback(base):
        return _result(
            "eval.hermes_reasoning",
            FAIL if options.require_live else SKIP,
            0,
            30,
            "No API key available for /v1/responses",
            {"api_key_present": False},
        )
    if not api_key:
        api_key_source = "loopback_unauthenticated"

    cases = _reasoning_eval_cases(latency_budget_ms=45000)
    for case in cases:
        case["tool_choice"] = "none"
        case["tools"] = []
    passed: list[str] = []
    failed: dict[str, Any] = {}
    url = f"{base}/v1/responses"
    for case in cases:
        try:
            case_result = _run_responses_case(
                url=url,
                api_key=api_key,
                case=case,
                timeout=max(options.timeout, 60.0),
            )
        except Exception as exc:
            failed[case["name"]] = _http_exception_details(exc)
            continue
        if case_result["ok"]:
            passed.append(case["name"])
        else:
            failed[case["name"]] = case_result
    score = round(30.0 * len(passed) / len(cases), 1)
    result_status = PASS if not failed else FAIL if options.require_live else WARN
    return _result(
        "eval.hermes_reasoning",
        result_status,
        score,
        30,
        f"{len(passed)}/{len(cases)} Hermes no-tool reasoning/guardrail evals passed",
        {
            "api_key_present": bool(api_key),
            "api_key_source": api_key_source,
            "loopback_unauthenticated": api_key_source == "loopback_unauthenticated",
            "passed": passed,
            "failed": failed,
            "url": url,
        },
    )


def _resolve_frontier_api_key(options: CanaryOptions) -> tuple[str, str]:
    if options.frontier_api_key:
        return options.frontier_api_key, "argument"
    for env_name in ("HERMES_FRONTIER_API_KEY", "OPENAI_API_KEY"):
        value = os.getenv(env_name, "").strip()
        if value:
            return value, env_name
    return _resolve_env_wrapper_secret(
        options,
        ("HERMES_FRONTIER_API_KEY", "OPENAI_API_KEY"),
    )


def _resolve_env_wrapper_secret(
    options: CanaryOptions,
    keys: tuple[str, ...],
) -> tuple[str, str]:
    if not options.env_wrapper or not options.env_wrapper.is_file():
        return "", ""
    py = (
        "import json, os\n"
        f"keys = {list(keys)!r}\n"
        "print(json.dumps({key: os.environ.get(key, '') for key in keys}))\n"
    )
    script = "\n".join(
        [
            f"source {shlex.quote(str(options.env_wrapper))}",
            "if typeset -f aac_configure_hermes_deepseek_env >/dev/null; then",
            "  aac_configure_hermes_deepseek_env >/dev/null",
            "fi",
            "python3 - <<'PY'",
            py,
            "PY",
        ]
    )
    try:
        proc = subprocess.run(
            ["zsh", "-lc", script],
            capture_output=True,
            text=True,
            timeout=max(options.timeout, 1.0),
            check=False,
        )
    except Exception:
        return "", ""
    if proc.returncode != 0:
        return "", ""
    lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    if not lines:
        return "", ""
    try:
        data = json.loads(lines[-1])
    except json.JSONDecodeError:
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    for key in keys:
        value = str(data.get(key) or "").strip()
        if value:
            return value, f"env_wrapper:{key}"
    return "", ""


def _resolve_gemini_api_key(options: CanaryOptions) -> tuple[str, str]:
    for env_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        value = os.getenv(env_name, "").strip()
        if value:
            return value, env_name
    return _resolve_env_wrapper_secret(options, ("GEMINI_API_KEY", "GOOGLE_API_KEY"))


def _frontier_probe_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "ok": {"type": "boolean"},
            "answer": {"type": "integer"},
            "contract": {"type": "string"},
        },
        "required": ["ok", "answer", "contract"],
    }


def _frontier_probe_prompt() -> str:
    return (
        "Return only the JSON object. The JSON object must use exactly these keys: "
        "ok, answer, contract. Compute this probe: "
        "a frontier wrapper receives 4 batches, each with 17 checks, then "
        "drops 9 duplicate checks. How many unique checks remain? "
        "Set ok=true when you have followed the schema and include a short contract string."
    )


def _parse_frontier_probe_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end <= start:
            return {}
        try:
            parsed = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return {}
    return parsed if isinstance(parsed, dict) else {}


def _frontier_probe_passed(parsed: dict[str, Any], *, status: int) -> bool:
    return (
        status == 200
        and isinstance(parsed, dict)
        and parsed.get("ok") is True
        and parsed.get("answer") == 59
        and isinstance(parsed.get("contract"), str)
        and bool(parsed.get("contract", "").strip())
    )


def _run_openai_frontier_probe(options: CanaryOptions) -> dict[str, Any]:
    api_key, api_key_source = _resolve_frontier_api_key(options)
    model = (options.frontier_model or os.getenv("OPENAI_FRONTIER_MODEL") or "gpt-5.5").strip()
    base_url = (options.frontier_base_url or "https://api.openai.com/v1").rstrip("/")
    attempt: dict[str, Any] = {
        "provider": "openai",
        "api_key_present": bool(api_key),
        "api_key_source": api_key_source,
        "model": model,
        "base_url": base_url,
    }
    if not api_key:
        attempt.update({"ok": False, "summary": "No OpenAI frontier API key available"})
        return attempt

    payload = {
        "model": model,
        "input": _frontier_probe_prompt(),
        "reasoning": {"effort": "low"},
        "text": {
            "format": {
                "type": "json_schema",
                "name": "hermes_frontier_probe",
                "schema": _frontier_probe_schema(),
                "strict": True,
            }
        },
        "store": False,
        "max_output_tokens": 300,
    }
    started = time.perf_counter()
    try:
        status, data, raw = _http_post_json(
            f"{base_url}/responses",
            payload,
            timeout=max(options.timeout, 30.0),
            api_key=api_key,
        )
    except urllib.error.HTTPError as exc:
        details = _http_error_details(exc)
        attempt.update(
            {
                "ok": False,
                "summary": f"OpenAI frontier probe failed: HTTP {details['status']}",
                **details,
            }
        )
        return attempt
    except Exception as exc:
        attempt.update(
            {
                "ok": False,
                "summary": f"OpenAI frontier probe failed: {type(exc).__name__}",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
        return attempt

    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    text = _extract_response_text(data)
    parsed = _parse_frontier_probe_text(text)
    ok = _frontier_probe_passed(parsed, status=status)
    usage = data.get("usage") if isinstance(data, dict) else {}
    attempt.update(
        {
            "ok": ok,
            "summary": (
                f"{model} Responses wrapper passed structured reasoning probe in {elapsed_ms:.0f}ms"
                if ok
                else "OpenAI frontier wrapper returned an invalid structured probe result"
            ),
            "status": status,
            "latency_ms": elapsed_ms,
            "parsed": parsed,
            "usage": usage if isinstance(usage, dict) else {},
            "text_preview": text[:500],
            "raw_preview": raw[:500],
        }
    )
    return attempt


def _extract_gemini_text(data: dict[str, Any] | None) -> str:
    if not isinstance(data, dict):
        return ""
    parts_out: list[str] = []
    for candidate in data.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        for part in content.get("parts") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts_out.append(part["text"])
    return "\n".join(parts_out).strip()


def _gemini_model_candidates() -> list[str]:
    candidates: list[str] = []
    primary = (
        os.getenv("HERMES_GEMINI_FRONTIER_MODEL")
        or os.getenv("GEMINI_FRONTIER_MODEL")
        or "gemini-2.5-pro"
    ).strip()
    if primary:
        candidates.append(primary)
    fallback_raw = os.getenv("HERMES_GEMINI_FALLBACK_MODELS", "gemini-2.5-flash")
    for item in fallback_raw.split(","):
        model = item.strip()
        if model and model not in candidates:
            candidates.append(model)
    return candidates


def _run_gemini_model_probe(
    *,
    api_key: str,
    api_key_source: str,
    model: str,
    base_url: str,
    options: CanaryOptions,
) -> dict[str, Any]:
    resource = model if model.startswith("models/") else f"models/{model}"
    safe_url = f"{base_url}/{resource}:generateContent"
    attempt: dict[str, Any] = {
        "provider": "gemini",
        "api_key_present": bool(api_key),
        "api_key_source": api_key_source,
        "model": model,
        "base_url": base_url,
        "url": safe_url,
    }
    if not api_key:
        attempt.update({"ok": False, "summary": "No Gemini frontier API key available"})
        return attempt

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": _frontier_probe_prompt()}],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 1024,
            "responseMimeType": "application/json",
        },
    }
    url = (
        f"{base_url}/{urllib.parse.quote(resource, safe='/')}:generateContent"
        f"?key={urllib.parse.quote(api_key)}"
    )
    started = time.perf_counter()
    try:
        status, data, raw = _http_post_json(url, payload, timeout=max(options.timeout, 30.0))
    except urllib.error.HTTPError as exc:
        details = _http_error_details(exc)
        attempt.update(
            {
                "ok": False,
                "summary": f"Gemini frontier probe failed: HTTP {details['status']}",
                **details,
            }
        )
        return attempt
    except Exception as exc:
        attempt.update(
            {
                "ok": False,
                "summary": f"Gemini frontier probe failed: {type(exc).__name__}",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
        return attempt

    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
    text = _extract_gemini_text(data)
    parsed = _parse_frontier_probe_text(text)
    ok = _frontier_probe_passed(parsed, status=status)
    usage = data.get("usageMetadata") if isinstance(data, dict) else {}
    attempt.update(
        {
            "ok": ok,
            "summary": (
                f"{model} Gemini wrapper passed structured reasoning probe in {elapsed_ms:.0f}ms"
                if ok
                else "Gemini frontier wrapper returned an invalid structured probe result"
            ),
            "status": status,
            "latency_ms": elapsed_ms,
            "parsed": parsed,
            "usage": usage if isinstance(usage, dict) else {},
            "text_preview": text[:500],
            "raw_preview": raw[:500],
        }
    )
    return attempt


def _run_gemini_frontier_probe(options: CanaryOptions) -> dict[str, Any]:
    api_key, api_key_source = _resolve_gemini_api_key(options)
    base_url = os.getenv("HERMES_GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    model_attempts: list[dict[str, Any]] = []
    if not api_key:
        return {
            "provider": "gemini",
            "api_key_present": False,
            "api_key_source": api_key_source,
            "model": _gemini_model_candidates()[0],
            "base_url": base_url,
            "ok": False,
            "summary": "No Gemini frontier API key available",
        }
    for model in _gemini_model_candidates():
        attempt = _run_gemini_model_probe(
            api_key=api_key,
            api_key_source=api_key_source,
            model=model,
            base_url=base_url,
            options=options,
        )
        model_attempts.append(attempt)
        if attempt.get("ok"):
            successful_attempt = dict(attempt)
            successful_attempt["model_attempts"] = [dict(item) for item in model_attempts]
            return successful_attempt
        retryable_error = str(attempt.get("error_type") or "") in {
            "TimeoutError",
            "ConnectionError",
            "URLError",
        }
        if int(attempt.get("status") or 0) not in {429, 500, 502, 503, 504} and not retryable_error:
            break
    final_attempt = dict(model_attempts[-1] if model_attempts else {})
    final_attempt["model_attempts"] = model_attempts
    return final_attempt


def _canary_frontier_wrapper(options: CanaryOptions) -> CanaryResult:
    if not options.frontier_eval:
        return _result(
            "eval.frontier_wrapper",
            SKIP,
            0,
            20,
            "Use --frontier-eval to verify a frontier wrapper with structured reasoning controls",
            {
                "docs_basis": [
                    "OpenAI Responses API",
                    "Gemini generateContent API",
                    "Structured Outputs",
                    "structured JSON probe validation",
                ]
            },
        )
    provider = os.getenv("HERMES_FRONTIER_PROVIDER", "auto").strip().lower() or "auto"
    attempts: list[dict[str, Any]] = []
    if provider not in {"gemini", "google"}:
        attempts.append(_run_openai_frontier_probe(options))
    if provider in {"auto", "gemini", "google"} and not any(attempt.get("ok") for attempt in attempts):
        attempts.append(_run_gemini_frontier_probe(options))

    for attempt in attempts:
        if attempt.get("ok"):
            return _result(
                "eval.frontier_wrapper",
                PASS,
                20,
                20,
                str(attempt.get("summary") or "Frontier wrapper passed structured reasoning probe"),
                {
                    "provider": attempt.get("provider"),
                    "model": attempt.get("model"),
                    "latency_ms": attempt.get("latency_ms"),
                    "usage": attempt.get("usage") if isinstance(attempt.get("usage"), dict) else {},
                    "parsed": attempt.get("parsed") if isinstance(attempt.get("parsed"), dict) else {},
                    "attempts": attempts,
                },
            )

    summary = "No frontier wrapper backend passed the structured reasoning probe"
    if attempts:
        summary = str(attempts[-1].get("summary") or summary)
    return _result(
        "eval.frontier_wrapper",
        FAIL if options.require_live else WARN,
        0,
        20,
        summary,
        {
            "provider": provider,
            "attempts": attempts,
        },
    )


def _canary_telegram_e2e(options: CanaryOptions) -> CanaryResult:
    simulation: dict[str, Any] = {}
    if options.telegram_webhook_sim:
        simulation = _run_telegram_webhook_simulation(options)
    evidence_path = options.hermes_home / "canary" / "telegram_e2e_last.json"
    if not evidence_path.is_file():
        details: dict[str, Any] = {
            "evidence_path": str(evidence_path),
            "required_evidence": {
                "status": "pass",
                "latency_ms": "<= latency_budget_ms",
                "no_interruption": True,
                "no_capability_refusal": True,
                "restart_during_task": False,
            },
        }
        if simulation:
            details["simulation"] = simulation
        return _result(
            "live.telegram_e2e",
            WARN,
            0,
            20,
            "No live Telegram E2E latency/restart evidence; frontier readiness is capped until this passes",
            details,
        )

    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _result(
            "live.telegram_e2e",
            FAIL if options.require_live else WARN,
            0,
            20,
            f"Telegram E2E evidence could not be parsed: {type(exc).__name__}",
            {"evidence_path": str(evidence_path), "error": str(exc)},
        )
    if not isinstance(payload, dict):
        return _result(
            "live.telegram_e2e",
            FAIL if options.require_live else WARN,
            0,
            20,
            "Telegram E2E evidence is not a JSON object",
            {"evidence_path": str(evidence_path)},
        )

    latency_budget_ms = float(payload.get("latency_budget_ms") or 15000)
    latency_ms = float(payload.get("latency_ms") or 0)
    checks = {
        "status_pass": str(payload.get("status") or "").lower() == PASS,
        "latency_budget": latency_ms > 0 and latency_ms <= latency_budget_ms,
        "no_interruption": bool(payload.get("no_interruption")),
        "no_capability_refusal": bool(payload.get("no_capability_refusal")),
        "no_restart_during_task": not bool(payload.get("restart_during_task")),
    }
    if simulation.get("nonce"):
        checks["simulation_nonce_match"] = payload.get("nonce") == simulation.get("nonce")
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        return _result(
            "live.telegram_e2e",
            FAIL if options.require_live else WARN,
            0,
            20,
            "Telegram E2E evidence exists but did not pass latency/restart/refusal checks",
            {
                "evidence_path": str(evidence_path),
                "failed": failed,
                "checks": checks,
                "payload": payload,
                "simulation": simulation,
            },
        )

    return _result(
        "live.telegram_e2e",
        PASS,
        20,
        20,
        f"Telegram E2E passed in {latency_ms:.0f}ms without restart interruption or capability refusal",
        {
            "evidence_path": str(evidence_path),
            "checks": checks,
            "payload": payload,
            "simulation": simulation,
        },
    )


def _telegram_e2e_channel_id(options: CanaryOptions) -> str:
    env_value = os.getenv("HERMES_CANARY_TELEGRAM_CHAT_ID", "").strip()
    if env_value:
        return env_value
    channel_directory = options.hermes_home / "channel_directory.json"
    try:
        payload = json.loads(channel_directory.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    platforms = payload.get("platforms") if isinstance(payload, dict) else {}
    telegram_channels = platforms.get("telegram") if isinstance(platforms, dict) else []
    if isinstance(telegram_channels, list):
        for item in telegram_channels:
            if isinstance(item, dict) and item.get("id"):
                return str(item["id"])
    return ""


def _post_telegram_webhook_update(
    *,
    url: str,
    secret: str,
    update: dict[str, Any],
    timeout: float,
) -> tuple[int, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Telegram-Bot-Api-Secret-Token": secret,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(update).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(1024 * 1024).decode("utf-8", errors="replace")
        return int(resp.status), raw[:1000]


def _run_telegram_webhook_simulation(options: CanaryOptions) -> dict[str, Any]:
    secret_path = options.hermes_home / "private" / "telegram-webhook-secret"
    pending_path = options.hermes_home / "canary" / "telegram_e2e_pending.json"
    evidence_path = options.hermes_home / "canary" / "telegram_e2e_last.json"
    local_url = os.getenv(
        "HERMES_CANARY_TELEGRAM_WEBHOOK_URL",
        f"http://127.0.0.1:{os.getenv('TELEGRAM_WEBHOOK_PORT', '8443')}/telegram",
    )
    chat_id = _telegram_e2e_channel_id(options)
    started = time.time()
    details: dict[str, Any] = {
        "mode": "signed_webhook_simulation",
        "url": local_url,
        "secret_path": str(secret_path),
        "pending_path": str(pending_path),
        "evidence_path": str(evidence_path),
        "chat_id_present": bool(chat_id),
    }
    if not chat_id:
        details.update({"ok": False, "error": "No Telegram channel id found"})
        return details
    try:
        secret = secret_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        details.update({"ok": False, "error": f"webhook secret unavailable: {exc}"})
        return details
    if not secret:
        details.update({"ok": False, "error": "webhook secret is empty"})
        return details

    nonce = f"sim-{int(started * 1000)}"
    ack = f"ack {nonce}"
    pending = {
        "status": "sent",
        "mode": "signed_webhook_simulation",
        "chat_id": chat_id,
        "sent_at": started,
        "awaiting_reply": ack,
        "latency_budget_ms": 15000,
        "nonce": nonce,
        "text": f"Hermes signed webhook simulation {nonce}",
    }
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    pending_path.write_text(
        json.dumps(pending, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    try:
        chat_id_int = int(chat_id)
    except ValueError:
        details.update({"ok": False, "error": "Telegram channel id is not an integer"})
        return details
    update_id = int(started * 1000) % 2_000_000_000
    update = {
        "update_id": update_id,
        "message": {
            "message_id": update_id % 1_000_000,
            "from": {
                "id": chat_id_int,
                "is_bot": False,
                "first_name": "Hermes",
                "username": "hermes_e2e",
            },
            "chat": {
                "id": chat_id_int,
                "type": "private",
                "first_name": "Hermes",
            },
            "date": int(started),
            "text": ack,
        },
    }
    try:
        status, raw = _post_telegram_webhook_update(
            url=local_url,
            secret=secret,
            update=update,
            timeout=max(options.timeout, 10.0),
        )
    except Exception as exc:
        details.update(
            {
                "ok": False,
                "nonce": nonce,
                "error": f"{type(exc).__name__}: {exc}",
                "duration_ms": round((time.time() - started) * 1000.0, 1),
            }
        )
        return details

    deadline = time.time() + max(3.0, min(options.timeout, 10.0))
    evidence: dict[str, Any] = {}
    while time.time() < deadline:
        try:
            evidence_payload = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            time.sleep(0.2)
            continue
        if isinstance(evidence_payload, dict) and evidence_payload.get("nonce") == nonce:
            evidence = evidence_payload
            break
        time.sleep(0.2)
    details.update(
        {
            "ok": bool(evidence),
            "nonce": nonce,
            "status": status,
            "raw_preview": raw[:200],
            "duration_ms": round((time.time() - started) * 1000.0, 1),
            "evidence_observed": bool(evidence),
            "evidence_latency_ms": evidence.get("latency_ms") if evidence else None,
        }
    )
    if not evidence:
        details["error"] = "Webhook POST returned but no matching evidence was written"
    return details


def _telegram_api_request(
    bot_token: str,
    method: str,
    payload: dict[str, Any] | None,
    timeout: float,
) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = urllib.parse.urlencode(
            {key: str(value) for key, value in payload.items()}
        ).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        decoded = json.loads(resp.read().decode("utf-8"))
    return decoded if isinstance(decoded, dict) else {}


def _run_telegram_visible_probe(options: CanaryOptions) -> dict[str, Any]:
    _load_canary_env_files()
    bot_token = _env_value("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("HERMES_CANARY_TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        chat_id = os.getenv("TELEGRAM_HOME_CHANNEL", "").strip()
    if not chat_id:
        chat_id = _telegram_e2e_channel_id(options)

    pending_path = options.hermes_home / "canary" / "telegram_e2e_pending.json"
    evidence_path = options.hermes_home / "canary" / "telegram_e2e_last.json"
    details: dict[str, Any] = {
        "mode": "real_visible_delivery_probe",
        "chat_id_present": bool(chat_id),
        "pending_path": str(pending_path),
        "evidence_path": str(evidence_path),
    }
    if not bot_token:
        details.update({"ok": False, "error": "TELEGRAM_BOT_TOKEN is not configured"})
        return details
    if not chat_id:
        details.update({"ok": False, "error": "Telegram chat id is not configured"})
        return details

    started = time.time()
    nonce = f"real-{int(started)}"
    ack = f"ack {nonce}"
    text = f"Hermes visible delivery probe {nonce}. Reply: {ack} or ack."
    try:
        get_me = _telegram_api_request(
            bot_token,
            "getMe",
            None,
            max(options.timeout, 10.0),
        )
        chat = _telegram_api_request(
            bot_token,
            "getChat",
            {"chat_id": chat_id},
            max(options.timeout, 10.0),
        )
        webhook = _telegram_api_request(
            bot_token,
            "getWebhookInfo",
            None,
            max(options.timeout, 10.0),
        )
        sent = _telegram_api_request(
            bot_token,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "disable_notification": "false",
            },
            max(options.timeout, 10.0),
        )
    except Exception as exc:
        details.update(
            {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "failure_class": "telegram_delivery_failure",
            }
        )
        return details

    message = sent.get("result") if isinstance(sent.get("result"), dict) else {}
    pending = {
        "status": "sent",
        "mode": "real_visible_delivery_probe",
        "chat_id": str(chat_id),
        "sent_at": time.time(),
        "awaiting_reply": ack,
        "latency_budget_ms": 60_000,
        "nonce": nonce,
        "text": text,
        "message_id": message.get("message_id"),
    }
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    pending_path.write_text(
        json.dumps(pending, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    wait_seconds = max(0.0, min(float(options.telegram_visible_wait), 120.0))
    deadline = time.time() + wait_seconds
    evidence: dict[str, Any] = {}
    while time.time() < deadline:
        try:
            payload = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            time.sleep(0.5)
            continue
        if isinstance(payload, dict) and payload.get("nonce") == nonce:
            evidence = payload
            break
        time.sleep(0.5)

    bot = get_me.get("result") if isinstance(get_me.get("result"), dict) else {}
    chat_result = chat.get("result") if isinstance(chat.get("result"), dict) else {}
    webhook_result = (
        webhook.get("result") if isinstance(webhook.get("result"), dict) else {}
    )
    message_chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    details.update(
        {
            "ok": bool(evidence),
            "nonce": nonce,
            "expected_reply": ack,
            "bot": {
                "ok": bool(get_me.get("ok")),
                "id": bot.get("id"),
                "username": bot.get("username"),
                "first_name": bot.get("first_name"),
            },
            "chat": {
                "ok": bool(chat.get("ok")),
                "id": chat_result.get("id"),
                "type": chat_result.get("type"),
                "username": chat_result.get("username"),
                "first_name": chat_result.get("first_name"),
                "last_name": chat_result.get("last_name"),
            },
            "send": {
                "ok": bool(sent.get("ok")),
                "message_id": message.get("message_id"),
                "date": message.get("date"),
                "chat_id": message_chat.get("id"),
            },
            "webhook": {
                "ok": bool(webhook.get("ok")),
                "url": webhook_result.get("url"),
                "pending_update_count": webhook_result.get("pending_update_count"),
                "last_error_date": webhook_result.get("last_error_date"),
                "last_error_message": webhook_result.get("last_error_message"),
            },
            "evidence": evidence,
            "wait_seconds": wait_seconds,
            "duration_ms": round((time.time() - started) * 1000.0, 1),
        }
    )
    return details


def _canary_telegram_visible_delivery(options: CanaryOptions) -> CanaryResult:
    if not options.telegram_visible_probe:
        return _result(
            "live.telegram_visible_delivery",
            SKIP,
            0,
            20,
            "Visible Telegram probe not enabled; pass --telegram-visible-probe to require a real DM ack",
            {"enabled": False},
        )

    probe = _run_telegram_visible_probe(options)
    if probe.get("ok"):
        evidence = probe.get("evidence") if isinstance(probe.get("evidence"), dict) else {}
        latency_ms = float(evidence.get("latency_ms") or 0)
        ack_match = str(evidence.get("ack_match") or "exact")
        return _result(
            "live.telegram_visible_delivery",
            PASS,
            20,
            20,
            f"Visible Telegram delivery {ack_match} ack passed in {latency_ms:.0f}ms",
            {"enabled": True, "probe": probe, "failure_class": ""},
        )

    send = probe.get("send") if isinstance(probe.get("send"), dict) else {}
    send_ok = bool(send.get("ok"))
    status = WARN if send_ok else FAIL if options.require_live else WARN
    summary = (
        "Telegram sendMessage returned OK, but no real visible ack was observed"
        if send_ok
        else f"Visible Telegram probe failed: {probe.get('error', 'unknown error')}"
    )
    return _result(
        "live.telegram_visible_delivery",
        status,
        8 if send_ok else 0,
        20,
        summary,
        {
            "enabled": True,
            "probe": probe,
            "failure_class": "telegram_visible_ack_missing"
            if send_ok
            else "telegram_delivery_failure",
        },
    )


def _canary_scorecard_trend(options: CanaryOptions) -> CanaryResult:
    reports_dir = options.output_dir or (options.hermes_home / "canary" / "reports")
    latest_json = reports_dir / "latest.json"
    latest_markdown = reports_dir / "latest.md"
    history_jsonl = reports_dir / "history.jsonl"
    daily_script = options.hermes_home / "bin" / "hermes-canary-daily"
    launch_agents_dir = Path.home() / "Library" / "LaunchAgents"
    launch_agent_matches = sorted(launch_agents_dir.glob("*hermes*canary*.plist"))

    missing: list[str] = []
    if not latest_json.is_file():
        missing.append(str(latest_json))
    if not latest_markdown.is_file():
        missing.append(str(latest_markdown))
    if not history_jsonl.is_file():
        missing.append(str(history_jsonl))
    if not daily_script.is_file():
        missing.append(str(daily_script))
    if not launch_agent_matches:
        missing.append(str(launch_agents_dir / "*hermes*canary*.plist"))

    history_entries = 0
    latest_status = ""
    latest_quality_score = 0.0
    parse_errors: list[str] = []
    if history_jsonl.is_file():
        try:
            for line in history_jsonl.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                history_entries += 1
                try:
                    json.loads(line)
                except json.JSONDecodeError as exc:
                    parse_errors.append(f"history line {history_entries}: {exc.msg}")
        except OSError as exc:
            parse_errors.append(str(exc))
    if latest_json.is_file():
        try:
            latest_payload = json.loads(latest_json.read_text(encoding="utf-8"))
            if isinstance(latest_payload, dict):
                latest_status = str(latest_payload.get("status", ""))
                quality = latest_payload.get("overall_quality", {})
                if isinstance(quality, dict):
                    latest_quality_score = float(quality.get("score", 0.0) or 0.0)
        except (OSError, ValueError, TypeError) as exc:
            parse_errors.append(f"latest.json: {exc}")

    if missing or parse_errors or history_entries < 1:
        problems = {}
        if missing:
            problems["missing"] = missing
        if parse_errors:
            problems["parse_errors"] = parse_errors
        if history_entries < 1:
            problems["history_entries"] = history_entries
        return _result(
            "contract.scorecard_trend",
            WARN,
            4,
            10,
            "Metrics trend or daily scheduler is not fully installed",
            {
                **problems,
                "reports_dir": str(reports_dir),
                "daily_script": str(daily_script),
                "launch_agents": [str(path) for path in launch_agent_matches],
            },
        )

    return _result(
        "contract.scorecard_trend",
        PASS,
        10,
        10,
        "Latest metrics report, history JSONL, and daily launchd canary are installed",
        {
            "reports_dir": str(reports_dir),
            "history_entries": history_entries,
            "latest_status": latest_status,
            "latest_quality_score": latest_quality_score,
            "daily_script": str(daily_script),
            "launch_agents": [str(path) for path in launch_agent_matches],
        },
    )


def _canary_aac_workflow_goldens(options: CanaryOptions) -> CanaryResult:
    from gateway import context_router
    from hermes_cli.workspace import WorkspaceStore

    scenarios = [
        {
            "name": "rfq_quote_prep",
            "message": "RFQ: Turkish Technic needs 762367B. Prep a quote with V11 stock, customer history, and price support.",
            "paths": {
                "_system/ROUTING.md",
                "advanced/pricing/CONTEXT.md",
                "advanced/customers/CONTEXT.md",
                "advanced/operations/CONTEXT.md",
                "advanced/products/CONTEXT.md",
            },
        },
        {
            "name": "v11_part_lookup",
            "message": "Look up V11 inventory and sales history for part number 713442.",
            "paths": {
                "_system/ROUTING.md",
                "advanced/operations/CONTEXT.md",
                "advanced/products/CONTEXT.md",
            },
        },
        {
            "name": "quote_workspace_report",
            "message": "Prepare a quote for Aviation Spare Parts PTY LTD for 762367B and create a Workspace report.",
            "paths": {
                "_system/ROUTING.md",
                "advanced/pricing/CONTEXT.md",
                "advanced/customers/CONTEXT.md",
                "advanced/operations/CONTEXT.md",
                "advanced/products/CONTEXT.md",
            },
        },
    ]

    failed: dict[str, Any] = {}
    passed: list[str] = []
    for scenario in scenarios:
        name = scenario["name"]
        message = scenario["message"]
        if not context_router._message_requests_alexandria_context(message):
            failed[name] = "message did not request Alexandria/V11 context"
            continue
        paths = set(context_router._direct_alexandria_context_paths(message))
        missing_paths = sorted(scenario["paths"] - paths)
        if missing_paths:
            failed[name] = {"missing_paths": missing_paths, "paths": sorted(paths)}
            continue
        passed.append(name)

    exact_probe = "Reply exactly HERMES_EVAL_OK"
    if context_router._message_requests_alexandria_context(exact_probe):
        failed["exact_probe_bypass"] = "exact diagnostic probe routed into Alexandria context"
    else:
        passed.append("exact_probe_bypass")

    source_paths = [
        options.repo_root / "gateway" / "context_router.py",
        options.repo_root / "hermes_cli" / "workspace.py",
        Path.home() / "alexandria" / "_system" / "ROUTING.md",
        Path.home() / "alexandria" / "advanced" / "operations" / "CONTEXT.md",
        Path.home() / "alexandria" / "advanced" / "pricing" / "CONTEXT.md",
    ]
    missing_sources = [str(path) for path in source_paths if not path.is_file()]
    if missing_sources:
        failed["source_files"] = {"missing": missing_sources}
    else:
        passed.append("source_files")

    with tempfile.TemporaryDirectory(prefix="hermes-aac-workflow-") as tmp:
        store = WorkspaceStore(Path(tmp) / "workspace.json")
        task = store.create_task(
            "AAC quote workflow golden",
            note="RFQ 762367B: verify V11 lookup, price support, and report capture.",
        )
        store.update_task(task["id"], status="in_progress")
        store.add_evidence(
            task_id=task["id"],
            kind="canary",
            title="V11/pricing/customer context routes resolved",
            summary="RFQ, V11 part lookup, quote prep, and Workspace report goldens passed.",
        )
        store.update_task(task["id"], status="done")
        report = store.create_report(title="AAC workflow golden report")
        report_path = Path(report.get("path", ""))
        if not report_path.is_file():
            failed["workspace_report"] = {"path": str(report_path)}
        else:
            report_text = report_path.read_text(encoding="utf-8")
            required = ["AAC workflow golden report", "V11/pricing/customer"]
            missing_text = [item for item in required if item not in report_text]
            if missing_text:
                failed["workspace_report"] = {"missing_text": missing_text}
            else:
                passed.append("workspace_report")

    total_checks = len(scenarios) + 3
    score = round(15.0 * len(passed) / total_checks, 1)
    status = PASS if not failed else WARN if passed else FAIL
    return _result(
        "contract.aac_workflows",
        status,
        score,
        15,
        f"{len(passed)}/{total_checks} AAC RFQ/V11/quote/Workspace goldens passed",
        {"passed": passed, "failed": failed},
    )


def _env_file_has_any_key(path: Path, keys: set[str]) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() in keys and value.strip().strip("'\""):
            return True
    return False


def _canary_quote_ops_runtime(options: CanaryOptions) -> CanaryResult:
    service_dir = Path.home() / ".hermes" / "services"
    files = {
        "service_env": service_dir / "service_env.py",
        "ils_auto_quote": service_dir / "ils_auto_quote.py",
        "quote_notify": service_dir / "quote_notify.py",
        "quote_approval_bot": service_dir / "quote_approval_bot.py",
        "quote_pdf": service_dir / "quote_pdf.py",
        "quote_pdf_server": service_dir / "quote_pdf_server.py",
    }
    launch_agents = [
        Path.home() / "Library" / "LaunchAgents" / "com.aac.ils-auto-quote.plist",
        Path.home() / "Library" / "LaunchAgents" / "com.aac.quote-approval-bot.plist",
        Path.home() / "Library" / "LaunchAgents" / "com.aac.quote-pdf-server.plist",
    ]

    failed: dict[str, Any] = {}
    passed: list[str] = []

    missing_files = [str(path) for path in files.values() if not path.is_file()]
    if missing_files:
        failed["service_files"] = missing_files
    else:
        passed.append("service_files")

    missing_launch_agents = [str(path) for path in launch_agents if not path.is_file()]
    if missing_launch_agents:
        failed["launch_agents"] = missing_launch_agents
    else:
        passed.append("launch_agents")

    source_text = ""
    if not missing_files:
        source_text = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in files.values()
        )
        forbidden_literals = ["advanced8125", "diamond11", "8714830761:", "TELEGRAM_BOT_TOKEN\", \""]
        found_literals = [literal for literal in forbidden_literals if literal in source_text]
        if found_literals:
            failed["embedded_credential_fallbacks"] = found_literals
        else:
            passed.append("no_embedded_credential_fallbacks")

        if "require_env(" in source_text and "load_service_env()" in source_text:
            passed.append("env_driven_services")
        else:
            failed["env_driven_services"] = "quote services do not load env and fail closed"

        approval_terms = ["Approve", "Reject", "email draft", "cancel the V11 draft"]
        missing_approval_terms = [term for term in approval_terms if term not in source_text]
        if missing_approval_terms:
            failed["approval_gate_terms"] = missing_approval_terms
        else:
            passed.append("approval_gate_terms")

        approval_bot_text = files["quote_approval_bot"].read_text(
            encoding="utf-8",
            errors="replace",
        )
        forbidden_customer_send = [
            "from gmail_draft import send_email",
            "send_email(",
            "Email delivered to",
        ]
        found_customer_send = [
            term for term in forbidden_customer_send if term in approval_bot_text
        ]
        if found_customer_send:
            failed["customer_send_blocked_on_approve"] = found_customer_send
        elif "Customer email was not sent" not in approval_bot_text:
            failed["customer_send_blocked_on_approve"] = "approve path lacks explicit no-send operator message"
        else:
            passed.append("customer_send_blocked_on_approve")

    env_sources = [
        Path.home() / ".hermes" / ".env",
        Path.home() / ".secrets" / "alexandria.env",
    ]
    required_env_groups = {
        "v11_password": {"ODOO_PASSWORD", "V11_WEB_PASS"},
        "telegram_token": {
            "TELEGRAM_APPROVAL_BOT_TOKEN",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_BOT_TOKEN_CZAR",
            "TELEGRAM_BOT_TOKEN_HERMESAC",
        },
        "ils_credentials": {"ILS_PASSWORD", "ILS_PASS"},
    }
    env_presence = {
        name: any(_env_file_has_any_key(path, keys) for path in env_sources)
        for name, keys in required_env_groups.items()
    }
    missing_env = [name for name, present in env_presence.items() if not present]
    if missing_env:
        failed["required_env"] = missing_env
    else:
        passed.append("required_env")

    try:
        status, data, _raw = _http_get_json("http://127.0.0.1:8699/health", min(options.timeout, 5.0))
        if status == 200 and isinstance(data, dict) and data.get("status") == "ok":
            passed.append("quote_pdf_health")
        else:
            failed["quote_pdf_health"] = {"status": status, "response": data}
    except Exception as exc:
        failed["quote_pdf_health"] = _http_exception_details(exc)

    total_checks = 8
    score = round(20.0 * len(passed) / total_checks, 1)
    status = PASS if not failed else WARN if passed else FAIL
    return _result(
        "contract.quote_ops_runtime",
        status,
        score,
        20,
        f"{len(passed)}/{total_checks} quote-ops runtime checks passed",
        {
            "service_dir": str(service_dir),
            "passed": passed,
            "failed": failed,
            "env_presence": env_presence,
            "launch_agents": [str(path) for path in launch_agents],
        },
    )


def _load_canary_env_files() -> dict[str, Any]:
    """Load operator env files without exposing values in canary reports."""
    loaded_keys: set[str] = set()
    present_files: list[str] = []
    for path in (Path.home() / ".hermes" / ".env", Path.home() / ".secrets" / "alexandria.env"):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        present_files.append(str(path))
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if not key or not value or key in os.environ:
                continue
            os.environ[key] = value
            loaded_keys.add(key)
    return {"files": present_files, "loaded_keys": sorted(loaded_keys)}


def _env_value(name: str, *aliases: str) -> str:
    for key in (name, *aliases):
        value = os.getenv(key, "").strip()
        if value:
            return value
    return ""


class _V11ReadOnlyClient:
    """Narrow XML-RPC client restricted to read-only V11 methods."""

    _ALLOWED_METHODS = {"search", "search_read", "read", "fields_get"}

    def __init__(self) -> None:
        self.url = os.getenv("ODOO_URL", "https://v11.advanced.aero").rstrip("/")
        self.db = os.getenv("ODOO_DB", "advancedaero")
        self.username = os.getenv("ODOO_USER", "ac@advanced.aero")
        self.password = _env_value("ODOO_PASSWORD", "V11_WEB_PASS")
        if not self.password:
            raise RuntimeError("missing V11 credentials: ODOO_PASSWORD or V11_WEB_PASS")
        self.uid: int | bool = False
        self._common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common", allow_none=True)
        self._models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object", allow_none=True)

    def authenticate(self) -> bool:
        self.uid = self._common.authenticate(self.db, self.username, self.password, {})
        return bool(self.uid)

    def execute(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        if method not in self._ALLOWED_METHODS:
            raise ValueError(f"refusing non-read-only V11 method: {model}.{method}")
        if not self.uid and not self.authenticate():
            raise ConnectionError("V11 authentication failed")
        return self._models.execute_kw(
            self.db,
            self.uid,
            self.password,
            model,
            method,
            list(args),
            kwargs,
        )

    def search_read(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        *,
        limit: int = 20,
        order: str | None = None,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"fields": fields, "limit": limit}
        if order:
            kwargs["order"] = order
        result = self.execute(model, "search_read", domain, **kwargs)
        return result if isinstance(result, list) else []

    def read(self, model: str, ids: list[int], fields: list[str]) -> list[dict[str, Any]]:
        result = self.execute(model, "read", ids, fields=fields)
        return result if isinstance(result, list) else []


def _float_or_zero(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _m2o_id(value: Any) -> int | None:
    if isinstance(value, list) and value:
        try:
            return int(value[0])
        except (TypeError, ValueError):
            return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _m2o_name(value: Any) -> str:
    if isinstance(value, list) and len(value) > 1:
        return str(value[1])
    return str(value or "")


def _read_csv_match(path: Path, key: str, value: str) -> dict[str, str] | None:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                candidate = str(row.get(key, "")).strip().strip('"')
                if candidate.lower() == value.lower():
                    return {str(k): str(v) for k, v in row.items()}
    except OSError:
        return None
    return None


def _read_customer_pricing_summary(customer_name: str) -> dict[str, Any]:
    path = Path.home() / "alexandria" / "advanced" / "pricing" / "data" / "v11_customer_pricing.csv"
    row = _read_csv_match(path, "customer_name", customer_name)
    if not row:
        return {"source": str(path), "found": False}
    total_revenue = _float_or_zero(row.get("total_revenue"))
    if total_revenue >= 500_000:
        tier = "PLATINUM"
    elif total_revenue >= 250_000:
        tier = "GOLD"
    elif total_revenue >= 50_000:
        tier = "SILVER"
    else:
        tier = "BRONZE"
    return {
        "source": str(path),
        "found": True,
        "customer_name": row.get("customer_name", customer_name),
        "total_orders": int(_float_or_zero(row.get("total_orders"))),
        "completed_orders": int(_float_or_zero(row.get("completed_orders"))),
        "total_revenue": total_revenue,
        "avg_order_value": _float_or_zero(row.get("avg_order_value")),
        "pending_quotes": int(_float_or_zero(row.get("pending_quotes"))),
        "first_order": row.get("first_order", ""),
        "last_order": row.get("last_order", ""),
        "tier": tier,
        "tier_rule_source": "advanced/pricing/CONTEXT.md; advanced/pricing/HARDENED_FACTS.md",
    }


def _read_master_part_pricing(part_number: str) -> dict[str, Any]:
    path = Path.home() / "alexandria" / "advanced" / "pricing" / "data" / "v11_master_part_pricing.csv"
    row = _read_csv_match(path, "part_number", part_number)
    if not row:
        return {"source": str(path), "found": False}
    return {
        "source": str(path),
        "found": True,
        "times_sold": int(_float_or_zero(row.get("times_sold"))),
        "times_quoted": int(_float_or_zero(row.get("times_quoted"))),
        "total_qty_sold": _float_or_zero(row.get("total_qty_sold")),
        "total_revenue": _float_or_zero(row.get("total_revenue")),
        "min_sale_price": _float_or_zero(row.get("min_sale_price")),
        "max_sale_price": _float_or_zero(row.get("max_sale_price")),
        "avg_sale_price": _float_or_zero(row.get("avg_sale_price")),
        "avg_quote_price": _float_or_zero(row.get("avg_quote_price")),
        "first_activity": row.get("first_activity", ""),
        "last_activity": row.get("last_activity", ""),
    }


def _read_oem_pricing(part_number: str) -> dict[str, Any]:
    path = Path.home() / "alexandria" / "advanced" / "pricing" / "oem_master_2026.csv"
    row = _read_csv_match(path, "PN", part_number)
    if not row:
        return {"source": str(path), "found": False}
    oem_price = _float_or_zero(row.get("MFG PV"))
    return {
        "source": str(path),
        "found": True,
        "part_number": row.get("PN", part_number),
        "description": row.get("DESCRIPTION", ""),
        "oem_price": oem_price,
        "model": row.get("MODEL?", ""),
        "sv_price": _float_or_zero(row.get("SV PRICE")),
        "price_ratio": _float_or_zero(row.get("PRICE RATIO")),
        "hot_idg_target_range": [round(oem_price * 0.55, 2), round(oem_price * 0.65, 2)]
        if oem_price
        else [],
    }


def _pricing_rule_evidence() -> dict[str, Any]:
    files = [
        Path.home() / "alexandria" / "advanced" / "pricing" / "START_HERE.md",
        Path.home() / "alexandria" / "advanced" / "pricing" / "HARDENED_FACTS.md",
        Path.home() / "alexandria" / "advanced" / "pricing" / "CORRECTION_2026-02-03_turkish_quotes.md",
    ]
    loaded: list[str] = []
    missing: list[str] = []
    required_evidence = {
        "customer_history_first": ["customer history", "last paid"],
        "list_price_not_primary": ["list_price"],
        "last_sale_anchor": ["last sale price", "last paid", "actual payment history"],
        "tier_discounts_not_automatic": ["tier discounts", "platinum discount"],
    }
    combined = ""
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            missing.append(str(path))
            continue
        loaded.append(str(path))
        combined += "\n" + text[:20_000]
    combined_lower = combined.lower()
    missing_evidence = [
        name
        for name, alternatives in required_evidence.items()
        if not any(alternative in combined_lower for alternative in alternatives)
    ]
    return {
        "loaded": loaded,
        "missing": missing,
        "required_phrases_present": not missing_evidence and len(loaded) >= 2,
        "missing_phrases": missing_evidence,
        "rules": [
            "Check this customer's same-part history first.",
            "If no same-part history exists, use general part history/OEM as support, not as an automatic send.",
            "Do not use V11 list_price directly as the quote price.",
            "Do not auto-apply tier discounts to lower an existing customer's historical price.",
            "External/customer-facing sends require explicit approval.",
        ],
    }


def _summarize_sale_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summarized: list[dict[str, Any]] = []
    for line in lines:
        summarized.append(
            {
                "line_id": line.get("id"),
                "order": _m2o_name(line.get("order_id")),
                "order_id": _m2o_id(line.get("order_id")),
                "customer": _m2o_name(line.get("order_partner_id")),
                "product": _m2o_name(line.get("product_id")),
                "quantity": _float_or_zero(line.get("product_uom_qty")),
                "unit_price": _float_or_zero(line.get("price_unit")),
                "create_date": str(line.get("create_date") or ""),
            }
        )
    return summarized


def _suggest_rfq_pricing(
    *,
    same_customer_history: list[dict[str, Any]],
    general_part_history: list[dict[str, Any]],
    master_pricing: dict[str, Any],
    oem_pricing: dict[str, Any],
    requested_quantity: float,
    available_quantity: float,
) -> dict[str, Any]:
    red_flags: list[str] = []
    basis = "manual_review_required"
    candidate = 0.0
    same_history = _summarize_sale_lines(same_customer_history)
    general_history = _summarize_sale_lines(general_part_history)

    if same_history:
        candidate = same_history[0]["unit_price"]
        basis = "customer_last_paid"
    elif general_history:
        candidate = general_history[0]["unit_price"]
        basis = "general_last_sale_support"
        red_flags.append("no_same_customer_part_history")
    else:
        avg_sale = _float_or_zero(master_pricing.get("avg_sale_price"))
        oem_range = oem_pricing.get("hot_idg_target_range") or []
        candidate = avg_sale or (float(oem_range[0]) if oem_range else 0.0)
        basis = "master_history_or_oem_support"
        red_flags.append("no_live_sale_history")

    if available_quantity <= requested_quantity:
        red_flags.append("low_or_exact_stock")
    if not same_history:
        red_flags.append("customer_specific_approval_required")

    avg_sale = _float_or_zero(master_pricing.get("avg_sale_price"))
    max_sale = _float_or_zero(master_pricing.get("max_sale_price"))
    if candidate and avg_sale:
        candidate = max(candidate, avg_sale)
    if candidate and max_sale and "low_or_exact_stock" in red_flags:
        candidate = max(candidate, max_sale)

    return {
        "basis": basis,
        "candidate_unit_price": round(candidate, 2) if candidate else 0.0,
        "currency": "USD",
        "manual_review_required": bool(red_flags),
        "red_flags": red_flags,
        "support": {
            "same_customer_history": same_history[:5],
            "general_part_history": general_history[:8],
            "master_avg_sale_price": _float_or_zero(master_pricing.get("avg_sale_price")),
            "master_max_sale_price": _float_or_zero(master_pricing.get("max_sale_price")),
            "oem_hot_idg_target_range": oem_pricing.get("hot_idg_target_range", []),
        },
    }


def _build_live_rfq_dry_run_package(options: CanaryOptions) -> dict[str, Any]:
    env_info = _load_canary_env_files()
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(max(1.0, min(options.timeout, 30.0)))
    try:
        client = _V11ReadOnlyClient()
        authenticated = client.authenticate()
        if not authenticated:
            raise ConnectionError("V11 authentication failed")

        part_number = "762367B"
        customer_query = "TURKISH TECHNIC"
        requested_quantity = 1.0

        products = client.search_read(
            "product.product",
            [["name", "=", part_number]],
            ["id", "name", "display_name", "qty_available", "product_tmpl_id", "lst_price"],
            limit=5,
        )
        if not products:
            products = client.search_read(
                "product.product",
                [["name", "ilike", part_number]],
                ["id", "name", "display_name", "qty_available", "product_tmpl_id", "lst_price"],
                limit=5,
            )
        product = products[0] if products else {}
        product_id = int(product.get("id") or 0)
        template_id = _m2o_id(product.get("product_tmpl_id")) or product_id
        template = (
            client.read(
                "product.template",
                [template_id],
                ["name", "description_sale", "description", "list_price"],
            )[0]
            if template_id
            else {}
        )

        internal_quants = (
            client.search_read(
                "stock.quant",
                [
                    ["product_id", "=", product_id],
                    ["quantity", ">", 0],
                    ["location_id.usage", "=", "internal"],
                ],
                ["id", "product_id", "lot_id", "quantity", "reserved_quantity", "location_id"],
                limit=20,
            )
            if product_id
            else []
        )
        available_quantity = sum(
            max(0.0, _float_or_zero(row.get("quantity")) - _float_or_zero(row.get("reserved_quantity")))
            for row in internal_quants
        )

        partners = client.search_read(
            "res.partner",
            [["name", "ilike", customer_query], ["customer", "=", True]],
            ["id", "name", "customer"],
            limit=5,
        )
        partner = partners[0] if partners else {}
        partner_id = int(partner.get("id") or 0)
        partner_name = str(partner.get("name") or customer_query)

        same_customer_history = (
            client.search_read(
                "sale.order.line",
                [
                    ["product_id.name", "=", part_number],
                    ["order_id.partner_id", "=", partner_id],
                    ["order_id.state", "in", ["sale", "done"]],
                    ["price_unit", ">", 0],
                ],
                ["id", "price_unit", "product_uom_qty", "order_id", "order_partner_id", "product_id", "create_date"],
                limit=8,
                order="create_date desc",
            )
            if partner_id
            else []
        )
        customer_recent_history = (
            client.search_read(
                "sale.order.line",
                [
                    ["order_id.partner_id", "=", partner_id],
                    ["order_id.state", "in", ["sale", "done"]],
                    ["price_unit", ">", 0],
                ],
                ["id", "price_unit", "product_uom_qty", "order_id", "order_partner_id", "product_id", "create_date"],
                limit=8,
                order="create_date desc",
            )
            if partner_id
            else []
        )
        general_part_history = client.search_read(
            "sale.order.line",
            [
                ["product_id.name", "=", part_number],
                ["order_id.state", "in", ["sale", "done"]],
                ["price_unit", ">", 0],
            ],
            ["id", "price_unit", "product_uom_qty", "order_id", "order_partner_id", "product_id", "create_date"],
            limit=10,
            order="create_date desc",
        )
    finally:
        socket.setdefaulttimeout(old_timeout)

    customer_summary = _read_customer_pricing_summary(partner_name)
    master_pricing = _read_master_part_pricing(part_number)
    oem_pricing = _read_oem_pricing(part_number)
    pricing_rules = _pricing_rule_evidence()
    suggested_pricing = _suggest_rfq_pricing(
        same_customer_history=same_customer_history,
        general_part_history=general_part_history,
        master_pricing=master_pricing,
        oem_pricing=oem_pricing,
        requested_quantity=requested_quantity,
        available_quantity=available_quantity,
    )

    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "scenario": {
            "source": "hermes_canary_live_rfq_dry_run",
            "rfq_id": "CANARY-RFQ-762367B-TURKISH",
            "customer_query": customer_query,
            "customer_name": partner_name,
            "part_number": part_number,
            "quantity": requested_quantity,
            "draft_only": True,
        },
        "v11": {
            "authenticated": True,
            "url": os.getenv("ODOO_URL", "https://v11.advanced.aero"),
            "database": os.getenv("ODOO_DB", "advancedaero"),
            "read_only_methods": ["authenticate", "search_read", "read"],
            "write_methods_called": [],
            "product": {
                "id": product_id,
                "name": product.get("name", ""),
                "display_name": product.get("display_name", ""),
                "qty_available": _float_or_zero(product.get("qty_available")),
                "template_id": template_id,
                "description": template.get("description_sale") or template.get("description") or "",
                "list_price_observed_not_used_as_quote": _float_or_zero(product.get("lst_price")),
            },
            "stock": {
                "available_internal_quantity": available_quantity,
                "quants": [
                    {
                        "quant_id": row.get("id"),
                        "lot": _m2o_name(row.get("lot_id")),
                        "quantity": _float_or_zero(row.get("quantity")),
                        "reserved_quantity": _float_or_zero(row.get("reserved_quantity")),
                        "location": _m2o_name(row.get("location_id")),
                    }
                    for row in internal_quants
                ],
            },
            "customer": {
                "id": partner_id,
                "name": partner_name,
            },
            "history": {
                "same_customer_same_part": _summarize_sale_lines(same_customer_history),
                "customer_recent_sales": _summarize_sale_lines(customer_recent_history),
                "general_same_part_sales": _summarize_sale_lines(general_part_history),
            },
        },
        "pricing": {
            "rules": pricing_rules,
            "customer_summary": customer_summary,
            "master_part_pricing": master_pricing,
            "oem_pricing": oem_pricing,
            "suggestion": suggested_pricing,
            "decision": "draft_package_only",
        },
        "guardrails": {
            "external_send": False,
            "customer_facing_send": False,
            "v11_create_or_write": False,
            "approval_required_before_send": True,
            "manual_review_required": bool(suggested_pricing.get("manual_review_required")),
            "blocked_actions": [
                "send_customer_email",
                "send_customer_telegram",
                "create_v11_sale_order",
                "confirm_v11_sale_order",
                "change_pricing_config",
            ],
        },
        "sources": [
            "live V11 XML-RPC read-only product.product/search_read",
            "live V11 XML-RPC read-only stock.quant/search_read",
            "live V11 XML-RPC read-only sale.order.line/search_read",
            "live V11 XML-RPC read-only res.partner/search_read",
            "alexandria/advanced/pricing/START_HERE.md",
            "alexandria/advanced/pricing/HARDENED_FACTS.md",
            "alexandria/advanced/pricing/data/v11_customer_pricing.csv",
            "alexandria/advanced/pricing/data/v11_master_part_pricing.csv",
            "alexandria/advanced/pricing/oem_master_2026.csv",
        ],
        "env": {
            "loaded_files": env_info["files"],
            "loaded_key_count": len(env_info["loaded_keys"]),
        },
    }


def _rfq_package_slug(package: dict[str, Any]) -> str:
    scenario = package.get("scenario", {})
    customer = str(scenario.get("customer_name") or scenario.get("customer_query") or "customer")
    part_number = str(scenario.get("part_number") or "part")
    raw = f"{customer}-{part_number}".lower()
    return "".join(ch if ch.isalnum() else "-" for ch in raw).strip("-")[:80] or "rfq-dry-run"


def _render_rfq_package_markdown(package: dict[str, Any]) -> str:
    scenario = package.get("scenario", {})
    v11 = package.get("v11", {})
    pricing = package.get("pricing", {})
    guardrails = package.get("guardrails", {})
    product = v11.get("product", {})
    stock = v11.get("stock", {})
    suggestion = (pricing.get("suggestion") or {})
    sources = package.get("sources", [])
    lines = [
        "# Hermes RFQ Dry-Run Quote Package",
        "",
        f"- Customer: {scenario.get('customer_name', '')}",
        f"- Part: {scenario.get('part_number', '')}",
        f"- Quantity: {scenario.get('quantity', '')}",
        f"- Description: {product.get('description', '')}",
        f"- Internal available quantity: {stock.get('available_internal_quantity', 0)}",
        f"- Candidate unit price: ${_float_or_zero(suggestion.get('candidate_unit_price')):,.2f}",
        f"- Pricing basis: {suggestion.get('basis', '')}",
        f"- Manual review required: {bool(suggestion.get('manual_review_required'))}",
        "",
        "## Guardrails",
        "",
        f"- Draft only: {bool(scenario.get('draft_only'))}",
        f"- External send: {bool(guardrails.get('external_send'))}",
        f"- V11 create/write: {bool(guardrails.get('v11_create_or_write'))}",
        f"- Approval required before send: {bool(guardrails.get('approval_required_before_send'))}",
        "",
        "## Red Flags",
        "",
    ]
    red_flags = suggestion.get("red_flags") or []
    if red_flags:
        lines.extend(f"- {flag}" for flag in red_flags)
    else:
        lines.append("- none")
    lines.extend(["", "## Sources", ""])
    lines.extend(f"- {source}" for source in sources)
    lines.append("")
    return "\n".join(lines)


def _write_rfq_package_artifacts(options: CanaryOptions, package: dict[str, Any]) -> dict[str, str]:
    output_dir = options.hermes_home / "canary" / "rfq_dry_runs"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    slug = _rfq_package_slug(package)
    json_path = output_dir / f"{timestamp}-{slug}.json"
    markdown_path = output_dir / f"{timestamp}-{slug}.md"
    json_path.write_text(json.dumps(package, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_render_rfq_package_markdown(package), encoding="utf-8")
    latest_json = output_dir / "latest.json"
    latest_markdown = output_dir / "latest.md"
    latest_json.write_text(json.dumps(package, indent=2, sort_keys=True), encoding="utf-8")
    latest_markdown.write_text(_render_rfq_package_markdown(package), encoding="utf-8")
    return {
        "json_path": str(json_path),
        "markdown_path": str(markdown_path),
        "latest_json": str(latest_json),
        "latest_markdown": str(latest_markdown),
    }


def _attach_rfq_package_to_workspace(options: CanaryOptions, package: dict[str, Any], artifacts: dict[str, str]) -> dict[str, str]:
    from hermes_cli.workspace import WorkspaceStore

    scenario = package.get("scenario", {})
    store = WorkspaceStore(options.hermes_home / "workspace" / "control_plane.json")
    task = store.create_task(
        f"RFQ dry-run canary: {scenario.get('customer_name', '')} {scenario.get('part_number', '')}",
        owner="hermes",
        priority="normal",
        project="hermes-canary",
        source="canary",
        status="in_progress",
        note="Draft-only RFQ package generated from V11 stock, sales history, customer summary, and pricing rules.",
    )
    store.add_evidence(
        task_id=task["id"],
        kind="rfq_dry_run",
        title="Sourced draft-only quote package",
        locator=artifacts["json_path"],
        summary=f"Package JSON: {artifacts['json_path']}; Markdown: {artifacts['markdown_path']}",
        metadata={"markdown_path": artifacts["markdown_path"]},
    )
    store.update_task(
        task["id"],
        status="done",
        next_action="Manual operator review required before any V11 draft creation or customer-facing send.",
    )
    return {"task_id": task["id"], "store_path": str(store.path)}


def _canary_rfq_dry_run_quote_package(options: CanaryOptions) -> CanaryResult:
    if not options.rfq_dry_run:
        return _result(
            "live.rfq_dry_run_quote_package",
            SKIP,
            0,
            25,
            "RFQ dry-run canary not enabled; pass --rfq-dry-run for live V11 quote-package proof",
            {"enabled": False},
        )

    try:
        package = _build_live_rfq_dry_run_package(options)
    except Exception as exc:
        return _result(
            "live.rfq_dry_run_quote_package",
            FAIL if options.require_live else WARN,
            0,
            25,
            f"RFQ dry-run package failed: {type(exc).__name__}: {exc}",
            {
                "enabled": True,
                "failure_class": "v11_live_read_failure",
                "error_type": type(exc).__name__,
            },
        )

    artifacts = _write_rfq_package_artifacts(options, package)
    workspace = _attach_rfq_package_to_workspace(options, package, artifacts)
    package["workspace"] = workspace
    artifacts = _write_rfq_package_artifacts(options, package)

    v11 = package.get("v11", {})
    pricing = package.get("pricing", {})
    guardrails = package.get("guardrails", {})
    checks = {
        "v11_authenticated": bool(v11.get("authenticated")),
        "product_found": bool((v11.get("product") or {}).get("id")),
        "stock_checked": "available_internal_quantity" in (v11.get("stock") or {}),
        "customer_found": bool((v11.get("customer") or {}).get("id")),
        "history_checked": bool(
            ((v11.get("history") or {}).get("same_customer_same_part") or [])
            or ((v11.get("history") or {}).get("general_same_part_sales") or [])
        ),
        "pricing_rules_loaded": bool((pricing.get("rules") or {}).get("required_phrases_present")),
        "customer_summary_loaded": bool((pricing.get("customer_summary") or {}).get("found")),
        "master_pricing_loaded": bool((pricing.get("master_part_pricing") or {}).get("found")),
        "oem_pricing_loaded": bool((pricing.get("oem_pricing") or {}).get("found")),
        "draft_only_guard": bool((package.get("scenario") or {}).get("draft_only")),
        "external_send_blocked": guardrails.get("external_send") is False
        and guardrails.get("customer_facing_send") is False,
        "v11_write_blocked": guardrails.get("v11_create_or_write") is False
        and not (v11.get("write_methods_called") or []),
        "workspace_task_created": bool(workspace.get("task_id")),
        "artifacts_written": Path(artifacts["json_path"]).is_file() and Path(artifacts["markdown_path"]).is_file(),
    }
    failed = {name: value for name, value in checks.items() if not value}
    score = round(25.0 * (len(checks) - len(failed)) / len(checks), 1)
    status = PASS if not failed else WARN
    return _result(
        "live.rfq_dry_run_quote_package",
        status,
        score,
        25,
        f"{len(checks) - len(failed)}/{len(checks)} live RFQ dry-run quote-package checks passed",
        {
            "enabled": True,
            "checks": checks,
            "failed": failed,
            "artifact": artifacts,
            "workspace": workspace,
            "scenario": package.get("scenario", {}),
            "pricing_basis": (pricing.get("suggestion") or {}).get("basis", ""),
            "manual_review_required": bool((pricing.get("suggestion") or {}).get("manual_review_required")),
            "failure_class": "missing_evidence" if failed else "",
        },
    )


def _parse_condition_from_lot(lot_name: str) -> str:
    raw = str(lot_name or "")
    if " - " in raw:
        candidate = raw.rsplit(" - ", 1)[-1].strip().upper()
        if candidate:
            return candidate
    return "SV"


def _condition_label(condition: str) -> str:
    return {
        "NE": "New",
        "NS": "New Surplus",
        "FN": "Factory New",
        "SV": "Serviceable",
        "OH": "Overhauled",
        "AR": "As Removed",
        "RP": "Repairable",
        "US": "Unserviceable",
        "IN": "Inspected",
    }.get(str(condition or "").upper(), str(condition or ""))


def _build_approved_rfq_draft_package(options: CanaryOptions) -> dict[str, Any]:
    rfq_package = _build_live_rfq_dry_run_package(options)
    scenario = rfq_package.get("scenario") or {}
    v11 = rfq_package.get("v11") or {}
    pricing = rfq_package.get("pricing") or {}
    product = v11.get("product") or {}
    customer = v11.get("customer") or {}
    stock = v11.get("stock") or {}
    quants = stock.get("quants") if isinstance(stock.get("quants"), list) else []
    first_quant = quants[0] if quants and isinstance(quants[0], dict) else {}
    suggestion = pricing.get("suggestion") or {}
    quantity = _float_or_zero(scenario.get("quantity")) or 1.0
    unit_price = _float_or_zero(suggestion.get("candidate_unit_price"))
    subtotal = round(quantity * unit_price, 2)
    part_number = str(scenario.get("part_number") or product.get("name") or "")
    condition = _parse_condition_from_lot(str(first_quant.get("lot") or ""))
    rfq_id = str(scenario.get("rfq_id") or "HERMES-CANARY-RFQ")
    draft_ref = f"HERMES-CANARY-DRAFT-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    line_description = str(product.get("description") or product.get("display_name") or part_number)
    partner_id = int(customer.get("id") or 0)
    product_id = int(product.get("id") or 0)

    draft_line = {
        "product_id": product_id,
        "part_number": part_number,
        "description": line_description,
        "quantity": quantity,
        "unit_price": unit_price,
        "condition": condition,
        "lot": first_quant.get("lot", ""),
        "price_basis": suggestion.get("basis", ""),
        "manual_review_required": bool(suggestion.get("manual_review_required")),
    }
    v11_payload = {
        "target": "v11.sale.order",
        "method": "create",
        "write_enabled": False,
        "values": {
            "partner_id": partner_id,
            "client_order_ref": rfq_id,
            "note": f"Internal Hermes approved-RFQ draft package: {draft_ref}",
            "order_line": [
                [
                    0,
                    0,
                    {
                        "product_id": product_id,
                        "product_uom_qty": quantity,
                        "price_unit": unit_price,
                        "x_studio_field_cOZEb": condition,
                    },
                ]
            ],
        },
    }
    atlas_payload = {
        "target": "atlas.admin.quotes",
        "method": "POST",
        "endpoint": "/api/admin/quotes",
        "write_enabled": False,
        "body": {
            "origin": rfq_id,
            "customerName": customer.get("name") or scenario.get("customer_name", ""),
            "lines": [
                {
                    "partNumber": part_number,
                    "description": line_description,
                    "quantity": quantity,
                    "unitPrice": unit_price,
                    "condition": condition,
                    "advancedIdOrLot": first_quant.get("lot", ""),
                }
            ],
        },
    }
    card_text = (
        f"Approved RFQ draft ready\n"
        f"{draft_ref}\n"
        f"Customer: {customer.get('name') or scenario.get('customer_name', '')}\n"
        f"Part: {part_number} {condition}\n"
        f"Qty: {quantity:g} | Unit: ${unit_price:,.2f} | Total: ${subtotal:,.2f}\n"
        "Customer email is blocked. Review QAMFORM preview before any send."
    )
    telegram_card = {
        "send_enabled": False,
        "chat_id": _telegram_e2e_channel_id(options),
        "parse_mode": "HTML",
        "text_preview": card_text,
        "reply_markup": {
            "inline_keyboard": [
                [
                    {"text": "Approve Draft", "callback_data": f"canary_draft_approve:{draft_ref}"},
                    {"text": "Reject", "callback_data": f"canary_draft_reject:{draft_ref}"},
                ]
            ]
        },
    }
    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "mode": "approved_rfq_draft_guarded",
        "draft_reference": draft_ref,
        "source_rfq_package": rfq_package,
        "operator_approval": {
            "status": "approved_for_internal_draft_package",
            "mode": "canary_simulated_operator_approval",
            "approved_actions": [
                "prepare_v11_draft_payload",
                "prepare_atlas_draft_payload",
                "render_qamform_preview",
                "prepare_telegram_approval_card",
            ],
            "blocked_actions": [
                "write_v11_sale_order",
                "write_atlas_quote",
                "send_customer_email",
                "send_customer_message",
                "confirm_sale_order",
            ],
        },
        "draft_line": draft_line,
        "v11_draft": v11_payload,
        "atlas_draft": atlas_payload,
        "telegram_approval_card": telegram_card,
        "qamform_preview": {},
        "guardrails": {
            "external_send": False,
            "customer_facing_send": False,
            "v11_create_or_write": False,
            "atlas_create_or_write": False,
            "telegram_send": False,
            "approval_required_before_customer_send": True,
            "write_methods_called": [],
        },
    }


def _render_approved_rfq_qamform_preview(
    package: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    service_dir = Path.home() / ".hermes" / "services"
    quote_pdf_path = service_dir / "quote_pdf.py"
    if not quote_pdf_path.is_file():
        raise FileNotFoundError(str(quote_pdf_path))
    previous_dyld_fallback = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
    homebrew_lib = "/opt/homebrew/lib"
    if Path(homebrew_lib).is_dir():
        paths = [path for path in previous_dyld_fallback.split(":") if path]
        if homebrew_lib not in paths:
            os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = ":".join([homebrew_lib, *paths])
    try:
        spec = importlib.util.spec_from_file_location("hermes_canary_quote_pdf", quote_pdf_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load quote_pdf module from {quote_pdf_path}")
        quote_pdf = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(quote_pdf)

        source = package.get("source_rfq_package") or {}
        scenario = source.get("scenario") or {}
        line = package.get("draft_line") or {}
        customer_name = str(scenario.get("customer_name") or "Hermes Canary Customer")
        quantity = _float_or_zero(line.get("quantity")) or 1.0
        unit_price = _float_or_zero(line.get("unit_price"))
        subtotal = round(quantity * unit_price, 2)
        condition = str(line.get("condition") or "")
        now = datetime.now(timezone.utc).replace(microsecond=0)
        so_data = quote_pdf._Obj(
            name=package.get("draft_reference", "HERMES-CANARY-DRAFT"),
            state="draft",
            date_order=now.strftime("%Y-%m-%d %H:%M:%S"),
            validity_date="",
            client_order_ref=scenario.get("rfq_id", ""),
            amount_total=subtotal,
            payment_term="Net 30",
            partner=quote_pdf._Obj(
                name=customer_name,
                street="",
                street2="",
                city="",
                state="",
                zip="",
                country="",
                phone="",
                email="",
                mobile="",
            ),
            ship_to=quote_pdf._Obj(
                name=customer_name,
                street="",
                street2="",
                city="",
                state="",
                zip="",
                country="",
                phone="",
                email="",
                mobile="",
            ),
            lines=[
                quote_pdf._Obj(
                    part_number=line.get("part_number", ""),
                    description=line.get("description", ""),
                    condition=_condition_label(condition),
                    condition_raw=condition,
                    needs_fresh_tag=condition == "SV",
                    needs_overhaul=condition in {"OH", "RP"},
                    needs_work=condition in {"SV", "OH", "RP"},
                    tag_info="",
                    tag_number="",
                    trace_to="",
                    qty=quantity,
                    unit_price=unit_price,
                    subtotal=subtotal,
                )
            ],
        )
        pdf_bytes = quote_pdf.render_quote_pdf(
            so_data,
            access_url=f"https://advanced.aero/d/{package.get('draft_reference', 'hermes-canary')}",
        )
    finally:
        if previous_dyld_fallback:
            os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = previous_dyld_fallback
        else:
            os.environ.pop("DYLD_FALLBACK_LIBRARY_PATH", None)
    if not pdf_bytes:
        raise RuntimeError("QAMFORM renderer returned empty PDF")
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_ref = "".join(
        ch if ch.isalnum() or ch in {"-", "_"} else "-"
        for ch in str(package.get("draft_reference") or "hermes-canary")
    )
    pdf_path = output_dir / f"{safe_ref}-QAMFORM11-preview.pdf"
    pdf_path.write_bytes(pdf_bytes)
    return {
        "status": "rendered",
        "qamform_id": "QAMFORM11",
        "state": "draft",
        "pdf_path": str(pdf_path),
        "pdf_bytes": len(pdf_bytes),
    }


def _render_approved_rfq_draft_markdown(package: dict[str, Any]) -> str:
    source = package.get("source_rfq_package") or {}
    scenario = source.get("scenario") or {}
    line = package.get("draft_line") or {}
    preview = package.get("qamform_preview") or {}
    guardrails = package.get("guardrails") or {}
    return "\n".join(
        [
            "# Hermes Approved RFQ Draft Package",
            "",
            f"- Draft reference: {package.get('draft_reference', '')}",
            f"- Customer: {scenario.get('customer_name', '')}",
            f"- Part: {line.get('part_number', '')}",
            f"- Quantity: {_float_or_zero(line.get('quantity')):g}",
            f"- Unit price: ${_float_or_zero(line.get('unit_price')):,.2f}",
            f"- Condition: {line.get('condition', '')}",
            f"- QAMFORM preview: {preview.get('pdf_path', '')}",
            "",
            "## Guardrails",
            "",
            f"- V11 write enabled: {bool((package.get('v11_draft') or {}).get('write_enabled'))}",
            f"- Atlas write enabled: {bool((package.get('atlas_draft') or {}).get('write_enabled'))}",
            f"- Telegram send enabled: {bool((package.get('telegram_approval_card') or {}).get('send_enabled'))}",
            f"- Customer-facing send: {bool(guardrails.get('customer_facing_send'))}",
            f"- Approval required before customer send: {bool(guardrails.get('approval_required_before_customer_send'))}",
            "",
        ]
    )


def _write_approved_rfq_draft_artifacts(options: CanaryOptions, package: dict[str, Any]) -> dict[str, str]:
    output_dir = options.hermes_home / "canary" / "approved_rfq_drafts"
    output_dir.mkdir(parents=True, exist_ok=True)
    preview = _render_approved_rfq_qamform_preview(package, output_dir)
    package["qamform_preview"] = preview
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    draft_ref = str(package.get("draft_reference") or "hermes-canary-draft").lower()
    slug = "".join(ch if ch.isalnum() else "-" for ch in draft_ref).strip("-")[:80]
    json_path = output_dir / f"{timestamp}-{slug}.json"
    markdown_path = output_dir / f"{timestamp}-{slug}.md"
    json_path.write_text(json.dumps(package, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_render_approved_rfq_draft_markdown(package), encoding="utf-8")
    latest_json = output_dir / "latest.json"
    latest_markdown = output_dir / "latest.md"
    latest_json.write_text(json.dumps(package, indent=2, sort_keys=True), encoding="utf-8")
    latest_markdown.write_text(_render_approved_rfq_draft_markdown(package), encoding="utf-8")
    return {
        "json_path": str(json_path),
        "markdown_path": str(markdown_path),
        "pdf_path": str(preview.get("pdf_path", "")),
        "latest_json": str(latest_json),
        "latest_markdown": str(latest_markdown),
    }


def _attach_approved_rfq_draft_to_workspace(
    options: CanaryOptions,
    package: dict[str, Any],
    artifacts: dict[str, str],
) -> dict[str, str]:
    from hermes_cli.workspace import WorkspaceStore

    scenario = (package.get("source_rfq_package") or {}).get("scenario") or {}
    store = WorkspaceStore(options.hermes_home / "workspace" / "control_plane.json")
    task = store.create_task(
        f"Approved RFQ draft canary: {scenario.get('customer_name', '')} {scenario.get('part_number', '')}",
        owner="hermes",
        priority="normal",
        project="hermes-canary",
        source="canary",
        status="in_progress",
        note="Approved-RFQ draft payload, QAMFORM preview, and Telegram approval card generated with writes disabled.",
    )
    store.add_evidence(
        task_id=task["id"],
        kind="approved_rfq_draft",
        title="Write-guarded draft quote approval package",
        locator=artifacts["json_path"],
        summary=f"Draft JSON: {artifacts['json_path']}; QAMFORM preview: {artifacts['pdf_path']}",
        metadata={"markdown_path": artifacts["markdown_path"], "pdf_path": artifacts["pdf_path"]},
    )
    store.update_task(
        task["id"],
        status="done",
        next_action="Enable live draft write only after a real RFQ/operator approval; customer send remains blocked.",
    )
    return {"task_id": task["id"], "store_path": str(store.path)}


def _canary_approved_rfq_draft_quote(options: CanaryOptions) -> CanaryResult:
    if not options.approved_rfq_draft:
        return _result(
            "live.approved_rfq_draft_quote",
            SKIP,
            0,
            30,
            "Approved-RFQ draft canary not enabled; pass --approved-rfq-draft for QAMFORM/approval-card proof",
            {"enabled": False},
        )

    try:
        package = _build_approved_rfq_draft_package(options)
        artifacts = _write_approved_rfq_draft_artifacts(options, package)
        workspace = _attach_approved_rfq_draft_to_workspace(options, package, artifacts)
        package["workspace"] = workspace
        artifacts = _write_approved_rfq_draft_artifacts(options, package)
    except Exception as exc:
        return _result(
            "live.approved_rfq_draft_quote",
            FAIL if options.require_live else WARN,
            0,
            30,
            f"Approved-RFQ draft package failed: {type(exc).__name__}: {exc}",
            {
                "enabled": True,
                "failure_class": "approved_rfq_draft_failure",
                "error_type": type(exc).__name__,
            },
        )

    service_dir = Path.home() / ".hermes" / "services"
    approval_bot_path = service_dir / "quote_approval_bot.py"
    try:
        approval_bot_text = approval_bot_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        approval_bot_text = ""
    source = package.get("source_rfq_package") or {}
    guardrails = package.get("guardrails") or {}
    preview = package.get("qamform_preview") or {}
    checks = {
        "source_rfq_package_loaded": bool((source.get("v11") or {}).get("authenticated")),
        "operator_approval_recorded": (package.get("operator_approval") or {}).get("status")
        == "approved_for_internal_draft_package",
        "v11_draft_payload_ready": bool((package.get("v11_draft") or {}).get("values")),
        "atlas_draft_payload_ready": bool((package.get("atlas_draft") or {}).get("body")),
        "v11_write_guarded": (package.get("v11_draft") or {}).get("write_enabled") is False
        and guardrails.get("v11_create_or_write") is False,
        "atlas_write_guarded": (package.get("atlas_draft") or {}).get("write_enabled") is False
        and guardrails.get("atlas_create_or_write") is False,
        "customer_send_blocked": guardrails.get("customer_facing_send") is False
        and guardrails.get("external_send") is False,
        "telegram_card_ready": bool((package.get("telegram_approval_card") or {}).get("reply_markup")),
        "telegram_send_guarded": (package.get("telegram_approval_card") or {}).get("send_enabled") is False,
        "qamform_preview_written": Path(str(preview.get("pdf_path") or "")).is_file()
        and int(preview.get("pdf_bytes") or 0) > 1000,
        "artifacts_written": Path(artifacts["json_path"]).is_file()
        and Path(artifacts["markdown_path"]).is_file()
        and Path(artifacts["pdf_path"]).is_file(),
        "workspace_task_created": bool((package.get("workspace") or {}).get("task_id")),
        "approval_bot_does_not_send_customer_email": "from gmail_draft import send_email" not in approval_bot_text
        and "send_email(" not in approval_bot_text
        and "Customer email was not sent" in approval_bot_text,
    }
    failed = {name: value for name, value in checks.items() if not value}
    score = round(30.0 * (len(checks) - len(failed)) / len(checks), 1)
    status = PASS if not failed else WARN
    return _result(
        "live.approved_rfq_draft_quote",
        status,
        score,
        30,
        f"{len(checks) - len(failed)}/{len(checks)} approved-RFQ draft checks passed",
        {
            "enabled": True,
            "checks": checks,
            "failed": failed,
            "artifact": artifacts,
            "workspace": package.get("workspace", {}),
            "draft_reference": package.get("draft_reference", ""),
            "write_mode": "guarded_payload_only",
            "failure_class": "missing_evidence" if failed else "",
        },
    )


def _canary_planner_self_heal(options: CanaryOptions) -> CanaryResult:
    run_path = options.repo_root / "gateway" / "run.py"
    config_path = options.hermes_home / "config.yaml"
    playbook_path = options.repo_root / "docs" / "HERMES_SELF_HEAL_PLAYBOOKS.md"
    wrapper_path = options.hermes_home / "bin" / "hermes-telegram-webhook-gateway"

    failed: dict[str, Any] = {}
    passed: list[str] = []

    try:
        run_text = run_path.read_text(encoding="utf-8")
    except OSError as exc:
        run_text = ""
        failed["gateway_source"] = str(exc)

    gateway_needles = [
        "_build_hard_task_planner_prompt",
        "Hard task planner route",
        "Injected hard-task planner route",
        "docs/HERMES_SELF_HEAL_PLAYBOOKS.md",
    ]
    missing_gateway = [needle for needle in gateway_needles if needle not in run_text]
    if missing_gateway:
        failed["hard_task_route_source"] = {"missing": missing_gateway}
    else:
        passed.append("hard_task_route_source")

    self_heal_needles = [
        "_STUCK_LOOP_THRESHOLD",
        "mark_resume_pending",
        "suspend_recently_active",
        "request_restart",
        "restart_failure_counts",
    ]
    missing_self_heal = [needle for needle in self_heal_needles if needle not in run_text]
    if missing_self_heal:
        failed["gateway_self_heal_source"] = {"missing": missing_self_heal}
    else:
        passed.append("gateway_self_heal_source")

    try:
        from gateway.run import _build_hard_task_planner_prompt

        hard_prompt = _build_hard_task_planner_prompt(
            "Diagnose the production RFQ quote workflow root cause, then deploy the fix and verify with V11 and canary."
        )
        exact_prompt = _build_hard_task_planner_prompt("Reply exactly HERMES_EVAL_OK")
        if "Hard task planner route" not in hard_prompt or exact_prompt:
            failed["hard_task_route_behavior"] = {
                "hard_prompt_present": bool(hard_prompt),
                "exact_prompt_present": bool(exact_prompt),
            }
        else:
            passed.append("hard_task_route_behavior")
    except Exception as exc:
        failed["hard_task_route_behavior"] = f"{type(exc).__name__}: {exc}"

    snapshot = _env_model_snapshot(options)
    provider = snapshot.get("HERMES_PLANNER_PROVIDER") or snapshot.get("HERMES_INFERENCE_PROVIDER") or ""
    v4_available = snapshot.get("HERMES_V4_PLANNER_AVAILABLE") == "1"
    frontier_available = snapshot.get("HERMES_OPENAI_FRONTIER_AVAILABLE") == "1"
    if provider and (v4_available or frontier_available or "openai-frontier" in provider):
        passed.append("planner_runtime_route")
    else:
        failed["planner_runtime_route"] = {
            "provider": provider,
            "HERMES_V4_PLANNER_AVAILABLE": snapshot.get("HERMES_V4_PLANNER_AVAILABLE", ""),
            "HERMES_OPENAI_FRONTIER_AVAILABLE": snapshot.get("HERMES_OPENAI_FRONTIER_AVAILABLE", ""),
        }

    try:
        config_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        config_text = ""
        failed["config"] = str(exc)
    config_needles = [
        "fallback_providers:",
        "custom:office-deepseek-v4",
        "model_aliases:",
        "frontier:",
        "custom:openai-frontier",
    ]
    missing_config = [needle for needle in config_needles if needle not in config_text]
    if missing_config:
        failed["planner_config"] = {"missing": missing_config, "path": str(config_path)}
    else:
        passed.append("planner_config")

    try:
        playbook_text = playbook_path.read_text(encoding="utf-8")
    except OSError as exc:
        playbook_text = ""
        failed["playbook"] = str(exc)
    playbook_needles = [
        "## Planner Route",
        "## Canary Regression",
        "## Gateway Split-Brain",
        "## Stuck Session",
        "## Telegram/API Health",
    ]
    missing_playbook = [needle for needle in playbook_needles if needle not in playbook_text]
    if missing_playbook:
        failed["playbook"] = {"missing": missing_playbook, "path": str(playbook_path)}
    else:
        passed.append("self_heal_playbook")

    if wrapper_path.is_file():
        try:
            wrapper_text = wrapper_path.read_text(encoding="utf-8")
        except OSError as exc:
            wrapper_text = ""
            failed["launchd_wrapper"] = str(exc)
        if "gateway run --replace" in wrapper_text:
            passed.append("launchd_replace_wrapper")
        else:
            failed["launchd_replace_wrapper"] = {"path": str(wrapper_path)}
    else:
        failed["launchd_replace_wrapper"] = {"missing": str(wrapper_path)}

    total_checks = 7
    score = round(20.0 * len(passed) / total_checks, 1)
    status = PASS if not failed else WARN if passed else FAIL
    return _result(
        "contract.planner_self_heal",
        status,
        score,
        20,
        f"{len(passed)}/{total_checks} hard-planner and self-heal checks passed",
        {"passed": passed, "failed": failed},
    )


def run_canary_suite(options: CanaryOptions) -> CanaryReport:
    started = time.time()
    cases: list[tuple[str, float, Callable[[], CanaryResult]]] = [
        ("runtime.imports", 10, _canary_imports),
        ("runtime.model_route", 15, lambda: _canary_model_route(options)),
        ("live.gateway_health", 15, lambda: _canary_gateway_health(options)),
        ("contract.command_registry", 10, _canary_command_registry),
        ("contract.x_scrape", 10, _canary_x_scrape_contract),
        ("contract.workspace_store", 10, _canary_workspace_store),
        ("contract.goal_workspace", 15, _canary_goal_workspace_contract),
        ("contract.operator_safety", 10, lambda: _canary_safety_contract(options)),
        ("contract.behavior_goldens", 20, lambda: _canary_behavior_goldens(options)),
        ("live.behavior_golden", 20, lambda: _canary_live_behavior(options)),
        ("eval.local_model_reasoning", 30, lambda: _canary_local_model_reasoning_eval(options)),
        ("eval.hermes_reasoning", 30, lambda: _canary_hermes_reasoning_eval(options)),
        ("eval.frontier_wrapper", 20, lambda: _canary_frontier_wrapper(options)),
        ("live.telegram_e2e", 20, lambda: _canary_telegram_e2e(options)),
        ("live.telegram_visible_delivery", 20, lambda: _canary_telegram_visible_delivery(options)),
        ("live.x_scrape", 10, lambda: _canary_live_x_scrape(options)),
        ("contract.scorecard_trend", 10, lambda: _canary_scorecard_trend(options)),
        ("contract.aac_workflows", 15, lambda: _canary_aac_workflow_goldens(options)),
        ("contract.quote_ops_runtime", 20, lambda: _canary_quote_ops_runtime(options)),
        ("live.rfq_dry_run_quote_package", 25, lambda: _canary_rfq_dry_run_quote_package(options)),
        ("live.approved_rfq_draft_quote", 30, lambda: _canary_approved_rfq_draft_quote(options)),
        ("contract.planner_self_heal", 20, lambda: _canary_planner_self_heal(options)),
    ]
    results = [_time_case(name, max_score, fn) for name, max_score, fn in cases]
    try:
        from hermes_cli.runtime_status import collect_runtime_status

        runtime = collect_runtime_status(
            env_wrapper=options.env_wrapper,
            health_url=options.gateway_url,
            repo_root=options.repo_root,
            hermes_home=options.hermes_home,
            timeout=min(max(options.timeout, 1.0), 5.0),
        )
    except Exception as exc:
        runtime = {"error": f"{type(exc).__name__}: {exc}"}
    return CanaryReport(
        started_at=started,
        finished_at=time.time(),
        results=results,
        fail_under=options.fail_under,
        runtime=runtime,
    )


def _result_by_name(report: CanaryReport, name: str) -> CanaryResult | None:
    for result in report.results:
        if result.name == name:
            return result
    return None


def _quality_dimensions(report: CanaryReport) -> list[QualityDimension]:
    model_result = _result_by_name(report, "runtime.model_route")
    model_summary = model_result.summary if model_result else "unknown model route"
    model_score = 5.5
    if model_result and "openai-frontier" in model_result.summary:
        model_score = 8.2
    elif model_result and "kimi-coding" in model_result.summary:
        model_score = 6.4
    elif model_result and "office-deepseek-v4" in model_result.summary:
        model_score = 5.8
    planner_self_heal_result = _result_by_name(report, "contract.planner_self_heal")
    if planner_self_heal_result and planner_self_heal_result.status == PASS:
        model_score = max(model_score, 8.0)
    local_model_reasoning_result = _result_by_name(report, "eval.local_model_reasoning")
    hermes_reasoning_result = _result_by_name(report, "eval.hermes_reasoning")
    frontier_wrapper_result = _result_by_name(report, "eval.frontier_wrapper")
    if local_model_reasoning_result and local_model_reasoning_result.status == PASS:
        model_score = max(model_score, 7.0)
    elif local_model_reasoning_result and local_model_reasoning_result.status in {WARN, FAIL}:
        model_score = min(model_score, 5.0)
    if hermes_reasoning_result and hermes_reasoning_result.status == PASS:
        model_score = max(model_score, 8.2)
    elif hermes_reasoning_result and hermes_reasoning_result.status in {WARN, FAIL}:
        model_score = min(model_score, 5.5)
    if frontier_wrapper_result and frontier_wrapper_result.status == PASS:
        model_score = max(model_score, 8.8)

    health_result = _result_by_name(report, "live.gateway_health")
    runtime_score = 8.0 if health_result and health_result.status == PASS else 7.0
    if _result_by_name(report, "runtime.imports") and _result_by_name(report, "runtime.imports").status == PASS:
        runtime_score += 0.4
    runtime_score = min(runtime_score, 10.0)

    behavior_result = _result_by_name(report, "contract.behavior_goldens")
    live_behavior_result = _result_by_name(report, "live.behavior_golden")
    harness_score = 7.2
    if behavior_result and behavior_result.status == PASS:
        harness_score += 0.6
    if live_behavior_result and live_behavior_result.status == PASS:
        harness_score += 0.5
    harness_score = min(harness_score, 10.0)

    goal_result = _result_by_name(report, "contract.goal_workspace")
    workspace_result = _result_by_name(report, "contract.workspace_store")
    autonomy_score = 7.0
    if goal_result and goal_result.status == PASS and workspace_result and workspace_result.status == PASS:
        autonomy_score = 7.4

    memory_score = 6.8
    if behavior_result and "sonnet_continuity" in behavior_result.details.get("passed", []):
        memory_score = 7.2

    x_result = _result_by_name(report, "live.x_scrape")
    ux_score = 6.7
    if x_result and x_result.status == PASS:
        ux_score += 0.4
    if _result_by_name(report, "contract.operator_safety") and _result_by_name(report, "contract.operator_safety").status == PASS:
        ux_score += 0.3
    telegram_result = _result_by_name(report, "live.telegram_e2e")
    if telegram_result and telegram_result.status == PASS:
        ux_score = max(ux_score, 8.2)
    elif telegram_result and telegram_result.status == WARN:
        ux_score = min(ux_score, 4.0)
    elif telegram_result and telegram_result.status == FAIL:
        ux_score = min(ux_score, 2.0)
    ux_score = min(ux_score, 10.0)

    aac_workflows_result = _result_by_name(report, "contract.aac_workflows")
    aac_workflows_score = 5.4
    aac_workflows_summary = "Business workflows exist, but are not yet canary-scored end to end."
    if aac_workflows_result and aac_workflows_result.status == PASS:
        aac_workflows_score = 8.5
        aac_workflows_summary = "RFQ, V11 lookup, quote prep, and Workspace report goldens are canary-scored."
    elif aac_workflows_result and aac_workflows_result.status == WARN:
        aac_workflows_score = 6.5
        aac_workflows_summary = "AAC workflow canaries exist but one or more checks need hardening."

    quote_ops_result = _result_by_name(report, "contract.quote_ops_runtime")
    rfq_dry_run_result = _result_by_name(report, "live.rfq_dry_run_quote_package")
    approved_rfq_draft_result = _result_by_name(report, "live.approved_rfq_draft_quote")
    business_ops_score = 5.0
    business_ops_summary = "Quote automation runtime is not yet verified."
    if (
        quote_ops_result
        and quote_ops_result.status == PASS
        and rfq_dry_run_result
        and rfq_dry_run_result.status == PASS
        and approved_rfq_draft_result
        and approved_rfq_draft_result.status == PASS
    ):
        business_ops_score = 9.7
        business_ops_summary = "Approved-RFQ draft payload, QAMFORM preview, and Telegram approval-card package pass with customer sends blocked."
    elif (
        quote_ops_result
        and quote_ops_result.status == PASS
        and rfq_dry_run_result
        and rfq_dry_run_result.status == PASS
    ):
        business_ops_score = 9.5
        business_ops_summary = "Quote runtime plus live draft-only RFQ package generation pass from V11, customer history, and pricing rules."
    elif quote_ops_result and quote_ops_result.status == PASS:
        business_ops_score = 8.8
        business_ops_summary = "ILS RFQ, V11 draft quote, Telegram approval, and QAMFORM PDF runtime checks pass."
    elif quote_ops_result and quote_ops_result.status == WARN:
        business_ops_score = 6.8
        business_ops_summary = "Quote automation runtime exists but has open hardening checks."

    return [
        QualityDimension(
            "runtime",
            runtime_score,
            "Gateway process, imports, health, and deploy confidence.",
            "Add always-on daily canary with trend alerts.",
        ),
        QualityDimension(
            "harness",
            harness_score,
            "Canary metrics plus static behavior goldens.",
            "Add more live Telegram/API goldens with rubrics and latency budgets.",
        ),
        QualityDimension(
            "model",
            model_score,
            model_summary,
            "Fix frontier quota or route high-risk planning to a stronger planner.",
        ),
        QualityDimension(
            "autonomy",
            autonomy_score,
            "/goal, Workspace, evidence, and continuation control plane.",
            "Add goal budget policy, retries, and blocked-state triage reports.",
        ),
        QualityDimension(
            "memory",
            memory_score,
            "Alexandria continuity bridge and source-grounded behavior.",
            "Add live Alexandria/V11 golden evals with cited expected facts.",
        ),
        QualityDimension(
            "ux",
            ux_score,
            "Operator-mode commands, X ingestion, and guardrails.",
            "Add Telegram end-to-end canaries and concise-answer rubrics.",
        ),
        QualityDimension(
            "aac_workflows",
            aac_workflows_score,
            aac_workflows_summary,
            "Add RFQ/V11/quote/Workspace goldens from real AAC tasks.",
        ),
        QualityDimension(
            "business_ops",
            business_ops_score,
            business_ops_summary,
            "Add live RFQ dry-run, draft quote creation, approval, customer-send, and follow-up gates.",
        ),
    ]


def quality_summary(report: CanaryReport) -> dict[str, Any]:
    dimensions = _quality_dimensions(report)
    def passed(name: str) -> bool:
        result = _result_by_name(report, name)
        return bool(result and result.status == PASS)

    def result_status(name: str) -> str:
        result = _result_by_name(report, name)
        return result.status if result else FAIL

    def result_summary(name: str) -> str:
        result = _result_by_name(report, name)
        return result.summary if result else "missing result"

    model_result = _result_by_name(report, "runtime.model_route")
    local_deepseek_done = bool(
        model_result
        and model_result.status == PASS
        and "deepseek" in model_result.summary.lower()
    )
    golden_done = passed("contract.behavior_goldens")
    live_behavior_done = golden_done and passed("live.behavior_golden")
    trend_done = live_behavior_done and passed("contract.scorecard_trend")
    workflows_done = trend_done and passed("contract.aac_workflows")
    foundation_done = (
        workflows_done
        and passed("contract.planner_self_heal")
        and local_deepseek_done
        and passed("eval.hermes_reasoning")
        and passed("eval.frontier_wrapper")
        and passed("live.telegram_e2e")
        and passed("live.telegram_visible_delivery")
    )
    quote_ops_done = foundation_done and passed("contract.quote_ops_runtime")
    rfq_dry_run_done = quote_ops_done and passed("live.rfq_dry_run_quote_package")
    approved_rfq_draft_done = rfq_dry_run_done and passed("live.approved_rfq_draft_quote")
    increments = [
        {
            "target": "7.0/10",
            "increment": "Golden behavior layer and honest metrics",
            "status": "done" if golden_done else "open",
        },
        {
            "target": "7.5/10",
            "increment": "Live API behavior goldens with latency budgets",
            "status": "done" if live_behavior_done else "open",
        },
        {
            "target": "8.0/10",
            "increment": "Daily scheduled metrics trend and regression alerting",
            "status": "done" if trend_done else "open",
        },
        {
            "target": "8.5/10",
            "increment": "AAC workflow goldens: RFQ, V11 lookup, quote prep, Workspace report",
            "status": "done" if workflows_done else "open",
        },
        {
            "target": "9.0/10",
            "increment": "Local DeepSeek route, full Hermes reasoning, frontier wrapper, and live Telegram E2E gates",
            "status": "done" if foundation_done else "open",
        },
        {
            "target": "9.2/10",
            "increment": "Quote automation runtime is env-driven, approval-gated, and PDF generation is live",
            "status": "done" if quote_ops_done else "open",
        },
        {
            "target": "9.5/10",
            "increment": "Live RFQ dry-run creates a sourced quote package from V11 stock, customer history, and pricing rules",
            "status": "done" if rfq_dry_run_done else "open",
        },
        {
            "target": "9.7/10",
            "increment": "Approved RFQs create V11/Atlas draft quotes with QAMFORM preview and Telegram approval card",
            "status": "done" if approved_rfq_draft_done else "open",
        },
        {
            "target": "10.0/10",
            "increment": "Approved quotes send to customers, write audit proof, schedule follow-up, and surface exceptions",
            "status": "open",
        },
    ]
    raw_score = sum(item.score for item in dimensions) / len(dimensions)
    completed_targets = [
        float(item["target"].split("/", 1)[0])
        for item in increments
        if item["status"] == "done"
    ]
    score = round(max([raw_score, *completed_targets]), 1)
    score_caps: list[dict[str, Any]] = []
    if result_status("live.telegram_visible_delivery") != PASS:
        score_caps.append(
            {
                "cap": 8.9,
                "reason": "human-visible Telegram delivery is not currently proven",
                "evidence": result_summary("live.telegram_visible_delivery"),
            }
        )
    readiness = readiness_summary(report)
    if readiness.get("status") != "frontier_ready":
        score_caps.append(
            {
                "cap": 8.9,
                "reason": "frontier readiness has open gates",
                "evidence": readiness.get("open_gates", []),
            }
        )
    if score_caps:
        score = round(min(score, *(float(item["cap"]) for item in score_caps)), 1)
    return {
        "score": score,
        "target": 9.0,
        "dimensions": [asdict(item) for item in dimensions],
        "increments": increments,
        "caps": score_caps,
    }


def readiness_summary(report: CanaryReport) -> dict[str, Any]:
    def result_status(name: str) -> str:
        result = _result_by_name(report, name)
        return result.status if result else FAIL

    def result_summary(name: str) -> str:
        result = _result_by_name(report, name)
        return result.summary if result else "missing result"

    model_result = _result_by_name(report, "runtime.model_route")
    model_route_ok = bool(
        model_result
        and model_result.status == PASS
        and (
            "deepseek" in model_result.summary.lower()
            or "office-deepseek" in model_result.summary.lower()
        )
    )
    grounding_ok = (
        result_status("contract.aac_workflows") == PASS
        and result_status("contract.x_scrape") == PASS
    )
    control_plane_ok = (
        result_status("contract.workspace_store") == PASS
        and result_status("contract.goal_workspace") == PASS
    )
    reliability_ok = (
        result_status("live.gateway_health") == PASS
        and result_status("contract.scorecard_trend") == PASS
        and result_status("contract.planner_self_heal") == PASS
    )

    gates = [
        {
            "name": "local_deepseek_route",
            "status": PASS if model_route_ok else WARN,
            "requirement": "Active Hermes planner is the local DeepSeek route, not a stale fallback.",
            "evidence": result_summary("runtime.model_route"),
        },
        {
            "name": "hermes_reasoning_eval",
            "status": result_status("eval.hermes_reasoning"),
            "requirement": "Hermes /v1/responses no-tool path passes the same deterministic tasks within latency budgets.",
            "evidence": result_summary("eval.hermes_reasoning"),
        },
        {
            "name": "frontier_wrapper",
            "status": result_status("eval.frontier_wrapper"),
            "requirement": "Configured frontier wrapper works with structured reasoning controls.",
            "evidence": result_summary("eval.frontier_wrapper"),
        },
        {
            "name": "live_api_behavior",
            "status": result_status("live.behavior_golden"),
            "requirement": "OpenAI-compatible API behavior probes pass with latency budgets.",
            "evidence": result_summary("live.behavior_golden"),
        },
        {
            "name": "telegram_e2e",
            "status": result_status("live.telegram_e2e"),
            "requirement": "Telegram DM path proves latency, no restart interruption, and no bogus capability refusal.",
            "evidence": result_summary("live.telegram_e2e"),
        },
        {
            "name": "grounding_and_retrieval",
            "status": PASS if grounding_ok else WARN,
            "requirement": "AAC workflow facts and X/Twitter retrieval contracts pass from source material.",
            "evidence": f"{result_summary('contract.aac_workflows')} / {result_summary('contract.x_scrape')}",
        },
        {
            "name": "control_plane",
            "status": PASS if control_plane_ok else WARN,
            "requirement": "Workspace and goal state survive as durable control-plane primitives.",
            "evidence": f"{result_summary('contract.workspace_store')} / {result_summary('contract.goal_workspace')}",
        },
        {
            "name": "operator_safety",
            "status": result_status("contract.operator_safety"),
            "requirement": "External sends/destructive actions stay approval-gated and vague loops stay bounded.",
            "evidence": result_summary("contract.operator_safety"),
        },
        {
            "name": "runtime_reliability",
            "status": PASS if reliability_ok else WARN,
            "requirement": "Gateway health, scheduled trend, and self-heal playbooks are live.",
            "evidence": f"{result_summary('live.gateway_health')} / {result_summary('contract.scorecard_trend')} / {result_summary('contract.planner_self_heal')}",
        },
    ]
    passed = [gate for gate in gates if gate["status"] == PASS]
    open_gates = [gate for gate in gates if gate["status"] != PASS]
    return {
        "status": "frontier_ready" if not open_gates else "not_frontier_ready",
        "passed": len(passed),
        "total": len(gates),
        "open_gates": open_gates,
        "gates": gates,
        "diagnostics": [
            {
                "name": "raw_local_model_reasoning",
                "status": result_status("eval.local_model_reasoning"),
                "evidence": result_summary("eval.local_model_reasoning"),
                "purpose": "Direct DeepSeek telemetry; reported separately from orchestration readiness.",
            },
            {
                "name": "telegram_visible_delivery",
                "status": result_status("live.telegram_visible_delivery"),
                "evidence": result_summary("live.telegram_visible_delivery"),
                "purpose": "Human-visible Telegram DM loop; separate from signed webhook simulation.",
            }
        ],
        "docs_basis": [
            "Responses API state handling",
            "reasoning.effort controls",
            "Structured Outputs for grader/probe validation",
            "Direct local-model evals separated from agent-orchestration evals",
            "transcript replay and error-regression evals",
        ],
    }


def report_to_dict(report: CanaryReport) -> dict[str, Any]:
    return {
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "duration_seconds": round(report.finished_at - report.started_at, 3),
        "status": report.status,
        "score": report.score,
        "effective_max_score": report.effective_max_score,
        "percent": round(report.percent, 2),
        "fail_under": report.fail_under,
        "json_path": report.json_path,
        "markdown_path": report.markdown_path,
        "runtime": report.runtime,
        "overall_quality": quality_summary(report),
        "readiness": readiness_summary(report),
        "results": [asdict(result) for result in report.results],
    }


def render_markdown(report: CanaryReport) -> str:
    generated = time.strftime(
        "%Y-%m-%d %H:%M:%S %Z",
        time.localtime(report.finished_at),
    )
    readiness = readiness_summary(report)
    lines = [
        "# Hermes Capability Metrics",
        "",
        f"Generated: {generated}",
        f"Status: {report.status.upper()}",
        f"Measured gate coverage: {report.score:.1f}/{report.effective_max_score:.1f} ({report.percent:.1f}%)",
        f"Gate: {report.fail_under:.1f}%",
        f"Frontier readiness: {readiness['status'].upper()} ({readiness['passed']}/{readiness['total']} gates passed)",
        "",
        "## Runtime Snapshot",
        "",
    ]
    runtime = report.runtime or {}
    wrapper = runtime.get("wrapper", {}) if isinstance(runtime, dict) else {}
    model = runtime.get("model", {}) if isinstance(runtime, dict) else {}
    health = runtime.get("health", {}) if isinstance(runtime, dict) else {}
    git = runtime.get("git", {}) if isinstance(runtime, dict) else {}
    repo_git = git.get("repo_root", {}) if isinstance(git, dict) else {}
    runtime_git = git.get("runtime_repo", {}) if isinstance(git, dict) else {}
    lines.extend(
        [
            f"- Wrapper-selected repo: `{wrapper.get('selected_repo', 'unknown')}`",
            f"- Python path: `{(runtime.get('python') or {}).get('path', 'unknown') if isinstance(runtime, dict) else 'unknown'}`",
            f"- Model route: `{model.get('provider', '')} -> {model.get('name', '')}`",
            f"- Health URL: `{health.get('url', 'unknown')}` ({'ok' if health.get('ok') else 'not ok'})",
            f"- Repo SHA: `{repo_git.get('short_sha', 'unknown')}` dirty={repo_git.get('dirty', '')}",
            f"- Runtime SHA: `{runtime_git.get('short_sha', 'unknown')}` dirty={runtime_git.get('dirty', '')}",
            "",
        ]
    )
    lines.extend(
        [
        "## Readiness Gates",
        "",
        "| Gate | Status | Requirement | Evidence |",
        "|---|---:|---|---|",
        ]
    )
    for gate in readiness["gates"]:
        lines.append(
            "| {name} | {status} | {requirement} | {evidence} |".format(
                name=gate["name"],
                status=gate["status"].upper(),
                requirement=gate["requirement"].replace("|", "\\|"),
                evidence=gate["evidence"].replace("|", "\\|"),
            )
        )
    lines.extend(
        [
            "",
            "## Raw Checks",
        "",
        "| Canary | Status | Score | Summary |",
        "|---|---:|---:|---|",
        ]
    )
    for result in report.results:
        score = (
            "skip" if result.status == SKIP else f"{result.score:.1f}/{result.max_score:.1f}"
        )
        summary = result.summary.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{result.name}` | {result.status.upper()} | {score} | {summary} |")
    quality = quality_summary(report)
    lines.extend(
        [
            "",
            "## Legacy Heuristic",
            "",
            "This section is retained for trend compatibility. Readiness gates above are authoritative.",
            "",
            "| Dimension | Score | Summary | Next Increment |",
            "|---|---:|---|---|",
        ]
    )
    for dimension in quality["dimensions"]:
        lines.append(
            "| {name} | {score:.1f}/10 | {summary} | {next_increment} |".format(
                name=dimension["name"],
                score=dimension["score"],
                summary=dimension["summary"].replace("|", "\\|"),
                next_increment=dimension["next_increment"].replace("|", "\\|"),
            )
        )
    lines.extend(["", "## Legacy Ladder", ""])
    for item in quality["increments"]:
        lines.append(f"- `{item['target']}` [{item['status']}] {item['increment']}")
    lines.extend(["", "## Details", ""])
    for result in report.results:
        lines.extend(
            [
                f"### {result.name}",
                "",
                f"- Status: {result.status.upper()}",
                f"- Score: {result.score:.1f}/{result.max_score:.1f}",
                f"- Duration: {result.duration_ms:.1f}ms",
                f"- Summary: {result.summary}",
            ]
        )
        if result.details:
            lines.extend(
                [
                    "",
                    "```json",
                    json.dumps(result.details, indent=2, sort_keys=True),
                    "```",
                ]
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_report(report: CanaryReport, output_dir: Path) -> CanaryReport:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(report.finished_at))
    json_path = output_dir / f"hermes-canary-{stamp}.json"
    markdown_path = output_dir / f"hermes-canary-{stamp}.md"
    latest_json_path = output_dir / "latest.json"
    latest_markdown_path = output_dir / "latest.md"
    history_path = output_dir / "history.jsonl"
    report.json_path = str(json_path)
    report.markdown_path = str(markdown_path)
    payload = report_to_dict(report)
    markdown = render_markdown(report)
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(markdown, encoding="utf-8")
    latest_json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    latest_markdown_path.write_text(markdown, encoding="utf-8")
    history_entry = {
        "finished_at": report.finished_at,
        "status": report.status,
        "score": report.score,
        "effective_max_score": report.effective_max_score,
        "percent": round(report.percent, 2),
        "overall_score": payload["overall_quality"]["score"],
        "json_path": str(json_path),
        "markdown_path": str(markdown_path),
        "repo_sha": (
            payload.get("runtime", {})
            .get("git", {})
            .get("repo_root", {})
            .get("short_sha", "")
        ),
        "runtime_sha": (
            payload.get("runtime", {})
            .get("git", {})
            .get("runtime_repo", {})
            .get("short_sha", "")
        ),
        "failed": [result.name for result in report.results if result.status == FAIL],
        "warned": [result.name for result in report.results if result.status == WARN],
        "skipped": [result.name for result in report.results if result.status == SKIP],
    }
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(history_entry, sort_keys=True) + "\n")
    return report


def _print_text_summary(report: CanaryReport) -> None:
    readiness = readiness_summary(report)
    print("Hermes Capability Metrics")
    print(f"Status: {report.status.upper()}")
    print(f"Measured gate coverage: {report.score:.1f}/{report.effective_max_score:.1f} ({report.percent:.1f}%)")
    print(f"Frontier readiness: {readiness['status'].upper()} ({readiness['passed']}/{readiness['total']} gates passed)")
    if report.markdown_path:
        print(f"Markdown: {report.markdown_path}")
    if report.json_path:
        print(f"JSON: {report.json_path}")
    print()
    print("Readiness gates:")
    for gate in readiness["gates"]:
        print(f"{gate['status'].upper():<5} {gate['name']:<24} - {gate['evidence']}")
    print()
    print("Raw checks:")
    for result in report.results:
        score = "skip" if result.status == SKIP else f"{result.score:.1f}/{result.max_score:.1f}"
        print(f"{result.status.upper():<5} {score:<11} {result.name} - {result.summary}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes canary",
        description="Run Hermes canary/product-eval checks and write a metrics report.",
    )
    parser.add_argument(
        "--gateway-url",
        default=os.getenv("HERMES_CANARY_GATEWAY_URL", "http://127.0.0.1:8642"),
    )
    parser.add_argument(
        "--env-wrapper",
        type=Path,
        default=None,
        help="Path to hermes-env.sh for runtime model selection checks",
    )
    parser.add_argument("--repo-root", type=Path, default=_repo_root())
    parser.add_argument("--hermes-home", type=Path, default=None)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for JSON and Markdown metrics reports",
    )
    parser.add_argument(
        "--require-live",
        action="store_true",
        help="Fail if live HTTP/network canaries are unavailable",
    )
    parser.add_argument(
        "--live-behavior",
        action="store_true",
        help="Run live /v1/responses behavior goldens using the API key",
    )
    parser.add_argument(
        "--reasoning-eval",
        action="store_true",
        help="Run deterministic reasoning/guardrail evals against direct DeepSeek and full Hermes paths",
    )
    parser.add_argument(
        "--frontier-eval",
        action="store_true",
        help="Run frontier wrapper probe with structured reasoning controls",
    )
    parser.add_argument(
        "--frontier-model",
        default=os.getenv("OPENAI_FRONTIER_MODEL", os.getenv("HERMES_FRONTIER_MODEL", "gpt-5.5")),
        help="OpenAI-compatible frontier model for --frontier-eval",
    )
    parser.add_argument(
        "--frontier-base-url",
        default=os.getenv("HERMES_FRONTIER_BASE_URL", "https://api.openai.com/v1"),
        help="OpenAI-compatible base URL for --frontier-eval",
    )
    parser.add_argument(
        "--frontier-api-key",
        default=os.getenv("HERMES_FRONTIER_API_KEY", os.getenv("OPENAI_API_KEY", "")),
        help="OpenAI-compatible API key for --frontier-eval; Gemini uses GEMINI_API_KEY/GOOGLE_API_KEY",
    )
    parser.add_argument(
        "--telegram-webhook-sim",
        action="store_true",
        default=os.getenv("HERMES_CANARY_TELEGRAM_WEBHOOK_SIM", "").strip().lower() in {"1", "true", "yes", "on"},
        help="Run a signed local Telegram webhook simulation and require matching E2E evidence",
    )
    parser.add_argument(
        "--telegram-visible-probe",
        action="store_true",
        default=os.getenv("HERMES_CANARY_TELEGRAM_VISIBLE_PROBE", "").strip().lower() in {"1", "true", "yes", "on"},
        help="Send a real Telegram DM and wait for a human-visible ack",
    )
    parser.add_argument(
        "--telegram-visible-wait",
        type=float,
        default=float(os.getenv("HERMES_CANARY_TELEGRAM_VISIBLE_WAIT", "15") or 15),
        help="Seconds to wait for --telegram-visible-probe ack",
    )
    parser.add_argument(
        "--rfq-dry-run",
        action="store_true",
        default=os.getenv("HERMES_CANARY_RFQ_DRY_RUN", "").strip().lower() in {"1", "true", "yes", "on"},
        help="Run a live read-only V11 RFQ quote-package dry run; no V11 writes or customer sends",
    )
    parser.add_argument(
        "--approved-rfq-draft",
        action="store_true",
        default=os.getenv("HERMES_CANARY_APPROVED_RFQ_DRAFT", "").strip().lower() in {"1", "true", "yes", "on"},
        help="Build an approved-RFQ draft payload, QAMFORM preview, and Telegram approval-card artifact; writes/sends remain disabled",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("HERMES_CANARY_API_KEY", ""),
        help="API key for live /v1/responses canaries; defaults to HERMES_CANARY_API_KEY",
    )
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument(
        "--fail-under",
        type=float,
        default=80.0,
        help="Minimum score percentage for zero exit",
    )
    parser.add_argument(
        "--x-url",
        action="append",
        default=[],
        help="Optional live X/Twitter URL canary; repeatable",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Print the metrics JSON to stdout",
    )
    return parser


def options_from_args(args: argparse.Namespace) -> CanaryOptions:
    explicit_hermes_home = args.hermes_home is not None
    hermes_home = Path(args.hermes_home).expanduser() if explicit_hermes_home else _default_hermes_home()
    env_wrapper = args.env_wrapper
    if env_wrapper is None:
        env_wrapper = _default_env_wrapper(hermes_home)
    wrapper_home = _home_from_env_wrapper_path(env_wrapper)
    default_home = Path.home() / ".hermes"
    if wrapper_home and (not explicit_hermes_home or hermes_home == default_home):
        hermes_home = wrapper_home
    return CanaryOptions(
        repo_root=Path(args.repo_root).expanduser(),
        hermes_home=hermes_home,
        gateway_url=args.gateway_url,
        env_wrapper=Path(env_wrapper).expanduser() if env_wrapper else None,
        api_key=str(getattr(args, "api_key", "") or ""),
        live_behavior=bool(getattr(args, "live_behavior", False)),
        reasoning_eval=bool(getattr(args, "reasoning_eval", False)),
        frontier_eval=bool(getattr(args, "frontier_eval", False)),
        frontier_model=str(getattr(args, "frontier_model", "") or ""),
        frontier_api_key=str(getattr(args, "frontier_api_key", "") or ""),
        frontier_base_url=str(getattr(args, "frontier_base_url", "") or "https://api.openai.com/v1"),
        telegram_webhook_sim=bool(getattr(args, "telegram_webhook_sim", False)),
        telegram_visible_probe=bool(getattr(args, "telegram_visible_probe", False)),
        telegram_visible_wait=float(getattr(args, "telegram_visible_wait", 15.0) or 15.0),
        rfq_dry_run=bool(getattr(args, "rfq_dry_run", False)),
        approved_rfq_draft=bool(getattr(args, "approved_rfq_draft", False)),
        require_live=bool(args.require_live),
        timeout=float(args.timeout),
        fail_under=float(args.fail_under),
        output_dir=Path(args.output_dir).expanduser() if args.output_dir else None,
        x_urls=tuple(args.x_url or ()),
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    options = options_from_args(args)
    output_dir = options.output_dir or (options.hermes_home / "canary" / "reports")
    report = run_canary_suite(options)
    write_report(report, output_dir)
    if args.json_output:
        print(json.dumps(report_to_dict(report), indent=2, sort_keys=True))
    else:
        _print_text_summary(report)
    return 0 if report.status != FAIL else 1


def cmd_canary(args: argparse.Namespace) -> None:
    options = options_from_args(args)
    output_dir = options.output_dir or (options.hermes_home / "canary" / "reports")
    report = run_canary_suite(options)
    write_report(report, output_dir)
    if getattr(args, "json_output", False):
        print(json.dumps(report_to_dict(report), indent=2, sort_keys=True))
    else:
        _print_text_summary(report)
    if report.status == FAIL:
        raise SystemExit(1)


if __name__ == "__main__":
    raise SystemExit(main())
