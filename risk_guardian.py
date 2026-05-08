#!/usr/bin/env python3
"""
Risk Guardian — Python replacement for the LLM cron.

Runs every 30 minutes via systemd timer. Reads executor_state.json and
trading_state.json, applies hard rules, and:
  - WARN (Telegram alert) on soft thresholds
  - ACTION (write trading_control.json + Telegram) on hard thresholds

Hard ACTION thresholds (any one triggers auto-pause):
  - daily_pnl <= -$8 (80% of $10 daily loss limit)
  - consecutive_losses >= 3
  - drawdown > 15% of starting_balance

Soft WARN thresholds (Telegram only, no pause):
  - daily_pnl <= -$5 (50% limit)
  - consecutive_losses >= 2
  - active_positions > 4
  - drawdown > 10%

Idempotent: silent (NO_REPLY) when everything OK.
Sends ONE alert per state-change cycle (uses small marker file).

Usage:
    python3 risk_guardian.py         # check + act + telegram if needed
    python3 risk_guardian.py --dry   # report only, no writes
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
EXECUTOR_STATE = SCRIPT_DIR / "data" / "executor_state.json"
TRADING_STATE = SCRIPT_DIR / "data" / "workspace-finance" / "trading_state.json"
TRADING_CONTROL = SCRIPT_DIR / "data" / "workspace-finance" / "trading_control.json"
GUARDIAN_STATE = SCRIPT_DIR / "data" / "risk_guardian_state.json"
ENV_FILE = SCRIPT_DIR / ".env"

DAILY_LOSS_LIMIT = 10.0
ACTION_DAILY_LOSS = 8.0
ACTION_CONSEC_LOSSES = 3
ACTION_DD_PCT = 15.0
WARN_DAILY_LOSS = 5.0
WARN_CONSEC_LOSSES = 2
WARN_ACTIVE_POSITIONS = 4
WARN_DD_PCT = 10.0


def load_env():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_ALERT_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[WARN] Telegram credentials missing")
        return False
    payload = json.dumps({"chat_id": chat, "text": text, "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True
    except urllib.error.HTTPError as exc:
        print(f"[WARN] Telegram failed: {exc.code} {exc.read().decode()[:200]}")
    except Exception as exc:
        print(f"[WARN] Telegram error: {exc}")
    return False


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def get_binance_wallet() -> float | None:
    """Live USDT futures wallet balance — source of truth for DD."""
    try:
        from binance.client import Client
        c = Client(os.environ.get("BINANCE_API_KEY"), os.environ.get("BINANCE_API_SECRET"))
        return float(c.futures_account()["totalWalletBalance"])
    except Exception as e:
        print(f"[WARN] Binance balance fetch failed: {e}")
        return None


def evaluate(es: dict, ts: dict) -> dict:
    daily_pnl = float(es.get("daily_pnl") or 0)
    consec = int(es.get("consecutive_losses") or 0)
    starting = float(es.get("starting_balance") or 0) or 1.0
    live_balance = get_binance_wallet()
    if live_balance is not None:
        current_balance = live_balance
    else:
        total_pnl = float(es.get("total_pnl") or 0)
        current_balance = starting + total_pnl
    dd_pct = max(0.0, (starting - current_balance) / starting * 100)
    active = sum(1 for s in (ts.get("states") or {}).values() if s.get("state") == "ACTIVE")

    actions, warnings = [], []
    if daily_pnl <= -ACTION_DAILY_LOSS:
        actions.append(f"Daily loss ${daily_pnl:.2f} >= ${ACTION_DAILY_LOSS} (80% limit)")
    if consec >= ACTION_CONSEC_LOSSES:
        actions.append(f"Consecutive losses {consec} >= {ACTION_CONSEC_LOSSES}")
    if dd_pct > ACTION_DD_PCT:
        actions.append(f"Drawdown {dd_pct:.2f}% > {ACTION_DD_PCT}% (catastrophic)")

    if not actions:
        if daily_pnl <= -WARN_DAILY_LOSS:
            warnings.append(f"Daily loss ${daily_pnl:.2f} >= ${WARN_DAILY_LOSS} (50% limit)")
        if consec >= WARN_CONSEC_LOSSES:
            warnings.append(f"Consecutive losses {consec} >= {WARN_CONSEC_LOSSES}")
        if active > WARN_ACTIVE_POSITIONS:
            warnings.append(f"Active positions {active} > {WARN_ACTIVE_POSITIONS}")
        if dd_pct > WARN_DD_PCT:
            warnings.append(f"Drawdown {dd_pct:.2f}% > {WARN_DD_PCT}%")

    return {
        "daily_pnl": daily_pnl,
        "consecutive_losses": consec,
        "starting_balance": starting,
        "current_balance": current_balance,
        "drawdown_pct": dd_pct,
        "active_positions": active,
        "actions": actions,
        "warnings": warnings,
        "level": "ACTION" if actions else ("WARN" if warnings else "OK"),
    }


def maybe_pause(reasons: list[str]) -> bool:
    """Write trading_control.json to disable auto-trade. Idempotent."""
    current = load_json(TRADING_CONTROL, {})
    if current.get("auto_trade_enabled") is False:
        return False
    new_state = {
        "auto_trade_enabled": False,
        "reason": "Risk Guardian (auto): " + "; ".join(reasons),
        "updated_by": "risk_guardian.py",
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "max_daily_loss": int(current.get("max_daily_loss", DAILY_LOSS_LIMIT)),
        "emergency_close_all": bool(current.get("emergency_close_all", False)),
    }
    save_json(TRADING_CONTROL, new_state)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    load_env()
    es = load_json(EXECUTOR_STATE, {})
    ts = load_json(TRADING_STATE, {})
    rep = evaluate(es, ts)

    print(json.dumps({k: v for k, v in rep.items() if k not in ("trade_history",)}, indent=2))

    last = load_json(GUARDIAN_STATE, {})
    last_level = last.get("last_level")
    last_signature = last.get("last_signature")
    signature = f"{rep['level']}|{','.join(rep['actions'] or rep['warnings'])}"

    if args.dry:
        return

    if rep["level"] == "ACTION":
        paused = maybe_pause(rep["actions"])
        if paused or signature != last_signature:
            msg = (
                "🚨 *RISK GUARDIAN — AUTO PAUSE*\n"
                f"Balance: ${rep['current_balance']:.2f} (DD {rep['drawdown_pct']:.2f}%)\n"
                f"Daily PnL: ${rep['daily_pnl']:+.2f} | Consec losses: {rep['consecutive_losses']}\n"
                f"Active positions: {rep['active_positions']}\n\n"
                "*Triggered:*\n- " + "\n- ".join(rep["actions"]) +
                "\n\nAuto-trade DISABLED. Review then resume manually:\n"
                '`echo \'{"auto_trade_enabled": true, ...}\' > data/workspace-finance/trading_control.json`'
            )
            send_telegram(msg)
    elif rep["level"] == "WARN":
        if signature != last_signature:
            msg = (
                "⚠️ *RISK GUARDIAN — Warning*\n"
                f"Balance: ${rep['current_balance']:.2f} (DD {rep['drawdown_pct']:.2f}%)\n"
                f"Daily PnL: ${rep['daily_pnl']:+.2f} | Consec: {rep['consecutive_losses']}\n"
                f"Active: {rep['active_positions']}\n\n"
                "*Watching:*\n- " + "\n- ".join(rep["warnings"]) +
                "\n\nNo action taken — monitoring."
            )
            send_telegram(msg)
    else:
        if last_level in ("ACTION", "WARN"):
            send_telegram("✅ *RISK GUARDIAN — Recovered*\nAll metrics back to normal.")

    save_json(GUARDIAN_STATE, {
        "last_level": rep["level"],
        "last_signature": signature,
        "last_check": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })


if __name__ == "__main__":
    main()
