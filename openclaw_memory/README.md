# openclaw_memory

Context management, rate limiting, and fallback handling module for OpenClaw.

## Architecture

```
User Message
     │
     ▼
┌─────────────────┐     ┌──────────────┐
│ memory_manager   │────▶│   Qdrant     │  long-term vector memory
│  append()        │◀────│   (retrieve) │
│  get_recent()    │     └──────────────┘
│  summarize()     │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ prompt_builder   │  system + summary + memories + messages + scratchpad
│  build()         │  enforces MAX_PROMPT_TOKENS
└────────┬────────┘
         │
         ▼
┌─────────────────┐     ┌──────────────┐
│ queue_worker     │────▶│   Redis      │  job queue + token-bucket rate limiter
│  enqueue_call()  │◀────│   (queue)    │
│  process_one()   │     └──────────────┘
└────────┬────────┘
         │
    ┌────┴────┐
    ▼         ▼
 Cloud      Ollama
(Gemini)   (fallback)
```

## Quick Start

```bash
cd openclaw_memory

# Install dependencies
pip install -r requirements.txt

# Run tests (no external services needed)
pytest tests/ -v

# Start infrastructure
cd docker && docker compose up -d

# Run example pipeline
python -m examples.example_pipeline
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `AI_STUDIO_API_KEY` | (required) | Google AI Studio API key |
| `AI_STUDIO_ENDPOINT` | `https://generativelanguage.googleapis.com/v1beta/models` | Gemini API base URL |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint for fallback |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection string |
| `QDRANT_URL` | `http://qdrant:6333` | Qdrant vector DB URL |
| `QDRANT_API_KEY` | (empty) | Qdrant API key (optional) |
| `MAX_PROMPT_TOKENS` | `8000` | Maximum tokens in final prompt |
| `SLIDING_WINDOW_SIZE` | `10` | Recent messages to keep |
| `SUMMARIZE_THRESHOLD_TOKENS` | `20000` | Trigger summarization above this |
| `SUMMARY_KEEP_MESSAGES` | `3` | Messages kept after summarization |
| `RATE_LIMIT_RPM` | `30` | Max requests per minute |
| `RATE_LIMIT_TPM` | `32000` | Max tokens per minute |
| `WORKER_COUNT` | `2` | Queue worker threads |
| `RETRY_MAX` | `4` | Max retries before fallback |
| `BACKOFF_BASE` | `2` | Exponential backoff base (seconds) |

## Integration with OpenClaw

Call `memory_manager.build_prompt()` before every LLM request and `enqueue_call()` to send through the rate-limited queue:

```python
from openclaw_memory.config import load_settings
from openclaw_memory.memory_manager import MemoryManager
from openclaw_memory.prompt_builder import PromptBuilder
from openclaw_memory.queue_worker import enqueue_call
from openclaw_memory.summarizer import Summarizer

settings = load_settings()
summarizer = Summarizer(settings)
mm = MemoryManager(settings, summarizer=summarizer)
pb = PromptBuilder(settings, mm, summarizer=summarizer)

# On every incoming message:
mm.append("user", user_message)
prompt = pb.build(query=user_message, scratchpad=reasoning_state)
call_id = enqueue_call(redis_client, prompt.text, scratchpad=reasoning_state)

# On conversation end:
mm.store_to_vector_db(mm.get_recent())
```

## CLI Tool

```bash
# Summarize a conversation file
python -m openclaw_memory.cli summarize --file conversation.json

# Summarize plain text
python -m openclaw_memory.cli summarize --text "Long conversation content..."

# Retrieve relevant memories
python -m openclaw_memory.cli retrieve --q "search terms" --k 3
```

## Behaviors

### Summarization Policy

When `total_prompt_tokens > SUMMARIZE_THRESHOLD_TOKENS`:
1. Calls `summarizer.summarize(older_messages)` (cloud first, Ollama fallback)
2. Replaces old messages with a compact summary object
3. Keeps the last `SUMMARY_KEEP_MESSAGES` messages intact
4. Summary placed at the top of the history

### Prompt Composition Order

1. System prompt (always included)
2. Conversation summary (if exists, compressed to fit budget)
3. Relevant memories from Qdrant (top-k)
4. Last N messages (sliding window)
5. Scratchpad (reasoning state)

Token budget enforced by truncating oldest messages first, then compressing summary.

### Queue & Rate Limiter

- All outbound LLM calls go through `enqueue_call()`
- Worker checks Redis token-bucket before sending
- On 429: parse `Retry-After`, exponential backoff, re-enqueue with delay
- After `RETRY_MAX` fails: switch to Ollama with conservative settings
- Responses annotated: `{"used": "cloud"|"fallback", "reason": "429"|"timeout"}`

### Fallback Continuity

- Fallback receives the exact same prompt
- If local model cannot handle token size, prompt is compressed via summarizer
- Conservative defaults: temperature=0.2, max_output_tokens=512

## Monitoring

Suggested Prometheus metric names:
- `llm_requests_total` — total LLM calls (label: `model`)
- `llm_429_total` — rate-limited responses
- `llm_fallback_total` — fallback invocations
- `prompt_tokens_sent` — histogram of prompt token counts

## Security

- API keys loaded from env vars only, never hardcoded
- `.env` files should have `chmod 600`
- 429 events logged with counts and `Retry-After` values
- All ports in docker-compose bind to `127.0.0.1`

## Tests

```bash
# Run all tests (mocked, no external services needed)
pytest tests/ -v

# Run specific test
pytest tests/test_queue_worker.py -v -k "test_429"
```

Tests mock:
- Cloud API returning 429 with Retry-After header
- Cloud API success
- Local Ollama endpoint returning success
- Qdrant upsert/query (in-memory stubs)
