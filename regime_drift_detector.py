#!/usr/bin/env python3
"""Regime Drift Detector — Shield 2.

Daily check (mỗi 6h):
  1. BTC 7-day momentum (% change) → classify UPTREND / DOWNTREND / SIDEWAYS
  2. Per-coin rolling 7d WR vs backtest baseline
  3. Overall live WR rolling 7d vs target (≥ 45%)
  4. Send Telegram alert nếu regime change OR WR drop > 15pp

Output: data/regime_state.json
Alert: Telegram khi có anomaly
"""

from __future__ import annotations
import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "decisions.db"
STATE_FILE = ROOT / "data" / "regime_state.json"
GRID_CONFIG = ROOT / "data" / "grid_config.json"
ENV_FILE = ROOT / ".env"

# Grid regime gate: how close (%) to stop_lower/stop_upper before we warn.
GRID_NEAR_STOP_PCT = 2.0

# Backtest 90d baseline (V6_COIN_FILTER allowlist 7)
BASELINE_WR = 45.6
WR_DROP_ALERT_PP = 15
ROLLING_DAYS = 7

ALLOWLIST = ["aave", "bnb", "btc", "eth", "link", "trx", "xrp"]
PER_COIN_BASELINE_WR = {
    "btc": 48, "eth": 43, "bnb": 40, "xrp": 49,
    "aave": 47, "link": 41, "trx": 54,
}


def load_env():
    if not ENV_FILE.exists(): return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def send_telegram(message: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID_FINANCE")
    if not (token and chat): return
    try:
        import urllib.parse, urllib.request
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat, "text": message,
                                        "parse_mode": "Markdown"}).encode()
        urllib.request.urlopen(url, data=data, timeout=10).read()
    except Exception as e:
        print(f"[WARN] Telegram failed: {e}")


def get_btc_regime() -> dict:
    """Return BTC 7d % change + regime label."""
    try:
        from binance.client import Client
        c = Client(os.environ.get("BINANCE_API_KEY"), os.environ.get("BINANCE_API_SECRET"))
        klines = c.futures_klines(symbol="BTCUSDT", interval="1d", limit=8)
        closes = [float(k[4]) for k in klines]
        first = closes[0]; last = closes[-1]
        pct = (last - first) / first * 100
        if pct > 5: regime = "UPTREND"
        elif pct < -5: regime = "DOWNTREND"
        else: regime = "SIDEWAYS"
        return {"regime": regime, "btc_change_pct": round(pct, 2),
                 "btc_first": first, "btc_last": last}
    except Exception as e:
        return {"regime": "UNKNOWN", "error": str(e)}


