"""Tests for agent/context_compressor.py — compression logic, thresholds, truncation fallback."""

import pytest
from unittest.mock import patch, MagicMock

from agent.context_compressor import ContextCompressor


@pytest.fixture()
def compressor():
    """Create a ContextCompressor with mocked dependencies."""
    with patch("agent.context_compressor.get_model_context_length", return_value=100000), \
         patch("agent.context_compressor.get_text_auxiliary_client", return_value=(None, None)):
        c = ContextCompressor(
            model="test/model",
            threshold_percent=0.85,
            protect_first_n=2,
            protect_last_n=2,
            quiet_mode=True,
        )
        return c


class TestShouldCompress:
    def test_below_threshold(self, compressor):
        compressor.last_prompt_tokens = 50000
        assert compressor.should_compress() is False

    def test_above_threshold(self, compressor):
        compressor.last_prompt_tokens = 90000
        assert compressor.should_compress() is True

    def test_exact_threshold(self, compressor):
        compressor.last_prompt_tokens = 85000
        assert compressor.should_compress() is True

    def test_explicit_tokens(self, compressor):
        assert compressor.should_compress(prompt_tokens=90000) is True
        assert compressor.should_compress(prompt_tokens=50000) is False


class TestShouldCompressPreflight:
    def test_short_messages(self, compressor):
        msgs = [{"role": "user", "content": "short"}]
        assert compressor.should_compress_preflight(msgs) is False

    def test_long_messages(self, compressor):
        # Each message ~100k chars / 4 = 25k tokens, need >85k threshold
        msgs = [{"role": "user", "content": "x" * 400000}]
        assert compressor.should_compress_preflight(msgs) is True


class TestUpdateFromResponse:
    def test_updates_fields(self, compressor):
        compressor.update_from_response({
            "prompt_tokens": 5000,
            "completion_tokens": 1000,
            "total_tokens": 6000,
        })
        assert compressor.last_prompt_tokens == 5000
        assert compressor.last_completion_tokens == 1000
        assert compressor.last_total_tokens == 6000

    def test_missing_fields_default_zero(self, compressor):
        compressor.update_from_response({})
        assert compressor.last_prompt_tokens == 0


class TestGetStatus:
    def test_returns_expected_keys(self, compressor):
        status = compressor.get_status()
        assert "last_prompt_tokens" in status
        assert "threshold_tokens" in status
        assert "context_length" in status
        assert "usage_percent" in status
        assert "compression_count" in status

    def test_usage_percent_calculation(self, compressor):
        compressor.last_prompt_tokens = 50000
        status = compressor.get_status()
        assert status["usage_percent"] == 50.0


class TestCompress:
    def _make_messages(self, n):
        return [{"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"} for i in range(n)]

    def test_too_few_messages_returns_unchanged(self, compressor):
        msgs = self._make_messages(4)  # protect_first=2 + protect_last=2 + 1 = 5 needed
        result = compressor.compress(msgs)
        assert result == msgs

    def test_truncation_fallback_no_client(self, compressor):
        # compressor has client=None, so should use truncation fallback
        # Need to use summarization strategy since we're testing _compress_summarization
        compressor.strategy = "summarization"
        msgs = [{"role": "system", "content": "System prompt"}] + self._make_messages(10)
        result = compressor.compress(msgs)
        assert len(result) < len(msgs)
        # Should keep system message and last N
        assert result[0]["role"] == "system"
        assert compressor.compression_count == 1

    def test_compression_increments_count(self, compressor):
        # Use summarization strategy since we're testing summarization path
        compressor.strategy = "summarization"
        msgs = self._make_messages(10)
        compressor.compress(msgs)
        assert compressor.compression_count == 1
        compressor.compress(msgs)
        assert compressor.compression_count == 2

    def test_protects_first_and_last(self, compressor):
        msgs = self._make_messages(10)
        result = compressor.compress(msgs)
        # First 2 messages should be preserved (protect_first_n=2)
        # Last 2 messages should be preserved (protect_last_n=2)
        assert result[-1]["content"] == msgs[-1]["content"]
        assert result[-2]["content"] == msgs[-2]["content"]


