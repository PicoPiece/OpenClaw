"""Redis-backed token-bucket rate limiter for RPM and TPM enforcement."""

from __future__ import annotations

import logging
import time

import redis

from .config import Settings

log = logging.getLogger(__name__)

# Lua script: atomic token-bucket consume.
# KEYS[1] = bucket key, ARGV[1] = max_tokens, ARGV[2] = refill_rate_per_sec,
# ARGV[3] = requested, ARGV[4] = now (float seconds).
# Returns: [allowed (0|1), tokens_remaining, wait_seconds_if_denied].
_LUA_CONSUME = """
local key       = KEYS[1]
local max_t     = tonumber(ARGV[1])
local rate      = tonumber(ARGV[2])
local requested = tonumber(ARGV[3])
local now       = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'tokens', 'last')
local tokens = tonumber(data[1]) or max_t
local last   = tonumber(data[2]) or now

-- refill
local elapsed = math.max(now - last, 0)
tokens = math.min(max_t, tokens + elapsed * rate)

if tokens >= requested then
    tokens = tokens - requested
    redis.call('HMSET', key, 'tokens', tokens, 'last', now)
    redis.call('EXPIRE', key, 120)
    return {1, math.floor(tokens), 0}
else
    local wait = math.ceil((requested - tokens) / rate)
    return {0, math.floor(tokens), wait}
end
"""


class RateLimiter:
    """Global rate limiter backed by Redis token buckets (RPM + TPM)."""

    def __init__(self, settings: Settings, redis_client: redis.Redis | None = None):
        self.cfg = settings.rate_limit
        self._r = redis_client or redis.Redis.from_url(
            settings.redis.url, decode_responses=True
        )
        self._script = self._r.register_script(_LUA_CONSUME)

    def acquire(self, estimated_tokens: int = 1, blocking: bool = True) -> bool:
        """Try to acquire capacity for one request + *estimated_tokens* tokens.

        If *blocking* is True, sleeps until capacity is available.
        Returns True when acquired, False if non-blocking and denied.
        """
        while True:
            rpm_ok, _, rpm_wait = self._try_bucket(
                "oc:rl:rpm", self.cfg.rpm, self.cfg.rpm / 60.0, 1
            )
            tpm_ok, _, tpm_wait = self._try_bucket(
                "oc:rl:tpm", self.cfg.tpm, self.cfg.tpm / 60.0, estimated_tokens
            )

            if rpm_ok and tpm_ok:
                return True

            if not blocking:
                return False

            wait = max(rpm_wait, tpm_wait, 1)
            log.debug("Rate limiter: sleeping %ds (rpm_wait=%d, tpm_wait=%d)", wait, rpm_wait, tpm_wait)
            time.sleep(wait)

    def _try_bucket(
        self, key: str, max_tokens: int, rate: float, requested: int
    ) -> tuple[bool, int, int]:
        result = self._script(
            keys=[key],
            args=[max_tokens, rate, requested, time.time()],
        )
        allowed, remaining, wait = int(result[0]), int(result[1]), int(result[2])
        return bool(allowed), remaining, wait

    def reset(self) -> None:
        """Reset all buckets (useful for testing)."""
        self._r.delete("oc:rl:rpm", "oc:rl:tpm")
