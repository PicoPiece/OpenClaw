#!/usr/bin/env python3
"""
Shadow Paper Trader — runs alongside live trading to validate new strategies.

Reads the latest signal from data/pending_signal.json (or generates one),
and instead of executing on Binance, logs an `is_shadow=1` trade to
decisions.db with the signal's planned entry/SL/TP. A separate watcher
process (run as cron) closes shadow trades by polling Binance prices and
applying TP/SL/timeout rules.

Use cases:
  - Test new prompt variant (B) against live (A)
  - Test parameter changes (e.g. ATR_TP_MULT) on real-time market data
  - Pre-deploy validation before risking real capital

Usage:
    python3 shadow_trader.py --open    # open shadow trade from current pending signal
    python3 shadow_trader.py --close   # poll prices and close any TP/SL/timeout
    python3 shadow_trader.py --report  # compare shadow vs live PnL
    python3 shadow_trader.py --daemon  # loop --close every 60s
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import decision_logger  # noqa: E402

PENDING_SIGNAL_FILE = SCRIPT_DIR / "data" / "pending_signal.json"
SHADOW_TIMEOUT_HOURS = 48
PRICE_API = "https://fapi.binance.com/fapi/v1/ticker/price"
SYMBOL_SUFFIX = "USDT"


def fetch_price(symbol: str) -> float | None:
    try:
        req = urllib.request.Request(f"{PRICE_API}?symbol={symbol}",
                                       headers={"User-Agent": "shadow/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        return float(data.get("price", 0))
    except Exception:
        return None


def open_shadow_from_pending() -> dict | None:
    if not PENDING_SIGNAL_FILE.exists():
        return None
    sig = json.loads(PENDING_SIGNAL_FILE.read_text())
    coin = (sig.get("coin") or "").lower()
    if not coin:
        return None

    with decision_logger._conn() as c:
        existing = c.execute(
            """SELECT id FROM trades
               WHERE coin=? AND is_shadow=1 AND closed_at IS NULL""",
            (coin,),
        ).fetchone()
    if existing:
        return {"already_open": existing["id"]}

    trade_id = decision_logger.log_trade_open(
        coin=coin,
        direction=sig["direction"],
        entry_price=float(sig["entry"]),
        sl_price=float(sig.get("sl", 0)) or None,
        tp_price=float(sig.get("tp", 0)) or None,
        qty=float(sig.get("qty", 0)) or None,
        position_usd=float(sig.get("position_usd", 0)) or None,
        risk_usd=float(sig.get("risk_usd", 0)) or None,
        leverage=int(sig.get("leverage", 0)) or None,
        signal_decision_id=sig.get("decision_id"),
        notes=f"shadow opened from pending_signal status={sig.get('status')}",
        is_shadow=True,
        indicators={
            "rsi": sig.get("rsi"), "ema_gap_pct": sig.get("ema_gap_pct"),
            "vol_ratio": sig.get("vol_ratio"), "atr": sig.get("atr"),
            "trend": sig.get("trend"), "rr": sig.get("rr_ratio"),
        },
        market_state={"variant": sig.get("prompt_variant", "A")},
    )
    return {"opened": trade_id, "coin": coin, "direction": sig["direction"]}


def close_open_shadow_trades() -> dict:
    closed = []
    with decision_logger._conn() as c:
        rows = c.execute(
            """SELECT * FROM trades WHERE is_shadow=1 AND closed_at IS NULL"""
        ).fetchall()

    for r in rows:
        coin = r["coin"]
        symbol = (coin or "").upper() + SYMBOL_SUFFIX
        price = fetch_price(symbol)
        if price is None:
            continue

        sl = r["sl_price"] or 0
        tp = r["tp_price"] or 0
        entry = r["entry_price"]
        direction = r["direction"]

        result = None
        close_price = price
        if direction == "LONG":
            if tp and price >= tp:
                result, close_price = "TP_HIT", tp
            elif sl and price <= sl:
                result, close_price = "SL_HIT", sl
        else:
            if tp and price <= tp:
                result, close_price = "TP_HIT", tp
            elif sl and price >= sl:
                result, close_price = "SL_HIT", sl

        try:
            opened = datetime.fromisoformat(r["opened_at"].replace("Z", "+00:00"))
            age = datetime.now(timezone.utc) - opened
        except Exception:
            age = timedelta(0)
        if not result and age >= timedelta(hours=SHADOW_TIMEOUT_HOURS):
            result = "TIMEOUT"
            close_price = price

        if not result:
            continue

        if direction == "LONG":
            pnl = (close_price - entry) * (r["qty"] or 1)
        else:
            pnl = (entry - close_price) * (r["qty"] or 1)

        decision_logger.log_trade_close(
            trade_id=r["id"],
            close_price=close_price,
            result=result,
            pnl_usd=round(pnl, 6),
            notes="shadow auto-close",
        )
        closed.append({"id": r["id"], "coin": coin, "result": result,
                        "pnl": round(pnl, 4)})

    return {"closed": closed, "checked": len(rows)}


def report() -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    return {
        "live_30d": decision_logger.trade_pnl_stats(since=since, is_shadow=False),
        "shadow_30d": decision_logger.trade_pnl_stats(since=since, is_shadow=True),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--close", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args()

    if args.open:
        print(json.dumps(open_shadow_from_pending() or {"opened": None}, indent=2))
        return
    if args.close:
        print(json.dumps(close_open_shadow_trades(), indent=2))
        return
    if args.report:
        print(json.dumps(report(), indent=2, default=str))
        return
    if args.daemon:
        while True:
            try:
                out = close_open_shadow_trades()
                if out["closed"]:
                    print(json.dumps(out))
            except Exception as e:
                print(f"shadow daemon error: {e}")
            time.sleep(args.interval)
        return

    ap.print_help()


if __name__ == "__main__":
    main()
