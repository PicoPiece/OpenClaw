#!/usr/bin/env python3
"""
Backtest CANDIDATES — test 8 candidate coins for allowlist expansion.

Tests: ARB, OP, POL (was MATIC), ATOM, NEAR, INJ, FET, TRX
Strategy: V6_TIMEOUT (mode-aware SL/TP, profit-aware timeout) — same as live.
Vol regime filter: ATR/price <= 2.5% (matches live VOL_REGIME_MAX_PCT).

Decision rule per coin:
  KEEP (promote to allowlist) if:  total_r > 0  AND  win_rate >= 45%  AND  pf >= 1.0  AND  n_signals >= 8
  WATCH (paper-trade only)    if:  total_r > -1 AND win_rate >= 40%
  REJECT                       otherwise

Usage:
    python3 backtest_candidates.py
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from backtest_v3_v4 import (
    calc_atr,
    detect_v4_signal,
    fetch_full_history,
    WARMUP_BARS, LOOKAHEAD_BARS, SIGNAL_COOLDOWN_BARS,
)
from backtest_v5 import (
    classify_mode, closes_4h_up_to,
    sim_timeout,
)

SCRIPT_DIR = Path(__file__).resolve().parent

import sys
BACKTEST_DAYS = int(os.environ.get("BACKTEST_DAYS", "30"))
_default_candidates = ["APT", "SUI", "DOT", "TIA", "JUP"]
_env_candidates = os.environ.get("CANDIDATES", "").strip()
if _env_candidates:
    CANDIDATES = [c.strip().upper() for c in _env_candidates.split(",") if c.strip()]
else:
    CANDIDATES = _default_candidates
SYMBOLS = {c: f"{c}USDT" for c in CANDIDATES}
VOL_REGIME_MAX_PCT = 2.5  # matches live setting


def backtest_coin_simple(coin: str, klines_1h: list, klines_4h: list):
    closes_1h = [float(k[4]) for k in klines_1h]
    highs_1h = [float(k[2]) for k in klines_1h]
    lows_1h = [float(k[3]) for k in klines_1h]
    vols_1h = [float(k[5]) for k in klines_1h]
    times_1h = [int(k[0]) for k in klines_1h]
    closes_4h = [float(k[4]) for k in klines_4h]
    times_4h = [int(k[0]) for k in klines_4h]

    trades, last_signal_bar = [], -100
    for i in range(WARMUP_BARS, len(klines_1h) - LOOKAHEAD_BARS):
        if i - last_signal_bar < SIGNAL_COOLDOWN_BARS:
            continue
        bar_time = times_1h[i]
        c1 = closes_1h[:i + 1]
        h1 = highs_1h[:i + 1]
        l1 = lows_1h[:i + 1]
        v1 = vols_1h[:i + 1]
        c4 = closes_4h_up_to(closes_4h, times_4h, bar_time)

        sig = detect_v4_signal(c1, h1, l1, v1, c4)
        if not sig:
            continue
        atr_initial = calc_atr(h1, l1, c1)
        if not atr_initial or atr_initial <= 0:
            continue

        atr_pct = atr_initial / sig["entry"] * 100
        if atr_pct > VOL_REGIME_MAX_PCT:
            continue

        future_bars = klines_1h[i + 1: i + 1 + LOOKAHEAD_BARS]
        mode = classify_mode(sig["direction"], c4)
        out = sim_timeout(sig, mode, future_bars, atr_initial)
        trades.append({
            "coin": coin, "time": bar_time, "direction": sig["direction"],
            "entry": sig["entry"], "atr_pct": atr_pct, "mode": mode, **out,
        })
        last_signal_bar = i
    return trades


def stats(trades: list) -> dict:
    if not trades:
        return {"n": 0, "win_rate": 0, "total_r": 0, "pf": 0, "max_dd": 0,
                "avg_hold_bars": 0, "scalp": 0, "swing": 0}
    n = len(trades)
    wins = sum(1 for t in trades if t["r"] > 0)
    losses_r = sum(t["r"] for t in trades if t["r"] <= 0)
    wins_r = sum(t["r"] for t in trades if t["r"] > 0)
    total_r = sum(t["r"] for t in trades)
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in trades:
        equity += t["r"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "n": n,
        "win_rate": wins / n * 100,
        "total_r": total_r,
        "avg_r": total_r / n,
        "pf": abs(wins_r / losses_r) if losses_r else float("inf"),
        "max_dd": max_dd,
        "avg_hold_bars": sum(t["bars"] for t in trades) / n,
        "scalp": sum(1 for t in trades if t.get("mode") == "SCALP"),
        "swing": sum(1 for t in trades if t.get("mode") == "SWING"),
    }


def classify(s: dict) -> str:
    if s["n"] < 5:
        return "INSUFFICIENT_DATA"
    if s["total_r"] > 0 and s["win_rate"] >= 45 and s["pf"] >= 1.0 and s["n"] >= 8:
        return "KEEP"
    if s["total_r"] > -1 and s["win_rate"] >= 40:
        return "WATCH"
    return "REJECT"


def main():
    print(f"\n=== Backtest CANDIDATES — {BACKTEST_DAYS} days, vol_filter ≤ {VOL_REGIME_MAX_PCT}% ===")
    print(f"Coins: {', '.join(CANDIDATES)}\n")
    results = {}
    for coin, sym in SYMBOLS.items():
        print(f"[fetch] {coin} ({sym}) ...")
        try:
            t0 = time.time()
            klines_1h = fetch_full_history(sym, days=BACKTEST_DAYS, interval="1h")
            klines_4h = fetch_full_history(sym, days=BACKTEST_DAYS, interval="4h")
            if not klines_1h or not klines_4h:
                print(f"  SKIP: no data")
                continue
            trades = backtest_coin_simple(coin, klines_1h, klines_4h)
            s = stats(trades)
            s["verdict"] = classify(s)
            results[coin] = {"stats": s, "trades": trades}
            print(f"  done in {time.time()-t0:.1f}s — {s['n']} trades, WR {s['win_rate']:.1f}%, R {s['total_r']:+.2f}, PF {s['pf']:.2f}, DD {s['max_dd']:.2f} → {s['verdict']}")
        except Exception as e:
            print(f"  ERROR: {e}")

    print("\n" + "=" * 80)
    print(f"{'COIN':6} {'N':>4} {'WR%':>6} {'TotalR':>8} {'AvgR':>7} {'PF':>5} {'DD':>6} {'Hold(bars)':>10} {'SCALP/SWING':>13} {'VERDICT':>10}")
    print("-" * 80)
    for coin in CANDIDATES:
        if coin not in results:
            print(f"{coin:6} -- no data --")
            continue
        s = results[coin]["stats"]
        print(f"{coin:6} {s['n']:>4} {s['win_rate']:>6.1f} {s['total_r']:>+8.2f} {s.get('avg_r',0):>+7.3f} {s['pf']:>5.2f} {s['max_dd']:>6.2f} {s['avg_hold_bars']:>10.1f} {s['scalp']:>5}/{s['swing']:<7} {s['verdict']:>10}")
    print("=" * 80)

    keep = [c for c in CANDIDATES if c in results and results[c]["stats"]["verdict"] == "KEEP"]
    watch = [c for c in CANDIDATES if c in results and results[c]["stats"]["verdict"] == "WATCH"]
    reject = [c for c in CANDIDATES if c in results and results[c]["stats"]["verdict"] == "REJECT"]
    print(f"\nKEEP   ({len(keep)}): {keep}")
    print(f"WATCH  ({len(watch)}): {watch}")
    print(f"REJECT ({len(reject)}): {reject}")

    out = SCRIPT_DIR / "data" / "backtest_candidates_results.json"
    out.write_text(json.dumps({
        "backtest_at": datetime.now(timezone.utc).isoformat(),
        "days": BACKTEST_DAYS,
        "vol_regime_max_pct": VOL_REGIME_MAX_PCT,
        "candidates": CANDIDATES,
        "results": {k: {"stats": v["stats"]} for k, v in results.items()},
        "keep": keep, "watch": watch, "reject": reject,
    }, indent=2, default=str))
    print(f"\nSaved → {out}")


if __name__ == "__main__":
    main()
