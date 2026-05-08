#!/usr/bin/env python3
"""Backtest v7 — 4-Gap Upgrade Impact Analysis.

So sánh các variant để đo impact lý thuyết:
  V7_BASELINE         : V6 reference (TIMEOUT_FULL allowlist 7, vol≤2.5%, no chase, no partial)
  V7_GAP1             : + EARLY_LOCK (SL → entry-0.5×ATR at profit ≥ 0.7×ATR)
  V7_GAP1_2           : + auto PARTIAL_CLOSE (30% @ +2×ATR, 30% @ +3×ATR)
  V7_GAP1_2_3         : + CHASE_TIGHT tune (5×ATR / 0.8 offset vs 4×ATR / 1.0 baseline)
  V7_FULL             : + Explosive BREAKOUT entry path (1h range≥1.5×ATR + vol≥3×)

Each simulation iterates bar-by-bar, supports trail SL tiers + partial closes.
"""

from __future__ import annotations
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from backtest_v3_v4 import (
    calc_atr, calc_ema, calc_rsi,
    detect_v4_signal, fetch_full_history,
    EMA_FAST, EMA_SLOW,
    WARMUP_BARS, LOOKAHEAD_BARS, SIGNAL_COOLDOWN_BARS,
)
from backtest_v5 import classify_mode, closes_4h_up_to, MODE_PARAMS, SLIPPAGE_PCT, _atr_at

SCRIPT_DIR = Path(__file__).resolve().parent
BACKTEST_DAYS = int(os.environ.get("BACKTEST_DAYS", "30"))
COINS = ["AAVE", "BNB", "BTC", "ETH", "LINK", "TRX", "XRP"]
SYMBOLS = {c: f"{c}USDT" for c in COINS}
VOL_REGIME_MAX_PCT = 2.5

# Gap 1: trail tier — same as live position_manager (descending order)
TRAIL_TIERS = [
    (5.0, 0.8, "CHASE_TIGHT"),
    (3.0, 1.5, "CHASE_WIDE"),
    (2.5, 1.5, "TRAIL_3"),
    (2.0, 1.0, "TRAIL_2"),
    (1.5, 0.5, "TRAIL_1"),
    (1.0, 0.0, "BREAKEVEN"),
    (0.7, -0.5, "EARLY_LOCK"),
]
# Baseline (Gap 3 not applied) — same except CHASE_TIGHT trigger
TRAIL_TIERS_BASELINE_CHASE = [
    (4.0, 1.0, "CHASE_TIGHT"),
    (3.0, 1.5, "CHASE_WIDE"),
    (2.5, 1.5, "TRAIL_3"),
    (2.0, 1.0, "TRAIL_2"),
    (1.5, 0.5, "TRAIL_1"),
    (1.0, 0.0, "BREAKEVEN"),
]
CHASE_TIER_NAMES = {"CHASE_TIGHT", "CHASE_WIDE"}

# Gap 2 partial levels
AUTO_PARTIAL_LEVELS = [
    {"trigger_atr": 2.0, "close_pct": 30, "label": "PROFIT_LOCK_2X"},
    {"trigger_atr": 3.0, "close_pct": 30, "label": "PROFIT_LOCK_3X"},
]


def _new_sl_for_tier(direction: str, entry: float, current: float, atr: float,
                      tier: tuple) -> float:
    """Compute new SL price for a trail tier."""
    min_atr, sl_offset, tier_name = tier
    if tier_name in CHASE_TIER_NAMES:
        return current - sl_offset * atr if direction == "LONG" else current + sl_offset * atr
    if sl_offset == 0.0:  # breakeven
        return entry
    return entry + sl_offset * atr if direction == "LONG" else entry - sl_offset * atr