def check_grid_regime_gate(btc_regime: str) -> dict:
    """Regime-gated grid deployment check.

    Lesson (2026-06-02): Grid bots only profit in SIDEWAYS markets. In a BTC
    DOWNTREND they keep buying the dip all the way down and bleed (AAVE bot
    auto-stopped at -$62 / -14%). Rotating coins does NOT fix this — it just
    repeats "buy high, sell low". The fix is regime-gating: do NOT run/deploy
    grid bots while BTC is in a DOWNTREND.

    Returns dict with:
      - deploy_ok: bool — whether NEW grid deployment is advisable now
      - bots: list of per-bot status (in_range / below / above / near_stop)
      - alerts: list of human-readable warnings
    """
    result = {"deploy_ok": btc_regime != "DOWNTREND", "bots": [], "alerts": []}

    if not GRID_CONFIG.exists():
        return result

    try:
        cfg = json.loads(GRID_CONFIG.read_text())
    except Exception:
        return result

    symbols = [s for s in cfg if s.endswith("USDT")]
    if not symbols:
        return result

    # Fetch live spot prices
    prices = {}
    try:
        from binance.client import Client
        c = Client(os.environ.get("BINANCE_API_KEY"), os.environ.get("BINANCE_API_SECRET"))
        for sym in symbols:
            try:
                t = c.get_symbol_ticker(symbol=sym)
                prices[sym] = float(t["price"])
            except Exception:
                prices[sym] = None
    except Exception as e:
        result["alerts"].append(f"GRID_PRICE_FETCH_FAILED: {e}")
        return result

    below_range = []
    near_stop = []
    for sym in symbols:
        c_cfg = cfg[sym]
        # Skip bots already marked closed
        if c_cfg.get("status") == "closed" or c_cfg.get("binance_grid_id") == "CLOSED":
            continue
        price = prices.get(sym)
        if price is None:
            continue
        lo = c_cfg.get("lower", 0)
        hi = c_cfg.get("upper", 0)
        sl_lo = c_cfg.get("stop_lower", 0)
        sl_hi = c_cfg.get("stop_upper", 0)

        if sl_lo and price <= sl_lo * (1 + GRID_NEAR_STOP_PCT / 100):
            status = "NEAR_STOP_LOW"
            near_stop.append(sym)
        elif sl_hi and price >= sl_hi * (1 - GRID_NEAR_STOP_PCT / 100):
            status = "NEAR_STOP_HIGH"
            near_stop.append(sym)
        elif lo and price < lo:
            status = "BELOW_RANGE"
            below_range.append(sym)
        elif hi and price > hi:
            status = "ABOVE_RANGE"
        else:
            status = "IN_RANGE"

        result["bots"].append({
            "symbol": sym, "price": price, "lower": lo, "upper": hi,
            "stop_lower": sl_lo, "stop_upper": sl_hi, "status": status,
            "invested_usd": c_cfg.get("invested_usd", 0),
        })

    if near_stop:
        result["alerts"].append(
            f"GRID_NEAR_STOP: {', '.join(near_stop)} within {GRID_NEAR_STOP_PCT}% of stop — "
            f"review/close manually (downtrend kills grids)"
        )
    if btc_regime == "DOWNTREND" and below_range:
        result["alerts"].append(
            f"GRID_REGIME_RISK: BTC DOWNTREND + {len(below_range)} bot(s) below range "
            f"({', '.join(below_range)}). Grids bleed in downtrend — do NOT deploy new "
            f"grids; consider parking capital in Earn until SIDEWAYS resumes."
        )
    elif btc_regime == "DOWNTREND":
        result["alerts"].append(
            "GRID_DEPLOY_BLOCKED: BTC DOWNTREND — new grid deployment not advised. "
            "Park free capital in Earn until regime turns SIDEWAYS/UPTREND."
        )

    return result


def get_live_wr() -> dict:
    """Per-coin + overall WR over rolling 7d from decisions.db."""
    if not DB_PATH.exists():
        return {"overall": None, "per_coin": {}}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ROLLING_DAYS)).isoformat()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute("""SELECT coin, pnl_usd, r_multiple FROM trades
                          WHERE closed_at IS NOT NULL AND closed_at >= ?
                            AND (notes IS NULL OR notes NOT LIKE '%phantom_closed_via_sibling%')""",
                       (cutoff,)).fetchall()
    con.close()
    if not rows:
        return {"overall": None, "per_coin": {}, "n": 0}
    per = {}
    for r in rows:
        coin = (r["coin"] or "").lower()
        d = per.setdefault(coin, {"n": 0, "w": 0, "l": 0, "pnl": 0.0, "r": 0.0})
        d["n"] += 1
        pnl = float(r["pnl_usd"] or 0)
        if pnl > 0: d["w"] += 1
        elif pnl < 0: d["l"] += 1
        d["pnl"] += pnl
        d["r"] += float(r["r_multiple"] or 0)
    for d in per.values():
        d["wr"] = d["w"]/(d["w"]+d["l"])*100 if d["w"]+d["l"] else 0
    total_n = sum(d["n"] for d in per.values())
    total_w = sum(d["w"] for d in per.values())
    total_l = sum(d["l"] for d in per.values())
    total_r = sum(d["r"] for d in per.values())
    total_pnl = sum(d["pnl"] for d in per.values())
    return {
        "overall": {"n": total_n, "w": total_w, "l": total_l, "pnl": total_pnl, "r": total_r,
                    "wr": total_w/(total_w+total_l)*100 if total_w+total_l else 0},
        "per_coin": per,
    }


