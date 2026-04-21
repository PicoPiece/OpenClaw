#!/usr/bin/env python3
"""
Backtest v3 vs v4 signal algorithms on 1 week of historical data.

v3 (old): SMA50 trend, SIDEWAYS-BULL/BEAR allowed, SL 2xATR, TP 3.0xATR (R:R 1.5)
v4 (new): EMA20/EMA50 cross, no SIDEWAYS, multi-tf 4h confirm, SL 2xATR, TP 3.5xATR (R:R 1.75)

Method:
  1. Fetch 7 days of 1h klines for top coins
  2. Walk-forward bar-by-bar: at each bar, compute indicators on past data only
  3. Apply v3 and v4 rules to detect entry signals
  4. For each signal, simulate forward bars to find TP/SL/timeout outcome
  5. Report stats: signal count, win rate, avg R, total R-multiples

Usage:
    python3 backtest_v3_v4.py
"""

import json
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys
import time

SCRIPT_DIR = Path(__file__).resolve().parent
BINANCE_KLINES_API = "https://api.binance.com/api/v3/klines"

# Backtest config
BACKTEST_DAYS = 30
COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "AAVE"]
SYMBOLS = {c: f"{c}USDT" for c in COINS}

# Need extra warmup bars for indicators (50 EMA + 14 ATR + buffer)
WARMUP_BARS = 100
LOOKAHEAD_BARS = 72  # max 72h to hit TP/SL before timeout

# Indicator params (same as live)
RSI_PERIOD = 14
EMA_FAST = 20
EMA_SLOW = 50
SMA_PERIOD = 50  # v3 used SMA
ATR_PERIOD = 14
VOL_RATIO_THRESHOLD = 1.2
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
RSI_LONG_MIN = 45
RSI_SHORT_MAX = 55
RSI_MOMENTUM_DELTA = 3
EMA_GAP_MIN_PCT = 0.1

# Risk params
ATR_SL_MULT = 2.0
V3_TP_MULT = 3.0  # R:R 1.5
V4_TP_MULT = 3.0  # R:R 1.5 (was 3.5 — tuned down per backtest)
VOL_STRONG_RATIO = 2.0

# Cooldown between signals on same coin (bars)
SIGNAL_COOLDOWN_BARS = 6  # 6 hours


# ---------------------------------------------------------------------------
# Indicator math (replicated from binance_price_alert.py)
# ---------------------------------------------------------------------------

def calc_rsi(closes, period=RSI_PERIOD):
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = deltas[-period:]
    gains = [d for d in recent if d > 0]
    losses = [-d for d in recent if d < 0]
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_ema(closes, period):
    if len(closes) < period:
        return None
    mult = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]:
        ema = (p - ema) * mult + ema
    return ema


def calc_sma(closes, period):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def calc_atr(highs, lows, closes, period=ATR_PERIOD):
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    return sum(trs[-period:]) / period


def calc_vol_ratio(volumes):
    """Latest volume vs avg of preceding bars."""
    if len(volumes) < 11:
        return None
    avg = sum(volumes[-11:-1]) / 10
    return volumes[-1] / avg if avg > 0 else None


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------

