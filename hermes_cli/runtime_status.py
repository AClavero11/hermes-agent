"""Runtime resolver for Hermes local and Studio deployments."""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from hermes_cli.model_routes import resolve_model_routes, route_label


PROJECT_ROOT = Path(__file__).resolve().parents[1]

ENV_SNAPSHOT_KEYS = [
    "AAC_HERMES_DEEPSEEK_REPO",
    "AAC_HERMES_DEEPSEEK_PYTHON",
    "AAC_HERMES_DEEPSEEK_HOME",
    "HERMES_HOME",
    "HERMES_PLANNER_ENV_FILE",
    "HERMES_PLANNER_PROVIDER",
    "HERMES_PLANNER_MODEL",
    "HERMES_EXECUTOR_PROVIDER",
    "HERMES_EXECUTOR_MODEL",
    "HERMES_JUDGE_PROVIDER",
    "HERMES_JUDGE_MODEL",
    "HERMES_SYNTHESIZER_PROVIDER",
    "HERMES_SYNTHESIZER_MODEL",
    "HERMES_FRONTIER_PROVIDER",
    "HERMES_FRONTIER_MODEL",
    "HERMES_FRONTIER_AVAILABLE",
    "HERMES_INFERENCE_PROVIDER",
    "OPENROUTER_BASE_URL",
    "HERMES_OPENROUTER_FRONTIER_AVAILABLE",
    "HERMES_OPENROUTER_FRONTIER_MODEL",
    "HERMES_OPENROUTER_PLANNER_MODEL",
    "DEEPSEEK_LOCAL_BASE_URL",
    "DEEPSEEK_LOCAL_MODEL",
    "DEEPSEEK_V4_BASE_URL",
    "DEEPSEEK_V4_MODEL",
    "HERMES_OPENAI_FRONTIER_AVAILABLE",
    "HERMES_GEMINI_FRONTIER_AVAILABLE",
    "HERMES_V4_PLANNER_AVAILABLE",
    "OPENAI_FRONTIER_MODEL",
    "HERMES_FRONTIER_BASE_URL",
    "HERMES_GEMINI_FRONTIER_MODEL",
    "HERMES_GEMINI_FRONTIER_FALLBACK_MODEL",
    "GEMINI_FRONTIER_MODEL",
    "GEMINI_FRONTIER_FALLBACK_MODEL",
    "OPENAI_API_KEY_PRESENT",
    "OPENROUTER_API_KEY_PRESENT",
    "GEMINI_API_KEY_PRESENT",
    "GOOGLE_API_KEY_PRESENT",
    "HERMES_SERVICE_KEY_PRESENT",
]

SECRET_PRESENCE_KEYS = {
    "OPENROUTER_API_KEY": "OPENROUTER_API_KEY_PRESENT",
    "OPENAI_API_KEY": "OPENAI_API_KEY_PRESENT",
    "GEMINI_API_KEY": "GEMINI_API_KEY_PRESENT",
    "GOOGLE_API_KEY": "GOOGLE_API_KEY_PRESENT",
    "HERMES_SERVICE_KEY": "HERMES_SERVICE_KEY_PRESENT",
}


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=max(timeout, 1.0),
            check=False,
        )
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "returncode": 127,
            "stdout": "",
            "stderr": str(exc),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            "stderr": f"timeout after {timeout:.1f}s",
        }
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
    }


def _default_hermes_home() -> Path:
    for name in ("HERMES_HOME", "AAC_HERMES_DEEPSEEK_HOME"):
        value = os.getenv(name, "").strip()
        if value:
            return Path(value).expanduser()
    try:
        from hermes_cli.config import get_hermes_home

        configured_home = get_hermes_home()
    except Exception:
        configured_home = Path.home() / ".hermes"
    configured_home = Path(configured_home).expanduser()
    deepseek_home = Path.home() / ".hermes-deepseek"
    if configured_home == (Path.home() / ".hermes") and (deepseek_home / "bin" / "hermes-env.sh").is_file():
        return deepseek_home
    return configured_home


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    deduped: list[Path] = []
    for path in paths:
        key = str(path.expanduser())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path.expanduser())
    return deduped


