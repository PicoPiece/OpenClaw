#!/usr/bin/env python3
"""
Outcome Linker — reconcile closed trades from executor_state.json into decisions.db.

Runs every ~15 minutes via OpenClaw cron. Idempotent.

Responsibilities:
  1. Insert missing trades from executor_state.trade_history into decisions.db
     (when the executor itself didn't record them, e.g. legacy entries or
     trades closed outside the script).
  2. For trades already in DB but missing closed_at, mark them closed using
     executor_state data.
  3. Recompute aggregate metrics so the dashboard shows up-to-date stats.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import decision_logger  # noqa: E402

EXECUTOR_STATE = SCRIPT_DIR / "data" / "executor_state.json"


def reconcile() -> dict:
    decision_logger.init_db()
    if not EXECUTOR_STATE.exists():
        return {"updated": 0, "inserted": 0, "skipped": 0}

    state = json.loads(EXECUTOR_STATE.read_text())
    history = state.get("trade_history", [])
    updated = inserted = skipped = 0

    for entry in history:
        coin = (entry.get("coin") or "").lower()
        if not coin:
            continue
        opened_at = entry.get("time")
        close_price = float(entry.get("close") or 0)
        pnl = float(entry.get("pnl") or 0)
        result = entry.get("result", "MANUAL")
        direction = entry.get("direction", "LONG")
        ent = float(entry.get("entry") or 0)

        with decision_logger._conn() as c:
            row = c.execute(
                """SELECT id, closed_at FROM trades
                   WHERE coin=? AND ABS(entry_price-?)/MAX(entry_price,?,1e-9) < 0.001
                   ORDER BY id DESC LIMIT 1""",
                (coin, ent, ent),
            ).fetchone()

        if row:
            if not row["closed_at"]:
                decision_logger.log_trade_close(
                    trade_id=row["id"],
                    close_price=close_price,
                    result=result,
                    pnl_usd=pnl,
                    notes=entry.get("note", ""),
                )
                updated += 1
            else:
                skipped += 1
        else:
            with decision_logger._conn() as c:
                c.execute(
                    """INSERT INTO trades
                       (coin, direction, entry_price, opened_at, closed_at,
                        close_price, result, pnl_usd, notes, is_shadow)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                    (coin, direction, ent, opened_at, opened_at,
                     close_price, result, pnl,
                     entry.get("note", "outcome_linker")),
                )
            inserted += 1

    rag_status = {}
    try:
        import rag_memory
        rag_status = rag_memory.rebuild_index()
    except Exception as e:
        rag_status = {"error": str(e)}

    return {"updated": updated, "inserted": inserted, "skipped": skipped,
            "total_in_history": len(history), "rag": rag_status}


if __name__ == "__main__":
    out = reconcile()
    print(json.dumps(out, indent=2))
