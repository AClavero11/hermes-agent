#!/usr/bin/env python3
"""
Claude Code Remote Control -- dispatch tasks from Telegram.

Lets AC send `/cc "task description"` from Telegram to spawn a local
Claude Code session on Mac Studio, stream progress back every 30s,
and approve/reject the resulting changes via inline keyboard buttons.

Architecture:
  1. Telegram /cc command parsed by gateway, routed here
  2. Spawns `claude -p "task" --output-format stream-json` as subprocess
  3. Background asyncio task parses stream-json lines, sends Telegram
     progress updates every 30s (or on significant events)
  4. On completion: sends git diff summary with [Approve] [Reject] [Diff] buttons
  5. Callback handler processes button presses: commit, revert, or show diff

Session state persists to ~/.hermes/cc-sessions.json so sessions survive
gateway restarts. Orphaned sessions (dead process) are cleaned up on load.
"""

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Coroutine, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_CONCURRENT = 3
DEFAULT_REPO = str(Path.home() / "projects" / "advanced-parts")
DEFAULT_MAX_TURNS = 50
PROGRESS_INTERVAL_SECONDS = 30
APPROVAL_TIMEOUT_SECONDS = 3600  # 1 hour
SESSIONS_FILE = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes")) / "cc-sessions.json"
TELEGRAM_MAX_LENGTH = 4096

# Type aliases for the send callbacks the gateway passes in
SendMessageFn = Callable[..., Coroutine]
SendKeyboardFn = Callable[..., Coroutine]


# ---------------------------------------------------------------------------
# Session model
# ---------------------------------------------------------------------------

@dataclass
class CCSession:
    session_id: str
    task: str
    status: str  # running, complete, failed, approved, rejected, timeout
    chat_id: str
    repo_path: str
    started_at: float
    pid: Optional[int] = None
    files_touched: List[str] = field(default_factory=list)
    output_lines: List[str] = field(default_factory=list)
    result_summary: str = ""
    last_progress_at: float = 0.0
    progress_message_id: Optional[str] = None
    completion_message_id: Optional[str] = None

    def elapsed(self) -> float:
        return time.time() - self.started_at

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CCSession":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# In-memory session store (persisted to JSON)
# ---------------------------------------------------------------------------

_sessions: Dict[str, CCSession] = {}


def _save_sessions() -> None:
    """Persist sessions to disk. Best-effort, never raises."""
    try:
        serializable = {sid: s.to_dict() for sid, s in _sessions.items()}
        SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SESSIONS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(serializable, indent=2))
        tmp.rename(SESSIONS_FILE)
    except Exception as exc:
        logger.warning("Failed to persist cc-sessions: %s", exc)


def _load_sessions() -> None:
    """Load sessions from disk, cleaning up orphans."""
    global _sessions
    if not SESSIONS_FILE.exists():
        return
    try:
        raw = json.loads(SESSIONS_FILE.read_text())
        for sid, data in raw.items():
            session = CCSession.from_dict(data)
            # Check if process is still alive
            if session.status == "running" and session.pid:
                if not _is_process_alive(session.pid):
                    session.status = "failed"
                    session.result_summary = "Process died (orphaned session)"
                    logger.info("Cleaned up orphaned cc session %s (pid %d)", sid, session.pid)
            _sessions[sid] = session
        _save_sessions()
    except Exception as exc:
        logger.warning("Failed to load cc-sessions: %s", exc)


