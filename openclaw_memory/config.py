"""Configuration loader — merges config.yaml defaults with environment variables."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_RE = re.compile(r"\$\{([^}]+)}")
_CFG_DIR = Path(__file__).parent


def _resolve_env(value: str) -> str:
    """Replace ${VAR} or ${VAR:-default} patterns with env values."""

    def _replace(m: re.Match) -> str:
        expr = m.group(1)
        if ":-" in expr:
            var, default = expr.split(":-", 1)
            return os.environ.get(var, default)
        return os.environ.get(expr, "")

    return _ENV_RE.sub(_replace, value)


def _walk_resolve(obj: Any) -> Any:
    if isinstance(obj, str):
        return _resolve_env(obj)
    if isinstance(obj, dict):
        return {k: _walk_resolve(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_resolve(v) for v in obj]
    return obj


@dataclass
class AIStudioCfg:
    api_key: str = ""
    model: str = "gemini-1.5-flash"
    endpoint: str = "https://generativelanguage.googleapis.com/v1beta/models"


@dataclass
class OllamaCfg:
    url: str = "http://localhost:11434"
    model: str = "llama3"


@dataclass
class RedisCfg:
    url: str = "redis://redis:6379/0"


@dataclass
class QdrantCfg:
    url: str = "http://qdrant:6333"
    api_key: str = ""
    collection: str = "openclaw_memory"
    vector_size: int = 384


@dataclass
class TokensCfg:
    max_prompt: int = 8000
    sliding_window_size: int = 10
    summarize_threshold: int = 20000
    summary_keep_messages: int = 3


@dataclass
class RateLimitCfg:
    rpm: int = 30
    tpm: int = 32000


@dataclass
class WorkerCfg:
    count: int = 2
    retry_max: int = 4
    backoff_base: int = 2


@dataclass
class FallbackCfg:
    temperature: float = 0.2
    max_output_tokens: int = 512


@dataclass
class RetrievalCfg:
    top_k: int = 3


@dataclass
class Settings:
    system_prompt: str = "You are a helpful AI assistant."
    ai_studio: AIStudioCfg = field(default_factory=AIStudioCfg)
    ollama: OllamaCfg = field(default_factory=OllamaCfg)
    redis: RedisCfg = field(default_factory=RedisCfg)
    qdrant: QdrantCfg = field(default_factory=QdrantCfg)
    tokens: TokensCfg = field(default_factory=TokensCfg)
    rate_limit: RateLimitCfg = field(default_factory=RateLimitCfg)
    worker: WorkerCfg = field(default_factory=WorkerCfg)
    fallback: FallbackCfg = field(default_factory=FallbackCfg)
    retrieval: RetrievalCfg = field(default_factory=RetrievalCfg)


def _apply_env_overrides(s: Settings) -> None:
    """Env vars take final precedence over config.yaml values."""
    env = os.environ
    if v := env.get("AI_STUDIO_API_KEY"):
        s.ai_studio.api_key = v
    if v := env.get("AI_STUDIO_ENDPOINT"):
        s.ai_studio.endpoint = v
    if v := env.get("OLLAMA_URL"):
        s.ollama.url = v
    if v := env.get("REDIS_URL"):
        s.redis.url = v
    if v := env.get("QDRANT_URL"):
        s.qdrant.url = v
    if v := env.get("QDRANT_API_KEY"):
        s.qdrant.api_key = v
    if v := env.get("MAX_PROMPT_TOKENS"):
        s.tokens.max_prompt = int(v)
    if v := env.get("SLIDING_WINDOW_SIZE"):
        s.tokens.sliding_window_size = int(v)
    if v := env.get("SUMMARIZE_THRESHOLD_TOKENS"):
        s.tokens.summarize_threshold = int(v)
    if v := env.get("SUMMARY_KEEP_MESSAGES"):
        s.tokens.summary_keep_messages = int(v)
    if v := env.get("RATE_LIMIT_RPM"):
        s.rate_limit.rpm = int(v)
    if v := env.get("RATE_LIMIT_TPM"):
        s.rate_limit.tpm = int(v)
    if v := env.get("WORKER_COUNT"):
        s.worker.count = int(v)
    if v := env.get("RETRY_MAX"):
        s.worker.retry_max = int(v)
    if v := env.get("BACKOFF_BASE"):
        s.worker.backoff_base = int(v)


def _dict_to_dataclass(cls, data: dict) -> Any:
    import dataclasses

    if not dataclasses.is_dataclass(cls):
        return data
    fields = {f.name: f for f in dataclasses.fields(cls)}
    kwargs = {}
    for name, fld in fields.items():
        if name in data:
            val = data[name]
            if dataclasses.is_dataclass(fld.type if isinstance(fld.type, type) else None):
                kwargs[name] = _dict_to_dataclass(fld.type, val) if isinstance(val, dict) else val
            else:
                kwargs[name] = val
    return cls(**kwargs)


def load_settings(config_path: str | Path | None = None) -> Settings:
    """Load settings: config.yaml -> env-var expansion -> env-var overrides."""
    path = Path(config_path) if config_path else _CFG_DIR / "config.yaml"
    raw: dict = {}
    if path.exists():
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
    raw = _walk_resolve(raw)

    s = Settings(
        system_prompt=raw.get("system_prompt", Settings.system_prompt),
        ai_studio=_dict_to_dataclass(AIStudioCfg, raw.get("ai_studio", {})),
        ollama=_dict_to_dataclass(OllamaCfg, raw.get("ollama", {})),
        redis=_dict_to_dataclass(RedisCfg, raw.get("redis", {})),
        qdrant=_dict_to_dataclass(QdrantCfg, raw.get("qdrant", {})),
        tokens=_dict_to_dataclass(TokensCfg, raw.get("tokens", {})),
        rate_limit=_dict_to_dataclass(RateLimitCfg, raw.get("rate_limit", {})),
        worker=_dict_to_dataclass(WorkerCfg, raw.get("worker", {})),
        fallback=_dict_to_dataclass(FallbackCfg, raw.get("fallback", {})),
        retrieval=_dict_to_dataclass(RetrievalCfg, raw.get("retrieval", {})),
    )
    _apply_env_overrides(s)
    return s
