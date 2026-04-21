#!/usr/bin/env python3
"""
Trade Executor v1 — Binance Futures Auto-Execution

Watches trading_state.json for ACTIVE signals (confirmed by LLM) and
automatically places market orders with exchange-side SL/TP stop orders.

Safety mechanisms:
  - Exchange-side SL/TP (survives script crash)
  - Daily loss limit (pauses trading after hitting)
  - Circuit breaker (N consecutive SL -> pause 24h)
  - Max leverage hardcoded
  - Position sync on startup (reconcile with exchange)
  - Kill switch via AUTO_TRADE_ENABLED=false in .env

Usage:
    python3 trade_executor.py              # one-shot sync + execute
    python3 trade_executor.py --daemon     # poll every EXEC_INTERVAL_SEC
    python3 trade_executor.py --status     # show exchange positions vs local state
    python3 trade_executor.py --close-all  # emergency close all positions

Environment (reads from .env):
    BINANCE_API_KEY        required
    BINANCE_API_SECRET     required
    BINANCE_TESTNET        optional (default true — uses testnet)
    FUTURES_LEVERAGE       optional (default 5)
    DAILY_LOSS_LIMIT       optional (default 10 USD)
    CIRCUIT_BREAKER_MAX    optional (default 3 consecutive losses)
    AUTO_TRADE_ENABLED     optional (default true)
    EXEC_INTERVAL_SEC      optional (default 15)
    PORTFOLIO_BALANCE      optional (default 100)
"""

import json
import os
import sys
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from decimal import Decimal, ROUND_DOWN

from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env"
TRADING_STATE_FILE = SCRIPT_DIR / "data" / "workspace-finance" / "trading_state.json"
EXECUTOR_STATE_FILE = SCRIPT_DIR / "data" / "executor_state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("executor")

DEFAULT_LEVERAGE = 5
MAX_LEVERAGE_CAP = 20
DEFAULT_DAILY_LOSS_LIMIT = 10.0
DEFAULT_CIRCUIT_BREAKER = 3
DEFAULT_EXEC_INTERVAL = 15

FUTURES_SYMBOL_MAP = {
    "btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "bnb": "BNBUSDT",
    "xrp": "XRPUSDT", "doge": "DOGEUSDT", "ada": "ADAUSDT", "avax": "AVAXUSDT",
    "link": "LINKUSDT", "aave": "AAVEUSDT", "trx": "TRXUSDT", "zec": "ZECUSDT",
    "sui": "SUIUSDT", "ton": "TONUSDT", "pepe": "1000PEPEUSDT", "ordi": "ORDIUSDT",
    "tao": "TAOUSDT", "edu": "EDUUSDT",
}


def load_dotenv():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()


def cfg(key: str, default=None):
    return os.environ.get(key, default)


def send_telegram(text: str):
    token = cfg("TELEGRAM_BOT_TOKEN")
    chat_id = cfg("TELEGRAM_ALERT_CHAT_ID", cfg("TELEGRAM_CHAT_ID"))
    if not token or not chat_id:
        log.warning("Telegram not configured, skipping notification")
        return
    import urllib.request
    import urllib.error
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        log.warning(f"Telegram send failed: {exc}")


def fmt_price(val: float) -> str:
    if val >= 100:
        return f"${val:,.2f}"
    if val >= 1:
        return f"${val:,.4f}"
    if val >= 0.01:
        return f"${val:.6f}"
    return f"${val:.8f}"


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


def load_executor_state() -> dict:
    if EXECUTOR_STATE_FILE.exists():
        return json.loads(EXECUTOR_STATE_FILE.read_text())
    return {
        "daily_pnl": 0.0,
        "daily_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "consecutive_losses": 0,
        "paused_until": None,
        "total_trades": 0,
        "total_pnl": 0.0,
        "trade_history": [],
    }


def save_executor_state(state: dict):
    EXECUTOR_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    EXECUTOR_STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Binance Futures helpers
# ---------------------------------------------------------------------------

