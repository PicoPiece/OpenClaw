#!/usr/bin/env python3
"""Backtest multi-timeframe explosive burst combos.

Philosophy: large TF filters noise, small TF triggers action.
Compares 5m / 15m / 1h action layers with 1h + 4h regime filters.

Usage:
    python3 backtest_mtf_burst.py [days]

Configs sweep:
  A  1H burst only (baseline)
  B  15M solo (no MTF filter — noisy)
  C  15M + 4H filter
  D  15M + 1H + 4H (current live-ish, late RSI 75)
  E  5M + 4H
  F  5M + 15M confirm + 4H
  G  5M + 15M + 1H + 4H (full MTF)
  H  G aggressive (lower thresholds, late RSI 82)
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime, timezone

FAPI = "https://fapi.binance.com"
ALLOWLIST = ["AAVE", "BNB", "BTC", "ENA", "ETH", "INJ", "LINK", "ORDI", "TRX", "XRP", "ATOM"]
OFFLIST_PUMPS = ["WLD", "DOGE", "SOL", "ALLO"]
COINS = ALLOWLIST + [c for c in OFFLIST_PUMPS if c not in ALLOWLIST]

RSI_PERIOD = 14
EMA_FAST = 20
EMA_SLOW = 50
ATR_PERIOD = 14
WARMUP = 60
FEE_R = 0.08          # ~0.08R round-trip taker drag on tight SL
VOL_CAP_PCT = 12.0
COOLDOWN_BARS = {"5m": 12, "15m": 4, "1h": 4}   # min bars between signals per coin
TIMEOUT_BARS = {"5m": 36, "15m": 12, "1h": 3}   # ~3h probe timeout
SL_MULT = 0.6
TP_MULT = 2.0


def fetch_klines(symbol: str, interval: str, limit: int, end_ms: int | None = None) -> list:
    url = f"{FAPI}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    if end_ms:
        url += f"&endTime={end_ms}"
    req = urllib.request.Request(url, headers={"User-Agent": "MTFBacktest/1.0"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def bars_per_day(interval: str) -> int:
    return {"5m": 288, "15m": 96, "1h": 24, "4h": 6}[interval]


def fetch_full(symbol: str, interval: str, days: int) -> list:
    need = days * bars_per_day(interval) + WARMUP + max(TIMEOUT_BARS.values())
    out: list = []
    end_ms = None
    while len(out) < need:
        batch = fetch_klines(symbol, interval, min(1500, need - len(out)), end_ms)
        if not batch:
            break
        out = batch + out
        end_ms = batch[0][0] - 1
        time.sleep(0.05)
    return out[-need:]


def ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi_series(closes: list[float], period: int = RSI_PERIOD) -> list[float]:
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


def atr_series(highs, lows, closes, period=ATR_PERIOD) -> list[float]:
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    out = [trs[0]] * len(closes)
    if len(trs) < period:
        return out
    a = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        a = (a * (period - 1) + trs[i]) / period
        out[i] = a
    return out


def trend_label(price, ema20, ema50) -> str:
    if price > ema20 > ema50:
        return "UPTREND"
    if price < ema20 < ema50:
        return "DOWNTREND"
    return "NEUTRAL"


def build_tf(klines: list) -> dict:
    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    vols = [float(k[5]) for k in klines]
    times = [int(k[0]) for k in klines]
    opens = [float(k[1]) for k in klines]
    return {
        "closes": closes, "highs": highs, "lows": lows, "vols": vols,
        "times": times, "opens": opens,
        "ema20": ema_series(closes, EMA_FAST),
        "ema50": ema_series(closes, EMA_SLOW),
        "rsi": rsi_series(closes, RSI_PERIOD),
        "atr": atr_series(highs, lows, closes, ATR_PERIOD),
    }


def idx_at_or_before(times: list[int], ts: int) -> int:
    idx = 0
    for j, t in enumerate(times):
        if t <= ts:
            idx = j
        else:
            break
    return idx


def burst_on_closed_bar(tf: dict, i: int, range_atr_min: float, vol_ratio_min: float) -> str | None:
    """Return LONG/SHORT if closed bar i-1 is a burst."""
    if i < 22:
        return None
    bar_i = i - 1
    atr = tf["atr"][bar_i]
    if not atr or atr <= 0:
        return None
    h, l = tf["highs"][bar_i], tf["lows"][bar_i]
    o, c = tf["opens"][bar_i], tf["closes"][bar_i]
    vol = tf["vols"][bar_i]
    avg_vol = sum(tf["vols"][bar_i - 20:bar_i]) / 20
    range_atr = (h - l) / atr
    vol_ratio = vol / avg_vol if avg_vol else 0
    if range_atr >= range_atr_min and vol_ratio >= vol_ratio_min:
        return "LONG" if c > o else "SHORT"
    return None


def simulate_trade(tf: dict, entry_i: int, direction: str, atr: float) -> dict:
    entry = tf["closes"][entry_i]
    sl_dist = SL_MULT * atr
    tp_dist = TP_MULT * atr
    if direction == "LONG":
        sl, tp = entry - sl_dist, entry + tp_dist
    else:
        sl, tp = entry + sl_dist, entry - tp_dist
    risk = sl_dist
    timeout = TIMEOUT_BARS.get("5m", 36)  # overridden by caller via tf name
    max_i = min(entry_i + timeout, len(tf["closes"]) - 1)
    for j in range(entry_i + 1, max_i + 1):
        hi, lo = tf["highs"][j], tf["lows"][j]
        if direction == "LONG":
            if lo <= sl:
                r = (sl - entry) / risk - FEE_R
                return {"result": "SL", "r": r, "bars": j - entry_i}
            if hi >= tp:
                r = (tp - entry) / risk - FEE_R
                return {"result": "TP", "r": r, "bars": j - entry_i}
        else:
            if hi >= sl:
                r = (entry - sl) / risk - FEE_R
                return {"result": "SL", "r": r, "bars": j - entry_i}
            if lo <= tp:
                r = (entry - tp) / risk - FEE_R
                return {"result": "TP", "r": r, "bars": j - entry_i}
    exit_p = tf["closes"][max_i]
    if direction == "LONG":
        r = (exit_p - entry) / risk - FEE_R
    else:
        r = (entry - exit_p) / risk - FEE_R
    return {"result": "TIMEOUT", "r": r, "bars": max_i - entry_i}


def load_symbol(symbol: str, days: int, cache: dict) -> dict | None:
    key = (symbol, days)
    if key in cache:
        return cache[key]
    try:
        k5 = fetch_full(symbol, "5m", days)
        k15 = fetch_full(symbol, "15m", days)
        k1h = fetch_full(symbol, "1h", days)
        k4h = fetch_full(symbol, "4h", days)
    except Exception:
        cache[key] = None
        return None
    if len(k1h) < WARMUP + 10 or len(k4h) < 30:
        cache[key] = None
        return None
    data = {
        "5m": build_tf(k5),
        "15m": build_tf(k15),
        "1h": build_tf(k1h),
        "4h": build_tf(k4h),
    }
    cache[key] = data
    return data


def backtest_coin(symbol: str, days: int, cfg: dict, cache: dict) -> list[dict]:
    data = load_symbol(symbol, days, cache)
    if not data:
        return []

    action_tf = cfg["action_tf"]
    act = data[action_tf]
    h1 = data["1h"]
    h4 = data["4h"]
    m15 = data["15m"]

    trades = []
    last_signal_i = -999
    cooldown = COOLDOWN_BARS[action_tf]
    trade_timeout = cfg.get("timeout_bars", TIMEOUT_BARS[action_tf])
    start = max(WARMUP, 22)

    for i in range(start, len(act["closes"]) - trade_timeout - 1):
        if i - last_signal_i < cooldown:
            continue

        burst_dir = burst_on_closed_bar(act, i, cfg["range_atr"], cfg["vol_ratio"])
        if not burst_dir:
            continue

        ts = act["times"][i]
        i1 = idx_at_or_before(h1["times"], ts)
        i4 = idx_at_or_before(h4["times"], ts)
        i15 = idx_at_or_before(m15["times"], ts)

        price = act["closes"][i]
        atr_4h = h4["atr"][i4] or h1["atr"][i1]
        atr_pct = atr_4h / price * 100 if price else 999
        if atr_pct > VOL_CAP_PCT:
            continue

        # 4h filter
        if cfg.get("filter_4h"):
            bull4 = h4["ema20"][i4] > h4["ema50"][i4]
            bear4 = h4["ema20"][i4] < h4["ema50"][i4]
            if burst_dir == "LONG" and not bull4:
                continue
            if burst_dir == "SHORT" and not bear4:
                continue

        # 1h trend filter (block opposing trend)
        if cfg.get("filter_1h_trend"):
            tr = trend_label(h1["closes"][i1], h1["ema20"][i1], h1["ema50"][i1])
            if burst_dir == "LONG" and tr == "DOWNTREND":
                continue
            if burst_dir == "SHORT" and tr == "UPTREND":
                continue

        # 15m confirmation for 5m action
        if cfg.get("confirm_15m") and action_tf == "5m":
            if i15 < 22:
                continue
            m15_dir = burst_on_closed_bar(
                m15, i15 + 1,
                cfg.get("confirm_15m_range", 0.8),
                cfg.get("confirm_15m_vol", 1.5),
            )
            if m15_dir != burst_dir:
                # softer confirm: same candle direction on last closed 15m
                bi = i15
                if bi < 1:
                    continue
                c15, o15 = m15["closes"][bi], m15["opens"][bi]
                dir15 = "LONG" if c15 > o15 else "SHORT"
                if dir15 != burst_dir:
                    continue

        # Late RSI gate (1h RSI)
        rsi_1h = h1["rsi"][i1]
        late = cfg.get("late_rsi", 999)
        if burst_dir == "LONG" and rsi_1h >= late:
            continue
        if burst_dir == "SHORT" and rsi_1h <= (100 - late):
            continue

        def _atr_at(tf: str) -> float:
            tfd = data[tf]
            idx = idx_at_or_before(tfd["times"], ts)
            v = tfd["atr"][idx]
            return v or h1["atr"][i1]

        # Split sizing: SL and TP can use different timeframe ATRs
        sl_tf = cfg.get("sl_atr_tf") or cfg.get("sizing_tf", action_tf)
        tp_tf = cfg.get("tp_atr_tf") or cfg.get("sizing_tf", action_tf)
        sl_mult = cfg.get("sl_mult", SL_MULT)
        tp_mult = cfg.get("tp_mult", TP_MULT)
        atr_sl = _atr_at(sl_tf)
        atr_tp = _atr_at(tp_tf)

        entry = act["closes"][i]
        sl_dist = sl_mult * atr_sl
        tp_dist = tp_mult * atr_tp
        if burst_dir == "LONG":
            sl, tp = entry - sl_dist, entry + tp_dist
        else:
            sl, tp = entry + sl_dist, entry - tp_dist
        risk = sl_dist
        if risk <= 0:
            continue
        max_i = min(i + trade_timeout, len(act["closes"]) - 1)
        result = None
        for j in range(i + 1, max_i + 1):
            hi, lo = act["highs"][j], act["lows"][j]
            if burst_dir == "LONG":
                if lo <= sl:
                    result = {"result": "SL", "r": (sl - entry) / risk - FEE_R, "bars": j - i}
                    break
                if hi >= tp:
                    result = {"result": "TP", "r": (tp - entry) / risk - FEE_R, "bars": j - i}
                    break
            else:
                if hi >= sl:
                    result = {"result": "SL", "r": (entry - sl) / risk - FEE_R, "bars": j - i}
                    break
                if lo <= tp:
                    result = {"result": "TP", "r": (entry - tp) / risk - FEE_R, "bars": j - i}
                    break
        else:
            exit_p = act["closes"][max_i]
            if burst_dir == "LONG":
                r = (exit_p - entry) / risk - FEE_R
            else:
                r = (entry - exit_p) / risk - FEE_R
            result = {"result": "TIMEOUT", "r": r, "bars": max_i - i}

        trades.append({
            "symbol": symbol,
            "time": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()[:16],
            "action_tf": action_tf,
            "direction": burst_dir,
            "rsi_1h": round(rsi_1h, 1),
            "atr_pct": round(atr_pct, 2),
            "result": result["result"],
            "r": round(result["r"], 3),
            "bars": result["bars"],
        })
        last_signal_i = i

    return trades


CONFIGS = {
    "A_1H_only": {
        "action_tf": "1h", "filter_4h": True, "filter_1h_trend": False,
        "range_atr": 1.5, "vol_ratio": 3.0, "late_rsi": 999, "sizing_tf": "4h",
    },
    "B_15M_solo": {
        "action_tf": "15m", "filter_4h": False, "filter_1h_trend": False,
        "range_atr": 1.2, "vol_ratio": 2.5, "late_rsi": 999, "sizing_tf": "15m",
    },
    "C_15M+4H": {
        "action_tf": "15m", "filter_4h": True, "filter_1h_trend": False,
        "range_atr": 1.2, "vol_ratio": 2.5, "late_rsi": 999, "sizing_tf": "15m",
    },
    "D_15M+1H+4H": {
        "action_tf": "15m", "filter_4h": True, "filter_1h_trend": True,
        "range_atr": 1.2, "vol_ratio": 2.5, "late_rsi": 75, "sizing_tf": "15m",
    },
    "E_5M+4H": {
        "action_tf": "5m", "filter_4h": True, "filter_1h_trend": False,
        "range_atr": 1.0, "vol_ratio": 2.0, "late_rsi": 999, "sizing_tf": "5m",
    },
    "F_5M+15M+4H": {
        "action_tf": "5m", "filter_4h": True, "filter_1h_trend": False,
        "confirm_15m": True, "confirm_15m_range": 0.8, "confirm_15m_vol": 1.5,
        "range_atr": 1.0, "vol_ratio": 2.0, "late_rsi": 999, "sizing_tf": "5m",
    },
    "G_5M+15M+1H+4H": {
        "action_tf": "5m", "filter_4h": True, "filter_1h_trend": True,
        "confirm_15m": True, "confirm_15m_range": 0.8, "confirm_15m_vol": 1.5,
        "range_atr": 1.0, "vol_ratio": 2.0, "late_rsi": 80, "sizing_tf": "5m",
    },
    "H_range_0.9": {
        "action_tf": "5m", "filter_4h": True, "filter_1h_trend": True,
        "confirm_15m": True, "confirm_15m_range": 0.7, "confirm_15m_vol": 1.3,
        "range_atr": 0.9, "vol_ratio": 1.8, "late_rsi": 82, "sizing_tf": "5m",
    },
    "I_range_0.8": {
        "action_tf": "5m", "filter_4h": True, "filter_1h_trend": True,
        "confirm_15m": True, "confirm_15m_range": 0.7, "confirm_15m_vol": 1.3,
        "range_atr": 0.8, "vol_ratio": 1.8, "late_rsi": 82, "sizing_tf": "5m",
    },
    "J_range_0.7": {
        "action_tf": "5m", "filter_4h": True, "filter_1h_trend": True,
        "confirm_15m": True, "confirm_15m_range": 0.7, "confirm_15m_vol": 1.3,
        "range_atr": 0.7, "vol_ratio": 1.8, "late_rsi": 82, "sizing_tf": "5m",
    },
    "K_range_0.7_vol1.6": {
        "action_tf": "5m", "filter_4h": True, "filter_1h_trend": True,
        "confirm_15m": True, "confirm_15m_range": 0.7, "confirm_15m_vol": 1.3,
        "range_atr": 0.7, "vol_ratio": 1.6, "late_rsi": 82, "sizing_tf": "5m",
    },
}

# Live baseline vs split SL/TP sizing (5m action, same MTF stack as K)
_BASE_5M = {
    "action_tf": "5m", "filter_4h": True, "filter_1h_trend": True,
    "confirm_15m": True, "confirm_15m_range": 0.7, "confirm_15m_vol": 1.3,
    "range_atr": 0.7, "vol_ratio": 1.6, "late_rsi": 82,
}
SIZING_CONFIGS = {
    "L_live_SL5m_TP5m": {
        **_BASE_5M, "sl_atr_tf": "5m", "tp_atr_tf": "5m", "timeout_bars": 36,
    },
    "M_SL15m_TP1h": {
        **_BASE_5M, "sl_atr_tf": "15m", "tp_atr_tf": "1h", "timeout_bars": 36,
    },
    "N_SL15m_TP15m": {
        **_BASE_5M, "sl_atr_tf": "15m", "tp_atr_tf": "15m", "timeout_bars": 36,
    },
    "O_SL5m_TP1h": {
        **_BASE_5M, "sl_atr_tf": "5m", "tp_atr_tf": "1h", "timeout_bars": 36,
    },
    "P_SL15m_TP1h_t6h": {
        **_BASE_5M, "sl_atr_tf": "15m", "tp_atr_tf": "1h", "timeout_bars": 72,
    },
    "Q_SL15m_TP1h_t8h": {
        **_BASE_5M, "sl_atr_tf": "15m", "tp_atr_tf": "1h", "timeout_bars": 96,
    },
}

# Subset for quick range-atr sweep (same MTF stack, only 5m range/vol changes)
RANGE_SWEEP = ["H_range_0.9", "I_range_0.8", "J_range_0.7", "K_range_0.7_vol1.6"]
SIZING_SWEEP = list(SIZING_CONFIGS.keys())


def run_config(name: str, cfg: dict, days: int, cache: dict) -> tuple:
    all_trades: list[dict] = []
    for coin in COINS:
        sym = coin + "USDT"
        try:
            all_trades.extend(backtest_coin(sym, days, cfg, cache))
        except Exception as exc:
            print(f"  [warn] {sym}: {exc}")
    n = len(all_trades)
    if n == 0:
        print(f"  {name:22} | no signals")
        return name, 0, 0.0, 0.0, 0.0
    wins = sum(1 for t in all_trades if t["r"] > 0)
    total_r = sum(t["r"] for t in all_trades)
    wr = wins / n * 100
    avg = total_r / n
    avg_bars = sum(t["bars"] for t in all_trades) / n
    flag = "  <-- +EV" if total_r > 0 and n >= 15 else ""
    print(f"  {name:22} | N={n:>4} WR={wr:>5.1f}% totalR={total_r:>+8.2f} "
          f"avgR={avg:>+.3f} avgBars={avg_bars:>5.1f}{flag}")
    return name, n, total_r, avg, wr


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    days = int(args[0]) if args else 45
    sweep_only = "--range-sweep" in flags
    sizing_sweep = "--sizing-sweep" in flags
    if sizing_sweep:
        configs = SIZING_CONFIGS
    elif sweep_only:
        configs = {k: CONFIGS[k] for k in RANGE_SWEEP}
    else:
        configs = CONFIGS

    title = ("MTF SL/TP Sizing Sweep" if sizing_sweep else
             "MTF Range-ATR Sweep" if sweep_only else "MTF Burst Backtest")
    print(f"=== {title} — {days}d, {len(COINS)} coins ===")
    print(f"  Coins: {', '.join(COINS)}")
    print(f"  Default mult: SL={SL_MULT}xATR TP={TP_MULT}xATR fee={FEE_R}R/trade vol_cap={VOL_CAP_PCT}%\n")

    cache: dict = {}
    results = []
    for name, cfg in configs.items():
        tf = cfg["action_tf"]
        filters = []
        if cfg.get("filter_4h"):
            filters.append("4H")
        if cfg.get("filter_1h_trend"):
            filters.append("1H-trend")
        if cfg.get("confirm_15m"):
            filters.append("15M-confirm")
        late = cfg.get("late_rsi", 999)
        sl_tf = cfg.get("sl_atr_tf") or cfg.get("sizing_tf", tf)
        tp_tf = cfg.get("tp_atr_tf") or cfg.get("sizing_tf", tf)
        to_b = cfg.get("timeout_bars", TIMEOUT_BARS.get(tf, 36))
        print(f"--- {name}: action={tf} SL={sl_tf} TP={tp_tf} timeout={to_b}bars "
              f"filters=[{','.join(filters) or 'none'}] range={cfg['range_atr']} "
              f"vol={cfg['vol_ratio']} lateRSI={late} ---")
        results.append(run_config(name, cfg, days, cache))
        print()

    viable = [r for r in results if r[1] >= 10]
    if not viable:
        print("No config with N>=10. Extend days or loosen params.")
        return
    best_ev = max(viable, key=lambda x: x[2])
    best_avg = max(viable, key=lambda x: x[3])
    print("=== SUMMARY ===")
    print(f"  Best totalR: {best_ev[0]} ({best_ev[2]:+.2f}R, N={best_ev[1]}, WR={best_ev[4]:.1f}%)")
    print(f"  Best avgR:   {best_avg[0]} ({best_avg[3]:+.3f}R/trade, N={best_avg[1]})")
    if sweep_only or sizing_sweep:
        ranked = sorted(viable, key=lambda x: x[2], reverse=True)
        label = "Sizing" if sizing_sweep else "Range"
        print(f"\n  {label} sweep ranking (totalR):")
        for r in ranked:
            cfg = configs[r[0]]
            if sizing_sweep:
                sl_tf = cfg.get("sl_atr_tf", "?")
                tp_tf = cfg.get("tp_atr_tf", "?")
                to_b = cfg.get("timeout_bars", "?")
                print(f"    {r[0]:22} SL={sl_tf} TP={tp_tf} to={to_b} "
                      f"→ {r[2]:+.1f}R avg={r[3]:+.3f} N={r[1]} WR={r[4]:.1f}%")
            else:
                print(f"    {r[0]:22} range={cfg['range_atr']} vol={cfg['vol_ratio']} "
                      f"→ {r[2]:+.1f}R avg={r[3]:+.3f} N={r[1]} WR={r[4]:.1f}%")
    if best_ev[2] <= 0:
        print("  VERDICT: No +EV combo on this window — deploy cautiously, prefer D or G.")
    else:
        rec = configs[best_ev[0]]
        print(f"  VERDICT: Deploy range_atr={rec['range_atr']} vol_ratio={rec['vol_ratio']} on live.")


if __name__ == "__main__":
    main()
