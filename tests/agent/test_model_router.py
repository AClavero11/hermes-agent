"""Tests for agent/model_router.py — task-based model routing."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest
import yaml

from agent.model_router import (
    ModelRouter,
    DEFAULT_TASK_MODEL_MAP,
    TASK_CLASSIFICATION_KEYWORDS,
)


# =========================================================================
# Task Classification
# =========================================================================

class TestTaskClassification:
    """Test classify_task() with various message patterns."""

    def test_empty_messages_returns_chat(self):
        router = ModelRouter()
        assert router.classify_task([]) == "chat"

    def test_no_user_message_returns_chat(self):
        router = ModelRouter()
        messages = [
            {"role": "assistant", "content": "Hello!"}
        ]
        assert router.classify_task(messages) == "chat"

    def test_classify_keyword(self):
        router = ModelRouter()
        messages = [
            {"role": "user", "content": "classify this text into categories"}
        ]
        assert router.classify_task(messages) == "classify"

    def test_extract_keyword(self):
        router = ModelRouter()
        messages = [
            {"role": "user", "content": "extract all email addresses from this"}
        ]
        assert router.classify_task(messages) == "extract"

    def test_summarize_keyword(self):
        router = ModelRouter()
        messages = [
            {"role": "user", "content": "give me a summary of this document"}
        ]
        assert router.classify_task(messages) == "summarize"

    def test_code_keyword(self):
        router = ModelRouter()
        messages = [
            {"role": "user", "content": "write a python function to calculate"}
        ]
        assert router.classify_task(messages) == "code"

    def test_plan_keyword(self):
        router = ModelRouter()
        messages = [
            {"role": "user", "content": "what's the best approach to design this system"}
        ]
        assert router.classify_task(messages) == "plan"

    def test_debug_keyword(self):
        router = ModelRouter()
        messages = [
            {"role": "user", "content": "why is my code not working"}
        ]
        assert router.classify_task(messages) == "complex_debug"

    def test_reason_keyword(self):
        router = ModelRouter()
        messages = [
            {"role": "user", "content": "explain why this approach is better"}
        ]
        assert router.classify_task(messages) == "reason"

    def test_case_insensitive(self):
        router = ModelRouter()
        messages = [
            {"role": "user", "content": "CLASSIFY this into categories"}
        ]
        assert router.classify_task(messages) == "classify"

    def test_last_user_message_used(self):
        """Only the last user message should be classified."""
        router = ModelRouter()
        messages = [
            {"role": "user", "content": "classify this"},
            {"role": "assistant", "content": "Classification done"},
            {"role": "user", "content": "write a function"}
        ]
        # Last user message is "write a function" -> should classify as code
        assert router.classify_task(messages) == "code"

    def test_ambiguous_message_defaults_to_chat(self):
        router = ModelRouter()
        messages = [
            {"role": "user", "content": "hello world"}
        ]
        assert router.classify_task(messages) == "chat"

    def test_multiple_keywords_highest_score_wins(self):
        """If multiple keywords match, highest scorer wins."""
        router = ModelRouter()
        # "write" is code, "function" is code, "summarize" is summarize
        # code should win with 2 matches
        messages = [
            {"role": "user", "content": "write a function to summarize this"}
        ]
        result = router.classify_task(messages)
        # "write" + "function" = 2 for code
        # "summarize" = 1 for summarize
        assert result == "code"

    def test_none_content_returns_chat(self):
        router = ModelRouter()
        messages = [
            {"role": "user", "content": None}
        ]
        assert router.classify_task(messages) == "chat"

    def test_empty_content_returns_chat(self):
        router = ModelRouter()
        messages = [
            {"role": "user", "content": ""}
        ]
        assert router.classify_task(messages) == "chat"


# =========================================================================
# Model Routing
# =========================================================================

class TestModelRouting:
    """Test route_model() selects correct model for task type."""

    def test_classify_uses_haiku(self):
        router = ModelRouter()
        model = router.route_model("classify")
        assert "haiku" in model.lower()

    def test_extract_uses_haiku(self):
        router = ModelRouter()
        model = router.route_model("extract")
        assert "haiku" in model.lower()

    def test_summarize_uses_haiku(self):
        router = ModelRouter()
        model = router.route_model("summarize")
        assert "haiku" in model.lower()

    def test_code_uses_sonnet(self):
        router = ModelRouter()
        model = router.route_model("code")
        assert "sonnet" in model.lower()

    def test_reason_uses_sonnet(self):
        router = ModelRouter()
        model = router.route_model("reason")
        assert "sonnet" in model.lower()

    def test_chat_uses_sonnet(self):
        router = ModelRouter()
        model = router.route_model("chat")
        assert "sonnet" in model.lower()

    def test_plan_uses_opus(self):
        router = ModelRouter()
        model = router.route_model("plan")
        assert "opus" in model.lower()

    def test_complex_debug_uses_opus(self):
        router = ModelRouter()
        model = router.route_model("complex_debug")
        assert "opus" in model.lower()

    def test_unknown_task_defaults_to_sonnet(self):
        router = ModelRouter()
        model = router.route_model("unknown_task_type")
        # Unknown task should fall back to 'chat' model (sonnet)
        assert "sonnet" in model.lower()

    def test_returns_string(self):
        router = ModelRouter()
        model = router.route_model("code")
        assert isinstance(model, str)
        assert len(model) > 0


# =========================================================================
# Route Messages Convenience Method
# =========================================================================

class TestRouteMessages:
    """Test route_messages() as end-to-end convenience method."""

    def test_classify_and_route_in_one_call(self):
        router = ModelRouter()
        messages = [
            {"role": "user", "content": "classify this text"}
        ]
        model = router.route_messages(messages)
        assert "haiku" in model.lower()

    def test_complex_and_route_in_one_call(self):
        router = ModelRouter()
        messages = [
            {"role": "user", "content": "design a system architecture"}
        ]
        model = router.route_messages(messages)
        assert "opus" in model.lower()


# =========================================================================
# Configuration Loading
# =========================================================================

class TestConfigLoading:
    """Test loading custom model routing from config.yaml."""

    def test_loads_default_when_no_config(self):
        router = ModelRouter(config_path="/nonexistent/path/config.yaml")
        # Should use defaults
        assert router.task_model_map == DEFAULT_TASK_MODEL_MAP

    def test_loads_custom_mapping_from_config(self):
        """Test that custom model_routing config overrides defaults."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config = {
                "model_routing": {
                    "task_model_map": {
                        "classify": "custom/haiku-model",
                        "code": "custom/opus-model",
                    }
                }
            }
            with open(config_path, "w") as f:
                yaml.dump(config, f)

            router = ModelRouter(config_path=str(config_path))
            assert router.task_model_map["classify"] == "custom/haiku-model"
            assert router.task_model_map["code"] == "custom/opus-model"
            # Other tasks should still have defaults
            assert router.task_model_map["chat"] == DEFAULT_TASK_MODEL_MAP["chat"]

    def test_merges_with_defaults(self):
        """Partial config should merge with defaults."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config = {
                "model_routing": {
                    "task_model_map": {
                        "custom_task": "custom/model",
                    }
                }
            }
            with open(config_path, "w") as f:
                yaml.dump(config, f)

            router = ModelRouter(config_path=str(config_path))
            assert router.task_model_map["custom_task"] == "custom/model"
            # Original defaults should still be there
            assert "classify" in router.task_model_map
            assert router.task_model_map["classify"] == DEFAULT_TASK_MODEL_MAP["classify"]

    def test_handles_missing_model_routing_section(self):
        """Config without model_routing section should use defaults."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            config = {
                "agent": {"max_turns": 50}
            }
            with open(config_path, "w") as f:
                yaml.dump(config, f)

            router = ModelRouter(config_path=str(config_path))
            assert router.task_model_map == DEFAULT_TASK_MODEL_MAP

    def test_handles_invalid_yaml(self):
        """Router should gracefully handle invalid YAML."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yaml"
            with open(config_path, "w") as f:
                f.write("invalid: [yaml: content:")

            # Should not raise, just use defaults
            router = ModelRouter(config_path=str(config_path))
            assert router.task_model_map == DEFAULT_TASK_MODEL_MAP

    def test_uses_hermes_home_env_var(self):
        """Router should check HERMES_HOME env var for config."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hermes_home = Path(tmpdir)
            config_path = hermes_home / "config.yaml"
            config = {
                "model_routing": {
                    "task_model_map": {
                        "classify": "env/haiku",
                    }
                }
            }
            with open(config_path, "w") as f:
                yaml.dump(config, f)

            with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
                router = ModelRouter()
                assert router.task_model_map["classify"] == "env/haiku"