def _is_process_alive(pid: int) -> bool:
    """Check if a process with given PID is still running."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# Load on module import
_load_sessions()


# ---------------------------------------------------------------------------
# Command parsing
# ---------------------------------------------------------------------------

def parse_cc_command(text: str) -> tuple[str, str]:
    """Parse /cc command text into (task, repo_path).

    Supports:
      /cc "fix the tests"
      /cc fix the tests --repo /path/to/repo
      cc do something

    Returns:
        (task_description, repo_path)
    """
    # Strip leading /cc or cc prefix
    stripped = text.strip()
    for prefix in ("/cc ", "cc "):
        if stripped.lower().startswith(prefix):
            stripped = stripped[len(prefix):].strip()
            break
    else:
        # Handle exact match with no trailing content (e.g. "/cc" or "cc")
        if stripped.lower() in ("/cc", "cc"):
            stripped = ""

    # Extract --repo flag if present
    repo_path = DEFAULT_REPO
    if "--repo " in stripped:
        parts = stripped.split("--repo ", 1)
        stripped = parts[0].strip()
        repo_candidate = parts[1].strip().split()[0] if parts[1].strip() else ""
        if repo_candidate:
            repo_path = repo_candidate

    # Strip surrounding quotes from task
    task = stripped.strip('"').strip("'").strip()
    return task, repo_path


# ---------------------------------------------------------------------------
# Progress message formatting
# ---------------------------------------------------------------------------

def format_progress_message(session: CCSession) -> str:
    """Build a Telegram-friendly progress message."""
    elapsed = int(session.elapsed())
    minutes, seconds = divmod(elapsed, 60)
    time_str = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"

    files_str = ", ".join(session.files_touched[-5:]) if session.files_touched else "none yet"
    if len(session.files_touched) > 5:
        files_str += f" (+{len(session.files_touched) - 5} more)"

    # Last activity line
    last_line = ""
    if session.output_lines:
        last_line = session.output_lines[-1][:80]

    lines = [
        f"<b>CC Remote</b>: {_escape_html(session.task[:60])}",
        f"Status: running",
        f"Files: {_escape_html(files_str)}",
        f"Elapsed: {time_str}",
    ]
    if last_line:
        lines.append(f"Last: {_escape_html(last_line)}")

    return "\n".join(lines)


def format_completion_message(session: CCSession, diff_stat: str) -> str:
    """Build a Telegram-friendly completion message."""
    elapsed = int(session.elapsed())
    minutes, seconds = divmod(elapsed, 60)
    time_str = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"

    status_icon = "done" if session.status == "complete" else "failed"
    summary = session.result_summary[:300] if session.result_summary else "No summary"
    diff_preview = diff_stat[:500] if diff_stat else "No changes"

    lines = [
        f"<b>CC Remote</b>: {status_icon}",
        f"Task: {_escape_html(session.task[:80])}",
        f"Time: {time_str}",
        f"Files: {len(session.files_touched)}",
        "",
        f"<b>Summary</b>: {_escape_html(summary)}",
        "",
        f"<pre>{_escape_html(diff_preview)}</pre>",
    ]
    return "\n".join(lines)


def _escape_html(text: str) -> str:
    """Escape HTML special chars for Telegram HTML parse mode."""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


# ---------------------------------------------------------------------------
# Stream JSON parsing
# ---------------------------------------------------------------------------

def parse_stream_event(line: str) -> Optional[dict]:
    """Parse a single stream-json line from Claude Code.

    Claude Code --output-format stream-json emits JSON objects, one per line.
    Returns parsed dict or None if unparseable.
    """
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def extract_event_info(event: dict) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract (event_type, file_path, text_content) from a stream event.

    Returns:
        (event_type, file_touched_or_none, status_text_or_none)
    """
    event_type = event.get("type")
    file_path = None
    text_content = None

    if event_type == "tool_use":
        tool = event.get("tool", "")
        file_path = event.get("file") or event.get("path")
        # Also check nested input for file references
        tool_input = event.get("input", {})
        if isinstance(tool_input, dict):
            file_path = file_path or tool_input.get("file_path") or tool_input.get("file")
        text_content = f"Using {tool}" + (f" on {Path(file_path).name}" if file_path else "")

    elif event_type == "text":
        text_content = event.get("content", "")[:100]

    elif event_type == "result":
        text_content = event.get("content", "")

    elif event_type == "assistant":
        # Assistant message container, may have content
        content = event.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_content = block.get("text", "")[:100]
                    break
        elif isinstance(content, str):
            text_content = content[:100]

    return event_type, file_path, text_content


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def get_git_diff_stat(repo_path: str) -> str:
    """Run git diff --stat in the repo. Returns the output string."""
    try:
        result = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() or "(no changes)"
    except Exception as exc:
        return f"(error getting diff: {exc})"


