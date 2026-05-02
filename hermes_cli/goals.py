"""Persistent cross-turn goals for Hermes gateway sessions."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

DEFAULT_MAX_TURNS = 20
GOAL_STORE_PATH = get_hermes_home() / "goals.json"
_STORE_LOCK = threading.Lock()


CONTINUATION_PROMPT_TEMPLATE = """[Hermes /goal continuation]
Standing goal:
{goal}

Supervisor note:
{judgment}

Continue working toward the standing goal. Inspect the current state before changing code. Execute the next concrete step, verify it, and stop only when the goal is complete or blocked by an irreversible external action, missing credentials, or required user approval."""


@dataclass
class GoalState:
    session_key: str
    goal: str
    active: bool = True
    paused: bool = False
    status: str = "running"
    turn_count: int = 0
    max_turns: int = DEFAULT_MAX_TURNS
    created_at: float = 0.0
    updated_at: float = 0.0
    last_judgment: str = ""
    last_response_preview: str = ""
    workspace_task_id: str = ""


def _now() -> float:
    return time.time()


def _coerce_max_turns(value: Any) -> int:
    try:
        turns = int(value)
    except (TypeError, ValueError):
        turns = DEFAULT_MAX_TURNS
    return max(1, min(turns, 100))


def _load_store(path: Path = GOAL_STORE_PATH) -> dict[str, dict[str, Any]]:
    with _STORE_LOCK:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as exc:
            logger.warning("Failed to read goal store %s: %s", path, exc)
            return {}
    return data if isinstance(data, dict) else {}


def _save_store(data: dict[str, dict[str, Any]], path: Path = GOAL_STORE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True)
    with _STORE_LOCK:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                tmp.write(payload)
                tmp.write("\n")
            Path(tmp_name).replace(path)
        except Exception:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except Exception:
                pass
            raise


def _state_from_dict(session_key: str, raw: dict[str, Any]) -> GoalState:
    created = float(raw.get("created_at") or _now())
    updated = float(raw.get("updated_at") or created)
    return GoalState(
        session_key=session_key,
        goal=str(raw.get("goal") or "").strip(),
        active=bool(raw.get("active", True)),
        paused=bool(raw.get("paused", False)),
        status=str(raw.get("status") or "running"),
        turn_count=max(0, int(raw.get("turn_count") or 0)),
        max_turns=_coerce_max_turns(raw.get("max_turns")),
        created_at=created,
        updated_at=updated,
        last_judgment=str(raw.get("last_judgment") or ""),
        last_response_preview=str(raw.get("last_response_preview") or ""),
        workspace_task_id=str(raw.get("workspace_task_id") or ""),
    )


def _extract_json_object(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_judgment(text: str) -> tuple[bool, str]:
    parsed = _extract_json_object(text)
    if parsed is not None:
        done = bool(parsed.get("done"))
        reason = str(parsed.get("reason") or parsed.get("judgment") or "").strip()
        return done, reason[:500]

    lowered = (text or "").strip().lower()
    if not lowered:
        return False, "No supervisor judgment returned."
    if re.search(r"\b(done|complete|completed|satisfied|achieved)\b", lowered):
        if not re.search(r"\b(not|incomplete|unfinished|continue|remaining)\b", lowered):
            return True, text.strip()[:500]
    return False, text.strip()[:500]


def _fallback_judgment(last_response: str) -> tuple[bool, str]:
    lowered = (last_response or "").lower()
    completion_markers = (
        "implemented",
        "deployed",
        "verified",
        "tests passed",
        "goal complete",
        "done",
    )
    blocker_markers = (
        "blocked",
        "need approval",
        "requires approval",
        "missing credential",
        "cannot proceed",
    )
    if any(marker in lowered for marker in blocker_markers):
        return True, "Latest response reports a blocker that requires user action."
    if any(marker in lowered for marker in completion_markers):
        return True, "Latest response appears to report completion."
    return False, "No auxiliary judge was available; continuing conservatively."


class GoalManager:
    """File-backed manager for a single gateway session goal."""

    def __init__(
        self,
        session_key: str,
        *,
        default_max_turns: int = DEFAULT_MAX_TURNS,
        store_path: Path = GOAL_STORE_PATH,
    ):
        self.session_key = session_key
        self.default_max_turns = _coerce_max_turns(default_max_turns)
        self.store_path = store_path

    def load(self) -> GoalState | None:
        raw = _load_store(self.store_path).get(self.session_key)
        if not isinstance(raw, dict):
            return None
        state = _state_from_dict(self.session_key, raw)
        return state if state.goal else None

    def save(self, state: GoalState) -> GoalState:
        state.updated_at = _now()
        data = _load_store(self.store_path)
        data[self.session_key] = asdict(state)
        _save_store(data, self.store_path)
        return state

    def clear(self) -> None:
        data = _load_store(self.store_path)
        if self.session_key in data:
            data.pop(self.session_key, None)
            _save_store(data, self.store_path)

    def set_goal(
        self,
        goal: str,
        *,
        max_turns: int | None = None,
        workspace_task_id: str | None = None,
    ) -> GoalState:
        goal = (goal or "").strip()
        if not goal:
            raise ValueError("Goal text is required.")
        now = _now()
        state = GoalState(
            session_key=self.session_key,
            goal=goal,
            max_turns=_coerce_max_turns(max_turns or self.default_max_turns),
            created_at=now,
            updated_at=now,
            workspace_task_id=(workspace_task_id or "").strip(),
        )
        return self.save(state)

    def link_workspace_task(self, workspace_task_id: str) -> GoalState | None:
        state = self.load()
        if not state:
            return None
        state.workspace_task_id = (workspace_task_id or "").strip()
        return self.save(state)

    def pause(self) -> GoalState | None:
        state = self.load()
        if not state:
            return None
        state.paused = True
        state.status = "paused"
        state.active = True
        state.last_judgment = "Paused by user."
        return self.save(state)

    def resume(self) -> GoalState | None:
        state = self.load()
        if not state:
            return None
        state.paused = False
        state.active = True
        state.status = "running"
        state.last_judgment = "Resumed by user."
        return self.save(state)

    def status_message(self) -> str:
        state = self.load()
        if not state:
            return "No active goal for this session. Use `/goal <objective>` to set one."
        status = "paused" if state.paused else state.status
        lines = [
            f"Goal: {state.goal}",
            f"Status: {status}",
            f"Turns: {state.turn_count}/{state.max_turns}",
        ]
        if state.workspace_task_id:
            lines.append(f"Workspace task: {state.workspace_task_id}")
        if state.last_judgment:
            lines.append(f"Last judgment: {state.last_judgment}")
        return "\n".join(lines)

    def continuation_prompt(self, state: GoalState, judgment: str | None = None) -> str:
        return CONTINUATION_PROMPT_TEMPLATE.format(
            goal=state.goal,
            judgment=(judgment or state.last_judgment or "Goal is active."),
        )

    def evaluate_after_turn(self, last_response: str, *, failed: bool = False) -> str | None:
        state = self.load()
        if not state or not state.active or state.paused or state.status != "running":
            return None
        if failed:
            state.paused = True
            state.status = "paused"
            state.last_judgment = "Paused because the last turn failed."
            self.save(state)
            return None

        last_response = (last_response or "").strip()
        if not last_response:
            return None

        state.turn_count += 1
        state.last_response_preview = last_response[:500]

        done, judgment = self._judge_goal(state.goal, last_response)
        state.last_judgment = judgment or ("Goal complete." if done else "Continue.")

        if done:
            state.active = False
            state.paused = False
            state.status = "done"
            self.save(state)
            return None

        if state.turn_count >= state.max_turns:
            state.paused = True
            state.status = "paused"
            state.last_judgment = (
                f"Turn budget exhausted at {state.turn_count}/{state.max_turns}; "
                "paused for user review."
            )
            self.save(state)
            return None

        self.save(state)
        return self.continuation_prompt(state)

    def _judge_goal(self, goal: str, last_response: str) -> tuple[bool, str]:
        prompt = (
            "You are the Hermes /goal supervisor. Decide whether the standing "
            "goal is fully complete after the latest assistant response.\n\n"
            f"Standing goal:\n{goal}\n\n"
            f"Latest assistant response:\n{last_response[:8000]}\n\n"
            "Return strict JSON only: "
            '{"done": true|false, "reason": "one short sentence"}.\n'
            "Mark done=true only when no further Hermes action is useful. "
            "If user approval, credentials, or an irreversible external action "
            "is required, mark done=true and say it is blocked for user action."
        )
        try:
            from agent.auxiliary_client import get_text_auxiliary_client

            client, model = get_text_auxiliary_client()
            if client is None or not model:
                return _fallback_judgment(last_response)
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "timeout": 60.0,
                "max_tokens": 200,
            }
            try:
                response = client.chat.completions.create(**kwargs)
            except Exception as first_err:
                if "max_tokens" in str(first_err) or "unsupported_parameter" in str(first_err):
                    kwargs.pop("max_tokens", None)
                    kwargs["max_completion_tokens"] = 200
                    response = client.chat.completions.create(**kwargs)
                else:
                    raise
            content = response.choices[0].message.content or ""
            return _parse_judgment(content)
        except Exception as exc:
            logger.warning("Goal judge failed for %s: %s", self.session_key[:30], exc)
            return _fallback_judgment(last_response)
