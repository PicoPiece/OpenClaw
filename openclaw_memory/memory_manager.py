"""Memory Manager — sliding window, summarization trigger, and Qdrant long-term storage."""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import requests
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from .config import Settings
from .summarizer import Summarizer, SummaryResult, estimate_tokens

log = logging.getLogger(__name__)


@dataclass
class Message:
    role: str
    content: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: float = field(default_factory=time.time)
    type: str = "message"  # "message" | "summary"
    source_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
            "type": self.type,
            "source_ids": self.source_ids,
        }


class MemoryManager:
    """Manages conversation history with automatic summarization and vector retrieval."""

    def __init__(
        self,
        settings: Settings,
        summarizer: Summarizer | None = None,
        qdrant_client: QdrantClient | None = None,
    ):
        self.cfg = settings
        self._summarizer = summarizer or Summarizer(settings)
        self._history: list[Message] = []
        self._summary: Message | None = None
        self._qdrant = qdrant_client
        self._embedding_dim = settings.qdrant.vector_size

        if self._qdrant is None and settings.qdrant.url:
            try:
                kwargs: dict[str, Any] = {"url": settings.qdrant.url, "timeout": 10}
                if settings.qdrant.api_key:
                    kwargs["api_key"] = settings.qdrant.api_key
                self._qdrant = QdrantClient(**kwargs)
            except Exception as exc:
                log.warning("Qdrant connection failed — long-term memory disabled: %s", exc)

        self._ensure_collection()

    # -- public API -----------------------------------------------------

    def append(self, role: str, content: str) -> Message:
        """Add a message and trigger summarization if threshold is exceeded."""
        msg = Message(role=role, content=content)
        self._history.append(msg)

        total_tokens = self._total_tokens()
        if total_tokens > self.cfg.tokens.summarize_threshold:
            log.info(
                "Token threshold exceeded (%d > %d) — summarizing",
                total_tokens,
                self.cfg.tokens.summarize_threshold,
            )
            self._run_summarization()
        return msg

    def get_recent(self, n: int | None = None) -> list[Message]:
        """Return the last *n* messages (default: SLIDING_WINDOW_SIZE)."""
        n = n or self.cfg.tokens.sliding_window_size
        return self._history[-n:]

    def get_summary(self) -> Message | None:
        return self._summary

    def get_full_context(self) -> list[Message]:
        """Summary (if any) + recent messages — ready for prompt builder."""
        parts: list[Message] = []
        if self._summary:
            parts.append(self._summary)
        parts.extend(self.get_recent())
        return parts

    def store_to_vector_db(self, conversation_block: list[Message]) -> None:
        """Embed a conversation block and upsert to Qdrant."""
        if not self._qdrant:
            log.debug("Qdrant not available — skipping vector store")
            return

        text = "\n".join(f"{m.role}: {m.content}" for m in conversation_block)
        embedding = self._embed(text)
        if embedding is None:
            return

        point_id = hashlib.md5(text.encode()).hexdigest()
        self._qdrant.upsert(
            collection_name=self.cfg.qdrant.collection,
            points=[
                PointStruct(
                    id=point_id,
                    vector=embedding,
                    payload={
                        "text": text,
                        "timestamp": time.time(),
                        "message_ids": [m.id for m in conversation_block],
                    },
                )
            ],
        )
        log.info("Stored conversation block to Qdrant (id=%s)", point_id)

    def retrieve_relevant(self, query: str, top_k: int | None = None) -> list[str]:
        """Retrieve top-k relevant memories from Qdrant."""
        top_k = top_k or self.cfg.retrieval.top_k
        if not self._qdrant:
            return []

        embedding = self._embed(query)
        if embedding is None:
            return []

        try:
            results = self._qdrant.search(
                collection_name=self.cfg.qdrant.collection,
                query_vector=embedding,
                limit=top_k,
            )
            return [hit.payload["text"] for hit in results if hit.payload]
        except Exception as exc:
            log.warning("Qdrant search failed: %s", exc)
            return []

    def clear(self) -> None:
        self._history.clear()
        self._summary = None

    # -- internals ------------------------------------------------------

    def _total_tokens(self) -> int:
        total = sum(estimate_tokens(m.content) for m in self._history)
        if self._summary:
            total += estimate_tokens(self._summary.content)
        return total

    def _run_summarization(self) -> None:
        keep = self.cfg.tokens.summary_keep_messages
        if len(self._history) <= keep:
            return

        to_summarize = self._history[:-keep]
        kept = self._history[-keep:]

        result: SummaryResult = self._summarizer.summarize(
            [m.to_dict() for m in to_summarize],
            source_ids=[m.id for m in to_summarize],
        )

        self._summary = Message(
            role="system",
            content=f"[Previous conversation summary]\n{result.text}",
            type="summary",
            source_ids=result.source_ids,
        )
        self._history = kept
        log.info(
            "Summarized %d messages into %d tokens (source=%s)",
            len(to_summarize),
            result.tokens,
            result.source,
        )

    def _embed(self, text: str) -> list[float] | None:
        """Generate embedding via Ollama embedding endpoint."""
        try:
            resp = requests.post(
                f"{self.cfg.ollama.url}/api/embeddings",
                json={"model": self.cfg.ollama.model, "prompt": text},
                timeout=30,
            )
            resp.raise_for_status()
            emb = resp.json().get("embedding")
            if emb and len(emb) != self._embedding_dim:
                log.warning(
                    "Embedding dimension mismatch: got %d, expected %d",
                    len(emb),
                    self._embedding_dim,
                )
            return emb
        except Exception as exc:
            log.warning("Embedding generation failed: %s", exc)
            return None

    def _ensure_collection(self) -> None:
        if not self._qdrant:
            return
        try:
            collections = [c.name for c in self._qdrant.get_collections().collections]
            if self.cfg.qdrant.collection not in collections:
                self._qdrant.create_collection(
                    collection_name=self.cfg.qdrant.collection,
                    vectors_config=VectorParams(
                        size=self._embedding_dim, distance=Distance.COSINE
                    ),
                )
                log.info("Created Qdrant collection: %s", self.cfg.qdrant.collection)
        except Exception as exc:
            log.warning("Qdrant collection check failed: %s", exc)