def sim_v7(signal: dict, mode: str, future_bars: list, atr_initial: float,
           use_gap1_early_lock: bool = False,
           use_gap2_partial: bool = False,
           use_gap3_chase_tune: bool = False) -> dict:
    """Bar-by-bar simulator with trail SL + auto partial close.

    Returns: {result, bars, r, mode, partial_events, trail_events, exit_reason}
    """
    entry = signal["entry"]
    direction = signal["direction"]
    p = MODE_PARAMS[mode]
    initial_sl_dist = p["sl_mult"] * atr_initial
    tp_dist = p["tp_mult"] * atr_initial
    if direction == "LONG":
        sl = entry - initial_sl_dist
        tp = entry + tp_dist
    else:
        sl = entry + initial_sl_dist
        tp = entry - tp_dist

    initial_risk = abs(entry - sl)
    max_bars = min(p["timeout_bars"], len(future_bars))

    if max_bars == 0:
        return {"result": "NO_BARS", "bars": 0, "r": 0.0, "mode": mode}

    qty_remaining = 1.0
    closed_pnl_R = 0.0
    partials_taken = []
    partial_events = []
    trail_events = []
    current_tier = None

    tiers_to_use = TRAIL_TIERS if (use_gap1_early_lock and use_gap3_chase_tune) else (
        [t for t in TRAIL_TIERS if t[2] != "EARLY_LOCK"] if not use_gap1_early_lock else TRAIL_TIERS
    )
    if use_gap1_early_lock and not use_gap3_chase_tune:
        tiers_to_use = (
            [TRAIL_TIERS_BASELINE_CHASE[0]]
            + [t for t in TRAIL_TIERS_BASELINE_CHASE[1:] if True]
            + [(0.7, -0.5, "EARLY_LOCK")]
        )
    elif not use_gap1_early_lock and use_gap3_chase_tune:
        tiers_to_use = TRAIL_TIERS[:-1]  # drop EARLY_LOCK
    elif not use_gap1_early_lock and not use_gap3_chase_tune:
        tiers_to_use = TRAIL_TIERS_BASELINE_CHASE

    for i in range(max_bars):
        bar = future_bars[i]
        high = float(bar[2]); low = float(bar[3]); close = float(bar[4])

        if direction == "LONG":
            if low <= sl:
                exit_price = sl
                pnl_per_unit = exit_price - entry
                closed_pnl_R += (pnl_per_unit / initial_risk) * qty_remaining
                return {"result": "SL_HIT" if sl <= entry else "TRAIL_SL_HIT",
                         "bars": i + 1, "r": closed_pnl_R, "mode": mode,
                         "partial_events": partial_events, "trail_events": trail_events,
                         "exit_reason": f"SL @ {exit_price:.6f}"}
            if high >= tp:
                exit_price = tp
                pnl_per_unit = exit_price - entry
                closed_pnl_R += (pnl_per_unit / initial_risk) * qty_remaining
                return {"result": "TP_HIT", "bars": i + 1, "r": closed_pnl_R, "mode": mode,
                         "partial_events": partial_events, "trail_events": trail_events,
                         "exit_reason": f"TP @ {exit_price:.6f}"}
            current_high_profit = high - entry
            current_low_loss = entry - low
        else:
            if high >= sl:
                exit_price = sl
                pnl_per_unit = entry - exit_price
                closed_pnl_R += (pnl_per_unit / initial_risk) * qty_remaining
                return {"result": "SL_HIT" if sl >= entry else "TRAIL_SL_HIT",
                         "bars": i + 1, "r": closed_pnl_R, "mode": mode,
                         "partial_events": partial_events, "trail_events": trail_events,
                         "exit_reason": f"SL @ {exit_price:.6f}"}
            if low <= tp:
                exit_price = tp
                pnl_per_unit = entry - exit_price
                closed_pnl_R += (pnl_per_unit / initial_risk) * qty_remaining
                return {"result": "TP_HIT", "bars": i + 1, "r": closed_pnl_R, "mode": mode,
                         "partial_events": partial_events, "trail_events": trail_events,
                         "exit_reason": f"TP @ {exit_price:.6f}"}
            current_high_profit = entry - low
            current_low_loss = high - entry

        peak_profit_atr = current_high_profit / atr_initial if atr_initial > 0 else 0
        close_profit_atr = ((close - entry) if direction == "LONG" else (entry - close)) / atr_initial if atr_initial > 0 else 0

        if use_gap2_partial and qty_remaining > 0:
            for lvl in AUTO_PARTIAL_LEVELS:
                if peak_profit_atr < lvl["trigger_atr"]:
                    continue
                if lvl["label"] in partials_taken:
                    continue
                close_frac = lvl["close_pct"] / 100.0
                if close_frac > qty_remaining:
                    close_frac = qty_remaining
                if direction == "LONG":
                    fill_price = entry + lvl["trigger_atr"] * atr_initial
                else:
                    fill_price = entry - lvl["trigger_atr"] * atr_initial
                pnl_per_unit = (fill_price - entry) if direction == "LONG" else (entry - fill_price)
                closed_pnl_R += (pnl_per_unit / initial_risk) * close_frac
                qty_remaining -= close_frac
                partials_taken.append(lvl["label"])
                partial_events.append({"bar": i, "label": lvl["label"], "fraction": close_frac,
                                        "price": fill_price, "profit_atr_at_partial": lvl["trigger_atr"]})

        for tier in tiers_to_use:
            min_atr, sl_offset, tier_name = tier
            if peak_profit_atr >= min_atr:
                if tier_name in CHASE_TIER_NAMES:
                    ref_price = (entry + min_atr * atr_initial) if direction == "LONG" else (entry - min_atr * atr_initial)
                    new_sl = ref_price - sl_offset * atr_initial if direction == "LONG" else ref_price + sl_offset * atr_initial
                else:
                    new_sl = _new_sl_for_tier(direction, entry, close, atr_initial, tier)

                if direction == "LONG" and new_sl > sl:
                    sl = new_sl
                    if current_tier != tier_name:
                        trail_events.append({"bar": i, "tier": tier_name, "new_sl": sl,
                                              "profit_atr": peak_profit_atr})
                        current_tier = tier_name
                if direction == "SHORT" and new_sl < sl:
                    sl = new_sl
                    if current_tier != tier_name:
                        trail_events.append({"bar": i, "tier": tier_name, "new_sl": sl,
                                              "profit_atr": peak_profit_atr})
                        current_tier = tier_name
                break

    last_close = float(future_bars[max_bars - 1][4])
    pnl_per_unit = (last_close - entry) if direction == "LONG" else (entry - last_close)
    closed_pnl_R += (pnl_per_unit / initial_risk) * qty_remaining
    final_profit_atr = pnl_per_unit / atr_initial if atr_initial > 0 else 0
    if final_profit_atr > 0.3:
        result = "TIMEOUT_PROFIT"
    elif final_profit_atr > -0.3:
        result = "TIMEOUT_BE"
    else:
        result = "TIMEOUT_LOSS"
    return {"result": result, "bars": max_bars, "r": closed_pnl_R, "mode": mode,
             "partial_events": partial_events, "trail_events": trail_events,
             "exit_reason": "TIMEOUT"}


