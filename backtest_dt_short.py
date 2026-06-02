#!/usr/bin/env python3
"""Backtest the DOWNTREND CONTINUATION SHORT path.

Self-contained: fetches 1h + 4h klines from Binance Futures and simulates the
DT_SHORT entry rules exactly as implemented in binance_price_alert.py.

Usage: python3 backtest_dt_short.py [days]
"""
from __future__ import annotations
import sys
import json
import urllib.request
from datetime import datetime, timezone

FAPI = "https://fapi.binance.com"
ALLOWLIST = ["AAVE", "ETH", "LINK", "BNB", "XRP", "BTC", "TRX", "INJ", "ORDI", "ATOM", "ENA"]

# Params mirror binance_price_alert.py DT_SHORT defaults
RSI_PERIOD = 14
EMA_FAST = 20
EMA_SLOW = 50
ATR_PERIOD = 14
DT_RSI_MIN = 40.0
DT_RSI_MAX = 65.0
DT_EMA_PROX_PCT = 1.5
DT_MIN_VOL = 0.6
DT_SL_ATR = 1.0
DT_TP_ATR = 2.0
SIGNAL_COOLDOWN_H = 4


def fetch_klines(symbol: str, interval: str, limit: int = 1000) -> list:
    url = f"{FAPI}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "curl"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def ema_series(values: list[float], period: int) -> list[float]:
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi_series(closes: list[float], period: int = 14) -> list[float]:
    out = [50.0] * len(closes)
    if len(closes) <= period:
        return out
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(abs(min(d, 0)))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(closes)):
        if i > period:
            avg_g = (avg_g * (period - 1) + gains[i - 1]) / period
            avg_l = (avg_l * (period - 1) + losses[i - 1]) / period
        rs = avg_g / avg_l if avg_l else 999
        out[i] = 100 - 100 / (1 + rs)
    return out


def atr_series(highs, lows, closes, period=14) -> list[float]:
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    out = [trs[0]] * len(closes)
    a = sum(trs[:period]) / period
    for i in range(period, len(closes)):
        a = (a * (period - 1) + trs[i]) / period
        out[i] = a
    return out


def trend_label(price, ema20, ema50):
    if price > ema20 > ema50:
        return "UPTREND"
    if price < ema20 < ema50:
        return "DOWNTREND"
    if ema20 < ema50:
        return "NEUTRAL-BEAR"
    return "NEUTRAL-BULL"


def _load_coin_data(symbol: str, days: int):
    hours = min(days * 24, 1000)
    k1h = fetch_klines(symbol, "1h", hours)
    k4h = fetch_klines(symbol, "4h", min(days * 6, 1000))
    if len(k1h) < 60 or len(k4h) < 50:
        return None
    closes = [float(k[4]) for k in k1h]
    highs = [float(k[2]) for k in k1h]
    lows = [float(k[3]) for k in k1h]
    vols = [float(k[5]) for k in k1h]
    times = [int(k[0]) for k in k1h]
    data = {
        "closes": closes, "highs": highs, "lows": lows, "vols": vols, "times": times,
        "ema20": ema_series(closes, EMA_FAST), "ema50": ema_series(closes, EMA_SLOW),
        "rsi": rsi_series(closes, RSI_PERIOD), "atr": atr_series(highs, lows, closes, ATR_PERIOD),
    }
    c4 = [float(k[4]) for k in k4h]
    t4 = [int(k[0]) for k in k4h]
    e4_20 = ema_series(c4, EMA_FAST)
    e4_50 = ema_series(c4, EMA_SLOW)
    # precompute 4h bearish + 4h gap pct mapped per 1h bar
    bear4 = []
    gap4 = []
    for ts in times:
        idx = 0
        for j in range(len(t4)):
            if t4[j] <= ts:
                idx = j
            else:
                break
        bear4.append(e4_20[idx] < e4_50[idx])
        gap4.append((e4_50[idx] - e4_20[idx]) / e4_50[idx] * 100 if e4_50[idx] else 0)
    data["bear4"] = bear4
    data["gap4"] = gap4
    return data


