"""Shared fixtures for openclaw_memory tests."""

from __future__ import annotations

import pytest

from openclaw_memory.config import (
    AIStudioCfg,
    FallbackCfg,
    OllamaCfg,
    QdrantCfg,
    RateLimitCfg,
    RedisCfg,
    RetrievalCfg,
    Settings,
    TokensCfg,
    WorkerCfg,
)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        system_prompt="You are a test assistant.",
        ai_studio=AIStudioCfg(api_key="test-key", model="gemini-1.5-flash"),
        ollama=OllamaCfg(url="http://localhost:11434", model="llama3"),
        redis=RedisCfg(url="redis://localhost:6379/0"),
        qdrant=QdrantCfg(url="", api_key="", collection="test_mem", vector_size=384),
        tokens=TokensCfg(
            max_prompt=200,
            sliding_window_size=5,
            summarize_threshold=100,
            summary_keep_messages=2,
        ),
        rate_limit=RateLimitCfg(rpm=30, tpm=32000),
        worker=WorkerCfg(count=1, retry_max=3, backoff_base=2),
        fallback=FallbackCfg(temperature=0.2, max_output_tokens=512),
        retrieval=RetrievalCfg(top_k=3),
    )