class TestGenerateSummaryNoneContent:
    """Regression: content=None (from tool-call-only assistant messages) must not crash."""

    def test_none_content_does_not_crash(self):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "[CONTEXT SUMMARY]: tool calls happened"
        mock_client.chat.completions.create.return_value = mock_response

        with patch("agent.context_compressor.get_model_context_length", return_value=100000), \
             patch("agent.context_compressor.get_text_auxiliary_client", return_value=(mock_client, "test-model")):
            c = ContextCompressor(model="test", quiet_mode=True)

        messages = [
            {"role": "user", "content": "do something"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"function": {"name": "search"}}
            ]},
            {"role": "tool", "content": "result"},
            {"role": "assistant", "content": None},
            {"role": "user", "content": "thanks"},
        ]

        summary = c._generate_summary(messages)
        assert isinstance(summary, str)
        assert "CONTEXT SUMMARY" in summary

    def test_none_content_in_system_message_compress(self):
        """System message with content=None should not crash during compress."""
        with patch("agent.context_compressor.get_model_context_length", return_value=100000), \
             patch("agent.context_compressor.get_text_auxiliary_client", return_value=(None, None)):
            c = ContextCompressor(model="test", quiet_mode=True, protect_first_n=2, protect_last_n=2, strategy="summarization")

        msgs = [{"role": "system", "content": None}] + [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"}
            for i in range(10)
        ]
        result = c.compress(msgs)
        assert len(result) < len(msgs)


class TestCompressWithClient:
    def test_summarization_path(self):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "[CONTEXT SUMMARY]: stuff happened"
        mock_client.chat.completions.create.return_value = mock_response

        with patch("agent.context_compressor.get_model_context_length", return_value=100000), \
             patch("agent.context_compressor.get_text_auxiliary_client", return_value=(mock_client, "test-model")):
            c = ContextCompressor(model="test", quiet_mode=True, strategy="summarization")

        msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"} for i in range(10)]
        result = c.compress(msgs)

        # Should have summary message in the middle
        contents = [m.get("content", "") for m in result]
        assert any("CONTEXT SUMMARY" in c for c in contents)
        assert len(result) < len(msgs)


class TestSlidingWindowCompression:
    """Test the free sliding window compression strategy."""

    def test_sliding_window_keeps_last_n(self):
        with patch("agent.context_compressor.get_model_context_length", return_value=100000), \
             patch("agent.context_compressor.get_text_auxiliary_client", return_value=(None, None)):
            c = ContextCompressor(model="test", quiet_mode=True, window_size=3)

        msgs = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "msg 0"},
            {"role": "assistant", "content": "msg 1"},
            {"role": "user", "content": "msg 2"},
            {"role": "assistant", "content": "msg 3"},
            {"role": "user", "content": "msg 4"},
            {"role": "assistant", "content": "msg 5"},
        ]

        result = c.compress_sliding_window(msgs)

        # Should have system message + last 3 non-system messages
        assert len(result) == 4  # 1 system + 3 last
        assert result[0]["role"] == "system"
        assert result[1]["content"] == "msg 3"
        assert result[2]["content"] == "msg 4"
        assert result[3]["content"] == "msg 5"

    def test_sliding_window_no_compression_needed(self):
        with patch("agent.context_compressor.get_model_context_length", return_value=100000), \
             patch("agent.context_compressor.get_text_auxiliary_client", return_value=(None, None)):
            c = ContextCompressor(model="test", quiet_mode=True, window_size=10)

        msgs = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "msg 1"},
            {"role": "assistant", "content": "msg 2"},
        ]

        result = c.compress_sliding_window(msgs)
        # Only 2 non-system messages, window_size=10, so no compression
        assert result == msgs

    def test_sliding_window_increments_count(self):
        with patch("agent.context_compressor.get_model_context_length", return_value=100000), \
             patch("agent.context_compressor.get_text_auxiliary_client", return_value=(None, None)):
            c = ContextCompressor(model="test", quiet_mode=True, window_size=2)

        msgs = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"}
            for i in range(10)
        ]

        initial_count = c.compression_count
        c.compress_sliding_window(msgs)
        assert c.compression_count == initial_count + 1


