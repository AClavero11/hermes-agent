"""Codex CLI worker tool for subscription-backed coding delegation."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from tools.registry import registry, tool_error, tool_result


DEFAULT_CODEX_WORKER_MODEL = "gpt-5.5"
DEFAULT_TIMEOUT_SECONDS = 900
MIN_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 3600
STATUS_CACHE_TTL_SECONDS = 30.0

_STATUS_CACHE: tuple[float, dict[str, Any]] | None = None

_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,119}$")
_ALLOWED_SANDBOXES = {"read-only", "workspace-write"}
_CODING_SIGNAL_RE = re.compile(
    r"\b("
    r"repo|repository|code|coding|test|tests|pytest|bug|debug|fix|repair|"
    r"implement|refactor|review|patch|file|module|function|class|script|"
    r"lint|build|compile|typecheck|package|dependency|git|branch|pull request|"
    r"pr|readme|docs|config|tool|cli|canary"
    r")\b",
    re.IGNORECASE,
)
_BLOCKED_TASK_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(send|email|text|sms|telegram|whatsapp|slack|discord|dm|post|"
            r"publish|tweet)\b.{0,80}\b(customer|supplier|vendor|client|rfq|"
            r"quote|invoice|purchase order|po|x\.com|twitter)\b",
            re.IGNORECASE | re.DOTALL,
        ),
        "external-facing sends/posts/customer communications require explicit operator approval",
    ),
    (
        re.compile(
            r"\b(create|submit|issue|send|approve|release|update|write)\b.{0,80}"
            r"\b(quote|invoice|purchase order|po|rfq|order|payment|wire|"
            r"shipment|aeroxchange|v11|atlas)\b",
            re.IGNORECASE | re.DOTALL,
        ),
        "business-system writes and quote/order/payment actions are outside the Codex worker lane",
    ),
    (
        re.compile(
            r"\b(git\s+push|push\s+to\s+(github|origin|production|prod)|"
            r"push\s+(the\s+)?(repo|repository|branch|changes|code)\s+to\s+"
            r"(github|origin|production|prod)|"
            r"force[-\s]?push|deploy\s+to\s+(prod|production)|"
            r"release\s+to\s+production)\b",
            re.IGNORECASE,
        ),
        "pushes and production deploys must stay in the parent operator flow",
    ),
    (
        re.compile(
            r"\b(commit\s+(the|these|my|changes|it|to)|make\s+a\s+commit|"
            r"create\s+a\s+commit)\b",
            re.IGNORECASE,
        ),
        "commits must be made by the parent Hermes operator after review",
    ),
    (
        re.compile(
            r"\b(rm\s+-rf|git\s+reset\s+--hard|git\s+clean\s+-fd|"
            r"drop\s+database|truncate\s+table)\b",
            re.IGNORECASE,
        ),
        "destructive commands are blocked for delegated Codex workers",
    ),
    (
        re.compile(
            r"\b(credential|secret|api key|token|password)\b.{0,80}"
            r"\b(exfiltrate|print|dump|show|send|upload)\b",
            re.IGNORECASE | re.DOTALL,
        ),
        "credential disclosure is blocked",
    ),
)
_SENSITIVE_ENV_RE = re.compile(
    r"(API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH_COOKIE|SESSION_COOKIE)$",
    re.IGNORECASE,
)
_SENSITIVE_ENV_PREFIXES = (
    "OPENAI_",
    "OPENROUTER_",
    "ANTHROPIC_",
    "GEMINI_",
    "GOOGLE_",
    "XAI_",
    "DEEPSEEK_",
    "KIMI_",
    "MOONSHOT_",
    "MINIMAX_",
    "MISTRAL_",
    "TOGETHER_",
    "TAVILY_",
    "FIRECRAWL_",
    "AEROXCHANGE_",
    "V11_",
    "ATLAS_",
)


def _copy_status(status: dict[str, Any]) -> dict[str, Any]:
    return dict(status)


def _build_codex_env() -> dict[str, str]:
    """Return an environment that lets Codex use local login without API keys."""
    env = dict(os.environ)
    for key in list(env):
        upper = key.upper()
        if _SENSITIVE_ENV_RE.search(upper) or upper.startswith(_SENSITIVE_ENV_PREFIXES):
            env.pop(key, None)
    env["NO_COLOR"] = "1"
    env["HERMES_CODEX_WORKER"] = "1"
    return env


def _run_codex_login_status(timeout: float = 10.0) -> tuple[int, str]:
    codex_bin = shutil.which("codex")
    if not codex_bin:
        return 127, "codex CLI not found on PATH"
    proc = subprocess.run(
        [codex_bin, "login", "status"],
        capture_output=True,
        text=True,
        timeout=max(float(timeout), 1.0),
        check=False,
        env=_build_codex_env(),
    )
    return proc.returncode, "\n".join(part for part in (proc.stdout, proc.stderr) if part)


def _parse_auth_mode(output: str) -> str:
    text = (output or "").strip().lower()
    if "logged in using chatgpt" in text:
        return "chatgpt"
    if "logged in using an api key" in text or "logged in using api key" in text:
        return "api_key"
    if "not logged" in text or "no login" in text:
        return "not_logged_in"
    return "unknown"


def codex_worker_status(*, force: bool = False, timeout: float = 10.0) -> dict[str, Any]:
    """Return current Codex worker availability without exposing credentials."""
    global _STATUS_CACHE

    now = time.monotonic()
    if not force and _STATUS_CACHE and now - _STATUS_CACHE[0] <= STATUS_CACHE_TTL_SECONDS:
        return _copy_status(_STATUS_CACHE[1])

    codex_bin = shutil.which("codex")
    if not codex_bin:
        status = {
            "available": False,
            "auth_mode": "missing_cli",
            "binary": "",
            "summary": "codex CLI not found on PATH",
        }
        _STATUS_CACHE = (now, status)
        return _copy_status(status)

    try:
        returncode, output = _run_codex_login_status(timeout=timeout)
    except subprocess.TimeoutExpired:
        status = {
            "available": False,
            "auth_mode": "unknown",
            "binary": codex_bin,
            "summary": "codex login status timed out",
        }
        _STATUS_CACHE = (now, status)
        return _copy_status(status)
    except Exception as exc:
        status = {
            "available": False,
            "auth_mode": "unknown",
            "binary": codex_bin,
            "summary": f"{type(exc).__name__}: {exc}",
        }
        _STATUS_CACHE = (now, status)
        return _copy_status(status)

    auth_mode = _parse_auth_mode(output)
    available = returncode == 0 and auth_mode == "chatgpt"
    if available:
        summary = "Codex CLI is logged in using ChatGPT"
    elif auth_mode == "api_key":
        summary = "Codex CLI is using an API key; ChatGPT login is required for the subscription worker lane"
    elif auth_mode == "not_logged_in":
        summary = "Codex CLI is not logged in"
    else:
        summary = "Codex CLI login state could not be verified"

    status = {
        "available": available,
        "auth_mode": auth_mode,
        "binary": codex_bin,
        "summary": summary,
        "status_preview": _tail(output, 500),
    }
    _STATUS_CACHE = (now, status)
    return _copy_status(status)


def check_codex_worker_requirements() -> bool:
    return bool(codex_worker_status().get("available"))


def validate_codex_worker_task(task: str) -> str | None:
    """Return an error string if *task* is unsafe or not code/repo work."""
    text = str(task or "").strip()
    if not text:
        return "codex_worker requires a non-empty coding task"

    for pattern, reason in _BLOCKED_TASK_PATTERNS:
        if pattern.search(text):
            return reason

    if not _CODING_SIGNAL_RE.search(text):
        return "codex_worker is limited to coding, repository, debugging, test, docs, and config tasks"

    return None


def _resolve_workdir(raw_workdir: Any = None) -> tuple[Path | None, str | None]:
    raw = str(raw_workdir or os.getenv("TERMINAL_CWD") or os.getcwd()).strip()
    if not raw:
        return None, "workdir could not be resolved"

    base = Path(os.getenv("TERMINAL_CWD") or os.getcwd()).expanduser()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate

    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        return None, f"workdir does not exist or cannot be resolved: {exc}"

    if not resolved.is_dir():
        return None, f"workdir is not a directory: {resolved}"

    protected_roots = [
        Path.home() / ".secrets",
        Path.home() / ".ssh",
        Path.home() / ".gnupg",
        Path.home() / "Library" / "Keychains",
        Path("/etc"),
        Path("/private/etc"),
        Path("/var/db"),
        Path("/Library/Keychains"),
    ]
    for protected in protected_roots:
        try:
            resolved.relative_to(protected.expanduser().resolve(strict=False))
        except ValueError:
            continue
        return None, f"workdir is inside a protected path: {protected}"

    if ".secrets" in resolved.parts:
        return None, "workdir cannot be inside a .secrets directory"

    return resolved, None


def _normalize_model(raw_model: Any) -> tuple[str | None, str | None]:
    model = str(raw_model or DEFAULT_CODEX_WORKER_MODEL).strip()
    if not _MODEL_RE.match(model):
        return None, "model must be a simple Codex model id such as gpt-5.5 or gpt-5.4-mini"
    return model, None


def _normalize_sandbox(raw_sandbox: Any) -> tuple[str | None, str | None]:
    sandbox = str(raw_sandbox or "workspace-write").strip()
    if sandbox not in _ALLOWED_SANDBOXES:
        return None, "sandbox must be read-only or workspace-write"
    return sandbox, None


def _coerce_timeout(raw_timeout: Any) -> int:
    try:
        timeout = int(raw_timeout)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SECONDS
    return max(MIN_TIMEOUT_SECONDS, min(timeout, MAX_TIMEOUT_SECONDS))


def _tail(value: Any, limit: int = 12000) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    if len(text) <= limit:
        return text
    return "[truncated]\n" + text[-limit:]


def _build_worker_prompt(task: str, workdir: Path) -> str:
    return (
        "You are a delegated Hermes Codex worker launched through the local Codex CLI.\n"
        f"Working directory: {workdir}\n\n"
        "Scope:\n"
        "- Do coding, repository, debugging, test, documentation, and config work only.\n"
        "- Keep changes minimal and consistent with the existing codebase.\n"
        "- Respect existing user edits; do not revert unrelated changes.\n\n"
        "Hard safety rules:\n"
        "- Do not send emails, messages, posts, quotes, invoices, orders, payments, or customer/vendor communications.\n"
        "- Do not mutate AAC business systems such as V11, Atlas, Aeroxchange, procurement, inventory, or accounting.\n"
        "- Do not commit, push, force-push, deploy, reset hard, clean files, delete unrelated files, or expose secrets.\n\n"
        "When you change code, run focused verification that fits the change. "
        "End with a concise summary, files changed, and verification performed.\n\n"
        "Task:\n"
        f"{task.strip()}\n"
    )


def _command_preview(command: list[str]) -> list[str]:
    preview = list(command)
    if preview and preview[-1] == "-":
        preview[-1] = "<prompt-stdin>"
    return preview


def codex_worker(args: dict[str, Any], **kwargs) -> str:
    """Run a bounded Codex CLI worker for eligible code/repo tasks."""
    args = args or {}
    task = str(args.get("task") or "").strip()
    validation_error = validate_codex_worker_task(task)
    if validation_error:
        return tool_error(
            validation_error,
            success=False,
            code="unsafe_or_non_coding_task",
        )

    workdir, workdir_error = _resolve_workdir(args.get("workdir"))
    if workdir_error:
        return tool_error(workdir_error, success=False, code="invalid_workdir")

    model, model_error = _normalize_model(args.get("model"))
    if model_error:
        return tool_error(model_error, success=False, code="invalid_model")

    sandbox, sandbox_error = _normalize_sandbox(args.get("sandbox"))
    if sandbox_error:
        return tool_error(sandbox_error, success=False, code="invalid_sandbox")

    timeout_seconds = _coerce_timeout(args.get("timeout_seconds"))
    status = codex_worker_status(timeout=min(timeout_seconds, 10))
    if not status.get("available"):
        return tool_error(
            f"codex_worker unavailable: {status.get('summary', 'unknown status')}",
            success=False,
            code="codex_worker_unavailable",
            status=status,
        )

    codex_bin = str(status.get("binary") or shutil.which("codex") or "codex")
    prompt = _build_worker_prompt(task, workdir)

    with tempfile.TemporaryDirectory(prefix="hermes-codex-worker-") as temp_dir:
        final_message_path = Path(temp_dir) / "last-message.md"
        command = [
            codex_bin,
            "-a",
            "never",
            "exec",
            "-m",
            model,
            "-s",
            sandbox,
            "-C",
            str(workdir),
            "--output-last-message",
            str(final_message_path),
            "-",
        ]

        if bool(args.get("dry_run")):
            return tool_result(
                success=True,
                dry_run=True,
                available=True,
                auth_mode=status.get("auth_mode"),
                model=model,
                sandbox=sandbox,
                workdir=str(workdir),
                timeout_seconds=timeout_seconds,
                command_preview=_command_preview(command),
            )

        started = time.perf_counter()
        try:
            proc = subprocess.run(
                command,
                input=prompt,
                cwd=str(workdir),
                env=_build_codex_env(),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return tool_error(
                f"codex_worker timed out after {timeout_seconds}s",
                success=False,
                code="timeout",
                timeout_seconds=timeout_seconds,
                elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1),
                stdout_tail=_tail(exc.stdout),
                stderr_tail=_tail(exc.stderr),
            )

        final_message = ""
        try:
            if final_message_path.is_file():
                final_message = final_message_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            final_message = ""

    payload = {
        "success": proc.returncode == 0,
        "available": True,
        "auth_mode": status.get("auth_mode"),
        "model": model,
        "sandbox": sandbox,
        "workdir": str(workdir),
        "timeout_seconds": timeout_seconds,
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1),
        "exit_code": proc.returncode,
        "final_message": _tail(final_message, 20000),
        "stdout_tail": _tail(proc.stdout),
        "stderr_tail": _tail(proc.stderr),
    }
    if proc.returncode != 0:
        payload["error"] = f"codex exec exited with status {proc.returncode}"
    return tool_result(payload)


CODEX_WORKER_SCHEMA = {
    "name": "codex_worker",
    "description": (
        "Delegate substantial coding/repo/debug/test/doc/config work to the local Codex CLI "
        "using Studio's ChatGPT login, so eligible work uses Codex subscription limits instead "
        "of Hermes API/OpenRouter spend. Use only for codebase work. Do not use for quotes, "
        "customer messages, V11/Atlas/Aeroxchange writes, external posts, commits, pushes, or deploys."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Concrete coding/repo task for the Codex worker.",
            },
            "workdir": {
                "type": "string",
                "description": "Repository directory. Defaults to TERMINAL_CWD or the current process cwd.",
            },
            "model": {
                "type": "string",
                "description": "Codex model id. Defaults to gpt-5.5; use gpt-5.4-mini when preserving 5.5 limits matters.",
            },
            "sandbox": {
                "type": "string",
                "enum": ["workspace-write", "read-only"],
                "description": "Codex sandbox mode. workspace-write is default; read-only is for review/analysis.",
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "Execution timeout, clamped to 60-3600 seconds. Default 900.",
            },
            "dry_run": {
                "type": "boolean",
                "description": "Validate auth/safety/workdir and return the command preview without launching Codex.",
            },
        },
        "required": ["task"],
    },
}


registry.register(
    name="codex_worker",
    toolset="code_execution",
    schema=CODEX_WORKER_SCHEMA,
    handler=codex_worker,
    check_fn=check_codex_worker_requirements,
    max_result_size_chars=60_000,
)
