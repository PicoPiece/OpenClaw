#!/usr/bin/env python3
"""
Weekly LLM Review — runs Sundays 21:00 VN.

Reads last 7 days of llm_decisions + linked trades, computes:
  - TP/SL/timeout rates per source
  - False positive (CONFIRM -> SL) and false negative (REJECT -> would-have-TP, requires shadow)
  - Per-coin LLM accuracy
  - Confidence calibration drift
Asks DeepSeek to suggest prompt tweaks based on the data.
Sends Telegram report and writes a JSON summary to data/reviews/.

Usage:
    python3 weekly_llm_review.py            # generate + send + save
    python3 weekly_llm_review.py --dry-run  # just print the report
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


def cfg(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def load_env():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def compute_review(days: int = 7) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    pnl = decision_logger.trade_pnl_stats(since=since, is_shadow=False)
    accuracy = decision_logger.llm_accuracy_stats(since=since)
    calibration = decision_logger.confidence_calibration(since=since)

    with decision_logger._conn() as c:
        false_positives = c.execute(
            """SELECT d.coin, d.confidence, d.reason, t.pnl_usd
               FROM llm_decisions d JOIN trades t ON d.trade_id = t.id
               WHERE d.decision='CONFIRM' AND t.result='SL_HIT'
                 AND d.ts >= ? AND t.is_shadow=0
               ORDER BY t.pnl_usd ASC LIMIT 5""",
            (since,),
        ).fetchall()
        big_wins = c.execute(
            """SELECT d.coin, d.confidence, d.reason, t.pnl_usd, t.r_multiple
               FROM llm_decisions d JOIN trades t ON d.trade_id = t.id
               WHERE d.decision='CONFIRM' AND t.result='TP_HIT'
                 AND d.ts >= ? AND t.is_shadow=0
               ORDER BY t.pnl_usd DESC LIMIT 5""",
            (since,),
        ).fetchall()
        per_coin = c.execute(
            """SELECT t.coin,
                      COUNT(*) AS n,
                      SUM(CASE WHEN t.pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                      SUM(t.pnl_usd) AS pnl
               FROM trades t WHERE t.closed_at >= ? AND t.is_shadow=0
               GROUP BY t.coin ORDER BY pnl ASC""",
            (since,),
        ).fetchall()

    return {
        "period": {"since": since, "days": days},
        "pnl": pnl,
        "accuracy": accuracy,
        "calibration": calibration,
        "false_positives": [dict(r) for r in false_positives],
        "big_wins": [dict(r) for r in big_wins],
        "per_coin": [dict(r) for r in per_coin],
    }


def llm_suggest_tweaks(review: dict) -> str:
    """Ask DeepSeek to suggest prompt tweaks based on review."""
    api_key = cfg("DEEPSEEK_API_KEY")
    if not api_key:
        return "(no DEEPSEEK_API_KEY — skipping LLM suggestions)"

    summary = json.dumps({
        "win_rate": review["pnl"]["win_rate"],
        "total_r": review["pnl"]["total_r"],
        "by_coin": review["pnl"]["by_coin"],
        "top_3_losses": review["false_positives"][:3],
        "top_3_wins": review["big_wins"][:3],
        "calibration": review["calibration"],
    }, default=str, indent=2)

    prompt = f"""You are a trading strategy auditor. Below is a 7-day performance summary
of an algorithmic crypto futures system whose entries are reviewed by DeepSeek.

DATA:
{summary}

Suggest 3-5 SPECIFIC prompt tweaks (not generic advice) that would improve LLM
decision quality. Focus on:
- Patterns in CONFIRM->SL losers (false positives)
- Confidence calibration drift (LLM over/under confident?)
- Coin-specific issues
- Whether to tighten/loosen entry rules

Reply concise, max 200 words, bullet points."""

    body = json.dumps({
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 500,
        "temperature": 0.3,
    }).encode()
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        text = result["choices"][0]["message"]["content"].strip()
        usage = result.get("usage", {})
        decision_logger.log_decision(
            source="cron_summary",
            coin=None,
            prompt=prompt,
            response=text,
            decision="REPORT",
            reason="weekly_llm_review",
            model="deepseek-chat",
            tokens_in=usage.get("prompt_tokens"),
            tokens_out=usage.get("completion_tokens"),
            indicators={"period_days": review["period"]["days"]},
        )
        return text
    except Exception as e:
        return f"(LLM error: {e})"


def format_telegram(review: dict, suggestions: str) -> str:
    p = review["pnl"]
    cal_lines = []
    for c in review["calibration"]:
        if c["n"]:
            cal_lines.append(f"{c['bucket']}: {c['n']}t WR {c['win_rate']}%")
    cal = " | ".join(cal_lines) or "(no data)"

    coin_lines = []
    for c in review["per_coin"][:5]:
        coin_lines.append(f"  {c['coin'].upper()}: {c['wins']}/{c['n']} = ${c['pnl']:+.2f}")
    worst = "\n".join(coin_lines) or "  (no trades)"

    return (
        f"*[WEEKLY LLM REVIEW] last {review['period']['days']} days*\n\n"
        f"Trades: {p['trades']} | Win rate: {p['win_rate']:.1f}% | "
        f"Total R: {p['total_r']:.2f} | P&L: ${p['pnl']:+.2f}\n"
        f"Calibration: {cal}\n\n"
        f"Worst coins:\n{worst}\n\n"
        f"*LLM Suggestions:*\n{suggestions[:1500]}"
    )


def send_telegram(text: str):
    token = cfg("TELEGRAM_BOT_TOKEN")
    chat = cfg("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("(no telegram creds — skipping send)")
        return
    payload = json.dumps({"chat_id": chat, "text": text, "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"telegram send failed: {e}")


def main():
    load_env()
    dry = "--dry-run" in sys.argv
    days = 7
    for a in sys.argv:
        if a.startswith("--days="):
            days = int(a.split("=")[1])

    review = compute_review(days=days)
    suggestions = llm_suggest_tweaks(review)
    msg = format_telegram(review, suggestions)
    print(msg)

    if not dry:
        REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_file = REVIEWS_DIR / f"weekly_{ts}.json"
        out_file.write_text(json.dumps({
            "review": review, "suggestions": suggestions, "telegram": msg,
        }, indent=2, default=str))
        print(f"\nSaved: {out_file}")
        send_telegram(msg)


if __name__ == "__main__":
    main()
