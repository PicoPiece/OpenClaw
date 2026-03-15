"""Queue Worker — Redis-backed job queue with retry, backoff, and automatic fallback."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

import redis
import requests

from .config import Settings
from .rate_limiter import RateLimiter
from .summarizer import Summarizer, estimate_tokens

log = logging.getLogger(__name__)

QUEUE_KEY = "oc:llm:queue"
RESULT_PREFIX = "oc:llm:result:"
PRIORITIES = {"high": 0, "medium": 1, "low": 2}


@dataclass
class LLMCall:
    """A queued LLM request."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    prompt: str = ""
    scratchpad: str = ""
    priority: str = "medium"
    retries: int = 0
    delay_until: float = 0.0
    fallback_used: bool = False
    estimated_tokens: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> LLMCall:
        return cls(**json.loads(data))


@dataclass
class LLMResult:
    call_id: str
    text: str
    used: str  # "cloud" | "fallback"
    reason: str = ""
    tokens_in: int = 0
    tokens_out: int = 0


def enqueue_call(
    redis_client: redis.Redis,
    prompt: str,
    scratchpad: str = "",
    priority: str = "medium",
) -> str:
    """Push an LLM call onto the Redis queue. Returns the call ID."""
    call = LLMCall(
        prompt=prompt,
        scratchpad=scratchpad,
        priority=priority,
        estimated_tokens=estimate_tokens(prompt),
    )
    redis_client.lpush(QUEUE_KEY, call.to_json())
    log.info("Enqueued LLM call %s (priority=%s, ~%d tokens)", call.id, priority, call.estimated_tokens)
    return call.id


def get_result(redis_client: redis.Redis, call_id: str, timeout: float = 60) -> LLMResult | None:
    """Block-wait for a result from the worker."""
    key = f"{RESULT_PREFIX}{call_id}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = redis_client.get(key)
        if raw:
            data = json.loads(raw)
            return LLMResult(**data)
        time.sleep(0.5)
    return None


class QueueWorker:
    """Worker that processes LLM calls from Redis with rate limiting and fallback."""

    def __init__(
        self,
        settings: Settings,
        redis_client: redis.Redis | None = None,
        rate_limiter: RateLimiter | None = None,
        summarizer: Summarizer | None = None,
    ):
        self.cfg = settings
        self._r = redis_client or redis.Redis.from_url(
            settings.redis.url, decode_responses=True
        )
        self._rl = rate_limiter or RateLimiter(settings, self._r)
        self._summarizer = summarizer or Summarizer(settings)
        self._running = False

    def start(self, blocking: bool = True) -> threading.Thread | None:
        """Start the worker loop. If not blocking, runs in a background thread."""
        self._running = True
        if blocking:
            self._loop()
            return None
        t = threading.Thread(target=self._loop, daemon=True, name="oc-queue-worker")
        t.start()
        return t

    def stop(self) -> None:
        self._running = False

    def process_one(self, call: LLMCall) -> LLMResult:
        """Process a single LLM call with retry and fallback logic."""
        # Respect delay (re-enqueued after backoff)
        if call.delay_until and time.time() < call.delay_until:
            time.sleep(max(0, call.delay_until - time.time()))

        # Acquire rate-limit capacity
        self._rl.acquire(estimated_tokens=call.estimated_tokens, blocking=True)

        # Try cloud
        try:
            text = self._call_cloud(call.prompt)
            return LLMResult(
                call_id=call.id,
                text=text,
                used="cloud",
                tokens_in=call.estimated_tokens,
                tokens_out=estimate_tokens(text),
            )
        except CloudRateLimitError as exc:
            log.warning(
                "Cloud 429 for call %s (retry %d/%d, Retry-After=%d)",
                call.id,
                call.retries,
                self.cfg.worker.retry_max,
                exc.retry_after,
            )
            call.retries += 1
            if call.retries < self.cfg.worker.retry_max:
                backoff = self.cfg.worker.backoff_base ** call.retries
                delay = max(backoff, exc.retry_after)
                call.delay_until = time.time() + delay
                self._r.lpush(QUEUE_KEY, call.to_json())
                return LLMResult(
                    call_id=call.id, text="", used="cloud", reason="requeued_429"
                )

            log.warning("Max retries for call %s — switching to fallback", call.id)

        except CloudTimeoutError:
            log.warning("Cloud timeout for call %s — switching to fallback", call.id)

        # Fallback to Ollama
        return self._run_fallback(call)

    # -- internals ------------------------------------------------------

    def _loop(self) -> None:
        log.info("Queue worker started")
        while self._running:
            raw = self._r.brpop(QUEUE_KEY, timeout=2)
            if raw is None:
                continue
            _, payload = raw
            call = LLMCall.from_json(payload)
            result = self.process_one(call)
            if result.text:
                self._publish_result(result)
        log.info("Queue worker stopped")

    def _call_cloud(self, prompt: str) -> str:
        cfg = self.cfg.ai_studio
        url = f"{cfg.endpoint}/{cfg.model}:generateContent?key={cfg.api_key}"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 2048},
        }
        try:
            resp = requests.post(url, json=payload, timeout=30)
        except requests.Timeout:
            raise CloudTimeoutError()

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "60"))
            raise CloudRateLimitError(retry_after=retry_after)
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()

    def _run_fallback(self, call: LLMCall) -> LLMResult:
        """Call Ollama with conservative settings. Compress prompt if needed."""
        prompt = call.prompt
        if call.scratchpad:
            prompt += f"\n\n--- Reasoning State ---\n{call.scratchpad}"

        if estimate_tokens(prompt) > self.cfg.tokens.max_prompt:
            log.info("Compressing prompt for fallback model")
            prompt = self._summarizer.compress_text(prompt, self.cfg.tokens.max_prompt)

        try:
            text = self._call_ollama(prompt)
            return LLMResult(
                call_id=call.id,
                text=text,
                used="fallback",
                reason="429" if call.retries > 0 else "timeout",
                tokens_in=estimate_tokens(prompt),
                tokens_out=estimate_tokens(text),
            )
        except Exception as exc:
            log.error("Fallback also failed for call %s: %s", call.id, exc)
            return LLMResult(
                call_id=call.id,
                text=f"[Error] Both cloud and fallback failed: {exc}",
                used="fallback",
                reason="all_failed",
            )

    def _call_ollama(self, prompt: str) -> str:
        url = f"{self.cfg.ollama.url}/api/generate"
        payload = {
            "model": self.cfg.ollama.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": self.cfg.fallback.temperature,
                "num_predict": self.cfg.fallback.max_output_tokens,
            },
        }
        resp = requests.post(url, json=payload, timeout=120)
        resp.raise_for_status()
        return resp.json()["response"].strip()

    def _publish_result(self, result: LLMResult) -> None:
        key = f"{RESULT_PREFIX}{result.call_id}"
        self._r.setex(key, 300, json.dumps(asdict(result)))


class CloudRateLimitError(Exception):
    def __init__(self, retry_after: int = 60):
        self.retry_after = retry_after
        super().__init__(f"429 — Retry-After: {retry_after}s")


class CloudTimeoutError(Exception):
    pass
