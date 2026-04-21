#!/usr/bin/env python3
"""
Active Position Manager v1 — Smart Trailing SL + Partial Close with LLM Review

Runs periodically to manage open positions:
  1. Calculate profit in ATR multiples for each ACTIVE position
  2. Apply rule-based trailing SL tiers
  3. Consult LLM for final decision (HOLD / TRAIL_SL / PARTIAL_CLOSE / CLOSE)
  4. Execute: cancel old SL, place new SL, or partial/full close

Trailing SL Tiers (LONG example, SHORT is mirrored):
  Breakeven:  profit >= 1.0 ATR → SL = entry + fee_buffer
  Trail 1:    profit >= 1.5 ATR → SL = entry + 0.5 ATR
  Trail 2:    profit >= 2.0 ATR → SL = entry + 1.0 ATR
  Trail 3:    profit >= 2.5 ATR → SL = entry + 1.5 ATR

Usage:
    python3 position_manager.py              # one-shot check
    python3 position_manager.py --daemon     # run every PM_INTERVAL_SEC
    python3 position_manager.py --status     # show position management status

Environment (reads from .env):
    PM_INTERVAL_SEC         optional (default 180 — every 3 minutes)
    PM_COOLDOWN_MIN         optional (default 30 — min time between actions per coin)
    BINANCE_API_KEY         required
    BINANCE_API_SECRET      required
    DEEPSEEK_API_KEY        optional (disables LLM review if missing)
    TELEGRAM_BOT_TOKEN      required for alerts
"""

import json
import os
import sys
import time
import hmac
import hashlib
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal, ROUND_DOWN

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env"
TRADING_STATE_FILE = SCRIPT_DIR / "data" / "workspace-finance" / "trading_state.json"
EXECUTOR_STATE_FILE = SCRIPT_DIR / "data" / "executor_state.json"
PM_STATE_FILE = SCRIPT_DIR / "data" / "position_manager_state.json"
TRADING_CONTROL_FILE = SCRIPT_DIR / "data" / "workspace-finance" / "trading_control.json"

BINANCE_KLINES_API = "https://api.binance.com/api/v3/klines"
BINANCE_FUTURES_PRICE = "https://fapi.binance.com/fapi/v1/ticker/price"
BINANCE_FUTURES_ORDER = "https://fapi.binance.com/fapi/v1/order"
BINANCE_FUTURES_ALGO = "https://fapi.binance.com/fapi/v1/algoOrder"
BINANCE_FUTURES_ACCOUNT = "https://fapi.binance.com/fapi/v2/account"

FUTURES_SYMBOL_MAP = {
    "btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "bnb": "BNBUSDT",
    "xrp": "XRPUSDT", "doge": "DOGEUSDT", "ada": "ADAUSDT", "avax": "AVAXUSDT",
    "link": "LINKUSDT", "aave": "AAVEUSDT", "trx": "TRXUSDT", "zec": "ZECUSDT",
    "sui": "SUIUSDT", "ton": "TONUSDT", "pepe": "1000PEPEUSDT", "ordi": "ORDIUSDT",
    "tao": "TAOUSDT", "edu": "EDUUSDT",
}

# Trailing SL tiers: (min_profit_atr_mult, sl_offset_atr_mult, tier_name)
TRAIL_TIERS = [
    (2.5, 1.5, "TRAIL_3"),
    (2.0, 1.0, "TRAIL_2"),
    (1.5, 0.5, "TRAIL_1"),
    (1.0, 0.0, "BREAKEVEN"),  # 0.0 = entry + fee buffer
]

ATR_PERIOD = 14
ATR_KLINES_INTERVAL = "4h"
ATR_KLINES_LIMIT = 20
RSI_PERIOD = 14
KLINES_LIMIT = 55
KLINES_INTERVAL = "1h"

TAKER_FEE_PCT = 0.04  # Binance Futures taker fee
FEE_BUFFER_MULT = 3   # breakeven SL includes 3x round-trip fee as buffer

DEFAULT_PM_INTERVAL = 180     # 3 minutes
DEFAULT_PM_COOLDOWN = 30      # 30 min cooldown per coin
PARTIAL_CLOSE_PCT = 50        # close 50% on partial


# ---------------------------------------------------------------------------
# Environment & utilities
# ---------------------------------------------------------------------------

def load_dotenv():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def cfg(key: str, default=None):
    return os.environ.get(key, default)


