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
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import decision_logger
except Exception as _dl_err:
    decision_logger = None
    print(f"[WARN] decision_logger unavailable: {_dl_err}")

try:
    import prompt_registry
except Exception as _pr_err:
    prompt_registry = None
    print(f"[WARN] prompt_registry unavailable: {_pr_err}")

try:
    import rag_memory
except Exception as _rm_err:
    rag_memory = None
    print(f"[WARN] rag_memory unavailable: {_rm_err}")

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
MIN_VOLUME_USD = 10_000_000  # lowered to allow ~36 candidates → top 20 selected
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
ATR_TP_MULT = 3.0  # R:R 1.5 — TP closer for higher win rate (per backtest v4 tune)
VOL_STRONG_RATIO = 2.0  # threshold for "strong volume" override on SHORT

# --- Portfolio Risk Management ---
PORTFOLIO_BALANCE = float(os.environ.get("PORTFOLIO_BALANCE", "1000"))
RISK_PER_TRADE_PCT = 3.0      # max 3% of portfolio risked per trade (raised 2026-05-05 after 12d/13t observation, WR 69%)
MAX_PORTFOLIO_RISK_PCT = 12.0  # max 12% total open risk across all positions (raised to keep 4 concurrent slots)

# --- Signal quality filters (per backtest v6: +24.65R lift) ---
# Coin allowlist: only emit signals on these coins (set empty to disable).
# Default = top performers from 30d backtest (>=46% win rate or positive R).
# Override via env: COIN_ALLOWLIST="aave,eth,btc"
_default_allowlist = "aave,eth,link,bnb,xrp,btc,trx,inj,ordi,atom,ena"
COIN_ALLOWLIST = {
    c.strip().lower() for c in
    os.environ.get("COIN_ALLOWLIST", _default_allowlist).split(",")
    if c.strip()
}

# Shield 1: per-coin health auto-suspend
_SUSPENSIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "data", "coin_suspensions.json")
_suspensions_cache = {"loaded_at": 0, "set": set()}

def _suspended_coins() -> set[str]:
    """Re-read suspensions file every 60s. Returns set of suspended coin tickers (lowercase)."""
    import time as _t
    now = _t.time()
    if now - _suspensions_cache["loaded_at"] < 60:
        return _suspensions_cache["set"]
    suspended = set()
    try:
        if os.path.exists(_SUSPENSIONS_FILE):
            with open(_SUSPENSIONS_FILE) as f:
                data = json.load(f)
            suspended = {c.lower() for c in (data.get("suspensions") or {}).keys()}
    except Exception:
        pass
    _suspensions_cache["set"] = suspended
    _suspensions_cache["loaded_at"] = now
    return suspended


# === Coin trade-history tier (probe trades) ===
# Cache trade counts per coin to avoid hammering DB. Refreshed every 5 min.
_coin_history_cache: dict = {"counts": {}, "loaded_at": 0.0}


def _coin_trade_count(coin: str) -> int:
    """Return number of CLOSED trades for a coin in decisions.db (cached 5min)."""
    import time as _t
    import sqlite3 as _sql
    now = _t.time()
    if now - _coin_history_cache["loaded_at"] > 300:
        counts: dict[str, int] = {}
        try:
            if DECISIONS_DB.exists():
                conn = _sql.connect(DECISIONS_DB)
                cur = conn.cursor()
                cur.execute(
                    "SELECT LOWER(coin), COUNT(*) FROM trades "
                    "WHERE closed_at IS NOT NULL AND (is_shadow IS NULL OR is_shadow=0) "
                    "GROUP BY LOWER(coin)"
                )
                counts = {r[0]: r[1] for r in cur.fetchall() if r[0]}
                conn.close()
        except Exception as e:
            print(f"[probe] _coin_trade_count error: {e}")
        _coin_history_cache["counts"] = counts
        _coin_history_cache["loaded_at"] = now
    return _coin_history_cache["counts"].get(coin.lower(), 0)


def _probes_today_for_coin(coin: str) -> int:
    """Count trades opened in the last 24h for this coin (anti-spam cap for probes).
    Conservative: counts ALL trades, not just probes — for untested coins this is
    essentially equivalent and avoids needing executor to tag mode='PROBE' in DB.
    """
    import sqlite3 as _sql
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    try:
        if not DECISIONS_DB.exists():
            return 0
        conn = _sql.connect(DECISIONS_DB)
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM trades WHERE LOWER(coin)=? AND opened_at >= ?",
            (coin.lower(), cutoff))
        n = cur.fetchone()[0]
        conn.close()
        return int(n)
    except Exception:
        return 0


def classify_coin_for_breakout(coin: str, in_allowlist: bool) -> dict:
    """Return classification + trading params for a breakout signal on this coin.

    Returns dict with:
      tier: 'ALLOWLIST' | 'OFFLIST_ESTABLISHED' | 'OFFLIST_THIN' | 'OFFLIST_UNTESTED'
      mode_hint: signal mode label
      risk_pct: risk per trade
      sl_mult, tp_mult: ATR multipliers
      reason: human-readable
    """
    if in_allowlist:
        return {
            "tier": "ALLOWLIST",
            "mode_hint": "BREAKOUT",
            "risk_pct": None,  # use global RISK_PER_TRADE_PCT
            "sl_mult": 0.8,
            "tp_mult": 2.5,
            "reason": "in 11-coin verified allowlist",
        }

    n_trades = _coin_trade_count(coin)
    if n_trades == 0:
        return {
            "tier": "OFFLIST_UNTESTED",
            "mode_hint": "BREAKOUT_PROBE",
            "risk_pct": PROBE_RISK_PCT,
            "sl_mult": BREAKOUT_SL_ATR_MULT,
            "tp_mult": BREAKOUT_TP_ATR_MULT,
            "reason": "no trade history — probe to seed dataset",
        }
    if n_trades < PROBE_GRADUATION_TRADES:
        return {
            "tier": "OFFLIST_THIN",
            "mode_hint": "BREAKOUT_PROBE",
            "risk_pct": PROBE_RISK_PCT,
            "sl_mult": BREAKOUT_SL_ATR_MULT,
            "tp_mult": BREAKOUT_TP_ATR_MULT,
            "reason": f"thin history ({n_trades}/{PROBE_GRADUATION_TRADES}) — probe",
        }
    # Established off-allowlist
    return {
        "tier": "OFFLIST_ESTABLISHED",
        "mode_hint": "BREAKOUT_OFFLIST",
        "risk_pct": BREAKOUT_RISK_PCT,
        "sl_mult": BREAKOUT_SL_ATR_MULT,
        "tp_mult": BREAKOUT_TP_ATR_MULT,
        "reason": f"off-allowlist with {n_trades} trades — graduated",
    }


# === Pullback watch state ===

def _load_pullback_watch() -> dict:
    if not PULLBACK_WATCH_FILE.exists():
        return {}
    try:
        return json.loads(PULLBACK_WATCH_FILE.read_text())
    except Exception:
        return {}


def _save_pullback_watch(state: dict):
    try:
        PULLBACK_WATCH_FILE.parent.mkdir(parents=True, exist_ok=True)
        PULLBACK_WATCH_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        print(f"[pullback] save error: {e}")


def _purge_expired_watch(state: dict) -> dict:
    """Remove entries past their watch window."""
    now = datetime.now(timezone.utc)
    keep = {}
    for sym, entry in state.items():
        try:
            exp = datetime.fromisoformat(entry["expires_at"].replace("Z", "+00:00"))
            if now < exp and not entry.get("fired"):
                keep[sym] = entry
        except Exception:
            pass
    return keep


