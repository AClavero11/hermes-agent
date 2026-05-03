"""Hermes model routing contract.

The route table is intentionally deterministic. Models execute roles; they do
not decide which role they are playing.
"""

from __future__ import annotations

import os
from typing import Any, Mapping


REQUIRED_ROLES = (
    "planner",
    "hard_task_planner",
    "executor",
    "verifier",
    "synthesizer",
    "deterministic_math",
)


def _value(env: Mapping[str, Any], name: str, default: str = "") -> str:
    value = env.get(name)
    if value is None and env is not os.environ:
        value = os.environ.get(name)
    return str(value or default).strip()


def _first_value(env: Mapping[str, Any], names: tuple[str, ...], default: str = "") -> str:
    for name in names:
        value = _value(env, name)
        if value:
            return value
    return default


def _flag(env: Mapping[str, Any], name: str) -> bool:
    return _value(env, name).lower() in {"1", "true", "yes", "on"}


def _key_present(env: Mapping[str, Any], name: str) -> bool:
    redacted_presence = _value(env, f"{name}_PRESENT")
    if redacted_presence:
        return redacted_presence.lower() in {"1", "true", "yes", "on"}
    value = env.get(name)
    if value is None and env is not os.environ:
        value = os.environ.get(name)
    return bool(str(value or "").strip())


def _route(
    role: str,
    provider: str,
    model: str,
    *,
    source: str,
    risk: str,
    notes: str = "",
) -> dict[str, Any]:
    return {
        "role": role,
        "provider": provider,
        "model": model,
        "source": source,
        "risk": risk,
        "available": bool(provider and model),
        "notes": notes,
    }


def _frontier_model(env: Mapping[str, Any], provider: str) -> str:
    if provider == "gemini":
        if not (_flag(env, "HERMES_FRONTIER_AVAILABLE") or _flag(env, "HERMES_GEMINI_FRONTIER_AVAILABLE")):
            return _first_value(
                env,
                ("HERMES_GEMINI_FRONTIER_FALLBACK_MODEL", "GEMINI_FRONTIER_FALLBACK_MODEL"),
                "gemini-2.5-flash",
            )
        return _first_value(
            env,
            ("HERMES_FRONTIER_MODEL", "HERMES_GEMINI_FRONTIER_MODEL", "GEMINI_FRONTIER_MODEL"),
            "gemini-2.5-pro",
        )
    return _first_value(
        env,
        ("HERMES_FRONTIER_MODEL", "OPENAI_FRONTIER_MODEL"),
        "gpt-5.4-mini",
    )


def _frontier_route(env: Mapping[str, Any]) -> tuple[str, str, str, bool]:
    generic_frontier = _flag(env, "HERMES_FRONTIER_AVAILABLE")
    openai_frontier = generic_frontier or _flag(env, "HERMES_OPENAI_FRONTIER_AVAILABLE")
    gemini_frontier = (
        generic_frontier
        or _flag(env, "HERMES_GEMINI_FRONTIER_AVAILABLE")
        or _key_present(env, "GEMINI_API_KEY")
        or _key_present(env, "GOOGLE_API_KEY")
    )
    requested_provider = _value(env, "HERMES_FRONTIER_PROVIDER").lower()

    if requested_provider in {"gemini", "google"} and gemini_frontier:
        return "gemini", _frontier_model(env, "gemini"), "gemini", True
    if requested_provider in {"openai", "custom:openai-frontier"} and openai_frontier:
        return "custom:openai-frontier", _frontier_model(env, "openai"), "openai", True
    if openai_frontier:
        return "custom:openai-frontier", _frontier_model(env, "openai"), "openai", True
    if gemini_frontier:
        return "gemini", _frontier_model(env, "gemini"), "gemini", True
    return "", "", "", False