def fmt_price(val: float) -> str:
    if val >= 100:
        return f"${val:,.2f}"
    if val >= 1:
        return f"${val:,.4f}"
    if val >= 0.01:
        return f"${val:.6f}"
    return f"${val:.8f}"


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def send_telegram(text: str):
    token = cfg("TELEGRAM_BOT_TOKEN")
    chat_id = cfg("TELEGRAM_ALERT_CHAT_ID", cfg("TELEGRAM_CHAT_ID"))
    if not token or not chat_id:
        return
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload, headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception as e:
        log(f"Telegram error: {e}")


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_trading_state() -> dict:
    if TRADING_STATE_FILE.exists():
        return json.loads(TRADING_STATE_FILE.read_text())
    return {"states": {}}


def save_trading_state(data: dict):
    TRADING_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    TRADING_STATE_FILE.write_text(json.dumps(data, indent=2))


def load_pm_state() -> dict:
    if PM_STATE_FILE.exists():
        return json.loads(PM_STATE_FILE.read_text())
    return {"last_action": {}, "trail_history": []}


def save_pm_state(state: dict):
    PM_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PM_STATE_FILE.write_text(json.dumps(state, indent=2))


def load_executor_state() -> dict:
    if EXECUTOR_STATE_FILE.exists():
        return json.loads(EXECUTOR_STATE_FILE.read_text())
    return {"trade_history": [], "total_pnl": 0, "total_trades": 0}


def save_executor_state(state: dict):
    EXECUTOR_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    EXECUTOR_STATE_FILE.write_text(json.dumps(state, indent=2))


def is_on_cooldown(pm_state: dict, coin: str, cooldown_min: int) -> bool:
    last_ts = pm_state.get("last_action", {}).get(coin)
    if not last_ts:
        return False
    return (time.time() - last_ts) < cooldown_min * 60


def mark_action(pm_state: dict, coin: str):
    pm_state.setdefault("last_action", {})[coin] = time.time()


# ---------------------------------------------------------------------------
# Technical analysis (same formulas as binance_price_alert.py)
# ---------------------------------------------------------------------------

def calc_rsi(closes: list, period: int = RSI_PERIOD) -> float | None:
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


def calc_ema(closes: list, period: int = 20) -> float | None:
    if len(closes) < period:
        return None
    mult = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]:
        ema = (p - ema) * mult + ema
    return ema


def calc_atr(highs: list, lows: list, closes: list, period: int = ATR_PERIOD) -> float | None:
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    return sum(trs[-period:]) / period


