#!/usr/bin/env python3
"""
Capital Scaling Manager — proposes capital level changes based on gates.

Phase 4 rules (from roadmap):
  $99   -> $500   if Phase 3 gates pass (WR>=45% 4w, PF>=1.3, DD<=20%)
  $500  -> $1000  if first half of month gains >5%
  $1000 -> $1500  if previous month gains >5%

This script is ADVISORY: it computes whether gates pass and writes a proposal
to data/capital_proposal.json. It NEVER auto-edits .env or PORTFOLIO_BALANCE.
The user must explicitly run with --apply to update .env.

Usage:
    python3 capital_scaling.py                  # analyze only
    python3 capital_scaling.py --propose 500    # check if scaling to $500 is allowed
    python3 capital_scaling.py --apply 500      # update .env after user approval
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import decision_logger  # noqa: E402

ENV_FILE = SCRIPT_DIR / ".env"
PROPOSAL_FILE = SCRIPT_DIR / "data" / "capital_proposal.json"

SCALE_TIERS = [99, 500, 1000, 1500, 3000]


def current_balance() -> float:
    if not ENV_FILE.exists():
        return 99.0
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("PORTFOLIO_BALANCE="):
            try:
                return float(line.split("=", 1)[1].strip())
            except Exception:
                pass
    return 99.0


def gates_for_target(target: float) -> dict:
    """Compute gates relevant to scaling to `target`."""
    now = datetime.now(timezone.utc)
    last_30d = (now - timedelta(days=30)).isoformat()
    last_14d = (now - timedelta(days=14)).isoformat()

    pnl_30 = decision_logger.trade_pnl_stats(since=last_30d, is_shadow=False)
    pnl_14 = decision_logger.trade_pnl_stats(since=last_14d, is_shadow=False)

    with decision_logger._conn() as c:
        rows = c.execute(
            """SELECT pnl_usd FROM trades
               WHERE closed_at >= ? AND is_shadow=0
               ORDER BY closed_at""",
            (last_30d,),
        ).fetchall()
    pnls = [r["pnl_usd"] or 0 for r in rows]
    cum = 0
    peak = 0
    max_dd = 0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    pf = (sum(p for p in pnls if p > 0) /
          abs(sum(p for p in pnls if p < 0))) if any(p < 0 for p in pnls) else None

    cur_balance = current_balance()
    drawdown_pct = (max_dd / cur_balance * 100) if cur_balance else 0

    gates = {
        "current_balance_usd": cur_balance,
        "target_balance_usd": target,
        "trades_30d": pnl_30["trades"],
        "win_rate_30d": pnl_30["win_rate"],
        "profit_factor_30d": round(pf, 2) if pf else None,
        "total_pnl_30d": pnl_30["pnl"],
        "max_drawdown_usd_30d": round(max_dd, 4),
        "max_drawdown_pct_30d": round(drawdown_pct, 2),
        "pnl_14d": pnl_14["pnl"],
        "checks": [],
        "approved": False,
    }

    def check(name, ok, msg):
        gates["checks"].append({"name": name, "ok": ok, "msg": msg})
        return ok

    if target == 500:
        ok1 = check("WR >= 45% sustained 30d", pnl_30["win_rate"] >= 45,
                    f"actual {pnl_30['win_rate']:.1f}%")
        ok2 = check("Profit factor >= 1.3", pf is not None and pf >= 1.3,
                    f"actual {pf}")
        ok3 = check("Max DD <= 20%", drawdown_pct <= 20,
                    f"actual {drawdown_pct:.1f}%")
        ok4 = check("Trades >= 30 in 30d", pnl_30["trades"] >= 30,
                    f"actual {pnl_30['trades']}")
        gates["approved"] = ok1 and ok2 and ok3 and ok4
    elif target == 1000:
        ok1 = check("Last 14d P&L > 5% of $500", pnl_14["pnl"] > 25,
                    f"actual ${pnl_14['pnl']:.2f}")
        ok2 = check("Max DD <= 15%", drawdown_pct <= 15,
                    f"actual {drawdown_pct:.1f}%")
        gates["approved"] = ok1 and ok2
    elif target == 1500:
        ok1 = check("Last month P&L > 5% of $1000", pnl_30["pnl"] > 50,
                    f"actual ${pnl_30['pnl']:.2f}")
        ok2 = check("WR >= 45%", pnl_30["win_rate"] >= 45,
                    f"actual {pnl_30['win_rate']:.1f}%")
        gates["approved"] = ok1 and ok2
    elif target == 3000:
        ok1 = check("Last month P&L > 10% of $1500", pnl_30["pnl"] > 150,
                    f"actual ${pnl_30['pnl']:.2f}")
        ok2 = check("Max DD <= 15%", drawdown_pct <= 15,
                    f"actual {drawdown_pct:.1f}%")
        gates["approved"] = ok1 and ok2
    else:
        check("custom target", True, "no preset gates — manual review only")

    return gates


def slippage_report(days: int = 30) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with decision_logger._conn() as c:
        rows = c.execute(
            "SELECT slippage_bps, coin FROM slippage_log WHERE ts >= ?",
            (since,),
        ).fetchall()
    if not rows:
        return {"n": 0, "avg_bps": 0, "max_bps": 0, "by_coin": {}}
    bps = [r["slippage_bps"] or 0 for r in rows]
    by_coin: dict[str, list[float]] = {}
    for r in rows:
        by_coin.setdefault(r["coin"], []).append(r["slippage_bps"] or 0)
    return {
        "n": len(bps),
        "avg_bps": round(sum(bps) / len(bps), 2),
        "max_bps": round(max(abs(x) for x in bps), 2),
        "by_coin": {
            c: {"n": len(v), "avg_bps": round(sum(v) / len(v), 2)}
            for c, v in by_coin.items()
        },
    }


def write_proposal(gates: dict, slippage: dict):
    PROPOSAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROPOSAL_FILE.write_text(json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "gates": gates,
        "slippage_30d": slippage,
        "applied": False,
    }, indent=2))


def apply_balance(target: float, gates: dict):
    if not gates.get("approved"):
        print("⚠️  Gates not satisfied — refusing to apply automatically.")
        print("   Pass --force to override (NOT RECOMMENDED).")
        if "--force" not in sys.argv:
            sys.exit(2)

    if not ENV_FILE.exists():
        print(f"⚠️  .env missing — creating new one with PORTFOLIO_BALANCE={target}")
        ENV_FILE.write_text(f"PORTFOLIO_BALANCE={target:g}\n")
        return

    backup = ENV_FILE.with_suffix(".env.bak." + datetime.now().strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(ENV_FILE, backup)
    print(f"backup: {backup}")

    content = ENV_FILE.read_text()
    if re.search(r"^PORTFOLIO_BALANCE=", content, flags=re.M):
        content = re.sub(r"^PORTFOLIO_BALANCE=.*$",
                          f"PORTFOLIO_BALANCE={target:g}", content, flags=re.M)
    else:
        content = content.rstrip() + f"\nPORTFOLIO_BALANCE={target:g}\n"
    ENV_FILE.write_text(content)
    print(f"PORTFOLIO_BALANCE updated to {target:g}")
    print("⚠️  Restart binance_price_alert + trade_executor for change to take effect.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--propose", type=float, help="evaluate gates for this target balance")
    ap.add_argument("--apply", type=float, help="set PORTFOLIO_BALANCE to this value (after user OK)")
    ap.add_argument("--force", action="store_true", help="bypass gate check on --apply")
    ap.add_argument("--slippage-days", type=int, default=30)
    args = ap.parse_args()

    cur = current_balance()
    if args.propose:
        target = args.propose
    elif args.apply:
        target = args.apply
    else:
        next_idx = next((i+1 for i, t in enumerate(SCALE_TIERS) if cur < t), len(SCALE_TIERS)-1)
        target = SCALE_TIERS[min(next_idx, len(SCALE_TIERS)-1)]

    gates = gates_for_target(target)
    slip = slippage_report(days=args.slippage_days)
    print(json.dumps({"current_balance": cur, "target": target,
                       "gates": gates, "slippage": slip}, indent=2))
    write_proposal(gates, slip)

    if args.apply:
        apply_balance(args.apply, gates)


if __name__ == "__main__":
    main()