class TestHybridCompression:
    """Test the hybrid (two-tier) compression strategy."""

    def test_hybrid_uses_sliding_window_first(self):
        """At 70% threshold, hybrid should use sliding window (free compression)."""
        with patch("agent.context_compressor.get_model_context_length", return_value=100000), \
             patch("agent.context_compressor.get_text_auxiliary_client", return_value=(None, None)), \
             patch("agent.context_compressor.estimate_messages_tokens_rough", side_effect=[
                 75000,  # First call: initial token estimate (above 70% threshold)
                 65000,  # Second call: after sliding window (below 85% threshold)
             ]):
            c = ContextCompressor(
                model="test",
                quiet_mode=True,
                threshold_percent=0.85,
                sliding_window_threshold_percent=0.70,
                window_size=5,
                strategy="hybrid",
            )

        msgs = [{"role": "system", "content": "System"}] + [
            {"role": "user", "content": f"msg {i}"}
            for i in range(15)
        ]

        result = c.compress(msgs, current_tokens=75000)

        # Should have compressed using sliding window
        # System + last 5 non-system = 6 messages
        assert len(result) == 6
        assert c.compression_count == 1

    def test_hybrid_falls_back_to_summarization(self):
        """If sliding window doesn't reduce enough, hybrid should use LLM summarization."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "[CONTEXT SUMMARY]: compressed"
        mock_client.chat.completions.create.return_value = mock_response

        with patch("agent.context_compressor.get_model_context_length", return_value=100000), \
             patch("agent.context_compressor.get_text_auxiliary_client", return_value=(mock_client, "test-model")), \
             patch("agent.context_compressor.estimate_messages_tokens_rough", side_effect=[
                 88000,  # After sliding window (still above 85% threshold)
                 82000,  # After LLM summarization (below 85%)
             ]):
            c = ContextCompressor(
                model="test",
                quiet_mode=True,
                threshold_percent=0.85,
                sliding_window_threshold_percent=0.70,
                window_size=8,  # Larger window so sliding window result has enough messages for summarization
                strategy="hybrid",
                protect_first_n=2,
                protect_last_n=2,
            )

            msgs = [{"role": "system", "content": "System"}] + [
                {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"}
                for i in range(20)
            ]

            result = c.compress(msgs, current_tokens=75000)

            # Should have applied both tiers
            assert c.compression_count == 2  # Both sliding window and summarization
            # Result should contain a summary message
            contents = [m.get("content", "") for m in result]
            assert any("CONTEXT SUMMARY" in c for c in contents)

    def test_hybrid_no_compression_below_threshold(self):
        """Below 70% threshold, hybrid should not compress."""
        with patch("agent.context_compressor.get_model_context_length", return_value=100000), \
             patch("agent.context_compressor.get_text_auxiliary_client", return_value=(None, None)):
            c = ContextCompressor(
                model="test",
                quiet_mode=True,
                threshold_percent=0.85,
                sliding_window_threshold_percent=0.70,
                strategy="hybrid",
            )

        msgs = [
            {"role": "user", "content": f"msg {i}"}
            for i in range(5)
        ]

        result = c.compress(msgs, current_tokens=50000)  # Below 70% threshold

        # Should not compress
        assert result == msgs
        assert c.compression_count == 0

    def test_sliding_window_only_strategy(self):
        """Test strategy='sliding_window' uses only sliding window."""
        with patch("agent.context_compressor.get_model_context_length", return_value=100000), \
             patch("agent.context_compressor.get_text_auxiliary_client", return_value=(None, None)):
            c = ContextCompressor(
                model="test",
                quiet_mode=True,
                window_size=2,
                strategy="sliding_window",
            )

        msgs = [{"role": "user", "content": f"msg {i}"} for i in range(10)]

        result = c.compress(msgs)

        # Should only apply sliding window compression
        assert len(result) == 2
        assert c.compression_count == 1

    def test_summarization_only_strategy(self):
        """Test strategy='summarization' uses only LLM summarization."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "[CONTEXT SUMMARY]: compressed"
        mock_client.chat.completions.create.return_value = mock_response

        with patch("agent.context_compressor.get_model_context_length", return_value=100000), \
             patch("agent.context_compressor.get_text_auxiliary_client", return_value=(mock_client, "test-model")):
            c = ContextCompressor(
                model="test",
                quiet_mode=True,
                strategy="summarization",
                protect_first_n=1,
                protect_last_n=1,
            )

        msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"} for i in range(10)]

        result = c.compress(msgs)

        # Should have summary message
        contents = [m.get("content", "") for m in result]
        assert any("CONTEXT SUMMARY" in c for c in contents)
        assert c.compression_count == 1
