#!/usr/bin/env python3
"""
Binance Price Alert v4 — Hybrid Trading Signal System

Layer 1: Python rule-based signal generator with TA (RSI, EMA20/EMA50 cross, ATR, Volume)
Layer 2: LLM reviews pending signals (real-time DeepSeek review before execution)

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
TRADING_CONTROL_FILE = SCRIPT_DIR / "data" / "workspace-finance" / "trading_control.json"
ENV_FILE = SCRIPT_DIR / ".env"

BINANCE_PRICE_API = "https://api.binance.com/api/v3/ticker/price"
BINANCE_KLINES_API = "https://api.binance.com/api/v3/klines"
BINANCE_24H_API = "https://api.binance.com/api/v3/ticker/24hr"

CORE_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
TOP_N_COINS = 20
MIN_VOLUME_USD = 50_000_000  # raised from 20M for higher quality
STABLECOIN_BLACKLIST = {"USDC", "USDT", "FDUSD", "TUSD", "DAI", "BUSD", "USD1", "RLUSD",
                         "USDP", "PYUSD", "AEUR", "EURI", "EUROC", "EUR", "GBP", "BRL",
                         "PAXG", "WBTC", "WBETH", "STETH", "CBBTC", "WETH", "U"}
# Filter out high-volatility meme/lowcap coins prone to wicks and pump/dump.
# These can still be CORE_SYMBOLS overridden — blacklist applies only to auto-discovery.
MEME_BLACKLIST = {
    "PEPE", "1000PEPE", "SHIB", "1000SHIB", "FLOKI", "1000FLOKI",
    "BONK", "1000BONK", "WIF", "DOGE", "MEME", "POPCAT", "BOME",
    "NEIRO", "1000NEIRO", "TURBO", "BRETT", "MEW", "BOOK", "TRUMP",
    "PNUT", "GOAT", "ACT", "MOODENG", "BAN", "FARTCOIN", "CHILLGUY",
    "1000SATS", "1000RATS", "PONKE", "MOG", "DEGEN",
}
SYMBOL_CACHE_FILE = SCRIPT_DIR / "data" / "top_coins_cache.json"
SYMBOL_CACHE_TTL = 3600

SYMBOLS: list[str] = []
SYMBOL_MAP: dict[str, str] = {}

REVERSAL_PCT = 0.03

RSI_PERIOD = 14
EMA_FAST_PERIOD = 20
EMA_SLOW_PERIOD = 50
ATR_PERIOD = 14
KLINES_LIMIT = 60
KLINES_INTERVAL = "1h"
ATR_KLINES_INTERVAL = "4h"
ATR_KLINES_LIMIT = 20
DAILY_KLINES_DAYS = 7
VOLUME_CONFIRM_RATIO = 1.2
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
RSI_LONG_MIN = 45
RSI_SHORT_MAX = 55
RSI_MOMENTUM_DELTA = 3

ATR_SL_MULT = 2.0
ATR_TP_MULT = 3.5  # R:R 1.75 — buffer for 0.08% round-trip fee

# --- Portfolio Risk Management ---
PORTFOLIO_BALANCE = float(os.environ.get("PORTFOLIO_BALANCE", "1000"))
RISK_PER_TRADE_PCT = 2.0      # max 2% of portfolio risked per trade
MAX_PORTFOLIO_RISK_PCT = 10.0  # max 10% total open risk across all positions


# ---------------------------------------------------------------------------
# Auto-discover top coins by volume
# ---------------------------------------------------------------------------

def discover_top_coins() -> tuple[list[str], dict[str, str]]:
    """Fetch top N coins by 24h USDT volume from Binance. Uses cache to avoid hammering API."""
    global SYMBOLS, SYMBOL_MAP

    if SYMBOL_CACHE_FILE.exists():
        cache = json.loads(SYMBOL_CACHE_FILE.read_text())
        age = time.time() - cache.get("timestamp", 0)
        if age < SYMBOL_CACHE_TTL and cache.get("symbols"):
            SYMBOLS = cache["symbols"]
            SYMBOL_MAP = cache["symbol_map"]
            return SYMBOLS, SYMBOL_MAP

    try:
        req = urllib.request.Request(BINANCE_24H_API, headers={"User-Agent": "PicoAlerts/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())

        usdt_pairs = []
        for d in data:
            sym = d["symbol"]
            if not sym.endswith("USDT"):
                continue
            if not sym.isascii():
                continue
            base = sym.replace("USDT", "")
            if base in STABLECOIN_BLACKLIST:
                continue
            if base in MEME_BLACKLIST and sym not in CORE_SYMBOLS:
                continue
            if len(base) > 10:
                continue
            # Skip leveraged tokens (UP/DOWN/BULL/BEAR suffixes)
            if any(base.endswith(suf) for suf in ("UP", "DOWN", "BULL", "BEAR")) and base not in {"FUEL", "JUP"}:
                continue
            vol = float(d["quoteVolume"])
            if vol < MIN_VOLUME_USD:
                continue
            usdt_pairs.append((sym, base.lower(), vol))

        usdt_pairs.sort(key=lambda x: x[2], reverse=True)

        for core in CORE_SYMBOLS:
            if not any(p[0] == core for p in usdt_pairs):
                base = core.replace("USDT", "").lower()
                usdt_pairs.append((core, base, 0))

        selected = []
        seen = set()
        for sym, base, vol in usdt_pairs:
            if sym in CORE_SYMBOLS and sym not in seen:
                selected.append((sym, base, vol))
                seen.add(sym)
        for sym, base, vol in usdt_pairs:
            if sym not in seen and len(selected) < TOP_N_COINS:
                selected.append((sym, base, vol))
                seen.add(sym)

        symbols = [s[0] for s in selected]
        symbol_map = {s[1]: s[0] for s in selected}

        SYMBOL_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        SYMBOL_CACHE_FILE.write_text(json.dumps({
            "timestamp": time.time(),
            "symbols": symbols,
            "symbol_map": symbol_map,
            "details": [{"symbol": s[0], "coin": s[1], "volume_24h": round(s[2])} for s in selected],
        }, indent=2))

    except Exception as exc:
        print(f"  [WARN] Failed to discover coins: {exc}, using core set")
        symbols = CORE_SYMBOLS
        symbol_map = {s.replace("USDT", "").lower(): s for s in CORE_SYMBOLS}

    SYMBOLS = symbols
    SYMBOL_MAP = symbol_map
    return symbols, symbol_map


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


def calc_ema(closes: list, period: int = EMA_FAST_PERIOD) -> float | None:
    if len(closes) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


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


def detect_trend(price: float, ema_fast: float | None, ema_slow: float | None) -> str:
    """Strict trend detection using EMA20/EMA50 crossover.
    Only UPTREND and DOWNTREND are valid for signal generation."""
    if ema_fast is None or ema_slow is None:
        return "UNKNOWN"
    if ema_fast > ema_slow and price > ema_fast:
        return "UPTREND"
    if ema_fast < ema_slow and price < ema_fast:
        return "DOWNTREND"
    if ema_fast > ema_slow:
        return "NEUTRAL-BULL"
    if ema_fast < ema_slow:
        return "NEUTRAL-BEAR"
    return "NEUTRAL"


def calc_dynamic_levels(symbol: str) -> dict:
    """Calculate breakout/breakdown from 7-day daily high/low."""
    url = f"{BINANCE_KLINES_API}?symbol={symbol}&interval=1d&limit={DAILY_KLINES_DAYS}"
    req = urllib.request.Request(url, headers={"User-Agent": "PicoAlerts/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = json.loads(resp.read())

    highs = [float(k[2]) for k in raw]
    lows = [float(k[3]) for k in raw]
    closes = [float(k[4]) for k in raw]

    high_7d = max(highs)
    low_7d = min(lows)
    atr = calc_atr(highs, lows, closes) if len(closes) > ATR_PERIOD else None

    buffer = atr * 0.3 if atr else (high_7d - low_7d) * 0.02
    breakout = round(high_7d + buffer, 2)
    breakdown = round(low_7d - buffer, 2)

    return {"breakout": breakout, "breakdown": breakdown, "high_7d": high_7d, "low_7d": low_7d}


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
    ema_fast = calc_ema(closes, EMA_FAST_PERIOD)
    ema_slow = calc_ema(closes, EMA_SLOW_PERIOD)
    atr_1h = calc_atr(highs, lows, closes, ATR_PERIOD)
    vol_ratio = calc_volume_ratio(volumes)
    price = closes[-1]
    trend = detect_trend(price, ema_fast, ema_slow)

    h24 = max(highs[-24:]) if len(highs) >= 24 else max(highs)
    l24 = min(lows[-24:]) if len(lows) >= 24 else min(lows)

    # 4h ATR for SL/TP + 4h EMA cross for trend confirmation
    atr_4h = atr_1h
    ema_fast_4h = None
    ema_slow_4h = None
    try:
        url4h = f"{BINANCE_KLINES_API}?symbol={symbol}&interval={ATR_KLINES_INTERVAL}&limit=60"
        req4h = urllib.request.Request(url4h, headers={"User-Agent": "PicoAlerts/1.0"})
        with urllib.request.urlopen(req4h, timeout=10) as resp4h:
            raw4h = json.loads(resp4h.read())
        h4h = [float(k[2]) for k in raw4h]
        l4h = [float(k[3]) for k in raw4h]
        c4h = [float(k[4]) for k in raw4h]
        atr_4h = calc_atr(h4h, l4h, c4h, ATR_PERIOD) or atr_1h
        ema_fast_4h = calc_ema(c4h, EMA_FAST_PERIOD)
        ema_slow_4h = calc_ema(c4h, EMA_SLOW_PERIOD)
    except Exception:
        pass

    if ema_fast_4h is not None and ema_slow_4h is not None:
        ema_cross_4h = "BULLISH" if ema_fast_4h > ema_slow_4h else "BEARISH"
    else:
        ema_cross_4h = "UNKNOWN"

    try:
        levels = calc_dynamic_levels(symbol)
    except Exception:
        levels = {"breakout": h24 * 1.02, "breakdown": l24 * 0.98, "high_7d": h24, "low_7d": l24}

    ema_gap_pct = ((ema_fast - ema_slow) / ema_slow * 100) if ema_fast and ema_slow and ema_slow != 0 else 0

    return {
        "price": price,
        "rsi": rsi_now,
        "rsi_prev": rsi_prev,
        "ema20": ema_fast,
        "ema50": ema_slow,
        "ema_cross": "BULLISH" if (ema_fast and ema_slow and ema_fast > ema_slow) else "BEARISH",
        "ema_cross_4h": ema_cross_4h,
        "ema20_4h": ema_fast_4h,
        "ema50_4h": ema_slow_4h,
        "ema_gap_pct": round(ema_gap_pct, 3),
        "atr": atr_4h,
        "atr_1h": atr_1h,
        "vol_ratio": vol_ratio,
        "trend": trend,
        "high_24h": h24,
        "low_24h": l24,
        "breakout": levels["breakout"],
        "breakdown": levels["breakdown"],
        "high_7d": levels["high_7d"],
        "low_7d": levels["low_7d"],
    }


def fetch_all_indicators() -> dict:
    result = {}
    for sym in SYMBOLS:
        try:
            result[sym] = fetch_klines(sym)
        except Exception as exc:
            print(f"  [WARN] Failed to fetch klines for {sym}: {exc}")
    return result


def fmt_price(val: float) -> str:
    """Smart price formatting: $75,432.07 for big, $0.0934 for small."""
    if val >= 100:
        return f"${val:,.0f}"
    if val >= 1:
        return f"${val:,.2f}"
    if val >= 0.01:
        return f"${val:.4f}"
    return f"${val:.6f}"


def format_indicator_line(ind: dict, price: float) -> str:
    parts = []
    if ind.get("rsi") is not None:
        rsi = ind["rsi"]
        tag = " OB" if rsi >= RSI_OVERBOUGHT else (" OS" if rsi <= RSI_OVERSOLD else "")
        parts.append(f"RSI:{rsi:.0f}{tag}")
    if ind.get("ema20") is not None:
        parts.append(f"EMA20:{fmt_price(ind['ema20'])}")
    if ind.get("ema50") is not None:
        parts.append(f"EMA50:{fmt_price(ind['ema50'])}")
    ema_cross = ind.get("ema_cross", "")
    ema_gap = ind.get("ema_gap_pct", 0)
    if ema_cross:
        parts.append(f"EMAx:{ema_cross}({ema_gap:+.2f}%)")
    ema_4h = ind.get("ema_cross_4h")
    if ema_4h and ema_4h != "UNKNOWN":
        parts.append(f"4h:{'BULL' if ema_4h == 'BULLISH' else 'BEAR'}")
    if ind.get("atr") is not None:
        parts.append(f"ATR:{fmt_price(ind['atr'])}")
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


def get_portfolio_balance() -> float:
    """Read portfolio balance from env (reloaded each cycle so user can update .env live)."""
    return float(os.environ.get("PORTFOLIO_BALANCE", str(PORTFOLIO_BALANCE)))


def calc_open_risk(trading_state: dict, prices: dict) -> tuple[float, list]:
    """Calculate total open risk in USD across all active positions.
    Returns (total_risk_usd, details_list)."""
    states = trading_state.get("states", {})
    total_risk = 0.0
    details = []
    for coin, s in states.items():
        if s.get("state") != "ACTIVE" or not s.get("entry_price"):
            continue
        entry = s["entry_price"]
        sl = s["sl_price"]
        direction = s.get("direction", "LONG")
        risk_per_unit = abs(entry - sl)
        balance = get_portfolio_balance()
        risk_pct_of_trade = RISK_PER_TRADE_PCT / 100
        position_size_usd = (balance * risk_pct_of_trade / risk_per_unit) * entry if risk_per_unit > 0 else 0
        risk_usd = balance * risk_pct_of_trade if risk_per_unit > 0 else 0
        total_risk += risk_usd
        details.append({
            "coin": coin,
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "risk_usd": round(risk_usd, 2),
            "position_usd": round(position_size_usd, 2),
        })
    return total_risk, details


def calc_position_size(entry: float, sl: float, balance: float) -> dict:
    """Calculate position size and risk for a new trade.
    Returns dict with qty, position_usd, risk_usd, risk_pct."""
    risk_per_unit = abs(entry - sl)
    if risk_per_unit == 0 or entry == 0:
        return {"qty": 0, "position_usd": 0, "risk_usd": 0, "risk_pct": 0}
    max_risk_usd = balance * (RISK_PER_TRADE_PCT / 100)
    qty = max_risk_usd / risk_per_unit
    position_usd = qty * entry
    return {
        "qty": round(qty, 6),
        "position_usd": round(position_usd, 2),
        "risk_usd": round(max_risk_usd, 2),
        "risk_pct": RISK_PER_TRADE_PCT,
    }


def generate_signals(prices: dict, indicators: dict, trading_state: dict,
                     alert_state: dict, cooldown_h: int) -> list:
    """Rule engine: generate LONG/SHORT signals based on strict TA confirmation.

    Portfolio risk management:
    - Each trade risks max RISK_PER_TRADE_PCT of portfolio
    - Total open risk capped at MAX_PORTFOLIO_RISK_PCT
    - Position size auto-calculated from SL distance
    """
    signals = []
    states = trading_state.get("states", {})
    balance = get_portfolio_balance()
    open_risk, _ = calc_open_risk(trading_state, prices)
    max_total_risk = balance * (MAX_PORTFOLIO_RISK_PCT / 100)
    risk_budget = max_total_risk - open_risk
    per_trade_risk = balance * (RISK_PER_TRADE_PCT / 100)

    if risk_budget < per_trade_risk * 0.5:
        return signals

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
        ema_fast = ind.get("ema20")
        ema_slow = ind.get("ema50")
        atr = ind.get("atr")
        vol_ratio = ind.get("vol_ratio")
        trend = ind.get("trend", "UNKNOWN")
        ema_cross = ind.get("ema_cross", "")
        ema_cross_4h = ind.get("ema_cross_4h", "UNKNOWN")

        if rsi is None or ema_fast is None or ema_slow is None or atr is None or vol_ratio is None:
            continue
        if rsi_prev is None:
            continue

        rsi_delta = rsi - rsi_prev
        vol_ok = vol_ratio >= VOLUME_CONFIRM_RATIO
        ema_gap_pct = abs(ema_fast - ema_slow) / ema_slow * 100 if ema_slow else 0

        # --- LONG rules (ALL must pass) ---
        # 1. EMA20 > EMA50 (bullish cross confirmed)
        ema_bullish = ema_fast > ema_slow
        # 2. Price above EMA20 (momentum)
        price_above_ema = price > ema_fast
        # 3. RSI momentum: cross up from oversold OR strong bullish delta
        rsi_cross_up = rsi_prev < RSI_OVERSOLD and rsi >= RSI_OVERSOLD
        rsi_strong_bull = rsi > RSI_LONG_MIN and rsi_delta >= RSI_MOMENTUM_DELTA
        rsi_long_ok = rsi_cross_up or rsi_strong_bull
        # 4. Volume confirmation
        # 5. Trend = UPTREND (price > EMA20 > EMA50)
        trend_bullish = trend == "UPTREND"
        # 6. Multi-timeframe: 4h EMA cross must also be BULLISH (or UNKNOWN if missing data)
        mtf_bullish = ema_cross_4h in ("BULLISH", "UNKNOWN")

        if ema_bullish and price_above_ema and rsi_long_ok and vol_ok and trend_bullish and mtf_bullish:
            entry = round(price, 6)
            sl = round(entry - ATR_SL_MULT * atr, 6)
            tp = round(entry + ATR_TP_MULT * atr, 6)
            sl_pct = abs(entry - sl) / entry * 100
            tp_pct = abs(tp - entry) / entry * 100
            pos = calc_position_size(entry, sl, balance)
            strength = "STRONG" if rsi_cross_up or rsi_delta >= 5 else "MODERATE"
            signals.append({
                "coin": coin,
                "symbol": symbol,
                "direction": "LONG",
                "strength": strength,
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "sl_pct": round(sl_pct, 2),
                "tp_pct": round(tp_pct, 2),
                "atr": round(atr, 6),
                "rsi": round(rsi, 1),
                "rsi_prev": round(rsi_prev, 1),
                "rsi_delta": round(rsi_delta, 1),
                "ema20": round(ema_fast, 6),
                "ema50": round(ema_slow, 6),
                "ema_gap_pct": round(ema_gap_pct, 3),
                "ema_cross_4h": ema_cross_4h,
                "vol_ratio": round(vol_ratio, 2),
                "trend": trend,
                "rr_ratio": round(ATR_TP_MULT / ATR_SL_MULT, 2),
                "position_usd": pos["position_usd"],
                "qty": pos["qty"],
                "risk_usd": pos["risk_usd"],
                "risk_pct": pos["risk_pct"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "pending_review",
            })

        # --- SHORT rules (ALL must pass) ---
        # 1. EMA20 < EMA50 (bearish cross confirmed)
        ema_bearish = ema_fast < ema_slow
        # 2. Price below EMA20
        price_below_ema = price < ema_fast
        # 3. RSI momentum: cross down from overbought OR strong bearish delta
        rsi_cross_down = rsi_prev > RSI_OVERBOUGHT and rsi <= RSI_OVERBOUGHT
        rsi_strong_bear = rsi < RSI_SHORT_MAX and rsi_delta <= -RSI_MOMENTUM_DELTA
        rsi_short_ok = rsi_cross_down or rsi_strong_bear
        # 4. Volume confirmation
        # 5. Trend = DOWNTREND (price < EMA20 < EMA50)
        trend_bearish = trend == "DOWNTREND"
        # 6. Multi-timeframe: 4h EMA cross must also be BEARISH (or UNKNOWN if missing)
        mtf_bearish = ema_cross_4h in ("BEARISH", "UNKNOWN")

        if ema_bearish and price_below_ema and rsi_short_ok and vol_ok and trend_bearish and mtf_bearish:
            entry = round(price, 6)
            sl = round(entry + ATR_SL_MULT * atr, 6)
            tp = round(entry - ATR_TP_MULT * atr, 6)
            sl_pct = abs(sl - entry) / entry * 100
            tp_pct = abs(entry - tp) / entry * 100
            pos = calc_position_size(entry, sl, balance)
            strength = "STRONG" if rsi_cross_down or rsi_delta <= -5 else "MODERATE"
            signals.append({
                "coin": coin,
                "symbol": symbol,
                "direction": "SHORT",
                "strength": strength,
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "sl_pct": round(sl_pct, 2),
                "tp_pct": round(tp_pct, 2),
                "atr": round(atr, 6),
                "rsi": round(rsi, 1),
                "rsi_prev": round(rsi_prev, 1),
                "rsi_delta": round(rsi_delta, 1),
                "ema20": round(ema_fast, 6),
                "ema50": round(ema_slow, 6),
                "ema_gap_pct": round(ema_gap_pct, 3),
                "ema_cross_4h": ema_cross_4h,
                "vol_ratio": round(vol_ratio, 2),
                "trend": trend,
                "rr_ratio": round(ATR_TP_MULT / ATR_SL_MULT, 2),
                "position_usd": pos["position_usd"],
                "qty": pos["qty"],
                "risk_usd": pos["risk_usd"],
                "risk_pct": pos["risk_pct"],
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


# ---------------------------------------------------------------------------
# LLM Signal Review (DeepSeek)
# ---------------------------------------------------------------------------

def llm_review_signal(signal: dict, indicators: dict, active_count: int) -> dict:
    """Call DeepSeek to review a signal before execution.
    Returns {"decision": "CONFIRM"|"REJECT", "reason": "...", "confidence": 0-100}
    """
    api_key = cfg("DEEPSEEK_API_KEY")
    if not api_key:
        return {"decision": "CONFIRM", "reason": "No API key, skip review", "confidence": 0}

    coin = signal["coin"].upper()
    direction = signal["direction"]
    entry = signal["entry"]
    sl = signal["sl"]
    tp = signal["tp"]
    rsi = signal.get("rsi", 0)
    rsi_prev = signal.get("rsi_prev", 0)
    ema20 = signal.get("ema20", 0)
    ema50 = signal.get("ema50", 0)
    ema_gap = signal.get("ema_gap_pct", 0)
    vol_ratio = signal.get("vol_ratio", 0)
    trend = signal.get("trend", "")
    strength = signal.get("strength", "")
    rr = signal.get("rr_ratio", 0)
    atr = signal.get("atr", 0)

    ema_cross_str = "BULLISH (EMA20>EMA50)" if ema20 > ema50 else "BEARISH (EMA20<EMA50)"
    ema_cross_4h_str = signal.get("ema_cross_4h", "UNKNOWN")
    mtf_align = "ALIGNED" if (
        (direction == "LONG" and ema_cross_4h_str == "BULLISH") or
        (direction == "SHORT" and ema_cross_4h_str == "BEARISH")
    ) else ("UNKNOWN" if ema_cross_4h_str == "UNKNOWN" else "DIVERGED")

    prompt = f"""You are a crypto futures trading risk analyst. Review this signal and decide CONFIRM or REJECT.

