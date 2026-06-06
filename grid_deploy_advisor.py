#!/usr/bin/env python3
"""Grid Deploy Advisor — suggest Spot Grid bots when BTC regime turns SIDEWAYS.

Unlike futures auto-trade (momentum/trend), grid bots only work in ranging
markets. This service WATCHES regime and sends a Telegram suggestion when:
  1. BTC 7d regime transitions INTO SIDEWAYS (primary trigger), OR
  2. SIDEWAYS persists and no alert sent in 7 days (gentle reminder, no spam)

It does NOT place orders — suggestions only. User deploys manually on Binance.

State: data/grid_deploy_advisor_state.json
"""
from __future__ import annotations
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
STATE_FILE = ROOT / "data" / "grid_deploy_advisor_state.json"
GRID_CONFIG = ROOT / "data" / "grid_config.json"

# Coins with liquid spot markets, historically reasonable for grids
GRID_CANDIDATES = ["DOTUSDT", "XRPUSDT", "LINKUSDT", "BNBUSDT", "ADAUSDT", "AVAXUSDT"]
MIN_RANGE_PCT = 8.0    # 14d range too tight = no grid profit
MAX_RANGE_PCT = 28.0   # too wide = trending, not sideways
REMINDER_DAYS = 7
SUGGESTED_BUDGET_USD = 500  # per deployment wave (from Reserve)


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
    chat = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_ALERT_CHAT_ID")
    if not (token and chat):
        print("[WARN] Telegram credentials missing")
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat, "text": message,
            "parse_mode": "Markdown", "disable_web_page_preview": "true",
        }).encode()
        urllib.request.urlopen(url, data=data, timeout=15).read()
        return True
    except Exception as e:
        print(f"[WARN] Telegram failed: {e}")
        return False


def get_btc_regime() -> dict:
    try:
        from binance.client import Client
        c = Client(os.environ.get("BINANCE_API_KEY"), os.environ.get("BINANCE_API_SECRET"))
        klines = c.futures_klines(symbol="BTCUSDT", interval="1d", limit=8)
        closes = [float(k[4]) for k in klines]
        first, last = closes[0], closes[-1]
        pct = (last - first) / first * 100
        if pct > 5:
            regime = "UPTREND"
        elif pct < -5:
            regime = "DOWNTREND"
        else:
            regime = "SIDEWAYS"
        return {"regime": regime, "btc_change_pct": round(pct, 2), "btc_last": last}
    except Exception as e:
        return {"regime": "UNKNOWN", "error": str(e)}


def active_grid_count() -> int:
    if not GRID_CONFIG.exists():
        return 0
    try:
        cfg = json.loads(GRID_CONFIG.read_text())
        return sum(
            1 for sym, c in cfg.items()
            if sym.endswith("USDT") and c.get("status") != "closed"
            and c.get("binance_grid_id") != "CLOSED"
        )
    except Exception:
        return 0


def fetch_klines(symbol: str, interval: str, limit: int) -> list:
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={symbol}&interval={interval}&limit={limit}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "OpenClaw/1.0"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def suggest_grid_params(symbol: str) -> dict | None:
    """Build suggested range from 14d high/low. Returns None if unsuitable."""
    try:
        klines = fetch_klines(symbol, "1d", 15)
        highs = [float(k[2]) for k in klines[:-1]]
        lows = [float(k[3]) for k in klines[:-1]]
        price = float(klines[-1][4])
        hi14, lo14 = max(highs), min(lows)
        range_pct = (hi14 - lo14) / lo14 * 100
        if range_pct < MIN_RANGE_PCT or range_pct > MAX_RANGE_PCT:
            return None
        # Grid range: 14d channel with small buffer; stops outside
        lower = round(lo14 * 0.99, 4 if price < 10 else 2)
        upper = round(hi14 * 1.01, 4 if price < 10 else 2)
        stop_lo = round(lower * 0.95, 4 if price < 10 else 2)
        stop_hi = round(upper * 1.05, 4 if price < 10 else 2)
        pos_in_range = (price - lo14) / (hi14 - lo14) * 100 if hi14 > lo14 else 50
        grids = max(30, min(60, int(range_pct * 2)))
        return {
            "symbol": symbol,
            "price": price,
            "lower": lower,
            "upper": upper,
            "stop_lower": stop_lo,
            "stop_upper": stop_hi,
            "grids": grids,
            "range_pct_14d": round(range_pct, 1),
            "pos_in_range_pct": round(pos_in_range, 0),
        }
    except Exception as e:
        print(f"[advisor] {symbol} skip: {e}")
        return None


