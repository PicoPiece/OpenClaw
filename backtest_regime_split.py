#!/usr/bin/env python3
"""Backtest split by regime: chia 90d thành 3 window 30d, chạy backtest từng window.

Mục đích: Xác minh giả thuyết "system làm tiền trong uptrend, lỗ trong sideways/downtrend".
"""

from __future__ import annotations
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from backtest_v3_v4 import (
    calc_atr, detect_v4_signal, fetch_full_history,
    WARMUP_BARS, LOOKAHEAD_BARS, SIGNAL_COOLDOWN_BARS,
)
from backtest_v5 import classify_mode, closes_4h_up_to, sim_timeout

COINS = ["BTC", "ETH", "BNB", "XRP", "AAVE", "LINK", "TRX"]
SYMBOLS = {c: f"{c}USDT" for c in COINS}
VOL_REGIME_MAX_PCT = 2.5

WINDOWS = [
    {"label": "W1 (Feb-Mar)",  "days_back_start": 90, "days_back_end": 60, "regime_btc": "UPTREND +8.3% (corr -19%)"},
    {"label": "W2 (Mar-Apr)",  "days_back_start": 60, "days_back_end": 30, "regime_btc": "SIDEWAYS +2.6%"},
    {"label": "W3 (Apr-May)",  "days_back_start": 30, "days_back_end": 0,  "regime_btc": "UPTREND +17.8%"},
]


def backtest_in_window(coin, klines_1h, klines_4h, t_start_ms, t_end_ms):
    closes_1h = [float(k[4]) for k in klines_1h]
    highs_1h = [float(k[2]) for k in klines_1h]
    lows_1h = [float(k[3]) for k in klines_1h]
    vols_1h = [float(k[5]) for k in klines_1h]
    times_1h = [int(k[0]) for k in klines_1h]
    closes_4h = [float(k[4]) for k in klines_4h]
    times_4h = [int(k[0]) for k in klines_4h]

    trades, last_signal_bar = [], -100
    for i in range(WARMUP_BARS, len(klines_1h) - LOOKAHEAD_BARS):
        bar_t = times_1h[i]
        if bar_t < t_start_ms or bar_t > t_end_ms:
            continue
        if i - last_signal_bar < SIGNAL_COOLDOWN_BARS:
            continue
        c1 = closes_1h[:i+1]; h1 = highs_1h[:i+1]; l1 = lows_1h[:i+1]; v1 = vols_1h[:i+1]
        c4 = closes_4h_up_to(closes_4h, times_4h, bar_t)
        sig = detect_v4_signal(c1, h1, l1, v1, c4)
        if not sig: continue
        atr = calc_atr(h1, l1, c1)
        if not atr or atr <= 0: continue
        atr_pct = atr / sig["entry"] * 100
        if atr_pct > VOL_REGIME_MAX_PCT: continue
        future = klines_1h[i+1: i+1+LOOKAHEAD_BARS]
        mode = classify_mode(sig["direction"], c4)
        out = sim_timeout(sig, mode, future, atr)
        trades.append({"coin": coin, "direction": sig["direction"], "mode": mode, **out})
        last_signal_bar = i
    return trades


def stats(trades):
    if not trades: return {"n": 0}
    n = len(trades)
    wins = sum(1 for t in trades if t["r"] > 0)
    losses = sum(1 for t in trades if t["r"] <= 0)
    wr = sum(t["r"] for t in trades if t["r"] > 0)
    lr = sum(t["r"] for t in trades if t["r"] <= 0)
    return {
        "n": n,
        "wins": wins, "losses": losses,
        "win_rate": wins/n*100 if n else 0,
        "total_r": sum(t["r"] for t in trades),
        "avg_r": sum(t["r"] for t in trades) / n,
        "pf": abs(wr/lr) if lr else float("inf"),
    }


def main():
    print(f"\n=== REGIME-SPLIT BACKTEST — coins: {','.join(COINS)} ===\n")
    end = datetime.now(timezone.utc)
    klines_cache = {}
    print("Fetching 90d history per coin ...")
    for coin, sym in SYMBOLS.items():
        try:
            k1h = fetch_full_history(sym, "1h", 95)
            k4h = fetch_full_history(sym, "4h", 95)
            klines_cache[coin] = (k1h, k4h)
            print(f"  {coin}: {len(k1h)} 1h bars, {len(k4h)} 4h bars")
        except Exception as e:
            print(f"  {coin}: ERR {e}")

    print("\n" + "="*90)
    print(f"{'Window':14}  {'Regime':30}  {'N':>4} {'WR%':>6} {'TotalR':>8} {'AvgR':>7} {'PF':>5}")
    print("="*90)
    overall_summary = {}
    for w in WINDOWS:
        t_end = int((end.timestamp() - w["days_back_end"]*86400) * 1000)
        t_start = int((end.timestamp() - w["days_back_start"]*86400) * 1000)
        all_trades = []
        per_coin = {}
        for coin in COINS:
            if coin not in klines_cache: continue
            k1h, k4h = klines_cache[coin]
            t = backtest_in_window(coin, k1h, k4h, t_start, t_end)
            all_trades.extend(t)
            per_coin[coin] = stats(t)
        s = stats(all_trades)
        overall_summary[w["label"]] = (s, per_coin)
        if s["n"]:
            print(f"{w['label']:14}  {w['regime_btc']:30}  {s['n']:>4} {s['win_rate']:>6.1f} {s['total_r']:>+8.2f} {s['avg_r']:>+7.3f} {s['pf']:>5.2f}")
        else:
            print(f"{w['label']:14}  {w['regime_btc']:30}  no trades")

    print("\n" + "="*90)
    print("PER-COIN BREAKDOWN BY WINDOW")
    print("="*90)
    print(f"{'Coin':6} {'W1 N/WR/R':>22} {'W2 N/WR/R':>22} {'W3 N/WR/R':>22}")
    for coin in COINS:
        row = f"{coin:6}"
        for label in [w["label"] for w in WINDOWS]:
            s = overall_summary[label][1].get(coin, {"n":0})
            if s["n"]:
                row += f"  {s['n']:>3}/{s['win_rate']:>4.0f}%/{s['total_r']:>+6.2f}  "
            else:
                row += f"  {'--':>20}  "
        print(row)


if __name__ == "__main__":
    main()
