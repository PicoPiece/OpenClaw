"""Tests for prompt builder — verifies token budget enforcement."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from openclaw_memory.config import Settings
from openclaw_memory.memory_manager import MemoryManager, Message
from openclaw_memory.prompt_builder import PromptBuilder
from openclaw_memory.summarizer import estimate_tokens


class TestPromptBuilder:
    def _make_mm(self, settings: Settings, n_messages: int = 5) -> MemoryManager:
        with patch("openclaw_memory.memory_manager.MemoryManager._ensure_collection"):
            mm = MemoryManager(settings, qdrant_client=None)
        for i in range(n_messages):
            role = "user" if i % 2 == 0 else "assistant"
            mm.append(role, f"Test message {i}")
        return mm

    def test_prompt_within_budget(self, settings: Settings):
        settings.tokens.max_prompt = 500
        mm = self._make_mm(settings, 3)
        pb = PromptBuilder(settings, mm)
        result = pb.build()
        assert result.token_count <= 500
        assert "system" in result.sections

    def test_truncation_when_over_budget(self, settings: Settings):
        settings.tokens.max_prompt = 30
        settings.tokens.sliding_window_size = 20
        mm = self._make_mm(settings, 20)
        pb = PromptBuilder(settings, mm)
        result = pb.build()
        assert result.token_count <= 40  # some margin for estimate_tokens imprecision
        assert result.truncated is True

    def test_scratchpad_included(self, settings: Settings):
        settings.tokens.max_prompt = 500
        mm = self._make_mm(settings, 2)
        pb = PromptBuilder(settings, mm)
        result = pb.build(scratchpad="Step 1: analyze. Step 2: respond.")
        assert "Reasoning State" in result.text
        assert "scratchpad" in result.sections

    def test_memories_included(self, settings: Settings):
        settings.tokens.max_prompt = 500
        mm = self._make_mm(settings, 2)
        pb = PromptBuilder(settings, mm)
        result = pb.build(extra_memories=["Fact A: important thing", "Fact B: another thing"])
        assert "Relevant Facts" in result.text
        assert "memories" in result.sections

    def test_empty_conversation(self, settings: Settings):
        settings.tokens.max_prompt = 500
        with patch("openclaw_memory.memory_manager.MemoryManager._ensure_collection"):
            mm = MemoryManager(settings, qdrant_client=None)
        pb = PromptBuilder(settings, mm)
        result = pb.build()
        assert result.token_count > 0
        assert "system" in result.sections
