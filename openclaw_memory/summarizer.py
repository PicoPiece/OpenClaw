"""Summarizer — compresses conversation history via cloud LLM with local fallback."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

from .config import Settings

log = logging.getLogger(__name__)

SUMMARIZE_SYSTEM = (
    "You are a concise summarizer. Condense the following conversation history "
    "into a short paragraph preserving all key facts, decisions, and action items. "
    "Output ONLY the summary, nothing else."
)


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~1.3 tokens per whitespace-delimited word."""
    return int(len(text.split()) * 1.3)


@dataclass
class SummaryResult:
    text: str
    tokens: int
    source: str  # "cloud" | "fallback"
    source_ids: list[str]


class Summarizer:
    def __init__(self, settings: Settings):
        self.cfg = settings

    # -- public ---------------------------------------------------------

    def summarize(
        self,
        messages: list[dict[str, Any]],
        source_ids: list[str] | None = None,
    ) -> SummaryResult:
        """Summarize a list of messages. Tries cloud first, falls back to Ollama."""
        history_text = self._format_history(messages)
        source_ids = source_ids or [m.get("id", "") for m in messages]

        try:
            text = self._call_cloud(history_text)
            return SummaryResult(
                text=text,
                tokens=estimate_tokens(text),
                source="cloud",
                source_ids=source_ids,
            )
        except Exception as exc:
            log.warning("Cloud summarization failed (%s), falling back to Ollama", exc)

        try:
            text = self._call_ollama(history_text)
            return SummaryResult(
                text=text,
                tokens=estimate_tokens(text),
                source="fallback",
                source_ids=source_ids,
            )
        except Exception as exc:
            log.error("Ollama summarization also failed: %s", exc)
            raise

    def compress_text(self, text: str, target_tokens: int) -> str:
        """Re-summarize already-summarized text to fit within *target_tokens*."""
        if estimate_tokens(text) <= target_tokens:
            return text
        prompt = (
            f"Compress the following text to under {target_tokens} tokens "
            f"while keeping all critical information:\n\n{text}"
        )
        try:
            return self._call_cloud(prompt)
        except Exception:
            return self._call_ollama(prompt)

    # -- internals ------------------------------------------------------

    @staticmethod
    def _format_history(messages: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            parts.append(f"{role}: {content}")
        return "\n".join(parts)

    def _call_cloud(self, user_text: str) -> str:
        cfg = self.cfg.ai_studio
        url = f"{cfg.endpoint}/{cfg.model}:generateContent?key={cfg.api_key}"
        payload = {
            "contents": [
                {"role": "user", "parts": [{"text": f"{SUMMARIZE_SYSTEM}\n\n{user_text}"}]}
            ],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 512},
        }
        resp = requests.post(url, json=payload, timeout=30)
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After", "60")
            log.warning("Cloud 429 — Retry-After: %s", retry_after)
            raise RateLimitError(retry_after=int(retry_after))
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()

    def _call_ollama(self, user_text: str) -> str:
        url = f"{self.cfg.ollama.url}/api/generate"
        payload = {
            "model": self.cfg.ollama.model,
            "prompt": f"{SUMMARIZE_SYSTEM}\n\n{user_text}",
            "stream": False,
            "options": {"temperature": 0.3, "num_predict": 512},
        }
        resp = requests.post(url, json=payload, timeout=120)
        resp.raise_for_status()
        return resp.json()["response"].strip()


class RateLimitError(Exception):
    def __init__(self, retry_after: int = 60):
        self.retry_after = retry_after
        super().__init__(f"Rate limited — retry after {retry_after}s")
