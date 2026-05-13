#!/usr/bin/env python3
"""Binance API Health Monitor.

Runs every 5 minutes via systemd timer.

Logic:
  1. Try a cheap authenticated Binance call (futures_account_balance).
  2. On success → if state was FAILING, send recovery alert + clear state.
  3. On auth/IP failure → fetch current public IP, send Telegram alert with
     instructions, persist state. Re-alert every cooldown_hours while still
     failing, OR immediately if IP changed since last alert.

State file: data/binance_api_health.json
Alert via TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID(_FINANCE).
"""

from __future__ import annotations
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "data" / "binance_api_health.json"
ENV_FILE = ROOT / ".env"

# Re-alert cadence while still failing (avoid Telegram spam)
COOLDOWN_HOURS = 1.0

# Auth/IP-related Binance error codes that require manual intervention
AUTH_ERROR_CODES = {-2015, -2014, -1022, -2008, -1021}

# Number of consecutive transient failures (network/DNS/timeout) before alerting.
# Single DNS hiccup → no alert; only persistent connectivity loss alerts.
TRANSIENT_FAIL_THRESHOLD = 3

# In-process retry on transient errors (DNS / connection reset / timeout)
TRANSIENT_RETRY_COUNT = 2
TRANSIENT_RETRY_DELAY_S = 5.0


def load_env():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now_dt() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def send_telegram(message: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID_FINANCE")
    if not (token and chat):
        print("[WARN] Telegram credentials missing — skipping alert")
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": "true",
        }).encode()
        urllib.request.urlopen(url, data=data, timeout=10).read()
        return True
    except Exception as e:
        print(f"[WARN] Telegram send failed: {e}")
        return False


def get_public_ip() -> str:
    """Best-effort public IPv4 lookup (multiple providers)."""
    providers = [
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "https://icanhazip.com",
    ]
    for url in providers:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                ip = r.read().decode().strip()
                if ip and len(ip) <= 45:
                    return ip
        except Exception:
            continue
    return "unknown"


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"status": "OK", "fail_since": None, "current_ip": None,
                "last_alert_ts": None, "alert_count": 0, "last_error": None}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"status": "OK", "fail_since": None, "current_ip": None,
                "last_alert_ts": None, "alert_count": 0, "last_error": None}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


TRANSIENT_ERROR_FRAGMENTS = (
    "name resolution",
    "nameresolutionerror",
    "temporary failure in name resolution",
    "max retries exceeded",
    "connection reset",
    "connection refused",
    "connection aborted",
    "read timed out",
    "timed out",
    "connectionerror",
    "remotedisconnected",
)


def is_transient_error(msg: str) -> bool:
    """True if the error looks like a flaky network / DNS issue (not an auth problem)."""
    if not msg:
        return False
    low = msg.lower()
    return any(frag in low for frag in TRANSIENT_ERROR_FRAGMENTS)


def _check_binance_once() -> tuple[bool, str | None, int | None]:
    """Single attempt — returns (ok, error_message, error_code)."""
    api_key = os.environ.get("BINANCE_API_KEY")
    api_secret = os.environ.get("BINANCE_API_SECRET")
    if not (api_key and api_secret):
        return False, "BINANCE_API_KEY / BINANCE_API_SECRET not set in env", None
    try:
        from binance.client import Client
        from binance.exceptions import BinanceAPIException
    except Exception as e:
        return False, f"binance SDK import failed: {e}", None
    try:
        c = Client(api_key, api_secret)
        c.futures_account_balance(recvWindow=5000)
        return True, None, None
    except BinanceAPIException as e:
        return False, f"{e}", getattr(e, "code", None)
    except Exception as e:
        return False, f"network/unknown: {e}", None


def check_binance() -> tuple[bool, str | None, int | None]:
    """Returns (ok, error_message, error_code).

    On transient errors (DNS / connection reset / timeout), retries up to
    TRANSIENT_RETRY_COUNT times with TRANSIENT_RETRY_DELAY_S between attempts.
    Auth errors (e.g. -2015 IP whitelist) fail fast on first attempt.
    """
    last_err: str | None = None
    last_code: int | None = None
    attempts = 1 + TRANSIENT_RETRY_COUNT
    for i in range(attempts):
        ok, err, code = _check_binance_once()
        if ok:
            return True, None, None
        last_err, last_code = err, code
        if not is_transient_error(err or ""):
            return False, err, code
        if i < attempts - 1:
            print(f"[transient] attempt {i+1}/{attempts} failed ({err[:80]}...), retrying in {TRANSIENT_RETRY_DELAY_S}s")
            time.sleep(TRANSIENT_RETRY_DELAY_S)
    return False, last_err, last_code