def detect_breakout(klines_1h, i, atr_initial):
    """Gap 4: explosive breakout detection on bar i.
    Returns dict {direction, entry, sl, tp, mode} or None.
    """
    if i < 21 or atr_initial <= 0:
        return None
    bar = klines_1h[i]
    last_high = float(bar[2]); last_low = float(bar[3])
    last_open = float(bar[1]); last_close = float(bar[4])
    last_vol = float(bar[5])
    avg_vol_20 = sum(float(klines_1h[j][5]) for j in range(i - 20, i)) / 20
    if avg_vol_20 <= 0:
        return None
    range_atr = (last_high - last_low) / atr_initial
    vol_ratio = last_vol / avg_vol_20
    if range_atr < 1.5 or vol_ratio < 3.0:
        return None
    direction = "LONG" if last_close > last_open else "SHORT"
    entry = last_close
    if direction == "LONG":
        sl = entry - 0.8 * atr_initial
        tp = entry + 2.5 * atr_initial
    else:
        sl = entry + 0.8 * atr_initial
        tp = entry - 2.5 * atr_initial
    return {"direction": direction, "entry": entry, "sl": sl, "tp": tp,
             "mode": "BREAKOUT", "range_atr": range_atr, "vol_ratio": vol_ratio}


# Add BREAKOUT mode params (tight SL, wide TP, short timeout)
BREAKOUT_MODE_PARAMS = {"sl_mult": 0.8, "tp_mult": 2.5, "timeout_bars": 4}


