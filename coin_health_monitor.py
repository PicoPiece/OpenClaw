#!/usr/bin/env python3
"""Coin Health Monitor — Shield 1.

Mỗi coin trong allowlist được track rolling 14d performance. Nếu coin có
≥ 3 closed trades trong 14d AND tổng R ≤ -3.0R → suspend coin đó tạm thời.
Coin sẽ resume khi có 5 trade thắng trong 7d kế tiếp HOẶC sau 7d kể từ
suspend (auto-recovery).

Source of truth: decisions.db (trades table)
Output: data/coin_suspensions.json (read by binance_price_alert.py)

Run via systemd timer mỗi 30 phút.
"""

from __future__ import annotations
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "decisions.db"
SUSPENSIONS_FILE = ROOT / "data" / "coin_suspensions.json"
ENV_FILE = ROOT / ".env"

ROLLING_WINDOW_HOURS = 14 * 24
SUSPEND_R_THRESHOLD = -3.0
SUSPEND_MIN_TRADES = 3
RESUME_WINS_REQUIRED = 5
AUTO_RECOVERY_DAYS = 7


def load_env():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def send_telegram(message: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID_FINANCE")
    if not (token and chat):
        return
    try:
        import urllib.parse, urllib.request
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat, "text": message,
                                        "parse_mode": "Markdown"}).encode()
        urllib.request.urlopen(url, data=data, timeout=10).read()
    except Exception as e:
        print(f"[WARN] Telegram send failed: {e}")


def fetch_recent_per_coin() -> dict:
    """Return {coin: {n, wins, losses, total_r, total_pnl, recent_trades}}."""
    if not DB_PATH.exists():
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=ROLLING_WINDOW_HOURS)).isoformat()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute("""SELECT coin, result, pnl_usd, r_multiple, opened_at, closed_at
                          FROM trades
                          WHERE closed_at IS NOT NULL
                            AND closed_at >= ?
                            AND (notes IS NULL OR notes NOT LIKE '%phantom_closed_via_sibling%')
                       """, (cutoff,)).fetchall()
    con.close()
    by_coin = {}
    for r in rows:
        coin = (r["coin"] or "").lower()
        if not coin: continue
        d = by_coin.setdefault(coin, {"n": 0, "wins": 0, "losses": 0,
                                       "total_r": 0.0, "total_pnl": 0.0, "trades": []})
        pnl = float(r["pnl_usd"] or 0)
        rm = float(r["r_multiple"] or 0)
        d["n"] += 1
        if pnl > 0: d["wins"] += 1
        elif pnl < 0: d["losses"] += 1
        d["total_pnl"] += pnl
        d["total_r"] += rm
        d["trades"].append({"closed_at": r["closed_at"], "pnl": pnl, "r": rm})
    for d in by_coin.values():
        d["trades"].sort(key=lambda t: t["closed_at"])
    return by_coin


def evaluate(perf: dict, current: dict) -> dict:
    """Decide suspension state per coin. current = previous suspensions JSON."""
    out_suspensions = {}
    now_iso = datetime.now(timezone.utc).isoformat()

    for coin, d in perf.items():
        prev = current.get(coin)
        if prev:
            since = datetime.fromisoformat(prev["suspended_at"])
            wins_since = sum(1 for t in d["trades"]
                             if t["pnl"] > 0
                             and datetime.fromisoformat(t["closed_at"].replace("Z","+00:00")) >= since)
            age_days = (datetime.now(timezone.utc) - since).total_seconds() / 86400
            if wins_since >= RESUME_WINS_REQUIRED:
                out_suspensions.pop(coin, None)
                continue
            if age_days >= AUTO_RECOVERY_DAYS:
                out_suspensions.pop(coin, None)
                continue
            out_suspensions[coin] = prev
            continue

        if d["n"] >= SUSPEND_MIN_TRADES and d["total_r"] <= SUSPEND_R_THRESHOLD:
            out_suspensions[coin] = {
                "suspended_at": now_iso,
                "reason": f"rolling 14d: N={d['n']} W={d['wins']} L={d['losses']} R={d['total_r']:+.2f} pnl=${d['total_pnl']:+.2f}",
                "wins": d["wins"], "losses": d["losses"],
                "total_r": round(d["total_r"], 2),
                "total_pnl": round(d["total_pnl"], 2),
            }

    for coin, prev in current.items():
        if coin in out_suspensions or coin in perf:
            continue
        since = datetime.fromisoformat(prev["suspended_at"])
        age_days = (datetime.now(timezone.utc) - since).total_seconds() / 86400
        if age_days < AUTO_RECOVERY_DAYS:
            out_suspensions[coin] = prev
    return out_suspensions


def main():
    load_env()
    perf = fetch_recent_per_coin()
    current = {}
    if SUSPENSIONS_FILE.exists():
        try:
            current = json.loads(SUSPENSIONS_FILE.read_text()).get("suspensions", {})
        except Exception:
            current = {}

    new_suspensions = evaluate(perf, current)

    added = [c for c in new_suspensions if c not in current]
    resumed = [c for c in current if c not in new_suspensions]

    SUSPENSIONS_FILE.write_text(json.dumps({
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "suspensions": new_suspensions,
        "rolling_window_hours": ROLLING_WINDOW_HOURS,
        "thresholds": {"min_trades": SUSPEND_MIN_TRADES, "r_threshold": SUSPEND_R_THRESHOLD,
                        "resume_wins": RESUME_WINS_REQUIRED, "auto_recovery_days": AUTO_RECOVERY_DAYS},
    }, indent=2))

    print(f"[health] Per-coin 14d performance:")
    for coin, d in sorted(perf.items(), key=lambda x: x[1]["total_r"]):
        flag = " ⛔ SUSPENDED" if coin in new_suspensions else ""
        print(f"  {coin:6} N={d['n']:>2} W={d['wins']} L={d['losses']} R={d['total_r']:+5.2f} pnl=${d['total_pnl']:+.2f}{flag}")

    if added:
        msg = "🛡️ *Shield 1 — COIN SUSPENDED*\n"
        for c in added:
            msg += f"\n• `{c.upper()}`: {new_suspensions[c]['reason']}"
        send_telegram(msg)
        print(f"\nNewly suspended: {added}")
    if resumed:
        msg = "✅ *Shield 1 — COIN RESUMED*\n" + "\n".join(f"• `{c.upper()}`" for c in resumed)
        send_telegram(msg)
        print(f"Resumed: {resumed}")
    if not added and not resumed and not new_suspensions:
        print("\nAll coins healthy. No changes.")


if __name__ == "__main__":
    main()
