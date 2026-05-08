#!/usr/bin/env python3
"""
Morning Briefing — Python rule-based daily portfolio summary.

Runs 08:00 ICT daily. Reads state files, computes metrics, sends Telegram.
NO LLM CALLS. Cost: $0.

Reads:
  - data/executor_state.json (Futures P&L)
  - data/wallet_balance_history.json (4-layer wallets)
  - data/grid_config.json (grid bots)
  - self_sustainability.compute_asi() (ASI metric)
  - Live Binance API (BTC market regime, 24h prices)
"""
from __future__ import annotations
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

ENV_FILE = ROOT / ".env"


def load_env():
    if not ENV_FILE.exists(): return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def safe_load(path: Path):
    if not path.exists(): return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def send_telegram(text: str, parse_mode: str = "HTML"):
    """Send Telegram with HTML parse mode (more reliable than Markdown)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[WARN] no Telegram creds")
        return
    body = {"chat_id": chat_id, "text": text}
    if parse_mode:
        body["parse_mode"] = parse_mode
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        if parse_mode:
            print(f"[WARN] {parse_mode} failed ({e}), retrying plain text")
            return send_telegram(text, parse_mode=None)
        print(f"[ERR] Telegram send failed: {e}")
        return None


def html_escape(s) -> str:
    if s is None: return ""
    return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")


def b(text) -> str:
    return f"<b>{html_escape(text)}</b>"


def i(text) -> str:
    return f"<i>{html_escape(text)}</i>"


def fetch_market_summary():
    """Fetch BTC + key altcoin 24h state. Public API, no auth."""
    url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    coins = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "AAVEUSDT", "XRPUSDT"]
    out = {}
    for sym in coins:
        try:
            with urllib.request.urlopen(f"{url}?symbol={sym}", timeout=8) as r:
                d = json.loads(r.read())
            out[sym] = {
                "price": float(d["lastPrice"]),
                "chg_24h": float(d["priceChangePercent"]),
                "high_24h": float(d["highPrice"]),
                "low_24h": float(d["lowPrice"]),
            }
        except Exception:
            out[sym] = None
    return out


def fetch_btc_regime():
    """7-day BTC trend vs EMA7."""
    try:
        url = "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1d&limit=14"
        with urllib.request.urlopen(url, timeout=8) as r:
            k = json.loads(r.read())
        last = float(k[-1][4])
        d7_ago = float(k[-7][4])
        ema7 = sum(float(c[4]) for c in k[-7:]) / 7
        chg_7d = (last - d7_ago) / d7_ago * 100
        regime = "🟢 UPTREND" if last > ema7 and chg_7d > 0 else \
                 "🔴 DOWNTREND" if last < ema7 and chg_7d < 0 else \
                 "🟡 NEUTRAL"
        return {"price": last, "ema7": ema7, "chg_7d": chg_7d, "regime": regime}
    except Exception as e:
        return {"regime": f"unknown ({e})"}


def get_overnight_trades(hours=12):
    """Trades from executor_state in last N hours."""
    state = safe_load(DATA / "executor_state.json") or {}
    history = state.get("trade_history", [])
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    recent = []
    for t in history:
        try:
            ts = datetime.fromisoformat(t.get("time", "").replace("Z", "+00:00"))
            if ts >= cutoff:
                recent.append(t)
        except Exception:
            pass
    return recent


def get_wallet_summary():
    history = safe_load(DATA / "wallet_balance_history.json") or {}
    snaps = history.get("snapshots", [])
    if not snaps:
        return None
    latest = snaps[-1]
    wallets = latest.get("wallets", {})
    total = latest.get("total", 0)

    # 24h delta
    cutoff_24h = datetime.fromisoformat(latest["ts"].replace("Z","+00:00")) - timedelta(hours=24)
    snap_24h = None
    for s in reversed(snaps):
        try:
            ts = datetime.fromisoformat(s["ts"].replace("Z","+00:00"))
            if ts <= cutoff_24h:
                snap_24h = s
                break
        except Exception:
            pass

    deltas = {}
    if snap_24h:
        prev_wallets = snap_24h.get("wallets", {})
        for w, v in wallets.items():
            deltas[w] = v - prev_wallets.get(w, 0)
        deltas["TOTAL"] = total - snap_24h.get("total", 0)

    return {"total": total, "wallets": wallets, "deltas_24h": deltas,
            "snapshot_count": len(snaps), "ts": latest["ts"]}


def get_pending_signal():
    p = safe_load(DATA / "pending_signal.json") or {}
    return p


def get_active_positions():
    """Read from trading_state.json."""
    state = safe_load(DATA / "workspace-finance" / "trading_state.json") or {}
    positions = state.get("positions", [])
    active = [p for p in positions if p.get("status") == "ACTIVE"]
    return active


def format_money(v: float, plus_sign=False) -> str:
    sign = "+" if (v >= 0 and plus_sign) else ("-" if v < 0 else "")
    return f"{sign}${abs(v):,.2f}"


def build_briefing() -> str:
    now = datetime.now()
    date_str = now.strftime("%a %d/%m/%Y %H:%M ICT")

    lines = [f"🌅 {b('MORNING BRIEFING')} — {html_escape(date_str)}", ""]

    # === Portfolio ===
    wallet = get_wallet_summary()
    if wallet:
        lines.append(f"💰 {b('PORTFOLIO')}")
        lines.append(f"Total: {b(format_money(wallet['total']))}")
        if wallet["deltas_24h"]:
            d = wallet["deltas_24h"].get("TOTAL", 0)
            arrow = "📈" if d > 0 else "📉" if d < 0 else "➖"
            lines.append(f"24h: {arrow} {html_escape(format_money(d, plus_sign=True))}")
        lines.append("")
        lines.append("Layers:")
        layer_map = {
            "Earn": "💎 HODL Core",
            "Trading Bots": "🌐 Grid Yield",
            "USDⓈ-M Futures": "🤖 Active Futures",
            "Spot": "💵 Reserve",
        }
        for w_name, label in layer_map.items():
            v = wallet["wallets"].get(w_name, 0)
            d = wallet["deltas_24h"].get(w_name, 0)
            sign = "+" if d > 0 else "-" if d < 0 else "·"
            lines.append(f"  {html_escape(label)}: ${v:,.2f}  ({sign}${abs(d):.2f})")
        lines.append("")

    # === ASI ===
    try:
        import self_sustainability
        asi = self_sustainability.compute_asi()
        emoji = asi["status"]
        lines.append(f"📊 {b('AI Self-Sustainability')}")
        asi_val = f"{asi['asi']:.2f}"
        asi_label = asi['label'].replace('_', ' ')
        lines.append(f"{emoji} ASI = {b(asi_val)} ({html_escape(asi_label)})")
        lines.append(f"Profit/mo: {html_escape(format_money(asi['profit_monthly'], True))}  ·  Cost/mo: ${asi['cost_monthly']:.2f}")
        lines.append(f"Net: {b(format_money(asi['net_monthly'], True) + '/mo')}")
        lines.append("")
    except Exception as e:
        lines.append(f"ASI compute err: {html_escape(e)}")
        lines.append("")

    # === Market regime ===
    btc = fetch_btc_regime()
    market = fetch_market_summary()
    lines.append(f"📉 {b('MARKET (BTC)')}")
    if "price" in btc:
        lines.append(f"Price: ${btc['price']:,.0f}  ·  EMA7: ${btc['ema7']:,.0f}")
        lines.append(f"7d: {btc['chg_7d']:+.2f}%  ·  Regime: {html_escape(btc['regime'])}")
    lines.append("")

    if market:
        lines.append("Top 5 (24h%):")
        for sym, d in market.items():
            if d:
                arrow = "🟢" if d["chg_24h"] > 0 else "🔴"
                coin = sym.replace('USDT','')
                lines.append(f"  {arrow} {coin:5s}: ${d['price']:,.4f}  ({d['chg_24h']:+.2f}%)")
        lines.append("")

    # === Overnight Futures activity ===
    overnight = get_overnight_trades(12)
    if overnight:
        lines.append(f"🌙 {b('OVERNIGHT FUTURES')} ({len(overnight)} trades 12h)")
        total_pnl = sum(t.get("pnl", 0) for t in overnight)
        wins = sum(1 for t in overnight if t.get("pnl", 0) > 0)
        lines.append(f"P&amp;L: {html_escape(format_money(total_pnl, True))}  ·  WR: {wins}/{len(overnight)}")
        for t in overnight[-3:]:  # last 3
            r = t.get("result", "?")
            emoji = "✅" if t.get("pnl", 0) > 0 else "❌"
            coin = t.get('coin','?').upper()
            dir_ = t.get('direction','?')[:1]
            lines.append(f"  {emoji} {coin}/{dir_} {format_money(t.get('pnl',0), True)} ({r})")
        lines.append("")
    else:
        lines.append(f"🌙 {b('OVERNIGHT FUTURES')}: No trades (market dead/filter active)")
        lines.append("")

    # === Active positions ===
    active = get_active_positions()
    if active:
        lines.append(f"📌 {b('ACTIVE POSITIONS')} ({len(active)})")
        for p in active[:5]:
            coin = p.get('coin','?').upper()
            dir_ = p.get('direction','?')[:1]
            lines.append(f"  {coin}/{dir_} entry ${p.get('entry','?')} qty {p.get('qty','?')}")
        lines.append("")
    else:
        lines.append(f"📌 {b('ACTIVE POSITIONS')}: 0")
        lines.append("")

    # === Pending signal (last reviewed) ===
    pending = get_pending_signal()
    if pending and pending.get("status"):
        lines.append(f"🔔 {b('LAST SIGNAL')}")
        ts = pending.get("timestamp", "")[:16]
        coin = pending.get("coin", "?").upper()
        dir_ = pending.get("direction", "?")
        status = pending.get("status", "?")
        reason = (pending.get("llm_reason", "") or pending.get("reason", ""))[:140]
        lines.append(f"  {coin} {dir_} - {html_escape(status)}")
        lines.append(f"  {html_escape(ts)}")
        lines.append(f"  {i('Reason:')} {html_escape(reason)}")
        lines.append("")

    # === Outlook ===
    lines.append(f"🎯 {b('OUTLOOK')}")
    if "regime" in btc:
        if "UPTREND" in btc["regime"]:
            lines.append("  Market uptrend → favor LONG signals on dips")
        elif "DOWNTREND" in btc["regime"]:
            lines.append("  Market downtrend → favor SHORT signals on rips")
        else:
            lines.append("  Market neutral → wait for clear breakout")

    if market and market.get("BTCUSDT"):
        chg = market["BTCUSDT"]["chg_24h"]
        if abs(chg) < 1.5:
            lines.append("  BTC low vol → grid bots ideal · Futures may have few signals")
        else:
            lines.append("  BTC high vol → expect more Futures signals · Grid alert risk")

    lines.append("")
    lines.append(i("Chat /status, /positions, /grids, /asi, /help for live data"))

    return "\n".join(lines)


def main():
    load_env()
    text = build_briefing()
    print(text)
    print()
    print("--- Sending to Telegram ---")
    result = send_telegram(text)
    if result and result.get("ok"):
        print("[ok] Briefing sent")
    else:
        print(f"[err] Telegram: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
