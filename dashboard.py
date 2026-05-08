#!/usr/bin/env python3
"""
Trading Dashboard — Real-time monitoring & historical analysis
Port 8686 — reads from JSON state files, no database needed.
"""

import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, jsonify, render_template_string

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import decision_logger
except Exception as _dl_err:
    decision_logger = None
    print(f"[WARN] dashboard: decision_logger unavailable: {_dl_err}")

app = Flask(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
TRADING_STATE_FILE = SCRIPT_DIR / "data" / "workspace-finance" / "trading_state.json"
EXECUTOR_STATE_FILE = SCRIPT_DIR / "data" / "executor_state.json"
ALERT_STATE_FILE = SCRIPT_DIR / "data" / "price_alert_state.json"
PENDING_SIGNAL_FILE = SCRIPT_DIR / "data" / "pending_signal.json"
SIGNAL_LOG_FILE = SCRIPT_DIR / "data" / "signal_log.json"
ENV_FILE = SCRIPT_DIR / ".env"

BINANCE_FUTURES_PRICE_API = "https://fapi.binance.com/fapi/v1/ticker/price"
BINANCE_SPOT_PRICE_API = "https://api.binance.com/api/v3/ticker/price"

_price_cache: dict = {}
_price_cache_ts: float = 0


def load_json(path: Path) -> dict | list:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def fetch_prices(symbols: list[str]) -> dict:
    global _price_cache, _price_cache_ts
    now = time.time()
    if now - _price_cache_ts < 5 and _price_cache:
        return {s: _price_cache.get(s, 0) for s in symbols}
    try:
        req = urllib.request.Request(BINANCE_FUTURES_PRICE_API, headers={"User-Agent": "PicoDash/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        all_prices = {d["symbol"]: float(d["price"]) for d in data}
        _price_cache = all_prices
        _price_cache_ts = now
        return {s: all_prices.get(s, 0) for s in symbols}
    except Exception:
        return {s: _price_cache.get(s, 0) for s in symbols}


def get_signal_log() -> list:
    if SIGNAL_LOG_FILE.exists():
        return json.loads(SIGNAL_LOG_FILE.read_text())
    return []


# ---------------------------------------------------------------------------
# Live Binance data (source of truth for balance + open positions)
# ---------------------------------------------------------------------------
_binance_cache: dict = {}
_binance_cache_ts: float = 0


def _binance_signed(path: str, params: dict, env: dict) -> object:
    api_key = env.get("BINANCE_API_KEY")
    api_secret = env.get("BINANCE_API_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError("Binance keys missing")
    p = dict(params)
    p["timestamp"] = int(time.time() * 1000)
    qs = urllib.parse.urlencode(p)
    sig = hmac.new(api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f"https://fapi.binance.com{path}?{qs}&signature={sig}"
    req = urllib.request.Request(url, headers={"X-MBX-APIKEY": api_key})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def fetch_binance_truth(env: dict) -> dict:
    """Live wallet + open positions + 7d realized P&L. Cached 30s."""
    global _binance_cache, _binance_cache_ts
    if time.time() - _binance_cache_ts < 30 and _binance_cache:
        return _binance_cache
    out = {"available": False, "wallet_total": 0, "wallet_avail": 0,
            "uPnL": 0, "open_positions": [], "realized_pnl_7d": 0,
            "realized_count_7d": 0, "fees_7d": 0,
            "realized_pnl_today": 0, "trades_today": 0,
            "error": None}
    try:
        acct = _binance_signed("/fapi/v2/account", {}, env)
        out["wallet_total"] = float(acct["totalWalletBalance"])
        out["wallet_avail"] = float(acct["availableBalance"])
        out["uPnL"] = float(acct["totalUnrealizedProfit"])
        positions = _binance_signed("/fapi/v2/positionRisk", {}, env)
        opens = []
        for p in positions:
            amt = float(p["positionAmt"])
            if amt == 0:
                continue
            entry = float(p["entryPrice"]); mark = float(p["markPrice"])
            opens.append({
                "symbol": p["symbol"],
                "direction": "LONG" if amt > 0 else "SHORT",
                "qty": abs(amt), "entry": entry, "mark": mark,
                "uPnL": float(p["unRealizedProfit"]),
                "leverage": int(p.get("leverage", 0)),
                "liq": float(p.get("liquidationPrice", 0)),
            })
        out["open_positions"] = opens

        end = int(time.time() * 1000)
        start7 = end - 7 * 86400 * 1000
        income = _binance_signed("/fapi/v1/income",
                                   {"startTime": start7, "endTime": end,
                                    "limit": 1000}, env)
        realized = [r for r in income if r["incomeType"] == "REALIZED_PNL"]
        fees = [r for r in income if r["incomeType"] == "COMMISSION"]
        out["realized_pnl_7d"] = round(sum(float(r["income"]) for r in realized), 4)
        out["realized_count_7d"] = len(realized)
        out["fees_7d"] = round(sum(float(r["income"]) for r in fees), 4)

        today = datetime.now(timezone.utc).date()
        today_realized = [r for r in realized
                            if datetime.fromtimestamp(int(r["time"]) / 1000,
                                                       tz=timezone.utc).date() == today]
        out["realized_pnl_today"] = round(sum(float(r["income"]) for r in today_realized), 4)
        out["trades_today"] = len(today_realized)

        out["available"] = True
    except Exception as e:
        out["error"] = str(e)
    _binance_cache = out
    _binance_cache_ts = time.time()
    return out


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


GRID_CONFIG_FILE = SCRIPT_DIR / "data" / "grid_config.json"
GRID_STATE_FILE = SCRIPT_DIR / "data" / "grid_monitor_state.json"
WALLET_HISTORY_FILE = SCRIPT_DIR / "data" / "wallet_balance_history.json"


@app.route("/api/wallet")
def api_wallet():
    """Wallet overview + delta tracking from snapshots."""
    if not WALLET_HISTORY_FILE.exists():
        return jsonify({"error": "No wallet snapshots yet", "snapshots": []})
    try:
        history = json.loads(WALLET_HISTORY_FILE.read_text())
    except Exception as e:
        return jsonify({"error": f"history parse fail: {e}"})

    snaps = history.get("snapshots", [])
    if not snaps:
        return jsonify({"error": "No snapshots", "snapshots": []})

    latest = snaps[-1]
    now_ts = datetime.fromisoformat(latest["ts"].replace("Z", "+00:00"))

    def find_snap_at(hours_ago: float) -> dict | None:
        cutoff = now_ts - timedelta(hours=hours_ago)
        for s in reversed(snaps):
            ts = datetime.fromisoformat(s["ts"].replace("Z", "+00:00"))
            if ts <= cutoff:
                return s
        return snaps[0] if snaps else None

    snap_24h = find_snap_at(24)
    snap_7d = find_snap_at(24 * 7)
    snap_first = snaps[0]

    wallet_rows = []
    icons = {"Spot": "🪙", "USDⓈ-M Futures": "📈", "COIN-M Futures": "🪙",
             "Earn": "💰", "Trading Bots": "🤖", "Cross Margin": "⚖️",
             "Isolated Margin": "⚖️", "Funding": "💵", "Options": "🎯",
             "Copy Trading": "👥"}
    for name, val in latest["wallets"].items():
        v24 = (snap_24h["wallets"].get(name) if snap_24h else val)
        v7d = (snap_7d["wallets"].get(name) if snap_7d else val)
        vfirst = snap_first["wallets"].get(name, val)
        wallet_rows.append({
            "name": name,
            "icon": icons.get(name, "  "),
            "balance": round(val, 2),
            "delta_24h": round(val - v24, 2),
            "delta_7d": round(val - v7d, 2),
            "delta_total": round(val - vfirst, 2),
        })
    wallet_rows.sort(key=lambda x: x["balance"], reverse=True)

    return jsonify({
        "latest_ts": latest["ts"],
        "first_ts": snap_first["ts"],
        "total": round(latest["total"], 2),
        "total_24h_ago": round(snap_24h["total"], 2) if snap_24h else None,
        "total_7d_ago": round(snap_7d["total"], 2) if snap_7d else None,
        "delta_24h": round(latest["total"] - (snap_24h["total"] if snap_24h else latest["total"]), 2),
        "delta_7d": round(latest["total"] - (snap_7d["total"] if snap_7d else latest["total"]), 2),
        "btc_price": latest.get("btc_price"),
        "wallets": wallet_rows,
        "snapshot_count": len(snaps),
    })


def _signed_spot_get(env: dict, path: str, params: dict | None = None):
    """Authenticated GET to api.binance.com (spot endpoints)."""
    api_key = env.get("BINANCE_API_KEY", "")
    api_secret = env.get("BINANCE_API_SECRET", "")
    if not (api_key and api_secret):
        return None, "no api credentials"
    p = dict(params or {})
    p["timestamp"] = int(time.time() * 1000)
    p["recvWindow"] = 5000
    qs = urllib.parse.urlencode(p)
    sig = hmac.new(api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f"https://api.binance.com{path}?{qs}&signature={sig}"
    req = urllib.request.Request(url, headers={"X-MBX-APIKEY": api_key})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read()), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:
        return None, str(e)


def _spot_price(symbol: str) -> float:
    try:
        url = f"{BINANCE_SPOT_PRICE_API}?symbol={symbol}"
        with urllib.request.urlopen(url, timeout=5) as r:
            return float(json.loads(r.read())["price"])
    except Exception:
        return 0.0


@app.route("/api/asi")
def api_asi():
    """AI Self-Sustainability Index: monthly profit / monthly cost."""
    try:
        import self_sustainability
        data = self_sustainability.compute_asi()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": f"ASI compute fail: {e}"}), 500


@app.route("/api/grid-bots")
def api_grid_bots():
    """Grid bot dashboard data: native Binance algo orders + tracked state."""
    env = load_env()
    config = {}
    state = {"daily_pnl": {}, "fills_by_symbol": {}}
    if GRID_CONFIG_FILE.exists():
        try:
            raw = json.loads(GRID_CONFIG_FILE.read_text())
            config = {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict)}
        except Exception:
            pass
    if GRID_STATE_FILE.exists():
        try:
            state = json.loads(GRID_STATE_FILE.read_text())
        except Exception:
            pass

    native_orders, native_err = _signed_spot_get(env, "/sapi/v1/algo/spot/openOrders")
    native_active = native_orders.get("orders", []) if native_orders else []
    native_count = len(native_active)

    spot_orders, _ = _signed_spot_get(env, "/api/v3/openOrders")
    spot_open_by_sym: dict[str, int] = {}
    if isinstance(spot_orders, list):
        for o in spot_orders:
            spot_open_by_sym[o["symbol"]] = spot_open_by_sym.get(o["symbol"], 0) + 1

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    grids = []
    earliest_started = None
    total_invest = 0.0
    total_today = 0.0
    total_cumulative = 0.0

    for symbol, cfg in config.items():
        invest = float(cfg.get("investment_usd", 0))
        total_invest += invest
        cur = _spot_price(symbol)
        lo, up = float(cfg["lower"]), float(cfg["upper"])
        sl_lo, sl_up = cfg.get("stop_lower"), cfg.get("stop_upper")
        in_range = lo <= cur <= up
        pos_pct = ((cur - lo) / (up - lo) * 100) if up > lo else 50

        status_label = "HEALTHY"
        if sl_lo and cur <= float(sl_lo) * 1.03:
            status_label = "NEAR_STOP"
        elif sl_up and cur >= float(sl_up) * 0.97:
            status_label = "NEAR_STOP"
        elif not in_range:
            status_label = "OUT_OF_RANGE"
        elif pos_pct < 10 or pos_pct > 90:
            status_label = "EDGE_WARN"

        sym_state = state.get("fills_by_symbol", {}).get(symbol, {})
        all_fills = sym_state.get("trades", [])
        n_total = len(all_fills)
        today_fills = sum(1 for t in all_fills if t["ts"][:10] == today)
        yday_fills = sum(1 for t in all_fills if t["ts"][:10] == yesterday)

        pnl_today = float(state.get("daily_pnl", {}).get(today, {}).get(symbol, 0))
        pnl_yday = float(state.get("daily_pnl", {}).get(yesterday, {}).get(symbol, 0))
        cumulative = sum(
            float(state.get("daily_pnl", {}).get(d, {}).get(symbol, 0))
            for d in state.get("daily_pnl", {}).keys()
        )
        total_today += pnl_today
        total_cumulative += cumulative

        started_at = cfg.get("started_at", "")
        if started_at and (earliest_started is None or started_at < earliest_started):
            earliest_started = started_at

        grids.append({
            "symbol": symbol,
            "lower": lo, "upper": up,
            "stop_lower": float(sl_lo) if sl_lo else None,
            "stop_upper": float(sl_up) if sl_up else None,
            "current_price": round(cur, 6),
            "position_pct": round(pos_pct, 1),
            "in_range": in_range,
            "status": status_label,
            "investment_usd": invest,
            "grids_count": cfg.get("grids", 0),
            "started_at": started_at,
            "fills_total": n_total,
            "fills_today": today_fills,
            "fills_yesterday": yday_fills,
            "pnl_today": round(pnl_today, 4),
            "pnl_yesterday": round(pnl_yday, 4),
            "pnl_cumulative": round(cumulative, 4),
            "open_spot_orders": spot_open_by_sym.get(symbol, 0),
            "native_grid_active": any(o.get("symbol") == symbol for o in native_active),
        })

    days_active = 0
    if earliest_started:
        try:
            d0 = datetime.fromisoformat(earliest_started.replace("Z", "+00:00"))
            days_active = max(1, (datetime.now(timezone.utc) - d0).days + 1)
        except Exception:
            days_active = 1

    daily_avg = (total_cumulative / days_active) if days_active else 0
    monthly_proj = daily_avg * 30
    roi_pct = (total_cumulative / total_invest * 100) if total_invest else 0

    return jsonify({
        "summary": {
            "active_grids": native_count,
            "tracked_grids": len(config),
            "total_investment": round(total_invest, 2),
            "pnl_today": round(total_today, 4),
            "pnl_cumulative": round(total_cumulative, 4),
            "roi_pct": round(roi_pct, 2),
            "days_active": days_active,
            "daily_avg": round(daily_avg, 4),
            "monthly_projection": round(monthly_proj, 2),
            "native_api_ok": native_err is None,
            "native_api_error": native_err,
        },
        "grids": grids,
        "last_update": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/llm")
def api_llm():
    """LLM performance + RAG impact stats (Phase 1 + 3)."""
    if not decision_logger:
        return jsonify({"error": "decision_logger unavailable"}), 503

    from datetime import datetime as _dt, timedelta as _td
    since_30d = (_dt.utcnow() - _td(days=30)).isoformat() + "+00:00"

    live_stats = decision_logger.trade_pnl_stats(since=since_30d, is_shadow=False)
    shadow_stats = decision_logger.trade_pnl_stats(since=since_30d, is_shadow=True)
    accuracy = decision_logger.llm_accuracy_stats(since=since_30d)
    calibration = decision_logger.confidence_calibration(since=since_30d)

    recent = decision_logger.query_decisions(limit=15)
    recent_clean = []
    for r in recent:
        recent_clean.append({
            "id": r["id"],
            "ts": r["ts"],
            "coin": r["coin"],
            "direction": r["direction"],
            "source": r["source"],
            "decision": r["decision"],
            "reason": (r["reason"] or "")[:200],
            "confidence": r["confidence"],
            "rag_used": r.get("rag_used"),
            "tokens_in": r["tokens_in"],
            "tokens_out": r["tokens_out"],
            "cost_usd": r["cost_usd"],
        })

    cost_total = 0
    with decision_logger._conn() as c:
        row = c.execute(
            "SELECT SUM(cost_usd) AS total, COUNT(*) AS n FROM llm_decisions WHERE ts >= ?",
            (since_30d,),
        ).fetchone()
        if row:
            cost_total = row["total"] or 0
            n_calls = row["n"] or 0
        else:
            n_calls = 0

        rag = c.execute(
            """SELECT
                  SUM(CASE WHEN d.rag_used=1 THEN 1 ELSE 0 END) AS with_rag,
                  SUM(CASE WHEN d.rag_used=0 THEN 1 ELSE 0 END) AS no_rag
               FROM llm_decisions d WHERE d.ts >= ?""",
            (since_30d,),
        ).fetchone()
        with_rag = (rag["with_rag"] or 0) if rag else 0
        no_rag = (rag["no_rag"] or 0) if rag else 0

        rag_outcome = c.execute(
            """SELECT d.rag_used,
                      SUM(CASE WHEN t.pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                      COUNT(t.id) AS n
               FROM llm_decisions d JOIN trades t ON t.id = d.trade_id
               WHERE t.closed_at IS NOT NULL AND t.is_shadow=0 AND d.ts >= ?
               GROUP BY d.rag_used""",
            (since_30d,),
        ).fetchall()

    rag_compare = {"with_rag": {"n": 0, "wins": 0}, "without_rag": {"n": 0, "wins": 0}}
    for row in rag_outcome:
        bucket = "with_rag" if row["rag_used"] else "without_rag"
        rag_compare[bucket] = {"n": row["n"], "wins": row["wins"] or 0}
    for k in rag_compare:
        n = rag_compare[k]["n"]
        rag_compare[k]["win_rate"] = round(rag_compare[k]["wins"] / n * 100, 1) if n else 0

    return jsonify({
        "since": since_30d,
        "live_stats": live_stats,
        "shadow_stats": shadow_stats,
        "accuracy_by_source": accuracy,
        "confidence_calibration": calibration,
        "cost": {"total_usd_30d": round(cost_total, 4), "calls_30d": n_calls},
        "rag": {
            "calls_with_rag": with_rag,
            "calls_without_rag": no_rag,
            "outcome_compare": rag_compare,
        },
        "recent_decisions": recent_clean,
    })


@app.route("/api/explain/<int:decision_id>")
def api_explain(decision_id):
    if not decision_logger:
        return jsonify({"error": "decision_logger unavailable"}), 503
    d = decision_logger.get_decision(decision_id)
    if not d:
        return jsonify({"error": "not found"}), 404
    out = {"decision": d}
    if d.get("trade_id"):
        out["trade"] = decision_logger.get_trade(d["trade_id"])
    return jsonify(out)


@app.route("/api/dashboard")
def api_dashboard():
    trading_state = load_json(TRADING_STATE_FILE)
    executor_state = load_json(EXECUTOR_STATE_FILE)
    pending = load_json(PENDING_SIGNAL_FILE)
    signal_log = get_signal_log()
    env = load_env()
    states = trading_state.get("states", {})

    SYMBOL_MAP = {
        "pepe": "1000PEPEUSDT",
        "shib": "1000SHIBUSDT",
    }

    symbols = []
    sym_map = {}
    for coin, s in states.items():
        sym = s.get("binance_symbol") or SYMBOL_MAP.get(coin, coin.upper() + "USDT")
        symbols.append(sym)
        sym_map[sym] = coin

    prices = fetch_prices(symbols) if symbols else {}

    active_positions = []
    watching = []
    for coin, s in sorted(states.items()):
        sym = s.get("binance_symbol") or SYMBOL_MAP.get(coin, coin.upper() + "USDT")
        price = prices.get(sym, 0)
        entry = s.get("fill_price") or s.get("entry_price", 0)
        direction = s.get("direction", "")

        pnl = 0
        pnl_pct = 0
        if entry and price and direction:
            if direction == "LONG":
                pnl_pct = (price - entry) / entry * 100
            else:
                pnl_pct = (entry - price) / entry * 100
            qty = s.get("fill_qty", 0)
            pnl = pnl_pct / 100 * entry * qty if qty else 0

        item = {
            "coin": coin.upper(),
            "state": s.get("state", "WATCHING"),
            "direction": direction,
            "entry": entry,
            "sl": s.get("sl_price", 0),
            "tp": s.get("tp_price", 0),
            "price": price,
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 2),
            "order_id": s.get("order_id", ""),
            "fill_qty": s.get("fill_qty", 0),
            "signal_strength": s.get("signal_strength", ""),
        }

        if s.get("state") == "ACTIVE":
            active_positions.append(item)
        else:
            watching.append(item)

    trade_history = executor_state.get("trade_history", [])

    wins = [t for t in trade_history if t.get("result") == "TP_HIT"]
    losses = [t for t in trade_history if t.get("result") == "SL_HIT"]
    total_trades = len(trade_history)
    win_rate = len(wins) / total_trades * 100 if total_trades else 0
    avg_win = sum(t.get("pnl", 0) for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t.get("pnl", 0) for t in losses) / len(losses) if losses else 0
    expectancy = (win_rate / 100 * avg_win + (1 - win_rate / 100) * avg_loss) if total_trades else 0

    equity_curve = []
    running_pnl = 0
    for t in trade_history:
        running_pnl += t.get("pnl", 0)
        equity_curve.append({
            "time": t.get("time", ""),
            "pnl": round(running_pnl, 4),
            "trade_pnl": round(t.get("pnl", 0), 4),
            "coin": t.get("coin", "").upper(),
            "direction": t.get("direction", ""),
            "result": t.get("result", ""),
        })

    coin_stats = {}
    for t in trade_history:
        c = t.get("coin", "unknown").upper()
        if c not in coin_stats:
            coin_stats[c] = {"wins": 0, "losses": 0, "total_pnl": 0}
        if t.get("result") == "TP_HIT":
            coin_stats[c]["wins"] += 1
        else:
            coin_stats[c]["losses"] += 1
        coin_stats[c]["total_pnl"] += t.get("pnl", 0)

    coin_perf = [{"coin": c, **s, "total_pnl": round(s["total_pnl"], 4)} for c, s in sorted(coin_stats.items(), key=lambda x: x[1]["total_pnl"], reverse=True)]

    balance = float(env.get("PORTFOLIO_BALANCE", "100"))
    auto_trade = env.get("AUTO_TRADE_ENABLED", "false").lower() in ("true", "1", "yes")
    leverage = int(env.get("FUTURES_LEVERAGE", "5"))
    daily_limit = float(env.get("DAILY_LOSS_LIMIT", "10"))

    pending_info = None
    if pending and pending.get("status") in ("pending_review", "auto_confirmed"):
        pending_info = {
            "coin": pending.get("coin", "").upper(),
            "direction": pending.get("direction", ""),
            "entry": pending.get("entry", 0),
            "strength": pending.get("strength", ""),
            "status": pending.get("status", ""),
            "timestamp": pending.get("timestamp", ""),
        }

    binance = fetch_binance_truth(env)
    starting = float(executor_state.get("starting_balance") or balance or 0)
    real_balance = binance["wallet_total"] if binance["available"] else balance
    realized_total = (real_balance - starting) if binance["available"] else \
        round(executor_state.get("total_pnl", 0), 4)
    state_total_pnl = round(executor_state.get("total_pnl", 0), 4)
    drift = round(realized_total - state_total_pnl, 4) if binance["available"] else 0

    return jsonify({
        "portfolio": {
            "balance": round(real_balance, 4),
            "balance_source": "binance" if binance["available"] else "env",
            "starting_balance": round(starting, 4),
            "leverage": leverage,
            "daily_loss_limit": daily_limit,
            "auto_trade": auto_trade,
            "daily_pnl": round(executor_state.get("daily_pnl", 0), 4),
            "binance_realized_today": binance.get("realized_pnl_today", 0),
            "binance_trades_today": binance.get("trades_today", 0),
            "binance_realized_7d": binance.get("realized_pnl_7d", 0),
            "binance_fees_7d": binance.get("fees_7d", 0),
            "binance_trades_7d": binance.get("realized_count_7d", 0),
            "total_pnl": state_total_pnl,
            "real_total_pnl": round(realized_total, 4),
            "state_drift_usd": drift,
            "uPnL": binance.get("uPnL", 0),
            "open_on_exchange": len(binance.get("open_positions", [])),
            "exchange_positions": binance.get("open_positions", []),
            "binance_error": binance.get("error"),
            "total_trades": executor_state.get("total_trades", 0),
            "consecutive_losses": executor_state.get("consecutive_losses", 0),
            "paused_until": executor_state.get("paused_until"),
        },
        "stats": {
            "total_trades": total_trades,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 1),
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "expectancy": round(expectancy, 4),
            "best_trade": round(max((t.get("pnl", 0) for t in trade_history), default=0), 4),
            "worst_trade": round(min((t.get("pnl", 0) for t in trade_history), default=0), 4),
        },
        "active_positions": active_positions,
        "watching_count": len(watching),
        "pending_signal": pending_info,
        "trade_history": list(reversed(trade_history[-50:])),
        "equity_curve": equity_curve,
        "coin_performance": coin_perf,
        "last_update": datetime.now(timezone.utc).isoformat(),
    })


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PicoTrader Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace; background: #0a0e17; color: #e1e5ee; font-size: 14px; }
.container { max-width: 1400px; margin: 0 auto; padding: 16px; }
header { display: flex; justify-content: space-between; align-items: center; padding: 12px 0; border-bottom: 1px solid #1e2a3a; margin-bottom: 16px; }
header h1 { font-size: 20px; color: #00d4aa; font-weight: 600; }
.status-badge { padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: 600; }
.badge-live { background: #1a3a2a; color: #00d4aa; }
.badge-paused { background: #3a2a1a; color: #ffaa00; }
.badge-auto { background: #1a2a3a; color: #00aaff; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; margin-bottom: 16px; }
.card { background: #111827; border: 1px solid #1e2a3a; border-radius: 8px; padding: 16px; }
.card-title { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: #6b7280; margin-bottom: 8px; }
.card-value { font-size: 28px; font-weight: 700; }
.card-sub { font-size: 12px; color: #6b7280; margin-top: 4px; }
.positive { color: #00d4aa; }
.negative { color: #ff4757; }
.neutral { color: #6b7280; }
.chart-container { background: #111827; border: 1px solid #1e2a3a; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
.chart-container h2 { font-size: 14px; color: #9ca3af; margin-bottom: 12px; }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; padding: 8px 12px; font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: #6b7280; border-bottom: 1px solid #1e2a3a; }
td { padding: 8px 12px; border-bottom: 1px solid #0d1117; font-size: 13px; }
tr:hover { background: #151d2b; }
.dir-long { color: #00d4aa; }
.dir-short { color: #ff4757; }
.tp-hit { color: #00d4aa; background: #0a2a1a; padding: 2px 8px; border-radius: 4px; font-size: 11px; }
.sl-hit { color: #ff4757; background: #2a0a0a; padding: 2px 8px; border-radius: 4px; font-size: 11px; }
.signal-pending { color: #ffaa00; background: #2a2a0a; padding: 2px 8px; border-radius: 4px; font-size: 11px; }
.coin-tag { background: #1e2a3a; padding: 2px 8px; border-radius: 4px; font-weight: 600; font-size: 12px; }
.progress-bar { height: 6px; background: #1e2a3a; border-radius: 3px; overflow: hidden; margin-top: 6px; }
.progress-fill { height: 100%; border-radius: 3px; transition: width 0.5s; }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px; }
@media (max-width: 768px) { .two-col { grid-template-columns: 1fr; } }
.refresh-timer { font-size: 11px; color: #4b5563; }
.positions-section { margin-bottom: 16px; }
.no-data { text-align: center; padding: 40px; color: #4b5563; }
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>PicoTrader</h1>
    <div>
      <span id="modeBadge" class="status-badge badge-auto">AUTO</span>
      <span id="statusBadge" class="status-badge badge-live">LIVE</span>
      <span class="refresh-timer" id="timer">--</span>
    </div>
  </header>

  <div class="grid" id="statsGrid"></div>

  <div id="pendingAlert" style="display:none; background:#2a2a0a; border:1px solid #554400; border-radius:8px; padding:12px 16px; margin-bottom:16px;">
    <span style="color:#ffaa00; font-weight:600;">PENDING SIGNAL</span>
    <span id="pendingText"></span>
  </div>

  <div class="positions-section">
    <div class="card">
      <div class="card-title">Active Positions</div>
      <table>
        <thead><tr><th>Coin</th><th>Dir</th><th>Entry</th><th>Price</th><th>SL</th><th>TP</th><th>P&L</th><th>Qty</th></tr></thead>
        <tbody id="positionsTable"></tbody>
      </table>
      <div class="no-data" id="noPositions" style="display:none;">No active positions</div>
    </div>
  </div>

  <div class="two-col">
    <div class="chart-container">
      <h2>Equity Curve</h2>
      <canvas id="equityChart" height="200"></canvas>
      <div class="no-data" id="noEquity" style="display:none;">No trades yet</div>
    </div>
    <div class="chart-container">
      <h2>Performance by Coin</h2>
      <canvas id="coinChart" height="200"></canvas>
      <div class="no-data" id="noCoinData" style="display:none;">No trades yet</div>
    </div>
  </div>

  <div class="card" style="margin-bottom: 16px; border-left: 4px solid #f59e0b;">
    <div class="card-title">
      AI Self-Sustainability Index
      <span id="asiBadge" style="font-size:14px; padding:3px 10px; border-radius:4px; margin-left:8px; font-weight:700;"></span>
      <span id="asiNet" style="font-size:12px; margin-left:8px; color:#9ca3af;"></span>
    </div>
    <div class="grid" id="asiGrid" style="margin-bottom:12px;"></div>
    <div style="display:grid; grid-template-columns:1fr 1fr; gap:16px;">
      <div>
        <div style="font-size:12px; color:#9ca3af; margin-bottom:6px;">PROFIT (monthly run-rate)</div>
        <table style="width:100%; font-size:13px;">
          <tbody id="asiProfitTable"></tbody>
        </table>
      </div>
      <div>
        <div style="font-size:12px; color:#9ca3af; margin-bottom:6px;">COST (monthly)</div>
        <table style="width:100%; font-size:13px;">
          <tbody id="asiCostTable"></tbody>
        </table>
      </div>
    </div>
    <div class="card-sub" id="asiMeta" style="margin-top:10px; font-size:11px; color:#6b7280;"></div>
  </div>

  <div class="card" style="margin-bottom: 16px;">
    <div class="card-title">
      Wallet Overview
      <span id="walletTotal" style="font-size:14px; color:#10b981; margin-left:8px;"></span>
      <span id="walletDelta24h" style="font-size:12px; margin-left:8px;"></span>
    </div>
    <table>
      <thead><tr><th>Wallet</th><th>Balance</th><th>24h Δ</th><th>7d Δ</th><th>Total Δ</th></tr></thead>
      <tbody id="walletTable"></tbody>
    </table>
    <div class="card-sub" id="walletMeta" style="margin-top:8px;"></div>
  </div>

  <div class="card" style="margin-bottom: 16px;">
    <div class="card-title">
      Spot Grid Bots
      <span id="gridApiBadge" style="font-size:10px; padding:2px 6px; border-radius:3px; margin-left:8px;"></span>
    </div>
    <div class="grid" id="gridSummaryGrid" style="margin-bottom:12px;"></div>
    <table>
      <thead><tr><th>Symbol</th><th>Range</th><th>Price</th><th>Position</th><th>Status</th><th>Fills (today/total)</th><th>P&L Today</th><th>P&L Cumulative</th><th>Invest</th></tr></thead>
      <tbody id="gridTable"></tbody>
    </table>
    <div class="no-data" id="noGrids" style="display:none; padding:20px;">
      No grid bots configured.<br/>
      <span style="font-size:11px; color:#6b7280;">Setup grids on Binance UI or via API → populate <code>data/grid_config.json</code></span>
    </div>
  </div>

  <div class="card" style="margin-bottom: 16px;">
    <div class="card-title">Trade History</div>
    <table>
      <thead><tr><th>Time</th><th>Coin</th><th>Dir</th><th>Entry</th><th>Close</th><th>P&L</th><th>Result</th></tr></thead>
      <tbody id="historyTable"></tbody>
    </table>
    <div class="no-data" id="noHistory" style="display:none;">No trades yet — signals are being monitored</div>
  </div>

  <div class="card" style="margin-bottom: 16px;">
    <div class="card-title">LLM Performance (last 30d)</div>
    <div class="grid" id="llmStatsGrid"></div>
    <div class="two-col" style="margin-top:12px;">
      <div>
        <h2 style="font-size: 13px; color: #9ca3af; margin-bottom: 8px;">Confidence Calibration</h2>
        <table>
          <thead><tr><th>Bucket</th><th>N</th><th>Win Rate</th><th>Avg P&L</th></tr></thead>
          <tbody id="calibTable"></tbody>
        </table>
      </div>
      <div>
        <h2 style="font-size: 13px; color: #9ca3af; margin-bottom: 8px;">RAG Impact</h2>
        <table>
          <thead><tr><th>Variant</th><th>Trades</th><th>Win Rate</th></tr></thead>
          <tbody id="ragTable"></tbody>
        </table>
        <div class="card-sub" style="margin-top:8px;">
          Calls with RAG: <span id="ragWithCount">0</span> | without: <span id="ragWithoutCount">0</span>
        </div>
      </div>
    </div>
    <h2 style="font-size: 13px; color: #9ca3af; margin: 16px 0 8px 0;">Recent LLM Decisions</h2>
    <table>
      <thead><tr><th>ID</th><th>Time</th><th>Source</th><th>Coin</th><th>Decision</th><th>Conf</th><th>Reason</th><th>RAG</th></tr></thead>
      <tbody id="llmRecentTable"></tbody>
    </table>
    <div class="no-data" id="noLlm" style="display:none;">No LLM decisions logged yet</div>
  </div>
</div>

<script>
let equityChartInstance = null;
let coinChartInstance = null;

function fmt(val) {
  if (Math.abs(val) >= 100) return '$' + val.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
  if (Math.abs(val) >= 1) return '$' + val.toFixed(4);
  if (Math.abs(val) >= 0.01) return '$' + val.toFixed(6);
  return '$' + val.toFixed(8);
}

function pnlClass(v) { return v > 0 ? 'positive' : v < 0 ? 'negative' : 'neutral'; }

function update() {
  fetch('/api/dashboard')
    .then(r => r.json())
    .then(d => render(d))
    .catch(e => console.error('Fetch error:', e));
}

function render(d) {
  const p = d.portfolio;
  const s = d.stats;

  document.getElementById('modeBadge').textContent = p.auto_trade ? 'AUTO' : 'MANUAL';
  document.getElementById('modeBadge').className = 'status-badge ' + (p.auto_trade ? 'badge-auto' : 'badge-paused');

  const paused = p.paused_until || p.consecutive_losses >= 3;
  document.getElementById('statusBadge').textContent = paused ? 'PAUSED' : 'LIVE';
  document.getElementById('statusBadge').className = 'status-badge ' + (paused ? 'badge-paused' : 'badge-live');

  const realPnl = (p.real_total_pnl != null) ? p.real_total_pnl : p.total_pnl;
  const driftBadge = (p.state_drift_usd && Math.abs(p.state_drift_usd) > 0.5)
      ? `<span class="negative" style="margin-left:6px">⚠ drift $${p.state_drift_usd.toFixed(2)}</span>` : '';
  const balSrc = p.balance_source === 'binance' ? 'BINANCE LIVE' : 'env';

  document.getElementById('statsGrid').innerHTML = `
    <div class="card">
      <div class="card-title">Wallet (${balSrc})</div>
      <div class="card-value">$${p.balance.toFixed(2)}</div>
      <div class="card-sub">Start: $${p.starting_balance.toFixed(2)} | ${p.leverage}x | Daily limit: -$${p.daily_loss_limit.toFixed(0)}</div>
    </div>
    <div class="card">
      <div class="card-title">Realized P&L (Binance)</div>
      <div class="card-value ${pnlClass(realPnl)}">$${realPnl >= 0 ? '+' : ''}${realPnl.toFixed(2)}</div>
      <div class="card-sub">Today: <span class="${pnlClass(p.binance_realized_today)}">$${p.binance_realized_today >= 0 ? '+' : ''}${p.binance_realized_today.toFixed(2)}</span> (${p.binance_trades_today} trades) | 7d: $${p.binance_realized_7d.toFixed(2)} (${p.binance_trades_7d}) ${driftBadge}</div>
    </div>
    <div class="card">
      <div class="card-title">Win Rate</div>
      <div class="card-value">${s.total_trades ? s.win_rate.toFixed(1) + '%' : '--'}</div>
      <div class="card-sub">${s.wins}W / ${s.losses}L | Expectancy: $${s.expectancy.toFixed(2)} | Fees 7d: $${p.binance_fees_7d.toFixed(2)}</div>
      <div class="progress-bar"><div class="progress-fill" style="width:${s.win_rate}%; background:${s.win_rate >= 50 ? '#00d4aa' : s.win_rate >= 40 ? '#ffaa00' : '#ff4757'};"></div></div>
    </div>
    <div class="card">
      <div class="card-title">Avg Win / Loss</div>
      <div class="card-value positive">+$${s.avg_win.toFixed(2)}</div>
      <div class="card-sub">Avg loss: <span class="negative">$${s.avg_loss.toFixed(2)}</span> | Best: $${s.best_trade.toFixed(2)} | Worst: $${s.worst_trade.toFixed(2)}</div>
    </div>
    <div class="card">
      <div class="card-title">Open on Binance</div>
      <div class="card-value">${p.open_on_exchange}</div>
      <div class="card-sub">uPnL: <span class="${pnlClass(p.uPnL)}">$${p.uPnL >= 0 ? '+' : ''}${p.uPnL.toFixed(2)}</span> | State says: ${d.active_positions.length} | Streak: ${p.consecutive_losses}/3</div>
    </div>
  `;

  // Pending signal
  const pa = document.getElementById('pendingAlert');
  if (d.pending_signal) {
    pa.style.display = 'block';
    const ps = d.pending_signal;
    document.getElementById('pendingText').innerHTML = ` — <span class="coin-tag">${ps.coin}</span> <span class="${ps.direction === 'LONG' ? 'dir-long' : 'dir-short'}">${ps.direction}</span> @ ${fmt(ps.entry)} (${ps.strength}) <span class="signal-pending">${ps.status}</span>`;
  } else {
    pa.style.display = 'none';
  }

  // Positions
  const tbody = document.getElementById('positionsTable');
  const noPosEl = document.getElementById('noPositions');
  if (d.active_positions.length === 0) {
    tbody.innerHTML = '';
    noPosEl.style.display = 'block';
  } else {
    noPosEl.style.display = 'none';
    tbody.innerHTML = d.active_positions.map(p => `
      <tr>
        <td><span class="coin-tag">${p.coin}</span></td>
        <td class="${p.direction === 'LONG' ? 'dir-long' : 'dir-short'}">${p.direction}</td>
        <td>${fmt(p.entry)}</td>
        <td>${fmt(p.price)}</td>
        <td>${fmt(p.sl)}</td>
        <td>${fmt(p.tp)}</td>
        <td class="${pnlClass(p.pnl_pct)}">${p.pnl_pct >= 0 ? '+' : ''}${p.pnl_pct.toFixed(2)}%</td>
        <td>${p.fill_qty ? p.fill_qty.toFixed(6) : '--'}</td>
      </tr>
    `).join('');
  }

  // Equity chart
  const ec = d.equity_curve;
  if (ec.length === 0) {
    document.getElementById('equityChart').style.display = 'none';
    document.getElementById('noEquity').style.display = 'block';
  } else {
    document.getElementById('equityChart').style.display = 'block';
    document.getElementById('noEquity').style.display = 'none';
    const labels = ec.map(e => e.time ? e.time.slice(5, 16).replace('T', ' ') : '');
    const data = ec.map(e => e.pnl);
    if (equityChartInstance) equityChartInstance.destroy();
    equityChartInstance = new Chart(document.getElementById('equityChart'), {
      type: 'line',
      data: {
        labels: labels,
        datasets: [{
          label: 'Cumulative P&L ($)',
          data: data,
          borderColor: data[data.length-1] >= 0 ? '#00d4aa' : '#ff4757',
          backgroundColor: (data[data.length-1] >= 0 ? 'rgba(0,212,170,' : 'rgba(255,71,87,') + '0.1)',
          fill: true,
          tension: 0.3,
          pointRadius: 3,
          pointBackgroundColor: ec.map(e => e.result === 'TP_HIT' ? '#00d4aa' : '#ff4757'),
        }]
      },
      options: {
        responsive: true,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: (items) => ec[items[0].dataIndex]?.time?.slice(0,16) || '',
              label: (item) => {
                const e = ec[item.dataIndex];
                return `${e.coin} ${e.direction} | Trade: $${e.trade_pnl.toFixed(2)} | Total: $${e.pnl.toFixed(2)}`;
              }
            }
          }
        },
        scales: {
          x: { ticks: { color: '#4b5563', maxTicksLimit: 10 }, grid: { color: '#1e2a3a' } },
          y: { ticks: { color: '#4b5563', callback: v => '$' + v.toFixed(2) }, grid: { color: '#1e2a3a' } }
        }
      }
    });
  }

  // Coin performance chart
  const cp = d.coin_performance;
  if (cp.length === 0) {
    document.getElementById('coinChart').style.display = 'none';
    document.getElementById('noCoinData').style.display = 'block';
  } else {
    document.getElementById('coinChart').style.display = 'block';
    document.getElementById('noCoinData').style.display = 'none';
    if (coinChartInstance) coinChartInstance.destroy();
    coinChartInstance = new Chart(document.getElementById('coinChart'), {
      type: 'bar',
      data: {
        labels: cp.map(c => c.coin),
        datasets: [
          { label: 'Wins', data: cp.map(c => c.wins), backgroundColor: '#00d4aa' },
          { label: 'Losses', data: cp.map(c => -c.losses), backgroundColor: '#ff4757' },
        ]
      },
      options: {
        responsive: true,
        plugins: { legend: { labels: { color: '#9ca3af' } } },
        scales: {
          x: { ticks: { color: '#4b5563' }, grid: { color: '#1e2a3a' }, stacked: true },
          y: { ticks: { color: '#4b5563' }, grid: { color: '#1e2a3a' }, stacked: true }
        }
      }
    });
  }

  // Trade history
  const hTbody = document.getElementById('historyTable');
  const noHist = document.getElementById('noHistory');
  if (d.trade_history.length === 0) {
    hTbody.innerHTML = '';
    noHist.style.display = 'block';
  } else {
    noHist.style.display = 'none';
    hTbody.innerHTML = d.trade_history.map(t => `
      <tr>
        <td>${t.time ? t.time.slice(0, 16).replace('T', ' ') : '--'}</td>
        <td><span class="coin-tag">${(t.coin || '').toUpperCase()}</span></td>
        <td class="${t.direction === 'LONG' ? 'dir-long' : 'dir-short'}">${t.direction}</td>
        <td>${fmt(t.entry || 0)}</td>
        <td>${fmt(t.close || 0)}</td>
        <td class="${pnlClass(t.pnl)}">$${t.pnl >= 0 ? '+' : ''}${(t.pnl || 0).toFixed(2)}</td>
        <td><span class="${t.result === 'TP_HIT' ? 'tp-hit' : 'sl-hit'}">${t.result}</span></td>
      </tr>
    `).join('');
  }

  document.getElementById('timer').textContent = 'Updated ' + new Date().toLocaleTimeString();
}

function renderLlm(d) {
  const live = d.live_stats || {};
  const acc = d.accuracy_by_source || {};
  const cost = d.cost || {};
  const sigAcc = acc.signal_review || {};
  const tpRate = (sigAcc.with_outcome ? (sigAcc.tp / sigAcc.with_outcome * 100) : 0).toFixed(1);
  const slRate = (sigAcc.with_outcome ? (sigAcc.sl / sigAcc.with_outcome * 100) : 0).toFixed(1);

  document.getElementById('llmStatsGrid').innerHTML = `
    <div class="card">
      <div class="card-title">LLM Calls (30d)</div>
      <div class="card-value">${cost.calls_30d || 0}</div>
      <div class="card-sub">Cost: $${(cost.total_usd_30d || 0).toFixed(3)}</div>
    </div>
    <div class="card">
      <div class="card-title">Signal Confirms</div>
      <div class="card-value">${sigAcc.by_decision?.CONFIRM || 0}</div>
      <div class="card-sub">Rejects: ${sigAcc.by_decision?.REJECT || 0}</div>
    </div>
    <div class="card">
      <div class="card-title">CONFIRM → TP</div>
      <div class="card-value ${tpRate >= 50 ? 'positive' : 'neutral'}">${tpRate}%</div>
      <div class="card-sub">SL rate: ${slRate}% | Sample: ${sigAcc.with_outcome || 0}</div>
    </div>
    <div class="card">
      <div class="card-title">Trade WR (30d)</div>
      <div class="card-value">${(live.win_rate || 0).toFixed(1)}%</div>
      <div class="card-sub">${live.wins || 0}W/${live.losses || 0}L | R: ${(live.total_r || 0).toFixed(2)}</div>
    </div>
  `;

  const calibTable = document.getElementById('calibTable');
  calibTable.innerHTML = (d.confidence_calibration || []).map(c => `
    <tr>
      <td>${c.bucket}</td>
      <td>${c.n}</td>
      <td>${c.win_rate === null ? '--' : c.win_rate.toFixed(1) + '%'}</td>
      <td>${c.avg_pnl === null ? '--' : '$' + c.avg_pnl.toFixed(2)}</td>
    </tr>
  `).join('');

  const ragOC = d.rag?.outcome_compare || {};
  document.getElementById('ragTable').innerHTML = `
    <tr>
      <td>With RAG</td>
      <td>${ragOC.with_rag?.n || 0}</td>
      <td class="${ragOC.with_rag?.win_rate >= 50 ? 'positive' : 'neutral'}">${(ragOC.with_rag?.win_rate || 0).toFixed(1)}%</td>
    </tr>
    <tr>
      <td>Without RAG</td>
      <td>${ragOC.without_rag?.n || 0}</td>
      <td class="${ragOC.without_rag?.win_rate >= 50 ? 'positive' : 'neutral'}">${(ragOC.without_rag?.win_rate || 0).toFixed(1)}%</td>
    </tr>
  `;
  document.getElementById('ragWithCount').textContent = d.rag?.calls_with_rag || 0;
  document.getElementById('ragWithoutCount').textContent = d.rag?.calls_without_rag || 0;

  const recent = d.recent_decisions || [];
  const llmTbody = document.getElementById('llmRecentTable');
  const noLlm = document.getElementById('noLlm');
  if (recent.length === 0) {
    llmTbody.innerHTML = '';
    noLlm.style.display = 'block';
  } else {
    noLlm.style.display = 'none';
    llmTbody.innerHTML = recent.map(r => `
      <tr>
        <td>${r.id}</td>
        <td>${(r.ts || '').slice(5, 16).replace('T', ' ')}</td>
        <td>${r.source}</td>
        <td>${r.coin ? `<span class="coin-tag">${r.coin.toUpperCase()}</span>` : '--'}</td>
        <td><span class="${r.decision === 'CONFIRM' ? 'tp-hit' : (r.decision === 'REJECT' ? 'sl-hit' : 'signal-pending')}">${r.decision}</span></td>
        <td>${r.confidence ?? '--'}</td>
        <td style="max-width:300px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${(r.reason || '').replace(/"/g, '&quot;')}">${r.reason || ''}</td>
        <td>${r.rag_used ? '✓' : ''}</td>
      </tr>
    `).join('');
  }
}

function updateLlm() {
  fetch('/api/llm')
    .then(r => r.json())
    .then(d => { if (!d.error) renderLlm(d); })
    .catch(e => console.error('LLM fetch error:', e));
}

function renderGrids(d) {
  const summary = d.summary || {};
  const grids = d.grids || [];
  const badge = document.getElementById('gridApiBadge');
  if (summary.native_api_ok) {
    badge.textContent = `${summary.active_grids} live on Binance`;
    badge.style.background = summary.active_grids > 0 ? '#0a3a1a' : '#1a1a2e';
    badge.style.color = summary.active_grids > 0 ? '#10b981' : '#9ca3af';
  } else {
    badge.textContent = 'Native API blocked';
    badge.style.background = '#3a0a0a'; badge.style.color = '#ef4444';
  }

  const sumGrid = document.getElementById('gridSummaryGrid');
  if (grids.length === 0 && summary.active_grids === 0) {
    sumGrid.innerHTML = '';
  } else {
    const pTodayCls = summary.pnl_today >= 0 ? 'positive' : 'negative';
    const pCumCls = summary.pnl_cumulative >= 0 ? 'positive' : 'negative';
    sumGrid.innerHTML = `
      <div class="card"><div class="card-title">Tracked Grids</div>
        <div class="card-value">${summary.tracked_grids}</div>
        <div class="card-sub">${summary.active_grids} active on Binance</div></div>
      <div class="card"><div class="card-title">Investment</div>
        <div class="card-value">$${summary.total_investment.toFixed(0)}</div>
        <div class="card-sub">${summary.days_active}d running</div></div>
      <div class="card"><div class="card-title">P&L Today</div>
        <div class="card-value ${pTodayCls}">${summary.pnl_today >= 0 ? '+' : ''}$${summary.pnl_today.toFixed(2)}</div>
        <div class="card-sub">Daily avg: $${summary.daily_avg.toFixed(2)}</div></div>
      <div class="card"><div class="card-title">Cumulative</div>
        <div class="card-value ${pCumCls}">${summary.pnl_cumulative >= 0 ? '+' : ''}$${summary.pnl_cumulative.toFixed(2)}</div>
        <div class="card-sub">${summary.roi_pct >= 0 ? '+' : ''}${summary.roi_pct.toFixed(2)}% ROI · proj $${summary.monthly_projection.toFixed(0)}/mo</div></div>
    `;
  }

  const tbody = document.getElementById('gridTable');
  const empty = document.getElementById('noGrids');
  if (grids.length === 0) {
    tbody.innerHTML = '';
    empty.style.display = 'block';
    return;
  }
  empty.style.display = 'none';
  tbody.innerHTML = grids.map(g => {
    const statusColors = {
      'HEALTHY': '#10b981', 'EDGE_WARN': '#f59e0b',
      'NEAR_STOP': '#ef4444', 'OUT_OF_RANGE': '#7f1d1d'
    };
    const statusBg = statusColors[g.status] || '#6b7280';
    const todayCls = g.pnl_today >= 0 ? 'positive' : 'negative';
    const cumCls = g.pnl_cumulative >= 0 ? 'positive' : 'negative';
    const liveDot = g.native_grid_active ? '<span style="color:#10b981;">●</span> ' : '<span style="color:#6b7280;">○</span> ';
    const posBar = `<div style="background:#1a1a2e; height:6px; border-radius:3px; position:relative; width:80px;">
        <div style="background:#3b82f6; height:100%; width:${Math.min(100,Math.max(0,g.position_pct))}%; border-radius:3px;"></div>
      </div><span style="font-size:10px; color:#9ca3af;">${g.position_pct.toFixed(0)}%</span>`;
    const priceFmt = g.current_price >= 100 ? g.current_price.toFixed(2)
                   : g.current_price >= 1 ? g.current_price.toFixed(4)
                   : g.current_price.toFixed(6);
    const lowerFmt = g.lower >= 100 ? g.lower.toFixed(0) : g.lower.toFixed(2);
    const upperFmt = g.upper >= 100 ? g.upper.toFixed(0) : g.upper.toFixed(2);
    return `
      <tr>
        <td>${liveDot}<strong>${g.symbol}</strong></td>
        <td>$${lowerFmt} – $${upperFmt}</td>
        <td>$${priceFmt}</td>
        <td>${posBar}</td>
        <td><span style="background:${statusBg}; color:#fff; padding:2px 8px; border-radius:4px; font-size:10px;">${g.status}</span></td>
        <td>${g.fills_today} / ${g.fills_total}</td>
        <td class="${todayCls}">${g.pnl_today >= 0 ? '+' : ''}$${g.pnl_today.toFixed(3)}</td>
        <td class="${cumCls}">${g.pnl_cumulative >= 0 ? '+' : ''}$${g.pnl_cumulative.toFixed(3)}</td>
        <td>$${g.investment_usd.toFixed(0)}</td>
      </tr>
    `;
  }).join('');
}

function updateGrids() {
  fetch('/api/grid-bots')
    .then(r => r.json())
    .then(renderGrids)
    .catch(e => console.error('Grid fetch error:', e));
}

function renderWallet(d) {
  if (d.error || !d.wallets) {
    document.getElementById('walletTable').innerHTML =
      '<tr><td colspan="5" style="text-align:center; color:#9ca3af; padding:12px;">' +
      (d.error || 'No data') + '</td></tr>';
    return;
  }
  const totalEl = document.getElementById('walletTotal');
  const dEl = document.getElementById('walletDelta24h');
  totalEl.textContent = '$' + d.total.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
  const d24cls = d.delta_24h >= 0 ? 'positive' : 'negative';
  dEl.innerHTML = '<span class="' + d24cls + '">' + (d.delta_24h >= 0 ? '+' : '') + '$' + d.delta_24h.toFixed(2) + ' (24h)</span>';
  dEl.className = '';

  const fmtDelta = (v) => {
    if (v === 0 || v === null || v === undefined) return '<span style="color:#6b7280;">—</span>';
    const cls = v >= 0 ? 'positive' : 'negative';
    return '<span class="' + cls + '">' + (v >= 0 ? '+' : '') + '$' + v.toFixed(2) + '</span>';
  };
  const tbody = document.getElementById('walletTable');
  tbody.innerHTML = d.wallets.filter(w => w.balance > 0.01 || Math.abs(w.delta_24h) > 0.01).map(w => {
    const isBots = w.name === 'Trading Bots';
    const highlight = isBots ? 'background:#0a1a2e;' : '';
    return `<tr style="${highlight}">
      <td>${w.icon} ${w.name}${isBots ? ' <span style="font-size:10px; color:#3b82f6;">[grid bots]</span>' : ''}</td>
      <td><strong>$${w.balance.toFixed(2)}</strong></td>
      <td>${fmtDelta(w.delta_24h)}</td>
      <td>${fmtDelta(w.delta_7d)}</td>
      <td>${fmtDelta(w.delta_total)}</td>
    </tr>`;
  }).join('');

  const meta = document.getElementById('walletMeta');
  const lastT = new Date(d.latest_ts);
  const ago = Math.round((Date.now() - lastT.getTime()) / 60000);
  meta.innerHTML = `Snapshot taken ${ago}m ago · ${d.snapshot_count} snapshots tracked · BTC $${(d.btc_price||0).toLocaleString()}`;
}

function updateWallet() {
  fetch('/api/wallet')
    .then(r => r.json())
    .then(renderWallet)
    .catch(e => console.error('Wallet fetch error:', e));
}

function renderAsi(d) {
  if (!d || d.error) return;
  const badge = document.getElementById('asiBadge');
  const colorMap = {
    SELF_SUSTAINING_PLUS: '#10b981',
    SURPLUS: '#22c55e',
    BREAK_EVEN: '#f59e0b',
    DEFICIT: '#ef4444',
  };
  const bg = colorMap[d.label] || '#6b7280';
  badge.style.background = bg + '22';
  badge.style.color = bg;
  badge.style.border = '1px solid ' + bg;
  badge.textContent = `${d.status} ASI ${d.asi.toFixed(2)} · ${d.label.replace(/_/g,' ')}`;

  const netEl = document.getElementById('asiNet');
  const netColor = d.net_monthly >= 0 ? '#10b981' : '#ef4444';
  const sign = d.net_monthly >= 0 ? '+' : '';
  netEl.innerHTML = `Net: <span style="color:${netColor}; font-weight:600;">${sign}$${d.net_monthly.toFixed(2)}/mo</span>`;

  const grid = document.getElementById('asiGrid');
  grid.innerHTML = `
    <div class="kpi"><div class="kpi-label">Profit/mo</div><div class="kpi-value" style="color:#10b981;">$${d.profit_monthly.toFixed(2)}</div></div>
    <div class="kpi"><div class="kpi-label">Cost/mo</div><div class="kpi-value" style="color:#ef4444;">$${d.cost_monthly.toFixed(2)}</div></div>
    <div class="kpi"><div class="kpi-label">ASI Score</div><div class="kpi-value" style="color:${bg};">${d.asi.toFixed(2)}</div></div>
    <div class="kpi"><div class="kpi-label">Status</div><div class="kpi-value" style="color:${bg}; font-size:14px;">${d.label.replace(/_/g,' ')}</div></div>
  `;

  const pb = d.profit_breakdown;
  document.getElementById('asiProfitTable').innerHTML = `
    <tr><td>Futures (OpenClaw)</td><td style="text-align:right; color:${pb.futures>=0?'#10b981':'#ef4444'};">$${pb.futures.toFixed(2)}</td></tr>
    <tr><td>Grid (Trading Bots)</td><td style="text-align:right; color:${pb.grid>=0?'#10b981':'#ef4444'};">$${pb.grid.toFixed(2)}</td></tr>
    <tr><td>Earn (Simple Earn)</td><td style="text-align:right; color:#10b981;">$${pb.earn.toFixed(4)}</td></tr>
    <tr style="border-top:1px solid #374151; font-weight:600;"><td>Total</td><td style="text-align:right; color:#10b981;">$${d.profit_monthly.toFixed(2)}</td></tr>
  `;
  const cb = d.cost_breakdown;
  document.getElementById('asiCostTable').innerHTML = `
    <tr><td>DeepSeek API</td><td style="text-align:right; color:#ef4444;">$${cb.deepseek.toFixed(2)}</td></tr>
    <tr><td>Cursor Pro</td><td style="text-align:right; color:#ef4444;">$${cb.cursor.toFixed(2)}</td></tr>
    <tr><td>Anthropic API</td><td style="text-align:right; color:#ef4444;">$${cb.anthropic.toFixed(2)}</td></tr>
    <tr style="border-top:1px solid #374151; font-weight:600;"><td>Total</td><td style="text-align:right; color:#ef4444;">$${d.cost_monthly.toFixed(2)}</td></tr>
  `;

  const f = d.details.futures;
  const g = d.details.grid;
  const c = d.details.cost.deepseek;
  let gridLine = '';
  if (g.method === 'config_anchor') {
    const warmup = g.warmup_status && g.warmup_status !== 'active' ? ` · <span style="color:#f59e0b;">${g.warmup_status}</span>` : '';
    const dDisp = g.days_float ?? g.days_active;
    gridLine = `Grid: invested $${g.invested_usd} → now $${g.current_bots_balance} (unrealized ${g.unrealized_pnl>=0?'+':''}$${g.unrealized_pnl}, ${dDisp}d, ${g.bot_count} bots)${warmup}`;
  } else if (g.method === '24h_delta') {
    gridLine = `Grid: 24h delta ${g.delta_24h>=0?'+':''}$${g.delta_24h} · balance $${g.current_bots_balance}`;
  } else {
    gridLine = `Grid: ${g.note} · balance $${g.current_bots_balance}`;
  }
  document.getElementById('asiMeta').innerHTML = `
    Futures: ${f.days}d tracking · total ${f.total_pnl>=0?'+':''}$${f.total_pnl} · since ${f.tracking_since}<br/>
    ${gridLine}<br/>
    DeepSeek: $${c.cumulative_usd} cumulative over ${c.tracking_days}d · balance $${c.balance_remaining} · daily $${c.daily_usd}<br/>
    <em>Target: ASI ≥ 2.0 = self-sustaining + reinvest. Current cost $${d.cost_monthly}/mo to fund AI infra.</em>
  `;
}

function updateAsi() {
  fetch('/api/asi')
    .then(r => r.json())
    .then(renderAsi)
    .catch(e => console.error('ASI fetch error:', e));
}

update();
updateLlm();
updateGrids();
updateWallet();
updateAsi();
setInterval(update, 10000);
setInterval(updateLlm, 30000);
setInterval(updateGrids, 15000);
setInterval(updateWallet, 60000);
setInterval(updateAsi, 60000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8686, debug=False)
