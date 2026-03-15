"""Example: how OpenClaw should use openclaw_memory before sending an LLM request.

Run with:
    cd openclaw_memory && python -m examples.example_pipeline

Requires Redis running at REDIS_URL (default redis://localhost:6379/0).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import redis

from openclaw_memory.config import load_settings
from openclaw_memory.memory_manager import MemoryManager
from openclaw_memory.prompt_builder import PromptBuilder
from openclaw_memory.queue_worker import QueueWorker, enqueue_call, get_result
from openclaw_memory.summarizer import Summarizer


def main():
    settings = load_settings()
    summarizer = Summarizer(settings)
    mm = MemoryManager(settings, summarizer=summarizer)
    pb = PromptBuilder(settings, mm, summarizer=summarizer)

    r = redis.Redis.from_url(settings.redis.url, decode_responses=True)

    # Start a background worker
    worker = QueueWorker(settings, redis_client=r)
    worker_thread = worker.start(blocking=False)

    # --- Simulate a conversation ---

    # User sends messages
    mm.append("user", "What is the capital of France?")
    mm.append("assistant", "The capital of France is Paris.")
    mm.append("user", "Tell me more about Paris landmarks.")

    # Build prompt (with optional Qdrant retrieval for the latest query)
    prompt = pb.build(
        query="Paris landmarks",
        scratchpad="Step 1: Recall known Paris landmarks. Step 2: Provide a concise list.",
    )

    print(f"=== Built Prompt ({prompt.token_count} tokens) ===")
    print(f"Sections: {prompt.sections}")
    print(f"Truncated: {prompt.truncated}")
    print(f"---\n{prompt.text[:500]}...\n")

    # Enqueue the LLM call through Redis
    call_id = enqueue_call(
        r,
        prompt=prompt.text,
        scratchpad="Step 1: Recall known Paris landmarks.",
        priority="medium",
    )
    print(f"Enqueued call: {call_id}")

    # Wait for result
    result = get_result(r, call_id, timeout=30)
    if result:
        print(f"\n=== LLM Result ===")
        print(f"Used: {result.used} (reason: {result.reason})")
        print(f"Tokens: {result.tokens_in} in / {result.tokens_out} out")
        print(f"Text: {result.text[:300]}")
    else:
        print("No result within timeout.")

    # Store the conversation block to Qdrant for long-term memory
    mm.store_to_vector_db(mm.get_recent())

    worker.stop()
    print("\nDone.")


if __name__ == "__main__":
    main()
