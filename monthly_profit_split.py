#!/usr/bin/env python3
"""
Monthly Profit Split — runs day 1 of month at 09:30 VN.

Reads previous month's PnL from decisions.db. If positive, splits:
  - 70% reinvest (proposes new PORTFOLIO_BALANCE — needs user --apply)
  - 15% AI budget (auto-credits ai_budget table for current month)
  - 15% withdraw (logged in profit_splits table; user pulls manually)

If PnL negative, NO split, NO topup. AI budget falls back to manual.

Usage:
    python3 monthly_profit_split.py             # compute + propose, save record
    python3 monthly_profit_split.py --apply     # actually credit AI budget + write env proposal
    python3 monthly_profit_split.py --dry-run   # report only
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import decision_logger  # noqa: E402

REVIEWS_DIR = SCRIPT_DIR / "data" / "reviews"


def previous_month() -> str:
    today = datetime.now(timezone.utc)
    first_of_this = today.replace(day=1)
    last_of_prev = first_of_this - timedelta(days=1)
    return last_of_prev.strftime("%Y-%m")


def month_pnl(period: str) -> dict:
    decision_logger.init_db()
    start = period + "-01"
    next_year, next_month = (int(period[:4]), int(period[5:7]) + 1)
    if next_month > 12:
        next_year += 1
        next_month = 1
    end = f"{next_year:04d}-{next_month:02d}-01"

    with decision_logger._conn() as c:
        rows = c.execute(
            """SELECT pnl_usd FROM trades
               WHERE closed_at >= ? AND closed_at < ? AND is_shadow=0""",
            (start, end),
        ).fetchall()
    pnls = [r["pnl_usd"] or 0 for r in rows]
    return {"period": period, "trades": len(pnls),
            "gross_pnl": round(sum(pnls), 4),
            "wins": sum(1 for p in pnls if p > 0),
            "losses": sum(1 for p in pnls if p < 0)}


def compute_split(pnl: float, *, reinvest_pct: float = 70,
                   ai_pct: float = 15, withdraw_pct: float = 15) -> dict:
    if pnl <= 0:
        return {"reinvest": 0, "ai_budget": 0, "withdraw": 0, "skipped": True,
                "reason": "non-positive monthly PnL"}
    r = round(pnl * reinvest_pct / 100, 4)
    a = round(pnl * ai_pct / 100, 4)
    w = round(pnl * withdraw_pct / 100, 4)
    return {"reinvest": r, "ai_budget": a, "withdraw": w, "skipped": False}


def record_split(period: str, pnl: float, split: dict):
    decision_logger.init_db()
    with decision_logger._conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO profit_splits
               (period, gross_pnl_usd, reinvest_usd, ai_budget_usd, withdraw_usd, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (period, pnl, split.get("reinvest"), split.get("ai_budget"),
             split.get("withdraw"), datetime.now(timezone.utc).isoformat()),
        )


def credit_ai_budget(amount: float):
    """Add `amount` to the CURRENT month's AI budget."""
    if amount <= 0:
        return
    import token_budget_guard as tbg
    cur = tbg.current_period()
    existing = tbg.get_or_create_budget(cur, default_budget=0,
                                         source="profit_split")
    tbg.update_budget(cur,
                      budget=(existing["budget_usd"] or 0) + amount,
                      source="profit_split")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--period", type=str, default=None,
                    help="YYYY-MM, default = previous month")
    args = ap.parse_args()

    period = args.period or previous_month()
    pnl_info = month_pnl(period)
    split = compute_split(pnl_info["gross_pnl"])

    out = {"pnl": pnl_info, "split": split, "applied": False}

    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    out_file = REVIEWS_DIR / f"profit_split_{period}.json"

    if args.apply and not split.get("skipped"):
        record_split(period, pnl_info["gross_pnl"], split)
        credit_ai_budget(split["ai_budget"])
        out["applied"] = True
        out["next_steps"] = [
            f"Update PORTFOLIO_BALANCE += ${split['reinvest']:.2f} via capital_scaling.py --apply <new>",
            f"Withdraw ${split['withdraw']:.2f} from Binance manually if desired",
            f"AI budget for current month auto-credited ${split['ai_budget']:.2f}",
        ]

    out_file.write_text(json.dumps(out, indent=2, default=str))
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
