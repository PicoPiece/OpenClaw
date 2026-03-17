"""Model Router — smart quota tracking and model selection across free-tier Gemini models.

Tracks per-model daily usage in Redis and routes requests to the best
available model based on remaining quota and task complexity.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import redis

log = logging.getLogger(__name__)

QUOTA_KEY_PREFIX = "oc:quota"


@dataclass(frozen=True)
class ModelTier:
    model_id: str
    rpm: int
    rpd: int
    quality: int  # 1=basic, 2=good, 3=best


FREE_TIERS: list[ModelTier] = [
    ModelTier("google/gemini-2.5-pro", rpm=5, rpd=100, quality=3),
    ModelTier("google/gemini-2.5-flash", rpm=10, rpd=250, quality=2),
    ModelTier("google/gemini-2.5-flash-lite", rpm=15, rpd=1000, quality=1),
]

TOTAL_FREE_RPD = sum(t.rpd for t in FREE_TIERS)


def _today_key() -> str:
    """Date key in Pacific Time (Google resets at midnight PT)."""
    utc_now = datetime.now(timezone.utc)
    pt_offset_hours = -7  # PDT; use -8 for PST
    pt_hour = utc_now.hour + pt_offset_hours
    day = utc_now.date()
    if pt_hour < 0:
        from datetime import timedelta
        day = day - timedelta(days=1)
    return day.isoformat()


class ModelRouter:
    """Selects the best available Gemini model based on remaining daily quota.

    Strategy:
        - quality_first (default): use highest-quality model that has quota
        - economy: use lowest-quality model first, save Pro for complex tasks
        - balanced: use Flash by default, Pro for complex, Lite as last resort
    """

    def __init__(
        self,
        redis_client: redis.Redis | None = None,
        redis_url: str = "redis://localhost:6379/0",
        strategy: str = "quality_first",
        tiers: list[ModelTier] | None = None,
    ):
        self._r = redis_client or redis.Redis.from_url(redis_url, decode_responses=True)
        self._tiers = tiers or FREE_TIERS
        self._strategy = strategy

    def select_model(self, complexity: str = "normal") -> ModelTier | None:
        """Pick the best model given current quota and task complexity.

        Args:
            complexity: "simple" | "normal" | "complex"
                - simple: short answers, translations -> prefer Lite
                - normal: general chat -> follow strategy
                - complex: code, analysis, reasoning -> prefer Pro
        """
        usage = self.get_usage()
        available = [
            t for t in self._tiers if usage.get(t.model_id, 0) < t.rpd
        ]

        if not available:
            log.warning("All models exhausted for today")
            return None

        if complexity == "complex":
            available.sort(key=lambda t: t.quality, reverse=True)
            return available[0]

        if complexity == "simple":
            available.sort(key=lambda t: t.quality)
            return available[0]

        if self._strategy == "quality_first":
            available.sort(key=lambda t: t.quality, reverse=True)
        elif self._strategy == "economy":
            available.sort(key=lambda t: t.quality)
        elif self._strategy == "balanced":
            available.sort(key=lambda t: (t.quality != 2, -t.quality))
        return available[0]

    def record_usage(self, model_id: str, count: int = 1) -> int:
        """Record that *count* requests were made to *model_id*. Returns new total."""
        key = f"{QUOTA_KEY_PREFIX}:{_today_key()}:{model_id}"
        pipe = self._r.pipeline()
        pipe.incrby(key, count)
        pipe.expire(key, 90000)  # ~25h TTL, auto-cleanup
        results = pipe.execute()
        new_total = results[0]
        log.debug("Usage recorded: %s = %d", model_id, new_total)
        return new_total

    def get_usage(self) -> dict[str, int]:
        """Get today's usage counts for all models."""
        today = _today_key()
        result = {}
        for tier in self._tiers:
            key = f"{QUOTA_KEY_PREFIX}:{today}:{tier.model_id}"
            val = self._r.get(key)
            result[tier.model_id] = int(val) if val else 0
        return result

    def get_remaining(self) -> dict[str, int]:
        """Get remaining quota for each model today."""
        usage = self.get_usage()
        return {t.model_id: max(0, t.rpd - usage.get(t.model_id, 0)) for t in self._tiers}

    def get_total_remaining(self) -> int:
        """Total requests remaining across all models."""
        return sum(self.get_remaining().values())

    def format_status(self) -> str:
        """Human-readable status for Telegram /usage command."""
        usage = self.get_usage()
        remaining = self.get_remaining()
        lines = [f"📊 Model Quota Status ({_today_key()} PT)\n"]
        for tier in self._tiers:
            used = usage.get(tier.model_id, 0)
            left = remaining.get(tier.model_id, 0)
            bar_len = 20
            filled = int((used / tier.rpd) * bar_len) if tier.rpd else 0
            bar = "█" * filled + "░" * (bar_len - filled)
            quality_label = {3: "Pro", 2: "Flash", 1: "Lite"}[tier.quality]
            lines.append(f"  {quality_label:5s} [{bar}] {used}/{tier.rpd} (left: {left})")

        total_used = sum(usage.values())
        total_left = sum(remaining.values())
        lines.append(f"\n  Total: {total_used}/{TOTAL_FREE_RPD} used, {total_left} remaining")
        lines.append(f"  Strategy: {self._strategy}")
        return "\n".join(lines)

    def reset_today(self) -> None:
        """Reset all counters for today (testing only)."""
        today = _today_key()
        for tier in self._tiers:
            self._r.delete(f"{QUOTA_KEY_PREFIX}:{today}:{tier.model_id}")
