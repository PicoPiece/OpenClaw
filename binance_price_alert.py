#!/usr/bin/env python3
"""
Binance Price Alert v3 — Hybrid Trading Signal System

Layer 1: Python rule-based signal generator with TA (RSI, EMA, SMA, ATR, Volume)
Layer 2: LLM reviews pending signals via OpenClaw cron (news + macro check)

Zero dependencies beyond Python stdlib.

Usage:
    python3 binance_price_alert.py              # one-shot check
    python3 binance_price_alert.py --daemon     # run every CHECK_INTERVAL_SEC
    python3 binance_price_alert.py --status     # show current indicators + signals

Environment (reads from .env in same directory):
    TELEGRAM_BOT_TOKEN     required
    TELEGRAM_CHAT_ID       required
    CHECK_INTERVAL_SEC     optional  (default 60)
    ALERT_COOLDOWN_MIN     optional  (default 30)
    SIGNAL_COOLDOWN_H      optional  (default 4, hours between signals for same coin)
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_FILE = SCRIPT_DIR / "data" / "price_alert_state.json"
TRADING_STATE_FILE = SCRIPT_DIR / "data" / "workspace-finance" / "trading_state.json"
PENDING_SIGNAL_FILE = SCRIPT_DIR / "data" / "pending_signal.json"
ENV_FILE = SCRIPT_DIR / ".env"

BINANCE_PRICE_API = "https://api.binance.com/api/v3/ticker/price"
BINANCE_KLINES_API = "https://api.binance.com/api/v3/klines"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
SYMBOL_MAP = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "bnb": "BNBUSDT"}

BREAKOUT_LEVELS = {
    "sol": {"breakout": 85.0, "breakdown": 80.0},
    "bnb": {"breakout": 650.0, "breakdown": 580.0},
}
REVERSAL_PCT = 0.03

RSI_PERIOD = 14
EMA_PERIOD = 20
SMA_PERIOD = 50
ATR_PERIOD = 14
KLINES_LIMIT = 55
KLINES_INTERVAL = "1h"
VOLUME_CONFIRM_RATIO = 1.5
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30

ATR_SL_MULT = 1.5
ATR_TP_MULT = 2.0


# ---------------------------------------------------------------------------
# Technical Analysis (pure math, no dependencies)
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


def calc_ema(closes: list, period: int = EMA_PERIOD) -> float | None:
    if len(closes) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


def calc_sma(closes: list, period: int = SMA_PERIOD) -> float | None:
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def calc_atr(highs: list, lows: list, closes: list, period: int = ATR_PERIOD) -> float | None:
    if len(closes) < period + 1:
        return None
    true_ranges = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        true_ranges.append(tr)
    return sum(true_ranges[-period:]) / period


def calc_volume_ratio(volumes: list, period: int = 20) -> float | None:
    if len(volumes) < period + 1:
        return None
    avg_vol = sum(volumes[-(period + 1):-1]) / period
    if avg_vol == 0:
        return None
    return volumes[-1] / avg_vol


def detect_trend(price: float, ema: float | None, sma: float | None) -> str:
    if ema is None or sma is None:
        return "UNKNOWN"
    if price > ema > sma:
        return "UPTREND"
    if price < ema < sma:
        return "DOWNTREND"
    if price > sma:
        return "SIDEWAYS-BULL"
    return "SIDEWAYS-BEAR"


def fetch_klines(symbol: str) -> dict:
    url = f"{BINANCE_KLINES_API}?symbol={symbol}&interval={KLINES_INTERVAL}&limit={KLINES_LIMIT}"
    req = urllib.request.Request(url, headers={"User-Agent": "PicoAlerts/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = json.loads(resp.read())

    closes = [float(k[4]) for k in raw]
    volumes = [float(k[5]) for k in raw]
    highs = [float(k[2]) for k in raw]
    lows = [float(k[3]) for k in raw]

    rsi_now = calc_rsi(closes, RSI_PERIOD)
    rsi_prev = calc_rsi(closes[:-1], RSI_PERIOD) if len(closes) > RSI_PERIOD + 1 else None
    ema = calc_ema(closes, EMA_PERIOD)
    sma = calc_sma(closes, SMA_PERIOD)
    atr = calc_atr(highs, lows, closes, ATR_PERIOD)
    vol_ratio = calc_volume_ratio(volumes)
    price = closes[-1]
    trend = detect_trend(price, ema, sma)

    h24 = max(highs[-24:]) if len(highs) >= 24 else max(highs)
    l24 = min(lows[-24:]) if len(lows) >= 24 else min(lows)

    return {
        "price": price,
        "rsi": rsi_now,
        "rsi_prev": rsi_prev,
        "ema20": ema,
        "sma50": sma,
        "atr": atr,
        "vol_ratio": vol_ratio,
        "trend": trend,
        "high_24h": h24,
        "low_24h": l24,
    }


def fetch_all_indicators() -> dict:
    result = {}
    for sym in SYMBOLS:
        try:
            result[sym] = fetch_klines(sym)
        except Exception as exc:
            print(f"  [WARN] Failed to fetch klines for {sym}: {exc}")
    return result


def format_indicator_line(ind: dict, price: float) -> str:
    parts = []
    if ind.get("rsi") is not None:
        rsi = ind["rsi"]
        tag = " OB" if rsi >= RSI_OVERBOUGHT else (" OS" if rsi <= RSI_OVERSOLD else "")
        parts.append(f"RSI:{rsi:.0f}{tag}")
    if ind.get("ema20") is not None:
        parts.append(f"EMA20:${ind['ema20']:,.0f}")
    if ind.get("sma50") is not None:
        parts.append(f"SMA50:${ind['sma50']:,.0f}")
    if ind.get("atr") is not None:
        parts.append(f"ATR:${ind['atr']:,.0f}")
    if ind.get("vol_ratio") is not None:
        vr = ind["vol_ratio"]
        tag = " HIGH" if vr >= VOLUME_CONFIRM_RATIO else ""
        parts.append(f"Vol:{vr:.1f}x{tag}")
    if ind.get("trend"):
        parts.append(ind["trend"])
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Signal Generation (Rule Engine)
# ---------------------------------------------------------------------------

def load_pending_signal() -> dict | None:
    if PENDING_SIGNAL_FILE.exists():
        data = json.loads(PENDING_SIGNAL_FILE.read_text())
        if data.get("status") == "pending_review":
            return data
    return None


def save_pending_signal(signal: dict):
    PENDING_SIGNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_SIGNAL_FILE.write_text(json.dumps(signal, indent=2))


def clear_pending_signal():
    if PENDING_SIGNAL_FILE.exists():
        expired = json.loads(PENDING_SIGNAL_FILE.read_text())
        expired["status"] = "expired"
        PENDING_SIGNAL_FILE.write_text(json.dumps(expired, indent=2))


def is_signal_on_cooldown(alert_state: dict, coin: str, cooldown_h: int) -> bool:
    key = f"signal_{coin}"
    last_ts = alert_state.get("alerted", {}).get(key)
    if last_ts is None:
        return False
    return (time.time() - last_ts) < cooldown_h * 3600


def generate_signals(prices: dict, indicators: dict, trading_state: dict,
                     alert_state: dict, cooldown_h: int) -> list:
    """Rule engine: generate LONG/SHORT signals based on TA confirmation."""
    signals = []
    states = trading_state.get("states", {})

    for coin, symbol in SYMBOL_MAP.items():
        if symbol not in prices or symbol not in indicators:
            continue

        coin_state = states.get(coin, {})
        has_position = bool(coin_state.get("entry_price"))
        current_state = coin_state.get("state", "")

        if has_position and current_state not in ("TP_HIT", "SL_HIT"):
            continue

        if is_signal_on_cooldown(alert_state, coin, cooldown_h):
            continue

        price = prices[symbol]
        ind = indicators[symbol]
        rsi = ind.get("rsi")
        rsi_prev = ind.get("rsi_prev")
        ema = ind.get("ema20")
        sma = ind.get("sma50")
        atr = ind.get("atr")
        vol_ratio = ind.get("vol_ratio")
        trend = ind.get("trend", "UNKNOWN")

        if rsi is None or ema is None or sma is None or atr is None or vol_ratio is None:
            continue

        # --- LONG rules (all must pass) ---
        rsi_cross_up = (rsi_prev is not None and rsi_prev < RSI_OVERSOLD and rsi >= RSI_OVERSOLD)
        rsi_bullish = rsi > 50 and (rsi_prev is not None and rsi > rsi_prev)
        rsi_long_ok = rsi_cross_up or rsi_bullish
        price_above_ema = price > ema
        price_above_sma = price > sma
        vol_ok = vol_ratio >= VOLUME_CONFIRM_RATIO

        if rsi_long_ok and price_above_ema and price_above_sma and vol_ok:
            entry = round(price, 2)
            sl = round(entry - ATR_SL_MULT * atr, 2)
            tp = round(entry + ATR_TP_MULT * atr, 2)
            signals.append({
                "coin": coin,
                "symbol": symbol,
                "direction": "LONG",
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "atr": round(atr, 2),
                "rsi": round(rsi, 1),
                "rsi_prev": round(rsi_prev, 1) if rsi_prev else None,
                "ema20": round(ema, 2),
                "sma50": round(sma, 2),
                "vol_ratio": round(vol_ratio, 2),
                "trend": trend,
                "rr_ratio": round(ATR_TP_MULT / ATR_SL_MULT, 2),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "pending_review",
            })

        # --- SHORT rules (all must pass) ---
        rsi_cross_down = (rsi_prev is not None and rsi_prev > RSI_OVERBOUGHT and rsi <= RSI_OVERBOUGHT)
        rsi_bearish = rsi < 50 and (rsi_prev is not None and rsi < rsi_prev)
        rsi_short_ok = rsi_cross_down or rsi_bearish
        price_below_ema = price < ema
        price_below_sma = price < sma

        if rsi_short_ok and price_below_ema and price_below_sma and vol_ok:
            entry = round(price, 2)
            sl = round(entry + ATR_SL_MULT * atr, 2)
            tp = round(entry - ATR_TP_MULT * atr, 2)
            signals.append({
                "coin": coin,
                "symbol": symbol,
                "direction": "SHORT",
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "atr": round(atr, 2),
                "rsi": round(rsi, 1),
                "rsi_prev": round(rsi_prev, 1) if rsi_prev else None,
                "ema20": round(ema, 2),
                "sma50": round(sma, 2),
                "vol_ratio": round(vol_ratio, 2),
                "trend": trend,
                "rr_ratio": round(ATR_TP_MULT / ATR_SL_MULT, 2),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "pending_review",
            })

    return signals


# ---------------------------------------------------------------------------
# Core alert logic (TP/SL/breakout — unchanged from v2)
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


def fetch_prices() -> dict:
    symbols_json = json.dumps(SYMBOLS, separators=(",", ":"))
    url = f"{BINANCE_PRICE_API}?symbols={symbols_json}"
    req = urllib.request.Request(url, headers={"User-Agent": "PicoAlerts/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    return {item["symbol"]: float(item["price"]) for item in data}


def load_trading_state() -> dict:
    if TRADING_STATE_FILE.exists():
        return json.loads(TRADING_STATE_FILE.read_text())
    return {}


def load_alert_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"alerted": {}, "last_prices": {}}


def save_alert_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def send_telegram(token: str, chat_id: str, text: str):
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        print(f"[WARN] Telegram send failed: {exc.code} {exc.read().decode()}")
        return None


def is_on_cooldown(alert_state: dict, alert_key: str, cooldown_min: int) -> bool:
    last_ts = alert_state.get("alerted", {}).get(alert_key)
    if last_ts is None:
        return False
    return (time.time() - last_ts) < cooldown_min * 60


def mark_alerted(alert_state: dict, alert_key: str):
    alert_state.setdefault("alerted", {})[alert_key] = time.time()


def check_conditions(prices: dict, indicators: dict, trading_state: dict,
                     alert_state: dict, cooldown_min: int) -> list:
    alerts = []
    states = trading_state.get("states", {})

    for coin, coin_state in states.items():
        symbol = SYMBOL_MAP.get(coin)
        if not symbol or symbol not in prices:
            continue

        price = prices[symbol]
        ind = indicators.get(symbol, {})
        entry = coin_state.get("entry_price")
        tp = coin_state.get("tp_price")
        sl = coin_state.get("sl_price")
        direction = coin_state.get("direction")
        state = coin_state.get("state", "")

        rsi = ind.get("rsi")
        vol_ratio = ind.get("vol_ratio")
        ind_line = format_indicator_line(ind, price) if ind else ""

        if state in ("TP_HIT", "SL_HIT") and coin_state.get("user_confirmed"):
            continue

        if entry and tp and sl and direction:
            if direction == "LONG":
                if price >= tp:
                    key = f"{coin}_tp_hit"
                    if not is_on_cooldown(alert_state, key, cooldown_min):
                        rsi_note = ""
                        if rsi and rsi >= RSI_OVERBOUGHT:
                            rsi_note = "\nRSI overbought — strong take-profit signal."
                        elif rsi and rsi >= 60:
                            rsi_note = "\nRSI still bullish — partial take-profit OK."
                        alerts.append(
                            f"*{coin.upper()} TP HIT*\n"
                            f"Price: *${price:,.2f}* >= TP ${tp:,.2f}\n"
                            f"Entry: ${entry:,.2f} | P&L: *+{((price - entry) / entry * 100):.1f}%*\n"
                            f"{ind_line}{rsi_note}"
                        )
                        mark_alerted(alert_state, key)
                elif price <= sl:
                    key = f"{coin}_sl_hit"
                    if not is_on_cooldown(alert_state, key, cooldown_min):
                        alerts.append(
                            f"*{coin.upper()} SL HIT*\n"
                            f"Price: *${price:,.2f}* <= SL ${sl:,.2f}\n"
                            f"Entry: ${entry:,.2f} | P&L: *{((price - entry) / entry * 100):.1f}%*\n"
                            f"{ind_line}\nExit position."
                        )
                        mark_alerted(alert_state, key)
                elif price < entry * (1 - REVERSAL_PCT):
                    key = f"{coin}_reversal"
                    if not is_on_cooldown(alert_state, key, cooldown_min):
                        alerts.append(
                            f"*{coin.upper()} REVERSAL WARNING*\n"
                            f"Price: *${price:,.2f}* ({((price - entry) / entry * 100):.1f}% from entry)\n"
                            f"Entry: ${entry:,.2f} | SL: ${sl:,.2f}\n"
                            f"{ind_line}"
                        )
                        mark_alerted(alert_state, key)
            elif direction == "SHORT":
                if price <= tp:
                    key = f"{coin}_tp_hit"
                    if not is_on_cooldown(alert_state, key, cooldown_min):
                        alerts.append(
                            f"*{coin.upper()} TP HIT (SHORT)*\n"
                            f"Price: *${price:,.2f}* <= TP ${tp:,.2f}\n"
                            f"Entry: ${entry:,.2f} | P&L: *+{((entry - price) / entry * 100):.1f}%*\n"
                            f"{ind_line}"
                        )
                        mark_alerted(alert_state, key)
                elif price >= sl:
                    key = f"{coin}_sl_hit"
                    if not is_on_cooldown(alert_state, key, cooldown_min):
                        alerts.append(
                            f"*{coin.upper()} SL HIT (SHORT)*\n"
                            f"Price: *${price:,.2f}* >= SL ${sl:,.2f}\n"
                            f"Entry: ${entry:,.2f} | P&L: *{((entry - price) / entry * 100):.1f}%*\n"
                            f"{ind_line}\nExit position."
                        )
                        mark_alerted(alert_state, key)
                elif price > entry * (1 + REVERSAL_PCT):
                    key = f"{coin}_reversal"
                    if not is_on_cooldown(alert_state, key, cooldown_min):
                        alerts.append(
                            f"*{coin.upper()} REVERSAL WARNING (SHORT)*\n"
                            f"Price: *${price:,.2f}* (+{((price - entry) / entry * 100):.1f}% from entry)\n"
                            f"{ind_line}"
                        )
                        mark_alerted(alert_state, key)

        levels = BREAKOUT_LEVELS.get(coin)
        if levels and not entry:
            bo = levels["breakout"]
            bd = levels["breakdown"]
            rsi_bull = rsi is not None and rsi > 50
            rsi_bear = rsi is not None and rsi < 50
            vol_ok = vol_ratio is not None and vol_ratio >= VOLUME_CONFIRM_RATIO

            if price > bo:
                confirmed = rsi_bull and vol_ok
                key = f"{coin}_breakout" + ("_confirmed" if confirmed else "_weak")
                if not is_on_cooldown(alert_state, key, cooldown_min):
                    strength = "CONFIRMED" if confirmed else "WEAK (low vol or RSI)"
                    alerts.append(
                        f"*{coin.upper()} BREAKOUT — {strength}*\n"
                        f"Price: *${price:,.2f}* > ${bo:,.2f}\n"
                        f"{ind_line}\n"
                        + ("Entry signal. RSI + volume confirm." if confirmed
                           else "Caution: wait for volume/RSI confirmation.")
                    )
                    mark_alerted(alert_state, key)
            elif price < bd:
                confirmed = rsi_bear and vol_ok
                key = f"{coin}_breakdown" + ("_confirmed" if confirmed else "_weak")
                if not is_on_cooldown(alert_state, key, cooldown_min):
                    strength = "CONFIRMED" if confirmed else "WEAK"
                    alerts.append(
                        f"*{coin.upper()} BREAKDOWN — {strength}*\n"
                        f"Price: *${price:,.2f}* < ${bd:,.2f}\n"
                        f"{ind_line}\n"
                        + ("Avoid longs. Downtrend confirmed." if confirmed
                           else "Possible fake breakdown. Wait for confirmation.")
                    )
                    mark_alerted(alert_state, key)

    return alerts


# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------

def run_once():
    load_dotenv()

    tg_token = cfg("TELEGRAM_BOT_TOKEN")
    tg_chat = cfg("TELEGRAM_CHAT_ID")
    cooldown_min = int(cfg("ALERT_COOLDOWN_MIN", "30"))
    signal_cooldown_h = int(cfg("SIGNAL_COOLDOWN_H", "4"))

    if not tg_token or not tg_chat:
        sys.exit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")

    prices = fetch_prices()
    indicators = fetch_all_indicators()
    trading_state = load_trading_state()
    alert_state = load_alert_state()

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    price_summary = " | ".join(
        f"{s.replace('USDT', '')}: ${p:,.2f}" for s, p in sorted(prices.items())
    )
    print(f"[{now_str}] {price_summary}")

    # --- Price alerts (TP/SL/breakout) ---
    alerts = check_conditions(prices, indicators, trading_state, alert_state, cooldown_min)
    if alerts:
        header = f"*Crypto Price Alert* — {now_str}\n"
        msg = header + "\n---\n".join(alerts)
        print(f"  ALERT: {len(alerts)} condition(s) triggered")
        send_telegram(tg_token, tg_chat, msg)
    else:
        print("  No alerts.")

    # --- Signal generation (rule engine) ---
    existing_pending = load_pending_signal()
    if existing_pending:
        age_h = (time.time() - datetime.fromisoformat(existing_pending["timestamp"]).timestamp()) / 3600
        if age_h > 4:
            print(f"  Expiring stale pending signal ({existing_pending['coin'].upper()} {existing_pending['direction']}, {age_h:.1f}h old)")
            clear_pending_signal()
            existing_pending = None
        else:
            print(f"  Pending signal exists: {existing_pending['coin'].upper()} {existing_pending['direction']} (waiting LLM review)")

    if not existing_pending:
        signals = generate_signals(prices, indicators, trading_state, alert_state, signal_cooldown_h)
        if signals:
            best = signals[0]
            save_pending_signal(best)
            mark_alerted(alert_state, f"signal_{best['coin']}")

            coin_u = best["coin"].upper()
            msg = (
                f"*[PENDING SIGNAL] {coin_u} {best['direction']}*\n"
                f"Entry: *${best['entry']:,.2f}*\n"
                f"SL: ${best['sl']:,.2f} ({ATR_SL_MULT}x ATR)\n"
                f"TP: ${best['tp']:,.2f} ({ATR_TP_MULT}x ATR)\n"
                f"R/R: {best['rr_ratio']}\n"
                f"RSI:{best['rsi']:.0f} | EMA20:${best['ema20']:,.0f} | SMA50:${best['sma50']:,.0f}\n"
                f"Vol:{best['vol_ratio']:.1f}x | ATR:${best['atr']:,.0f} | {best['trend']}\n\n"
                f"_Awaiting LLM news review (next cron cycle)_"
            )
            print(f"  SIGNAL: {coin_u} {best['direction']} @ ${best['entry']:,.2f}")
            send_telegram(tg_token, tg_chat, msg)
        else:
            print("  No signals.")

    alert_state["last_prices"] = {s: p for s, p in prices.items()}
    alert_state["last_check"] = now_str
    save_alert_state(alert_state)


def print_status():
    load_dotenv()
    print("Fetching indicators...\n")
    indicators = fetch_all_indicators()
    prices = fetch_prices()
    trading_state = load_trading_state()
    states = trading_state.get("states", {})

    for coin, symbol in sorted(SYMBOL_MAP.items()):
        price = prices.get(symbol, 0)
        ind = indicators.get(symbol, {})
        cs = states.get(coin, {})
        position = cs.get("direction") or "NO_POSITION"
        state_label = cs.get("state", "WATCHING")

        print(f"{'=' * 55}")
        print(f"  {coin.upper()} — ${price:,.2f}  [{state_label}] [{position}]")
        print(f"  {format_indicator_line(ind, price)}")

        if ind.get("high_24h"):
            print(f"  24h Range: ${ind['low_24h']:,.2f} — ${ind['high_24h']:,.2f}")

        entry = cs.get("entry_price")
        if entry and position != "NO_POSITION":
            pnl = ((price - entry) / entry * 100)
            if position == "SHORT":
                pnl = ((entry - price) / entry * 100)
            print(f"  Entry: ${entry:,.2f} | TP: ${cs.get('tp_price', 0):,.2f} | SL: ${cs.get('sl_price', 0):,.2f} | P&L: {pnl:+.1f}%")

        levels = BREAKOUT_LEVELS.get(coin)
        if levels and not entry:
            print(f"  Breakout: >${levels['breakout']:,.2f} | Breakdown: <${levels['breakdown']:,.2f}")

        # Signal eligibility check
        rsi = ind.get("rsi")
        ema = ind.get("ema20")
        sma = ind.get("sma50")
        vr = ind.get("vol_ratio")
        atr_val = ind.get("atr")
        if rsi and ema and sma and vr and atr_val:
            rsi_prev = ind.get("rsi_prev")
            rsi_cross_up = rsi_prev is not None and rsi_prev < RSI_OVERSOLD and rsi >= RSI_OVERSOLD
            rsi_bullish = rsi > 50 and rsi_prev is not None and rsi > rsi_prev
            rsi_ok = rsi_cross_up or rsi_bullish
            long_ema = price > ema
            long_sma = price > sma
            long_vol = vr >= VOLUME_CONFIRM_RATIO
            rsi_tag = "cross↑30" if rsi_cross_up else (f"↑{rsi_prev:.0f}→{rsi:.0f}" if rsi_bullish else f"✗{rsi_prev and f'{rsi_prev:.0f}' or '?'}→{rsi:.0f}")
            checks = [
                f"RSI:{rsi_tag}:{'Y' if rsi_ok else 'N'}",
                f"P>EMA:{'Y' if long_ema else 'N'}",
                f"P>SMA:{'Y' if long_sma else 'N'}",
                f"Vol>1.5x:{'Y' if long_vol else 'N'}",
            ]
            all_pass = rsi_ok and long_ema and long_sma and long_vol
            print(f"  LONG signal: {' | '.join(checks)} → {'PASS' if all_pass else 'NO'}")
            if atr_val:
                print(f"  If LONG: SL=${price - ATR_SL_MULT * atr_val:,.0f} | TP=${price + ATR_TP_MULT * atr_val:,.0f} (ATR=${atr_val:,.0f})")

        print()

    pending = load_pending_signal()
    if pending:
        print(f"{'=' * 55}")
        print(f"  PENDING SIGNAL: {pending['coin'].upper()} {pending['direction']} @ ${pending['entry']:,.2f}")
        print(f"  SL: ${pending['sl']:,.2f} | TP: ${pending['tp']:,.2f} | R/R: {pending['rr_ratio']}")
        print(f"  Status: {pending['status']} | Time: {pending['timestamp']}")
        print()


def daemon_loop():
    interval = int(cfg("CHECK_INTERVAL_SEC", "60"))
    print(f"[daemon] Binance price alert v3 (hybrid signals) — every {interval}s")
    print(f"[daemon] TA: RSI({RSI_PERIOD}), EMA({EMA_PERIOD}), SMA({SMA_PERIOD}), ATR({ATR_PERIOD})")
    print(f"[daemon] Signal: SL={ATR_SL_MULT}xATR, TP={ATR_TP_MULT}xATR, R/R={ATR_TP_MULT/ATR_SL_MULT:.2f}")
    print(f"[daemon] Trading state: {TRADING_STATE_FILE}")
    while True:
        try:
            run_once()
        except Exception as exc:
            print(f"[ERROR] {exc}")
        time.sleep(interval)


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        daemon_loop()
    elif "--status" in sys.argv:
        print_status()
    else:
        run_once()