class BinanceExecutor:
    def __init__(self):
        api_key = cfg("BINANCE_API_KEY")
        api_secret = cfg("BINANCE_API_SECRET")
        testnet = cfg("BINANCE_TESTNET", "true").lower() in ("true", "1", "yes")

        if not api_key or not api_secret:
            raise RuntimeError("BINANCE_API_KEY and BINANCE_API_SECRET required in .env")

        self.testnet = testnet
        if testnet:
            self.client = Client(api_key, api_secret, testnet=True)
            self.client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"
            self.client.FUTURES_COIN_URL = "https://testnet.binancefuture.com/dapi"
            log.info("Connected to Binance Futures TESTNET")
        else:
            self.client = Client(api_key, api_secret)
            log.info("Connected to Binance Futures MAINNET")

        self.leverage = min(int(cfg("FUTURES_LEVERAGE", str(DEFAULT_LEVERAGE))), MAX_LEVERAGE_CAP)
        self.daily_loss_limit = float(cfg("DAILY_LOSS_LIMIT", str(DEFAULT_DAILY_LOSS_LIMIT)))
        self.circuit_breaker_max = int(cfg("CIRCUIT_BREAKER_MAX", str(DEFAULT_CIRCUIT_BREAKER)))
        self.symbol_info_cache: dict = {}

    def get_symbol_info(self, symbol: str) -> dict:
        if symbol not in self.symbol_info_cache:
            info = self.client.futures_exchange_info()
            for s in info["symbols"]:
                self.symbol_info_cache[s["symbol"]] = s
        return self.symbol_info_cache.get(symbol, {})

    def get_price_precision(self, symbol: str) -> int:
        info = self.get_symbol_info(symbol)
        return info.get("pricePrecision", 2)

    def get_qty_precision(self, symbol: str) -> int:
        info = self.get_symbol_info(symbol)
        return info.get("quantityPrecision", 3)

    def get_min_qty(self, symbol: str) -> float:
        info = self.get_symbol_info(symbol)
        for f in info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                return float(f["minQty"])
        return 0.001

    def round_price(self, symbol: str, price: float) -> str:
        precision = self.get_price_precision(symbol)
        return str(Decimal(str(price)).quantize(Decimal(10) ** -precision, rounding=ROUND_DOWN))

    def round_qty(self, symbol: str, qty: float) -> str:
        precision = self.get_qty_precision(symbol)
        result = Decimal(str(qty)).quantize(Decimal(10) ** -precision, rounding=ROUND_DOWN)
        return str(result)

    def set_leverage(self, symbol: str):
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=self.leverage)
            log.info(f"Set leverage {self.leverage}x for {symbol}")
        except BinanceAPIException as e:
            if "No need to change leverage" in str(e):
                pass
            else:
                log.warning(f"Failed to set leverage for {symbol}: {e}")

    def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED"):
        try:
            self.client.futures_change_margin_type(symbol=symbol, marginType=margin_type)
            log.info(f"Set margin type {margin_type} for {symbol}")
        except BinanceAPIException as e:
            if "No need to change margin type" in str(e):
                pass
            else:
                log.warning(f"Failed to set margin type for {symbol}: {e}")

    def get_open_positions(self) -> dict:
        """Get all non-zero positions from exchange."""
        positions = {}
        try:
            account = self.client.futures_account()
            for pos in account.get("positions", []):
                amt = float(pos["positionAmt"])
                if amt != 0:
                    positions[pos["symbol"]] = {
                        "qty": amt,
                        "entry_price": float(pos["entryPrice"]),
                        "unrealized_pnl": float(pos["unrealizedProfit"]),
                        "leverage": int(pos["leverage"]),
                        "margin_type": pos.get("marginType", ""),
                    }
        except Exception as exc:
            log.error(f"Failed to fetch positions: {exc}")
        return positions

    def get_open_orders(self, symbol: str) -> list:
        try:
            return self.client.futures_get_open_orders(symbol=symbol)
        except Exception as exc:
            log.error(f"Failed to get open orders for {symbol}: {exc}")
            return []

    def cancel_all_orders(self, symbol: str):
        try:
            self.client.futures_cancel_all_open_orders(symbol=symbol)
            log.info(f"Cancelled all open orders for {symbol}")
        except Exception as exc:
            log.warning(f"Failed to cancel orders for {symbol}: {exc}")

    def open_position(self, symbol: str, direction: str, qty: float,
                      sl_price: float, tp_price: float) -> dict | None:
        """Place market order + SL/TP stop orders. Returns fill info or None."""
        side = SIDE_BUY if direction == "LONG" else SIDE_SELL
        qty_str = self.round_qty(symbol, abs(qty))

        if float(qty_str) < self.get_min_qty(symbol):
            log.error(f"Qty {qty_str} below min for {symbol}")
            return None

        self.set_leverage(symbol)
        self.set_margin_type(symbol)

        try:
            order = self.client.futures_create_order(
                symbol=symbol,
                side=side,
                type=FUTURE_ORDER_TYPE_MARKET,
                quantity=qty_str,
            )
            log.info(f"MARKET {side} {qty_str} {symbol} — order {order['orderId']}")
        except BinanceAPIException as exc:
            log.error(f"Failed to place market order: {exc}")
            return None

        fill_price = float(order.get("avgPrice", 0))
        if fill_price == 0 and order.get("fills"):
            total_qty = sum(float(f["qty"]) for f in order["fills"])
            total_cost = sum(float(f["qty"]) * float(f["price"]) for f in order["fills"])
            fill_price = total_cost / total_qty if total_qty else 0

        sl_order_id = None
        tp_order_id = None

        close_side = SIDE_SELL if direction == "LONG" else SIDE_BUY
        sl_price_str = self.round_price(symbol, sl_price)
        tp_price_str = self.round_price(symbol, tp_price)

        try:
            sl_order = self.client.futures_create_order(
                symbol=symbol,
                side=close_side,
                type=FUTURE_ORDER_TYPE_STOP_MARKET,
                stopPrice=sl_price_str,
                closePosition="true",
                timeInForce="GTC",
                workingType="MARK_PRICE",
            )
            sl_order_id = sl_order["orderId"]
            log.info(f"SL order placed: {sl_price_str} — order {sl_order_id}")
        except BinanceAPIException as exc:
            log.error(f"Failed to place SL order: {exc}")

        try:
            tp_order = self.client.futures_create_order(
                symbol=symbol,
                side=close_side,
                type=FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET,
                stopPrice=tp_price_str,
                closePosition="true",
                timeInForce="GTC",
                workingType="MARK_PRICE",
            )
            tp_order_id = tp_order["orderId"]
            log.info(f"TP order placed: {tp_price_str} — order {tp_order_id}")
        except BinanceAPIException as exc:
            log.error(f"Failed to place TP order: {exc}")

        return {
            "order_id": str(order["orderId"]),
            "fill_price": fill_price,
            "fill_qty": float(qty_str),
            "fill_time": datetime.now(timezone.utc).isoformat(),
            "sl_order_id": str(sl_order_id) if sl_order_id else "",
            "tp_order_id": str(tp_order_id) if tp_order_id else "",
        }

    def close_position(self, symbol: str, qty: float, direction: str) -> dict | None:
        """Close a position with market order."""
        close_side = SIDE_SELL if direction == "LONG" else SIDE_BUY
        qty_str = self.round_qty(symbol, abs(qty))
        try:
            self.cancel_all_orders(symbol)
            order = self.client.futures_create_order(
                symbol=symbol,
                side=close_side,
                type=FUTURE_ORDER_TYPE_MARKET,
                quantity=qty_str,
            )
            fill_price = float(order.get("avgPrice", 0))
            log.info(f"CLOSED {symbol} {direction} — fill {fill_price}")
            return {"fill_price": fill_price, "order_id": str(order["orderId"])}
        except BinanceAPIException as exc:
            log.error(f"Failed to close {symbol}: {exc}")
            return None

    def close_all_positions(self):
        """Emergency: close everything."""
        positions = self.get_open_positions()
        for symbol, pos in positions.items():
            direction = "LONG" if pos["qty"] > 0 else "SHORT"
            self.close_position(symbol, pos["qty"], direction)
        log.info(f"Emergency close: {len(positions)} positions closed")
        return len(positions)

    def get_futures_balance(self) -> float:
        try:
            account = self.client.futures_account()
            return float(account.get("totalWalletBalance", 0))
        except Exception as exc:
            log.error(f"Failed to get balance: {exc}")
            return 0


