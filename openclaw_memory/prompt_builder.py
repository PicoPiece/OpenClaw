"""Prompt Builder — assembles the final prompt with strict token-budget enforcement."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .memory_manager import MemoryManager, Message
from .summarizer import Summarizer, estimate_tokens

log = logging.getLogger(__name__)


@dataclass
class BuiltPrompt:
    """The final prompt ready to send to the LLM."""

    text: str
    token_count: int
    sections: dict[str, int] = field(default_factory=dict)
    truncated: bool = False


class PromptBuilder:
    """Composes prompts in order: system -> summary -> memories -> messages -> scratchpad.

    Enforces MAX_PROMPT_TOKENS by progressively truncating oldest messages
    and compressing the summary if needed.
    """

    def __init__(
        self,
        settings: Settings,
        memory_manager: MemoryManager,
        summarizer: Summarizer | None = None,
    ):
        self.cfg = settings
        self._mm = memory_manager
        self._summarizer = summarizer or Summarizer(settings)

    def build(
        self,
        scratchpad: str = "",
        extra_memories: list[str] | None = None,
        query: str | None = None,
    ) -> BuiltPrompt:
        """Build the final prompt within the token budget.

        Args:
            scratchpad: Structured reasoning state to append.
            extra_memories: Additional context strings to inject.
            query: If provided, retrieve relevant Qdrant memories for this query.
        """
        budget = self.cfg.tokens.max_prompt
        sections: dict[str, int] = {}
        parts: list[str] = []

        # 1. System prompt (always included, highest priority)
        sys_prompt = self.cfg.system_prompt.strip()
        sys_tokens = estimate_tokens(sys_prompt)
        parts.append(sys_prompt)
        sections["system"] = sys_tokens
        remaining = budget - sys_tokens

        # 2. Summary
        summary = self._mm.get_summary()
        summary_text = ""
        if summary:
            summary_text = summary.content
            stokens = estimate_tokens(summary_text)
            if stokens > remaining // 3:
                summary_text = self._summarizer.compress_text(
                    summary_text, remaining // 3
                )
                stokens = estimate_tokens(summary_text)
            parts.append(f"\n--- Conversation Summary ---\n{summary_text}")
            sections["summary"] = stokens
            remaining -= stokens

        # 3. Relevant memories from Qdrant
        memories: list[str] = extra_memories or []
        if query:
            memories.extend(self._mm.retrieve_relevant(query))
        if memories:
            mem_block = "\n--- Relevant Facts ---\n" + "\n".join(
                f"- {m}" for m in memories
            )
            mtokens = estimate_tokens(mem_block)
            if mtokens <= remaining // 3:
                parts.append(mem_block)
                sections["memories"] = mtokens
                remaining -= mtokens
            else:
                trimmed = self._trim_memories(memories, remaining // 3)
                parts.append(
                    "\n--- Relevant Facts ---\n" + "\n".join(f"- {m}" for m in trimmed)
                )
                mtokens = estimate_tokens("\n".join(trimmed))
                sections["memories"] = mtokens
                remaining -= mtokens

        # 4. Scratchpad (reasoning state — high priority, reserve space)
        scratch_tokens = 0
        if scratchpad:
            scratch_tokens = estimate_tokens(scratchpad)
            remaining -= scratch_tokens

        # 5. Recent messages (fill remaining budget)
        recent = self._mm.get_recent()
        messages_block, msg_tokens, was_truncated = self._fit_messages(
            recent, remaining
        )
        if messages_block:
            parts.append(f"\n--- Conversation ---\n{messages_block}")
        sections["messages"] = msg_tokens
        remaining -= msg_tokens

        # 6. Append scratchpad last
        if scratchpad:
            parts.append(f"\n--- Reasoning State ---\n{scratchpad}")
            sections["scratchpad"] = scratch_tokens

        final = "\n".join(parts)
        total = estimate_tokens(final)

        return BuiltPrompt(
            text=final,
            token_count=total,
            sections=sections,
            truncated=was_truncated,
        )

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _fit_messages(
        messages: list[Message], budget: int
    ) -> tuple[str, int, bool]:
        """Include as many recent messages as fit within *budget* tokens."""
        lines: list[str] = []
        total = 0
        truncated = False

        for msg in reversed(messages):
            line = f"{msg.role}: {msg.content}"
            t = estimate_tokens(line)
            if total + t > budget:
                truncated = True
                break
            lines.append(line)
            total += t

        lines.reverse()
        return "\n".join(lines), total, truncated

    @staticmethod
    def _trim_memories(memories: list[str], budget: int) -> list[str]:
        result: list[str] = []
        total = 0
        for m in memories:
            t = estimate_tokens(m)
            if total + t > budget:
                break
            result.append(m)
            total += t
        return result