def fetch_indicators(symbol: str) -> dict | None:
    """Fetch RSI + ATR(4h) + EMA20 for a symbol."""
    try:
        url = f"{BINANCE_KLINES_API}?symbol={symbol}&interval={KLINES_INTERVAL}&limit={KLINES_LIMIT}"
        req = urllib.request.Request(url, headers={"User-Agent": "PicoMgr/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read())
        closes = [float(k[4]) for k in raw]
        rsi = calc_rsi(closes)
        ema20 = calc_ema(closes, 20)

        url4h = f"{BINANCE_KLINES_API}?symbol={symbol}&interval={ATR_KLINES_INTERVAL}&limit={ATR_KLINES_LIMIT}"
        req4h = urllib.request.Request(url4h, headers={"User-Agent": "PicoMgr/1.0"})
        with urllib.request.urlopen(req4h, timeout=10) as resp4h:
            raw4h = json.loads(resp4h.read())
        h4 = [float(k[2]) for k in raw4h]
        l4 = [float(k[3]) for k in raw4h]
        c4 = [float(k[4]) for k in raw4h]
        atr = calc_atr(h4, l4, c4) or 0

        return {"rsi": rsi, "atr": atr, "ema20": ema20, "price": closes[-1]}
    except Exception as e:
        log(f"  Failed to fetch indicators for {symbol}: {e}")
        return None


def fetch_futures_price(symbol: str) -> float | None:
    try:
        url = f"{BINANCE_FUTURES_PRICE}?symbol={symbol}"
        req = urllib.request.Request(url, headers={"User-Agent": "PicoMgr/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return float(data["price"])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Binance Futures API helpers (raw REST)
# ---------------------------------------------------------------------------

def binance_sign(params: dict) -> str:
    query = urllib.parse.urlencode(params)
    sig = hmac.new(
        cfg("BINANCE_API_SECRET", "").encode(),
        query.encode(),
        hashlib.sha256,
    ).hexdigest()
    return query + f"&signature={sig}"


def binance_api(method: str, url: str, params: dict) -> dict:
    params["timestamp"] = int(time.time() * 1000)
    body = binance_sign(params).encode()
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={
            "X-MBX-APIKEY": cfg("BINANCE_API_KEY", ""),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        return {"error": f"HTTP {e.code}: {err_body}"}


def cancel_algo_order(algo_id: int) -> dict:
    return binance_api("DELETE", BINANCE_FUTURES_ALGO, {"algoId": algo_id})


def place_sl_order(symbol: str, side: str, stop_price: str) -> dict:
    """Place a new SL algo order (STOP_MARKET with closePosition)."""
    return binance_api("POST", BINANCE_FUTURES_ALGO, {
        "symbol": symbol,
        "side": side,
        "algoType": "CONDITIONAL",
        "orderType": "STOP_MARKET",
        "stopPrice": stop_price,
        "closePosition": "true",
        "workingType": "MARK_PRICE",
    })


def place_tp_order(symbol: str, side: str, stop_price: str) -> dict:
    """Place a new TP algo order (TAKE_PROFIT_MARKET with closePosition)."""
    return binance_api("POST", BINANCE_FUTURES_ALGO, {
        "symbol": symbol,
        "side": side,
        "algoType": "CONDITIONAL",
        "orderType": "TAKE_PROFIT_MARKET",
        "stopPrice": stop_price,
        "closePosition": "true",
        "workingType": "MARK_PRICE",
    })


def market_close_partial(symbol: str, side: str, qty: str) -> dict:
    """Close a portion of a position with a market order."""
    return binance_api("POST", BINANCE_FUTURES_ORDER, {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": qty,
        "reduceOnly": "true",
    })


def get_symbol_precision(symbol: str) -> tuple[int, int]:
    """Returns (price_precision, qty_precision) from exchange info."""
    try:
        url = f"https://fapi.binance.com/fapi/v1/exchangeInfo"
        req = urllib.request.Request(url, headers={"User-Agent": "PicoMgr/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            info = json.loads(resp.read())
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                return s.get("pricePrecision", 2), s.get("quantityPrecision", 3)
    except Exception:
        pass
    return 2, 3

_precision_cache: dict[str, tuple[int, int]] = {}

def round_price(symbol: str, price: float) -> str:
    if symbol not in _precision_cache:
        _precision_cache[symbol] = get_symbol_precision(symbol)
    pp, _ = _precision_cache[symbol]
    return str(Decimal(str(price)).quantize(Decimal(10) ** -pp, rounding=ROUND_DOWN))


def round_qty(symbol: str, qty: float) -> str:
    if symbol not in _precision_cache:
        _precision_cache[symbol] = get_symbol_precision(symbol)
    _, qp = _precision_cache[symbol]
    return str(Decimal(str(qty)).quantize(Decimal(10) ** -qp, rounding=ROUND_DOWN))


# ---------------------------------------------------------------------------
# LLM position review
# ---------------------------------------------------------------------------

def llm_review_position(coin: str, direction: str, entry: float, current: float,
                        sl: float, tp: float, atr: float, rsi: float | None,
                        profit_atr: float, current_tier: str,
                        suggested_new_sl: float, fill_qty: float) -> dict:
    """Ask DeepSeek to review a position and decide action.

    Returns: {"action": "HOLD"|"TRAIL_SL"|"PARTIAL_CLOSE"|"CLOSE",
              "reason": "...", "confidence": 0-100,
              "new_sl": float (if TRAIL_SL), "close_pct": int (if PARTIAL_CLOSE)}
    """
    api_key = cfg("DEEPSEEK_API_KEY")
    if not api_key:
        return {"action": "HOLD", "reason": "No API key", "confidence": 0}

    pnl_pct = ((current - entry) / entry * 100) if direction == "LONG" else ((entry - current) / entry * 100)
    fee_cost_pct = TAKER_FEE_PCT * 2  # round-trip fee
    position_value = fill_qty * current
    rsi_str = f"{rsi:.1f}" if rsi is not None else "N/A"

    prompt = f"""You are a crypto futures position manager. Review this open position and decide the best action.

POSITION: {coin.upper()} {direction}
Entry: ${entry} | Current: ${current} | P&L: {pnl_pct:+.2f}%
SL: ${sl} | TP: ${tp}
ATR(4h): ${atr:.6f} | Profit: {profit_atr:.2f}x ATR
RSI(14): {rsi_str}
Current trail tier: {current_tier}
Suggested new SL: ${suggested_new_sl:.6f}
Position size: {fill_qty} units (${position_value:.2f})
Trading fee per action: {TAKER_FEE_PCT}% (${position_value * TAKER_FEE_PCT / 100:.4f})

AVAILABLE ACTIONS:
1. HOLD — keep current SL/TP, no change
2. TRAIL_SL — move SL to ${suggested_new_sl:.6f} (lock in profit, reduce risk)
3. PARTIAL_CLOSE — close {PARTIAL_CLOSE_PCT}% now at profit, trail SL on remaining
4. CLOSE — close entire position now

DECISION RULES:
- TRAIL_SL if profit is solid (>1 ATR) and trend still intact (RSI not extreme)
- PARTIAL_CLOSE if profit > 2 ATR AND (RSI reaching extreme zone OR momentum weakening)
- CLOSE if RSI extremely overbought/oversold for the direction (LONG+RSI>78, SHORT+RSI<22) OR clear reversal signal
- HOLD if profit < 1 ATR or SL already at suggested level
- Fee-aware: PARTIAL_CLOSE only profitable if gain > {fee_cost_pct:.2f}% round-trip fee
- NEVER trail SL backwards (further from current price than existing SL)

Reply ONLY in this JSON format:
{{"action": "HOLD|TRAIL_SL|PARTIAL_CLOSE|CLOSE", "reason": "one sentence", "confidence": 0-100, "new_sl": {suggested_new_sl:.6f}, "close_pct": {PARTIAL_CLOSE_PCT}}}"""

    try:
        body = json.dumps({
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 200,
            "temperature": 0.1,
        }).encode()

        req = urllib.request.Request(
            "https://api.deepseek.com/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())

        content = result["choices"][0]["message"]["content"].strip()
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        content = content.strip()

        review = json.loads(content)
        action = review.get("action", "HOLD").upper()
        if action not in ("HOLD", "TRAIL_SL", "PARTIAL_CLOSE", "CLOSE"):
            action = "HOLD"
        review["action"] = action
        return review

    except Exception as e:
        log(f"  LLM review error: {e} — defaulting to rule-based")
        return {"action": "HOLD", "reason": f"API error: {e}", "confidence": 0}


# ---------------------------------------------------------------------------
# Core position management logic
# ---------------------------------------------------------------------------

def calculate_trail_tier(direction: str, entry: float, current: float,
                         atr: float, current_sl: float) -> tuple[str, float]:
    """Determine which trail tier applies and the suggested new SL.

    Returns (tier_name, new_sl_price). If no tier applies, returns ("NONE", current_sl).
    """
    if atr <= 0:
        return "NONE", current_sl

    if direction == "LONG":
        profit = current - entry
    else:
        profit = entry - current

    profit_atr = profit / atr

    fee_buffer = entry * (TAKER_FEE_PCT * FEE_BUFFER_MULT / 100)

    for min_atr, sl_offset, tier_name in TRAIL_TIERS:
        if profit_atr >= min_atr:
            if sl_offset == 0.0:
                # Breakeven: entry + fee buffer
                if direction == "LONG":
                    new_sl = entry + fee_buffer
                else:
                    new_sl = entry - fee_buffer
            else:
                if direction == "LONG":
                    new_sl = entry + sl_offset * atr
                else:
                    new_sl = entry - sl_offset * atr

            # Never trail SL backwards
            if direction == "LONG" and new_sl <= current_sl:
                return "NONE", current_sl
            if direction == "SHORT" and new_sl >= current_sl:
                return "NONE", current_sl

            return tier_name, new_sl

    return "NONE", current_sl


def execute_trail_sl(coin: str, cs: dict, symbol: str, new_sl: float,
                     direction: str) -> bool:
    """Safely move SL: cancel old → place new. If new fails, try restoring old.
    If restore also fails → emergency Telegram alert (position is unprotected).
    Returns True on success."""
    old_sl_id = cs.get("sl_order_id", "")
    old_sl_price = cs.get("sl_price", 0)
    close_side = "SELL" if direction == "LONG" else "BUY"
    new_sl_str = round_price(symbol, new_sl)

    # Step 1: cancel old SL (must do this — Binance rejects duplicate closePosition)
    if old_sl_id:
        try:
            r = cancel_algo_order(int(old_sl_id))
            status = r.get("algoStatus", r.get("code", "?"))
            log(f"  Cancelled old SL #{old_sl_id}: {status}")
            if "error" in r and "code" not in r.get("error", "") and "Algo order not exists" not in r.get("error", ""):
                # Cancel may have failed — try to place new SL anyway
                # If old SL still active, new placement will fail with -4130
                log(f"  WARN: cancel may have failed, attempting place anyway")
        except Exception as e:
            log(f"  WARN: failed to cancel old SL: {e}")

    time.sleep(0.5)

    # Step 2: place new SL
    r = place_sl_order(symbol, close_side, new_sl_str)
    if "error" in r:
        err_msg = r["error"]
        log(f"  ERROR placing new SL: {err_msg}")

        # Step 3: emergency — try to restore old SL
        if old_sl_price and old_sl_price != new_sl:
            log(f"  EMERGENCY: attempting to restore old SL @ {old_sl_price}")
            old_sl_str = round_price(symbol, old_sl_price)
            time.sleep(0.5)
            restore_r = place_sl_order(symbol, close_side, old_sl_str)
            if "error" in restore_r:
                log(f"  CRITICAL: restore failed: {restore_r['error']}")
                msg = (
                    f"🚨 *CRITICAL — {coin.upper()} {direction} SL UNPROTECTED*\n"
                    f"Failed to trail SL AND failed to restore old SL.\n"
                    f"New SL ${new_sl_str} error: {err_msg[:100]}\n"
                    f"Restore error: {restore_r['error'][:100]}\n"
                    f"⚠️ MANUAL INTERVENTION NEEDED — check Binance app"
                )
                send_telegram(msg)
                cs["sl_order_id"] = ""  # mark as orphaned
                return False
            else:
                restored_id = str(restore_r.get("algoId", ""))
                cs["sl_order_id"] = restored_id
                cs["sl_price"] = float(old_sl_str)
                log(f"  Restored old SL @ {old_sl_str} (algo #{restored_id})")
                msg = (
                    f"⚠️ *{coin.upper()} {direction} — SL trail failed, restored old*\n"
                    f"Old SL kept @ ${old_sl_str}\n"
                    f"Tried new SL ${new_sl_str}: {err_msg[:100]}"
                )
                send_telegram(msg)
        return False

    new_sl_id = str(r.get("algoId", ""))
    cs["sl_price"] = float(new_sl_str)
    cs["sl_order_id"] = new_sl_id
    log(f"  New SL placed: {new_sl_str} (algo #{new_sl_id})")
    return True


def execute_partial_close(coin: str, cs: dict, symbol: str, close_pct: int,
                          direction: str, current_price: float) -> float:
    """Close a portion of the position. Returns realized PnL of the closed portion.
    Safety: if market close fails after canceling SL/TP, restore them immediately."""
    fill_qty = cs.get("fill_qty", 0)
    if fill_qty <= 0:
        return 0.0

    close_qty_raw = fill_qty * (close_pct / 100)
    close_qty_str = round_qty(symbol, close_qty_raw)
    close_qty = float(close_qty_str)

    if close_qty <= 0:
        log(f"  Partial close qty too small: {close_qty_raw}")
        return 0.0

    close_side = "SELL" if direction == "LONG" else "BUY"

    old_sl_id = cs.get("sl_order_id", "")
    old_tp_id = cs.get("tp_order_id", "")
    old_sl_price = cs.get("sl_price", 0)
    old_tp_price = cs.get("tp_price", 0)
    if old_sl_id:
        try:
            cancel_algo_order(int(old_sl_id))
        except Exception:
            pass
    if old_tp_id:
        try:
            cancel_algo_order(int(old_tp_id))
        except Exception:
            pass

    time.sleep(0.3)

    r = market_close_partial(symbol, close_side, close_qty_str)
    if "error" in r:
        log(f"  ERROR partial close: {r['error']}")
        # Emergency: restore SL and TP since position still open
        log(f"  EMERGENCY: restoring SL/TP after failed partial close")
        if old_sl_price:
            sl_r = place_sl_order(symbol, close_side, round_price(symbol, old_sl_price))
            if "error" not in sl_r:
                cs["sl_order_id"] = str(sl_r.get("algoId", ""))
                log(f"  Restored SL @ {old_sl_price}")
        if old_tp_price:
            tp_r = place_tp_order(symbol, close_side, round_price(symbol, old_tp_price))
            if "error" not in tp_r:
                cs["tp_order_id"] = str(tp_r.get("algoId", ""))
                log(f"  Restored TP @ {old_tp_price}")
        send_telegram(
            f"⚠️ *{coin.upper()} {direction} — partial close failed*\n"
            f"Original SL/TP restored. Position size unchanged.\n"
            f"Error: {r['error'][:100]}"
        )
        return 0.0

    entry = cs.get("entry_price", 0)
    if direction == "LONG":
        pnl = (current_price - entry) * close_qty
    else:
        pnl = (entry - current_price) * close_qty

    fee = current_price * close_qty * (TAKER_FEE_PCT / 100)
    net_pnl = pnl - fee

    remaining_qty = fill_qty - close_qty
    cs["fill_qty"] = remaining_qty
    cs["sl_order_id"] = ""
    cs["tp_order_id"] = ""

    log(f"  Partial close: {close_qty_str} {symbol} @ ~{fmt_price(current_price)}")
    log(f"  Realized: ${net_pnl:+.4f} (gross ${pnl:+.4f} - fee ${fee:.4f})")
    log(f"  Remaining: {remaining_qty} units")

    return net_pnl


def execute_full_close(coin: str, cs: dict, symbol: str, direction: str,
                       current_price: float) -> float:
    """Close entire position. Returns net PnL."""
    fill_qty = cs.get("fill_qty", 0)
    if fill_qty <= 0:
        return 0.0

    close_qty_str = round_qty(symbol, fill_qty)
    close_side = "SELL" if direction == "LONG" else "BUY"

    old_sl_id = cs.get("sl_order_id", "")
    old_tp_id = cs.get("tp_order_id", "")
    if old_sl_id:
        try:
            cancel_algo_order(int(old_sl_id))
        except Exception:
            pass
    if old_tp_id:
        try:
            cancel_algo_order(int(old_tp_id))
        except Exception:
            pass

    time.sleep(0.3)

    r = market_close_partial(symbol, close_side, close_qty_str)
    if "error" in r:
        log(f"  ERROR full close: {r['error']}")
        return 0.0

    entry = cs.get("entry_price", 0)
    if direction == "LONG":
        pnl = (current_price - entry) * fill_qty
    else:
        pnl = (entry - current_price) * fill_qty

    fee = current_price * fill_qty * (TAKER_FEE_PCT / 100)
    net_pnl = pnl - fee

    log(f"  Full close: {close_qty_str} {symbol} @ ~{fmt_price(current_price)}")
    log(f"  Net PnL: ${net_pnl:+.4f}")

    return net_pnl


def place_new_sl_tp(cs: dict, symbol: str, direction: str, sl_price: float, tp_price: float):
    """Re-place SL and TP orders after partial close changed the position size."""
    close_side = "SELL" if direction == "LONG" else "BUY"
    sl_str = round_price(symbol, sl_price)
    tp_str = round_price(symbol, tp_price)

    r_sl = place_sl_order(symbol, close_side, sl_str)
    if "error" not in r_sl:
        cs["sl_order_id"] = str(r_sl.get("algoId", ""))
        cs["sl_price"] = float(sl_str)
        log(f"  Re-placed SL: {sl_str}")

    r_tp = place_tp_order(symbol, close_side, tp_str)
    if "error" not in r_tp:
        cs["tp_order_id"] = str(r_tp.get("algoId", ""))
        log(f"  Re-placed TP: {tp_str}")


# ---------------------------------------------------------------------------
# Main management loop
# ---------------------------------------------------------------------------

def manage_positions():
    """Check all ACTIVE positions and manage SL trailing / partial close."""
    trading_state = load_trading_state()
    pm_state = load_pm_state()
    cooldown_min = int(cfg("PM_COOLDOWN_MIN", str(DEFAULT_PM_COOLDOWN)))
    states = trading_state.get("states", {})

    active_positions = {
        coin: cs for coin, cs in states.items()
        if cs.get("state") == "ACTIVE" and cs.get("entry_price")
    }

    if not active_positions:
        log("No active positions to manage.")
        return

    log(f"Managing {len(active_positions)} active position(s)...")
    actions_taken = 0

    for coin, cs in active_positions.items():
        symbol = FUTURES_SYMBOL_MAP.get(coin)
        if not symbol:
            continue

        direction = cs.get("direction", "LONG")
        entry = cs["entry_price"]
        current_sl = cs.get("sl_price", 0)
        current_tp = cs.get("tp_price", 0)
        fill_qty = cs.get("fill_qty", 0)

        if is_on_cooldown(pm_state, coin, cooldown_min):
            continue

        current_price = fetch_futures_price(symbol)
        if not current_price:
            continue

        indicators = fetch_indicators(symbol)
        if not indicators:
            continue

        atr = indicators.get("atr", 0)
        rsi = indicators.get("rsi")

        if atr <= 0:
            continue

        if direction == "LONG":
            profit = current_price - entry
        else:
            profit = entry - current_price

        profit_atr = profit / atr
        pnl_pct = profit / entry * 100

        tier_name, suggested_sl = calculate_trail_tier(
            direction, entry, current_price, atr, current_sl
        )

        rsi_str = f"{rsi:.0f}" if rsi else "N/A"
        log(f"  {coin.upper()} {direction} | Entry: {fmt_price(entry)} | Now: {fmt_price(current_price)} | "
            f"P&L: {pnl_pct:+.2f}% ({profit_atr:.2f}x ATR) | RSI: {rsi_str} | "
            f"Tier: {tier_name}")

        if tier_name == "NONE":
            log(f"  → No trail action needed (profit {profit_atr:.2f}x ATR < 1.0 or SL already ahead)")
            continue

        # Consult LLM for final decision
        review = llm_review_position(
            coin=coin, direction=direction, entry=entry, current=current_price,
            sl=current_sl, tp=current_tp, atr=atr, rsi=rsi,
            profit_atr=profit_atr, current_tier=tier_name,
            suggested_new_sl=suggested_sl, fill_qty=fill_qty,
        )

        action = review.get("action", "HOLD")
        reason = review.get("reason", "")
        confidence = review.get("confidence", 0)

        log(f"  LLM: {action} ({confidence}%) — {reason}")

        if action == "HOLD":
            continue

        if action == "TRAIL_SL":
            new_sl = review.get("new_sl", suggested_sl)
            # Validate: never trail backwards
            if direction == "LONG" and new_sl <= current_sl:
                log(f"  Skip: new SL {fmt_price(new_sl)} <= current {fmt_price(current_sl)}")
                continue
            if direction == "SHORT" and new_sl >= current_sl:
                log(f"  Skip: new SL {fmt_price(new_sl)} >= current {fmt_price(current_sl)}")
                continue

            ok = execute_trail_sl(coin, cs, symbol, new_sl, direction)
            if ok:
                mark_action(pm_state, coin)
                save_trading_state(trading_state)
                actions_taken += 1

                old_sl_str = fmt_price(current_sl)
                new_sl_str = fmt_price(new_sl)
                msg = (
                    f"📈 *[TRAIL SL] {coin.upper()} {direction}* ({tier_name})\n"
                    f"SL: {old_sl_str} → *{new_sl_str}*\n"
                    f"Entry: {fmt_price(entry)} | Now: {fmt_price(current_price)}\n"
                    f"P&L: {pnl_pct:+.2f}% ({profit_atr:.1f}x ATR) | RSI: {rsi_str}\n\n"
                    f"🤖 LLM ({confidence}%): {reason}"
                )
                send_telegram(msg)

        elif action == "PARTIAL_CLOSE":
            close_pct = review.get("close_pct", PARTIAL_CLOSE_PCT)
            net_pnl = execute_partial_close(coin, cs, symbol, close_pct, direction, current_price)

            if net_pnl != 0:
                # Re-place SL/TP for remaining position with trailed SL
                place_new_sl_tp(cs, symbol, direction, suggested_sl, current_tp)
                mark_action(pm_state, coin)
                save_trading_state(trading_state)
                actions_taken += 1

                # Update executor state
                es = load_executor_state()
                es["daily_pnl"] = es.get("daily_pnl", 0) + net_pnl
                es["total_pnl"] = es.get("total_pnl", 0) + net_pnl
                es.setdefault("trade_history", []).append({
                    "coin": coin, "direction": direction,
                    "entry": entry, "close": current_price,
                    "pnl": round(net_pnl, 4), "result": "PARTIAL_CLOSE",
                    "time": datetime.now(timezone.utc).isoformat(),
                    "note": f"LLM: {reason}",
                })
                save_executor_state(es)

                msg = (
                    f"✂️ *[PARTIAL CLOSE] {coin.upper()} {direction}* ({close_pct}%)\n"
                    f"Closed {close_pct}% @ {fmt_price(current_price)}\n"
                    f"Net P&L: *${net_pnl:+.4f}*\n"
                    f"Remaining: {cs['fill_qty']:.4f} units\n"
                    f"New SL: {fmt_price(suggested_sl)} ({tier_name})\n"
                    f"P&L: {pnl_pct:+.2f}% ({profit_atr:.1f}x ATR) | RSI: {rsi_str}\n\n"
                    f"🤖 LLM ({confidence}%): {reason}"
                )
                send_telegram(msg)

        elif action == "CLOSE":
            net_pnl = execute_full_close(coin, cs, symbol, direction, current_price)

            cs["state"] = "TP_HIT" if net_pnl > 0 else "SL_HIT"
            cs["close_price"] = current_price
            cs["pnl_usd"] = round(net_pnl, 4)
            mark_action(pm_state, coin)
            save_trading_state(trading_state)
            actions_taken += 1

            es = load_executor_state()
            es["daily_pnl"] = es.get("daily_pnl", 0) + net_pnl
            es["total_pnl"] = es.get("total_pnl", 0) + net_pnl
            es["total_trades"] = es.get("total_trades", 0) + 1
            if net_pnl < 0:
                es["consecutive_losses"] = es.get("consecutive_losses", 0) + 1
            else:
                es["consecutive_losses"] = 0
            es.setdefault("trade_history", []).append({
                "coin": coin, "direction": direction,
                "entry": entry, "close": current_price,
                "pnl": round(net_pnl, 4),
                "result": "TP_HIT" if net_pnl > 0 else "SL_HIT",
                "time": datetime.now(timezone.utc).isoformat(),
                "note": f"LLM advised close: {reason}",
            })
            save_executor_state(es)

            emoji = "✅" if net_pnl > 0 else "🔴"
            msg = (
                f"{emoji} *[LLM CLOSE] {coin.upper()} {direction}*\n"
                f"Entry: {fmt_price(entry)} → Close: {fmt_price(current_price)}\n"
                f"Net P&L: *${net_pnl:+.4f}*\n"
                f"RSI: {rsi_str} | Profit was {profit_atr:.1f}x ATR\n\n"
                f"🤖 LLM ({confidence}%): {reason}"
            )
            send_telegram(msg)

    save_pm_state(pm_state)
    log(f"Done. Actions taken: {actions_taken}")


# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------

def print_status():
    load_dotenv()
    trading_state = load_trading_state()
    pm_state = load_pm_state()
    states = trading_state.get("states", {})

    active = {c: s for c, s in states.items() if s.get("state") == "ACTIVE" and s.get("entry_price")}
    print(f"\n{'=' * 60}")
    print(f"  POSITION MANAGER STATUS")
    print(f"{'=' * 60}")
    print(f"  Active positions: {len(active)}")
    print(f"  PM interval: {cfg('PM_INTERVAL_SEC', str(DEFAULT_PM_INTERVAL))}s")
    print(f"  Cooldown: {cfg('PM_COOLDOWN_MIN', str(DEFAULT_PM_COOLDOWN))} min")
    print(f"  Partial close: {PARTIAL_CLOSE_PCT}%")
    print(f"  Fee: {TAKER_FEE_PCT}% taker")
    print()

    for coin, cs in active.items():
        symbol = FUTURES_SYMBOL_MAP.get(coin, "?")
        direction = cs.get("direction", "?")
        entry = cs["entry_price"]
        current_sl = cs.get("sl_price", 0)
        current_tp = cs.get("tp_price", 0)
        fill_qty = cs.get("fill_qty", 0)

        current_price = fetch_futures_price(symbol)
        indicators = fetch_indicators(symbol) if current_price else None

        atr = indicators.get("atr", 0) if indicators else 0
        rsi = indicators.get("rsi") if indicators else None

        if current_price and atr > 0:
            if direction == "LONG":
                profit = current_price - entry
            else:
                profit = entry - current_price
            profit_atr = profit / atr
            pnl_pct = profit / entry * 100
        else:
            profit_atr = 0
            pnl_pct = 0

        tier_name, suggested_sl = calculate_trail_tier(
            direction, entry, current_price or entry, atr, current_sl
        ) if atr > 0 else ("N/A", current_sl)

        cooldown = is_on_cooldown(pm_state, coin, int(cfg("PM_COOLDOWN_MIN", str(DEFAULT_PM_COOLDOWN))))
        last_action_ts = pm_state.get("last_action", {}).get(coin)
        last_action_str = ""
        if last_action_ts:
            ago = (time.time() - last_action_ts) / 60
            last_action_str = f" (last action {ago:.0f}m ago)"

        print(f"  {coin.upper():>6} {direction:5} | Entry: {fmt_price(entry)} | Now: {fmt_price(current_price or 0)}")
        print(f"         SL: {fmt_price(current_sl)} | TP: {fmt_price(current_tp)} | Qty: {fill_qty}")
        rsi_str = f"{rsi:.0f}" if rsi else "N/A"
        print(f"         P&L: {pnl_pct:+.2f}% ({profit_atr:.2f}x ATR) | RSI: {rsi_str}")
        print(f"         Trail tier: {tier_name} | Suggested SL: {fmt_price(suggested_sl)}")
        print(f"         Cooldown: {'YES' if cooldown else 'NO'}{last_action_str}")
        print()

    trail_history = pm_state.get("trail_history", [])[-5:]
    if trail_history:
        print(f"  Recent trail actions:")
        for h in reversed(trail_history):
            print(f"    {h}")
    print()


# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------

def run_once():
    load_dotenv()

    # Check kill switch
    if TRADING_CONTROL_FILE.exists():
        try:
            ctrl = json.loads(TRADING_CONTROL_FILE.read_text())
            if not ctrl.get("auto_trade_enabled", True):
                log(f"Kill switch active: {ctrl.get('reason', 'disabled')}")
                return
        except Exception:
            pass

    manage_positions()


def daemon_loop():
    load_dotenv()
    interval = int(cfg("PM_INTERVAL_SEC", str(DEFAULT_PM_INTERVAL)))
    cooldown = int(cfg("PM_COOLDOWN_MIN", str(DEFAULT_PM_COOLDOWN)))

    log(f"Position Manager v1 — every {interval}s — cooldown {cooldown}min")
    log(f"Trail tiers: " + " | ".join(f"{t[2]}: >{t[0]}xATR→SL+{t[1]}xATR" for t in TRAIL_TIERS))
    log(f"Partial close: {PARTIAL_CLOSE_PCT}% | Fee: {TAKER_FEE_PCT}%")

    while True:
        try:
            run_once()
        except Exception as exc:
            log(f"ERROR: {exc}")
        time.sleep(interval)


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        daemon_loop()
    elif "--status" in sys.argv:
        print_status()
    else:
        run_once()
