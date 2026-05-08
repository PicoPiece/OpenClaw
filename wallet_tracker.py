#!/usr/bin/env python3
"""Wallet Tracker — poll Binance wallet balance breakdown over time.

Stores `data/wallet_balance_history.json` with snapshots:
  {
    "snapshots": [
      {"ts": "2026-05-07T14:00:00Z",
       "wallets": {"Spot": 33.75, "USDⓈ-M Futures": 93.62, "Earn": 1564.47,
                    "Trading Bots": 2172.33, ...},
       "total": 3864.28,
       "btc_price": 80475.0}
    ]
  }

Used by dashboard `/api/wallet` endpoint to show wallet overview + daily delta.

Run via systemd timer every 30 min. No alerts (aggregate-only monitoring per user choice).
"""
from __future__ import annotations
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
HISTORY_FILE = ROOT / "data" / "wallet_balance_history.json"
MAX_SNAPSHOTS = 500


def load_env():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def signed_get(path: str, params: dict | None = None):
    key = os.environ["BINANCE_API_KEY"]
    secret = os.environ["BINANCE_API_SECRET"]
    p = dict(params or {})
    p["timestamp"] = int(time.time() * 1000)
    p["recvWindow"] = 5000
    qs = urllib.parse.urlencode(p)
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f"https://api.binance.com{path}?{qs}&signature={sig}"
    req = urllib.request.Request(url, headers={"X-MBX-APIKEY": key})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def get_btc_price() -> float:
    url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
    with urllib.request.urlopen(url, timeout=10) as r:
        return float(json.loads(r.read())["price"])


def take_snapshot() -> dict:
    btc_price = get_btc_price()
    rows = signed_get("/sapi/v1/asset/wallet/balance", {"quoteAsset": "USDT"})
    wallets = {w["walletName"]: round(float(w["balance"]), 2) for w in rows}
    total = round(sum(wallets.values()), 2)
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "wallets": wallets,
        "total": total,
        "btc_price": btc_price,
    }


def append_snapshot(snap: dict):
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    history = {"snapshots": []}
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text())
        except Exception:
            pass
    history["snapshots"].append(snap)
    if len(history["snapshots"]) > MAX_SNAPSHOTS:
        history["snapshots"] = history["snapshots"][-MAX_SNAPSHOTS:]
    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False))


def main():
    load_env()
    try:
        snap = take_snapshot()
    except Exception as e:
        print(f"[ERR] snapshot failed: {e}")
        return 1
    append_snapshot(snap)
    print(f"[ok] {snap['ts']}  total=${snap['total']}  bots=${snap['wallets'].get('Trading Bots', 0)}")
    for k, v in snap["wallets"].items():
        if v > 0:
            print(f"     {k:25} ${v:>10.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