def _wrapper_candidates(
    *,
    hermes_home: Path,
    env_wrapper: Path | None,
) -> list[Path]:
    candidates: list[Path] = []
    if env_wrapper:
        candidates.append(env_wrapper)
    for value in (
        os.getenv("HERMES_RUNTIME_ENV_WRAPPER", ""),
        os.getenv("HERMES_CANARY_ENV_WRAPPER", ""),
    ):
        if value.strip():
            candidates.append(Path(value.strip()))
    candidates.extend(
        [
            hermes_home / "bin" / "hermes-env.sh",
            Path.home() / ".hermes-deepseek" / "bin" / "hermes-env.sh",
            Path.home() / ".hermes" / "bin" / "hermes-env.sh",
        ]
    )
    return _dedupe_paths(candidates)


def _first_existing(paths: list[Path]) -> Path | None:
    for path in paths:
        if path.is_file():
            return path
    return None


def _home_from_env_wrapper_path(path: Path | None) -> Path | None:
    if not path:
        return None
    expanded = Path(path).expanduser()
    if expanded.name == "hermes-env.sh" and expanded.parent.name == "bin":
        return expanded.parent.parent
    return None


def _source_wrapper_snapshot(path: Path, *, timeout: float) -> dict[str, Any]:
    py = (
        "import json, os\n"
        f"keys = {ENV_SNAPSHOT_KEYS!r}\n"
        f"presence = {SECRET_PRESENCE_KEYS!r}\n"
        "data = {key: os.environ.get(key, '') for key in keys}\n"
        "for env_name, snapshot_name in presence.items():\n"
        "    data[snapshot_name] = '1' if os.environ.get(env_name, '').strip() else '0'\n"
        "print(json.dumps(data))\n"
    )
    script = "\n".join(
        [
            "set +u",
            f"source {shlex.quote(str(path))}",
            "if typeset -f aac_configure_hermes_deepseek_env >/dev/null; then",
            "  aac_configure_hermes_deepseek_env >/dev/null",
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
        return {
            "ok": False,
            "path": str(path),
            "error": stderr or f"wrapper exited {proc.returncode}",
        }
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return {"ok": False, "path": str(path), "error": "wrapper produced no JSON"}
    try:
        data = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "path": str(path),
            "error": f"wrapper JSON parse failed: {exc}",
            "stdout_tail": lines[-3:],
        }
    if not isinstance(data, dict):
        return {"ok": False, "path": str(path), "error": "wrapper snapshot was not an object"}
    data = {key: str(value or "") for key, value in data.items()}
    data["ok"] = True
    data["path"] = str(path)
    if stderr:
        data["stderr_tail"] = stderr[-1000:]
    return data


def _env_snapshot(
    *,
    env_wrapper: Path | None,
    hermes_home: Path,
    timeout: float,
) -> tuple[dict[str, Any], list[Path], Path | None]:
    candidates = _wrapper_candidates(hermes_home=hermes_home, env_wrapper=env_wrapper)
    wrapper_path = _first_existing(candidates)
    if wrapper_path:
        return _source_wrapper_snapshot(wrapper_path, timeout=timeout), candidates, wrapper_path
    return (
        {
            "ok": False,
            "error": "no env wrapper found",
            **{key: os.getenv(key, "") for key in ENV_SNAPSHOT_KEYS},
            **{
                snapshot_name: "1" if os.getenv(env_name, "").strip() else "0"
                for env_name, snapshot_name in SECRET_PRESENCE_KEYS.items()
            },
        },
        candidates,
        None,
    )


def _git_info(repo: Path | None, *, timeout: float) -> dict[str, Any]:
    if not repo:
        return {"ok": False, "error": "no repo path"}
    repo = repo.expanduser()
    if not repo.exists():
        return {"ok": False, "path": str(repo), "error": "repo path does not exist"}
    root = _run(["git", "-C", str(repo), "rev-parse", "--show-toplevel"], timeout=timeout)
    if not root["ok"]:
        deployed = _deployed_version_info(repo)
        if deployed.get("ok"):
            return deployed
        return {
            "ok": False,
            "path": str(repo),
            "error": root["stderr"] or root["stdout"] or "not a git repository",
        }
    root_path = Path(str(root["stdout"]))
    sha = _run(["git", "-C", str(root_path), "rev-parse", "HEAD"], timeout=timeout)
    short_sha = _run(
        ["git", "-C", str(root_path), "rev-parse", "--short=12", "HEAD"],
        timeout=timeout,
    )
    branch = _run(
        ["git", "-C", str(root_path), "rev-parse", "--abbrev-ref", "HEAD"],
        timeout=timeout,
    )
    status = _run(
        ["git", "-C", str(root_path), "status", "--porcelain"],
        timeout=timeout,
    )
    dirty_lines = [line for line in str(status.get("stdout", "")).splitlines() if line.strip()]
    return {
        "ok": bool(sha["ok"] and short_sha["ok"]),
        "path": str(root_path),
        "sha": sha["stdout"] if sha["ok"] else "",
        "short_sha": short_sha["stdout"] if short_sha["ok"] else "",
        "branch": branch["stdout"] if branch["ok"] else "",
        "dirty": bool(dirty_lines),
        "dirty_count": len(dirty_lines),
    }


def _deployed_version_info(repo: Path) -> dict[str, Any]:
    metadata_path = repo / ".hermes-runtime-version.json"
    if not metadata_path.is_file():
        return {"ok": False}
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "path": str(repo),
            "metadata_path": str(metadata_path),
            "error": str(exc),
        }
    if not isinstance(data, dict):
        return {
            "ok": False,
            "path": str(repo),
            "metadata_path": str(metadata_path),
            "error": "runtime metadata is not an object",
        }
    sha = str(data.get("sha") or "")
    return {
        "ok": bool(sha),
        "path": str(repo),
        "sha": sha,
        "short_sha": str(data.get("short_sha") or sha[:12]),
        "branch": str(data.get("branch") or ""),
        "dirty": bool(data.get("dirty", False)),
        "dirty_count": int(data.get("dirty_count") or 0),
        "source": "deploy_metadata",
        "metadata_path": str(metadata_path),
        "deployed_at": str(data.get("deployed_at") or ""),
        "manifest": str(data.get("manifest") or ""),
    }