def fetch_klines(symbol, interval, limit, end_time_ms=None):
    """Fetch klines. Each kline = [openTime, open, high, low, close, volume, closeTime, ...]"""
    url = f"{BINANCE_KLINES_API}?symbol={symbol}&interval={interval}&limit={limit}"
    if end_time_ms:
        url += f"&endTime={end_time_ms}"
    req = urllib.request.Request(url, headers={"User-Agent": "PicoBacktest/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def fetch_full_history(symbol, interval, days):
    """Fetch enough klines to cover days + warmup."""
    bars_needed = days * (24 if interval == "1h" else 6) + WARMUP_BARS + LOOKAHEAD_BARS
    # Binance max 1000 per call
    all_klines = []
    end_ms = None
    while len(all_klines) < bars_needed:
        batch_size = min(1000, bars_needed - len(all_klines))
        batch = fetch_klines(symbol, interval, batch_size, end_time_ms=end_ms)
        if not batch:
            break
        all_klines = batch + all_klines
        end_ms = batch[0][0] - 1  # next batch ends just before this batch's first bar
        time.sleep(0.1)
    return all_klines[-bars_needed:]


# ---------------------------------------------------------------------------
# Signal detection
# ---------------------------------------------------------------------------

def detect_v3_signal(closes_1h, highs_1h, lows_1h, vols_1h):
    """v3: SMA50 trend (allow SIDEWAYS), RSI momentum, volume confirm."""
    if len(closes_1h) < SMA_PERIOD + RSI_PERIOD + 2:
        return None
    price = closes_1h[-1]
    rsi = calc_rsi(closes_1h)
    rsi_prev = calc_rsi(closes_1h[:-1])
    sma = calc_sma(closes_1h, SMA_PERIOD)
    atr = calc_atr(highs_1h, lows_1h, closes_1h)
    vol_ratio = calc_vol_ratio(vols_1h)

    if None in (rsi, rsi_prev, sma, atr, vol_ratio):
        return None

    # v3 trend: UPTREND if price > SMA*1.005, DOWNTREND if price < SMA*0.995
    sma_diff_pct = (price - sma) / sma * 100
    if sma_diff_pct > 0.5:
        trend = "UPTREND"
    elif sma_diff_pct < -0.5:
        trend = "DOWNTREND"
    elif sma_diff_pct > 0:
        trend = "SIDEWAYS-BULL"
    else:
        trend = "SIDEWAYS-BEAR"

    rsi_delta = rsi - rsi_prev
    vol_ok = vol_ratio >= VOL_RATIO_THRESHOLD

    # LONG: trend bullish-ish, RSI cross up or strong delta, price > SMA, vol confirm
    rsi_cross_up = rsi_prev < RSI_OVERSOLD and rsi >= RSI_OVERSOLD
    rsi_strong_bull = rsi > RSI_LONG_MIN and rsi_delta >= RSI_MOMENTUM_DELTA
    rsi_long_ok = rsi_cross_up or rsi_strong_bull
    trend_bull_v3 = trend in ("UPTREND", "SIDEWAYS-BULL")

    if price > sma and rsi_long_ok and vol_ok and trend_bull_v3:
        sl = price - ATR_SL_MULT * atr
        tp = price + V3_TP_MULT * atr
        return {"direction": "LONG", "entry": price, "sl": sl, "tp": tp, "rsi": rsi, "trend": trend, "vol": vol_ratio}

    rsi_cross_down = rsi_prev > RSI_OVERBOUGHT and rsi <= RSI_OVERBOUGHT
    rsi_strong_bear = rsi < RSI_SHORT_MAX and rsi_delta <= -RSI_MOMENTUM_DELTA
    rsi_short_ok = rsi_cross_down or rsi_strong_bear
    trend_bear_v3 = trend in ("DOWNTREND", "SIDEWAYS-BEAR")

    if price < sma and rsi_short_ok and vol_ok and trend_bear_v3:
        sl = price + ATR_SL_MULT * atr
        tp = price - V3_TP_MULT * atr
        return {"direction": "SHORT", "entry": price, "sl": sl, "tp": tp, "rsi": rsi, "trend": trend, "vol": vol_ratio}

    return None


def detect_v4_signal(closes_1h, highs_1h, lows_1h, vols_1h, closes_4h):
    """v4: EMA20/EMA50 cross 1h+4h aligned, RSI, volume, no SIDEWAYS."""
    if len(closes_1h) < EMA_SLOW + RSI_PERIOD + 2:
        return None
    price = closes_1h[-1]
    rsi = calc_rsi(closes_1h)
    rsi_prev = calc_rsi(closes_1h[:-1])
    ema_fast = calc_ema(closes_1h, EMA_FAST)
    ema_slow = calc_ema(closes_1h, EMA_SLOW)
    atr = calc_atr(highs_1h, lows_1h, closes_1h)
    vol_ratio = calc_vol_ratio(vols_1h)

    if None in (rsi, rsi_prev, ema_fast, ema_slow, atr, vol_ratio):
        return None

    # 4h EMA for multi-tf
    ema_fast_4h = calc_ema(closes_4h, EMA_FAST) if len(closes_4h) >= EMA_FAST else None
    ema_slow_4h = calc_ema(closes_4h, EMA_SLOW) if len(closes_4h) >= EMA_SLOW else None

    # v4 trend: strict UPTREND if price > ema_fast > ema_slow
    if ema_fast > ema_slow and price > ema_fast:
        trend = "UPTREND"
    elif ema_fast < ema_slow and price < ema_fast:
        trend = "DOWNTREND"
    else:
        trend = "NEUTRAL"

    rsi_delta = rsi - rsi_prev
    vol_ok = vol_ratio >= VOL_RATIO_THRESHOLD
    ema_gap_pct = abs(ema_fast - ema_slow) / ema_slow * 100

    rsi_cross_up = rsi_prev < RSI_OVERSOLD and rsi >= RSI_OVERSOLD
    rsi_strong_bull = rsi > RSI_LONG_MIN and rsi_delta >= RSI_MOMENTUM_DELTA
    rsi_long_ok = rsi_cross_up or rsi_strong_bull

    rsi_cross_down = rsi_prev > RSI_OVERBOUGHT and rsi <= RSI_OVERBOUGHT
    rsi_strong_bear = rsi < RSI_SHORT_MAX and rsi_delta <= -RSI_MOMENTUM_DELTA
    rsi_short_ok = rsi_cross_down or rsi_strong_bear

    # Multi-tf 4h alignment (LONG only — SHORT does not need 4h confirm per plan B)
    mtf_bull = (ema_fast_4h is None) or (ema_fast_4h > ema_slow_4h)

    # Updated trend bear: allow NEUTRAL-BEAR with RSI<35 or strong vol
    rsi_low = rsi < 35
    vol_strong = vol_ratio >= VOL_STRONG_RATIO
    trend_short_ok = trend == "DOWNTREND" or (
        ema_fast < ema_slow and price < ema_fast and (rsi_low or vol_strong)
    )

    ema_bull = ema_fast > ema_slow
    ema_bear = ema_fast < ema_slow

    if (ema_bull and price > ema_fast and rsi_long_ok and vol_ok
            and trend == "UPTREND" and ema_gap_pct >= EMA_GAP_MIN_PCT and mtf_bull):
        sl = price - ATR_SL_MULT * atr
        tp = price + V4_TP_MULT * atr
        return {"direction": "LONG", "entry": price, "sl": sl, "tp": tp, "rsi": rsi, "trend": trend, "vol": vol_ratio, "ema_gap": ema_gap_pct}

    if (ema_bear and price < ema_fast and rsi_short_ok and vol_ok
            and trend_short_ok and ema_gap_pct >= EMA_GAP_MIN_PCT):
        sl = price + ATR_SL_MULT * atr
        tp = price - V4_TP_MULT * atr
        return {"direction": "SHORT", "entry": price, "sl": sl, "tp": tp, "rsi": rsi, "trend": trend, "vol": vol_ratio, "ema_gap": ema_gap_pct}

    return None


# ---------------------------------------------------------------------------
# Trade simulation
# ---------------------------------------------------------------------------

def simulate_trade(signal, future_bars):
    """Walk forward bars, return outcome.

    Returns: (result, exit_price, bars_held, r_multiple)
    result in {"TP", "SL", "TIMEOUT"}
    r_multiple = (P&L) / (initial risk)
    """
    entry = signal["entry"]
    sl = signal["sl"]
    tp = signal["tp"]
    direction = signal["direction"]
    initial_risk = abs(entry - sl)

    for i, bar in enumerate(future_bars):
        high = float(bar[2])
        low = float(bar[3])

        if direction == "LONG":
            # Pessimistic: assume SL hit first if both TP and SL within bar range
            if low <= sl:
                return ("SL", sl, i + 1, -1.0)
            if high >= tp:
                r = (tp - entry) / initial_risk
                return ("TP", tp, i + 1, r)
        else:  # SHORT
            if high >= sl:
                return ("SL", sl, i + 1, -1.0)
            if low <= tp:
                r = (entry - tp) / initial_risk
                return ("TP", tp, i + 1, r)

    # Timeout: close at last bar's close
    last_close = float(future_bars[-1][4]) if future_bars else entry
    if direction == "LONG":
        r = (last_close - entry) / initial_risk
    else:
        r = (entry - last_close) / initial_risk
    return ("TIMEOUT", last_close, len(future_bars), r)


# ---------------------------------------------------------------------------
# Backtest runner
# ---------------------------------------------------------------------------

def backtest_coin(coin, klines_1h, klines_4h):
    """Walk-forward over 1h klines, detect signals, simulate outcomes."""
    closes_1h_full = [float(k[4]) for k in klines_1h]
    highs_1h_full = [float(k[2]) for k in klines_1h]
    lows_1h_full = [float(k[3]) for k in klines_1h]
    vols_1h_full = [float(k[5]) for k in klines_1h]
    times_1h = [int(k[0]) for k in klines_1h]

    closes_4h_full = [float(k[4]) for k in klines_4h]
    times_4h = [int(k[0]) for k in klines_4h]

    v3_trades = []
    v4_trades = []
    v3_last_signal_bar = -100
    v4_last_signal_bar = -100

    # Walk from WARMUP_BARS to len-LOOKAHEAD
    for i in range(WARMUP_BARS, len(klines_1h) - LOOKAHEAD_BARS):
        bar_time = times_1h[i]

        # Slice 1h history up to and including bar i
        c1 = closes_1h_full[:i + 1]
        h1 = highs_1h_full[:i + 1]
        l1 = lows_1h_full[:i + 1]
        v1 = vols_1h_full[:i + 1]

        # Find 4h closes up to this 1h time
        c4 = [c for j, c in enumerate(closes_4h_full) if times_4h[j] <= bar_time]

        future_bars = klines_1h[i + 1: i + 1 + LOOKAHEAD_BARS]

        # v3
        if i - v3_last_signal_bar >= SIGNAL_COOLDOWN_BARS:
            sig = detect_v3_signal(c1, h1, l1, v1)
            if sig:
                outcome = simulate_trade(sig, future_bars)
                v3_trades.append({
                    "coin": coin, "time": bar_time, **sig,
                    "result": outcome[0], "exit": outcome[1],
                    "bars": outcome[2], "r": outcome[3],
                })
                v3_last_signal_bar = i

        # v4
        if i - v4_last_signal_bar >= SIGNAL_COOLDOWN_BARS:
            sig = detect_v4_signal(c1, h1, l1, v1, c4)
            if sig:
                outcome = simulate_trade(sig, future_bars)
                v4_trades.append({
                    "coin": coin, "time": bar_time, **sig,
                    "result": outcome[0], "exit": outcome[1],
                    "bars": outcome[2], "r": outcome[3],
                })
                v4_last_signal_bar = i

    return v3_trades, v4_trades


def report(name, trades):
    if not trades:
        print(f"\n=== {name} ===")
        print(f"  No signals.")
        return

    n = len(trades)
    tp = sum(1 for t in trades if t["result"] == "TP")
    sl = sum(1 for t in trades if t["result"] == "SL")
    to = sum(1 for t in trades if t["result"] == "TIMEOUT")
    wins = sum(1 for t in trades if t["r"] > 0)
    losses = sum(1 for t in trades if t["r"] <= 0)
    win_rate = wins / n * 100
    total_r = sum(t["r"] for t in trades)
    avg_r = total_r / n
    avg_win = sum(t["r"] for t in trades if t["r"] > 0) / wins if wins else 0
    avg_loss = sum(t["r"] for t in trades if t["r"] <= 0) / losses if losses else 0
    avg_hold = sum(t["bars"] for t in trades) / n

    longs = [t for t in trades if t["direction"] == "LONG"]
    shorts = [t for t in trades if t["direction"] == "SHORT"]

    # Equity curve / max drawdown (in R)
    equity = 0
    peak = 0
    max_dd = 0
    for t in trades:
        equity += t["r"]
        peak = max(peak, equity)
        dd = peak - equity
        max_dd = max(max_dd, dd)

    print(f"\n=== {name} ===")
    print(f"  Total signals: {n} (LONG: {len(longs)}, SHORT: {len(shorts)})")
    print(f"  Outcomes: TP={tp} | SL={sl} | TIMEOUT={to}")
    print(f"  Win rate: {win_rate:.1f}% ({wins}/{n})")
    print(f"  Total R: {total_r:+.2f} (avg {avg_r:+.2f} per trade)")
    print(f"  Avg win: +{avg_win:.2f}R | Avg loss: {avg_loss:.2f}R")
    print(f"  Profit factor: {abs(sum(t['r'] for t in trades if t['r']>0) / sum(t['r'] for t in trades if t['r']<0)):.2f}" if losses and sum(t['r'] for t in trades if t['r']<0)!=0 else "  Profit factor: ∞")
    print(f"  Avg hold: {avg_hold:.1f} bars (~{avg_hold:.0f}h)")
    print(f"  Max drawdown: {max_dd:.2f}R")

    print(f"\n  Per-coin breakdown:")
    by_coin = {}
    for t in trades:
        by_coin.setdefault(t["coin"], []).append(t)
    for coin in sorted(by_coin):
        ts = by_coin[coin]
        n_c = len(ts)
        w_c = sum(1 for t in ts if t["r"] > 0)
        r_c = sum(t["r"] for t in ts)
        wr = w_c / n_c * 100
        print(f"    {coin:<6} {n_c:>3} signals | win {w_c:>2}/{n_c:<2} ({wr:>4.0f}%) | total {r_c:+.2f}R")


def main():
    print("=" * 70)
    print(f"  BACKTEST v3 vs v4 — {BACKTEST_DAYS} days, {len(COINS)} coins")
    print(f"  v3: SMA50 + SIDEWAYS allowed, R:R 1.5")
    print(f"  v4-tuned: EMA20/EMA50 cross + 4h MTF (LONG only) + NEUTRAL-BEAR allowed, R:R 1.5")
    print("=" * 70)

    all_v3 = []
    all_v4 = []

    for coin in COINS:
        symbol = SYMBOLS[coin]
        print(f"\n  [{coin}] Fetching klines...")
        try:
            k1h = fetch_full_history(symbol, "1h", BACKTEST_DAYS)
            k4h = fetch_full_history(symbol, "4h", BACKTEST_DAYS)
            print(f"    Loaded {len(k1h)} × 1h bars + {len(k4h)} × 4h bars")
            v3_t, v4_t = backtest_coin(coin, k1h, k4h)
            print(f"    v3: {len(v3_t)} signals | v4: {len(v4_t)} signals")
            all_v3.extend(v3_t)
            all_v4.extend(v4_t)
        except Exception as e:
            print(f"    ERROR: {e}")

    report("v3 (SMA50 + SIDEWAYS, R:R 1.5)", all_v3)
    report("v4-tuned (EMA20/50 + 4h LONG only + NEUTRAL-BEAR, R:R 1.5)", all_v4)

    print("\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    if all_v3 and all_v4:
        v3_total_r = sum(t["r"] for t in all_v3)
        v4_total_r = sum(t["r"] for t in all_v4)
        v3_wr = sum(1 for t in all_v3 if t["r"] > 0) / len(all_v3) * 100
        v4_wr = sum(1 for t in all_v4 if t["r"] > 0) / len(all_v4) * 100
        print(f"  v3: {len(all_v3):>3} signals | win rate {v3_wr:.1f}% | total {v3_total_r:+.2f}R")
        print(f"  v4: {len(all_v4):>3} signals | win rate {v4_wr:.1f}% | total {v4_total_r:+.2f}R")
        print()
        print(f"  Δ Signals: {len(all_v4) - len(all_v3):+d} ({(len(all_v4)/max(len(all_v3),1) - 1) * 100:+.0f}%)")
        print(f"  Δ Win rate: {v4_wr - v3_wr:+.1f}pp")
        print(f"  Δ Total R: {v4_total_r - v3_total_r:+.2f}R")

    # Save raw data
    out = SCRIPT_DIR / "data" / "backtest_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "backtest_at": datetime.now(timezone.utc).isoformat(),
        "days": BACKTEST_DAYS,
        "coins": COINS,
        "v3_trades": all_v3,
        "v4_trades": all_v4,
    }, indent=2, default=str))
    print(f"\n  Raw results saved to {out}")


if __name__ == "__main__":
    main()