# =========================================================================
# Classification Keywords
# =========================================================================

class TestTaskClassificationKeywords:
    """Verify that TASK_CLASSIFICATION_KEYWORDS is well-formed."""

    def test_all_keys_are_valid_task_types(self):
        """All keys in TASK_CLASSIFICATION_KEYWORDS should map to real tasks."""
        valid_tasks = set(DEFAULT_TASK_MODEL_MAP.keys())
        for task_type in TASK_CLASSIFICATION_KEYWORDS.keys():
            assert task_type in valid_tasks, f"Unknown task type: {task_type}"

    def test_all_keywords_are_strings(self):
        """All keyword lists should contain strings."""
        for task_type, keywords in TASK_CLASSIFICATION_KEYWORDS.items():
            assert isinstance(keywords, list), f"Keywords for {task_type} not a list"
            for kw in keywords:
                assert isinstance(kw, str), f"Non-string keyword in {task_type}: {kw}"

    def test_no_empty_keyword_lists(self):
        """No task type should have an empty keyword list."""
        for task_type, keywords in TASK_CLASSIFICATION_KEYWORDS.items():
            assert len(keywords) > 0, f"No keywords for {task_type}"


# =========================================================================
# Edge Cases
# =========================================================================

class TestEdgeCases:
    """Test edge cases and unusual inputs."""

    def test_very_long_message(self):
        """Router should handle very long messages."""
        router = ModelRouter()
        long_content = "write " + "x" * 10000
        messages = [{"role": "user", "content": long_content}]
        result = router.classify_task(messages)
        assert result == "code"

    def test_message_with_special_characters(self):
        """Router should handle special characters."""
        router = ModelRouter()
        messages = [
            {"role": "user", "content": "write 🚀 code with emoji!"}
        ]
        result = router.classify_task(messages)
        assert result == "code"

    def test_mixed_case_keywords(self):
        """Keywords should be case-insensitive."""
        router = ModelRouter()
        messages = [
            {"role": "user", "content": "cLaSSiFy this text"}
        ]
        result = router.classify_task(messages)
        assert result == "classify"

    def test_whitespace_variations(self):
        """Router should handle various whitespace."""
        router = ModelRouter()
        messages = [
            {"role": "user", "content": "  classify   this  "}
        ]
        result = router.classify_task(messages)
        assert result == "classify"
