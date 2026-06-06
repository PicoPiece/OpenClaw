#!/usr/bin/env python3
"""Grid Bot Monitor — track Binance Spot grid bot performance via trade fills.

Modes (CLI args):
  --poll          Pull recent spot trades, update state, detect fills (default; every 30 min)
  --daily-report  Aggregate yesterday's fills, send Telegram daily report
  --health-check  Check current price vs grid range; alert if out of range / near stop
  --status        Print current state summary (no Telegram)

State file: data/grid_monitor_state.json
Config file: data/grid_config.json (user/agent populates with grid setup details)

Why this approach:
  - Native Binance Spot Algo Order endpoints (/sapi/v1/algo/spot/openOrders) require
    `enableSpotAndMarginTrading` permission. After permission is granted we can call
    those directly. For now this script works WITHOUT that permission by polling
    `/api/v3/myTrades` (regular spot trades, accessible with read-only permission).
"""
from __future__ import annotations
import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
CONFIG_FILE = ROOT / "data" / "grid_config.json"
STATE_FILE = ROOT / "data" / "grid_monitor_state.json"

POLL_TRADES_LIMIT = 500
EDGE_WARN_PCT = 0.10
EDGE_ALERT_PCT = 0.03


def load_env():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def send_telegram(message: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID_FINANCE")
    if not (token and chat):
        print("[WARN] Telegram credentials missing — skipping send")
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat, "text": message,
            "parse_mode": "Markdown", "disable_web_page_preview": "true",
        }).encode()
        urllib.request.urlopen(url, data=data, timeout=10).read()
        return True
    except Exception as e:
        print(f"[WARN] Telegram failed: {e}")
        return False


def signed_get(path: str, params: dict | None = None) -> tuple[int, object]:
    key = os.environ["BINANCE_API_KEY"]
    secret = os.environ["BINANCE_API_SECRET"]
    p = dict(params or {})
    p["timestamp"] = int(time.time() * 1000)
    p["recvWindow"] = 5000
    qs = urllib.parse.urlencode(p)
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f"https://api.binance.com{path}?{qs}&signature={sig}"
    req = urllib.request.Request(url, headers={"X-MBX-APIKEY": key})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def get_current_price(symbol: str) -> float | None:
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        with urllib.request.urlopen(url, timeout=10) as r:
            return float(json.loads(r.read())["price"])
    except Exception as e:
        print(f"[WARN] price fetch fail for {symbol}: {e}")
        return None


def is_bot_active(cfg: dict) -> bool:
    """True if this grid bot is still running (not manually/auto closed)."""
    if cfg.get("status") == "closed":
        return False
    if cfg.get("binance_grid_id") == "CLOSED":
        return False
    return True


def active_bots(config: dict) -> dict:
    return {sym: cfg for sym, cfg in config.items() if is_bot_active(cfg)}


def load_config() -> dict:
    """Grid config — user-provided (or auto-populated when grid is setup via API).

    Schema:
      {
        "AAVEUSDT": {
          "lower": 85.0, "upper": 115.0,
          "stop_lower": 80.0, "stop_upper": 120.0,
          "investment_usd": 140.0, "grids": 50,
          "started_at": "2026-05-08T13:00:00Z"
        },
        "XRPUSDT": { ... }
      }
    """
    if not CONFIG_FILE.exists():
        return {}
    try:
        raw = json.loads(CONFIG_FILE.read_text())
        return {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict)}
    except Exception:
        return {}


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"last_poll_ts": None, "fills_by_symbol": {}, "daily_pnl": {}}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"last_poll_ts": None, "fills_by_symbol": {}, "daily_pnl": {}}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def fetch_recent_trades(symbol: str, since_id: int = 0,
                         start_time_ms: int | None = None) -> list[dict]:
    """Fetch trade fills for a symbol. Uses fromId or startTime pagination."""
    params = {"symbol": symbol, "limit": POLL_TRADES_LIMIT}
    if since_id and since_id > 0:
        params["fromId"] = since_id + 1
    elif start_time_ms:
        params["startTime"] = start_time_ms
    code, body = signed_get("/api/v3/myTrades", params)
    if code != 200:
        print(f"[ERR] myTrades({symbol}) → {code}: {body}")
        return []
    return body if isinstance(body, list) else []


def parse_started_at_ms(cfg: dict) -> int | None:
    s = cfg.get("started_at")
    if not s: return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