def backtest_coin(symbol: str, days: int, p: dict, _cache={}) -> list[dict]:
    key = (symbol, days)
    if key not in _cache:
        _cache[key] = _load_coin_data(symbol, days)
    data = _cache[key]
    if not data:
        return []
    closes, highs, lows, vols, times = data["closes"], data["highs"], data["lows"], data["vols"], data["times"]
    ema20, ema50, rsi, atr = data["ema20"], data["ema50"], data["rsi"], data["atr"]
    bear4, gap4 = data["bear4"], data["gap4"]

    trades = []
    last_signal_ts = 0
    i = 50
    while i < len(closes) - 1:
        price = closes[i]
        vw = vols[max(0, i - 20):i]
        vol_avg = sum(vw) / max(1, len(vw))
        vol_ratio = vols[i] / vol_avg if vol_avg else 0
        rsi_now = rsi[i]
        rsi_delta = rsi[i] - rsi[i - 1]
        e20, e50 = ema20[i], ema50[i]
        gap1 = (e50 - e20) / e50 * 100 if e50 else 0

        cooldown_ok = (times[i] - last_signal_ts) / 3600000 >= SIGNAL_COOLDOWN_H

        cond = (
            bear4[i]
            and gap4[i] >= p["min_gap4"]
            and e20 < e50
            and gap1 >= p["min_gap1"]
            and p["rsi_min"] <= rsi_now <= p["rsi_max"]
            and vol_ratio >= p["min_vol"]
            and abs(price - e20) / e20 * 100 <= p["ema_prox"]
            and rsi_delta <= p["max_rsi_delta"]
            and price <= e50
            and cooldown_ok
        )

        if cond:
            entry = price
            sl = entry + p["sl_atr"] * atr[i]
            tp = entry - p["tp_atr"] * atr[i]
            last_signal_ts = times[i]
            outcome = None
            for j in range(i + 1, min(i + 48, len(closes))):
                if highs[j] >= sl:
                    outcome = ("SL", -p["sl_atr"])
                    break
                if lows[j] <= tp:
                    outcome = ("TP", p["tp_atr"])
                    break
            if outcome is None:
                exit_p = closes[min(i + 47, len(closes) - 1)]
                outcome = ("TIMEOUT", round((entry - exit_p) / atr[i], 2))
            trades.append({
                "symbol": symbol,
                "time": datetime.fromtimestamp(times[i] / 1000, tz=timezone.utc).isoformat()[:16],
                "entry": round(entry, 6), "rsi": round(rsi_now, 1), "vol": round(vol_ratio, 2),
                "result": outcome[0], "r": outcome[1],
            })
            i += 4
        else:
            i += 1
    return trades


BASE = {
    "min_gap4": 0.0, "min_gap1": 0.0, "rsi_min": 40.0, "rsi_max": 65.0,
    "min_vol": 0.6, "ema_prox": 1.5, "max_rsi_delta": 1.0, "sl_atr": 1.0, "tp_atr": 2.0,
}

CONFIGS = {
    "baseline (loose)": {**BASE},
    "C1 strong-trend": {**BASE, "min_gap4": 1.5, "min_gap1": 1.0},
    "C2 +volume": {**BASE, "min_gap4": 1.5, "min_gap1": 1.0, "min_vol": 1.2},
    "C3 tighter-RSI": {**BASE, "min_gap4": 2.0, "min_gap1": 1.5, "rsi_min": 48, "rsi_max": 60, "max_rsi_delta": 0.0},
    "C4 wideTP 3R": {**BASE, "min_gap4": 1.5, "min_gap1": 1.0, "min_vol": 1.0, "tp_atr": 3.0},
    "C5 strict+vol+3R": {**BASE, "min_gap4": 2.0, "min_gap1": 1.5, "rsi_min": 48, "rsi_max": 62,
                          "max_rsi_delta": 0.0, "min_vol": 1.3, "tp_atr": 3.0},
}


def run_config(name: str, p: dict, days: int):
    all_trades = []
    for coin in ALLOWLIST:
        try:
            all_trades.extend(backtest_coin(coin + "USDT", days, p))
        except Exception:
            pass
    n = len(all_trades)
    if n == 0:
        print(f"  {name:22} | no signals")
        return name, 0, 0.0, 0.0
    wins = sum(1 for t in all_trades if t["r"] > 0)
    total_r = sum(t["r"] for t in all_trades)
    wr = wins / n * 100
    avg = total_r / n
    flag = "  <-- profitable" if total_r > 0 else ""
    print(f"  {name:22} | N={n:>3} WR={wr:>5.1f}% totalR={total_r:>+7.2f} avgR={avg:>+.3f}{flag}")
    return name, n, total_r, avg


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    print(f"=== DT_SHORT param sweep — {days}d, {len(ALLOWLIST)} coins ===")
    print(f"  (R = ATR multiples; need WR > 1/(1+TP:SL) to be +EV)\n")
    results = []
    for name, p in CONFIGS.items():
        results.append(run_config(name, p, days))
    best = max(results, key=lambda x: x[2])
    print(f"\n=== BEST: {best[0]} (totalR {best[2]:+.2f}, avgR {best[3]:+.3f}, N={best[1]}) ===")
    if best[2] <= 0:
        print("  VERDICT: No config is profitable on this data. Keep DOWNTREND_SHORT=0.")
    else:
        print("  VERDICT: Profitable config found — but verify N is large enough (>30).")


if __name__ == "__main__":
    main()