def sim_breakout(signal: dict, future_bars: list, atr_initial: float,
                  use_gap1: bool, use_gap2: bool, use_gap3: bool) -> dict:
    """Same as sim_v7 but with BREAKOUT mode params."""
    saved = MODE_PARAMS.get("BREAKOUT")
    MODE_PARAMS["BREAKOUT"] = BREAKOUT_MODE_PARAMS
    try:
        out = sim_v7(signal, "BREAKOUT", future_bars, atr_initial,
                      use_gap1_early_lock=use_gap1, use_gap2_partial=use_gap2,
                      use_gap3_chase_tune=use_gap3)
    finally:
        if saved is None:
            MODE_PARAMS.pop("BREAKOUT", None)
        else:
            MODE_PARAMS["BREAKOUT"] = saved
    return out


def backtest_coin_variants(coin: str, klines_1h: list, klines_4h: list) -> dict:
    """Run all 5 variants on the same coin's signal sequence."""
    closes_1h = [float(k[4]) for k in klines_1h]
    highs_1h = [float(k[2]) for k in klines_1h]
    lows_1h = [float(k[3]) for k in klines_1h]
    vols_1h = [float(k[5]) for k in klines_1h]
    times_1h = [int(k[0]) for k in klines_1h]
    closes_4h = [float(k[4]) for k in klines_4h]
    times_4h = [int(k[0]) for k in klines_4h]

    out = {
        "BASELINE": [],
        "GAP1": [],
        "GAP1_2": [],
        "GAP1_2_3": [],
        "FULL": [],
    }

    last_signal_bar = -100
    for i in range(WARMUP_BARS, len(klines_1h) - LOOKAHEAD_BARS):
        if i - last_signal_bar < SIGNAL_COOLDOWN_BARS:
            continue
        bar_t = times_1h[i]
        c1 = closes_1h[:i + 1]
        h1 = highs_1h[:i + 1]
        l1 = lows_1h[:i + 1]
        v1 = vols_1h[:i + 1]
        c4 = closes_4h_up_to(closes_4h, times_4h, bar_t)
        sig = detect_v4_signal(c1, h1, l1, v1, c4)
        if not sig:
            if i - last_signal_bar < SIGNAL_COOLDOWN_BARS:
                continue
            atr_for_breakout = calc_atr(h1, l1, c1)
            br = detect_breakout(klines_1h, i, atr_for_breakout) if atr_for_breakout else None
            if br:
                future = klines_1h[i + 1: i + 1 + LOOKAHEAD_BARS]
                br_sig = {"direction": br["direction"], "entry": br["entry"]}
                full_out = sim_breakout(br_sig, future, atr_for_breakout, True, True, True)
                full_out["is_breakout"] = True
                full_out["coin"] = coin
                out["FULL"].append({**br_sig, **full_out, "atr": atr_for_breakout})
                last_signal_bar = i
            continue

        atr = calc_atr(h1, l1, c1)
        if not atr or atr <= 0:
            continue
        atr_pct = atr / sig["entry"] * 100
        if atr_pct > VOL_REGIME_MAX_PCT:
            continue

        future = klines_1h[i + 1: i + 1 + LOOKAHEAD_BARS]
        mode = classify_mode(sig["direction"], c4)

        baseline = sim_v7(sig, mode, future, atr, False, False, False)
        gap1 = sim_v7(sig, mode, future, atr, True, False, False)
        gap1_2 = sim_v7(sig, mode, future, atr, True, True, False)
        gap1_2_3 = sim_v7(sig, mode, future, atr, True, True, True)
        full_emas = sim_v7(sig, mode, future, atr, True, True, True)

        common = {"coin": coin, "time": bar_t, "direction": sig["direction"],
                  "entry": sig["entry"], "mode": mode, "atr": atr}
        out["BASELINE"].append({**common, **baseline})
        out["GAP1"].append({**common, **gap1})
        out["GAP1_2"].append({**common, **gap1_2})
        out["GAP1_2_3"].append({**common, **gap1_2_3})
        out["FULL"].append({**common, **full_emas})
        last_signal_bar = i

    return out


