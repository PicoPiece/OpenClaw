"""Tests for queue worker — verifies retry logic and fallback switching on 429."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from openclaw_memory.config import Settings
from openclaw_memory.queue_worker import (
    CloudRateLimitError,
    CloudTimeoutError,
    LLMCall,
    QueueWorker,
)


class FakeRedis:
    """Minimal Redis stub for testing without a real server."""

    def __init__(self):
        self._store: dict[str, str] = {}
        self._lists: dict[str, list] = {}

    def lpush(self, key, value):
        self._lists.setdefault(key, []).insert(0, value)

    def brpop(self, key, timeout=0):
        lst = self._lists.get(key, [])
        if lst:
            return (key, lst.pop())
        return None

    def get(self, key):
        return self._store.get(key)

    def setex(self, key, ttl, value):
        self._store[key] = value

    def delete(self, *keys):
        for k in keys:
            self._store.pop(k, None)
            self._lists.pop(k, None)

    def register_script(self, script):
        def fake_script(keys, args):
            return [1, 100, 0]
        return fake_script

    @classmethod
    def from_url(cls, *a, **kw):
        return cls()


class TestQueueWorker:
    def _make_worker(self, settings: Settings) -> tuple[QueueWorker, FakeRedis]:
        fake_r = FakeRedis()
        rl = MagicMock()
        rl.acquire = MagicMock(return_value=True)
        worker = QueueWorker(settings, redis_client=fake_r, rate_limiter=rl)
        return worker, fake_r

    @patch("openclaw_memory.queue_worker.requests.post")
    def test_cloud_success(self, mock_post, settings: Settings):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "candidates": [{"content": {"parts": [{"text": "Cloud answer."}]}}]
            },
        )

        worker, _ = self._make_worker(settings)
        call = LLMCall(prompt="Hello", estimated_tokens=10)
        result = worker.process_one(call)

        assert result.used == "cloud"
        assert result.text == "Cloud answer."

    @patch("openclaw_memory.queue_worker.requests.post")
    def test_429_retries_then_fallback(self, mock_post, settings: Settings):
        settings.worker.retry_max = 2

        cloud_429 = MagicMock(
            status_code=429,
            headers={"Retry-After": "1"},
        )
        cloud_429.raise_for_status.side_effect = Exception("429")

        ollama_ok = MagicMock(
            status_code=200,
            json=lambda: {"response": "Fallback answer."},
        )
        ollama_ok.raise_for_status = MagicMock()

        mock_post.side_effect = [cloud_429, cloud_429, ollama_ok]

        worker, fake_r = self._make_worker(settings)
        call = LLMCall(prompt="Hello", estimated_tokens=10)

        # First attempt: 429 -> requeue
        r1 = worker.process_one(call)
        assert r1.reason == "requeued_429"

        # Second attempt (from queue): 429 -> max retries -> fallback
        call.retries = 2
        r2 = worker.process_one(call)
        assert r2.used == "fallback"
        assert r2.text == "Fallback answer."

    @patch("openclaw_memory.queue_worker.requests.post")
    def test_timeout_triggers_fallback(self, mock_post, settings: Settings):
        import requests as req

        ollama_ok = MagicMock(
            status_code=200,
            json=lambda: {"response": "Fallback on timeout."},
        )
        ollama_ok.raise_for_status = MagicMock()

        mock_post.side_effect = [req.Timeout("timeout"), ollama_ok]

        worker, _ = self._make_worker(settings)
        call = LLMCall(prompt="Hello", estimated_tokens=10)
        result = worker.process_one(call)

        assert result.used == "fallback"
        assert result.reason == "timeout"

    def test_llm_call_serialization(self):
        call = LLMCall(prompt="test", scratchpad="step1", priority="high")
        json_str = call.to_json()
        restored = LLMCall.from_json(json_str)
        assert restored.prompt == call.prompt
        assert restored.scratchpad == call.scratchpad
        assert restored.priority == call.priority

    @patch("openclaw_memory.queue_worker.requests.post")
    def test_result_annotated_with_metadata(self, mock_post, settings: Settings):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "candidates": [{"content": {"parts": [{"text": "OK"}]}}]
            },
        )

        worker, _ = self._make_worker(settings)
        call = LLMCall(prompt="Hello", estimated_tokens=10)
        result = worker.process_one(call)

        assert result.used in ("cloud", "fallback")
        assert result.call_id == call.id
        assert result.tokens_in > 0