def get_git_diff_full(repo_path: str) -> str:
    """Run git diff in the repo. Returns the full diff."""
    try:
        result = subprocess.run(
            ["git", "diff"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout.strip() or "(no changes)"
    except Exception as exc:
        return f"(error getting diff: {exc})"


def git_commit_changes(repo_path: str, message: str) -> tuple[bool, str]:
    """Stage all and commit. Returns (success, output)."""
    try:
        # Stage
        stage = subprocess.run(
            ["git", "add", "-A"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if stage.returncode != 0:
            return False, f"git add failed: {stage.stderr}"

        # Commit (no signing, cc-remote commits are auto-generated)
        commit = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if commit.returncode != 0:
            return False, f"git commit failed: {commit.stderr}"

        return True, commit.stdout.strip()
    except Exception as exc:
        return False, str(exc)


def git_discard_changes(repo_path: str) -> tuple[bool, str]:
    """Discard all unstaged and staged changes. Returns (success, output)."""
    try:
        # Reset staged
        subprocess.run(
            ["git", "reset", "HEAD", "--"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        # Discard working tree
        result = subprocess.run(
            ["git", "checkout", "--", "."],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        # Clean untracked files
        subprocess.run(
            ["git", "clean", "-fd"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return True, "Changes discarded"
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# Core: run Claude Code subprocess
# ---------------------------------------------------------------------------

async def _run_claude(
    session: CCSession,
    send_message: SendMessageFn,
    send_inline_keyboard: SendKeyboardFn,
) -> None:
    """Run claude -p in subprocess, stream progress, send completion.

    Spawns the Claude Code CLI, reads its stream-json stdout line by line,
    sends Telegram progress updates at PROGRESS_INTERVAL_SECONDS intervals,
    and on completion sends the diff summary with approve/reject buttons.
    """
    cmd = [
        "claude",
        "-p", session.task,
        "--output-format", "stream-json",
        "--max-turns", str(DEFAULT_MAX_TURNS),
    ]

    logger.info("CC Remote: spawning claude in %s: %s", session.repo_path, " ".join(cmd))

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=session.repo_path,
        )
        session.pid = proc.pid
        _save_sessions()

    except Exception as exc:
        session.status = "failed"
        session.result_summary = f"Failed to spawn claude: {exc}"
        _save_sessions()
        await send_message(
            chat_id=session.chat_id,
            text=f"CC Remote failed to start: {_escape_html(str(exc))}",
        )
        return

    session.last_progress_at = time.time()

    # Read stdout line by line
    try:
        while True:
            line_bytes = await proc.stdout.readline()
            if not line_bytes:
                break

            line = line_bytes.decode("utf-8", errors="replace")
            event = parse_stream_event(line)
            if not event:
                continue

            event_type, file_path, text_content = extract_event_info(event)

            if file_path and file_path not in session.files_touched:
                session.files_touched.append(file_path)

            if text_content:
                session.output_lines.append(text_content)
                # Keep memory bounded
                if len(session.output_lines) > 200:
                    session.output_lines = session.output_lines[-100:]

            if event_type == "result":
                session.result_summary = text_content or ""

            # Send progress update every PROGRESS_INTERVAL_SECONDS
            now = time.time()
            if now - session.last_progress_at >= PROGRESS_INTERVAL_SECONDS:
                session.last_progress_at = now
                try:
                    progress_text = format_progress_message(session)
                    await send_message(
                        chat_id=session.chat_id,
                        text=progress_text,
                    )
                except Exception as exc:
                    logger.warning("Failed to send progress: %s", exc)

    except Exception as exc:
        logger.error("Error reading claude stdout: %s", exc)

    # Wait for process to finish
    try:
        await asyncio.wait_for(proc.wait(), timeout=30)
    except asyncio.TimeoutError:
        logger.warning("Claude process did not exit in 30s, killing")
        proc.kill()
        await proc.wait()

    exit_code = proc.returncode
    if exit_code == 0:
        session.status = "complete"
    else:
        session.status = "failed"
        # Capture stderr for diagnostics
        stderr_bytes = await proc.stderr.read()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")[:500]
        if not session.result_summary:
            session.result_summary = f"Exit code {exit_code}. {stderr_text}"

    _save_sessions()

    # Send completion message with diff and buttons
    diff_stat = get_git_diff_stat(session.repo_path)
    completion_text = format_completion_message(session, diff_stat)

    # Only show approve/reject if there are actual changes and it succeeded
    has_changes = diff_stat and diff_stat != "(no changes)"
    if has_changes and session.status == "complete":
        try:
            await send_inline_keyboard(
                chat_id=session.chat_id,
                text=completion_text,
                buttons=[
                    [
                        {"text": "Approve", "callback_data": f"cc_approve:{session.session_id}"},
                        {"text": "Reject", "callback_data": f"cc_reject:{session.session_id}"},
                    ],
                    [
                        {"text": "Full Diff", "callback_data": f"cc_diff:{session.session_id}"},
                    ],
                ],
            )
        except Exception as exc:
            logger.error("Failed to send completion keyboard: %s", exc)
            await send_message(chat_id=session.chat_id, text=completion_text)
    else:
        await send_message(chat_id=session.chat_id, text=completion_text)

    # Start approval timeout watcher
    if has_changes and session.status == "complete":
        asyncio.create_task(_approval_timeout_watcher(session, send_message))


async def _approval_timeout_watcher(session: CCSession, send_message: SendMessageFn) -> None:
    """After APPROVAL_TIMEOUT_SECONDS, warn if no action taken."""
    await asyncio.sleep(APPROVAL_TIMEOUT_SECONDS)
    if session.status == "complete":
        session.status = "timeout"
        _save_sessions()
        try:
            await send_message(
                chat_id=session.chat_id,
                text=(
                    f"CC Remote: session {session.session_id[:8]} timed out "
                    f"waiting for approval. Changes are still on disk in "
                    f"{session.repo_path}. Run /cc-status for details."
                ),
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

async def handle_cc_command(
    task: str,
    chat_id: str,
    send_message: SendMessageFn,
    send_inline_keyboard: SendKeyboardFn,
    repo_path: Optional[str] = None,
) -> str:
    """Entry point: /cc 'task description' [--repo path].

    Returns a JSON string with the dispatch result.
    """
    if not task or not task.strip():
        return json.dumps({"error": "No task provided. Usage: /cc \"task description\""})

    # Check concurrent session limit
    running_count = sum(1 for s in _sessions.values() if s.status == "running")
    if running_count >= MAX_CONCURRENT:
        return json.dumps({
            "error": f"Max {MAX_CONCURRENT} concurrent CC sessions. "
                     f"Wait for a running session to finish or use /cc-status."
        })

    session_id = uuid.uuid4().hex[:8]
    session = CCSession(
        session_id=session_id,
        task=task.strip(),
        status="running",
        chat_id=str(chat_id),
        repo_path=repo_path or DEFAULT_REPO,
        started_at=time.time(),
    )
    _sessions[session_id] = session
    _save_sessions()

    # Notify user
    await send_message(
        chat_id=str(chat_id),
        text=(
            f"<b>CC Remote</b>: task dispatched\n"
            f"Session: <code>{session_id}</code>\n"
            f"Repo: {_escape_html(session.repo_path)}\n"
            f"Task: {_escape_html(task[:100])}\n\n"
            f"Progress updates every {PROGRESS_INTERVAL_SECONDS}s."
        ),
    )

    # Spawn the Claude process in background
    asyncio.create_task(_run_claude(session, send_message, send_inline_keyboard))

    return json.dumps({
        "status": "dispatched",
        "session_id": session_id,
        "task": task,
        "repo_path": session.repo_path,
    })


async def handle_approval(
    session_id: str,
    action: str,
    send_message: SendMessageFn,
) -> str:
    """Handle approve/reject/diff callback from Telegram inline keyboard.

    Args:
        session_id: The CC session ID
        action: One of "approve", "reject", "diff"
        send_message: Async callable to send a Telegram message

    Returns:
        JSON string with the result.
    """
    session = _sessions.get(session_id)
    if not session:
        return json.dumps({"error": f"Session {session_id} not found"})

    if action == "approve":
        if session.status not in ("complete", "timeout"):
            return json.dumps({"error": f"Session is {session.status}, cannot approve"})

        commit_msg = f"cc-remote: {session.task[:70]}"
        success, output = git_commit_changes(session.repo_path, commit_msg)
        if success:
            session.status = "approved"
            _save_sessions()
            await send_message(
                chat_id=session.chat_id,
                text=f"CC Remote: changes committed.\n<pre>{_escape_html(output[:500])}</pre>",
            )
            return json.dumps({"status": "approved", "output": output})
        else:
            await send_message(
                chat_id=session.chat_id,
                text=f"CC Remote: commit failed.\n<pre>{_escape_html(output[:500])}</pre>",
            )
            return json.dumps({"error": f"Commit failed: {output}"})

    elif action == "reject":
        if session.status not in ("complete", "timeout"):
            return json.dumps({"error": f"Session is {session.status}, cannot reject"})

        success, output = git_discard_changes(session.repo_path)
        session.status = "rejected"
        _save_sessions()
        await send_message(
            chat_id=session.chat_id,
            text=f"CC Remote: changes discarded. {_escape_html(output[:200])}",
        )
        return json.dumps({"status": "rejected"})

    elif action == "diff":
        full_diff = get_git_diff_full(session.repo_path)
        # Paginate for Telegram's 4096 char limit
        pages = _paginate(full_diff, TELEGRAM_MAX_LENGTH - 100)
        for i, page in enumerate(pages):
            header = f"<b>Diff ({i + 1}/{len(pages)})</b>\n" if len(pages) > 1 else ""
            await send_message(
                chat_id=session.chat_id,
                text=f"{header}<pre>{_escape_html(page)}</pre>",
            )
        return json.dumps({"status": "diff_sent", "pages": len(pages)})

    else:
        return json.dumps({"error": f"Unknown action: {action}"})


def get_active_sessions() -> List[dict]:
    """Return list of all sessions for status display."""
    return [s.to_dict() for s in _sessions.values()]


def get_session(session_id: str) -> Optional[CCSession]:
    """Get a session by ID."""
    return _sessions.get(session_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _paginate(text: str, max_len: int) -> List[str]:
    """Split text into pages of at most max_len characters."""
    if len(text) <= max_len:
        return [text]
    pages = []
    while text:
        pages.append(text[:max_len])
        text = text[max_len:]
    return pages


# ---------------------------------------------------------------------------
# Tool schema and registry
# ---------------------------------------------------------------------------

def check_cc_remote_requirements() -> bool:
    """CC Remote requires the claude CLI to be on PATH."""
    return shutil.which("claude") is not None


CC_REMOTE_SCHEMA = {
    "name": "cc_remote",
    "description": (
        "Dispatch a Claude Code task from Telegram. "
        "Spawns a local `claude` CLI session, streams progress, "
        "and sends approve/reject buttons on completion.\n\n"
        "Usage: /cc \"task description\" [--repo /path/to/repo]\n\n"
        "Also supports:\n"
        "  /cc-status -- show active sessions\n"
        "  Inline button callbacks for approve/reject/diff"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The task to send to Claude Code.",
            },
            "repo_path": {
                "type": "string",
                "description": (
                    "Repository path for Claude to work in. "
                    "Defaults to ~/projects/advanced-parts."
                ),
            },
            "action": {
                "type": "string",
                "enum": ["dispatch", "status", "approve", "reject", "diff"],
                "description": "Action to perform. Default: dispatch.",
            },
            "session_id": {
                "type": "string",
                "description": "Session ID for approve/reject/diff actions.",
            },
        },
        "required": ["task"],
    },
}


# --- Registry ---
from tools.registry import registry

registry.register(
    name="cc_remote",
    toolset="cc_remote",
    schema=CC_REMOTE_SCHEMA,
    handler=lambda args, **kw: json.dumps({
        "info": "cc_remote is dispatched via Telegram /cc command, not via tool call."
    }),
    check_fn=check_cc_remote_requirements,
    is_async=False,
    description="Dispatch Claude Code tasks from Telegram with progress streaming.",
)
