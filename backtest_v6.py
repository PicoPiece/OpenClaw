#!/usr/bin/env python3
"""
Backtest v6 — Multi-optimization stack on top of v5 TIMEOUT.

Adds 3 incremental optimizations (each ablation testable):

  V6_TIMEOUT_FULL  : full 10-coin universe, no extra filter (= V5_TIMEOUT, ref)
  V6_COIN_FILTER   : allowlist top-6 coins (AAVE, ETH, LINK, BNB, XRP, BTC)
  V6_FULL_FILTER   : allowlist + ATR/price <= 2.0% volatility regime filter
  V6_PYRAMID_RETUNED: V6_FULL_FILTER + retuned pyramid (trigger 2.0 ATR, SWING-only, no chase)

Reuses indicators + signal detection from backtest_v3_v4.py and many helpers from v5.

Usage:
    python3 backtest_v6.py
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from backtest_v3_v4 import (
    calc_atr, calc_ema,
    detect_v4_signal,
    fetch_full_history,
    EMA_FAST, EMA_SLOW,
    WARMUP_BARS, LOOKAHEAD_BARS, SIGNAL_COOLDOWN_BARS,
)

from backtest_v5 import (
    classify_mode, htf_still_aligned, closes_4h_up_to,
    pnl_R, _atr_at,
    MODE_PARAMS, SLIPPAGE_PCT,
    sim_timeout,
)

SCRIPT_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
import os as _os
BACKTEST_DAYS = int(_os.environ.get("BACKTEST_DAYS", "30"))
COINS_FULL = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "AAVE", "TRX",
              "INJ", "ORDI", "ATOM", "ENA"]
COINS_ALLOWLIST = ["AAVE", "ETH", "LINK", "BNB", "XRP", "BTC", "TRX",
                   "INJ", "ORDI", "ATOM", "ENA"]
SYMBOLS = {c: f"{c}USDT" for c in COINS_FULL}

VOL_REGIME_MAX_PCT = 2.0  # skip signal if ATR/price > 2.0%

# Retuned pyramid params (NO chase trail)
PYRAMID_RETUNED_LEGS = [
    {"trigger_atr": 2.0, "qty_frac": 0.5},   # leg 1: tighter trigger
    {"trigger_atr": 3.0, "qty_frac": 0.25},  # leg 2: wider gap (was 2.5)
]
PYRAMID_VOL_REGIME_MAX = 1.5  # skip add if ATR_now > 1.5 * ATR_entry
PYRAMID_SWING_ONLY = True


# ---------------------------------------------------------------------------
# Pyramid simulator (RETUNED — no chase trail)
# ---------------------------------------------------------------------------

def sim_pyramid_retuned(signal: dict, mode: str, future_bars: list, atr_initial: float,
                        bars_1h_full: list, idx_signal: int,
                        closes_4h_full: list, times_4h: list) -> dict:
    """Pyramid with leg-anchored SL only (no chase trail), trigger 2.0 ATR, SWING-only."""
    if PYRAMID_SWING_ONLY and mode != "SWING":
        # Fallback to plain timeout for SCALP signals
        return sim_timeout(signal, mode, future_bars, atr_initial)

    entry = signal["entry"]
    direction = signal["direction"]
    p = MODE_PARAMS[mode]
    if direction == "LONG":
        sl_initial = entry - p["sl_mult"] * atr_initial
        tp = entry + p["tp_mult"] * atr_initial
    else:
        sl_initial = entry + p["sl_mult"] * atr_initial
        tp = entry - p["tp_mult"] * atr_initial
    initial_risk = abs(entry - sl_initial)
    max_bars = min(p["timeout_bars"], len(future_bars))

    legs = [{"entry": entry, "qty_frac": 1.0, "sl": sl_initial, "active": True, "leg_idx": 0}]
    closed_pnl_R = 0.0
    add_events = []
    bars_held = 0

    for i in range(max_bars):
        bar = future_bars[i]
        high = float(bar[2])
        low = float(bar[3])
        close = float(bar[4])
        bars_held = i + 1

        for leg in legs:
            if not leg["active"]:
                continue
            if direction == "LONG":
                if low <= leg["sl"]:
                    closed_pnl_R += pnl_R(direction, leg["entry"], leg["sl"],
                                          leg["qty_frac"], initial_risk)
                    leg["active"] = False
                    leg["exit"] = leg["sl"]
                    leg["exit_reason"] = "SL"
                elif high >= tp:
                    closed_pnl_R += pnl_R(direction, leg["entry"], tp,
                                          leg["qty_frac"], initial_risk)
                    leg["active"] = False
                    leg["exit"] = tp
                    leg["exit_reason"] = "TP"
            else:
                if high >= leg["sl"]:
                    closed_pnl_R += pnl_R(direction, leg["entry"], leg["sl"],
                                          leg["qty_frac"], initial_risk)
                    leg["active"] = False
                    leg["exit"] = leg["sl"]
                    leg["exit_reason"] = "SL"
                elif low <= tp:
                    closed_pnl_R += pnl_R(direction, leg["entry"], tp,
                                          leg["qty_frac"], initial_risk)
                    leg["active"] = False
                    leg["exit"] = tp
                    leg["exit_reason"] = "TP"

        if not any(l["active"] for l in legs):
            return _pyramid_result(closed_pnl_R, bars_held, mode, legs, add_events,
                                   final_reason="ALL_EXITED")

        last_price = close
        if direction == "LONG":
            profit_atr = (last_price - entry) / atr_initial
        else:
            profit_atr = (entry - last_price) / atr_initial

        global_idx = idx_signal + 1 + i
        atr_now = _atr_at(bars_1h_full, global_idx, period=14)

        bar_time_ms = int(bar[6])
        for cfg_idx, cfg in enumerate(PYRAMID_RETUNED_LEGS):
            target_leg_idx = cfg_idx + 1
            already_added = any(l["leg_idx"] == target_leg_idx for l in legs)
            if already_added:
                continue
            if profit_atr < cfg["trigger_atr"]:
                continue
            if atr_now and atr_now > PYRAMID_VOL_REGIME_MAX * atr_initial:
                continue
            c4_now = closes_4h_up_to(closes_4h_full, times_4h, bar_time_ms)
            if not htf_still_aligned(direction, c4_now):
                continue
            anchor_entry = legs[target_leg_idx - 1]["entry"]
            legs.append({
                "entry": last_price,
                "qty_frac": cfg["qty_frac"],
                "sl": anchor_entry,
                "active": True,
                "leg_idx": target_leg_idx,
            })
            add_events.append({
                "leg_idx": target_leg_idx, "bar": i + 1,
                "entry": last_price, "profit_atr": profit_atr,
            })

    if max_bars == 0:
        return _pyramid_result(0.0, 0, mode, legs, add_events, final_reason="NO_BARS")
    last_close = float(future_bars[max_bars - 1][4])
    if direction == "LONG":
        profit_atr_final = (last_close - entry) / atr_initial
    else:
        profit_atr_final = (entry - last_close) / atr_initial

    for leg in legs:
        if leg["active"]:
            closed_pnl_R += pnl_R(direction, leg["entry"], last_close,
                                  leg["qty_frac"], initial_risk)
            leg["active"] = False
            leg["exit"] = last_close
            leg["exit_reason"] = "TIMEOUT"

    if profit_atr_final > 0.3:
        final_reason = "TIMEOUT_PROFIT"
    elif profit_atr_final > -0.3:
        final_reason = "TIMEOUT_BE"
    else:
        final_reason = "TIMEOUT_LOSS"

    return _pyramid_result(closed_pnl_R, bars_held, mode, legs, add_events,
                           final_reason=final_reason)


def _pyramid_result(total_r, bars, mode, legs, add_events, final_reason):
    return {
        "result": final_reason, "bars": bars, "r": total_r, "mode": mode,
        "n_legs": len(legs), "n_adds": len(add_events),
    }


# ---------------------------------------------------------------------------
# Backtest runner — generates 4 variants in one walk-forward pass
# ---------------------------------------------------------------------------

def backtest_coin(coin: str, klines_1h: list, klines_4h: list, in_allowlist: bool):
    """Walk-forward, but produce 4 variant outputs per signal."""
    closes_1h = [float(k[4]) for k in klines_1h]
    highs_1h = [float(k[2]) for k in klines_1h]
    lows_1h = [float(k[3]) for k in klines_1h]
    vols_1h = [float(k[5]) for k in klines_1h]
    times_1h = [int(k[0]) for k in klines_1h]
    closes_4h = [float(k[4]) for k in klines_4h]
    times_4h = [int(k[0]) for k in klines_4h]

    full, coin_filter, full_filter, pyramid = [], [], [], []
    last_signal_bar = -100

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

        future_bars = klines_1h[i + 1: i + 1 + LOOKAHEAD_BARS]
        mode = classify_mode(sig["direction"], c4)
        atr_pct = atr_initial / sig["entry"] * 100

        common = {
            "coin": coin, "time": bar_time, "direction": sig["direction"],
            "entry": sig["entry"], "atr": atr_initial, "atr_pct": atr_pct,
            "rsi": sig.get("rsi"), "mode": mode,
        }

        # V6_TIMEOUT_FULL: no filter
        out = sim_timeout(sig, mode, future_bars, atr_initial)
        full.append({**common, **out})

        # V6_COIN_FILTER: only allowlist
        if in_allowlist:
            coin_filter.append({**common, **out})

        # V6_FULL_FILTER: allowlist + vol regime
        if in_allowlist and atr_pct <= VOL_REGIME_MAX_PCT:
            full_filter.append({**common, **out})

            # V6_PYRAMID_RETUNED: full filter + retuned pyramid
            py_out = sim_pyramid_retuned(sig, mode, future_bars, atr_initial,
                                         klines_1h, i, closes_4h, times_4h)
            pyramid.append({**common, **py_out})

        last_signal_bar = i

    return full, coin_filter, full_filter, pyramid


# ---------------------------------------------------------------------------
# Reporting (reuse stats logic from v5)
# ---------------------------------------------------------------------------

def _stats(trades: list) -> dict:
    if not trades:
        return {"n": 0}
    n = len(trades)
    wins = sum(1 for t in trades if t["r"] > 0)
    losses_r = sum(t["r"] for t in trades if t["r"] <= 0)
    wins_r = sum(t["r"] for t in trades if t["r"] > 0)
    total_r = sum(t["r"] for t in trades)
    avg_hold = sum(t["bars"] for t in trades) / n
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in trades:
        equity += t["r"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    pf = abs(wins_r / losses_r) if losses_r != 0 else float("inf")
    return {
        "n": n, "wins": wins, "win_rate": wins / n * 100,
        "total_r": total_r, "avg_r": total_r / n,
        "avg_win_r": wins_r / wins if wins else 0,
        "avg_loss_r": losses_r / (n - wins) if (n - wins) else 0,
        "profit_factor": pf, "avg_hold": avg_hold, "max_dd": max_dd,
    }


def report_variant(name: str, trades: list):
    print(f"\n=== {name} ===")
    s = _stats(trades)
    if s["n"] == 0:
        print("  No signals.")
        return
    pf_str = f"{s['profit_factor']:.2f}" if s["profit_factor"] != float("inf") else "inf"
    print(f"  Signals: {s['n']} | Win: {s['win_rate']:.1f}% | Total R: {s['total_r']:+.2f} | Avg R: {s['avg_r']:+.3f}")
    print(f"  Avg win: +{s['avg_win_r']:.2f}R | Avg loss: {s['avg_loss_r']:.2f}R | PF: {pf_str}")
    print(f"  Avg hold: {s['avg_hold']:.1f}h | Max DD: {s['max_dd']:.2f}R")

    by_mode = {}
    for t in trades:
        by_mode.setdefault(t.get("mode", "?"), []).append(t)
    for mode in sorted(by_mode):
        sm = _stats(by_mode[mode])
        print(f"    {mode:<6} {sm['n']:>3} | win {sm['win_rate']:>4.0f}% | R {sm['total_r']:+.2f}")

    by_coin = {}
    for t in trades:
        by_coin.setdefault(t["coin"], []).append(t)
    for coin in sorted(by_coin):
        sm = _stats(by_coin[coin])
        print(f"    {coin:<6} {sm['n']:>3} | win {sm['win_rate']:>4.0f}% | R {sm['total_r']:+.2f}")


def comparison_table(variants: dict):
    print("\n" + "=" * 92)
    print("  4-VARIANT COMPARISON (incremental optimization)")
    print("=" * 92)
    print(f"  {'Variant':<24} {'Signals':>8} {'WinRate':>9} {'TotalR':>9} {'AvgR':>8} {'MaxDD':>8} {'PF':>6}")
    prev_total = None
    for name, trades in variants.items():
        s = _stats(trades)
        if s["n"] == 0:
            print(f"  {name:<24}    no signals")
            continue
        pf_str = f"{s['profit_factor']:.2f}" if s["profit_factor"] != float("inf") else "inf"
        delta = ""
        if prev_total is not None:
            delta = f"  Δ{s['total_r'] - prev_total:+.2f}R"
        print(f"  {name:<24} {s['n']:>8} {s['win_rate']:>8.1f}% {s['total_r']:>+9.2f} "
              f"{s['avg_r']:>+8.3f} {s['max_dd']:>8.2f} {pf_str:>6}{delta}")
        prev_total = s["total_r"]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 70)
    print(f"  BACKTEST v6 — {BACKTEST_DAYS} days, FULL universe ({len(COINS_FULL)})")
    print(f"  Allowlist: {COINS_ALLOWLIST}")
    print(f"  Vol regime filter: ATR/price <= {VOL_REGIME_MAX_PCT}%")
    print(f"  Pyramid retuned: trigger {PYRAMID_RETUNED_LEGS[0]['trigger_atr']}/{PYRAMID_RETUNED_LEGS[1]['trigger_atr']} ATR, SWING-only={PYRAMID_SWING_ONLY}, NO chase trail")
    print("=" * 70)

    all_full, all_coin, all_full_filter, all_pyramid = [], [], [], []
    t0 = time.time()

    for coin in COINS_FULL:
        symbol = SYMBOLS[coin]
        in_allow = coin in COINS_ALLOWLIST
        marker = "[ALLOWED]" if in_allow else "[FILTERED]"
        print(f"\n  [{coin}] {marker} fetching klines...")
        try:
            k1h = fetch_full_history(symbol, "1h", BACKTEST_DAYS)
            k4h = fetch_full_history(symbol, "4h", BACKTEST_DAYS)
            f, c, ff, py = backtest_coin(coin, k1h, k4h, in_allow)
            print(f"    Signals: full={len(f)} coin_filter={len(c)} full_filter={len(ff)} pyramid={len(py)}")
            all_full.extend(f)
            all_coin.extend(c)
            all_full_filter.extend(ff)
            all_pyramid.extend(py)
        except Exception as e:
            print(f"    ERROR: {e}")

    print(f"\n  Backtest completed in {time.time() - t0:.1f}s")

    report_variant("V6_TIMEOUT_FULL  (10 coins, no filter — REF = V5_TIMEOUT)", all_full)
    report_variant("V6_COIN_FILTER   (6-coin allowlist)", all_coin)
    report_variant("V6_FULL_FILTER   (allowlist + ATR/price <= 2.0%)", all_full_filter)
    report_variant("V6_PYRAMID_RETUNED (full_filter + pyramid 2.0/3.0 ATR, SWING-only, no chase)", all_pyramid)

    variants = {
        "V6_TIMEOUT_FULL": all_full,
        "V6_COIN_FILTER": all_coin,
        "V6_FULL_FILTER": all_full_filter,
        "V6_PYRAMID_RETUNED": all_pyramid,
    }
    comparison_table(variants)

    out = SCRIPT_DIR / "data" / "backtest_v6_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "backtest_at": datetime.now(timezone.utc).isoformat(),
        "days": BACKTEST_DAYS,
        "coins_full": COINS_FULL, "coins_allowlist": COINS_ALLOWLIST,
        "vol_regime_max_pct": VOL_REGIME_MAX_PCT,
        "pyramid_retuned_legs": PYRAMID_RETUNED_LEGS,
        "pyramid_swing_only": PYRAMID_SWING_ONLY,
        "variants": {k: v for k, v in variants.items()},
    }, indent=2, default=str))
    print(f"\n  Raw results saved to {out}")


if __name__ == "__main__":
    main()