def main():
    load_env()
    btc = get_btc_regime()
    live = get_live_wr()
    overall = live.get("overall")

    prev_state = {}
    if STATE_FILE.exists():
        try: prev_state = json.loads(STATE_FILE.read_text())
        except Exception: prev_state = {}

    grid_gate = check_grid_regime_gate(btc.get("regime", "UNKNOWN"))

    state = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "btc_regime": btc,
        "live_7d": live,
        "grid_gate": grid_gate,
        "alerts": [],
    }

    if prev_state.get("btc_regime", {}).get("regime") and prev_state["btc_regime"]["regime"] != btc.get("regime"):
        state["alerts"].append(f"REGIME_CHANGE: {prev_state['btc_regime']['regime']} → {btc['regime']} (BTC 7d: {btc['btc_change_pct']:+.2f}%)")

    # Grid regime-gate alerts (deduped against previous run to avoid spam)
    prev_grid_alerts = set(prev_state.get("grid_gate", {}).get("alerts", []))
    for ga in grid_gate.get("alerts", []):
        if ga not in prev_grid_alerts:
            state["alerts"].append(ga)

    if overall and overall["n"] >= 5:
        wr_drop = BASELINE_WR - overall["wr"]
        if wr_drop > WR_DROP_ALERT_PP:
            state["alerts"].append(f"LOW_WR: live 7d WR {overall['wr']:.1f}% < baseline {BASELINE_WR}% (drop {wr_drop:.1f}pp, N={overall['n']})")

    for coin, d in live.get("per_coin", {}).items():
        baseline = PER_COIN_BASELINE_WR.get(coin)
        if baseline and d["n"] >= 3:
            drop = baseline - d["wr"]
            if drop > 25:
                state["alerts"].append(f"COIN_WR_DROP: {coin.upper()} 7d WR {d['wr']:.0f}% << baseline {baseline}% (drop {drop:.0f}pp, N={d['n']})")

    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))

    print(f"[regime] BTC 7d: {btc.get('btc_change_pct',0):+.2f}% → {btc.get('regime')}")
    print(f"[grid] deploy_ok={grid_gate.get('deploy_ok')} ({len(grid_gate.get('bots',[]))} active bot(s))")
    for b in grid_gate.get("bots", []):
        print(f"  {b['symbol']:10} ${b['price']:>10.4f}  [{b['lower']}-{b['upper']}]  {b['status']}")
    if overall:
        print(f"[regime] Live 7d: N={overall['n']} W={overall['w']} L={overall['l']} WR={overall['wr']:.1f}% pnl=${overall['pnl']:+.2f} R={overall['r']:+.2f}")
        print(f"[regime] Per-coin (sample size>=2):")
        for coin, d in sorted(live["per_coin"].items()):
            if d["n"] >= 2:
                print(f"  {coin:6} N={d['n']:>2} WR={d['wr']:>5.1f}%  R={d['r']:+5.2f}  pnl=${d['pnl']:+.2f}")

    if state["alerts"]:
        msg = "🛡️ *Shield 2 — REGIME DRIFT DETECTED*\n\n" + "\n".join(f"• {a}" for a in state["alerts"])
        msg += f"\n\nBTC 7d: {btc.get('btc_change_pct',0):+.2f}% ({btc.get('regime')})"
        if overall: msg += f"\nLive 7d WR: {overall['wr']:.1f}% (N={overall['n']})"
        send_telegram(msg)
        print(f"\n⚠️  {len(state['alerts'])} alert(s) sent")
    else:
        print(f"\n✓ No anomaly")


if __name__ == "__main__":
    main()