def _parse_launchd_value(output: str, names: tuple[str, ...]) -> str:
    for name in names:
        patterns = [
            rf"{re.escape(name)}\s*=\s*\"?([^\"\n]+)\"?",
            rf"\"{re.escape(name)}\"\s*=>\s*\"?([^\"\n]+)\"?",
        ]
        for pattern in patterns:
            match = re.search(pattern, output)
            if match:
                return match.group(1).strip()
    return ""


def _parse_launchd_environment(output: str) -> dict[str, str]:
    env: dict[str, str] = {}
    in_block = False
    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped == "environment = {":
            in_block = True
            continue
        if in_block and stripped == "}":
            break
        if not in_block or "=>" not in stripped:
            continue
        key, value = stripped.split("=>", 1)
        env[key.strip()] = value.strip().strip('"')
    return env


def _launchd_snapshot(label: str, *, timeout: float) -> dict[str, Any]:
    target = f"gui/{os.getuid()}/{label}"
    result = _run(["launchctl", "print", target], timeout=timeout)
    output = "\n".join(part for part in (result["stdout"], result["stderr"]) if part)
    cwd = _parse_launchd_value(
        output,
        ("working directory", "WorkingDirectory", "cwd", "CWD"),
    )
    pid = _parse_launchd_value(output, ("pid", "PID"))
    state = _parse_launchd_value(output, ("state", "State"))
    snapshot = {
        "label": label,
        "target": target,
        "ok": bool(result["ok"]),
        "pid": pid,
        "state": state,
        "working_directory": cwd,
        "environment": _parse_launchd_environment(output),
    }
    if not result["ok"]:
        snapshot["error"] = (result["stderr"] or result["stdout"] or "launchctl unavailable")[-1000:]
    return snapshot


def _env_file_has_secret(path_value: str, secret_name: str) -> bool:
    path_value = str(path_value or "").strip()
    if not path_value:
        return False
    path = Path(path_value).expanduser()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    pattern = re.compile(rf"^(?:export\s+)?{re.escape(secret_name)}\s*=\s*(.*)$")
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = pattern.match(stripped)
        if not match:
            continue
        value = match.group(1).strip().strip('"').strip("'")
        return bool(value)
    return False


def _normalize_health_url(health_url: str | None) -> str:
    value = (
        health_url
        or os.getenv("HERMES_RUNTIME_HEALTH_URL")
        or os.getenv("HERMES_CANARY_GATEWAY_URL")
        or "http://127.0.0.1:8643"
    ).strip()
    if not value:
        return ""
    parsed_path = value.split("://", 1)[-1].split("/", 1)
    if len(parsed_path) == 1:
        return value.rstrip("/") + "/health"
    if value.rstrip("/").endswith("/health"):
        return value.rstrip("/")
    return value.rstrip("/") + "/health"


