#!/usr/bin/env python3
"""Backtest a quantified "Sneaky Pivot" / Level Reversion strategy.

Idea (from a price-action discretionary strategy, here made systematic):
  - Mark prior-day Range High / Range Low (yesterday's daily candle H/L).
  - On the 15m chart, when price TOUCHES a level, look for a REJECTION candle
    (long wick into the level = liquidity sweep), then a CONFIRMATION candle
    (closes back through the rejection candle's extreme) = entry.
  - SL behind the rejection wick; TP at fixed R:R.
  - "Location > signal": only trade AT the prior-day levels.

This is MEAN-REVERSION, so it should work best in SIDEWAYS regime and lose in
trends. We therefore gate on BTC 7d regime and compare sideway-only vs all.

Self-contained. Usage: python3 backtest_level_reversion.py [days]
"""
from __future__ import annotations
import sys
import json
import time
import urllib.request
from datetime import datetime, timezone

FAPI = "https://fapi.binance.com"
ALLOWLIST = ["AAVE", "ETH", "LINK", "BNB", "XRP", "BTC", "TRX", "INJ", "ORDI", "ATOM", "ENA"]

MS_15M = 15 * 60 * 1000
MS_DAY = 24 * 60 * 60 * 1000


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "curl"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def fetch_klines_paged(symbol: str, interval: str, total: int) -> list:
    """Fetch up to `total` klines, paginating backwards in 1000-candle pages."""
    out = []
    end = None
    while len(out) < total:
        url = f"{FAPI}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit=1000"
        if end is not None:
            url += f"&endTime={end}"
        batch = _get(url)
        if not batch:
            break
        out = batch + out
        end = batch[0][0] - 1
        if len(batch) < 1000:
            break
        time.sleep(0.15)
    return out[-total:] if len(out) > total else out


