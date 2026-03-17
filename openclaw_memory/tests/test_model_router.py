"""Tests for model_router — quota tracking, model selection, and strategy behavior."""

from __future__ import annotations

import pytest

from openclaw_memory.model_router import ModelRouter, ModelTier


TIERS = [
    ModelTier("pro", rpm=5, rpd=3, quality=3),
    ModelTier("flash", rpm=10, rpd=5, quality=2),
    ModelTier("lite", rpm=15, rpd=10, quality=1),
]


class FakeRedis:
    """Minimal Redis stub for testing without a real server."""

    def __init__(self):
        self._store: dict[str, str] = {}

    def get(self, key):
        return self._store.get(key)

    def incrby(self, key, amount):
        val = int(self._store.get(key, "0")) + amount
        self._store[key] = str(val)
        return val

    def expire(self, key, ttl):
        pass

    def delete(self, *keys):
        for k in keys:
            self._store.pop(k, None)

    def pipeline(self):
        return FakePipeline(self)

    @classmethod
    def from_url(cls, *a, **kw):
        return cls()


class FakePipeline:
    def __init__(self, r: FakeRedis):
        self._r = r
        self._ops: list = []

    def incrby(self, key, amount):
        self._ops.append(("incrby", key, amount))

    def expire(self, key, ttl):
        self._ops.append(("expire", key, ttl))

    def execute(self):
        results = []
        for op in self._ops:
            if op[0] == "incrby":
                results.append(self._r.incrby(op[1], op[2]))
            elif op[0] == "expire":
                self._r.expire(op[1], op[2])
                results.append(True)
        return results


class TestModelRouter:
    def _make_router(self, strategy: str = "quality_first") -> tuple[ModelRouter, FakeRedis]:
        fake_r = FakeRedis()
        router = ModelRouter(redis_client=fake_r, strategy=strategy, tiers=TIERS)
        return router, fake_r

    def test_select_model_quality_first(self):
        router, _ = self._make_router("quality_first")
        model = router.select_model()
        assert model is not None
        assert model.model_id == "pro"

    def test_select_model_economy(self):
        router, _ = self._make_router("economy")
        model = router.select_model()
        assert model is not None
        assert model.model_id == "lite"

    def test_select_model_balanced(self):
        router, _ = self._make_router("balanced")
        model = router.select_model()
        assert model is not None
        assert model.model_id == "flash"

    def test_complex_prefers_pro(self):
        router, _ = self._make_router("economy")
        model = router.select_model(complexity="complex")
        assert model.model_id == "pro"

    def test_simple_prefers_lite(self):
        router, _ = self._make_router("quality_first")
        model = router.select_model(complexity="simple")
        assert model.model_id == "lite"

    def test_fallback_when_pro_exhausted(self):
        router, _ = self._make_router("quality_first")
        for _ in range(3):
            router.record_usage("pro")
        model = router.select_model()
        assert model.model_id == "flash"

    def test_fallback_chain_exhaustion(self):
        router, _ = self._make_router("quality_first")
        for _ in range(3):
            router.record_usage("pro")
        for _ in range(5):
            router.record_usage("flash")
        model = router.select_model()
        assert model.model_id == "lite"

    def test_all_exhausted_returns_none(self):
        router, _ = self._make_router("quality_first")
        for _ in range(3):
            router.record_usage("pro")
        for _ in range(5):
            router.record_usage("flash")
        for _ in range(10):
            router.record_usage("lite")
        model = router.select_model()
        assert model is None

    def test_record_usage_increments(self):
        router, _ = self._make_router()
        router.record_usage("pro")
        router.record_usage("pro")
        usage = router.get_usage()
        assert usage["pro"] == 2

    def test_get_remaining(self):
        router, _ = self._make_router()
        router.record_usage("pro", 2)
        remaining = router.get_remaining()
        assert remaining["pro"] == 1
        assert remaining["flash"] == 5
        assert remaining["lite"] == 10

    def test_total_remaining(self):
        router, _ = self._make_router()
        assert router.get_total_remaining() == 18  # 3+5+10
        router.record_usage("pro", 3)
        assert router.get_total_remaining() == 15

    def test_format_status_contains_info(self):
        router, _ = self._make_router()
        router.record_usage("pro", 1)
        status = router.format_status()
        assert "Pro" in status
        assert "Flash" in status
        assert "Lite" in status
        assert "1/3" in status
        assert "quality_first" in status

    def test_reset_clears_counters(self):
        router, _ = self._make_router()
        router.record_usage("pro", 3)
        router.reset_today()
        assert router.get_usage()["pro"] == 0
