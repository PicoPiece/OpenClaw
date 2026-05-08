#!/usr/bin/env python3
"""
Multi-LLM Escalation — for "critical" decisions, query multiple providers.

Used when:
  - Drawdown > 5% (high stakes)
  - RSI in extreme zone (potential reversal)
  - Position is the largest of the day
  - News event flag set in trading_control.json

The first available provider acts as primary. If escalation triggers, we ask
2-3 others (DeepSeek + Claude + Gemini if available) and aggregate the votes.

Logging: every model call is recorded via decision_logger so we can later
A/B test which provider gives the best edge per dollar.

Public API:
  should_escalate(context) -> bool
  escalate(prompt, context) -> {decision, votes, consensus, model_responses}

CLI:
  python3 multi_llm_escalator.py --test "Should we close BTC LONG at RSI 78?"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import decision_logger  # noqa: E402

ENV_FILE = SCRIPT_DIR / ".env"


def cfg(k, d=""):
    return os.environ.get(k, d)


def load_env():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


PROVIDERS = [
    {
        "name": "deepseek-chat",
        "env_key": "DEEPSEEK_API_KEY",
        "url": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-chat",
        "auth": "Bearer",
    },
    {
        "name": "deepseek-reasoner",
        "env_key": "DEEPSEEK_API_KEY",
        "url": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-reasoner",
        "auth": "Bearer",
    },
    {
        "name": "gemini-2.0-flash",
        "env_key": "GEMINI_API_KEY",
        "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
        "model": "gemini-2.0-flash",
        "auth": "google-key",
    },
    {
        "name": "groq-llama",
        "env_key": "GROQ_API_KEY",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "model": "llama-3.3-70b-versatile",
        "auth": "Bearer",
    },
]


def call_openai_compat(provider: dict, prompt: str, max_tokens: int = 200) -> dict:
    api_key = cfg(provider["env_key"])
    if not api_key:
        return {"ok": False, "error": "no api key"}
    body = json.dumps({
        "model": provider["model"],
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }).encode()
    req = urllib.request.Request(
        provider["url"], data=body,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        text = data["choices"][0]["message"]["content"].strip()
        usage = data.get("usage", {})
        return {"ok": True, "text": text,
                "tokens_in": usage.get("prompt_tokens"),
                "tokens_out": usage.get("completion_tokens")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def call_gemini(provider: dict, prompt: str, max_tokens: int = 200) -> dict:
    api_key = cfg(provider["env_key"])
    if not api_key:
        return {"ok": False, "error": "no api key"}
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.2},
    }).encode()
    url = f"{provider['url']}?key={api_key}"
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        usage = data.get("usageMetadata", {})
        return {"ok": True, "text": text,
                "tokens_in": usage.get("promptTokenCount"),
                "tokens_out": usage.get("candidatesTokenCount")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def query_provider(provider: dict, prompt: str, max_tokens: int = 200) -> dict:
    if provider["auth"] == "google-key":
        return call_gemini(provider, prompt, max_tokens)
    return call_openai_compat(provider, prompt, max_tokens)


def parse_decision(text: str) -> dict:
    """Best effort parse of {"decision": "...", "confidence": N, "reason": "..."}."""
    cleaned = text
    if "```" in cleaned:
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    try:
        return json.loads(cleaned.strip())
    except Exception:
        upper = text.upper()
        for word in ("CONFIRM", "REJECT", "HOLD", "CLOSE", "TRAIL_SL",
                      "PARTIAL_CLOSE"):
            if word in upper:
                return {"decision": word, "reason": text[:200], "confidence": 50}
        return {"decision": "UNKNOWN", "reason": text[:200], "confidence": 0}


def should_escalate(context: dict) -> bool:
    """Heuristic to decide if the current decision deserves multi-LLM consensus."""
    if context.get("force_escalate"):
        return True
    if (context.get("portfolio_drawdown_pct") or 0) >= 5:
        return True
    rsi = context.get("rsi") or 50
    if rsi >= 78 or rsi <= 22:
        return True
    if (context.get("position_usd") or 0) >= (context.get("portfolio_balance") or 100) * 0.5:
        return True
    if context.get("news_event_flag"):
        return True
    return False


def escalate(prompt: str, *, context: dict | None = None,
              max_providers: int = 3, source: str = "escalation") -> dict:
    load_env()
    context = context or {}
    available = []
    for p in PROVIDERS:
        if cfg(p["env_key"]):
            available.append(p)
        if len(available) >= max_providers:
            break

    if not available:
        return {"error": "no providers configured"}

    votes = []
    responses = []
    for p in available:
        res = query_provider(p, prompt, max_tokens=250)
        if not res.get("ok"):
            responses.append({"provider": p["name"], "error": res.get("error")})
            continue
        parsed = parse_decision(res["text"])
        votes.append(parsed.get("decision", "UNKNOWN"))
        responses.append({"provider": p["name"], "decision": parsed.get("decision"),
                          "confidence": parsed.get("confidence"),
                          "reason": parsed.get("reason"),
                          "tokens_in": res.get("tokens_in"),
                          "tokens_out": res.get("tokens_out")})
        try:
            decision_logger.log_decision(
                source=source, coin=context.get("coin"),
                direction=context.get("direction"),
                model=p["name"],
                prompt=prompt, response=res["text"],
                decision=parsed.get("decision", "UNKNOWN"),
                reason=parsed.get("reason", ""),
                confidence=parsed.get("confidence"),
                indicators=context.get("indicators"),
                market_state=context,
                tokens_in=res.get("tokens_in"),
                tokens_out=res.get("tokens_out"),
                prompt_version="multi_llm_v1",
            )
        except Exception as e:
            responses[-1]["log_error"] = str(e)

    if not votes:
        return {"error": "no successful provider responses",
                "responses": responses}

    counts: dict[str, int] = {}
    for v in votes:
        counts[v] = counts.get(v, 0) + 1
    majority = max(counts.items(), key=lambda x: x[1])
    consensus = (majority[1] >= max(2, len(votes) // 2 + 1))

    return {
        "votes": votes, "tally": counts,
        "majority_decision": majority[0],
        "consensus": consensus,
        "model_responses": responses,
        "providers_used": [p["name"] for p in available],
    }


def cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", type=str, help="prompt to test escalation with")
    ap.add_argument("--context", type=str, help="JSON context dict")
    args = ap.parse_args()
    if not args.test:
        ap.print_help(); return
    ctx = json.loads(args.context) if args.context else {}
    out = escalate(args.test, context=ctx)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    cli()