# ---------------------------------------------------------------------------
# Safety checks
# ---------------------------------------------------------------------------

def check_safety(executor_state: dict, executor: BinanceExecutor) -> tuple[bool, str]:
    """Returns (is_safe, reason). If not safe, trading is paused."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if executor_state.get("daily_date") != today:
        executor_state["daily_pnl"] = 0.0
        executor_state["daily_date"] = today

    auto_enabled = cfg("AUTO_TRADE_ENABLED", "true").lower() in ("true", "1", "yes")
    if not auto_enabled:
        return False, "AUTO_TRADE_ENABLED=false"

    # OpenClaw kill switch override
    ctrl_file = TRADING_STATE_FILE.parent / "trading_control.json"
    if ctrl_file.exists():
        try:
            ctrl = json.loads(ctrl_file.read_text())
            if not ctrl.get("auto_trade_enabled", True):
                return False, f"Kill switch by {ctrl.get('updated_by', 'OpenClaw')}: {ctrl.get('reason', 'disabled')}"
            if ctrl.get("emergency_close_all", False):
                log.warning("EMERGENCY CLOSE ALL triggered by OpenClaw")
                return False, "Emergency close — OpenClaw triggered"
        except Exception:
            pass

    paused_until = executor_state.get("paused_until")
    if paused_until:
        pause_time = datetime.fromisoformat(paused_until)
        if datetime.now(timezone.utc) < pause_time:
            remaining = (pause_time - datetime.now(timezone.utc)).total_seconds() / 3600
            return False, f"Circuit breaker active, resumes in {remaining:.1f}h"
        else:
            executor_state["paused_until"] = None
            executor_state["consecutive_losses"] = 0

    daily_limit = float(cfg("DAILY_LOSS_LIMIT", str(DEFAULT_DAILY_LOSS_LIMIT)))
    if executor_state.get("daily_pnl", 0) <= -daily_limit:
        return False, f"Daily loss limit hit: ${executor_state['daily_pnl']:.2f} / -${daily_limit:.2f}"

    consecutive = executor_state.get("consecutive_losses", 0)
    max_consecutive = int(cfg("CIRCUIT_BREAKER_MAX", str(DEFAULT_CIRCUIT_BREAKER)))
    if consecutive >= max_consecutive:
        pause_until = datetime.now(timezone.utc) + timedelta(hours=24)
        executor_state["paused_until"] = pause_until.isoformat()
        save_executor_state(executor_state)
        return False, f"Circuit breaker: {consecutive} consecutive losses, paused 24h"

    return True, "OK"


# ---------------------------------------------------------------------------
# Core execution logic
# ---------------------------------------------------------------------------

def process_new_signals(executor: BinanceExecutor, trading_state: dict,
                        executor_state: dict) -> int:
    """Find ACTIVE states without order_id and execute them. Returns count of new orders."""
    states = trading_state.get("states", {})
    executed = 0

    for coin, cs in states.items():
        if cs.get("state") != "ACTIVE":
            continue
        if cs.get("order_id"):
            continue
        if not cs.get("entry_price") or not cs.get("sl_price") or not cs.get("tp_price"):
            continue

        symbol = FUTURES_SYMBOL_MAP.get(coin)
        if not symbol:
            log.warning(f"No futures symbol mapping for {coin}")
            continue

        safe, reason = check_safety(executor_state, executor)
        if not safe:
            log.warning(f"Safety block for {coin}: {reason}")
            send_telegram(f"*[EXECUTOR] Trade blocked*\n{coin.upper()}: {reason}")
            break

        direction = cs["direction"]
        entry = cs["entry_price"]
        sl = cs["sl_price"]
        tp = cs["tp_price"]

        balance = float(cfg("PORTFOLIO_BALANCE", "100"))
        risk_pct = 2.0
        risk_per_unit = abs(entry - sl)
        if risk_per_unit == 0:
            log.error(f"Invalid SL for {coin}: entry={entry} sl={sl}")
            continue

        max_risk_usd = balance * (risk_pct / 100)
        qty = max_risk_usd / risk_per_unit

        log.info(f"Executing {coin.upper()} {direction}: entry≈{entry}, qty={qty:.6f}, SL={sl}, TP={tp}")

        result = executor.open_position(symbol, direction, qty, sl, tp)
        if result:
            cs.update(result)
            cs["executed_at"] = datetime.now(timezone.utc).isoformat()
            save_trading_state(trading_state)

            pnl_at_tp = abs(tp - result["fill_price"]) * result["fill_qty"]
            pnl_at_sl = abs(result["fill_price"] - sl) * result["fill_qty"]
            if direction == "SHORT":
                pnl_at_tp = abs(result["fill_price"] - tp) * result["fill_qty"]
                pnl_at_sl = abs(sl - result["fill_price"]) * result["fill_qty"]

            msg = (
                f"*[EXECUTED] {coin.upper()} {direction}*\n"
                f"Fill: *{fmt_price(result['fill_price'])}*\n"
                f"Qty: {result['fill_qty']:.6f}\n"
                f"SL: {fmt_price(sl)} (-${pnl_at_sl:.2f})\n"
                f"TP: {fmt_price(tp)} (+${pnl_at_tp:.2f})\n"
                f"Leverage: {executor.leverage}x\n"
                f"{'TESTNET' if executor.testnet else 'LIVE'}"
            )
            send_telegram(msg)
            executed += 1

            executor_state["total_trades"] = executor_state.get("total_trades", 0) + 1
            save_executor_state(executor_state)
        else:
            log.error(f"Failed to execute {coin.upper()} {direction}")
            send_telegram(f"*[EXEC FAILED] {coin.upper()} {direction}*\nCheck logs.")

    return executed


def check_position_status(executor: BinanceExecutor, trading_state: dict,
                          executor_state: dict) -> int:
    """Check exchange positions for fills/closes. Returns count of closed positions."""
    states = trading_state.get("states", {})
    exchange_positions = executor.get_open_positions()
    closed = 0

    skip_order_ids = {"synced_from_exchange", "manual_sync"}
    for coin, cs in states.items():
        if cs.get("state") != "ACTIVE" or not cs.get("order_id"):
            continue
        if cs.get("order_id") in skip_order_ids:
            continue

        symbol = FUTURES_SYMBOL_MAP.get(coin)
        if not symbol:
            continue

        if symbol in exchange_positions:
            continue

        # Position no longer on exchange — it was closed (TP/SL hit or manual)
        entry = cs.get("fill_price") or cs.get("entry_price", 0)
        direction = cs["direction"]
        fill_qty = cs.get("fill_qty", 0)

        # Try to get actual close price from recent trades
        close_price = 0
        try:
            trades = executor.client.futures_account_trades(symbol=symbol, limit=5)
            for t in reversed(trades):
                if t.get("side") != ("BUY" if direction == "LONG" else "SELL"):
                    close_price = float(t["price"])
                    break
        except Exception:
            pass

        if not close_price:
            try:
                ticker = executor.client.futures_symbol_ticker(symbol=symbol)
                close_price = float(ticker["price"])
            except Exception:
                pass

        if direction == "LONG":
            pnl = (close_price - entry) * fill_qty if close_price else 0
        else:
            pnl = (entry - close_price) * fill_qty if close_price else 0

        sl = cs.get("sl_price", 0)
        tp = cs.get("tp_price", 0)
        if close_price and sl and tp:
            if direction == "LONG":
                is_tp = close_price >= tp * 0.99
                is_sl = close_price <= sl * 1.01
            else:
                is_tp = close_price <= tp * 1.01
                is_sl = close_price >= sl * 0.99
        else:
            is_tp = pnl > 0
            is_sl = pnl <= 0

        new_state = "TP_HIT" if is_tp else "SL_HIT"
        cs["state"] = new_state
        cs["pnl_usd"] = round(pnl, 4)
        cs["closed_at"] = datetime.now(timezone.utc).isoformat()
        cs["close_price"] = close_price

        executor_state["daily_pnl"] = executor_state.get("daily_pnl", 0) + pnl
        executor_state["total_pnl"] = executor_state.get("total_pnl", 0) + pnl

        if is_sl:
            executor_state["consecutive_losses"] = executor_state.get("consecutive_losses", 0) + 1
        else:
            executor_state["consecutive_losses"] = 0

        history_entry = {
            "coin": coin,
            "direction": direction,
            "entry": entry,
            "close": close_price,
            "pnl": round(pnl, 4),
            "result": new_state,
            "time": datetime.now(timezone.utc).isoformat(),
        }
        executor_state.setdefault("trade_history", []).append(history_entry)
        if len(executor_state["trade_history"]) > 100:
            executor_state["trade_history"] = executor_state["trade_history"][-100:]

        save_trading_state(trading_state)
        save_executor_state(executor_state)

        emoji = "✅" if is_tp else "🔴"
        msg = (
            f"{emoji} *[{new_state}] {coin.upper()} {direction}*\n"
            f"Entry: {fmt_price(entry)} → Close: {fmt_price(close_price)}\n"
            f"P&L: *${pnl:+.2f}*\n"
            f"Daily P&L: ${executor_state['daily_pnl']:+.2f}\n"
            f"Consecutive losses: {executor_state['consecutive_losses']}\n"
            f"{'TESTNET' if executor.testnet else 'LIVE'}"
        )
        send_telegram(msg)
        closed += 1
        log.info(f"{new_state}: {coin.upper()} {direction} P&L=${pnl:+.2f}")

    return closed


def sync_positions(executor: BinanceExecutor, trading_state: dict):
    """On startup, reconcile exchange positions with local state."""
    exchange = executor.get_open_positions()
    states = trading_state.get("states", {})
    reverse_map = {v: k for k, v in FUTURES_SYMBOL_MAP.items()}

    for symbol, pos in exchange.items():
        coin = reverse_map.get(symbol)
        if not coin:
            log.warning(f"Unknown exchange position: {symbol}")
            continue

        if coin not in states:
            states[coin] = {}

        cs = states[coin]
        direction = "LONG" if pos["qty"] > 0 else "SHORT"

        if cs.get("state") == "ACTIVE" and cs.get("order_id"):
            log.info(f"Position {coin.upper()} already tracked locally")
            continue

        log.warning(f"Untracked exchange position found: {coin.upper()} {direction} qty={pos['qty']}")
        cs["state"] = "ACTIVE"
        cs["direction"] = direction
        cs["fill_price"] = pos["entry_price"]
        cs["fill_qty"] = abs(pos["qty"])
        cs["entry_price"] = pos["entry_price"]
        cs["order_id"] = "synced_from_exchange"
        cs["synced_at"] = datetime.now(timezone.utc).isoformat()

    skip_ids = {"synced_from_exchange", "manual_sync"}
    for coin, cs in states.items():
        if cs.get("state") != "ACTIVE" or not cs.get("order_id"):
            continue
        if cs.get("order_id") in skip_ids:
            continue
        symbol = FUTURES_SYMBOL_MAP.get(coin)
        if symbol and symbol not in exchange:
            log.warning(f"Local ACTIVE {coin.upper()} but no exchange position — marking SL_HIT")
            cs["state"] = "SL_HIT"

    trading_state["states"] = states
    save_trading_state(trading_state)
    log.info(f"Sync complete: {len(exchange)} exchange positions")


# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------

def run_once():
    load_dotenv()
    executor = BinanceExecutor()
    trading_state = load_trading_state()
    executor_state = load_executor_state()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if executor_state.get("daily_date") != today:
        executor_state["daily_pnl"] = 0.0
        executor_state["daily_date"] = today
        executor_state["consecutive_losses"] = 0
        save_executor_state(executor_state)

    closed = check_position_status(executor, trading_state, executor_state)
    if closed:
        log.info(f"Detected {closed} closed position(s)")

    safe, reason = check_safety(executor_state, executor)
    if safe:
        executed = process_new_signals(executor, trading_state, executor_state)
        if executed:
            log.info(f"Executed {executed} new trade(s)")
    else:
        log.info(f"Trading paused: {reason}")


def print_status():
    load_dotenv()
    executor = BinanceExecutor()
    executor_state = load_executor_state()
    trading_state = load_trading_state()

    balance = executor.get_futures_balance()
    positions = executor.get_open_positions()

    print(f"\n{'=' * 60}")
    print(f"  EXECUTOR STATUS ({'TESTNET' if executor.testnet else 'LIVE'})")
    print(f"{'=' * 60}")
    print(f"  Futures Balance: ${balance:,.2f}")
    print(f"  Leverage: {executor.leverage}x")
    print(f"  Daily P&L: ${executor_state.get('daily_pnl', 0):+.2f} / -${executor.daily_loss_limit:.0f} limit")
    print(f"  Consecutive losses: {executor_state.get('consecutive_losses', 0)} / {executor.circuit_breaker_max}")
    print(f"  Total trades: {executor_state.get('total_trades', 0)}")
    print(f"  Total P&L: ${executor_state.get('total_pnl', 0):+.2f}")

    safe, reason = check_safety(executor_state, executor)
    print(f"  Status: {'ACTIVE' if safe else f'PAUSED — {reason}'}")

    print(f"\n  Exchange Positions ({len(positions)}):")
    if positions:
        for sym, pos in positions.items():
            d = "LONG" if pos["qty"] > 0 else "SHORT"
            print(f"    {sym} {d} qty={pos['qty']:.6f} entry={pos['entry_price']:.4f} "
                  f"uPnL=${pos['unrealized_pnl']:+.4f}")
    else:
        print("    (none)")

    states = trading_state.get("states", {})
    active_local = {k: v for k, v in states.items() if v.get("state") == "ACTIVE" and v.get("order_id")}
    pending_exec = {k: v for k, v in states.items() if v.get("state") == "ACTIVE" and not v.get("order_id")}

    print(f"\n  Local ACTIVE with orders ({len(active_local)}):")
    for coin, cs in active_local.items():
        print(f"    {coin.upper()} {cs['direction']} entry={cs.get('fill_price', cs['entry_price']):.4f} "
              f"SL={cs['sl_price']:.4f} TP={cs['tp_price']:.4f}")

    print(f"\n  Pending execution ({len(pending_exec)}):")
    for coin, cs in pending_exec.items():
        print(f"    {coin.upper()} {cs['direction']} entry≈{cs['entry_price']:.4f}")

    recent = executor_state.get("trade_history", [])[-5:]
    if recent:
        print(f"\n  Recent trades:")
        for t in reversed(recent):
            emoji = "✅" if t["result"] == "TP_HIT" else "🔴"
            print(f"    {emoji} {t['coin'].upper()} {t['direction']} "
                  f"${t['pnl']:+.2f} ({t['result']}) @ {t['time'][:16]}")

    print()


def daemon_loop():
    load_dotenv()
    interval = int(cfg("EXEC_INTERVAL_SEC", str(DEFAULT_EXEC_INTERVAL)))
    testnet = cfg("BINANCE_TESTNET", "true").lower() in ("true", "1", "yes")

    log.info(f"Trade executor v1 — {'TESTNET' if testnet else 'LIVE'} — every {interval}s")
    log.info(f"Leverage: {min(int(cfg('FUTURES_LEVERAGE', str(DEFAULT_LEVERAGE))), MAX_LEVERAGE_CAP)}x")
    log.info(f"Daily loss limit: ${float(cfg('DAILY_LOSS_LIMIT', str(DEFAULT_DAILY_LOSS_LIMIT))):.0f}")
    log.info(f"Circuit breaker: {int(cfg('CIRCUIT_BREAKER_MAX', str(DEFAULT_CIRCUIT_BREAKER)))} consecutive losses")

    executor = BinanceExecutor()

    trading_state = load_trading_state()
    sync_positions(executor, trading_state)

    while True:
        try:
            trading_state = load_trading_state()
            executor_state = load_executor_state()

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if executor_state.get("daily_date") != today:
                executor_state["daily_pnl"] = 0.0
                executor_state["daily_date"] = today
                save_executor_state(executor_state)

            closed = check_position_status(executor, trading_state, executor_state)
            if closed:
                log.info(f"Detected {closed} closed position(s)")

            safe, reason = check_safety(executor_state, executor)
            if safe:
                executed = process_new_signals(executor, trading_state, executor_state)
                if executed:
                    log.info(f"Executed {executed} new trade(s)")

        except Exception as exc:
            log.error(f"Error in executor loop: {exc}", exc_info=True)

        time.sleep(interval)


def close_all():
    load_dotenv()
    executor = BinanceExecutor()
    count = executor.close_all_positions()
    print(f"Closed {count} positions")

    trading_state = load_trading_state()
    for coin, cs in trading_state.get("states", {}).items():
        if cs.get("state") == "ACTIVE":
            cs["state"] = "EMERGENCY_CLOSED"
            cs["closed_at"] = datetime.now(timezone.utc).isoformat()
    save_trading_state(trading_state)

    send_telegram(f"*[EMERGENCY] Closed {count} positions*\nAll trading paused.")
    os.environ["AUTO_TRADE_ENABLED"] = "false"


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        daemon_loop()
    elif "--status" in sys.argv:
        print_status()
    elif "--close-all" in sys.argv:
        close_all()
    else:
        run_once()