SIGNAL: {coin} {direction}
Entry: ${entry} | SL: ${sl} | TP: ${tp} | R:R: {rr}
RSI(14): {rsi:.1f} (prev: {rsi_prev:.1f}, delta: {rsi-rsi_prev:+.1f})
EMA20: ${ema20} | EMA50: ${ema50} | EMA cross 1h: {ema_cross_str} (gap: {ema_gap:+.2f}%)
EMA cross 4h: {ema_cross_4h_str} (multi-timeframe: {mtf_align})
Volume ratio: {vol_ratio:.2f}x | ATR(4h): ${atr}
Trend: {trend} | Strength: {strength}
Currently {active_count} other positions open.

REJECT if ANY of these:
1. {direction} against EMA cross 1h (e.g. LONG when EMA20<EMA50, SHORT when EMA20>EMA50)
2. EMA gap too small (<0.1%) — cross not confirmed, high whipsaw risk
3. Multi-timeframe DIVERGED — 4h trend opposes 1h signal (very risky)
4. Volume ratio < 1.0 (no volume confirmation)
5. R:R < 1.5 after fees (need buffer for 0.08% round-trip taker fee)
6. Already {active_count} positions open AND this is a MODERATE signal
7. RSI in extreme zone for the direction (LONG with RSI>70, SHORT with RSI<30)
8. {direction} against strong reversal (LONG when RSI>75 and dropping, SHORT when RSI<25 and rising)