def rank_candidates() -> list[dict]:
    """Return top grid candidates sorted by suitability (mid-range position)."""
    out = []
    for sym in GRID_CANDIDATES:
        p = suggest_grid_params(sym)
        if not p:
            continue
        # Prefer coins trading mid-range (40-70%) — ideal for grid fills both sides
        mid_score = 100 - abs(p["pos_in_range_pct"] - 55)
        p["score"] = mid_score
        out.append(p)
    out.sort(key=lambda x: -x["score"])
    return out[:3]


def should_alert(btc: dict, prev: dict) -> tuple[bool, str]:
    regime = btc.get("regime")
    if regime != "SIDEWAYS":
        return False, f"regime={regime} — wait for SIDEWAYS"
    if active_grid_count() > 0:
        return False, f"{active_grid_count()} active grid(s) already running"
    prev_reg = prev.get("last_regime")
    if prev_reg and prev_reg != "SIDEWAYS":
        return True, f"REGIME_CHANGE {prev_reg} → SIDEWAYS"
    last_alert = prev.get("last_alert_ts")
    if not last_alert:
        return True, "first SIDEWAYS alert"
    try:
        last_dt = datetime.fromisoformat(last_alert.replace("Z", "+00:00"))
        age_d = (datetime.now(timezone.utc) - last_dt).total_seconds() / 86400
        if age_d >= REMINDER_DAYS:
            return True, f"SIDEWAYS reminder ({age_d:.0f}d since last alert)"
    except Exception:
        return True, "stale state"
    return False, "SIDEWAYS ongoing, reminder not due"


def build_message(btc: dict, candidates: list[dict], reason: str) -> str:
    lines = [
        "📐 *Grid Deploy Suggestion*",
        "",
        f"BTC regime: *SIDEWAYS* ({btc.get('btc_change_pct', 0):+.1f}% / 7d)",
        f"Trigger: {reason}",
        "",
        "Grid bots work best in ranging markets. Suggested setup (manual on Binance):",
        f"Budget: ~${SUGGESTED_BUDGET_USD} from Reserve (split across 1-2 bots)",
        "",
    ]
    if not candidates:
        lines.append("_No suitable candidates right now (range too tight/wide)._")
        lines.append("Re-check when alts show 8-25% 14d range.")
    else:
        for i, c in enumerate(candidates, 1):
            coin = c["symbol"].replace("USDT", "")
            lines.append(
                f"*{i}. {coin}* — ${c['price']:.4g} (14d range {c['range_pct_14d']}%)"
            )
            lines.append(
                f"   Grid: ${c['lower']} - ${c['upper']} | {c['grids']} grids"
            )
            lines.append(
                f"   Stop: ${c['stop_lower']} / ${c['stop_upper']}"
            )
            lines.append(
                f"   Price at {c['pos_in_range_pct']:.0f}% of 14d range"
            )
            lines.append("")
    lines.extend([
        "⚠️ Suggestion only — no auto-deploy.",
        "After setup: `systemctl --user enable grid-monitor.timer`",
        "Rule: do NOT deploy if BTC returns to DOWNTREND.",
    ])
    return "\n".join(lines)


def main():
    load_env()
    btc = get_btc_regime()
    prev = {}
    if STATE_FILE.exists():
        try:
            prev = json.loads(STATE_FILE.read_text())
        except Exception:
            prev = {}

    regime = btc.get("regime", "UNKNOWN")
    print(f"[advisor] BTC 7d: {btc.get('btc_change_pct', 0):+.2f}% → {regime}")
    print(f"[advisor] Active grids: {active_grid_count()}")

    alert, reason = should_alert(btc, prev)
    sent = False
    if alert:
        candidates = rank_candidates()
        msg = build_message(btc, candidates, reason)
        sent = send_telegram(msg)
        print(f"[advisor] ALERT sent={sent} — {reason}")
        if candidates:
            for c in candidates:
                print(f"  {c['symbol']}: ${c['lower']}-${c['upper']} score={c['score']:.0f}")
    else:
        print(f"[advisor] No alert: {reason}")

    state = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "btc_regime": btc,
        "last_regime": regime,
        "active_grids": active_grid_count(),
        "last_alert_ts": datetime.now(timezone.utc).isoformat() if sent else prev.get("last_alert_ts"),
        "last_alert_reason": reason if sent else prev.get("last_alert_reason"),
    }
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