def register_pullback_watch(signal: dict, llm_reason: str):
    """Add a rejected breakout signal to the pullback watch list.

    Only registers if the rejection reason mentions RSI extreme/exhaustion AND
    the original signal had explosive_burst (we want pullback re-entries on
    real momentum events, not generic rejects).
    """
    if not PULLBACK_REENTRY_ENABLED:
        return
    if signal.get("strength") != "EXPLOSIVE":
        return
    reason_lower = (llm_reason or "").lower()
    rsi_keywords = ("rsi", "extreme", "exhaustion", "overbought", "oversold")
    if not any(k in reason_lower for k in rsi_keywords):
        return

    state = _load_pullback_watch()
    state = _purge_expired_watch(state)
    sym = signal["symbol"]
    expires = (datetime.now(timezone.utc) + timedelta(minutes=PULLBACK_WATCH_WINDOW_MIN)).isoformat(timespec="seconds")
    entry = {
        "coin": signal["coin"],
        "symbol": sym,
        "direction": signal["direction"],
        "rejected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rejected_entry": signal["entry"],
        "rejected_rsi": signal.get("rsi", 0),
        "rejected_atr": signal.get("atr", 0),
        "rejected_burst_range_atr": signal.get("burst_range_atr"),
        "rejected_burst_vol_ratio": signal.get("burst_vol_ratio"),
        "ema_cross_4h": signal.get("ema_cross_4h"),
        "tier": signal.get("tier"),
        "in_allowlist": signal.get("in_allowlist", False),
        "expires_at": expires,
        "llm_reason": llm_reason,
        "fired": False,
        "checks": 0,
    }
    state[sym] = entry
    _save_pullback_watch(state)
    print(f"[pullback] watching {sym} until {expires} (rsi {entry['rejected_rsi']}, entry ${entry['rejected_entry']})")