CONFIRM if EMA cross 1h+4h ALIGNED with direction, RSI has momentum, volume confirms, R:R >= 1.5.

Reply ONLY in this JSON format, nothing else:
{{"decision": "CONFIRM" or "REJECT", "reason": "one sentence", "confidence": 0-100}}"""

    try:
        body = json.dumps({
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 150,
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
        # Extract JSON from response (handle markdown code blocks)
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        content = content.strip()

        review = json.loads(content)
        review["decision"] = review.get("decision", "CONFIRM").upper()
        if review["decision"] not in ("CONFIRM", "REJECT"):
            review["decision"] = "CONFIRM"
        return review

    except Exception as e:
        print(f"  LLM review error: {e} — defaulting to CONFIRM")
        return {"decision": "CONFIRM", "reason": f"API error: {e}", "confidence": 0}


def fetch_prices() -> dict:
    symbols_json = json.dumps(SYMBOLS, separators=(",", ":"))
    url = f"{BINANCE_PRICE_API}?symbols={symbols_json}"
    req = urllib.request.Request(url, headers={"User-Agent": "PicoAlerts/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    return {item["symbol"]: float(item["price"]) for item in data}


def load_trading_state() -> dict:
    if TRADING_STATE_FILE.exists():
        data = json.loads(TRADING_STATE_FILE.read_text())
    else:
        data = {"states": {}}

    states = data.get("states", {})
    changed = False
    default_state = {
        "state": "WATCHING",
        "direction": "",
        "entry_price": 0,
        "tp_price": 0,
        "sl_price": 0,
        "user_confirmed": False,
        "order_id": "",
        "sl_order_id": "",
        "tp_order_id": "",
        "fill_price": 0,
        "fill_qty": 0,
        "fill_time": "",
        "pnl_usd": 0,
    }
    for coin in SYMBOL_MAP:
        if coin not in states:
            states[coin] = dict(default_state)
            changed = True
    data["states"] = states
    if changed:
        save_trading_state(data)
    return data


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
    state_changed = False

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

        if entry and tp and sl and direction and state == "ACTIVE":
            if direction == "LONG":
                if price >= tp:
                    key = f"{coin}_tp_hit"
                    if not is_on_cooldown(alert_state, key, cooldown_min):
                        pnl_pct = (price - entry) / entry * 100
                        rsi_note = ""
                        if rsi and rsi >= RSI_OVERBOUGHT:
                            rsi_note = "\nRSI overbought — strong take-profit signal."
                        elif rsi and rsi >= 60:
                            rsi_note = "\nRSI still bullish — partial take-profit OK."
                        alerts.append(
                            f"*{coin.upper()} TP HIT*\n"
                            f"Price: *{fmt_price(price)}* >= TP {fmt_price(tp)}\n"
                            f"Entry: {fmt_price(entry)} | P&L: *+{pnl_pct:.1f}%*\n"
                            f"{ind_line}{rsi_note}"
                        )
                        mark_alerted(alert_state, key)
                        coin_state["state"] = "TP_HIT"
                        coin_state["close_price"] = price
                        state_changed = True
                elif price <= sl:
                    key = f"{coin}_sl_hit"
                    if not is_on_cooldown(alert_state, key, cooldown_min):
                        pnl_pct = (price - entry) / entry * 100
                        alerts.append(
                            f"*{coin.upper()} SL HIT*\n"
                            f"Price: *{fmt_price(price)}* <= SL {fmt_price(sl)}\n"
                            f"Entry: {fmt_price(entry)} | P&L: *{pnl_pct:.1f}%*\n"
                            f"{ind_line}\nExit position."
                        )
                        mark_alerted(alert_state, key)
                        coin_state["state"] = "SL_HIT"
                        coin_state["close_price"] = price
                        state_changed = True
                elif price < entry * (1 - REVERSAL_PCT):
                    key = f"{coin}_reversal"
                    if not is_on_cooldown(alert_state, key, cooldown_min):
                        alerts.append(
                            f"*{coin.upper()} REVERSAL WARNING*\n"
                            f"Price: *{fmt_price(price)}* ({((price - entry) / entry * 100):.1f}% from entry)\n"
                            f"Entry: {fmt_price(entry)} | SL: {fmt_price(sl)}\n"
                            f"{ind_line}"
                        )
                        mark_alerted(alert_state, key)
            elif direction == "SHORT":
                if price <= tp:
                    key = f"{coin}_tp_hit"
                    if not is_on_cooldown(alert_state, key, cooldown_min):
                        pnl_pct = (entry - price) / entry * 100
                        alerts.append(
                            f"*{coin.upper()} TP HIT (SHORT)*\n"
                            f"Price: *{fmt_price(price)}* <= TP {fmt_price(tp)}\n"
                            f"Entry: {fmt_price(entry)} | P&L: *+{pnl_pct:.1f}%*\n"
                            f"{ind_line}"
                        )
                        mark_alerted(alert_state, key)
                        coin_state["state"] = "TP_HIT"
                        coin_state["close_price"] = price
                        state_changed = True
                elif price >= sl:
                    key = f"{coin}_sl_hit"
                    if not is_on_cooldown(alert_state, key, cooldown_min):
                        pnl_pct = (entry - price) / entry * 100
                        alerts.append(
                            f"*{coin.upper()} SL HIT (SHORT)*\n"
                            f"Price: *{fmt_price(price)}* >= SL {fmt_price(sl)}\n"
                            f"Entry: {fmt_price(entry)} | P&L: *{pnl_pct:.1f}%*\n"
                            f"{ind_line}\nExit position."
                        )
                        mark_alerted(alert_state, key)
                        coin_state["state"] = "SL_HIT"
                        coin_state["close_price"] = price
                        state_changed = True
                elif price > entry * (1 + REVERSAL_PCT):
                    key = f"{coin}_reversal"
                    if not is_on_cooldown(alert_state, key, cooldown_min):
                        alerts.append(
                            f"*{coin.upper()} REVERSAL WARNING (SHORT)*\n"
                            f"Price: *{fmt_price(price)}* (+{((price - entry) / entry * 100):.1f}% from entry)\n"
                            f"{ind_line}"
                        )
                        mark_alerted(alert_state, key)

        bo = ind.get("breakout")
        bd = ind.get("breakdown")
        if bo and bd and not entry:
            rsi_bull = rsi is not None and rsi > 50
            rsi_bear = rsi is not None and rsi < 50
            vol_ok = vol_ratio is not None and vol_ratio >= VOLUME_CONFIRM_RATIO

            if price > bo:
                confirmed = rsi_bull and vol_ok
                key = f"{coin}_breakout" + ("_confirmed" if confirmed else "_weak")
                if not is_on_cooldown(alert_state, key, cooldown_min):
                    strength = "CONFIRMED" if confirmed else "WEAK (low vol or RSI)"
                    h7d = ind.get("high_7d", bo)
                    alerts.append(
                        f"*{coin.upper()} BREAKOUT — {strength}*\n"
                        f"Price: *{fmt_price(price)}* > 7D High {fmt_price(h7d)} (level {fmt_price(bo)})\n"
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
                    l7d = ind.get("low_7d", bd)
                    alerts.append(
                        f"*{coin.upper()} BREAKDOWN — {strength}*\n"
                        f"Price: *{fmt_price(price)}* < 7D Low {fmt_price(l7d)} (level {fmt_price(bd)})\n"
                        f"{ind_line}\n"
                        + ("Avoid longs. Downtrend confirmed." if confirmed
                           else "Possible fake breakdown. Wait for confirmation.")
                    )
                    mark_alerted(alert_state, key)

    if state_changed:
        save_trading_state(trading_state)

    return alerts


def save_trading_state(data: dict):
    TRADING_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    TRADING_STATE_FILE.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------

def run_once():
    load_dotenv()
    discover_top_coins()

    tg_token = cfg("TELEGRAM_BOT_TOKEN")
    tg_chat = cfg("TELEGRAM_ALERT_CHAT_ID", cfg("TELEGRAM_CHAT_ID"))
    cooldown_min = int(cfg("ALERT_COOLDOWN_MIN", "30"))
    signal_cooldown_h = int(cfg("SIGNAL_COOLDOWN_H", "4"))

    if not tg_token or not tg_chat:
        sys.exit("TELEGRAM_BOT_TOKEN and TELEGRAM_ALERT_CHAT_ID (or TELEGRAM_CHAT_ID) are required")

    prices = fetch_prices()
    indicators = fetch_all_indicators()
    trading_state = load_trading_state()
    alert_state = load_alert_state()

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    core_prices = {s: p for s, p in prices.items() if s in CORE_SYMBOLS}
    price_summary = " | ".join(
        f"{s.replace('USDT', '')}: ${p:,.2f}" for s, p in sorted(core_prices.items())
    )
    print(f"[{now_str}] {price_summary} (+{len(prices) - len(core_prices)} more)")

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
    auto_trade = cfg("AUTO_TRADE_ENABLED", "false").lower() in ("true", "1", "yes")

    # OpenClaw kill switch override
    if TRADING_CONTROL_FILE.exists():
        try:
            ctrl = json.loads(TRADING_CONTROL_FILE.read_text())
            if not ctrl.get("auto_trade_enabled", True):
                if auto_trade:
                    print(f"  KILL SWITCH: auto-trade disabled by {ctrl.get('updated_by', 'OpenClaw')} — {ctrl.get('reason', '')}")
                auto_trade = False
        except Exception:
            pass

    existing_pending = load_pending_signal()
    if existing_pending and not auto_trade:
        age_h = (time.time() - datetime.fromisoformat(existing_pending["timestamp"]).timestamp()) / 3600
        if age_h > 4:
            print(f"  Expiring stale pending signal ({existing_pending['coin'].upper()} {existing_pending['direction']}, {age_h:.1f}h old)")
            clear_pending_signal()
            existing_pending = None
        else:
            print(f"  Pending signal exists: {existing_pending['coin'].upper()} {existing_pending['direction']} (waiting LLM review)")

    can_generate = auto_trade or not existing_pending
    if can_generate:
        signals = generate_signals(prices, indicators, trading_state, alert_state, signal_cooldown_h)
        if signals:
            best = signals[0]
            mark_alerted(alert_state, f"signal_{best['coin']}")
            alert_state["last_signal_ts"] = time.time()

            coin_u = best["coin"].upper()
            strength = best.get("strength", "MODERATE")
            sl_pct = best.get("sl_pct", 0)
            tp_pct = best.get("tp_pct", 0)
            rsi_delta = best.get("rsi_delta", 0)
            pos_usd = best.get("position_usd", 0)
            risk_usd = best.get("risk_usd", 0)
            qty = best.get("qty", 0)
            balance = get_portfolio_balance()

            if auto_trade:
                active_count = sum(1 for s in trading_state.get("states", {}).values() if s.get("state") == "ACTIVE")
                print(f"  Signal: {coin_u} {best['direction']} @ {fmt_price(best['entry'])} — LLM reviewing...")
                review = llm_review_signal(best, indicators, active_count)
                decision = review.get("decision", "CONFIRM")
                reason = review.get("reason", "")
                confidence = review.get("confidence", 0)
                print(f"  LLM: {decision} ({confidence}%) — {reason}")

                if decision == "REJECT":
                    best["status"] = "llm_rejected"
                    best["llm_reason"] = reason
                    best["llm_confidence"] = confidence
                    save_pending_signal(best)

                    msg = (
                        f"🚫 *[REJECTED] {coin_u} {best['direction']}* ({strength})\n"
                        f"Entry: {fmt_price(best['entry'])} | SL: {fmt_price(best['sl'])} | TP: {fmt_price(best['tp'])}\n"
                        f"R/R: {best['rr_ratio']} | RSI:{best['rsi']:.0f} | Vol:{best['vol_ratio']:.1f}x\n\n"
                        f"*LLM ({confidence}%):* {reason}"
                    )
                    send_telegram(tg_token, tg_chat, msg)
                    print(f"  REJECTED: {coin_u} {best['direction']} — {reason}")
                else:
                    coin_key = best["coin"]
                    states = trading_state.get("states", {})
                    if coin_key not in states:
                        states[coin_key] = {}
                    states[coin_key].update({
                        "state": "ACTIVE",
                        "direction": best["direction"],
                        "entry_price": best["entry"],
                        "sl_price": best["sl"],
                        "tp_price": best["tp"],
                        "user_confirmed": False,
                        "order_id": "",
                        "signal_strength": strength,
                        "signal_time": best["timestamp"],
                    })
                    trading_state["states"] = states
                    save_trading_state(trading_state)

                    best["status"] = "auto_confirmed"
                    best["llm_reason"] = reason
                    best["llm_confidence"] = confidence
                    save_pending_signal(best)

                    msg = (
                        f"✅ *[AUTO SIGNAL] {coin_u} {best['direction']}* ({strength})\n"
                        f"Entry: *{fmt_price(best['entry'])}*\n"
                        f"SL: {fmt_price(best['sl'])} (-{sl_pct:.1f}%, {ATR_SL_MULT}x ATR4h)\n"
                        f"TP: {fmt_price(best['tp'])} (+{tp_pct:.1f}%, {ATR_TP_MULT}x ATR4h)\n"
                        f"R/R: {best['rr_ratio']}\n"
                        f"\n💰 *Position sizing* (${balance:,.0f} portfolio)\n"
                        f"Size: ${pos_usd:,.0f} ({qty:.4f} {coin_u})\n"
                        f"Risk: ${risk_usd:.0f} ({RISK_PER_TRADE_PCT:.0f}% of portfolio)\n"
                        f"\nRSI:{best['rsi']:.0f} (Δ{rsi_delta:+.0f}) | EMA20:{fmt_price(best['ema20'])} | EMA50:{fmt_price(best['ema50'])}\n"
                        f"EMA 1h: {best.get('ema_gap_pct', 0):+.2f}% gap | EMA 4h: {best.get('ema_cross_4h', 'N/A')}\n"
                        f"Vol:{best['vol_ratio']:.1f}x | ATR4h:{fmt_price(best['atr'])} | {best['trend']}\n\n"
                        f"🤖 *LLM ({confidence}%):* {reason}\n"
                        f"_Auto-executing via trade executor..._"
                    )
                    print(f"  AUTO SIGNAL: {coin_u} {best['direction']} @ {fmt_price(best['entry'])} → ACTIVE")
            else:
                save_pending_signal(best)
                msg = (
                    f"*[PENDING SIGNAL] {coin_u} {best['direction']}* ({strength})\n"
                    f"Entry: *{fmt_price(best['entry'])}*\n"
                    f"SL: {fmt_price(best['sl'])} (-{sl_pct:.1f}%, {ATR_SL_MULT}x ATR4h)\n"
                    f"TP: {fmt_price(best['tp'])} (+{tp_pct:.1f}%, {ATR_TP_MULT}x ATR4h)\n"
                    f"R/R: {best['rr_ratio']}\n"
                    f"\n💰 *Position sizing* (${balance:,.0f} portfolio)\n"
                    f"Size: ${pos_usd:,.0f} ({qty:.4f} {coin_u})\n"
                    f"Risk: ${risk_usd:.0f} ({RISK_PER_TRADE_PCT:.0f}% of portfolio)\n"
                    f"\nRSI:{best['rsi']:.0f} (Δ{rsi_delta:+.0f}) | EMA20:{fmt_price(best['ema20'])} | EMA50:{fmt_price(best['ema50'])}\n"
                    f"EMA 1h: {best.get('ema_gap_pct', 0):+.2f}% gap | EMA 4h: {best.get('ema_cross_4h', 'N/A')}\n"
                    f"Vol:{best['vol_ratio']:.1f}x | ATR4h:{fmt_price(best['atr'])} | {best['trend']}\n\n"
                    f"_Awaiting LLM news review (next cron cycle)_"
                )
                print(f"  SIGNAL: {coin_u} {best['direction']} @ {fmt_price(best['entry'])}")

            send_telegram(tg_token, tg_chat, msg)
        else:
            print("  No signals.")
            # Idle market alert: no signal for IDLE_ALERT_HOURS, alert once per IDLE_ALERT_COOLDOWN_HOURS
            IDLE_ALERT_HOURS = 48
            IDLE_ALERT_COOLDOWN_HOURS = 24
            last_signal_ts = alert_state.get("last_signal_ts")
            if last_signal_ts is None:
                # Initialize on first idle so we don't alert immediately
                alert_state["last_signal_ts"] = time.time()
            else:
                last_idle_ts = alert_state.get("last_idle_alert_ts", 0)
                idle_for = (time.time() - last_signal_ts) / 3600
                since_last_idle = (time.time() - last_idle_ts) / 3600
                if idle_for >= IDLE_ALERT_HOURS and since_last_idle >= IDLE_ALERT_COOLDOWN_HOURS:
                    active_count = sum(1 for s in trading_state.get("states", {}).values() if s.get("state") == "ACTIVE")
                    last_str = datetime.fromtimestamp(last_signal_ts, timezone.utc).strftime("%m/%d %H:%M UTC")
                    send_telegram(
                        tg_token, tg_chat,
                        f"💤 *Market Idle Alert*\n"
                        f"No new signals for *{idle_for:.0f}h* (last: {last_str}).\n"
                        f"Active positions: {active_count}\n"
                        f"Possible causes: low volatility, sideways market, strict EMA filter.\n"
                        f"Consider: review filter strictness or wait for trend.",
                    )
                    alert_state["last_idle_alert_ts"] = time.time()
                    print(f"  IDLE ALERT sent (idle for {idle_for:.0f}h)")

    alert_state["last_prices"] = {s: p for s, p in prices.items()}
    alert_state["last_check"] = now_str
    save_alert_state(alert_state)


def print_status():
    load_dotenv()
    discover_top_coins()
    print(f"Monitoring {len(SYMBOLS)} coins (top by volume + core)\n")
    print("Fetching indicators...\n")
    indicators = fetch_all_indicators()
    prices = fetch_prices()
    trading_state = load_trading_state()
    states = trading_state.get("states", {})

    # Portfolio risk dashboard
    balance = get_portfolio_balance()
    open_risk, risk_details = calc_open_risk(trading_state, prices)
    max_risk = balance * (MAX_PORTFOLIO_RISK_PCT / 100)
    risk_budget = max_risk - open_risk
    active_count = sum(1 for s in states.values() if s.get("state") == "ACTIVE")
    print(f"{'=' * 55}")
    print(f"  💰 PORTFOLIO: ${balance:,.0f} | Risk: ${open_risk:,.0f}/{max_risk:,.0f} ({open_risk/balance*100:.1f}%/{MAX_PORTFOLIO_RISK_PCT:.0f}%)")
    print(f"  Active: {active_count} positions | Budget left: ${risk_budget:,.0f} ({risk_budget/balance*100:.1f}%)")
    if risk_details:
        for rd in risk_details:
            print(f"    {rd['coin'].upper():>6} {rd['direction']:5} — pos: ${rd['position_usd']:,.0f} | risk: ${rd['risk_usd']:,.0f}")
    print()

    for coin, symbol in sorted(SYMBOL_MAP.items()):
        price = prices.get(symbol, 0)
        ind = indicators.get(symbol, {})
        cs = states.get(coin, {})
        position = cs.get("direction") or "NO_POSITION"
        state_label = cs.get("state", "WATCHING")

        print(f"{'=' * 55}")
        print(f"  {coin.upper()} — {fmt_price(price)}  [{state_label}] [{position}]")
        print(f"  {format_indicator_line(ind, price)}")

        if ind.get("high_24h"):
            print(f"  24h Range: ${ind['low_24h']:,.2f} — ${ind['high_24h']:,.2f}")
        if ind.get("high_7d"):
            print(f"  7D  Range: ${ind['low_7d']:,.2f} — ${ind['high_7d']:,.2f}")

        entry = cs.get("entry_price")
        if entry and position != "NO_POSITION":
            pnl = ((price - entry) / entry * 100)
            if position == "SHORT":
                pnl = ((entry - price) / entry * 100)
            print(f"  Entry: ${entry:,.2f} | TP: ${cs.get('tp_price', 0):,.2f} | SL: ${cs.get('sl_price', 0):,.2f} | P&L: {pnl:+.1f}%")

        bo = ind.get("breakout")
        bd = ind.get("breakdown")
        if bo and bd and not entry:
            print(f"  Breakout: >${bo:,.2f} | Breakdown: <${bd:,.2f} (auto from 7D range)")

        rsi = ind.get("rsi")
        ema_f = ind.get("ema20")
        ema_s = ind.get("ema50")
        vr = ind.get("vol_ratio")
        atr_val = ind.get("atr")
        if rsi and ema_f and ema_s and vr and atr_val:
            rsi_prev = ind.get("rsi_prev")
            rsi_d = (rsi - rsi_prev) if rsi_prev else 0
            ema_gap = (ema_f - ema_s) / ema_s * 100 if ema_s else 0

            rsi_cross_up = rsi_prev is not None and rsi_prev < RSI_OVERSOLD and rsi >= RSI_OVERSOLD
            rsi_strong_bull = rsi > RSI_LONG_MIN and rsi_d >= RSI_MOMENTUM_DELTA
            rsi_long_ok = rsi_cross_up or rsi_strong_bull
            ema_bull = ema_f > ema_s
            p_above_ema = price > ema_f
            vol_ok = vr >= VOLUME_CONFIRM_RATIO
            trend_bull = ind.get("trend", "") == "UPTREND"
            rsi_tag = "cross↑30" if rsi_cross_up else (f"Δ{rsi_d:+.0f}" if rsi_strong_bull else f"✗Δ{rsi_d:+.0f}")
            checks = [
                f"EMAx:{'Y' if ema_bull else 'N'}({ema_gap:+.2f}%)",
                f"P>EMA20:{'Y' if p_above_ema else 'N'}",
                f"RSI:{rsi:.0f}({rsi_tag}):{'Y' if rsi_long_ok else 'N'}",
                f"Vol:{vr:.1f}x:{'Y' if vol_ok else 'N'}",
                f"Trend:{'Y' if trend_bull else 'N'}",
            ]
            all_pass = ema_bull and p_above_ema and rsi_long_ok and vol_ok and trend_bull
            print(f"  LONG:  {' | '.join(checks)} → {'PASS' if all_pass else 'NO'}")

            rsi_cross_dn = rsi_prev is not None and rsi_prev > RSI_OVERBOUGHT and rsi <= RSI_OVERBOUGHT
            rsi_strong_bear = rsi < RSI_SHORT_MAX and rsi_d <= -RSI_MOMENTUM_DELTA
            rsi_short_ok = rsi_cross_dn or rsi_strong_bear
            ema_bear = ema_f < ema_s
            p_below_ema = price < ema_f
            trend_bear = ind.get("trend", "") == "DOWNTREND"
            rsi_tag_s = "cross↓70" if rsi_cross_dn else (f"Δ{rsi_d:+.0f}" if rsi_strong_bear else f"✗Δ{rsi_d:+.0f}")
            checks_s = [
                f"EMAx:{'Y' if ema_bear else 'N'}({ema_gap:+.2f}%)",
                f"P<EMA20:{'Y' if p_below_ema else 'N'}",
                f"RSI:{rsi:.0f}({rsi_tag_s}):{'Y' if rsi_short_ok else 'N'}",
                f"Vol:{vr:.1f}x:{'Y' if vol_ok else 'N'}",
                f"Trend:{'Y' if trend_bear else 'N'}",
            ]
            all_pass_s = ema_bear and p_below_ema and rsi_short_ok and vol_ok and trend_bear
            print(f"  SHORT: {' | '.join(checks_s)} → {'PASS' if all_pass_s else 'NO'}")

            if atr_val:
                sl_pct = ATR_SL_MULT * atr_val / price * 100
                tp_pct = ATR_TP_MULT * atr_val / price * 100
                print(f"  SL/TP: SL={fmt_price(ATR_SL_MULT * atr_val)} ({sl_pct:.1f}%) | TP={fmt_price(ATR_TP_MULT * atr_val)} ({tp_pct:.1f}%) | ATR4h={fmt_price(atr_val)}")

        print()

    pending = load_pending_signal()
    if pending:
        print(f"{'=' * 55}")
        print(f"  PENDING SIGNAL: {pending['coin'].upper()} {pending['direction']} @ {fmt_price(pending['entry'])}")
        print(f"  SL: {fmt_price(pending['sl'])} | TP: {fmt_price(pending['tp'])} | R/R: {pending['rr_ratio']}")
        print(f"  Status: {pending['status']} | Time: {pending['timestamp']}")
        print()


def daemon_loop():
    load_dotenv()
    discover_top_coins()
    interval = int(cfg("CHECK_INTERVAL_SEC", "60"))
    coins_str = ", ".join(s.replace("USDT", "") for s in SYMBOLS[:6])
    balance = get_portfolio_balance()
    auto_trade = cfg("AUTO_TRADE_ENABLED", "false").lower() in ("true", "1", "yes")
    mode = "AUTO-TRADE (direct execute)" if auto_trade else "MANUAL (LLM review)"
    print(f"[daemon] Binance price alert v7 (EMA cross) — every {interval}s — {mode}")
    print(f"[daemon] Monitoring {len(SYMBOLS)} coins: {coins_str}...")
    print(f"[daemon] TA: RSI({RSI_PERIOD}), EMA({EMA_FAST_PERIOD}/{EMA_SLOW_PERIOD} cross), ATR({ATR_PERIOD})")
    print(f"[daemon] Signal: STRICT EMA cross only (no SIDEWAYS), SL={ATR_SL_MULT}xATR, TP={ATR_TP_MULT}xATR")
    print(f"[daemon] Portfolio: ${balance:,.0f} | Risk/trade: {RISK_PER_TRADE_PCT}% | Max risk: {MAX_PORTFOLIO_RISK_PCT}%")
    print(f"[daemon] Coin list refreshes every {SYMBOL_CACHE_TTL//60}min")
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