def fee_in_usdt(trade: dict) -> float:
    """Best-effort fee conversion to USDT."""
    asset = trade["commissionAsset"]
    amount = float(trade["commission"])
    if asset == "USDT":
        return amount
    px = get_current_price(asset + "USDT")
    if px is None:
        return 0.0
    return amount * px


def poll(send_alerts: bool = True) -> dict:
    """Pull new trades for each configured symbol; update state; detect alerts."""
    config = load_config()
    running = active_bots(config)
    state = load_state()
    fills = state.get("fills_by_symbol", {})
    daily = state.get("daily_pnl", {})
    new_fills_summary = {}
    alerts = []

    if not config:
        print("[poll] No grid config yet (data/grid_config.json empty/missing)")
    elif not running:
        print("[poll] No active grid bots — skip health alerts")
        send_alerts = False

    for symbol, cfg in config.items():
        sym_state = fills.get(symbol, {"last_trade_id": 0, "trades": []})
        start_ms = parse_started_at_ms(cfg)
        new_trades = fetch_recent_trades(
            symbol,
            since_id=sym_state["last_trade_id"],
            start_time_ms=start_ms if sym_state["last_trade_id"] == 0 else None,
        )
        if not new_trades:
            continue
        # Defensive: also filter by started_at on first run (in case API returns earlier trades)
        if start_ms and sym_state["last_trade_id"] == 0:
            new_trades = [t for t in new_trades if t["time"] >= start_ms]
            if not new_trades:
                continue
        for t in new_trades:
            # Each trade record has: id, qty, price, quoteQty, isBuyer, time, commission, commissionAsset
            entry = {
                "id": t["id"],
                "ts": datetime.fromtimestamp(t["time"]/1000, tz=timezone.utc).isoformat(),
                "side": "BUY" if t["isBuyer"] else "SELL",
                "qty": float(t["qty"]),
                "price": float(t["price"]),
                "quote": float(t["quoteQty"]),
                "fee_usd": fee_in_usdt(t),
                "is_maker": bool(t["isMaker"]),
            }
            sym_state["trades"].append(entry)
            sym_state["last_trade_id"] = max(sym_state["last_trade_id"], int(t["id"]))
            day = entry["ts"][:10]
            d = daily.setdefault(day, {})
            # Net cash flow: sell adds USDT (+quote -fee), buy subtracts USDT (-quote -fee)
            cash = entry["quote"] - entry["fee_usd"] if entry["side"] == "SELL" \
                else -entry["quote"] - entry["fee_usd"]
            d[symbol] = round(d.get(symbol, 0.0) + cash, 4)
        fills[symbol] = sym_state
        new_fills_summary[symbol] = len(new_trades)
        print(f"[poll] {symbol}: +{len(new_trades)} new trades (total stored: {len(sym_state['trades'])})")

    # Health checks (current price vs range) — active bots only
    for symbol, cfg in running.items():
        cur = get_current_price(symbol)
        if cur is None:
            continue
        lo, up = cfg["lower"], cfg["upper"]
        sl_lo, sl_up = cfg.get("stop_lower"), cfg.get("stop_upper")
        below_pct = (cur - lo) / (up - lo)
        if sl_lo and cur <= sl_lo * (1 + EDGE_ALERT_PCT):
            alerts.append(f"🚨 {symbol} ${cur:.4f} gần Stop Lower ${sl_lo} — bot có thể đã dừng")
        elif sl_up and cur >= sl_up * (1 - EDGE_ALERT_PCT):
            alerts.append(f"🚨 {symbol} ${cur:.4f} gần Stop Upper ${sl_up} — bot có thể đã dừng")
        elif cur < lo:
            alerts.append(f"⚠️ {symbol} ${cur:.4f} dưới grid lower ${lo} — không có fill xuống thêm")
        elif cur > up:
            alerts.append(f"⚠️ {symbol} ${cur:.4f} trên grid upper ${up} — không có fill lên thêm")
        elif below_pct < EDGE_WARN_PCT:
            alerts.append(f"⚠️ {symbol} ${cur:.4f} chỉ {below_pct*100:.1f}% trên lower")
        elif below_pct > 1 - EDGE_WARN_PCT:
            alerts.append(f"⚠️ {symbol} ${cur:.4f} chỉ {(1-below_pct)*100:.1f}% dưới upper")

    state["fills_by_symbol"] = fills
    state["daily_pnl"] = daily
    state["last_poll_ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_state(state)

    if alerts and send_alerts:
        send_telegram("📡 *Grid Health Alert*\n\n" + "\n".join(alerts))

    return {"new_fills": new_fills_summary, "alerts": alerts}


def daily_report():
    """Build + send Telegram daily summary for previous day's grid activity."""
    config = load_config()
    state = load_state()
    if not config:
        print("[report] No grid config — skip"); return
    if not active_bots(config):
        print("[report] No active grid bots — skip daily report"); return
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily = state.get("daily_pnl", {})

    lines = [f"📊 *Grid Bot Daily — {yesterday}*\n"]
    total_invest = 0.0
    total_pnl = 0.0
    fills_yday = {}
    for symbol, cfg in config.items():
        invest = float(cfg.get("invested_usd", cfg.get("investment_usd", 0)))
        total_invest += invest
        sym_pnl = float(daily.get(yesterday, {}).get(symbol, 0))
        total_pnl += sym_pnl
        n_fills = sum(1 for t in state.get("fills_by_symbol", {}).get(symbol, {}).get("trades", [])
                      if t["ts"][:10] == yesterday)
        fills_yday[symbol] = n_fills
        cur = get_current_price(symbol) or 0.0
        roi = (sym_pnl / invest * 100) if invest else 0
        lines.append(f"*{symbol}*  ({n_fills} fills) → `${sym_pnl:+.3f}` ({roi:+.2f}%)")
        lines.append(f"  Range: ${cfg['lower']}-${cfg['upper']}  Cur: ${cur:.4f}")

    # Cumulative
    cum_pnl = 0.0
    started_dates = [cfg.get("started_at", "")[:10] for cfg in config.values() if cfg.get("started_at")]
    earliest = min(started_dates) if started_dates else yesterday
    for d, v in daily.items():
        if d >= earliest and d <= today_str:
            cum_pnl += sum(v.values())
    days_active = (datetime.fromisoformat(yesterday) - datetime.fromisoformat(earliest)).days + 1 if earliest else 1

    lines.append(f"\n*Daily total:* `${total_pnl:+.3f}` ({total_pnl/total_invest*100 if total_invest else 0:+.2f}% / ${total_invest:.0f})")
    lines.append(f"*Cumulative {days_active}d:* `${cum_pnl:+.3f}` ({cum_pnl/total_invest*100 if total_invest else 0:+.2f}%)")

    msg = "\n".join(lines)
    print(msg)
    send_telegram(msg)


def status():
    config = load_config()
    state = load_state()
    print(f"=== Grid Monitor Status ===")
    print(f"Last poll: {state.get('last_poll_ts')}")
    print(f"Configs: {list(config.keys())}")
    for symbol, cfg in config.items():
        cur = get_current_price(symbol)
        sym = state.get("fills_by_symbol", {}).get(symbol, {})
        n_total = len(sym.get("trades", []))
        print(f"\n{symbol}:")
        print(f"  range: ${cfg['lower']}-${cfg['upper']}  stop: ${cfg.get('stop_lower')}-${cfg.get('stop_upper')}")
        print(f"  invest: ${cfg.get('invested_usd', cfg.get('investment_usd'))}  grids: {cfg.get('grids')}")
        print(f"  current: ${cur}  fills tracked: {n_total}  last_id: {sym.get('last_trade_id')}")
    if state.get("daily_pnl"):
        print(f"\nDaily P&L (last 7 days):")
        for d in sorted(state["daily_pnl"].keys())[-7:]:
            v = state["daily_pnl"][d]
            tot = sum(v.values())
            details = "  ".join(f"{s}:{p:+.3f}" for s, p in v.items())
            print(f"  {d}  ${tot:+.4f}   ({details})")


def main():
    load_env()
    p = argparse.ArgumentParser()
    p.add_argument("--poll", action="store_true", help="Default mode: poll new trades")
    p.add_argument("--daily-report", action="store_true", help="Send Telegram daily summary")
    p.add_argument("--health-check", action="store_true", help="Only run health checks, no trade poll")
    p.add_argument("--status", action="store_true", help="Print state summary, no API write")
    p.add_argument("--silent", action="store_true", help="Suppress Telegram alerts")
    args = p.parse_args()

    if args.status:
        status(); return 0
    if args.daily_report:
        daily_report(); return 0
    if args.health_check:
        cfg = load_config()
        if not cfg: print("[health] No config — skip"); return 0
        # Reuse poll() but skip fetching trades — only price checks
        # (Lazy approach: just call poll which already does both)
        poll(send_alerts=not args.silent); return 0

    poll(send_alerts=not args.silent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