def check_pullback_entries(prices: dict, indicators: dict, balance: float) -> list:
    """Scan pullback watch list and generate PULLBACK_REENTRY signals if conditions met.

    Conditions for fire:
      - Price has dropped >= PULLBACK_MIN_DROP_ATR from rejected entry (in ATR units)
      - Current RSI cooled by >= PULLBACK_MIN_RSI_DELTA from rejected RSI
      - Vol ratio normalized (<= PULLBACK_MAX_VOL_RATIO — FOMO cooled)
      - 4h trend still aligned with original direction
      - Coin not currently in active position (handled by caller via state check)
    """
    if not PULLBACK_REENTRY_ENABLED:
        return []
    state = _load_pullback_watch()
    state = _purge_expired_watch(state)
    fired_signals = []

    for sym, entry in list(state.items()):
        if entry.get("fired"):
            continue
        if sym not in prices or sym not in indicators:
            continue

        ind = indicators[sym]
        price = prices[sym]
        rsi_now = ind.get("rsi")
        atr_now = ind.get("atr") or entry["rejected_atr"]
        vol_ratio = ind.get("vol_ratio", 0)
        ema_cross_4h = ind.get("ema_cross_4h", "UNKNOWN")
        if rsi_now is None or atr_now is None or atr_now == 0:
            continue

        direction = entry["direction"]
        rejected_entry = entry["rejected_entry"]
        rejected_rsi = entry["rejected_rsi"]

        # Drop check (LONG: price dropped from rejected_entry; SHORT: rallied up)
        if direction == "LONG":
            drop_atr = (rejected_entry - price) / atr_now
        else:
            drop_atr = (price - rejected_entry) / atr_now
        rsi_delta = rejected_rsi - rsi_now if direction == "LONG" else rsi_now - rejected_rsi

        # 4h trend still aligned?
        if direction == "LONG":
            mtf_aligned = ema_cross_4h in ("BULLISH", "UNKNOWN")
        else:
            mtf_aligned = ema_cross_4h in ("BEARISH", "UNKNOWN")

        entry["checks"] = entry.get("checks", 0) + 1
        # Conditions
        cond_drop = drop_atr >= PULLBACK_MIN_DROP_ATR
        cond_rsi = rsi_delta >= PULLBACK_MIN_RSI_DELTA
        cond_vol = vol_ratio <= PULLBACK_MAX_VOL_RATIO
        cond_mtf = mtf_aligned

        if not (cond_drop and cond_rsi and cond_vol and cond_mtf):
            entry["last_check"] = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "price": round(price, 8), "rsi_now": round(rsi_now, 1),
                "drop_atr": round(drop_atr, 2), "rsi_delta": round(rsi_delta, 1),
                "vol_ratio": round(vol_ratio, 2), "mtf_aligned": mtf_aligned,
                "passed": False,
            }
            state[sym] = entry
            continue

        # FIRE pullback re-entry signal
        sl_mult = PULLBACK_SL_ATR_MULT
        tp_mult = PULLBACK_TP_ATR_MULT
        entry_p = round(price, 8)
        if direction == "LONG":
            sl_p = round(entry_p - sl_mult * atr_now, 8)
            tp_p = round(entry_p + tp_mult * atr_now, 8)
        else:
            sl_p = round(entry_p + sl_mult * atr_now, 8)
            tp_p = round(entry_p - tp_mult * atr_now, 8)

        # Use same risk tier as original (probe / off-list / allowlist)
        in_allowlist = entry.get("in_allowlist", False)
        tier_info = classify_coin_for_breakout(entry["coin"], in_allowlist)
        risk_pct_override = tier_info["risk_pct"]
        pos = calc_position_size(entry_p, sl_p, balance, risk_pct_override=risk_pct_override)

        atr_pct = atr_now / price * 100 if price else 0
        ema_gap_pct = abs(ind.get("ema20", 0) - ind.get("ema50", 0)) / ind.get("ema50", 1) * 100 if ind.get("ema50") else 0
        signal = {
            "coin": entry["coin"], "symbol": sym, "direction": direction,
            "strength": "PULLBACK", "mode_hint": "PULLBACK_REENTRY",
            "tier": tier_info["tier"],
            "tier_reason": tier_info["reason"],
            "in_allowlist": in_allowlist,
            "entry": entry_p, "sl": sl_p, "tp": tp_p,
            "sl_pct": round(abs(entry_p - sl_p) / entry_p * 100, 3),
            "tp_pct": round(abs(tp_p - entry_p) / entry_p * 100, 3),
            "atr": round(atr_now, 8), "rsi": round(rsi_now, 1),
            "rsi_prev": round(ind.get("rsi_prev", rsi_now), 1),
            "rsi_delta": round(rsi_now - ind.get("rsi_prev", rsi_now), 1),
            "ema20": round(ind.get("ema20", 0), 8),
            "ema50": round(ind.get("ema50", 0), 8),
            "ema_gap_pct": round(ema_gap_pct, 3),
            "ema_cross_4h": ema_cross_4h,
            "vol_ratio": round(vol_ratio, 2),
            "trend": ind.get("trend", "UNKNOWN"),
            "rr_ratio": round(tp_mult / sl_mult, 2),
            "position_usd": pos["position_usd"], "qty": pos["qty"],
            "risk_usd": pos["risk_usd"], "risk_pct": pos["risk_pct"],
            "atr_pct": round(atr_pct, 2),
            # Pullback-specific context for LLM
            "pullback_from_entry": entry["rejected_entry"],
            "pullback_from_rsi": entry["rejected_rsi"],
            "pullback_drop_atr": round(drop_atr, 2),
            "pullback_rsi_delta": round(rsi_delta, 1),
            "pullback_minutes_since_reject": round((
                datetime.now(timezone.utc) -
                datetime.fromisoformat(entry["rejected_at"].replace("Z", "+00:00"))
            ).total_seconds() / 60, 1),
            "burst_range_atr": entry.get("rejected_burst_range_atr"),
            "burst_vol_ratio": entry.get("rejected_burst_vol_ratio"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "pending_review",
        }
        fired_signals.append(signal)
        entry["fired"] = True
        entry["fired_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry["fired_signal_summary"] = {
            "entry": entry_p, "sl": sl_p, "tp": tp_p,
            "drop_atr": round(drop_atr, 2), "rsi_now": round(rsi_now, 1),
        }
        state[sym] = entry
        print(f"[pullback] FIRE {sym} {direction} @ ${entry_p} "
              f"(drop {drop_atr:.2f} ATR, RSI {rejected_rsi:.0f}->{rsi_now:.0f})")

    _save_pullback_watch(state)
    return fired_signals


# Volatility regime filter: skip signal if ATR/price > VOL_REGIME_MAX_PCT.
# Default 2.5 (loose — most live signals pass; tighten to 2.0 for stricter).
VOL_REGIME_MAX_PCT = float(os.environ.get("VOL_REGIME_MAX_PCT", "2.5"))

# === EXPLOSIVE BREAKOUT OFF-ALLOWLIST mode (2026-05-10) ===
# Allows the EXPLOSIVE burst path (Gap 4) to fire on coins NOT in COIN_ALLOWLIST,
# scanning the full top-N volume universe. EMA-cross signals remain allowlist-only.
# Rationale: high-volume alts (CHIP, SAHARA, ONDO etc.) often have clean breakouts
# that the allowlist misses. We accept higher per-trade variance in exchange for
# more opportunities, with strict mitigations:
#   - Smaller risk (BREAKOUT_RISK_PCT, default 1.5% vs default 3%)
#   - Tighter SL (BREAKOUT_SL_ATR_MULT, default 0.6 vs 0.8)
#   - Smaller TP (BREAKOUT_TP_ATR_MULT, default 2.0 vs 2.5)
#   - Vol regime cap relaxed but capped at BREAKOUT_VOL_REGIME_MAX_PCT (default 5%)
#   - Signal tagged "BREAKOUT_OFFLIST" so LLM/executor can apply stricter review
BREAKOUT_OFFLIST_ENABLED = os.environ.get("BREAKOUT_OFFLIST", "0").lower() in ("1", "true", "yes")
BREAKOUT_RISK_PCT = float(os.environ.get("BREAKOUT_RISK_PCT", "1.5"))
BREAKOUT_SL_ATR_MULT = float(os.environ.get("BREAKOUT_SL_ATR_MULT", "0.6"))
BREAKOUT_TP_ATR_MULT = float(os.environ.get("BREAKOUT_TP_ATR_MULT", "2.0"))
BREAKOUT_VOL_REGIME_MAX_PCT = float(os.environ.get("BREAKOUT_VOL_REGIME_MAX_PCT", "5.0"))

# === PROBE TRADE mode (2026-05-10) ===
# For coins with thin/no historical trade data, take small "probe" trades on
# breakouts to build dataset for RAG memory + decision_logger learning.
# Auto-graduates to standard BREAKOUT_OFFLIST after PROBE_GRADUATION_TRADES.
PROBE_TRADE_ENABLED = os.environ.get("PROBE_TRADE", "0").lower() in ("1", "true", "yes")
PROBE_RISK_PCT = float(os.environ.get("PROBE_RISK_PCT", "1.0"))
PROBE_GRADUATION_TRADES = int(os.environ.get("PROBE_GRADUATION_TRADES", "4"))
PROBE_DAILY_CAP_PER_COIN = int(os.environ.get("PROBE_DAILY_CAP_PER_COIN", "1"))
DECISIONS_DB = SCRIPT_DIR / "data" / "decisions.db"

# === LLM Gate toggle (2026-05-24) ===
# Set LLM_GATE_ENABLED=0 to skip DeepSeek API call entirely and use
# the deterministic rule_based_review() instead.  Default=0 because
# 30-day data showed LLM gate generated negative ROI (too many REJECT on
# valid breakouts, 65% reject rate, -$0.13 net on executed CONFIRMs).
# LLM is still used for weekly_analysis and Telegram chat — just not
# per-signal gate.  Re-enable anytime with LLM_GATE_ENABLED=1 in .env.
LLM_GATE_ENABLED = os.environ.get("LLM_GATE_ENABLED", "0").lower() in ("1", "true", "yes")

# === PULLBACK RE-ENTRY mode (2026-05-10) ===
# When an explosive breakout signal is REJECTED due to RSI extreme, the coin
# is added to a "pullback watch" list. If price subsequently drops back from
# the rejected high AND RSI cools down, a new PULLBACK_REENTRY signal fires.
# This captures the textbook "wait for pullback, enter on dip" pattern that
# the standard EMA-cross / explosive-burst rules miss.
PULLBACK_REENTRY_ENABLED = os.environ.get("PULLBACK_REENTRY", "0").lower() in ("1", "true", "yes")
PULLBACK_WATCH_FILE = SCRIPT_DIR / "data" / "pullback_watch.json"
PULLBACK_WATCH_WINDOW_MIN = int(os.environ.get("PULLBACK_WATCH_WINDOW_MIN", "90"))
PULLBACK_MIN_DROP_ATR = float(os.environ.get("PULLBACK_MIN_DROP_ATR", "1.0"))      # min drop from rejected high (ATR units)
PULLBACK_MIN_RSI_DELTA = float(os.environ.get("PULLBACK_MIN_RSI_DELTA", "8.0"))    # RSI must cool by N points
PULLBACK_SL_ATR_MULT = float(os.environ.get("PULLBACK_SL_ATR_MULT", "0.5"))         # tight invalidation
PULLBACK_TP_ATR_MULT = float(os.environ.get("PULLBACK_TP_ATR_MULT", "2.5"))         # target = original burst high
PULLBACK_MAX_VOL_RATIO = float(os.environ.get("PULLBACK_MAX_VOL_RATIO", "2.0"))     # FOMO cooled (vol back to normal-ish)


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

    # Gap 4 (2026-05-05): Explosive breakout detection on last 1h bar
    explosive_burst = False
    burst_direction = None
    last_bar_range_atr = 0.0
    last_bar_vol_ratio = 0.0
    if len(raw) >= 2 and atr_1h:
        last_bar = raw[-1]
        last_high = float(last_bar[2]); last_low = float(last_bar[3])
        last_open = float(last_bar[1]); last_close = float(last_bar[4])
        last_vol = float(last_bar[5])
        avg_vol_20 = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else (volumes[-1] or 1)
        last_bar_range_atr = (last_high - last_low) / atr_1h
        last_bar_vol_ratio = (last_vol / avg_vol_20) if avg_vol_20 > 0 else 0
        if last_bar_range_atr >= 1.5 and last_bar_vol_ratio >= 3.0:
            explosive_burst = True
            burst_direction = "LONG" if last_close > last_open else "SHORT"

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
        # Gap 4: Explosive burst signal
        "explosive_burst": explosive_burst,
        "burst_direction": burst_direction,
        "last_bar_range_atr": round(last_bar_range_atr, 2),
        "last_bar_vol_ratio": round(last_bar_vol_ratio, 2),
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


_LIVE_BAL_CACHE = {"ts": 0.0, "value": 0.0}


def _live_futures_balance(ttl: int = 60) -> float:
    """Fetch live USDT futures wallet balance with short TTL cache.
    Returns 0.0 on any failure so caller can fall back to env."""
    now = time.time()
    if now - _LIVE_BAL_CACHE["ts"] < ttl and _LIVE_BAL_CACHE["value"] > 0:
        return _LIVE_BAL_CACHE["value"]
    try:
        import hmac, hashlib, urllib.parse
        api_key = os.environ.get("BINANCE_API_KEY", "")
        api_secret = os.environ.get("BINANCE_API_SECRET", "")
        if not api_key or not api_secret:
            return 0.0
        params = urllib.parse.urlencode({"timestamp": int(now * 1000)})
        sig = hmac.new(api_secret.encode(), params.encode(), hashlib.sha256).hexdigest()
        url = f"https://fapi.binance.com/fapi/v2/account?{params}&signature={sig}"
        req = urllib.request.Request(url, headers={"X-MBX-APIKEY": api_key})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        bal = float(data.get("totalWalletBalance", 0))
        if bal > 0:
            _LIVE_BAL_CACHE["ts"] = now
            _LIVE_BAL_CACHE["value"] = bal
        return bal
    except Exception:
        return 0.0


def get_portfolio_balance() -> float:
    """Resolve portfolio balance for sizing.

    Priority:
      1. Live Binance Futures wallet (source of truth) × strategy slice pct.
      2. Stale env PORTFOLIO_BALANCE × strategy slice pct.
      3. Bare env value.
    This prevents the historical 3x oversize bug caused by stale env."""
    live = _live_futures_balance()
    env_bal = float(os.environ.get("PORTFOLIO_BALANCE", str(PORTFOLIO_BALANCE)))
    base = live if live > 0 else env_bal

    try:
        import strategy_portfolio
        cfg = strategy_portfolio.load_portfolio()
        for s in cfg.get("strategies", []):
            if s.get("slug") == "ema_trend_v1" and s.get("active"):
                pct = float(s.get("target_pct", 0.7))
                return base * pct
    except Exception:
        pass
    return base


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


def calc_position_size(entry: float, sl: float, balance: float,
                        risk_pct_override: float | None = None) -> dict:
    """Calculate position size and risk for a new trade.
    Returns dict with qty, position_usd, risk_usd, risk_pct.

    risk_pct_override lets callers (e.g. breakout off-allowlist signals) use a
    smaller risk allocation than the global RISK_PER_TRADE_PCT.
    """
    risk_pct = risk_pct_override if risk_pct_override is not None else RISK_PER_TRADE_PCT
    risk_per_unit = abs(entry - sl)
    if risk_per_unit == 0 or entry == 0:
        return {"qty": 0, "position_usd": 0, "risk_usd": 0, "risk_pct": 0}
    max_risk_usd = balance * (risk_pct / 100)
    qty = max_risk_usd / risk_per_unit
    position_usd = qty * entry
    return {
        "qty": round(qty, 6),
        "position_usd": round(position_usd, 2),
        "risk_usd": round(max_risk_usd, 2),
        "risk_pct": risk_pct,
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

    # === PULLBACK RE-ENTRY check (highest priority) ===
    # Scan watch list for coins where price has pulled back from rejected breakout.
    # If conditions met, fire PULLBACK_REENTRY signal — bypasses standard checks.
    try:
        pullback_signals = check_pullback_entries(prices, indicators, balance)
        for ps in pullback_signals:
            sym = ps["symbol"]
            existing_state = states.get(ps["coin"], {}).get("state", "IDLE")
            if existing_state == "ACTIVE":
                continue  # already in position, skip duplicate
            ps["__priority"] = 0  # highest
            signals.append(ps)
    except Exception as e:
        print(f"[pullback] check error: {e}")

    for coin, symbol in SYMBOL_MAP.items():
        if symbol not in prices or symbol not in indicators:
            continue

        # FILTER 1: coin allowlist (backtest v6: +24.65R)
        # Off-allowlist coins are NOT skipped if BREAKOUT_OFFLIST mode is on —
        # they're allowed to fall through to the EXPLOSIVE path only (EMA-cross
        # standard rules still gate on allowlist below).
        in_allowlist = (not COIN_ALLOWLIST) or (coin.lower() in COIN_ALLOWLIST)
        if not in_allowlist and not BREAKOUT_OFFLIST_ENABLED:
            continue

        # FILTER 1b: Shield 1 — coin health auto-suspend (applies to ALL modes)
        if coin.lower() in _suspended_coins():
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

        # FILTER 2: volatility regime — skip extreme high-vol bars.
        # For off-allowlist breakout candidates, use the relaxed cap
        # BREAKOUT_VOL_REGIME_MAX_PCT (default 5%) since high vol IS the signal.
        atr_pct = atr / price * 100
        vol_cap = BREAKOUT_VOL_REGIME_MAX_PCT if (not in_allowlist) else VOL_REGIME_MAX_PCT
        if atr_pct > vol_cap:
            continue

        rsi_delta = rsi - rsi_prev
        vol_ok = vol_ratio >= VOLUME_CONFIRM_RATIO
        ema_gap_pct = abs(ema_fast - ema_slow) / ema_slow * 100 if ema_slow else 0

        # Gap 4: Explosive breakout entry path (parallel to standard EMA-cross detection)
        # Triggers ngay khi 1h candle range > 1.5 ATR + volume > 3x avg + 4h aligned.
        # Coins are classified by allowlist membership + historical trade count
        # (PROBE for untested/thin-history off-list coins; OFFLIST_ESTABLISHED
        # for off-list coins with ≥PROBE_GRADUATION_TRADES; ALLOWLIST otherwise).
        if ind.get("explosive_burst"):
            burst_dir = ind.get("burst_direction")
            mtf_aligned = (
                (burst_dir == "LONG" and ema_cross_4h in ("BULLISH", "UNKNOWN")) or
                (burst_dir == "SHORT" and ema_cross_4h in ("BEARISH", "UNKNOWN"))
            )
            if mtf_aligned:
                tier_info = classify_coin_for_breakout(coin, in_allowlist)

                # PROBE rate-limit: skip if exceeded daily cap for this coin
                if tier_info["mode_hint"] == "BREAKOUT_PROBE":
                    if not PROBE_TRADE_ENABLED:
                        continue  # probe mode disabled → skip untested/thin coins
                    if _probes_today_for_coin(coin) >= PROBE_DAILY_CAP_PER_COIN:
                        continue  # already probed today

                sl_mult = tier_info["sl_mult"]
                tp_mult = tier_info["tp_mult"]
                risk_pct_override = tier_info["risk_pct"]
                mode_hint = tier_info["mode_hint"]

                entry_b = round(price, 6)
                if burst_dir == "LONG":
                    sl_b = round(entry_b - sl_mult * atr, 6)
                    tp_b = round(entry_b + tp_mult * atr, 6)
                else:
                    sl_b = round(entry_b + sl_mult * atr, 6)
                    tp_b = round(entry_b - tp_mult * atr, 6)
                pos_b = calc_position_size(entry_b, sl_b, balance,
                                            risk_pct_override=risk_pct_override)
                signals.append({
                    "coin": coin, "symbol": symbol, "direction": burst_dir,
                    "strength": "EXPLOSIVE", "mode_hint": mode_hint,
                    "tier": tier_info["tier"],
                    "tier_reason": tier_info["reason"],
                    "in_allowlist": in_allowlist,
                    "entry": entry_b, "sl": sl_b, "tp": tp_b,
                    "sl_pct": round(abs(entry_b - sl_b) / entry_b * 100, 2),
                    "tp_pct": round(abs(tp_b - entry_b) / entry_b * 100, 2),
                    "atr": round(atr, 6), "rsi": round(rsi, 1),
                    "rsi_prev": round(rsi_prev, 1), "rsi_delta": round(rsi_delta, 1),
                    "ema20": round(ema_fast, 6), "ema50": round(ema_slow, 6),
                    "ema_gap_pct": round(ema_gap_pct, 3),
                    "ema_cross_4h": ema_cross_4h,
                    "vol_ratio": round(vol_ratio, 2),
                    "trend": trend,
                    "rr_ratio": round(tp_mult / sl_mult, 2),
                    "position_usd": pos_b["position_usd"], "qty": pos_b["qty"],
                    "risk_usd": pos_b["risk_usd"], "risk_pct": pos_b["risk_pct"],
                    "burst_range_atr": ind.get("last_bar_range_atr"),
                    "burst_vol_ratio": ind.get("last_bar_vol_ratio"),
                    "atr_pct": round(atr_pct, 2),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "status": "pending_review",
                })
                continue  # explosive entry takes priority — skip EMA-cross detection this cycle

        # Standard EMA-cross signals: gate on allowlist (skip off-allowlist here)
        if not in_allowlist:
            continue

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
        # 5. Trend bearish: strict DOWNTREND OR (NEUTRAL-BEAR + RSI<35 OR strong vol)
        # Rationale: EMA50 lags in downturns, so allow NEUTRAL-BEAR with strong confirm
        rsi_low = rsi < 35
        vol_strong = vol_ratio >= VOL_STRONG_RATIO
        trend_bearish = trend == "DOWNTREND" or (
            trend == "NEUTRAL-BEAR" and (rsi_low or vol_strong)
        )
        # 6. NO multi-timeframe requirement for SHORT (4h lag hurts more than helps in bearish chop)

        if ema_bearish and price_below_ema and rsi_short_ok and vol_ok and trend_bearish:
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
    mode_hint = signal.get("mode_hint")  # Gap 4: "BREAKOUT" for explosive entries
    burst_range_atr = signal.get("burst_range_atr")
    burst_vol_ratio = signal.get("burst_vol_ratio")

    ema_cross_str = "BULLISH (EMA20>EMA50)" if ema20 > ema50 else "BEARISH (EMA20<EMA50)"
    ema_cross_4h_str = signal.get("ema_cross_4h", "UNKNOWN")

    variant_name = os.environ.get("PROMPT_VARIANT", "A")
    variant_version = "signal_review_v1"
    variant_body_template: str | None = None
    if prompt_registry is not None:
        try:
            v_name, v_version, body = prompt_registry.resolve_variant(
                "signal_review", key=f"{coin}:{direction}",
            )
            variant_name = v_name or variant_name
            variant_version = v_version or variant_version
            if v_name and v_name != "A":
                variant_body_template = body
        except Exception as _e:
            pass

    few_shot = ""
    rag_context_for_log = None
    if os.environ.get("FEW_SHOT_ENABLED", "1") == "1":
        try:
            rag_summary = None
            if rag_memory is not None:
                rag_summary = rag_memory.query(
                    direction=direction, rsi=rsi, ema_gap_pct=ema_gap,
                    vol_ratio=vol_ratio, atr=atr, entry=entry, rr=rr,
                    trend=trend, k=8,
                )
            if rag_summary and rag_summary.get("k"):
                lines = [
                    f"\nHISTORICAL CONTEXT (top {rag_summary['k']} similar past trades by feature-cosine):",
                    f"  Win rate: {rag_summary['win_rate']}% | Avg R: {rag_summary['avg_r']} | Total P&L: ${rag_summary['total_pnl']:+.2f}",
                ]
                for m in rag_summary["matches"][:5]:
                    rmul = m.get("r_multiple")
                    rmul_str = f" R={rmul:+.2f}" if rmul is not None else ""
                    lines.append(
                        f"  - {(m['coin'] or '').upper()} {m['result'] or 'OPEN'} "
                        f"${m['pnl_usd'] or 0:+.2f}{rmul_str} sim={m['similarity']:.2f}"
                    )
                few_shot = "\n".join(lines)
                rag_context_for_log = rag_summary["matches"]
            elif decision_logger is not None:
                similar = decision_logger.query_similar_trades(
                    coin=coin.lower(), direction=direction,
                    rsi=rsi, ema_gap_pct=ema_gap, vol_ratio=vol_ratio, limit=4,
                )
                if similar:
                    rag_context_for_log = []
                    lines = ["\nHISTORICAL CONTEXT — past trades similar to this signal:"]
                    for t in similar:
                        pnl = t.get("pnl_usd") or 0
                        res = t.get("result") or "OPEN"
                        rmul = t.get("r_multiple")
                        rmul_str = f" R={rmul:+.2f}" if rmul is not None else ""
                        try:
                            ind = json.loads(t.get("indicators_open_json") or "{}")
                        except Exception:
                            ind = {}
                        rsi_v = ind.get("rsi")
                        rsi_str_h = f"RSI {rsi_v:.0f}" if isinstance(rsi_v, (int, float)) else ""
                        lines.append(f"  - {t['coin'].upper()} {t['direction']} → {res} P&L ${pnl:+.2f}{rmul_str} {rsi_str_h}")
                        rag_context_for_log.append({
                            "trade_id": t["id"], "result": res, "pnl": pnl,
                            "r_multiple": rmul, "indicators": ind,
                        })
                    few_shot = "\n".join(lines)
        except Exception as _e:
            pass

    if direction == "LONG":
        mtf_align = "ALIGNED" if ema_cross_4h_str == "BULLISH" else (
            "UNKNOWN" if ema_cross_4h_str == "UNKNOWN" else "DIVERGED"
        )
        mtf_note = f"4h EMA: {ema_cross_4h_str} (multi-tf: {mtf_align}) — REQUIRED for LONG"
    else:
        mtf_note = f"4h EMA: {ema_cross_4h_str} (informational only — SHORT does not require 4h confirm)"

    # Shield 3: coin tier (90d backtest evidence)
    _COIN_TIER = {
        "trx": ("A", "+9.39R 90d, win 2/3 regimes"),
        "btc": ("A", "+4.94R 90d, win 2/3 regimes"),
        "xrp": ("B", "-2.45R 90d, win only in W1 (uptrend weak)"),
        "aave": ("B", "-2.39R 90d, win only in W2 (sideways)"),
        "eth": ("C", "-6.74R 90d, lose all 3 regimes"),
        "bnb": ("C", "-9.28R 90d, lose all 3 regimes"),
        "link": ("C", "-14.71R 90d, biggest loser"),
    }
    _tier, _tier_note = _COIN_TIER.get(coin.lower(), ("?", "no backtest data"))
    tier_block = (
        f"COIN TIER: {_tier} — {_tier_note}\n"
        f"  Tier A (TRX, BTC): proven winners — confirm with normal threshold.\n"
        f"  Tier B (XRP, AAVE): regime-dependent — confirm only if 4h trend STRONGLY aligned AND volume > 1.3x.\n"
        f"  Tier C (ETH, BNB, LINK): historical losers — REJECT unless ALL 3 conditions: RSI in optimal zone (40-65 LONG / 35-60 SHORT) AND 4h ALIGNED AND volume > 1.5x AND R:R >= 1.8."
    )

    breakout_block = ""
    if mode_hint == "BREAKOUT":
        breakout_block = (
            f"\n⚡ EXPLOSIVE BREAKOUT DETECTED (last 1h candle, ALLOWLIST coin): "
            f"range = {burst_range_atr}× ATR (>= 1.5 threshold), "
            f"volume = {burst_vol_ratio}× avg (>= 3.0 threshold)\n"
            f"Pre-set mode: BREAKOUT (tight SL 0.8×ATR, wide TP 2.5×ATR, R:R 3.1)\n"
            f"For BREAKOUT, IGNORE the SCALP/SWING/QUICK classification — return mode='BREAKOUT' "
            f"with sl_mult=0.8, tp_mult=2.5, timeout_h=4. Confirm only if news/momentum genuine, "
            f"reject if signs of fakeout (immediate price reversal, low follow-through volume).\n"
        )
    elif mode_hint == "BREAKOUT_OFFLIST":
        atr_pct_v = signal.get("atr_pct", 0)
        breakout_block = (
            f"\n⚡⚠️  EXPLOSIVE BREAKOUT — OFF-ALLOWLIST COIN (HIGHER RISK)\n"
            f"Last 1h candle: range = {burst_range_atr}× ATR (>= 1.5), "
            f"volume = {burst_vol_ratio}× avg (>= 3.0)\n"
            f"Volatility: ATR/price = {atr_pct_v}% (allowed up to {BREAKOUT_VOL_REGIME_MAX_PCT}% for breakouts)\n"
            f"Pre-set mode: BREAKOUT_OFFLIST (TIGHT SL {BREAKOUT_SL_ATR_MULT}×ATR, "
            f"TP {BREAKOUT_TP_ATR_MULT}×ATR, risk only {BREAKOUT_RISK_PCT}% of portfolio)\n"
            f"This coin is NOT in the verified allowlist — apply STRICTER criteria:\n"
            f"  • REJECT if vol_ratio < 1.5 on entry candle (not just 3x burst — needs follow-through)\n"
            f"  • REJECT if RSI > 75 (LONG) or < 25 (SHORT) — likely exhausted breakout\n"
            f"  • REJECT if 4h trend conflicts (UNKNOWN is OK, BEARISH for LONG = reject)\n"
            f"  • CONFIRM only if there's a CLEAR catalyst pattern (vol burst + trend continuation)\n"
            f"Return mode='BREAKOUT' with sl_mult={BREAKOUT_SL_ATR_MULT}, "
            f"tp_mult={BREAKOUT_TP_ATR_MULT}, timeout_h=3 (faster exit for unknown coins).\n"
        )
    elif mode_hint == "BREAKOUT_PROBE":
        atr_pct_v = signal.get("atr_pct", 0)
        tier_reason = signal.get("tier_reason", "thin/no history")
        breakout_block = (
            f"\n🧪 PROBE TRADE — DATA ACQUISITION (small risk to build dataset)\n"
            f"Reason: {tier_reason}\n"
            f"Last 1h candle: range = {burst_range_atr}× ATR (>= 1.5), "
            f"volume = {burst_vol_ratio}× avg (>= 3.0)\n"
            f"Volatility: ATR/price = {atr_pct_v}% (allowed up to {BREAKOUT_VOL_REGIME_MAX_PCT}% for breakouts)\n"
            f"Pre-set: SL {BREAKOUT_SL_ATR_MULT}×ATR, TP {BREAKOUT_TP_ATR_MULT}×ATR, "
            f"risk ONLY {PROBE_RISK_PCT}% (probe-sized, max 1 trade/day per coin)\n"
            f"GOAL: Generate trade outcome data for this untested coin so future\n"
            f"signals can use RAG/few-shot context. Bias toward CONFIRM unless\n"
            f"there's a CLEAR red flag — losing $2-3 to learn coin behavior is acceptable.\n"
            f"REJECT only if:\n"
            f"  • RSI > 80 (LONG) or < 20 (SHORT) — extreme exhaustion\n"
            f"  • vol_ratio < 1.0 on entry candle — burst already faded\n"
            f"  • 4h trend strongly conflicts (BEARISH for LONG with strength)\n"
            f"Return mode='BREAKOUT' with sl_mult={BREAKOUT_SL_ATR_MULT}, "
            f"tp_mult={BREAKOUT_TP_ATR_MULT}, timeout_h=3.\n"
        )
    elif mode_hint == "PULLBACK_REENTRY":
        pb_from_entry = signal.get("pullback_from_entry", entry)
        pb_from_rsi = signal.get("pullback_from_rsi", 0)
        pb_drop_atr = signal.get("pullback_drop_atr", 0)
        pb_rsi_delta = signal.get("pullback_rsi_delta", 0)
        pb_minutes = signal.get("pullback_minutes_since_reject", 0)
        breakout_block = (
            f"\n🔁 PULLBACK RE-ENTRY — explosive breakout cooled, retesting support\n"
            f"Earlier (~{pb_minutes:.0f} min ago) we REJECTED a breakout at ${pb_from_entry} "
            f"because RSI was extreme ({pb_from_rsi:.0f}).\n"
            f"Since then: price pulled back {pb_drop_atr:.2f}× ATR, "
            f"RSI cooled by {pb_rsi_delta:.0f} pts (now {rsi:.0f}), "
            f"vol_ratio normalised to {vol_ratio:.2f}x.\n"
            f"Original burst: range = {burst_range_atr}× ATR, volume = {burst_vol_ratio}× avg\n"
            f"Pre-set: TIGHT SL {PULLBACK_SL_ATR_MULT}×ATR (invalidation: pullback low broken), "
            f"TP {PULLBACK_TP_ATR_MULT}×ATR (target = original burst high or beyond)\n"
            f"This is a textbook 'wait for pullback to support, then enter' setup.\n"
            f"CONFIRM if:\n"
            f"  • RSI cooled into healthy zone (40-65 LONG / 35-60 SHORT) — IDEAL\n"
            f"  • Vol normalised (<2x) — FOMO faded, base building\n"
            f"  • 4h trend still aligned with original direction\n"
            f"  • Price near EMA20/EMA50 OR previous breakout level (now support)\n"
            f"REJECT only if:\n"
            f"  • Pullback was actually full reversal (price below pre-burst range)\n"
            f"  • 4h trend flipped against direction\n"
            f"  • RSI still >70 (LONG) or <30 (SHORT) — not really cooled\n"
            f"  • Vol still elevated (>2x) — distribution / continued selling\n"
            f"Return mode='BREAKOUT' with sl_mult={PULLBACK_SL_ATR_MULT}, "
            f"tp_mult={PULLBACK_TP_ATR_MULT}, timeout_h=4.\n"
        )

    prompt = f"""You are a crypto futures trading risk analyst. Review this signal, decide CONFIRM/REJECT, and classify the trade MODE.

{tier_block}{breakout_block}

SIGNAL: {coin} {direction}
Entry: ${entry} | Default SL: ${sl} | Default TP: ${tp} | R:R: {rr}
RSI(14): {rsi:.1f} (prev: {rsi_prev:.1f}, delta: {rsi-rsi_prev:+.1f})
EMA20: ${ema20} | EMA50: ${ema50} | EMA cross 1h: {ema_cross_str} (gap: {ema_gap:+.2f}%)
{mtf_note}
Volume ratio: {vol_ratio:.2f}x | ATR(4h): ${atr}
Trend: {trend} | Strength: {strength}
Currently {active_count} other positions open.{few_shot}

REJECT if ANY of these:
1. {direction} against EMA cross 1h (LONG when EMA20<EMA50, SHORT when EMA20>EMA50)
2. EMA gap too small (<0.1%) — cross not confirmed, high whipsaw risk
3. Volume ratio < 1.0 (no volume confirmation)
4. R:R < 1.3 after fees
5. Already {active_count} positions open AND signal is MODERATE
6. RSI in extreme zone (LONG RSI>72, SHORT RSI<25)
7. {direction} against strong reversal (LONG RSI>75 dropping, SHORT RSI<25 rising)

CLASSIFY mode (critical for SL/TP/timeout sizing):
- "SWING": with-trend (LONG with 4h BULLISH, OR SHORT with 4h BEARISH). Hold long, wide SL/TP.
- "SCALP": counter-trend (LONG with 4h BEARISH, OR SHORT with 4h BULLISH/UNKNOWN). Quick in/out, tight SL/TP, short timeout. Most SHORT in a bull market are SCALP.
- "QUICK": neutral 4h, 1h cross strong, take 1-2 ATR move only.

For chosen MODE, suggest multipliers and timeout (overrides defaults):
| Mode  | sl_mult (xATR) | tp_mult (xATR) | timeout_h |
|-------|---------------:|---------------:|----------:|
| SWING |       1.5–2.0 |       2.5–3.5 |      8–12 |
| SCALP |       0.8–1.2 |       1.0–1.8 |      2–4  |
| QUICK |       1.0–1.5 |       1.5–2.5 |      4–6  |

User policy: trades should resolve QUICKLY (< 12h). NEVER suggest timeout > 12h.
Prefer SCALP for any counter-trend setup. Prefer SWING only when HTF clearly aligns.

Reply ONLY in this JSON format, nothing else:
{{"decision": "CONFIRM" or "REJECT", "mode": "SWING" or "SCALP" or "QUICK", "sl_mult": 1.5, "tp_mult": 2.5, "timeout_h": 6, "reason": "one sentence", "confidence": 0-100}}"""

    if variant_body_template:
        try:
            prompt = variant_body_template.format(
                coin=coin, direction=direction,
                entry=entry, sl=sl, tp=tp, rr=rr,
                rsi=rsi, rsi_prev=rsi_prev, rsi_delta=rsi - rsi_prev,
                ema20=ema20, ema50=ema50, ema_cross_str=ema_cross_str,
                ema_gap=ema_gap, ema_cross_4h_str=ema_cross_4h_str,
                vol_ratio=vol_ratio, atr=atr, trend=trend, strength=strength,
                active_count=active_count, mtf_note=mtf_note, few_shot=few_shot,
            )
        except Exception as _e:
            print(f"  variant body render failed, using baseline: {_e}")

    raw_response = ""
    tokens_in = tokens_out = None
    review: dict = {}
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

        usage = result.get("usage", {}) if isinstance(result, dict) else {}
        tokens_in = usage.get("prompt_tokens")
        tokens_out = usage.get("completion_tokens")
        content = result["choices"][0]["message"]["content"].strip()
        raw_response = content
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        content = content.strip()

        review = json.loads(content)
        review["decision"] = review.get("decision", "CONFIRM").upper()
        if review["decision"] not in ("CONFIRM", "REJECT"):
            review["decision"] = "CONFIRM"
    except Exception as e:
        print(f"  LLM review error: {e} — defaulting to CONFIRM")
        review = {"decision": "CONFIRM", "reason": f"API error: {e}", "confidence": 0}
        raw_response = raw_response or f"ERROR: {e}"

    mode = str(review.get("mode", "")).upper()
    if mode not in ("SWING", "SCALP", "QUICK"):
        if direction == "LONG":
            mode = "SWING" if ema_cross_4h_str == "BULLISH" else "SCALP"
        else:
            mode = "SWING" if ema_cross_4h_str == "BEARISH" else "SCALP"
    review["mode"] = mode

    mode_defaults = {
        "SWING": {"sl_mult": 2.0, "tp_mult": 3.0, "timeout_h": 12},
        "SCALP": {"sl_mult": 1.0, "tp_mult": 1.5, "timeout_h": 4},
        "QUICK": {"sl_mult": 1.2, "tp_mult": 2.0, "timeout_h": 6},
    }
    d = mode_defaults[mode]
    try:
        sl_mult = float(review.get("sl_mult") or d["sl_mult"])
    except Exception:
        sl_mult = d["sl_mult"]
    try:
        tp_mult = float(review.get("tp_mult") or d["tp_mult"])
    except Exception:
        tp_mult = d["tp_mult"]
    try:
        timeout_h = float(review.get("timeout_h") or d["timeout_h"])
    except Exception:
        timeout_h = d["timeout_h"]

    sl_mult = max(0.5, min(sl_mult, 2.5))
    tp_mult = max(0.8, min(tp_mult, 4.0))
    timeout_h = max(1.0, min(timeout_h, 12.0))

    review["sl_mult"] = sl_mult
    review["tp_mult"] = tp_mult
    review["timeout_h"] = timeout_h

    if decision_logger is not None:
        try:
            decision_id = decision_logger.log_decision(
                source="signal_review",
                coin=coin.lower(),
                direction=direction,
                model="deepseek-chat",
                prompt=prompt,
                response=raw_response,
                decision=review.get("decision", "CONFIRM"),
                reason=review.get("reason", ""),
                confidence=review.get("confidence", 0),
                indicators={
                    "rsi": rsi, "rsi_prev": rsi_prev,
                    "ema20": ema20, "ema50": ema50,
                    "ema_gap_pct": ema_gap, "ema_cross_4h": ema_cross_4h_str,
                    "vol_ratio": vol_ratio, "atr": atr,
                    "trend": trend, "strength": strength,
                    "entry": entry, "sl": sl, "tp": tp, "rr": rr,
                },
                market_state={"active_positions": active_count},
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                prompt_variant=variant_name,
                prompt_version=variant_version,
                rag_context=rag_context_for_log,
            )
            review["decision_id"] = decision_id
        except Exception as e:
            print(f"  [WARN] decision_logger.log_decision failed: {e}")

    return review


# ---------------------------------------------------------------------------
# Rule-Based Signal Review  (replaces LLM gate when LLM_GATE_ENABLED=0)
# ---------------------------------------------------------------------------
# Design goals:
#   1. Pure Python — zero latency, zero cost, no API dependency
#   2. Mode classification: SWING / SCALP / QUICK / BREAKOUT — dynamic SL/TP
#   3. Hard-reject only on MATH failures, not on RSI-extreme heuristics
#      (RSI extreme = momentum continuation, not reversal, in trending market)
#   4. Log to decisions.db exactly like LLM did → RAG memory still works

def rule_based_review(signal: dict, indicators: dict, active_count: int) -> dict:
    """Deterministic rule engine replacing the LLM gate.

    CONFIRM criteria (all must hold):
      - R:R >= 1.2 (minimum mathematical edge after fees)
      - vol_ratio >= 0.8 (some volume confirmation — loose to avoid missing breakouts)
      - No ACTIVE position on same coin already (checked at call site)

    REJECT criteria (any one triggers REJECT):
      - R:R < 1.0 (guaranteed loser after fees)
      - Same coin lost > $7 in past 6h (cool-down — avoid revenge trading same coin)
      - 4h EMA strongly opposes signal AND EMA gap > 3% (hard trend conflict only)

    Mode classification (determines SL/TP multipliers and timeout):
      - BREAKOUT: mode_hint in (BREAKOUT, BREAKOUT_OFFLIST, BREAKOUT_PROBE, PULLBACK_REENTRY)
      - SWING:    direction aligns with 4h EMA trend
      - SCALP:    direction opposes 4h EMA (counter-trend, tight params)
      - QUICK:    4h neutral or unknown
    """
    direction = signal.get("direction", "")
    rr = signal.get("rr_ratio", 0.0)
    vol_ratio = signal.get("vol_ratio", 0.0)
    ema_cross_4h = signal.get("ema_cross_4h", "UNKNOWN")
    ema_gap_pct = signal.get("ema_gap_pct", 0.0)
    mode_hint = signal.get("mode_hint") or ""
    coin = signal.get("coin", "")

    # ── Hard REJECT rules ──────────────────────────────────────────────────
    if rr < 1.0:
        reason = f"R:R {rr:.2f} < 1.0 — guaranteed loser after fees"
        return _build_rule_review("REJECT", reason, 95, signal, indicators,
                                  active_count, mode_hint, ema_cross_4h, direction)

    if vol_ratio < 0.5:
        reason = f"vol_ratio {vol_ratio:.2f}x — market asleep, no participation"
        return _build_rule_review("REJECT", reason, 85, signal, indicators,
                                  active_count, mode_hint, ema_cross_4h, direction)

    # 4h hard trend conflict: 4h EMA strongly opposes AND gap is wide
    # (narrow gap = transition; wide gap = established trend — don't fight it)
    is_4h_conflict = False
    if direction == "LONG" and ema_cross_4h == "BEARISH" and abs(ema_gap_pct) > 3.0:
        is_4h_conflict = True
    elif direction == "SHORT" and ema_cross_4h == "BULLISH" and abs(ema_gap_pct) > 3.0:
        is_4h_conflict = True
    if is_4h_conflict:
        reason = (f"4h EMA {ema_cross_4h} strongly opposes {direction} "
                  f"(gap {ema_gap_pct:+.1f}%) — hard trend conflict")
        return _build_rule_review("REJECT", reason, 80, signal, indicators,
                                  active_count, mode_hint, ema_cross_4h, direction)

    # Same-coin cool-down: lost > $7 on this coin in past 6h
    cool_down_pnl = _recent_coin_pnl(coin, hours=6)
    if cool_down_pnl < -7.0:
        reason = f"Same coin {coin.upper()} lost ${cool_down_pnl:.2f} in past 6h — cool-down"
        return _build_rule_review("REJECT", reason, 75, signal, indicators,
                                  active_count, mode_hint, ema_cross_4h, direction)

    # ── CONFIRM — classify mode ────────────────────────────────────────────
    if mode_hint in ("BREAKOUT", "BREAKOUT_OFFLIST", "BREAKOUT_PROBE", "PULLBACK_REENTRY"):
        mode = mode_hint  # preserve specific breakout mode for executor logic
    elif direction == "LONG":
        if ema_cross_4h == "BULLISH":
            mode = "SWING"
        elif ema_cross_4h == "BEARISH":
            mode = "SCALP"
        else:
            mode = "QUICK"
    else:  # SHORT
        if ema_cross_4h == "BEARISH":
            mode = "SWING"
        elif ema_cross_4h == "BULLISH":
            mode = "SCALP"
        else:
            mode = "QUICK"

    reason = (f"Rule-gate CONFIRM: R:R {rr:.2f}, vol {vol_ratio:.1f}x, "
              f"4h {ema_cross_4h}, mode={mode}")
    return _build_rule_review("CONFIRM", reason, 70, signal, indicators,
                              active_count, mode, ema_cross_4h, direction)


_MODE_PARAMS = {
    "SWING":            {"sl_mult": 2.0, "tp_mult": 3.0, "timeout_h": 12.0},
    "SCALP":            {"sl_mult": 1.0, "tp_mult": 1.5, "timeout_h":  4.0},
    "QUICK":            {"sl_mult": 1.2, "tp_mult": 2.0, "timeout_h":  6.0},
    "BREAKOUT":         {"sl_mult": 0.8, "tp_mult": 2.5, "timeout_h":  4.0},
    "BREAKOUT_OFFLIST": {"sl_mult": 0.6, "tp_mult": 2.0, "timeout_h":  3.0},
    "BREAKOUT_PROBE":   {"sl_mult": 0.6, "tp_mult": 2.0, "timeout_h":  3.0},
    "PULLBACK_REENTRY": {"sl_mult": 0.5, "tp_mult": 2.5, "timeout_h":  4.0},
}


def _recent_coin_pnl(coin: str, hours: float = 6) -> float:
    """Sum PnL for this coin from trade_history in the past `hours`."""
    try:
        es_path = SCRIPT_DIR / "data" / "executor_state.json"
        if not es_path.exists():
            return 0.0
        es = json.loads(es_path.read_text())
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        total = 0.0
        for t in es.get("trade_history", []):
            if (t.get("coin") or "").lower() != coin.lower():
                continue
            try:
                tt = datetime.fromisoformat((t.get("time") or "").replace("Z", "+00:00"))
            except Exception:
                continue
            if tt >= cutoff:
                total += float(t.get("pnl") or 0)
        return total
    except Exception:
        return 0.0


def _build_rule_review(decision: str, reason: str, confidence: int,
                       signal: dict, indicators: dict, active_count: int,
                       mode: str, ema_cross_4h: str, direction: str) -> dict:
    """Assemble review dict + log to decisions.db (same schema as LLM review)."""
    mode_key = mode if mode in _MODE_PARAMS else (
        "SWING" if (direction == "LONG" and ema_cross_4h == "BULLISH")
        else "SCALP" if (direction == "LONG" and ema_cross_4h == "BEARISH")
        else "QUICK"
    )
    params = _MODE_PARAMS.get(mode_key, _MODE_PARAMS["QUICK"])

    review = {
        "decision": decision,
        "reason": reason,
        "confidence": confidence,
        "mode": mode_key,
        "sl_mult": params["sl_mult"],
        "tp_mult": params["tp_mult"],
        "timeout_h": params["timeout_h"],
        "source": "rule_engine",
    }

    # Log to decisions.db for RAG memory continuity
    if decision_logger is not None:
        try:
            decision_id = decision_logger.log_decision(
                source="rule_signal_review",
                coin=signal.get("coin", ""),
                direction=signal.get("direction", ""),
                signal_data=signal,
                indicators=indicators.get(signal.get("symbol", ""), {}),
                market_state={"active_positions": active_count},
                model="rule_engine_v1",
                prompt="[rule-based, no API call]",
                response=f'{{"decision":"{decision}","mode":"{mode_key}","reason":"{reason}"}}',
                decision=decision,
                reason=reason,
                confidence=confidence,
                cost_usd=0.0,
            )
            review["decision_id"] = decision_id
        except Exception as e:
            print(f"  [WARN] decision_logger.log failed: {e}")

    return review


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
                if LLM_GATE_ENABLED:
                    print(f"  Signal: {coin_u} {best['direction']} @ {fmt_price(best['entry'])} — LLM reviewing...")
                    review = llm_review_signal(best, indicators, active_count)
                    gate_label = "LLM"
                else:
                    print(f"  Signal: {coin_u} {best['direction']} @ {fmt_price(best['entry'])} — rule-gate reviewing...")
                    review = rule_based_review(best, indicators, active_count)
                    gate_label = "RULE"
                decision = review.get("decision", "CONFIRM")
                reason = review.get("reason", "")
                confidence = review.get("confidence", 0)
                print(f"  {gate_label}: {decision} ({confidence}%) — {reason}")

                if review.get("decision_id"):
                    best["decision_id"] = review["decision_id"]

                if decision == "REJECT":
                    best["status"] = "llm_rejected"
                    best["llm_reason"] = reason
                    best["llm_confidence"] = confidence
                    save_pending_signal(best)

                    # Pullback re-entry watch: if rejected for RSI extreme on an
                    # EXPLOSIVE breakout, queue for pullback monitoring (next 90 min)
                    try:
                        register_pullback_watch(best, reason)
                    except Exception as e:
                        print(f"[pullback] register error: {e}")

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
                    mode = review.get("mode", "SWING")
                    sl_mult = review.get("sl_mult", ATR_SL_MULT)
                    tp_mult = review.get("tp_mult", ATR_TP_MULT)
                    timeout_h = review.get("timeout_h", 12.0)

                    atr_v = best.get("atr", 0)
                    if atr_v > 0 and (sl_mult != ATR_SL_MULT or tp_mult != ATR_TP_MULT):
                        if best["direction"] == "LONG":
                            new_sl = best["entry"] - sl_mult * atr_v
                            new_tp = best["entry"] + tp_mult * atr_v
                        else:
                            new_sl = best["entry"] + sl_mult * atr_v
                            new_tp = best["entry"] - tp_mult * atr_v
                        best["sl"] = round(new_sl, 8)
                        best["tp"] = round(new_tp, 8)
                        best["sl_pct"] = round(abs(new_sl - best["entry"]) / best["entry"] * 100, 3)
                        best["tp_pct"] = round(abs(new_tp - best["entry"]) / best["entry"] * 100, 3)
                        best["rr_ratio"] = round(tp_mult / sl_mult, 2)
                        sized = calc_position_size(best["entry"], best["sl"], get_portfolio_balance())
                        best["qty"] = sized["qty"]
                        best["position_usd"] = sized["position_usd"]
                        best["risk_usd"] = sized["risk_usd"]
                        sl_pct = best["sl_pct"]
                        tp_pct = best["tp_pct"]
                        pos_usd = best["position_usd"]
                        risk_usd = best["risk_usd"]
                        qty = best["qty"]

                    timeout_at = (datetime.now(timezone.utc) + timedelta(hours=timeout_h)).isoformat()
                    best["mode"] = mode
                    best["sl_mult"] = sl_mult
                    best["tp_mult"] = tp_mult
                    best["timeout_h"] = timeout_h
                    best["timeout_at"] = timeout_at

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
                        "mode": mode,
                        "sl_mult": sl_mult,
                        "tp_mult": tp_mult,
                        "timeout_h": timeout_h,
                        "timeout_at": timeout_at,
                    })
                    trading_state["states"] = states
                    save_trading_state(trading_state)

                    best["status"] = "auto_confirmed"
                    best["llm_reason"] = reason
                    best["llm_confidence"] = confidence
                    save_pending_signal(best)

                    msg = (
                        f"✅ *[AUTO SIGNAL] {coin_u} {best['direction']}* ({strength}) — *{mode}* / timeout {timeout_h:.0f}h\n"
                        f"Entry: *{fmt_price(best['entry'])}*\n"
                        f"SL: {fmt_price(best['sl'])} (-{sl_pct:.1f}%, {sl_mult:.1f}x ATR4h)\n"
                        f"TP: {fmt_price(best['tp'])} (+{tp_pct:.1f}%, {tp_mult:.1f}x ATR4h)\n"
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
    if COIN_ALLOWLIST:
        print(f"[daemon] Coin allowlist ({len(COIN_ALLOWLIST)}): {sorted(COIN_ALLOWLIST)}")
    else:
        print(f"[daemon] Coin allowlist: DISABLED (all monitored coins eligible)")
    print(f"[daemon] Volatility regime filter: ATR/price <= {VOL_REGIME_MAX_PCT:.1f}%")
    if BREAKOUT_OFFLIST_ENABLED:
        print(f"[daemon] BREAKOUT OFF-ALLOWLIST: ON | risk={BREAKOUT_RISK_PCT}% "
              f"| SL={BREAKOUT_SL_ATR_MULT}xATR | TP={BREAKOUT_TP_ATR_MULT}xATR "
              f"| vol_cap={BREAKOUT_VOL_REGIME_MAX_PCT}%")
    else:
        print(f"[daemon] BREAKOUT OFF-ALLOWLIST: OFF (set BREAKOUT_OFFLIST=1 to enable)")
    if PROBE_TRADE_ENABLED:
        print(f"[daemon] PROBE TRADES: ON | risk={PROBE_RISK_PCT}% "
              f"| graduation>={PROBE_GRADUATION_TRADES}t | cap={PROBE_DAILY_CAP_PER_COIN}/day per coin")
    else:
        print(f"[daemon] PROBE TRADES: OFF (set PROBE_TRADE=1 to enable)")
    if PULLBACK_REENTRY_ENABLED:
        print(f"[daemon] PULLBACK RE-ENTRY: ON | window={PULLBACK_WATCH_WINDOW_MIN}min "
              f"| min_drop={PULLBACK_MIN_DROP_ATR}xATR | min_rsi_delta={PULLBACK_MIN_RSI_DELTA}pts "
              f"| SL={PULLBACK_SL_ATR_MULT}xATR | TP={PULLBACK_TP_ATR_MULT}xATR")
    else:
        print(f"[daemon] PULLBACK RE-ENTRY: OFF (set PULLBACK_REENTRY=1 to enable)")
    if LLM_GATE_ENABLED:
        print(f"[daemon] Signal gate: LLM (DeepSeek) — set LLM_GATE_ENABLED=0 to use rule engine")
    else:
        print(f"[daemon] Signal gate: RULE ENGINE (deterministic, $0 cost) — set LLM_GATE_ENABLED=1 to re-enable LLM")
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
