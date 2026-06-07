#!/usr/bin/env python3
"""Backtest 2-class trade router: SCALP vs TREND_BREAKOUT.

Rule-based classification only (no LLM) — same philosophy as LLM_GATE_ENABLED=0.

Compares 60d:
  A  LIVE_ALL_SCALP   — current: every burst → 5m SL/TP, 3h timeout
  B  ROUTER_2CLASS    — rules route → SCALP or TREND sizing
  C  ALL_TREND        — force TREND sizing on every signal (upper bound)
  D  ROUTER_NO_SCALP  — router but skip marginal signals (trend-only + strong scalp block)

Usage:
    python3 backtest_trade_class.py [days]
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime, timezone

from backtest_mtf_burst import (
    ALLOWLIST,
    COINS,
    FEE_R,
    SL_MULT,
    TP_MULT,
    VOL_CAP_PCT,
    burst_on_closed_bar,
    idx_at_or_before,
    load_symbol,
    trend_label,
)

# Shared 5m MTF entry stack (live K config)
ENTRY = {
    "action_tf": "5m",
    "filter_4h": True,
    "filter_1h_trend": True,
    "confirm_15m": True,
    "confirm_15m_range": 0.7,
    "confirm_15m_vol": 1.3,
    "range_atr": 0.7,
    "vol_ratio": 1.6,
    "late_rsi": 82,
}

SCALP_SIZING = {"sl_atr_tf": "5m", "tp_atr_tf": "5m", "timeout_bars": 36}
TREND_SIZING = {"sl_atr_tf": "5m", "tp_atr_tf": "1h", "timeout_bars": 72}

COOLDOWN_AFTER_SL_BARS = 48      # 4h on 5m
SCALP_MAX_PER_DAY = 2
TREND_MAX_PER_DAY = 1
TREND_VOL_MIN = 2.5
BOUNCE_PCT = 2.0                 # block SHORT if bounced this much off 1h low

# USD simulation (mirrors live .env risk tiers)
PORTFOLIO_USD = 300.0            # ~futures wallet; effective sizing base
RISK_PCT_ALLOWLIST = 3.0         # RISK_PER_TRADE_PCT
RISK_PCT_OFFLIST_TREND = 1.5       # BREAKOUT_RISK_PCT
RISK_PCT_OFFLIST_SCALP = 1.0     # PROBE_RISK_PCT
FEE_BPS = 4.0                    # ~0.04% taker each side on notional (simplified)


def _burst_vol_ratio(act: dict, i: int) -> float:
    if i < 22:
        return 0.0
    bi = i - 1
    atr = act["atr"][bi]
    if not atr:
        return 0.0
    vol = act["vols"][bi]
    avg = sum(act["vols"][bi - 20:bi]) / 20
    return vol / avg if avg else 0.0


def _day_key(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def classify_trade(
    *,
    burst_dir: str,
    coin: str,
    price: float,
    burst_vol: float,
    h1: dict,
    h4: dict,
    i1: int,
    i4: int,
    mode: str,
) -> str | None:
    """Return SCALP | TREND | None (skip). Rules only."""
    rsi_1h = h1["rsi"][i1]
    tr = trend_label(h1["closes"][i1], h1["ema20"][i1], h1["ema50"][i1])
    bull4 = h4["ema20"][i4] > h4["ema50"][i4]
    bear4 = h4["ema20"][i4] < h4["ema50"][i4]
    in_allow = coin.upper() in ALLOWLIST

    # Bounce trap: SHORT into recovery off 1h low
    if burst_dir == "SHORT":
        lo_win = h1["lows"][max(0, i1 - 12): i1 + 1]
        if lo_win and price > min(lo_win) * (1 + BOUNCE_PCT / 100):
            if burst_vol < TREND_VOL_MIN:
                return None

    if burst_dir == "LONG":
        lo_win = h1["highs"][max(0, i1 - 12): i1 + 1]
        if lo_win and price < max(lo_win) * (1 - BOUNCE_PCT / 100):
            if burst_vol < TREND_VOL_MIN:
                return None

    if mode == "all_scalp":
        return "SCALP"
    if mode == "all_trend":
        return "TREND"
    if mode == "trend_only":
        # Strict: only take TREND-class; skip the rest
        pass
    else:
        mode = "router"

    # --- TREND_BREAKOUT criteria (all required) ---
    trend_aligned = (
        (burst_dir == "LONG" and tr == "UPTREND" and bull4) or
        (burst_dir == "SHORT" and tr == "DOWNTREND" and bear4)
    )
    rsi_ok = (
        (burst_dir == "LONG" and rsi_1h < 75) or
        (burst_dir == "SHORT" and rsi_1h > 25)
    )
    strong_vol = burst_vol >= TREND_VOL_MIN
    quality = in_allow or burst_vol >= 3.0

    if trend_aligned and rsi_ok and strong_vol and quality:
        return "TREND"

    if mode == "trend_only":
        return None

    # --- SCALP: marginal burst, weaker confirm ---
    if burst_vol >= ENTRY["vol_ratio"]:
        return "SCALP"
    return None


def _sizing_for_class(trade_class: str) -> dict:
    return TREND_SIZING if trade_class == "TREND" else SCALP_SIZING


def _risk_pct(trade_class: str, in_allowlist: bool) -> float:
    if in_allowlist:
        return RISK_PCT_ALLOWLIST
    if trade_class == "TREND":
        return RISK_PCT_OFFLIST_TREND
    return RISK_PCT_OFFLIST_SCALP


def _risk_usd(trade_class: str, in_allowlist: bool, portfolio: float) -> float:
    return portfolio * _risk_pct(trade_class, in_allowlist) / 100.0


def _fee_usd(risk_usd: float, r_net: float) -> float:
    """Rough fee drag: scale with risk notional (~position ~ risk/sl_pct)."""
    notional_approx = risk_usd * 15
    return notional_approx * (FEE_BPS / 10000) * 2


def pnl_stats(trades: list[dict], portfolio: float = PORTFOLIO_USD) -> dict:
    """Convert R-multiples to USD PnL using live risk tiers."""
    enriched = []
    for t in trades:
        risk_usd = _risk_usd(t["class"], t["in_allowlist"], portfolio)
        gross_pnl = t["r"] * risk_usd
        fee = _fee_usd(risk_usd, t["r"])
        net_pnl = gross_pnl - fee
        enriched.append({**t, "risk_usd": round(risk_usd, 2), "pnl_usd": round(net_pnl, 2)})

    n = len(enriched)
    if n == 0:
        return {"trades": [], "n": 0}

    pnls = [x["pnl_usd"] for x in enriched]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    cum, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    by_class = defaultdict(list)
    for x in enriched:
        by_class[x["class"]].append(x)

    class_stats = {}
    for cls, ct in by_class.items():
        cp = [c["pnl_usd"] for c in ct]
        cw = [p for p in cp if p > 0]
        cl = [p for p in cp if p <= 0]
        class_stats[cls] = {
            "n": len(ct),
            "total_pnl": sum(cp),
            "wr": len(cw) / len(ct) * 100 if ct else 0,
            "avg_win": sum(cw) / len(cw) if cw else 0,
            "avg_loss": sum(cl) / len(cl) if cl else 0,
            "profit_factor": (sum(cw) / abs(sum(cl))) if cl and sum(cl) != 0 else float("inf"),
        }

    return {
        "trades": enriched,
        "n": n,
        "total_pnl_usd": total,
        "avg_pnl_usd": total / n,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": pf,
        "avg_win_usd": sum(wins) / len(wins) if wins else 0,
        "avg_loss_usd": sum(losses) / len(losses) if losses else 0,
        "win_count": len(wins),
        "loss_count": len(losses),
        "max_drawdown_usd": max_dd,
        "expectancy_usd": total / n,
        "wr": len(wins) / n * 100,
        "by_class": class_stats,
    }


def backtest_coin(symbol: str, days: int, mode: str, cache: dict) -> list[dict]:
    data = load_symbol(symbol, days, cache)
    if not data:
        return []

    coin = symbol.replace("USDT", "")
    act = data["5m"]
    h1, h4, m15 = data["1h"], data["4h"], data["15m"]
    trade_timeout_max = max(SCALP_SIZING["timeout_bars"], TREND_SIZING["timeout_bars"])
    cooldown_entry = 12
    start = 60

    trades = []
    last_entry_i = -999
    last_sl_i: dict[str, int] = {}
    day_counts: dict[str, dict[str, int]] = defaultdict(lambda: {"SCALP": 0, "TREND": 0})

    for i in range(start, len(act["closes"]) - trade_timeout_max - 1):
        if i - last_entry_i < cooldown_entry:
            continue

        if coin in last_sl_i and i - last_sl_i[coin] < COOLDOWN_AFTER_SL_BARS:
            continue

        burst_dir = burst_on_closed_bar(act, i, ENTRY["range_atr"], ENTRY["vol_ratio"])
        if not burst_dir:
            continue

        ts = act["times"][i]
        i1 = idx_at_or_before(h1["times"], ts)
        i4 = idx_at_or_before(h4["times"], ts)
        i15 = idx_at_or_before(m15["times"], ts)
        price = act["closes"][i]
        burst_vol = _burst_vol_ratio(act, i)

        atr_4h = h4["atr"][i4] or h1["atr"][i1]
        if atr_4h / price * 100 > VOL_CAP_PCT:
            continue

        if ENTRY["filter_4h"]:
            bull4 = h4["ema20"][i4] > h4["ema50"][i4]
            bear4 = h4["ema20"][i4] < h4["ema50"][i4]
            if burst_dir == "LONG" and not bull4:
                continue
            if burst_dir == "SHORT" and not bear4:
                continue

        if ENTRY["filter_1h_trend"]:
            tr = trend_label(h1["closes"][i1], h1["ema20"][i1], h1["ema50"][i1])
            if burst_dir == "LONG" and tr == "DOWNTREND":
                continue
            if burst_dir == "SHORT" and tr == "UPTREND":
                continue

        if ENTRY["confirm_15m"]:
            if i15 < 1:
                continue
            c15, o15 = m15["closes"][i15], m15["opens"][i15]
            if ("LONG" if c15 > o15 else "SHORT") != burst_dir:
                continue

        rsi_1h = h1["rsi"][i1]
        if burst_dir == "LONG" and rsi_1h >= ENTRY["late_rsi"]:
            continue
        if burst_dir == "SHORT" and rsi_1h <= (100 - ENTRY["late_rsi"]):
            continue

        trade_class = classify_trade(
            burst_dir=burst_dir,
            coin=coin,
            price=price,
            burst_vol=burst_vol,
            h1=h1,
            h4=h4,
            i1=i1,
            i4=i4,
            mode=mode,
        )
        if not trade_class:
            continue

        dk = _day_key(ts)
        cap = TREND_MAX_PER_DAY if trade_class == "TREND" else SCALP_MAX_PER_DAY
        if day_counts[dk][trade_class] >= cap:
            continue

        sz = _sizing_for_class(trade_class)
        atr_sl = data[sz["sl_atr_tf"]]["atr"][idx_at_or_before(data[sz["sl_atr_tf"]]["times"], ts)]
        atr_tp = data[sz["tp_atr_tf"]]["atr"][idx_at_or_before(data[sz["tp_atr_tf"]]["times"], ts)]
        if not atr_sl or not atr_tp:
            continue

        entry = price
        sl_dist = SL_MULT * atr_sl
        tp_dist = TP_MULT * atr_tp
        if burst_dir == "LONG":
            sl, tp = entry - sl_dist, entry + tp_dist
        else:
            sl, tp = entry + sl_dist, entry - tp_dist
        risk = sl_dist
        if risk <= 0:
            continue

        timeout = sz["timeout_bars"]
        max_i = min(i + timeout, len(act["closes"]) - 1)
        result = {"result": "TIMEOUT", "r": 0.0, "bars": timeout}
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
                result["r"] = (exit_p - entry) / risk - FEE_R
            else:
                result["r"] = (entry - exit_p) / risk - FEE_R
            result["bars"] = max_i - i

        if result["result"] == "SL":
            last_sl_i[coin] = i

        day_counts[dk][trade_class] += 1
        last_entry_i = i
        trades.append({
            "symbol": symbol,
            "coin": coin,
            "class": trade_class,
            "direction": burst_dir,
            "burst_vol": round(burst_vol, 2),
            "result": result["result"],
            "r": round(result["r"], 3),
            "bars": result["bars"],
            "in_allowlist": coin.upper() in ALLOWLIST,
        })

    return trades


MODES = {
    "A_LIVE_ALL_SCALP": "all_scalp",
    "B_ROUTER_2CLASS": "router",
    "C_ALL_TREND": "all_trend",
    "D_TREND_ONLY": "trend_only",
}


def run_mode(name: str, mode: str, days: int, cache: dict) -> dict:
    all_trades: list[dict] = []
    for coin in COINS:
        sym = coin + "USDT"
        try:
            all_trades.extend(backtest_coin(sym, days, mode, cache))
        except Exception as exc:
            print(f"  [warn] {sym}: {exc}")

    n = len(all_trades)
    if n == 0:
        print(f"  {name:22} | no signals")
        return {"name": name, "n": 0, "total_r": 0, "avg_r": 0, "wr": 0, "trades": []}

    wins = sum(1 for t in all_trades if t["r"] > 0)
    total_r = sum(t["r"] for t in all_trades)
    wr = wins / n * 100
    avg = total_r / n
    avg_bars = sum(t["bars"] for t in all_trades) / n

    by_class: dict[str, list] = defaultdict(list)
    for t in all_trades:
        by_class[t["class"]].append(t)

    flag = "  <-- +EV" if total_r > 0 and n >= 20 else ""
    print(f"  {name:22} | N={n:>4} WR={wr:>5.1f}% totalR={total_r:>+9.2f} "
          f"avgR={avg:>+.3f} avgBars={avg_bars:>5.1f}{flag}")
    for cls in ("TREND", "SCALP"):
        ct = by_class.get(cls, [])
        if ct:
            cr = sum(x["r"] for x in ct)
            cwr = sum(1 for x in ct if x["r"] > 0) / len(ct) * 100
            print(f"      {cls:5} n={len(ct):>4} WR={cwr:>5.1f}% subR={cr:>+8.2f}")

    pnl = pnl_stats(all_trades)
    return {
        "name": name, "n": n, "total_r": total_r, "avg_r": avg, "wr": wr,
        "trades": all_trades, "pnl": pnl,
    }


def _print_pnl_block(pnl: dict, indent: str = "  ") -> None:
    if not pnl or pnl.get("n", 0) == 0:
        return
    print(f"{indent}PnL (${PORTFOLIO_USD:.0f} wallet, tiered risk):")
    print(f"{indent}  Net: ${pnl['total_pnl_usd']:+,.2f}  |  "
          f"Gross +${pnl['gross_profit']:,.2f} / -${pnl['gross_loss']:,.2f}  |  "
          f"PF {pnl['profit_factor']:.2f}")
    print(f"{indent}  Avg/trade: ${pnl['avg_pnl_usd']:+.2f}  |  "
          f"Avg win: ${pnl['avg_win_usd']:+.2f}  |  "
          f"Avg loss: ${pnl['avg_loss_usd']:+.2f}")
    print(f"{indent}  W/L count: {pnl['win_count']}W / {pnl['loss_count']}L  |  "
          f"Max DD: ${pnl['max_drawdown_usd']:,.2f}")
    for cls in ("TREND", "SCALP"):
        cs = pnl.get("by_class", {}).get(cls)
        if cs:
            print(f"{indent}  {cls}: net ${cs['total_pnl']:+,.2f}  "
                  f"({cs['n']}t PF {cs['profit_factor']:.2f}  "
                  f"avgW ${cs['avg_win']:+.2f} avgL ${cs['avg_loss']:+.2f})")


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    print(f"=== Trade-Class Router Backtest — {days}d, {len(COINS)} coins ===")
    print(f"  Entry: 5m MTF (0.7 range, 1.6 vol, 4H+1H+15m confirm)")
    print(f"  SCALP: SL/TP 5m ATR, 3h timeout | TREND: SL 5m / TP 1h ATR, 6h timeout")
    print(f"  Router: rule-based (no LLM) | SL-cooldown 4h | cap 2 scalp + 1 trend/day/coin\n")

    cache: dict = {}
    results = []
    for name, mode in MODES.items():
        print(f"--- {name} ---")
        r = run_mode(name, mode, days, cache)
        _print_pnl_block(r.get("pnl", {}))
        results.append(r)
        print()

    viable = [r for r in results if r["n"] >= 20]
    if not viable:
        print("Insufficient signals.")
        return

    best = max(viable, key=lambda x: x["total_r"])
    best_avg = max(viable, key=lambda x: x["avg_r"])
    print("=== SUMMARY ===")
    print(f"  Best totalR: {best['name']} ({best['total_r']:+.2f}R, N={best['n']}, WR={best['wr']:.1f}%)")
    print(f"  Best avgR:   {best_avg['name']} ({best_avg['avg_r']:+.3f}R/trade)")

    a = next(r for r in results if r["name"] == "A_LIVE_ALL_SCALP")
    b = next(r for r in results if r["name"] == "B_ROUTER_2CLASS")
    if a["n"] and b["n"]:
        delta_r = b["total_r"] - a["total_r"]
        pa, pb = a.get("pnl", {}), b.get("pnl", {})
        delta_usd = pb.get("total_pnl_usd", 0) - pa.get("total_pnl_usd", 0)
        print(f"  Router vs Live (R):   {delta_r:+.2f}R")
        print(f"  Router vs Live (USD): ${delta_usd:+,.2f} net  "
              f"(${pa.get('total_pnl_usd', 0):+,.2f} → ${pb.get('total_pnl_usd', 0):+,.2f})")
        print(f"  Profit factor:        {pa.get('profit_factor', 0):.2f} → "
              f"{pb.get('profit_factor', 0):.2f}")
        print(f"  Max drawdown:         ${pa.get('max_drawdown_usd', 0):,.2f} → "
              f"${pb.get('max_drawdown_usd', 0):,.2f}")

    print("\n=== PNL COMPARISON TABLE ===")
    print(f"  {'Mode':<22} {'Net$':>9} {'PF':>5} {'Avg$':>7} {'AvgW$':>7} {'AvgL$':>7} "
          f"{'MaxDD$':>8} {'WR%':>5} {'N':>5}")
    for r in viable:
        p = r.get("pnl", {})
        if not p:
            continue
        print(f"  {r['name']:<22} ${p['total_pnl_usd']:>+8.2f} {p['profit_factor']:>5.2f} "
              f"${p['avg_pnl_usd']:>+6.2f} ${p['avg_win_usd']:>+6.2f} ${p['avg_loss_usd']:>+6.2f} "
              f"${p['max_drawdown_usd']:>7.2f} {p['wr']:>5.1f} {p['n']:>5}")

    print("\n  LLM for classification? NO — use rules:")
    print("    - Deterministic, backtestable, $0, <1ms (LLM gate was -EV in May 2026)")
    print("    - LLM kept for Telegram analysis only, not per-signal routing")

    if best["name"] == "B_ROUTER_2CLASS" and b["total_r"] > a.get("total_r", 0):
        print("  VERDICT: Deploy rule-based 2-class router after live review week.")
    elif best["name"] == "A_LIVE_ALL_SCALP":
        print("  VERDICT: Router does not beat all-scalp on this window — keep live, tune rules.")
    else:
        print(f"  VERDICT: Best mode {best['name']} — review class breakdown before deploy.")


if __name__ == "__main__":
    main()
