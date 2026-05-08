#!/usr/bin/env python3
"""
Token Budget Guard — tracks LLM spend against monthly budget.

Budget records live in decisions.db `ai_budget` table (set by
monthly_profit_split.py). LLM call costs are recorded in `llm_decisions.cost_usd`
by decision_logger.

Public API:
  current_budget() -> {budget, spent, remaining, period}
  remaining_pct() -> 0..100
  can_afford(cost) -> bool
  alert_if_low(threshold_pct=20) -> sends Telegram if remaining < threshold
  enforce(hard_block=True) -> raises if budget exhausted

CLI:
  python3 token_budget_guard.py --status
  python3 token_budget_guard.py --set 50      # set this month's budget to $50
  python3 token_budget_guard.py --add 10      # top up by $10 (manual deposit)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
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


def current_period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def get_or_create_budget(period: str | None = None,
                          default_budget: float = 20.0,
                          source: str = "manual") -> dict:
    period = period or current_period()
    decision_logger.init_db()
    with decision_logger._conn() as c:
        row = c.execute(
            "SELECT * FROM ai_budget WHERE period=?", (period,)
        ).fetchone()
        if row:
            return dict(row)
        now_iso = datetime.now(timezone.utc).isoformat()
        c.execute(
            """INSERT INTO ai_budget (period, budget_usd, spent_usd, source,
                                      created_at, updated_at)
               VALUES (?, ?, 0, ?, ?, ?)""",
            (period, default_budget, source, now_iso, now_iso),
        )
        c.commit()
        row = c.execute(
            "SELECT * FROM ai_budget WHERE period=?", (period,)
        ).fetchone()
        return dict(row) if row else {
            "period": period, "budget_usd": default_budget,
            "spent_usd": 0, "source": source,
        }


def update_budget(period: str, *, budget: float | None = None,
                   spent_delta: float = 0, source: str | None = None):
    decision_logger.init_db()
    now_iso = datetime.now(timezone.utc).isoformat()
    with decision_logger._conn() as c:
        existing = c.execute("SELECT * FROM ai_budget WHERE period=?", (period,)).fetchone()
        if not existing:
            c.execute(
                """INSERT INTO ai_budget (period, budget_usd, spent_usd, source,
                                          created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (period, budget or 0, spent_delta, source or "manual",
                 now_iso, now_iso),
            )
        else:
            new_budget = budget if budget is not None else existing["budget_usd"]
            new_spent = existing["spent_usd"] + spent_delta
            c.execute(
                """UPDATE ai_budget
                   SET budget_usd=?, spent_usd=?, source=COALESCE(?,source), updated_at=?
                   WHERE period=?""",
                (new_budget, new_spent, source, now_iso, period),
            )


def actual_spent(period: str | None = None) -> float:
    """Sum of cost_usd from llm_decisions in period."""
    period = period or current_period()
    start = period + "-01"
    decision_logger.init_db()
    with decision_logger._conn() as c:
        row = c.execute(
            "SELECT COALESCE(SUM(cost_usd),0) AS s FROM llm_decisions WHERE ts >= ?",
            (start,),
        ).fetchone()
    return row["s"] or 0


def current_budget() -> dict:
    period = current_period()
    b = get_or_create_budget(period)
    spent = actual_spent(period)
    if abs(spent - (b.get("spent_usd") or 0)) > 0.01:
        update_budget(period, spent_delta=spent - (b.get("spent_usd") or 0))
    budget_usd = b["budget_usd"] or 0
    return {
        "period": period,
        "budget_usd": round(budget_usd, 4),
        "spent_usd": round(spent, 4),
        "remaining_usd": round(budget_usd - spent, 4),
        "remaining_pct": round((budget_usd - spent) / budget_usd * 100, 1)
            if budget_usd > 0 else 0,
        "source": b["source"],
    }


def remaining_pct() -> float:
    return current_budget()["remaining_pct"]


def can_afford(cost: float) -> bool:
    b = current_budget()
    return b["remaining_usd"] >= cost


def enforce(hard_block: bool = True):
    """Raise RuntimeError if budget exhausted. Pass hard_block=False for warn-only."""
    b = current_budget()
    if b["remaining_usd"] <= 0:
        msg = f"AI budget exhausted for {b['period']} (spent ${b['spent_usd']:.2f} / ${b['budget_usd']:.2f})"
        if hard_block:
            raise RuntimeError(msg)
        print(f"[BUDGET WARN] {msg}")


def alert_if_low(threshold_pct: float = 20):
    load_env()
    b = current_budget()
    if b["budget_usd"] <= 0:
        return
    if b["remaining_pct"] > threshold_pct:
        return
    token = cfg("TELEGRAM_BOT_TOKEN")
    chat = cfg("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    text = (f"⚠️ *AI Budget Alert*\n"
            f"Period: {b['period']}\n"
            f"Spent: ${b['spent_usd']:.2f} / ${b['budget_usd']:.2f}\n"
            f"Remaining: ${b['remaining_usd']:.2f} ({b['remaining_pct']:.1f}%)")
    payload = json.dumps({"chat_id": chat, "text": text, "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload, headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"telegram alert failed: {e}")


def cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--set", type=float, dest="set_budget",
                    help="set this month's total budget USD")
    ap.add_argument("--add", type=float, help="add to budget (top up)")
    ap.add_argument("--source", type=str, default=None)
    ap.add_argument("--alert", action="store_true", help="send Telegram if low")
    args = ap.parse_args()

    period = current_period()
    if args.set_budget is not None:
        update_budget(period, budget=args.set_budget, source=args.source)
    if args.add is not None:
        cur = get_or_create_budget(period)
        update_budget(period, budget=cur["budget_usd"] + args.add, source=args.source)
    if args.alert:
        alert_if_low()
    print(json.dumps(current_budget(), indent=2))


if __name__ == "__main__":
    cli()