def btc_regime_by_day(days: int) -> dict:
    """Map each UTC day -> BTC regime (rolling 7d % change)."""
    daily = fetch_klines_paged("BTCUSDT", "1d", days + 10)
    closes = [float(k[4]) for k in daily]
    day_ts = [int(k[0]) for k in daily]
    regime = {}
    for i in range(len(closes)):
        if i < 7:
            regime[day_ts[i] // MS_DAY] = "UNKNOWN"
            continue
        pct = (closes[i] - closes[i - 7]) / closes[i - 7] * 100
        if pct > 5:
            r = "UPTREND"
        elif pct < -5:
            r = "DOWNTREND"
        else:
            r = "SIDEWAYS"
        regime[day_ts[i] // MS_DAY] = r
    return regime


def prior_day_levels(symbol: str, days: int) -> dict:
    """Map each UTC day -> {high, low} of the PREVIOUS day."""
    daily = fetch_klines_paged(symbol, "1d", days + 5)
    levels = {}
    for i in range(1, len(daily)):
        day_key = int(daily[i][0]) // MS_DAY
        levels[day_key] = {"high": float(daily[i - 1][2]), "low": float(daily[i - 1][3])}
    return levels


def backtest_coin(symbol: str, days: int, p: dict, btc_regime: dict, _cache={}) -> list[dict]:
    ck = (symbol, days)
    if ck not in _cache:
        n15 = min(days * 96, 6000)  # 96 x 15m per day
        _cache[ck] = {
            "k15": fetch_klines_paged(symbol, "15m", n15),
            "levels": prior_day_levels(symbol, days),
        }
    k15 = _cache[ck]["k15"]
    levels = _cache[ck]["levels"]
    if len(k15) < 100:
        return []

    trades = []
    last_entry_ts = 0
    i = 2
    while i < len(k15) - 1:
        ts = int(k15[i][0])
        day_key = ts // MS_DAY
        lv = levels.get(day_key)
        if not lv:
            i += 1
            continue
        # regime gate
        reg = btc_regime.get(day_key, "UNKNOWN")
        if p["sideway_only"] and reg != "SIDEWAYS":
            i += 1
            continue
        # cooldown (4h = 16 x 15m)
        if (ts - last_entry_ts) < 4 * 60 * 60 * 1000:
            i += 1
            continue

        o = float(k15[i][1]); h = float(k15[i][2]); lo = float(k15[i][3]); c = float(k15[i][4])
        rng = h - lo
        if rng <= 0:
            i += 1
            continue

        buf_lo = lv["low"] * (1 + p["touch_buf"] / 100)
        buf_hi = lv["high"] * (1 - p["touch_buf"] / 100)

        signal = None
        # LONG rejection at prior-day LOW: candle dips to/below level, long lower wick
        if lo <= buf_lo:
            lower_wick = min(o, c) - lo
            if lower_wick / rng >= p["wick_ratio"] and c > o:  # hammer-ish, closes up
                signal = ("LONG", h, lo)
        # SHORT rejection at prior-day HIGH
        elif h >= buf_hi:
            upper_wick = h - max(o, c)
            if upper_wick / rng >= p["wick_ratio"] and c < o:
                signal = ("SHORT", lo, h)

        if signal:
            direction, rej_high, rej_low = signal
            # confirmation candle = next candle breaks rejection extreme
            nxt = k15[i + 1]
            n_h = float(nxt[2]); n_l = float(nxt[3]); n_c = float(nxt[4])
            confirmed = False
            entry = sl = tp = None
            if direction == "LONG" and n_c > rej_high:
                entry = n_c
                sl = rej_low * (1 - p["sl_buf"] / 100)
                risk = entry - sl
                tp = entry + p["rr"] * risk
                confirmed = risk > 0
            elif direction == "SHORT" and n_c < rej_low:
                entry = n_c
                sl = rej_high * (1 + p["sl_buf"] / 100)
                risk = sl - entry
                tp = entry - p["rr"] * risk
                confirmed = risk > 0

            if confirmed:
                last_entry_ts = ts
                outcome = None
                # walk forward up to 48 x 15m = 12h
                for j in range(i + 2, min(i + 50, len(k15))):
                    jh = float(k15[j][2]); jl = float(k15[j][3])
                    if direction == "LONG":
                        if jl <= sl:
                            outcome = ("SL", -1.0); break
                        if jh >= tp:
                            outcome = ("TP", p["rr"]); break
                    else:
                        if jh >= sl:
                            outcome = ("SL", -1.0); break
                        if jl <= tp:
                            outcome = ("TP", p["rr"]); break
                if outcome is None:
                    outcome = ("TIMEOUT", 0.0)
                trades.append({
                    "symbol": symbol, "dir": direction,
                    "time": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()[:16],
                    "regime": reg, "result": outcome[0], "r": outcome[1],
                })
                i += 2
                continue
        i += 1
    return trades


BASE = {"touch_buf": 0.15, "wick_ratio": 0.5, "sl_buf": 0.1, "rr": 2.0, "sideway_only": True}

CONFIGS = {
    "sideway RR2 wick0.5": {**BASE},
    "sideway RR2 wick0.6": {**BASE, "wick_ratio": 0.6},
    "sideway RR3 wick0.6": {**BASE, "wick_ratio": 0.6, "rr": 3.0},
    "sideway RR1.5 wick0.4": {**BASE, "wick_ratio": 0.4, "rr": 1.5},
    "ALL-regimes RR2 (control)": {**BASE, "sideway_only": False},
}


def run_config(name, p, days, btc_regime):
    trades = []
    for coin in ALLOWLIST:
        try:
            trades.extend(backtest_coin(coin + "USDT", days, p, btc_regime))
        except Exception as e:
            print(f"    {coin} err: {e}")
    n = len(trades)
    if n == 0:
        print(f"  {name:28} | no signals")
        return name, 0, 0.0
    wins = sum(1 for t in trades if t["r"] > 0)
    sl = sum(1 for t in trades if t["result"] == "SL")
    to = sum(1 for t in trades if t["result"] == "TIMEOUT")
    total_r = sum(t["r"] for t in trades)
    wr = wins / n * 100
    flag = "  <-- +EV" if total_r > 0 else ""
    print(f"  {name:28} | N={n:>3} WR={wr:>5.1f}% SL={sl:>3} TO={to:>2} totalR={total_r:>+7.2f} avgR={total_r/n:>+.3f}{flag}")
    return name, n, total_r


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 45
    print(f"=== LEVEL REVERSION (Sneaky Pivot) backtest — {days}d, {len(ALLOWLIST)} coins, 15m ===")
    print(f"  Fetching BTC regime map...")
    btc_regime = btc_regime_by_day(days)
    sdays = sum(1 for v in btc_regime.values() if v == "SIDEWAYS")
    ddays = sum(1 for v in btc_regime.values() if v == "DOWNTREND")
    udays = sum(1 for v in btc_regime.values() if v == "UPTREND")
    print(f"  Regime days in window: SIDEWAYS={sdays} DOWNTREND={ddays} UPTREND={udays}")
    print(f"  (need WR > 1/(1+RR) to be +EV: RR2->33%, RR3->25%, RR1.5->40%)\n")
    results = []
    for name, p in CONFIGS.items():
        results.append(run_config(name, p, days, btc_regime))
    best = max(results, key=lambda x: x[2])
    print(f"\n=== BEST: {best[0]} (totalR {best[2]:+.2f}, N={best[1]}) ===")
    if best[2] <= 0 or best[1] < 15:
        print("  VERDICT: No robust edge (negative or too few trades). Do NOT implement yet.")
    else:
        print("  VERDICT: Positive edge found — worth implementing as sideway-gated path.")


if __name__ == "__main__":
    main()
