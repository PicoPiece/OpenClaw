#!/usr/bin/env python3
"""
DeepSeek Cost Tracker — checks balance via DeepSeek API and sends
a spending report + warnings to Telegram.

Usage:
    python3 deepseek_cost_tracker.py              # one-shot report
    python3 deepseek_cost_tracker.py --daemon     # run every POLL_INTERVAL_MIN

Environment (reads from .env in same directory):
    DEEPSEEK_API_KEY       required
    TELEGRAM_BOT_TOKEN     required
    TELEGRAM_CHAT_ID       required  (your numeric user id)
    DEEPSEEK_INITIAL_BAL   optional  (starting balance, default 2.00)
    WARN_THRESHOLDS        optional  (comma-sep USD thresholds, default 1.50,1.00,0.50,0.20)
    POLL_INTERVAL_MIN      optional  (minutes between checks in daemon mode, default 60)
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
STATE_FILE = SCRIPT_DIR / "data" / "deepseek_cost_state.json"
ENV_FILE = SCRIPT_DIR / ".env"


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


def get_balance(api_key: str) -> dict:
    req = urllib.request.Request(
        "https://api.deepseek.com/user/balance",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


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


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def run_once():
    load_dotenv()

    api_key = cfg("DEEPSEEK_API_KEY")
    tg_token = cfg("TELEGRAM_BOT_TOKEN")
    tg_chat = cfg("TELEGRAM_CHAT_ID")
    thresholds = [float(x) for x in cfg("WARN_THRESHOLDS", "1.50,1.00,0.50,0.20").split(",")]
    daily_limit = float(cfg("DEEPSEEK_DAILY_LIMIT", "2.00"))

    if not api_key:
        sys.exit("DEEPSEEK_API_KEY not set")
    if not tg_token or not tg_chat:
        sys.exit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")

    data = get_balance(api_key)
    usd_info = next(
        (b for b in data.get("balance_infos", []) if b["currency"] == "USD"),
        None,
    )
    if not usd_info:
        sys.exit("No USD balance info returned")

    current_bal = float(usd_info["total_balance"])
    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%d %H:%M UTC")
    today_str = now.strftime("%Y-%m-%d")

    state = load_state()
    last_bal = state.get("last_balance")
    cumulative_spent = state.get("cumulative_spent", 0.0)
    total_topup = state.get("total_topup", 0.0)
    warned = set(state.get("warned_thresholds", []))

    daily_date = state.get("daily_date", today_str)
    daily_spent = state.get("daily_spent", 0.0)
    daily_warned = set(state.get("daily_warned", []))

    if daily_date != today_str:
        daily_spent = 0.0
        daily_warned = set()
        daily_date = today_str

    if last_bal is None:
        session_delta = 0.0
    else:
        balance_diff = last_bal - current_bal
        if balance_diff >= 0:
            session_delta = balance_diff
        else:
            topup_amount = current_bal - last_bal
            total_topup += topup_amount
            session_delta = 0.0

    cumulative_spent += session_delta
    daily_spent += session_delta

    lines = [
        f"*DeepSeek Cost Report* — {now_str}",
        "",
        f"Balance: *${current_bal:.2f}*",
        f"Today spent: *${daily_spent:.4f}* / ${daily_limit:.2f} limit",
        f"Total spent: *${cumulative_spent:.2f}*",
        f"Since last check: ${session_delta:.4f}",
    ]

    if total_topup > 0:
        lines.append(f"Total topped up: ${total_topup:.2f}")

    if not data["is_available"]:
        lines.append("")
        lines.append("*BALANCE DEPLETED — API calls will fail!*")

    daily_thresholds = [0.50, 1.00, 1.50, daily_limit]
    daily_alerts = []
    for dt in daily_thresholds:
        dt_key = f"daily_{dt:.2f}"
        if daily_spent >= dt and dt_key not in daily_warned:
            daily_alerts.append(dt)
            daily_warned.add(dt_key)

    if daily_alerts:
        lines.append("")
        for dt in daily_alerts:
            if dt >= daily_limit:
                lines.append(f"*DAILY LIMIT HIT* — spent ${daily_spent:.2f} today (limit: ${daily_limit:.2f})")
            else:
                lines.append(f"Daily warning: spent ${daily_spent:.4f} today (threshold: ${dt:.2f})")

    new_warnings = []
    for t in sorted(thresholds, reverse=True):
        t_key = f"{t:.2f}"
        if current_bal <= t and t_key not in warned:
            new_warnings.append(t)
            warned.add(t_key)

    if new_warnings:
        lines.append("")
        for t in new_warnings:
            lines.append(f"Warning: balance dropped below *${t:.2f}*")

    msg = "\n".join(lines)
    print(msg.replace("*", ""))

    send_telegram(tg_token, tg_chat, msg)

    state.update({
        "last_balance": current_bal,
        "last_check": now_str,
        "cumulative_spent": round(cumulative_spent, 4),
        "total_topup": round(total_topup, 2),
        "total_spent": round(cumulative_spent, 4),
        "warned_thresholds": sorted(warned),
        "daily_date": daily_date,
        "daily_spent": round(daily_spent, 4),
        "daily_warned": sorted(daily_warned),
    })
    save_state(state)

    return current_bal


def daemon_loop():
    interval = int(cfg("POLL_INTERVAL_MIN", "60")) * 60
    print(f"[daemon] polling every {interval // 60} min")
    while True:
        try:
            run_once()
        except Exception as exc:
            print(f"[ERROR] {exc}")
        time.sleep(interval)


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        daemon_loop()
    else:
        run_once()
