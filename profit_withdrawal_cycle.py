#!/usr/bin/env python3
"""Profit Withdrawal Cycle — milestone gate checker (advisory only).

When futures wallet hits a milestone (e.g. $300 → $500), proposes harvesting
profits into DCA / gold / stocks / futures reinvest per
data/profit_withdrawal_cycle.json and KNOWLEDGE.md SECTION:PROFIT_WITHDRAWAL_CYCLE.

NEVER auto-withdraws. User executes transfers manually after review.

Usage:
    python3 profit_withdrawal_cycle.py           # check active milestone gates
    python3 profit_withdrawal_cycle.py --json  # machine-readable output
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "data" / "profit_withdrawal_cycle.json"
CONFIG_TEMPLATE = SCRIPT_DIR / "profit_withdrawal_cycle.template.json"
EXECUTOR_STATE = SCRIPT_DIR / "data" / "executor_state.json"
TRADING_CONTROL = SCRIPT_DIR / "data" / "workspace-finance" / "trading_control.json"
ENV_FILE = SCRIPT_DIR / ".env"


def load_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def load_config() -> dict:
    cfg = load_json(CONFIG_FILE)
    if cfg:
        return cfg
    if CONFIG_TEMPLATE.exists():
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(CONFIG_TEMPLATE.read_text())
        return json.loads(CONFIG_FILE.read_text())
    return {}


def _live_futures_balance() -> float | None:
    if not ENV_FILE.exists():
        return None
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    key = env.get("BINANCE_API_KEY")
    secret = env.get("BINANCE_API_SECRET")
    if not key or not secret:
        return None
    try:
        from binance.client import Client
        c = Client(key, secret)
        return float(c.futures_account()["totalWalletBalance"])
    except Exception:
        return None


def _auto_stats_30d() -> dict:
    es = load_json(EXECUTOR_STATE)
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    trades = []
    for t in es.get("trade_history", []):
        if (t.get("source") or "auto") != "auto":
            continue
        ts = t.get("time", "")
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            continue
        if dt >= cutoff:
            trades.append(t)
    pnls = [float(t.get("pnl") or 0) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gp = sum(wins)
    gl = abs(sum(losses))
    pf = gp / gl if gl else float("inf")
    return {
        "n": len(trades),
        "net_pnl": round(sum(pnls), 2),
        "profit_factor": round(pf, 2) if pf != float("inf") else 999.0,
        "wr": round(len(wins) / len(pnls) * 100, 1) if pnls else 0,
    }


def evaluate_milestone(ms: dict, balance: float | None) -> dict:
    gates = ms.get("gates", {})
    stats = _auto_stats_30d()
    target = float(ms.get("target_futures_balance_usd", 0))
    start = float(ms.get("start_baseline_usd", 0))

    checks = []
    if balance is not None:
        ok_bal = balance >= target
        checks.append({
            "gate": f"Futures balance ≥ ${target:.0f}",
            "value": f"${balance:.2f}",
            "pass": ok_bal,
        })
    else:
        checks.append({"gate": f"Futures balance ≥ ${target:.0f}", "value": "N/A", "pass": False})

    min_pnl = float(gates.get("min_auto_pnl_30d_usd", 0))
    checks.append({
        "gate": f"Auto net 30d ≥ ${min_pnl:.0f}",
        "value": f"${stats['net_pnl']:.2f} ({stats['n']}t)",
        "pass": stats["net_pnl"] >= min_pnl,
    })

    min_pf = float(gates.get("min_auto_profit_factor_30d", 1.0))
    checks.append({
        "gate": f"Auto PF 30d ≥ {min_pf}",
        "value": str(stats["profit_factor"]),
        "pass": stats["profit_factor"] >= min_pf,
    })

    min_n = int(gates.get("min_auto_trades_30d", 0))
    checks.append({
        "gate": f"Auto trades 30d ≥ {min_n}",
        "value": str(stats["n"]),
        "pass": stats["n"] >= min_n,
    })

    ctrl = load_json(TRADING_CONTROL)
    checks.append({
        "gate": "Auto-trade enabled",
        "value": str(ctrl.get("auto_trade_enabled", True)),
        "pass": ctrl.get("auto_trade_enabled", True) is not False,
    })

    all_pass = all(c["pass"] for c in checks)
    alloc = ms.get("allocation_on_trigger", {})
    profit = (balance or 0) - start if balance else 0

    return {
        "milestone_id": ms.get("id"),
        "label": ms.get("label"),
        "status": ms.get("status"),
        "start_baseline_usd": start,
        "target_usd": target,
        "current_balance": balance,
        "unrealized_vs_start": round(profit, 2),
        "allocation": alloc,
        "auto_stats_30d": stats,
        "gates": checks,
        "ready": all_pass and ms.get("status") == "pending",
        "note": "Sustained-days gate not automated — confirm balance held ≥3 days manually.",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cfg = load_json(CONFIG_FILE)
    mid = cfg.get("active_milestone_id", "M1")
    ms = next((m for m in cfg.get("milestones", []) if m.get("id") == mid), None)
    if not ms:
        print(f"No milestone {mid} in {CONFIG_FILE}")
        sys.exit(1)

    balance = _live_futures_balance()
    report = evaluate_milestone(ms, balance)

    if args.json:
        print(json.dumps(report, indent=2))
        return

    print(f"=== Profit Withdrawal Cycle — {mid} ===")
    print(f"  {report['label']}")
    print(f"  Target: ${report['target_usd']:.0f} | Current: "
          f"${report['current_balance']:.2f}" if report["current_balance"] else "  Current: N/A")
    print(f"  vs start ${report['start_baseline_usd']:.0f}: "
          f"${report['unrealized_vs_start']:+.2f}")
    print()
    print("  Gates:")
    for g in report["gates"]:
        mark = "✓" if g["pass"] else "✗"
        print(f"    {mark} {g['gate']}: {g['value']}")
    print(f"  {report['note']}")
    print()
    if report["allocation"]:
        a = report["allocation"]
        print("  Allocation on execute:")
        print(f"    DCA coin:      ${a.get('dca_coin_usd', 0):.0f}")
        print(f"    Physical gold: ${a.get('physical_gold_usd', 0):.0f}")
        print(f"    Stocks:        ${a.get('stocks_usd', 0):.0f}")
        print(f"    Futures reinvest:${a.get('futures_reinvest_usd', 0):.0f}")
    print()
    if report["ready"]:
        print("  STATUS: Gates pass (except sustained-days) — USER confirm before withdraw.")
    else:
        print("  STATUS: Not ready — keep running engine.")


if __name__ == "__main__":
    main()
