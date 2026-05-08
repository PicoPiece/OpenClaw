#!/usr/bin/env python3
"""
Strategy Portfolio — multi-strategy capital allocator.

Goal: split PORTFOLIO_BALANCE across several distinct strategies so that good
edges can be scaled and bad ones starved without rewriting any single bot.

Each strategy has:
  - slug (e.g. "ema_trend_v1", "rsi_meanrev_v1", "breakout_v1", "forex_research")
  - target_pct (proposed allocation 0..1)
  - min_pct / max_pct (guardrails)
  - source filter (matches decisions.db `llm_decisions.source` and `trades.notes`)

Allocations are stored in data/strategy_portfolio.json and consumed by other
runners (binance_price_alert, shadow_trader, forex_research) via
`available_balance(strategy)`.

Performance scoring per strategy (last 30 days):
  - PnL%, win rate, profit factor, Sharpe-ish (mean / stdev of R)
  - Composite score = 0.4 * pnl_z + 0.3 * win_rate_z + 0.3 * profit_factor_z

Rebalance algorithm (advisory, never auto-applied):
  - Take composite score, normalise to weights
  - Clamp to (min_pct, max_pct) per strategy
  - Re-normalise so weights sum to 1
  - Print proposed allocation; user runs `--apply` to write file

CLI:
  python3 strategy_portfolio.py --status
  python3 strategy_portfolio.py --rebalance --days 30
  python3 strategy_portfolio.py --apply           # write last proposal to file
  python3 strategy_portfolio.py --add-strategy slug=foo target_pct=0.1 min=0.02 max=0.30
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import decision_logger  # noqa: E402

PORTFOLIO_FILE = SCRIPT_DIR / "data" / "strategy_portfolio.json"
PROPOSAL_FILE = SCRIPT_DIR / "data" / "strategy_portfolio_proposal.json"
ENV_FILE = SCRIPT_DIR / ".env"

DEFAULT_PORTFOLIO = {
    "version": 1,
    "updated_at": None,
    "total_balance_usd": 100.0,
    "strategies": [
        {"slug": "ema_trend_v1", "label": "EMA20/50 trend (crypto)",
         "source_filter": "signal_review",
         "target_pct": 0.70, "min_pct": 0.30, "max_pct": 0.90,
         "active": True},
        {"slug": "rsi_meanrev_v1", "label": "RSI mean-reversion (crypto)",
         "source_filter": "rsi_meanrev",
         "target_pct": 0.10, "min_pct": 0.0, "max_pct": 0.30,
         "active": False},
        {"slug": "breakout_v1", "label": "Breakout (crypto)",
         "source_filter": "breakout",
         "target_pct": 0.10, "min_pct": 0.0, "max_pct": 0.30,
         "active": False},
        {"slug": "forex_research", "label": "FX / Gold research-only",
         "source_filter": "forex_research",
         "target_pct": 0.10, "min_pct": 0.0, "max_pct": 0.20,
         "active": True},
    ],
}


def load_env_balance() -> float | None:
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith("PORTFOLIO_BALANCE="):
            try:
                return float(line.split("=", 1)[1].strip())
            except Exception:
                return None
    return None


def load_portfolio() -> dict:
    if not PORTFOLIO_FILE.exists():
        PORTFOLIO_FILE.parent.mkdir(parents=True, exist_ok=True)
        PORTFOLIO_FILE.write_text(json.dumps(DEFAULT_PORTFOLIO, indent=2))
        return DEFAULT_PORTFOLIO.copy()
    return json.loads(PORTFOLIO_FILE.read_text())


def save_portfolio(p: dict) -> None:
    p["updated_at"] = datetime.now(timezone.utc).isoformat()
    PORTFOLIO_FILE.parent.mkdir(parents=True, exist_ok=True)
    PORTFOLIO_FILE.write_text(json.dumps(p, indent=2))


def available_balance(strategy_slug: str) -> float:
    """Used by other scripts: how much capital this strategy can deploy."""
    p = load_portfolio()
    env_balance = load_env_balance()
    total = env_balance if env_balance is not None else p["total_balance_usd"]
    for s in p["strategies"]:
        if s["slug"] == strategy_slug and s.get("active"):
            return round(total * s["target_pct"], 2)
    return 0.0


# ---------------------------------------------------------------------------
# Performance scoring
# ---------------------------------------------------------------------------

def _stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def strategy_metrics(slug: str, source_filter: str, days: int = 30) -> dict:
    """Pull trades whose `notes` LIKE %strategy% OR linked decision.source matches."""
    decision_logger.init_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with decision_logger._conn() as c:
        rows = c.execute(
            """SELECT t.* FROM trades t
                 LEFT JOIN llm_decisions d ON t.signal_decision_id = d.id
                WHERE t.opened_at >= ?
                  AND (t.notes LIKE ? OR d.source = ?)
                  AND t.closed_at IS NOT NULL
                  AND t.is_shadow = 0""",
            (cutoff, f"%{slug}%", source_filter),
        ).fetchall()
    pnls = [r["pnl_usd"] or 0 for r in rows]
    rmults = [r["r_multiple"] for r in rows if r["r_multiple"] is not None]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    total = wins + losses
    win_rate = (wins / total * 100) if total else 0
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (
        gross_profit if gross_profit > 0 else 0)
    sharpe = (sum(rmults) / len(rmults) / _stdev(rmults)) if len(rmults) > 1 and _stdev(rmults) > 0 else 0
    return {
        "slug": slug, "trades": len(rows),
        "wins": wins, "losses": losses,
        "win_rate_pct": round(win_rate, 1),
        "pnl_usd": round(sum(pnls), 2),
        "profit_factor": round(profit_factor, 2),
        "sharpe_proxy": round(sharpe, 2),
        "avg_r": round(sum(rmults) / len(rmults), 2) if rmults else 0,
    }


def composite_score(m: dict) -> float:
    if m["trades"] < 3:
        return 0.0
    pnl_score = max(min(m["pnl_usd"] / 100, 3), -3)
    wr_score = (m["win_rate_pct"] - 50) / 25
    pf_score = max(min(math.log(m["profit_factor"] + 0.01), 2), -2)
    return round(0.4 * pnl_score + 0.3 * wr_score + 0.3 * pf_score, 3)


# ---------------------------------------------------------------------------
# Rebalance
# ---------------------------------------------------------------------------

def rebalance(days: int = 30) -> dict:
    p = load_portfolio()
    metrics = []
    for s in p["strategies"]:
        m = strategy_metrics(s["slug"], s["source_filter"], days=days)
        m["score"] = composite_score(m)
        m["current_pct"] = s["target_pct"]
        m["min_pct"] = s["min_pct"]
        m["max_pct"] = s["max_pct"]
        m["active"] = s.get("active", True)
        metrics.append(m)

    active = [m for m in metrics if m["active"]]
    scores = [max(m["score"] + 1.0, 0.05) for m in active]
    total_score = sum(scores)
    if total_score <= 0:
        for m in active:
            m["proposed_pct"] = 1.0 / len(active)
    else:
        for i, m in enumerate(active):
            raw = scores[i] / total_score
            m["proposed_pct"] = max(m["min_pct"], min(m["max_pct"], raw))
        renorm = sum(m["proposed_pct"] for m in active)
        if renorm > 0:
            for m in active:
                m["proposed_pct"] = round(m["proposed_pct"] / renorm, 4)

    inactive = [m for m in metrics if not m["active"]]
    for m in inactive:
        m["proposed_pct"] = 0.0

    proposal = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_days": days,
        "metrics": metrics,
        "strategies": [
            {"slug": m["slug"], "current_pct": m["current_pct"],
             "proposed_pct": m["proposed_pct"], "score": m["score"],
             "trades": m["trades"], "pnl_usd": m["pnl_usd"]}
            for m in metrics
        ],
    }
    PROPOSAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROPOSAL_FILE.write_text(json.dumps(proposal, indent=2))
    return proposal


def apply_proposal() -> dict:
    if not PROPOSAL_FILE.exists():
        return {"error": "no proposal — run --rebalance first"}
    proposal = json.loads(PROPOSAL_FILE.read_text())
    p = load_portfolio()
    by_slug = {x["slug"]: x for x in proposal["strategies"]}
    for s in p["strategies"]:
        if s["slug"] in by_slug:
            s["target_pct"] = by_slug[s["slug"]]["proposed_pct"]
    save_portfolio(p)
    return {"applied": True, "portfolio": p}


def add_strategy(args: list[str]) -> dict:
    kv = {}
    for a in args:
        if "=" in a:
            k, v = a.split("=", 1)
            kv[k.strip()] = v.strip()
    if "slug" not in kv:
        return {"error": "slug=... is required"}
    p = load_portfolio()
    new = {
        "slug": kv["slug"],
        "label": kv.get("label", kv["slug"]),
        "source_filter": kv.get("source_filter", kv["slug"]),
        "target_pct": float(kv.get("target_pct", 0.05)),
        "min_pct": float(kv.get("min_pct", 0.0)),
        "max_pct": float(kv.get("max_pct", 0.20)),
        "active": kv.get("active", "true").lower() == "true",
    }
    for i, s in enumerate(p["strategies"]):
        if s["slug"] == new["slug"]:
            p["strategies"][i] = new
            break
    else:
        p["strategies"].append(new)
    save_portfolio(p)
    return {"added": new}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--rebalance", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--add-strategy", nargs="+",
                    help="add or update strategy via key=value pairs")
    ap.add_argument("--available", type=str,
                    help="print available USD for a given strategy slug")
    args = ap.parse_args()

    if args.add_strategy:
        print(json.dumps(add_strategy(args.add_strategy), indent=2)); return
    if args.available:
        print(json.dumps({"slug": args.available,
                           "available_usd": available_balance(args.available)},
                          indent=2)); return
    if args.rebalance:
        print(json.dumps(rebalance(days=args.days), indent=2)); return
    if args.apply:
        print(json.dumps(apply_proposal(), indent=2)); return
    if args.status or True:
        p = load_portfolio()
        env_balance = load_env_balance()
        total = env_balance if env_balance is not None else p["total_balance_usd"]
        out = {
            "total_balance_usd": total,
            "balance_source": "env" if env_balance is not None else "file",
            "updated_at": p.get("updated_at"),
            "strategies": [
                {"slug": s["slug"], "label": s["label"],
                 "active": s.get("active", True),
                 "target_pct": s["target_pct"],
                 "allocated_usd": round(total * s["target_pct"], 2),
                 "min_pct": s["min_pct"], "max_pct": s["max_pct"]}
                for s in p["strategies"]
            ],
        }
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    cli()
