#!/usr/bin/env python3
"""
Backtest v5 — Timeout-aware + Pyramiding.

Compares 3 variants on the SAME signal set (EMA20/50 cross + 4h MTF asymmetric,
i.e. same as live algo):

  V5_BASELINE: static SL=2xATR, TP=3xATR, no timeout, no trail
  V5_TIMEOUT : mode-aware (SWING/SCALP) SL/TP + profit-aware close at timeout
  V5_PYRAMID : V5_TIMEOUT + add legs at +1.5/+2.5 ATR profit + chase trail SL

Reuses indicator math + signal detection from backtest_v3_v4.py to guarantee
"same signals, different management" comparison.

Usage:
    python3 backtest_v5.py
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from backtest_v3_v4 import (
    calc_ema,
    calc_atr,
    detect_v4_signal,
    fetch_full_history,
    EMA_FAST,
    EMA_SLOW,
    WARMUP_BARS,
    LOOKAHEAD_BARS,
    SIGNAL_COOLDOWN_BARS,
)

SCRIPT_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Backtest config (fast: 30 days x 10 coins)
# ---------------------------------------------------------------------------
BACKTEST_DAYS = 30
COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "AAVE"]
SYMBOLS = {c: f"{c}USDT" for c in COINS}

# Slippage + taker fee per side (Binance Futures realistic)
SLIPPAGE_PCT = 0.0005  # 0.05% per side -> 0.10% round trip per leg

# Mode params (mirror live patch in binance_price_alert.py)
MODE_PARAMS = {
    "SWING": {"sl_mult": 2.0, "tp_mult": 3.0, "timeout_bars": 8},
    "SCALP": {"sl_mult": 1.0, "tp_mult": 1.5, "timeout_bars": 4},
}

# Baseline (v4 live-pre-patch) static params
BASELINE_SL_MULT = 2.0
BASELINE_TP_MULT = 3.0

# Pyramid sizing (fractional, leg gốc = 1.0)
PYRAMID_LEGS = [
    {"trigger_atr": 1.5, "qty_frac": 0.5},   # leg 1: add at +1.5 ATR profit, 50% size
    {"trigger_atr": 2.5, "qty_frac": 0.25},  # leg 2: add at +2.5 ATR profit, 25% size
]
PYRAMID_VOL_REGIME_MAX = 1.5  # skip add if ATR_now > 1.5 * ATR_entry

# Trail SL CHASE tiers (mirror position_manager.py)
# (min_profit_atr, sl_offset_atr, name)
CHASE_TIERS = [
    (4.0, 1.0, "CHASE_TIGHT"),  # SL = current ± 1.0 ATR
    (3.0, 1.5, "CHASE_WIDE"),   # SL = current ± 1.5 ATR
]

# Profit-aware timeout thresholds
TIMEOUT_PROFIT_THRESHOLD = 0.3   # profit_atr above this → lock profit
TIMEOUT_LOSS_THRESHOLD = -0.3    # profit_atr below this → cut loss

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def classify_mode(direction: str, closes_4h: list) -> str:
    """SWING if 4h aligned with direction, else SCALP.

    LONG  + 4h bullish  → SWING
    SHORT + 4h bearish  → SWING
    Otherwise           → SCALP
    """
    if len(closes_4h) < EMA_SLOW:
        return "SCALP"
    ema_f = calc_ema(closes_4h, EMA_FAST)
    ema_s = calc_ema(closes_4h, EMA_SLOW)
    if ema_f is None or ema_s is None:
        return "SCALP"
    htf_bull = ema_f > ema_s
    if direction == "LONG" and htf_bull:
        return "SWING"
    if direction == "SHORT" and not htf_bull:
        return "SWING"
    return "SCALP"


def htf_still_aligned(direction: str, closes_4h_now: list) -> bool:
    """Re-check HTF alignment at any bar (no look-ahead — caller must pre-slice)."""
    if len(closes_4h_now) < EMA_SLOW:
        return False
    ema_f = calc_ema(closes_4h_now, EMA_FAST)
    ema_s = calc_ema(closes_4h_now, EMA_SLOW)
    if ema_f is None or ema_s is None:
        return False
    if direction == "LONG":
        return ema_f > ema_s
    return ema_f < ema_s


def closes_4h_up_to(closes_4h_full: list, times_4h: list, bar_time_ms: int) -> list:
    """Slice 4h closes to only those whose closeTime <= bar_time_ms."""
    return [c for j, c in enumerate(closes_4h_full) if times_4h[j] <= bar_time_ms]


def chase_trail(direction: str, entry: float, current: float, atr: float,
                current_sl: float) -> float:
    """Return new SL from CHASE tiers (anchored to CURRENT price), or current_sl if no tier."""
    if direction == "LONG":
        profit_atr = (current - entry) / atr
    else:
        profit_atr = (entry - current) / atr
    for min_atr, sl_offset, _name in CHASE_TIERS:
        if profit_atr >= min_atr:
            if direction == "LONG":
                new_sl = current - sl_offset * atr
                return max(new_sl, current_sl)
            new_sl = current + sl_offset * atr
            return min(new_sl, current_sl)
    return current_sl


def pnl_R(direction: str, entry: float, exit_price: float, qty_frac: float,
          initial_risk_per_unit: float) -> float:
    """PnL in R-multiples of original-leg risk, including round-trip slippage."""
    if direction == "LONG":
        gross = (exit_price - entry) * qty_frac
    else:
        gross = (entry - exit_price) * qty_frac
    fees = (entry + exit_price) * qty_frac * SLIPPAGE_PCT
    return (gross - fees) / initial_risk_per_unit


# ---------------------------------------------------------------------------
# Simulator 1: BASELINE (static SL/TP, no timeout, no trail)
# ---------------------------------------------------------------------------

def sim_baseline(signal: dict, future_bars: list, atr: float) -> dict:
    """Static SL=2xATR, TP=3xATR, max LOOKAHEAD bars."""
    entry = signal["entry"]
    direction = signal["direction"]
    if direction == "LONG":
        sl = entry - BASELINE_SL_MULT * atr
        tp = entry + BASELINE_TP_MULT * atr
    else:
        sl = entry + BASELINE_SL_MULT * atr
        tp = entry - BASELINE_TP_MULT * atr
    initial_risk = abs(entry - sl)

    for i, bar in enumerate(future_bars):
        high = float(bar[2])
        low = float(bar[3])
        if direction == "LONG":
            if low <= sl:
                return _result("SL", sl, i + 1, pnl_R(direction, entry, sl, 1.0, initial_risk))
            if high >= tp:
                return _result("TP", tp, i + 1, pnl_R(direction, entry, tp, 1.0, initial_risk))
        else:
            if high >= sl:
                return _result("SL", sl, i + 1, pnl_R(direction, entry, sl, 1.0, initial_risk))
            if low <= tp:
                return _result("TP", tp, i + 1, pnl_R(direction, entry, tp, 1.0, initial_risk))

    last_close = float(future_bars[-1][4]) if future_bars else entry
    return _result("EXPIRED", last_close, len(future_bars),
                   pnl_R(direction, entry, last_close, 1.0, initial_risk))


# ---------------------------------------------------------------------------
# Simulator 2: TIMEOUT (mode-aware SL/TP + profit-aware close at timeout)
# ---------------------------------------------------------------------------

def sim_timeout(signal: dict, mode: str, future_bars: list, atr: float) -> dict:
    """Mode-aware SL/TP + close at timeout_bars based on profit_atr."""
    entry = signal["entry"]
    direction = signal["direction"]
    p = MODE_PARAMS[mode]
    if direction == "LONG":
        sl = entry - p["sl_mult"] * atr
        tp = entry + p["tp_mult"] * atr
    else:
        sl = entry + p["sl_mult"] * atr
        tp = entry - p["tp_mult"] * atr
    initial_risk = abs(entry - sl)
    max_bars = min(p["timeout_bars"], len(future_bars))

    for i in range(max_bars):
        bar = future_bars[i]
        high = float(bar[2])
        low = float(bar[3])
        if direction == "LONG":
            if low <= sl:
                return _result("SL", sl, i + 1,
                               pnl_R(direction, entry, sl, 1.0, initial_risk),
                               mode=mode)
            if high >= tp:
                return _result("TP", tp, i + 1,
                               pnl_R(direction, entry, tp, 1.0, initial_risk),
                               mode=mode)
        else:
            if high >= sl:
                return _result("SL", sl, i + 1,
                               pnl_R(direction, entry, sl, 1.0, initial_risk),
                               mode=mode)
            if low <= tp:
                return _result("TP", tp, i + 1,
                               pnl_R(direction, entry, tp, 1.0, initial_risk),
                               mode=mode)

    if max_bars == 0:
        return _result("EXPIRED", entry, 0, 0.0, mode=mode)
    last_close = float(future_bars[max_bars - 1][4])
    if direction == "LONG":
        profit_atr = (last_close - entry) / atr
    else:
        profit_atr = (entry - last_close) / atr

    if profit_atr > TIMEOUT_PROFIT_THRESHOLD:
        result = "TIMEOUT_PROFIT"
    elif profit_atr > TIMEOUT_LOSS_THRESHOLD:
        result = "TIMEOUT_BE"
    else:
        result = "TIMEOUT_LOSS"
    return _result(result, last_close, max_bars,
                   pnl_R(direction, entry, last_close, 1.0, initial_risk),
                   mode=mode)


# ---------------------------------------------------------------------------
# Simulator 3: PYRAMID (multi-leg + chase trail + timeout)
# ---------------------------------------------------------------------------

def sim_pyramid(signal: dict, mode: str, future_bars: list, atr_initial: float,
                bars_1h_full: list, idx_signal: int,
                closes_4h_full: list, times_4h: list) -> dict:
    """Multi-leg pyramid with chase trail SL and mode-aware timeout."""
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

    last_price = entry
    bars_held = 0

    for i in range(max_bars):
        bar = future_bars[i]
        high = float(bar[2])
        low = float(bar[3])
        close = float(bar[4])
        bars_held = i + 1

        # 1) Check SL / TP for each active leg this bar
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

        active_legs = [l for l in legs if l["active"]]
        if not active_legs:
            return _pyramid_result(closed_pnl_R, bars_held, mode, legs, add_events,
                                   final_reason="ALL_EXITED")

        # 2) Use bar close as "current price" for pyramid + trail decisions
        last_price = close
        if direction == "LONG":
            profit_atr = (last_price - entry) / atr_initial
        else:
            profit_atr = (entry - last_price) / atr_initial

        # 3) Recompute current ATR (using bars up to and including current bar)
        global_idx = idx_signal + 1 + i
        atr_now = _atr_at(bars_1h_full, global_idx, period=14)

        # 4) Chase trail: anchor to current price, lift all active leg SLs
        if atr_now and atr_now > 0:
            for leg in active_legs:
                new_sl = chase_trail(direction, entry, last_price, atr_now, leg["sl"])
                if direction == "LONG" and new_sl > leg["sl"]:
                    leg["sl"] = new_sl
                elif direction == "SHORT" and new_sl < leg["sl"]:
                    leg["sl"] = new_sl

        # 5) Pyramid add logic
        bar_time_ms = int(bar[6])  # closeTime
        for leg_cfg_idx, cfg in enumerate(PYRAMID_LEGS):
            target_leg_idx = leg_cfg_idx + 1
            already_added = any(l["leg_idx"] == target_leg_idx for l in legs)
            if already_added:
                continue
            if profit_atr < cfg["trigger_atr"]:
                continue
            # Volatility regime check
            if atr_now and atr_now > PYRAMID_VOL_REGIME_MAX * atr_initial:
                continue
            # HTF re-check
            c4_now = closes_4h_up_to(closes_4h_full, times_4h, bar_time_ms)
            if not htf_still_aligned(direction, c4_now):
                continue
            # Add this leg
            anchor_entry = legs[target_leg_idx - 1]["entry"]  # SL = entry of previous leg
            legs.append({
                "entry": last_price,
                "qty_frac": cfg["qty_frac"],
                "sl": anchor_entry,
                "active": True,
                "leg_idx": target_leg_idx,
            })
            add_events.append({
                "leg_idx": target_leg_idx,
                "bar": i + 1,
                "entry": last_price,
                "profit_atr": profit_atr,
                "atr_ratio": (atr_now / atr_initial) if atr_now else None,
            })

    # Timeout: close remaining active legs at last close
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

    if profit_atr_final > TIMEOUT_PROFIT_THRESHOLD:
        final_reason = "TIMEOUT_PROFIT"
    elif profit_atr_final > TIMEOUT_LOSS_THRESHOLD:
        final_reason = "TIMEOUT_BE"
    else:
        final_reason = "TIMEOUT_LOSS"

    return _pyramid_result(closed_pnl_R, bars_held, mode, legs, add_events,
                           final_reason=final_reason)


def _atr_at(bars: list, idx: int, period: int = 14) -> float | None:
    """ATR computed from bars up to and including idx."""
    if idx < period + 1:
        return None
    highs = [float(b[2]) for b in bars[:idx + 1]]
    lows = [float(b[3]) for b in bars[:idx + 1]]
    closes = [float(b[4]) for b in bars[:idx + 1]]
    return calc_atr(highs, lows, closes, period=period)


def _result(result: str, exit_price: float, bars: int, r: float, mode: str = "") -> dict:
    return {"result": result, "exit": exit_price, "bars": bars, "r": r, "mode": mode}


def _pyramid_result(total_r: float, bars: int, mode: str, legs: list,
                    add_events: list, final_reason: str) -> dict:
    return {
        "result": final_reason,
        "bars": bars,
        "r": total_r,
        "mode": mode,
        "n_legs": len(legs),
        "n_adds": len(add_events),
        "legs": [
            {k: v for k, v in l.items() if k in ("entry", "qty_frac", "leg_idx",
                                                 "exit", "exit_reason")}
            for l in legs
        ],
        "add_events": add_events,
    }


# ---------------------------------------------------------------------------
# Backtest runner
# ---------------------------------------------------------------------------

def backtest_coin(coin: str, klines_1h: list, klines_4h: list) -> tuple:
    """Walk-forward over 1h klines, detect signals, run all 3 simulators."""
    closes_1h = [float(k[4]) for k in klines_1h]
    highs_1h = [float(k[2]) for k in klines_1h]
    lows_1h = [float(k[3]) for k in klines_1h]
    vols_1h = [float(k[5]) for k in klines_1h]
    times_1h = [int(k[0]) for k in klines_1h]

    closes_4h = [float(k[4]) for k in klines_4h]
    times_4h = [int(k[0]) for k in klines_4h]

    base_trades, to_trades, py_trades = [], [], []
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

        common = {
            "coin": coin, "time": bar_time, "direction": sig["direction"],
            "entry": sig["entry"], "atr": atr_initial,
            "rsi": sig.get("rsi"), "trend": sig.get("trend"),
            "ema_gap": sig.get("ema_gap"), "vol": sig.get("vol"),
            "mode": mode,
        }

        base_out = sim_baseline(sig, future_bars, atr_initial)
        base_trades.append({**common, **base_out, "mode": mode})

        to_out = sim_timeout(sig, mode, future_bars, atr_initial)
        to_trades.append({**common, **to_out})

        py_out = sim_pyramid(sig, mode, future_bars, atr_initial,
                             klines_1h, i, closes_4h, times_4h)
        py_trades.append({**common, **py_out})

        last_signal_bar = i

    return base_trades, to_trades, py_trades


# ---------------------------------------------------------------------------
# Reporting
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
    print(f"  Signals: {s['n']} | Win rate: {s['win_rate']:.1f}% ({s['wins']}/{s['n']})")
    print(f"  Total R: {s['total_r']:+.2f} | Avg R: {s['avg_r']:+.3f}")
    print(f"  Avg win: +{s['avg_win_r']:.2f}R | Avg loss: {s['avg_loss_r']:.2f}R | PF: {pf_str}")
    print(f"  Avg hold: {s['avg_hold']:.1f} bars | Max DD: {s['max_dd']:.2f}R")

    # Outcome breakdown
    outcomes = {}
    for t in trades:
        outcomes[t["result"]] = outcomes.get(t["result"], 0) + 1
    print(f"  Outcomes: {dict(sorted(outcomes.items()))}")

    # Per-mode breakdown (if mode field present)
    by_mode = {}
    for t in trades:
        by_mode.setdefault(t.get("mode", "?"), []).append(t)
    if len(by_mode) > 1 or list(by_mode.keys())[0] != "":
        print("  Per-mode:")
        for mode in sorted(by_mode):
            ts = by_mode[mode]
            sm = _stats(ts)
            print(f"    {mode:<6} {sm['n']:>3} | win {sm['win_rate']:>4.0f}% | "
                  f"total {sm['total_r']:+.2f}R | avg {sm['avg_r']:+.3f}R")

    # Per-coin breakdown
    by_coin = {}
    for t in trades:
        by_coin.setdefault(t["coin"], []).append(t)
    print("  Per-coin:")
    for coin in sorted(by_coin):
        ts = by_coin[coin]
        sm = _stats(ts)
        print(f"    {coin:<6} {sm['n']:>3} | win {sm['win_rate']:>4.0f}% | "
              f"total {sm['total_r']:+.2f}R")


def report_pyramid_extra(trades: list):
    if not trades:
        return
    n = len(trades)
    n_with_add = sum(1 for t in trades if t.get("n_adds", 0) > 0)
    total_adds = sum(t.get("n_adds", 0) for t in trades)
    add1_count = sum(1 for t in trades if t.get("n_adds", 0) >= 1)
    add2_count = sum(1 for t in trades if t.get("n_adds", 0) >= 2)
    pyramid_r = sum(t["r"] for t in trades if t.get("n_adds", 0) > 0)
    no_add_r = sum(t["r"] for t in trades if t.get("n_adds", 0) == 0)
    print("\n  Pyramid stats:")
    print(f"    Trades with adds: {n_with_add}/{n} ({n_with_add/n*100:.0f}%)")
    print(f"    Total leg adds:   {total_adds}  (leg1: {add1_count}, leg2: {add2_count})")
    print(f"    R from pyramided trades: {pyramid_r:+.2f}")
    print(f"    R from single-leg trades: {no_add_r:+.2f}")


def comparison_table(base: list, to: list, py: list):
    print("\n" + "=" * 88)
    print("  3-VARIANT COMPARISON")
    print("=" * 88)
    print(f"  {'Variant':<14} {'Signals':>8} {'WinRate':>9} {'TotalR':>9} {'AvgR':>8} {'MaxDD':>8} {'AvgHold':>9}")
    for name, trades in [("V5_BASELINE", base), ("V5_TIMEOUT", to), ("V5_PYRAMID", py)]:
        s = _stats(trades)
        if s["n"] == 0:
            print(f"  {name:<14}    no signals")
            continue
        print(f"  {name:<14} {s['n']:>8} {s['win_rate']:>8.1f}% {s['total_r']:>+9.2f} "
              f"{s['avg_r']:>+8.3f} {s['max_dd']:>8.2f} {s['avg_hold']:>8.1f}h")

    if base and to and py:
        bs = _stats(base); ts = _stats(to); ps = _stats(py)
        print()
        print(f"  Δ TIMEOUT vs BASELINE:")
        print(f"    Win rate: {ts['win_rate'] - bs['win_rate']:+.1f}pp | "
              f"Total R: {ts['total_r'] - bs['total_r']:+.2f} | "
              f"Avg R: {ts['avg_r'] - bs['avg_r']:+.3f} | "
              f"Max DD: {ts['max_dd'] - bs['max_dd']:+.2f}")
        print(f"  Δ PYRAMID vs TIMEOUT:")
        print(f"    Win rate: {ps['win_rate'] - ts['win_rate']:+.1f}pp | "
              f"Total R: {ps['total_r'] - ts['total_r']:+.2f} | "
              f"Avg R: {ps['avg_r'] - ts['avg_r']:+.3f} | "
              f"Max DD: {ps['max_dd'] - ts['max_dd']:+.2f}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 70)
    print(f"  BACKTEST v5 — {BACKTEST_DAYS} days × {len(COINS)} coins")
    print(f"  Same signals (v4 EMA cross). 3 management variants.")
    print(f"  Slippage: {SLIPPAGE_PCT*100:.2f}% per side ({SLIPPAGE_PCT*200:.2f}% round-trip/leg)")
    print("=" * 70)

    all_base, all_to, all_py = [], [], []
    t0 = time.time()

    for coin in COINS:
        symbol = SYMBOLS[coin]
        print(f"\n  [{coin}] Fetching klines...")
        try:
            k1h = fetch_full_history(symbol, "1h", BACKTEST_DAYS)
            k4h = fetch_full_history(symbol, "4h", BACKTEST_DAYS)
            print(f"    {len(k1h)} × 1h + {len(k4h)} × 4h bars")
            b, t, p = backtest_coin(coin, k1h, k4h)
            print(f"    Signals: {len(b)} | "
                  f"BASELINE total R: {sum(x['r'] for x in b):+.2f} | "
                  f"TIMEOUT: {sum(x['r'] for x in t):+.2f} | "
                  f"PYRAMID: {sum(x['r'] for x in p):+.2f}")
            all_base.extend(b)
            all_to.extend(t)
            all_py.extend(p)
        except Exception as e:
            print(f"    ERROR: {e}")

    elapsed = time.time() - t0
    print(f"\n  Backtest completed in {elapsed:.1f}s")

    report_variant("V5_BASELINE (static SL=2x, TP=3x, no timeout)", all_base)
    report_variant("V5_TIMEOUT  (mode-aware SL/TP + profit-aware timeout)", all_to)
    report_variant("V5_PYRAMID  (timeout + add legs at +1.5/+2.5 ATR + chase trail)", all_py)
    report_pyramid_extra(all_py)
    comparison_table(all_base, all_to, all_py)

    out = SCRIPT_DIR / "data" / "backtest_v5_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "backtest_at": datetime.now(timezone.utc).isoformat(),
        "days": BACKTEST_DAYS, "coins": COINS,
        "slippage_pct": SLIPPAGE_PCT,
        "mode_params": MODE_PARAMS,
        "pyramid_legs": PYRAMID_LEGS,
        "baseline_trades": all_base,
        "timeout_trades": all_to,
        "pyramid_trades": all_py,
    }, indent=2, default=str))
    print(f"\n  Raw results saved to {out}")


if __name__ == "__main__":
    main()
