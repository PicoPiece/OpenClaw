#!/usr/bin/env python3
"""
Monthly Strategy Review — runs day 1 of each month at 09:00 VN.

Analyzes the trailing 30 days and asks DeepSeek to suggest STRUCTURED
strategy changes (NOT auto-applied — needs user approval). Output is JSON
saved to data/reviews/monthly_*.json plus a Telegram summary.

Suggested changes the LLM may propose:
  - Coin blacklist additions (consistently losing coins)
  - Risk parameter tweaks (RISK_PER_TRADE_PCT, ATR multipliers)
  - Trail tier adjustments
  - Disable LONG/SHORT in current regime
  - Promote prompt variant B if A/B data shows uplift

Output schema (the LLM is instructed to return this JSON):
  {"summary": "...",
   "blacklist_suggestions": [{"coin": "x", "reason": "..."}],
   "param_changes": [{"name": "ATR_TP_MULT", "from": 3.0, "to": 2.5, "reason": "..."}],
   "prompt_promote": "A"|"B"|null,
   "regime_action": "LONGS_ONLY"|"SHORTS_ONLY"|"PAUSE"|"NORMAL"}
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import decision_logger  # noqa: E402

REVIEWS_DIR = SCRIPT_DIR / "data" / "reviews"
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


def gather_facts(days: int = 30) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    pnl = decision_logger.trade_pnl_stats(since=since, is_shadow=False)
    shadow = decision_logger.trade_pnl_stats(since=since, is_shadow=True)
    accuracy = decision_logger.llm_accuracy_stats(since=since)
    calibration = decision_logger.confidence_calibration(since=since)

    try:
        from prompt_registry import ab_compare
        ab = ab_compare("signal_review", since=since)
    except Exception:
        ab = {}

    with decision_logger._conn() as c:
        per_coin = c.execute(
            """SELECT coin,
                      COUNT(*) AS n,
                      SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                      SUM(pnl_usd) AS pnl,
                      SUM(r_multiple) AS r,
                      SUM(CASE WHEN result='SL_HIT' THEN 1 ELSE 0 END) AS sl_hits
               FROM trades
               WHERE closed_at >= ? AND is_shadow=0
               GROUP BY coin""",
            (since,),
        ).fetchall()
        recurring_losers = c.execute(
            """SELECT coin, COUNT(*) AS sl_streak
               FROM trades
               WHERE closed_at >= ? AND is_shadow=0 AND result='SL_HIT'
               GROUP BY coin HAVING sl_streak >= 3""",
            (since,),
        ).fetchall()

    return {
        "period": {"since": since, "days": days},
        "pnl_live": pnl,
        "pnl_shadow": shadow,
        "accuracy": accuracy,
        "calibration": calibration,
        "ab_compare": ab,
        "per_coin": [dict(r) for r in per_coin],
        "recurring_losers": [dict(r) for r in recurring_losers],
    }


def llm_suggest(facts: dict) -> dict:
    api_key = cfg("DEEPSEEK_API_KEY")
    if not api_key:
        return {"error": "no DEEPSEEK_API_KEY"}

    prompt = f"""You are a senior quant. Review the last 30 days of an algorithmic
crypto futures system. Suggest STRUCTURED strategy changes.

DATA:
{json.dumps(facts, default=str, indent=2)}

Reply ONLY in this JSON format (no markdown, no extra text):
{{
  "summary": "1-2 sentences on overall regime + system health",
  "blacklist_suggestions": [
    {{"coin": "...", "reason": "..."}}
  ],
  "param_changes": [
    {{"name": "ATR_TP_MULT|RISK_PER_TRADE_PCT|...", "from": <old>, "to": <new>, "reason": "..."}}
  ],
  "prompt_promote": "A" or "B" or null,
  "regime_action": "LONGS_ONLY" or "SHORTS_ONLY" or "PAUSE" or "NORMAL",
  "regime_reason": "...",
  "confidence": 0-100
}}"""

    body = json.dumps({
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1500,
        "temperature": 0.2,
    }).encode()
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
        text = result["choices"][0]["message"]["content"].strip()
        usage = result.get("usage", {})
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        suggestions = json.loads(text.strip())

        decision_logger.log_decision(
            source="cron_summary",
            coin=None,
            prompt=prompt,
            response=text,
            decision="STRATEGY_REVIEW",
            reason=suggestions.get("summary", "")[:200],
            confidence=suggestions.get("confidence"),
            model="deepseek-chat",
            tokens_in=usage.get("prompt_tokens"),
            tokens_out=usage.get("completion_tokens"),
            indicators={"period_days": facts["period"]["days"]},
        )
        return suggestions
    except Exception as e:
        return {"error": str(e)}


def format_telegram(facts: dict, sug: dict) -> str:
    p = facts["pnl_live"]
    bl = sug.get("blacklist_suggestions", []) or []
    pc = sug.get("param_changes", []) or []
    bl_text = "\n".join(f"  - {b['coin']}: {b['reason']}" for b in bl[:5]) or "  (none)"
    pc_text = "\n".join(
        f"  - {p['name']}: {p['from']} → {p['to']}  ({p['reason']})" for p in pc[:5]
    ) or "  (none)"

    return (
        f"*[MONTHLY STRATEGY REVIEW]* (last {facts['period']['days']} days)\n\n"
        f"Trades: {p['trades']} | WR: {p['win_rate']:.1f}% | "
        f"R: {p['total_r']:.2f} | P&L: ${p['pnl']:+.2f}\n"
        f"Regime action: *{sug.get('regime_action', 'NORMAL')}* "
        f"({sug.get('confidence', '?')}%)\n\n"
        f"Summary: {sug.get('summary', '(no summary)')[:300]}\n\n"
        f"Blacklist:\n{bl_text}\n\n"
        f"Param changes:\n{pc_text}\n\n"
        f"Prompt promote: {sug.get('prompt_promote') or 'no change'}\n"
        f"_⚠️ Cần Sư duyệt trước khi áp dụng_"
    )


def send_telegram(text: str):
    token = cfg("TELEGRAM_BOT_TOKEN")
    chat = cfg("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    payload = json.dumps({"chat_id": chat, "text": text, "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload, headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print(f"telegram send failed: {e}")


def main():
    load_env()
    dry = "--dry-run" in sys.argv
    days = 30
    for a in sys.argv:
        if a.startswith("--days="):
            days = int(a.split("=")[1])

    facts = gather_facts(days=days)
    suggestions = llm_suggest(facts)
    msg = format_telegram(facts, suggestions)
    print(msg)

    if not dry:
        REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out = REVIEWS_DIR / f"monthly_{ts}.json"
        out.write_text(json.dumps({
            "facts": facts, "suggestions": suggestions, "telegram": msg,
        }, indent=2, default=str))
        print(f"\nSaved: {out}")
        send_telegram(msg)


if __name__ == "__main__":
    main()
