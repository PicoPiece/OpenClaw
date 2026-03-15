"""Tests for summarizer — verifies token reduction and fallback behavior."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from openclaw_memory.config import Settings
from openclaw_memory.summarizer import Summarizer, estimate_tokens


class TestEstimateTokens:
    def test_empty(self):
        assert estimate_tokens("") == 0

    def test_short(self):
        result = estimate_tokens("hello world")
        assert 2 <= result <= 4

    def test_longer(self):
        text = " ".join(["word"] * 100)
        result = estimate_tokens(text)
        assert 120 <= result <= 140


class TestSummarizer:
    def _make_messages(self, n: int) -> list[dict]:
        return [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"Message {i} " * 20}
            for i in range(n)
        ]

    @patch("openclaw_memory.summarizer.requests.post")
    def test_cloud_success_reduces_tokens(self, mock_post, settings: Settings):
        original = self._make_messages(10)
        original_tokens = sum(estimate_tokens(m["content"]) for m in original)

        summary_text = "Summary of 10 messages about various topics."
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "candidates": [{"content": {"parts": [{"text": summary_text}]}}]
            },
        )

        s = Summarizer(settings)
        result = s.summarize(original)

        assert result.source == "cloud"
        assert result.tokens < original_tokens
        assert "Summary" in result.text

    @patch("openclaw_memory.summarizer.requests.post")
    def test_fallback_on_429(self, mock_post, settings: Settings):
        cloud_resp = MagicMock(status_code=429, headers={"Retry-After": "30"})
        cloud_resp.raise_for_status.side_effect = Exception("429")

        ollama_resp = MagicMock(
            status_code=200,
            json=lambda: {"response": "Fallback summary of conversation."},
        )
        ollama_resp.raise_for_status = MagicMock()

        mock_post.side_effect = [cloud_resp, ollama_resp]

        s = Summarizer(settings)
        result = s.summarize(self._make_messages(5))

        assert result.source == "fallback"
        assert "Fallback summary" in result.text

    @patch("openclaw_memory.summarizer.requests.post")
    def test_compress_text(self, mock_post, settings: Settings):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "candidates": [{"content": {"parts": [{"text": "Compressed."}]}}]
            },
        )

        s = Summarizer(settings)
        result = s.compress_text("A very long text " * 100, target_tokens=10)
        assert result == "Compressed."