def _health_probe(url: str, *, timeout: float) -> dict[str, Any]:
    if not url:
        return {"ok": False, "url": "", "error": "no health URL configured"}
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=max(timeout, 1.0)) as resp:
            raw = resp.read(1024 * 1024).decode("utf-8", errors="replace")
            duration_ms = round((time.perf_counter() - started) * 1000.0, 1)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = None
            ok = int(resp.status) == 200 and isinstance(data, dict) and (
                data.get("ok") is True or data.get("status") == "ok"
            )
            return {
                "ok": ok,
                "url": url,
                "status": int(resp.status),
                "duration_ms": duration_ms,
                "response": data,
                "raw_preview": raw[:500] if data is None else "",
            }
    except urllib.error.HTTPError as exc:
        body = exc.read(1024).decode("utf-8", errors="replace")
        return {
            "ok": False,
            "url": url,
            "status": int(exc.code),
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 1),
            "error": body[:500] or str(exc),
        }
    except Exception as exc:
        return {
            "ok": False,
            "url": url,
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 1),
            "error": f"{type(exc).__name__}: {exc}",
        }


def _state_candidates(hermes_home: Path) -> list[Path]:
    candidates: list[Path] = []
    if os.getenv("HERMES_GATEWAY_STATE_PATH", "").strip():
        candidates.append(Path(os.getenv("HERMES_GATEWAY_STATE_PATH", "")))
    candidates.extend(
        [
            hermes_home / "gateway_state.json",
            Path.home() / ".hermes-deepseek" / "gateway_state.json",
            Path.home() / ".hermes" / "gateway_state.json",
        ]
    )
    return _dedupe_paths(candidates)


def _gateway_state_snapshot(hermes_home: Path) -> dict[str, Any]:
    for path in _state_candidates(hermes_home):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"ok": False, "path": str(path), "error": str(exc)}
        if not isinstance(data, dict):
            return {"ok": False, "path": str(path), "error": "state file is not an object"}
        active_agents = data.get("active_agents")
        active_agent_ids: list[str] = []
        if isinstance(active_agents, dict):
            active_agent_ids = [str(key) for key in active_agents.keys()]
        elif isinstance(active_agents, list):
            active_agent_ids = [str(item) for item in active_agents]
        platforms = data.get("platforms")
        platform_names: list[str] = []
        if isinstance(platforms, dict):
            platform_names = [str(key) for key in platforms.keys()]
        elif isinstance(platforms, list):
            platform_names = [str(item) for item in platforms]
        stat = path.stat()
        return {
            "ok": True,
            "path": str(path),
            "mtime": stat.st_mtime,
            "pid": data.get("pid", ""),
            "active_agents_count": len(active_agent_ids),
            "active_agent_ids": active_agent_ids[:10],
            "platforms": platform_names,
        }
    return {"ok": False, "error": "no gateway state file found"}


def _goal_snapshot(hermes_home: Path) -> dict[str, Any]:
    path = hermes_home / "goals.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"ok": True, "path": str(path), "active_count": 0}
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "path": str(path), "error": str(exc)}
    if not isinstance(data, dict):
        return {"ok": False, "path": str(path), "error": "goal store is not an object"}
    active = [
        item for item in data.values()
        if isinstance(item, dict)
        and item.get("active", True)
        and not item.get("paused", False)
        and str(item.get("status") or "running") == "running"
    ]
    return {
        "ok": True,
        "path": str(path),
        "active_count": len(active),
        "total_count": len(data),
    }


def _workspace_snapshot(hermes_home: Path) -> dict[str, Any]:
    path = Path(os.getenv("HERMES_WORKSPACE_STORE", "") or hermes_home / "workspace" / "control_plane.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"ok": True, "path": str(path), "task_count": 0, "active_task_count": 0}
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "path": str(path), "error": str(exc)}
    if not isinstance(data, dict):
        return {"ok": False, "path": str(path), "error": "workspace store is not an object"}
    tasks = data.get("tasks", {})
    task_values = list(tasks.values()) if isinstance(tasks, dict) else []
    active = [
        task for task in task_values
        if isinstance(task, dict)
        and str(task.get("status") or "open") in {"open", "pending", "in_progress", "blocked"}
    ]
    return {
        "ok": True,
        "path": str(path),
        "task_count": len(task_values),
        "active_task_count": len(active),
    }


