#!/usr/bin/env python3
"""
Forex Research Track — skeleton for testing strategies on FX / Gold pairs.

This is research-only (no live execution). It:
  1. Fetches OHLCV bars for configured FX symbols from Yahoo Finance v8
     chart endpoint (no API key required).
  2. Reuses the same EMA20/EMA50 + RSI + ATR signal logic the crypto bot uses.
  3. Logs simulated decisions into decisions.db with source='forex_research' so
     they can be reviewed alongside crypto decisions but do not pollute crypto
     stats.
  4. Runs daily as a cron job (or on demand). When user is ready to go live we
     swap the simulator for a broker adapter (Exness MT5, Oanda REST, etc.).

Configured pairs (edit FOREX_SYMBOLS list below):
  - EURUSD=X, GBPUSD=X, USDJPY=X        -> majors
  - GC=F                                 -> Gold front-month future
  - XAUUSD=X                             -> Spot gold (when available)

CLI:
  python3 forex_research.py --scan              # one-off scan + log decisions
  python3 forex_research.py --backtest --days N # walk-forward over N days
  python3 forex_research.py --report            # print research report

Future hooks (not implemented yet, marked TODO):
  - Broker adapter interface (open/close orders)
  - Session-aware trading (London / NY / Tokyo)
  - News calendar filter (forexfactory / investing.com scrape)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import decision_logger  # noqa: E402

FOREX_SYMBOLS = [
    {"symbol": "EURUSD=X", "label": "EURUSD", "type": "major"},
    {"symbol": "GBPUSD=X", "label": "GBPUSD", "type": "major"},
    {"symbol": "USDJPY=X", "label": "USDJPY", "type": "major"},
    {"symbol": "GC=F",     "label": "GOLD",   "type": "metal"},
]

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------

def fetch_bars(symbol: str, interval: str = "1h", range_: str = "30d") -> list[dict]:
    """Returns list of {ts, open, high, low, close, volume} bars."""
    params = {"interval": interval, "range": range_,
              "includePrePost": "false", "events": "div,splits"}
    url = YAHOO_CHART.format(symbol=urllib.parse.quote(symbol)) + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())
    res = data["chart"]["result"]
    if not res:
        return []
    r0 = res[0]
    ts = r0["timestamp"]
    q = r0["indicators"]["quote"][0]
    bars = []
    for i, t in enumerate(ts):
        if q["close"][i] is None:
            continue
        bars.append({"ts": t, "open": q["open"][i], "high": q["high"][i],
                     "low": q["low"][i], "close": q["close"][i],
                     "volume": q.get("volume", [0]*len(ts))[i] or 0})
    return bars


# ---------------------------------------------------------------------------
# Indicators (lightweight, mirror binance_price_alert.py)
# ---------------------------------------------------------------------------

def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(values: list[float], period: int = 14) -> list[float]:
    if len(values) < period + 1:
        return [50.0] * len(values)
    gains, losses = [0.0], [0.0]
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[1:period + 1]) / period
    avg_l = sum(losses[1:period + 1]) / period
    out = [50.0] * period
    for i in range(period, len(values)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        rs = avg_g / avg_l if avg_l > 0 else 999
        out.append(100 - (100 / (1 + rs)))
    return out


def atr(bars: list[dict], period: int = 14) -> list[float]:
    if len(bars) < 2:
        return [0.0] * len(bars)
    trs = [bars[0]["high"] - bars[0]["low"]]
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return [trs[0]] * len(bars)
    out = [0.0] * (period - 1)
    cur = sum(trs[:period]) / period
    out.append(cur)
    for i in range(period, len(trs)):
        cur = (cur * (period - 1) + trs[i]) / period
        out.append(cur)
    return out


# ---------------------------------------------------------------------------
# Signal generator (FX-tuned variant of crypto rules)
# ---------------------------------------------------------------------------

def compute_signal(bars: list[dict]) -> dict | None:
    if len(bars) < 60:
        return None
    closes = [b["close"] for b in bars]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    rs = rsi(closes, 14)
    a = atr(bars, 14)
    last = bars[-1]
    prev_e20, prev_e50 = e20[-2], e50[-2]
    cur_e20, cur_e50 = e20[-1], e50[-1]
    crossed_up = prev_e20 <= prev_e50 and cur_e20 > cur_e50
    crossed_dn = prev_e20 >= prev_e50 and cur_e20 < cur_e50
    side = None
    if crossed_up and rs[-1] < 70:
        side = "LONG"
    elif crossed_dn and rs[-1] > 30:
        side = "SHORT"
    if not side:
        return None
    indicators = {
        "rsi": round(rs[-1], 2),
        "ema20": round(cur_e20, 5),
        "ema50": round(cur_e50, 5),
        "atr": round(a[-1], 5),
        "close": round(last["close"], 5),
        "ema_gap_pct": round((cur_e20 - cur_e50) / cur_e50 * 100, 3),
    }
    sl = last["close"] - a[-1] * 1.5 if side == "LONG" else last["close"] + a[-1] * 1.5
    tp = last["close"] + a[-1] * 3.0 if side == "LONG" else last["close"] - a[-1] * 3.0
    return {"side": side, "entry": last["close"], "sl": sl, "tp": tp,
            "indicators": indicators}


# ---------------------------------------------------------------------------
# Scan + log
# ---------------------------------------------------------------------------

def scan_once(interval: str = "1h", range_: str = "30d",
                log: bool = True) -> list[dict]:
    out = []
    for sym in FOREX_SYMBOLS:
        try:
            bars = fetch_bars(sym["symbol"], interval=interval, range_=range_)
        except Exception as e:
            out.append({"symbol": sym["label"], "error": str(e)})
            continue
        sig = compute_signal(bars)
        if not sig:
            out.append({"symbol": sym["label"], "decision": "NO_SIGNAL"})
            continue
        ind_text = ", ".join(f"{k}={v}" for k, v in sig["indicators"].items())
        prompt = (f"FX research signal — {sym['label']} ({sym['type']}): "
                  f"{sig['side']} @ {sig['entry']:.5f} SL={sig['sl']:.5f} "
                  f"TP={sig['tp']:.5f}; indicators: {ind_text}")
        if log:
            try:
                decision_id = decision_logger.log_decision(
                    source="forex_research",
                    coin=sym["label"], direction=sig["side"],
                    model="rules-only",
                    prompt=prompt, response="(no LLM, research)",
                    decision="OBSERVE",
                    reason="research-only, not executed",
                    confidence=None,
                    indicators=sig["indicators"],
                    market_state={"asset_class": sym["type"],
                                  "session": _current_session()},
                    prompt_version="forex_research_v1",
                )
            except Exception as e:
                decision_id = None
                out.append({"symbol": sym["label"], "log_error": str(e)})
        else:
            decision_id = None
        out.append({"symbol": sym["label"], "side": sig["side"],
                    "entry": sig["entry"], "sl": sig["sl"], "tp": sig["tp"],
                    "indicators": sig["indicators"],
                    "decision_id": decision_id})
        time.sleep(0.4)
    return out


def _current_session() -> str:
    """Identify the active FX session by UTC hour."""
    h = datetime.now(timezone.utc).hour
    if 0 <= h < 7:
        return "tokyo"
    if 7 <= h < 12:
        return "london"
    if 12 <= h < 16:
        return "london_ny_overlap"
    if 16 <= h < 21:
        return "ny"
    return "ny_close"


# ---------------------------------------------------------------------------
# Backtest (very lightweight)
# ---------------------------------------------------------------------------

def backtest(days: int = 30, interval: str = "1h") -> dict:
    range_ = f"{max(days, 7)}d"
    summary = {"params": {"days": days, "interval": interval}, "by_symbol": []}
    for sym in FOREX_SYMBOLS:
        try:
            bars = fetch_bars(sym["symbol"], interval=interval, range_=range_)
        except Exception as e:
            summary["by_symbol"].append({"symbol": sym["label"], "error": str(e)})
            continue
        if len(bars) < 100:
            summary["by_symbol"].append({"symbol": sym["label"],
                                          "error": "insufficient bars"})
            continue
        wins = losses = 0
        rmults = []
        i = 60
        while i < len(bars) - 1:
            sig = compute_signal(bars[: i + 1])
            if not sig:
                i += 1
                continue
            r = abs(sig["entry"] - sig["sl"])
            entry = sig["entry"]
            hit_tp = hit_sl = False
            for j in range(i + 1, len(bars)):
                hi = bars[j]["high"]; lo = bars[j]["low"]
                if sig["side"] == "LONG":
                    if hi >= sig["tp"]:
                        hit_tp = True; break
                    if lo <= sig["sl"]:
                        hit_sl = True; break
                else:
                    if lo <= sig["tp"]:
                        hit_tp = True; break
                    if hi >= sig["sl"]:
                        hit_sl = True; break
            if hit_tp:
                wins += 1
                rmults.append(abs(sig["tp"] - entry) / r)
            elif hit_sl:
                losses += 1
                rmults.append(-1.0)
            i += max(4, 1)
        total = wins + losses
        win_rate = (wins / total * 100) if total else 0
        avg_r = (sum(rmults) / len(rmults)) if rmults else 0
        summary["by_symbol"].append({
            "symbol": sym["label"], "trades": total,
            "wins": wins, "losses": losses,
            "win_rate_pct": round(win_rate, 1),
            "avg_r": round(avg_r, 2),
            "total_r": round(sum(rmults), 2),
        })
        time.sleep(0.4)
    return summary


def report(days: int = 7) -> dict:
    """Pull forex_research decisions from db and summarize."""
    decision_logger.init_db()
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    with decision_logger._conn() as c:
        rows = c.execute(
            """SELECT coin, direction, decision, ts, indicators_json
                 FROM llm_decisions
                WHERE source='forex_research' AND ts >= ?
             ORDER BY ts DESC""", (cutoff_iso,)
        ).fetchall()
    by_sym: dict[str, dict] = {}
    for r in rows:
        s = by_sym.setdefault(r["coin"], {"long": 0, "short": 0, "total": 0})
        s["total"] += 1
        s["long" if r["direction"] == "LONG" else "short"] += 1
    return {"days": days, "total_signals": len(rows),
            "by_symbol": by_sym, "recent": [dict(r) for r in rows[:10]]}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true",
                    help="scan all FX symbols once and log signals")
    ap.add_argument("--backtest", action="store_true",
                    help="walk-forward backtest")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--no-log", action="store_true",
                    help="don't write decisions to DB (preview only)")
    args = ap.parse_args()
    if args.scan:
        out = scan_once(interval=args.interval, log=not args.no_log)
        print(json.dumps(out, indent=2, default=str))
    elif args.backtest:
        out = backtest(days=args.days, interval=args.interval)
        print(json.dumps(out, indent=2, default=str))
    elif args.report:
        out = report(days=args.days)
        print(json.dumps(out, indent=2, default=str))
    else:
        ap.print_help()


if __name__ == "__main__":
    cli()
