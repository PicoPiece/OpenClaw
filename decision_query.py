#!/usr/bin/env python3
"""
Decision Query — "Explain Why" CLI for OpenClaw.

Used by FinanceBot to answer questions like:
  - "Why did you reject SOL at 14:30?"
  - "Show me the last 10 LLM decisions for ETH"
  - "Explain decision 42"
  - "Win rate by coin since 2026-04-01?"

Usage examples:
    decision_query.py --coin sol --since 2026-04-21
    decision_query.py --id 123 --explain
    decision_query.py --stats --since 2026-04-01
    decision_query.py --recent 20
    decision_query.py --calibration

All commands emit JSON on stdout (suitable for OpenClaw tool consumption).
Add --pretty for human-readable output.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import decision_logger  # noqa: E402


def cmd_explain(args) -> dict:
    d = decision_logger.get_decision(args.id)
    if not d:
        return {"error": f"decision {args.id} not found"}
    out: dict = {"decision": d}
    if d.get("trade_id"):
        out["trade"] = decision_logger.get_trade(d["trade_id"])
    try:
        ind = json.loads(d.get("indicators_json") or "{}")
    except Exception:
        ind = {}

    if d.get("direction") and ind:
        try:
            import rag_memory
            out["rag"] = rag_memory.query(
                direction=d["direction"], rsi=ind.get("rsi"),
                ema_gap_pct=ind.get("ema_gap_pct"),
                vol_ratio=ind.get("vol_ratio"),
                atr=ind.get("atr"), entry=ind.get("entry"),
                rr=ind.get("rr"), trend=ind.get("trend"),
                k=8,
            )
        except Exception as e:
            out["rag_error"] = str(e)

    if d.get("coin") and d.get("direction") and ind:
        out["similar_past_trades"] = decision_logger.query_similar_trades(
            coin=d["coin"], direction=d["direction"],
            rsi=ind.get("rsi"), ema_gap_pct=ind.get("ema_gap_pct"),
            vol_ratio=ind.get("vol_ratio"), limit=5,
        )

    if d.get("rag_context_json"):
        try:
            out["rag_context_at_decision_time"] = json.loads(d["rag_context_json"])
        except Exception:
            pass
    return out


def cmd_list(args) -> dict:
    rows = decision_logger.query_decisions(
        coin=args.coin, source=args.source,
        since=args.since, until=args.until,
        decision=args.decision, limit=args.limit,
    )
    return {"count": len(rows), "decisions": rows}


def cmd_recent(args) -> dict:
    rows = decision_logger.query_decisions(limit=args.recent)
    return {"count": len(rows), "decisions": rows}


def cmd_trades(args) -> dict:
    rows = decision_logger.query_trades(
        coin=args.coin, direction=args.direction,
        closed_only=args.closed_only, since=args.since,
        is_shadow=args.shadow, limit=args.limit,
    )
    return {"count": len(rows), "trades": rows}


def cmd_stats(args) -> dict:
    return {
        "trades_live": decision_logger.trade_pnl_stats(since=args.since, is_shadow=False),
        "trades_shadow": decision_logger.trade_pnl_stats(since=args.since, is_shadow=True),
        "llm_accuracy": decision_logger.llm_accuracy_stats(since=args.since),
    }


def cmd_calibration(args) -> dict:
    return {"confidence_calibration": decision_logger.confidence_calibration(since=args.since)}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id", type=int, help="show full decision + outcome + similar")
    ap.add_argument("--explain", action="store_true", help="alias to enrich --id output")
    ap.add_argument("--coin", type=str, help="filter by coin (e.g. sol)")
    ap.add_argument("--direction", type=str, help="filter by LONG/SHORT")
    ap.add_argument("--source", type=str,
                    help="signal_review | position_mgmt | cron_summary")
    ap.add_argument("--decision", type=str,
                    help="filter by decision (CONFIRM/REJECT/HOLD/...)")
    ap.add_argument("--since", type=str, help="ISO timestamp lower bound")
    ap.add_argument("--until", type=str, help="ISO timestamp upper bound")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--recent", type=int, help="show N most recent decisions")
    ap.add_argument("--trades", action="store_true", help="list trades")
    ap.add_argument("--shadow", action="store_true", help="filter shadow trades")
    ap.add_argument("--closed-only", action="store_true", dest="closed_only")
    ap.add_argument("--stats", action="store_true",
                    help="aggregate trade + LLM stats")
    ap.add_argument("--calibration", action="store_true",
                    help="confidence calibration buckets")
    ap.add_argument("--pretty", action="store_true",
                    help="pretty-print JSON output")
    args = ap.parse_args()

    if args.id:
        out = cmd_explain(args)
    elif args.recent:
        out = cmd_recent(args)
    elif args.trades:
        out = cmd_trades(args)
    elif args.stats:
        out = cmd_stats(args)
    elif args.calibration:
        out = cmd_calibration(args)
    elif args.coin or args.source or args.since or args.decision:
        out = cmd_list(args)
    else:
        out = cmd_recent(argparse.Namespace(recent=10))

    print(json.dumps(out, indent=2 if args.pretty else None, default=str))


if __name__ == "__main__":
    main()
