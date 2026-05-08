#!/usr/bin/env python3
"""Self-Sustainability Index (ASI) calculator.

Tracks whether the AI trading agent generates enough profit to cover its
LLM infrastructure costs. ASI = monthly_profit / monthly_cost.

Profit sources:
  - OpenClaw Futures (executor_state.json: trade_history since tracking_since)
  - Spot Grid bots (wallet_balance_history.json: Trading Bots delta)
  - Earn passive (Simple Earn estimated APY × balance)

Cost sources:
  - DeepSeek API (deepseek_cost_state.json: cumulative_spent rate)
  - Cursor Pro subscription ($20/month assumed — configurable via env)
  - Anthropic API ($0/month if not used; configurable via env)

Usage:
  python3 self_sustainability.py            # print summary
  python3 self_sustainability.py --json     # machine-readable
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
ENV_FILE = ROOT / ".env"

EXECUTOR_STATE = DATA / "executor_state.json"
DEEPSEEK_STATE = DATA / "deepseek_cost_state.json"
WALLET_HISTORY = DATA / "wallet_balance_history.json"
GRID_CONFIG = DATA / "grid_config.json"

DEFAULT_CURSOR_USD_MO = 20.0
DEFAULT_ANTHROPIC_USD_MO = 0.0
EARN_APY = 0.005


def load_env():
    if not ENV_FILE.exists(): return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def parse_iso(s: str) -> datetime | None:
    if not s: return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def safe_load(path: Path):
    if not path.exists(): return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def compute_futures_profit() -> dict:
    state = safe_load(EXECUTOR_STATE) or {}
    tracking_since = parse_iso(state.get("tracking_since", ""))
    total_pnl = float(state.get("total_pnl", 0))
    if not tracking_since:
        return {"total_pnl": total_pnl, "days": 0, "monthly_rate": 0, "tracking_since": None}
    days = max(1, (datetime.now(timezone.utc) - tracking_since).days + 1)
    monthly_rate = total_pnl / days * 30
    return {
        "total_pnl": round(total_pnl, 2),
        "days": days,
        "monthly_rate": round(monthly_rate, 2),
        "tracking_since": tracking_since.isoformat(timespec="seconds"),
    }


def compute_grid_profit() -> dict:
    """Compute grid P&L using grid_config.json invested_usd as anchor.

    profit = current_trading_bots_wallet - sum(grid_config[*].invested_usd)

    Falls back to 24h wallet delta extrapolation if config has no invested data.
    Wallet transfers (user manually opening/closing bots) are NOT counted as P&L.
    """
    history = safe_load(WALLET_HISTORY) or {}
    snaps = history.get("snapshots", [])
    latest_bots = 0.0
    latest_ts = None
    if snaps:
        latest = snaps[-1]
        latest_bots = float(latest.get("wallets", {}).get("Trading Bots", 0))
        latest_ts = parse_iso(latest["ts"])

    cfg = safe_load(GRID_CONFIG) or {}
    invested_total = 0.0
    earliest_started = None
    bot_count = 0
    for sym, c in cfg.items():
        if sym.startswith("_") or not isinstance(c, dict): continue
        inv = float(c.get("invested_usd", 0) or 0)
        if inv > 0:
            invested_total += inv
            bot_count += 1
            started = parse_iso(c.get("started_at", ""))
            if started and (earliest_started is None or started < earliest_started):
                earliest_started = started

    if invested_total > 0 and latest_ts and earliest_started:
        unrealized = latest_bots - invested_total
        elapsed = latest_ts - earliest_started
        days_float = elapsed.total_seconds() / 86400
        days = max(1, (latest_ts - earliest_started).days + 1)
        WARMUP_DAYS = 7
        if days_float < WARMUP_DAYS:
            monthly_rate = 0.0
            warmup_status = f"warmup ({days_float:.1f}/{WARMUP_DAYS}d)"
        else:
            monthly_rate = unrealized / days_float * 30
            warmup_status = "active"
        return {
            "method": "config_anchor",
            "invested_usd": round(invested_total, 2),
            "current_bots_balance": round(latest_bots, 2),
            "unrealized_pnl": round(unrealized, 2),
            "days_active": days,
            "days_float": round(days_float, 2),
            "monthly_rate": round(monthly_rate, 2),
            "bot_count": bot_count,
            "warmup_status": warmup_status,
            "tracking_since": earliest_started.isoformat(timespec="seconds"),
        }

    if len(snaps) >= 2 and latest_ts:
        cutoff_24h = latest_ts - timedelta(hours=24)
        snap_24h = None
        for s in reversed(snaps):
            ts = parse_iso(s["ts"])
            if ts and ts <= cutoff_24h:
                snap_24h = s
                break
        if snap_24h:
            prev_bots = float(snap_24h.get("wallets", {}).get("Trading Bots", 0))
            delta_24h = latest_bots - prev_bots
            if abs(delta_24h) < 100:
                monthly_rate = delta_24h * 30
                return {
                    "method": "24h_delta",
                    "current_bots_balance": round(latest_bots, 2),
                    "delta_24h": round(delta_24h, 2),
                    "monthly_rate": round(monthly_rate, 2),
                    "snapshot_count": len(snaps),
                    "note": "Estimate from 24h wallet delta (no config anchor yet)",
                }

    return {
        "method": "unavailable",
        "current_bots_balance": round(latest_bots, 2),
        "monthly_rate": 0,
        "note": "Populate grid_config.json with invested_usd per bot to enable",
        "bot_count": bot_count,
    }


def compute_earn_yield() -> dict:
    history = safe_load(WALLET_HISTORY) or {}
    snaps = history.get("snapshots", [])
    earn_balance = 0
    if snaps:
        earn_balance = float(snaps[-1].get("wallets", {}).get("Earn", 0))
    monthly = earn_balance * EARN_APY / 12
    return {"earn_balance": round(earn_balance, 2),
            "apy_assumed": EARN_APY, "monthly_rate": round(monthly, 4)}


def compute_deepseek_cost() -> dict:
    """Estimate DeepSeek monthly rate from cumulative spend / days running.

    Tracking started when state file was first created — use file mtime of the
    .backup as proxy if available, else default to 30 days for conservative est.
    """
    state = safe_load(DEEPSEEK_STATE) or {}
    cumulative = float(state.get("cumulative_spent", 0))
    last_check = state.get("last_check", "")
    days = 30
    backup_path = DATA / "deepseek_cost_state.json.backup"
    if backup_path.exists():
        try:
            mtime = datetime.fromtimestamp(backup_path.stat().st_mtime, tz=timezone.utc)
            days = max(1, (datetime.now(timezone.utc) - mtime).days + 1)
        except Exception:
            pass
    monthly_rate = cumulative / days * 30
    daily = float(state.get("daily_spent", 0))
    return {
        "cumulative_usd": round(cumulative, 2),
        "daily_usd": round(daily, 4),
        "balance_remaining": round(float(state.get("last_balance", 0)), 2),
        "tracking_days": days,
        "monthly_rate": round(monthly_rate, 2),
        "last_check": last_check,
    }


def compute_cost() -> dict:
    deepseek = compute_deepseek_cost()
    cursor = float(os.environ.get("CURSOR_USD_MO", DEFAULT_CURSOR_USD_MO))
    anthropic = float(os.environ.get("ANTHROPIC_USD_MO", DEFAULT_ANTHROPIC_USD_MO))
    total_monthly = deepseek["monthly_rate"] + cursor + anthropic
    return {
        "deepseek": deepseek,
        "cursor_usd_mo": cursor,
        "anthropic_usd_mo": anthropic,
        "total_monthly": round(total_monthly, 2),
    }


def compute_asi() -> dict:
    futures = compute_futures_profit()
    grid = compute_grid_profit()
    earn = compute_earn_yield()
    cost = compute_cost()
    profit_monthly = futures["monthly_rate"] + grid["monthly_rate"] + earn["monthly_rate"]
    cost_monthly = cost["total_monthly"]
    asi = (profit_monthly / cost_monthly) if cost_monthly > 0 else 0
    if asi >= 2.0: status, label = "🚀", "SELF_SUSTAINING_PLUS"
    elif asi >= 1.5: status, label = "🟢", "SURPLUS"
    elif asi >= 1.0: status, label = "🟡", "BREAK_EVEN"
    else: status, label = "🔴", "DEFICIT"
    return {
        "asi": round(asi, 2),
        "status": status,
        "label": label,
        "profit_monthly": round(profit_monthly, 2),
        "cost_monthly": round(cost_monthly, 2),
        "net_monthly": round(profit_monthly - cost_monthly, 2),
        "profit_breakdown": {
            "futures": futures["monthly_rate"],
            "grid": grid["monthly_rate"],
            "earn": earn["monthly_rate"],
        },
        "cost_breakdown": {
            "deepseek": cost["deepseek"]["monthly_rate"],
            "cursor": cost["cursor_usd_mo"],
            "anthropic": cost["anthropic_usd_mo"],
        },
        "details": {
            "futures": futures, "grid": grid, "earn": earn, "cost": cost,
        },
        "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def print_report(data: dict):
    print(f"=== AI Self-Sustainability Index ===")
    print(f"Status: {data['status']}  ASI = {data['asi']}  ({data['label']})")
    print()
    print(f"PROFIT (monthly run-rate):  ${data['profit_monthly']:+.2f}")
    pb = data["profit_breakdown"]
    print(f"  Futures (OpenClaw):        ${pb['futures']:+.2f}/mo")
    print(f"  Grid (Trading Bots):       ${pb['grid']:+.2f}/mo")
    print(f"  Earn (Simple Earn 0.5%):   ${pb['earn']:+.4f}/mo")
    print()
    print(f"COST (monthly):              ${data['cost_monthly']:.2f}")
    cb = data["cost_breakdown"]
    print(f"  DeepSeek API:              ${cb['deepseek']:.2f}/mo")
    print(f"  Cursor Pro:                ${cb['cursor']:.2f}/mo")
    print(f"  Anthropic API:             ${cb['anthropic']:.2f}/mo")
    print()
    net = data["net_monthly"]
    print(f"NET monthly:                 ${net:+.2f}")
    print()
    print(f"--- Tracking detail ---")
    f = data["details"]["futures"]
    g = data["details"]["grid"]
    c = data["details"]["cost"]["deepseek"]
    print(f"Futures: {f['tracking_since']}  ({f['days']}d)  total=${f['total_pnl']:+.2f}")
    if g.get("method") == "config_anchor":
        print(f"Grid:    invested=${g['invested_usd']:.2f}  now=${g['current_bots_balance']:.2f}  unrealized=${g['unrealized_pnl']:+.2f}  ({g.get('days_float',g['days_active'])}d, {g.get('warmup_status','')})")
    elif g.get("method") == "24h_delta":
        print(f"Grid:    24h_delta=${g['delta_24h']:+.2f}  bal=${g['current_bots_balance']:.2f}")
    else:
        print(f"Grid:    {g.get('note')}  bal=${g['current_bots_balance']:.2f}")
    print(f"DeepSeek: cumulative=${c['cumulative_usd']:.2f} over {c['tracking_days']}d  balance=${c['balance_remaining']:.2f}")


def main():
    load_env()
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true", help="JSON output")
    args = p.parse_args()
    data = compute_asi()
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print_report(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