def collect_runtime_status(
    *,
    env_wrapper: Path | None = None,
    health_url: str | None = None,
    launchd_label: str | None = None,
    repo_root: Path | None = None,
    hermes_home: Path | None = None,
    timeout: float = 4.0,
) -> dict[str, Any]:
    explicit_hermes_home = hermes_home is not None
    hermes_home = (hermes_home or _default_hermes_home()).expanduser()
    repo_root = (repo_root or PROJECT_ROOT).expanduser()
    env, candidates, wrapper_path = _env_snapshot(
        env_wrapper=env_wrapper,
        hermes_home=hermes_home,
        timeout=timeout,
    )
    wrapper_home = _home_from_env_wrapper_path(wrapper_path)
    runtime_home = env.get("HERMES_HOME") or env.get("AAC_HERMES_DEEPSEEK_HOME")
    default_home = Path.home() / ".hermes"
    if wrapper_home and (not explicit_hermes_home or hermes_home == default_home):
        hermes_home = wrapper_home
    elif runtime_home and not explicit_hermes_home:
        hermes_home = Path(str(runtime_home)).expanduser()
    selected_repo = (
        env.get("AAC_HERMES_DEEPSEEK_REPO")
        or os.getenv("AAC_HERMES_DEEPSEEK_REPO", "")
        or str(repo_root)
    )
    selected_python = (
        env.get("AAC_HERMES_DEEPSEEK_PYTHON")
        or os.getenv("AAC_HERMES_DEEPSEEK_PYTHON", "")
        or sys.executable
    )
    model_provider = (
        env.get("HERMES_PLANNER_PROVIDER")
        or env.get("HERMES_INFERENCE_PROVIDER")
        or os.getenv("HERMES_PLANNER_PROVIDER", "")
        or os.getenv("HERMES_INFERENCE_PROVIDER", "")
    )
    model_name = (
        env.get("HERMES_PLANNER_MODEL")
        or env.get("DEEPSEEK_V4_MODEL")
        or env.get("DEEPSEEK_LOCAL_MODEL")
        or os.getenv("HERMES_PLANNER_MODEL", "")
    )
    model_routes = resolve_model_routes(env)
    normalized_health_url = _normalize_health_url(health_url)
    runtime_repo = Path(str(selected_repo)).expanduser() if selected_repo else repo_root
    label = (
        launchd_label
        or os.getenv("HERMES_RUNTIME_LAUNCHD_LABEL")
        or "ai.hermes.deepseek-gateway"
    )
    launchd = _launchd_snapshot(label, timeout=timeout)
    return {
        "collected_at": time.time(),
        "host": platform.node(),
        "hermes_home": str(hermes_home),
        "repo_root": str(repo_root),
        "launchd": launchd,
        "wrapper": {
            "path": str(wrapper_path) if wrapper_path else "",
            "exists": bool(wrapper_path),
            "candidate_paths": [str(path) for path in candidates],
            "selected_repo": str(runtime_repo),
            "snapshot_ok": bool(env.get("ok")),
            "snapshot_error": env.get("error", ""),
        },
        "python": {
            "path": str(selected_python),
            "current_executable": sys.executable,
        },
        "model": {
            "provider": str(model_provider or ""),
            "name": str(model_name or ""),
            "inference_provider": str(env.get("HERMES_INFERENCE_PROVIDER", "")),
            "planner_provider": str(env.get("HERMES_PLANNER_PROVIDER", "")),
            "planner_model": str(env.get("HERMES_PLANNER_MODEL", "")),
            "executor_provider": str(env.get("HERMES_EXECUTOR_PROVIDER", "")),
            "executor_model": str(env.get("HERMES_EXECUTOR_MODEL", "")),
            "judge_provider": str(env.get("HERMES_JUDGE_PROVIDER", "")),
            "judge_model": str(env.get("HERMES_JUDGE_MODEL", "")),
            "synthesizer_provider": str(env.get("HERMES_SYNTHESIZER_PROVIDER", "")),
            "synthesizer_model": str(env.get("HERMES_SYNTHESIZER_MODEL", "")),
            "routing": model_routes,
        },
        "health": _health_probe(normalized_health_url, timeout=timeout),
        "git": {
            "repo_root": _git_info(repo_root, timeout=timeout),
            "runtime_repo": _git_info(runtime_repo, timeout=timeout),
        },
        "state": _gateway_state_snapshot(hermes_home),
        "goals": _goal_snapshot(hermes_home),
        "workspace": _workspace_snapshot(hermes_home),
    }