def stats(trades: list) -> dict:
    if not trades:
        return {"n": 0, "win_rate": 0, "total_r": 0, "avg_r": 0, "pf": 0,
                "max_dd": 0, "wins": 0, "losses": 0, "be": 0,
                "avg_win": 0, "avg_loss": 0}
    n = len(trades)
    wins = sum(1 for t in trades if t["r"] > 0.01)
    losses = sum(1 for t in trades if t["r"] < -0.01)
    be = n - wins - losses
    total_r = sum(t["r"] for t in trades)
    win_r_sum = sum(t["r"] for t in trades if t["r"] > 0.01)
    loss_r_sum = sum(t["r"] for t in trades if t["r"] < -0.01)
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in trades:
        equity += t["r"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "n": n, "wins": wins, "losses": losses, "be": be,
        "win_rate": wins / n * 100 if n else 0,
        "total_r": total_r, "avg_r": total_r / n if n else 0,
        "pf": abs(win_r_sum / loss_r_sum) if loss_r_sum else (float("inf") if win_r_sum else 0),
        "max_dd": max_dd,
        "avg_win": win_r_sum / wins if wins else 0,
        "avg_loss": loss_r_sum / losses if losses else 0,
    }


def main():
    print(f"\n=== Backtest v7 — 4-Gap Upgrade Impact ({BACKTEST_DAYS} days, 7 coins) ===\n")
    all_variants = {"BASELINE": [], "GAP1": [], "GAP1_2": [], "GAP1_2_3": [], "FULL": []}

    for coin, sym in SYMBOLS.items():
        try:
            t0 = time.time()
            k1h = fetch_full_history(sym, "1h", BACKTEST_DAYS)
            k4h = fetch_full_history(sym, "4h", BACKTEST_DAYS)
            res = backtest_coin_variants(coin, k1h, k4h)
            for k, v in res.items():
                all_variants[k].extend(v)
            print(f"  {coin:6} done in {time.time()-t0:.1f}s — "
                  f"BASELINE n={len(res['BASELINE']):>3}  FULL n={len(res['FULL']):>3} "
                  f"(breakouts={len([t for t in res['FULL'] if t.get('is_breakout')])})")
        except Exception as e:
            print(f"  {coin}: ERROR {e}")

    print("\n" + "="*100)
    print(f"{'Variant':14}  {'N':>5}  {'WinR':>6}  {'TotalR':>8}  {'AvgR':>8}  {'PF':>5}  "
          f"{'MaxDD':>7}  {'AvgW':>7}  {'AvgL':>7}")
    print("="*100)
    summary = {}
    for k, trades in all_variants.items():
        s = stats(trades)
        summary[k] = s
        print(f"{k:14}  {s['n']:>5}  {s['win_rate']:>5.1f}%  {s['total_r']:>+8.2f}  "
              f"{s['avg_r']:>+8.3f}  {s['pf']:>5.2f}  {s['max_dd']:>7.2f}  "
              f"{s['avg_win']:>+7.2f}  {s['avg_loss']:>+7.2f}")
    print("="*100)

    base = summary["BASELINE"]["total_r"]
    print("\nDelta vs BASELINE:")
    for k in ["GAP1", "GAP1_2", "GAP1_2_3", "FULL"]:
        delta = summary[k]["total_r"] - base
        print(f"  {k:14}  ΔR = {delta:+.2f}  (n_trades_diff = {summary[k]['n'] - summary['BASELINE']['n']:+d})")

    breakouts = [t for t in all_variants["FULL"] if t.get("is_breakout")]
    if breakouts:
        bs = stats(breakouts)
        print(f"\nBreakout-only stats: n={bs['n']} WR={bs['win_rate']:.1f}% R={bs['total_r']:+.2f} avgR={bs['avg_r']:+.3f}")

    out = SCRIPT_DIR / "data" / "backtest_v7_results.json"
    out.write_text(json.dumps({
        "backtest_at": datetime.now(timezone.utc).isoformat(),
        "days": BACKTEST_DAYS,
        "coins": COINS,
        "summary": summary,
    }, indent=2, default=str))
    print(f"\nSaved → {out}")


if __name__ == "__main__":
    main()