def resolve_model_routes(env: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Resolve planner/executor/verifier/synthesizer routes from environment."""
    env = env or os.environ

    planner_provider = _first_value(
        env,
        ("HERMES_PLANNER_PROVIDER", "HERMES_INFERENCE_PROVIDER"),
    )
    planner_model = _first_value(
        env,
        ("HERMES_PLANNER_MODEL", "DEEPSEEK_V4_MODEL", "DEEPSEEK_LOCAL_MODEL"),
    )

    executor_provider = _value(env, "HERMES_EXECUTOR_PROVIDER")
    executor_model = _value(env, "HERMES_EXECUTOR_MODEL")
    if not executor_provider:
        if _flag(env, "HERMES_V4_PLANNER_AVAILABLE") or planner_provider == "custom:office-deepseek-v4":
            executor_provider = "custom:office-deepseek-v4"
        else:
            executor_provider = planner_provider or "custom:office-deepseek"
    if not executor_model:
        executor_model = _first_value(
            env,
            ("DEEPSEEK_V4_MODEL", "DEEPSEEK_LOCAL_MODEL"),
            planner_model,
        )

    frontier_provider, frontier_model, frontier_source, frontier_available = _frontier_route(env)
    frontier_verified = bool(
        _flag(env, "HERMES_FRONTIER_AVAILABLE")
        or _flag(env, "HERMES_OPENAI_FRONTIER_AVAILABLE")
        or _flag(env, "HERMES_GEMINI_FRONTIER_AVAILABLE")
    )

    judge_provider = _value(env, "HERMES_JUDGE_PROVIDER")
    judge_model = _value(env, "HERMES_JUDGE_MODEL")
    if not judge_provider and frontier_available:
        judge_provider = frontier_provider
        judge_model = frontier_model
    if not judge_provider:
        judge_provider = planner_provider
        judge_model = planner_model

    synthesizer_provider = _value(env, "HERMES_SYNTHESIZER_PROVIDER")
    synthesizer_model = _value(env, "HERMES_SYNTHESIZER_MODEL")
    if not synthesizer_provider and frontier_available:
        synthesizer_provider = frontier_provider
        synthesizer_model = frontier_model
    if not synthesizer_provider:
        synthesizer_provider = planner_provider
        synthesizer_model = planner_model

    hard_planner_provider = frontier_provider if frontier_available else planner_provider
    hard_planner_model = frontier_model if frontier_available else planner_model
    hard_planner_source = "frontier" if frontier_available else "planner"

    return {
        "frontier_available": frontier_available,
        "frontier_verified": frontier_verified,
        "frontier_source": frontier_source,
        "required_roles": list(REQUIRED_ROLES),
        "routes": {
            "planner": _route(
                "planner",
                planner_provider,
                planner_model,
                source="env",
                risk="default_planning",
            ),
            "hard_task_planner": _route(
                "hard_task_planner",
                hard_planner_provider,
                hard_planner_model,
                source=hard_planner_source,
                risk="ambiguous_or_high_risk",
                notes=(
                    "Use for architecture, root-cause, RFQ/quote, finance, and "
                    "customer-facing decisions."
                ),
            ),
            "executor": _route(
                "executor",
                executor_provider,
                executor_model,
                source="local_executor",
                risk="bounded_tools_and_drafts",
                notes="Use for retrieval, formatting, file ops, tool calls, and bounded draft work.",
            ),
            "verifier": _route(
                "verifier",
                judge_provider,
                judge_model,
                source="frontier" if frontier_available and judge_provider == frontier_provider else "planner",
                risk="judge_and_guardrail",
                notes="Use after deterministic validators for ambiguity, citations, and final risk checks.",
            ),
            "synthesizer": _route(
                "synthesizer",
                synthesizer_provider,
                synthesizer_model,
                source="frontier" if frontier_available and synthesizer_provider == frontier_provider else "planner",
                risk="operator_final_answer",
                notes="Use verified evidence; never expose raw executor output as final.",
            ),
            "deterministic_math": _route(
                "deterministic_math",
                "python",
                "business-rule-verifier",
                source="deterministic",
                risk="quote_math",
                notes="Quote totals, discounts, margins, and freight must be recomputed outside the model.",
            ),
        },
    }


def route_label(route: Mapping[str, Any]) -> str:
    provider = str(route.get("provider") or "missing")
    model = str(route.get("model") or "missing")
    return f"{provider}:{model}"


def missing_required_routes(policy: Mapping[str, Any]) -> list[str]:
    routes = policy.get("routes")
    if not isinstance(routes, Mapping):
        return list(REQUIRED_ROLES)
    missing: list[str] = []
    for role in REQUIRED_ROLES:
        route = routes.get(role)
        if not isinstance(route, Mapping) or not route.get("available"):
            missing.append(role)
    return missing


def route_summary(policy: Mapping[str, Any]) -> str:
    routes = policy.get("routes")
    if not isinstance(routes, Mapping):
        return "model routes unresolved"
    return ", ".join(
        f"{role}={route_label(routes.get(role, {}))}"
        for role in ("planner", "hard_task_planner", "executor", "verifier", "synthesizer")
    )


def format_route_contract(env: Mapping[str, Any] | None = None) -> str:
    policy = resolve_model_routes(env)
    routes = policy["routes"]
    lines = [
        "Resolved model routes:",
        f"- planner: `{route_label(routes['planner'])}`",
        f"- hard_task_planner: `{route_label(routes['hard_task_planner'])}`",
        f"- executor: `{route_label(routes['executor'])}`",
        f"- verifier: `{route_label(routes['verifier'])}`",
        f"- synthesizer: `{route_label(routes['synthesizer'])}`",
        "- deterministic_math: `python:business-rule-verifier`",
    ]
    frontier_state = "available" if policy["frontier_available"] else "not configured"
    if policy["frontier_available"] and not policy["frontier_verified"]:
        frontier_state = "configured, unverified"
    lines.append(f"- frontier: {frontier_state} source={policy.get('frontier_source') or 'none'}")
    return "\n".join(lines)