def _status_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def format_runtime_status(status: dict[str, Any]) -> str:
    launchd = status.get("launchd", {})
    wrapper = status.get("wrapper", {})
    python = status.get("python", {})
    model = status.get("model", {})
    health = status.get("health", {})
    git = status.get("git", {})
    repo_git = git.get("repo_root", {}) if isinstance(git, dict) else {}
    runtime_git = git.get("runtime_repo", {}) if isinstance(git, dict) else {}
    state = status.get("state", {})
    goals = status.get("goals", {})
    workspace = status.get("workspace", {})
    route = " -> ".join(
        part
        for part in (
            _status_value(model.get("provider")),
            _status_value(model.get("name")),
        )
        if part
    ) or "unresolved"
    routing = model.get("routing", {})
    routes = routing.get("routes", {}) if isinstance(routing, dict) else {}
    planner_route = routes.get("planner", {}) if isinstance(routes, dict) else {}
    hard_route = routes.get("hard_task_planner", {}) if isinstance(routes, dict) else {}
    executor_route = routes.get("executor", {}) if isinstance(routes, dict) else {}
    verifier_route = routes.get("verifier", {}) if isinstance(routes, dict) else {}
    synthesizer_route = routes.get("synthesizer", {}) if isinstance(routes, dict) else {}
    health_state = "ok" if health.get("ok") else "fail"
    lines = [
        "Hermes runtime status",
        f"host: {status.get('host', '')}",
        f"launchd: {launchd.get('label', '')} pid={launchd.get('pid', '') or 'n/a'} state={launchd.get('state', '') or 'unknown'}",
        f"launchd cwd: {launchd.get('working_directory', '') or 'unknown'}",
        f"wrapper: {wrapper.get('path', '') or 'not found'}",
        f"wrapper-selected repo: {wrapper.get('selected_repo', '')}",
        f"python path: {python.get('path', '')}",
        f"model route: {route}",
        f"planner: {route_label(planner_route)}",
        f"hard-task planner: {route_label(hard_route)}",
        f"executor: {route_label(executor_route)}",
        f"verifier: {route_label(verifier_route)}",
        f"synthesizer: {route_label(synthesizer_route)}",
        f"frontier: {'available' if routing.get('frontier_available') else 'not configured'} source={routing.get('frontier_source') or 'none'} verified={bool(routing.get('frontier_verified'))}",
        f"health URL: {health.get('url', '')}",
        f"health: {health_state} status={health.get('status', '') or 'n/a'} latency_ms={health.get('duration_ms', '') or 'n/a'}",
        f"repo git: {repo_git.get('short_sha', '') or 'unknown'} branch={repo_git.get('branch', '') or 'unknown'} dirty={repo_git.get('dirty', '')}",
        f"runtime git: {runtime_git.get('short_sha', '') or 'unknown'} branch={runtime_git.get('branch', '') or 'unknown'} dirty={runtime_git.get('dirty', '')}",
        f"gateway state: {state.get('path', '') or 'not found'} active_agents={state.get('active_agents_count', 0)}",
        f"goals: active={goals.get('active_count', 0)} total={goals.get('total_count', 0)}",
        f"workspace: active_tasks={workspace.get('active_task_count', 0)} total_tasks={workspace.get('task_count', 0)}",
    ]
    if launchd.get("error"):
        lines.append(f"launchd error: {launchd['error']}")
    if wrapper.get("snapshot_error"):
        lines.append(f"wrapper error: {wrapper['snapshot_error']}")
    if health.get("error"):
        lines.append(f"health error: {health['error']}")
    return "\n".join(lines) + "\n"


def cmd_runtime(args: Any) -> None:
    runtime_command = getattr(args, "runtime_command", None) or "status"
    if runtime_command != "status":
        raise SystemExit(f"Unsupported runtime command: {runtime_command}")
    status = collect_runtime_status(
        env_wrapper=getattr(args, "env_wrapper", None),
        health_url=getattr(args, "health_url", None),
        launchd_label=getattr(args, "launchd_label", None),
        repo_root=getattr(args, "repo_root", None),
        hermes_home=getattr(args, "hermes_home", None),
        timeout=float(getattr(args, "timeout", 4.0) or 4.0),
    )
    if getattr(args, "json_output", False):
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        print(format_runtime_status(status), end="")
