from __future__ import annotations

from hermes_cli.model_routes import missing_required_routes, resolve_model_routes, route_summary


def test_routes_use_local_deepseek_when_no_frontier():
    policy = resolve_model_routes(
        {
            "HERMES_PLANNER_PROVIDER": "custom:office-deepseek-v4",
            "HERMES_PLANNER_MODEL": "deepseek-v4",
            "HERMES_V4_PLANNER_AVAILABLE": "1",
        }
    )

    assert missing_required_routes(policy) == []
    assert policy["frontier_available"] is False
    assert policy["routes"]["planner"]["provider"] == "custom:office-deepseek-v4"
    assert policy["routes"]["executor"]["provider"] == "custom:office-deepseek-v4"
    assert policy["routes"]["hard_task_planner"]["source"] == "planner"


def test_routes_prefer_verified_openai_frontier():
    policy = resolve_model_routes(
        {
            "HERMES_PLANNER_PROVIDER": "custom:office-deepseek-v4",
            "HERMES_PLANNER_MODEL": "deepseek-v4",
            "HERMES_OPENAI_FRONTIER_AVAILABLE": "1",
            "OPENAI_FRONTIER_MODEL": "gpt-5.4-mini",
        }
    )

    assert policy["frontier_available"] is True
    assert policy["frontier_verified"] is True
    assert policy["frontier_source"] == "openai"
    assert policy["routes"]["hard_task_planner"]["provider"] == "custom:openai-frontier"
    assert policy["routes"]["verifier"]["source"] == "frontier"
    assert policy["routes"]["synthesizer"]["source"] == "frontier"


def test_routes_use_gemini_frontier_when_openai_is_unavailable():
    policy = resolve_model_routes(
        {
            "HERMES_PLANNER_PROVIDER": "custom:office-deepseek-v4",
            "HERMES_PLANNER_MODEL": "deepseek-v4",
            "HERMES_GEMINI_FRONTIER_AVAILABLE": "1",
            "HERMES_GEMINI_FRONTIER_MODEL": "gemini-2.5-pro",
        }
    )

    assert policy["frontier_available"] is True
    assert policy["frontier_source"] == "gemini"
    assert policy["routes"]["hard_task_planner"]["provider"] == "gemini"
    assert policy["routes"]["hard_task_planner"]["model"] == "gemini-2.5-pro"


def test_routes_accept_redacted_key_presence_for_gemini():
    policy = resolve_model_routes(
        {
            "HERMES_PLANNER_PROVIDER": "custom:office-deepseek-v4",
            "HERMES_PLANNER_MODEL": "deepseek-v4",
            "GEMINI_API_KEY_PRESENT": "1",
            "GEMINI_FRONTIER_MODEL": "gemini-2.5-pro",
        }
    )

    assert policy["frontier_available"] is True
    assert policy["frontier_verified"] is False
    assert "hard_task_planner=gemini:gemini-2.5-pro" in route_summary(policy)