def main():
    load_env()
    state = load_state()
    ok, err, code = check_binance()

    if ok:
        if state.get("status") == "FAILING":
            ip = get_public_ip()
            send_telegram(
                "✅ *Binance API recovered*\n"
                f"Current IP: `{ip}`\n"
                f"Was failing since: {state.get('fail_since')}\n"
                f"Total alerts sent: {state.get('alert_count', 0)}"
            )
            print(f"[recovered] state: FAILING → OK (ip={ip})")
        else:
            print("[ok] Binance API healthy")
        save_state({
            "status": "OK", "fail_since": None, "current_ip": None,
            "last_alert_ts": None, "alert_count": 0, "last_error": None,
            "last_check_ts": now_iso(),
        })
        return 0

    # FAILING path — distinguish auth errors (alert immediately) from transient
    # network/DNS errors (require N consecutive failures before alerting).
    is_auth_err = code in AUTH_ERROR_CODES
    is_transient = is_transient_error(err or "") and not is_auth_err
    current_ip = get_public_ip()
    prev_status = state.get("status", "OK")
    prev_alert_ip = state.get("current_ip")
    last_alert = parse_iso(state.get("last_alert_ts") or "")
    transient_streak = state.get("transient_streak", 0)

    should_alert = False
    reason = ""
    if is_transient:
        transient_streak += 1
        if transient_streak < TRANSIENT_FAIL_THRESHOLD:
            print(f"[transient] streak {transient_streak}/{TRANSIENT_FAIL_THRESHOLD} — suppressing alert ({err[:80]})")
            # save streak but don't alert / don't flip status to FAILING
            save_state({
                **state,
                "transient_streak": transient_streak,
                "last_check_ts": now_iso(),
                "last_error": err,
                "last_error_code": code,
            })
            return 0
        # threshold breached → upgrade to a real alert
        reason = f"transient threshold breached ({transient_streak} consecutive)"
        should_alert = True
    elif prev_status != "FAILING":
        should_alert = True
        reason = "first failure"
    elif current_ip != prev_alert_ip and current_ip != "unknown":
        should_alert = True
        reason = "IP changed"
    elif last_alert is None:
        should_alert = True
        reason = "no prior alert ts"
    else:
        elapsed = (now_dt() - last_alert).total_seconds() / 3600
        if elapsed >= COOLDOWN_HOURS:
            should_alert = True
            reason = f"cooldown elapsed ({elapsed:.1f}h)"

    if should_alert:
        if is_auth_err:
            msg = (
                "🚨 *Binance API blocked*\n"
                f"Error code: `{code}`\n"
                f"*Current public IP:* `{current_ip}`\n\n"
                "*Action required:*\n"
                "1. Open https://www.binance.com/en/my/settings/api-management\n"
                "2. Edit your API key → IP access restriction\n"
                f"3. Add/replace whitelist IP with: `{current_ip}`\n"
                "4. Save (changes apply within ~1 min)\n\n"
                f"Trade executor & position manager are halted until fixed.\n"
                f"Detail: `{err[:200]}`"
            )
        elif is_transient:
            msg = (
                "🌐 *Binance API: persistent network issue*\n"
                f"{transient_streak} consecutive transient failures (DNS/timeout/conn-reset).\n"
                f"Public IP: `{current_ip}`\n"
                f"Likely ISP DNS / connectivity issue, not a Binance/auth problem.\n"
                f"Detail: `{err[:250]}`"
            )
        else:
            msg = (
                "⚠️ *Binance API check failed*\n"
                f"Error code: `{code}`\n"
                f"Public IP: `{current_ip}`\n"
                f"Detail: `{err[:300]}`"
            )
        sent = send_telegram(msg)
        alert_count = state.get("alert_count", 0) + (1 if sent else 0)
        last_alert_ts = now_iso() if sent else state.get("last_alert_ts")
        print(f"[alert] sent={sent} reason={reason} ip={current_ip} code={code}")
    else:
        alert_count = state.get("alert_count", 0)
        last_alert_ts = state.get("last_alert_ts")
        print(f"[failing] suppressed (cooldown). ip={current_ip} code={code}")

    save_state({
        "status": "FAILING",
        "fail_since": state.get("fail_since") or now_iso(),
        "current_ip": current_ip,
        "last_alert_ts": last_alert_ts,
        "alert_count": alert_count,
        "last_error": err,
        "last_error_code": code,
        "last_check_ts": now_iso(),
        "transient_streak": transient_streak if is_transient else 0,
    })
    return 1


if __name__ == "__main__":
    sys.exit(main())
