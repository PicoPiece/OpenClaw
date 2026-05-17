#!/usr/bin/env python3
"""
Binance Reconcile — pull the truth from the exchange and patch local state.

Why: trade_executor only logs to trade_history when *it* closes a position.
When Sư đóng tay, hoặc TP/SL trên exchange tự fill mà executor không kịp
detect, hoặc executor restart làm mất tracking — kết quả là trade_history bị
thiếu lệnh, trading_state vẫn ACTIVE, dashboard sai số.

Source of truth = Binance Futures `income` endpoint (REALIZED_PNL,
COMMISSION, FUNDING_FEE, INSURANCE_CLEAR) + `positionRisk` (open positions).

Steps:
  1. Pull realized PnL events from Binance for the lookback window.
  2. Group consecutive REALIZED_PNL events on the same symbol within 60s into
     one logical trade (entry + exit produce 1 income row each side).
  3. Rebuild executor_state.trade_history merging anything missing.
  4. Sync trading_state.json: any ACTIVE coin without a Binance position →
     mark TP_HIT/SL_HIT/MANUAL based on PnL sign.
  5. Insert missing trades into decisions.db with notes='reconciled' so RAG
     and dashboards pick them up.
  6. Recompute executor totals (total_pnl, total_trades, daily_pnl,
     consecutive_losses).

Usage:
  python3 binance_reconcile.py --dry-run              # preview only
  python3 binance_reconcile.py --apply                # write changes
  python3 binance_reconcile.py --apply --days 30      # wider lookback
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import decision_logger  # noqa: E402

ENV_FILE = SCRIPT_DIR / ".env"
EXEC_STATE = SCRIPT_DIR / "data" / "executor_state.json"
TRADING_STATE = SCRIPT_DIR / "data" / "workspace-finance" / "trading_state.json"

API_BASE = "https://fapi.binance.com"


def load_env():
    if not ENV_FILE.exists():
        raise SystemExit(".env not found")
    out = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def signed_get(path: str, params: dict, env: dict) -> object:
    params = dict(params)
    params["timestamp"] = int(time.time() * 1000)
    qs = urllib.parse.urlencode(params)
    sig = hmac.new(env["BINANCE_API_SECRET"].encode(), qs.encode(),
                    hashlib.sha256).hexdigest()
    url = f"{API_BASE}{path}?{qs}&signature={sig}"
    req = urllib.request.Request(url, headers={"X-MBX-APIKEY": env["BINANCE_API_KEY"]})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------------------
# 1. Fetch raw history
# ---------------------------------------------------------------------------

def fetch_income(env: dict, days: int) -> list[dict]:
    end = int(time.time() * 1000)
    start = end - days * 86400 * 1000
    out: list[dict] = []
    cursor = start
    while True:
        rows = signed_get("/fapi/v1/income",
                           {"startTime": cursor, "endTime": end, "limit": 1000},
                           env)
        if not rows:
            break
        out.extend(rows)
        if len(rows) < 1000:
            break
        cursor = int(rows[-1]["time"]) + 1
    return out


def fetch_positions(env: dict) -> list[dict]:
    rows = signed_get("/fapi/v2/positionRisk", {}, env)
    return [p for p in rows if float(p["positionAmt"]) != 0]


def fetch_balance(env: dict) -> dict:
    a = signed_get("/fapi/v2/account", {}, env)
    return {"total": float(a["totalWalletBalance"]),
            "available": float(a["availableBalance"]),
            "uPnL": float(a["totalUnrealizedProfit"])}


def fetch_user_trades(env: dict, symbol: str, days: int) -> list[dict]:
    """Aggregate filled orders to learn entry side & qty per close."""
    end = int(time.time() * 1000)
    start = end - days * 86400 * 1000
    return signed_get("/fapi/v1/userTrades",
                       {"symbol": symbol, "startTime": start,
                        "endTime": end, "limit": 1000}, env)


# ---------------------------------------------------------------------------
# 2. Group income events into logical trades
# ---------------------------------------------------------------------------

SYMBOL_TO_COIN = {
    "1000PEPEUSDT": "pepe", "1000SHIBUSDT": "shib",
}


def normalise_coin(symbol: str) -> str:
    if symbol in SYMBOL_TO_COIN:
        return SYMBOL_TO_COIN[symbol]
    s = symbol
    for suffix in ("USDT", "BUSD"):
        if s.endswith(suffix):
            s = s[:-len(suffix)]
            break
    if s.startswith("1000"):
        s = s[4:]
    return s.lower()


def build_trade_records(income: list[dict], env: dict, days: int) -> list[dict]:
    """Each REALIZED_PNL row corresponds to one closed lot. Aggregate
    by (symbol, time-bucket) so multi-fill closes show as one trade.
    Pull side/qty/entry from userTrades for accuracy."""
    realized = sorted([r for r in income if r["incomeType"] == "REALIZED_PNL"],
                       key=lambda x: int(x["time"]))
    fees = [r for r in income if r["incomeType"] == "COMMISSION"]
    fee_by_time = {}
    for f in fees:
        key = (f["symbol"], int(f["time"]) // 60000)
        fee_by_time.setdefault(key, 0)
        fee_by_time[key] += float(f["income"])

    grouped: dict[tuple, dict] = {}
    for r in realized:
        bucket = int(r["time"]) // 60000
        key = (r["symbol"], bucket)
        g = grouped.setdefault(key, {
            "symbol": r["symbol"], "coin": normalise_coin(r["symbol"]),
            "time_ms": int(r["time"]), "pnl_usd": 0.0, "fee_usd": 0.0,
        })
        g["pnl_usd"] += float(r["income"])
        g["fee_usd"] += fee_by_time.get(key, 0.0)

    trades_by_sym: dict[str, list[dict]] = {}
    for sym in {g["symbol"] for g in grouped.values()}:
        try:
            trades_by_sym[sym] = fetch_user_trades(env, sym, days)
        except Exception as e:
            print(f"  warn: userTrades({sym}) failed: {e}")
            trades_by_sym[sym] = []

    records = []
    for g in grouped.values():
        sym_trades = trades_by_sym.get(g["symbol"], [])
        close_trade = None
        for ut in sym_trades:
            if abs(int(ut["time"]) - g["time_ms"]) <= 60000 and float(ut.get("realizedPnl", 0)) != 0:
                close_trade = ut
                break
        side = None
        entry_price = None
        qty = None
        close_price = None
        if close_trade:
            close_side = close_trade["side"]
            side = "SHORT" if close_side == "BUY" else "LONG"
            qty = abs(float(close_trade["qty"]))
            close_price = float(close_trade["price"])
            entries = [t for t in sym_trades if t["side"] == ("BUY" if side == "LONG" else "SELL")
                        and int(t["time"]) < g["time_ms"]]
            if entries:
                last_entry = max(entries, key=lambda t: int(t["time"]))
                entry_price = float(last_entry["price"])
        result = "TP_HIT" if g["pnl_usd"] > 0 else "SL_HIT"
        records.append({
            "coin": g["coin"], "symbol": g["symbol"],
            "direction": side or "?",
            "entry": entry_price or 0,
            "close": close_price or 0,
            "qty": qty or 0,
            "pnl": round(g["pnl_usd"] + g["fee_usd"], 4),
            "pnl_gross": round(g["pnl_usd"], 4),
            "fee": round(g["fee_usd"], 4),
            "result": result,
            "time": datetime.fromtimestamp(g["time_ms"] / 1000,
                                            tz=timezone.utc).isoformat(),
            "time_ms": g["time_ms"],
            "source": "binance_reconcile",
        })
    records.sort(key=lambda r: r["time_ms"])
    return records


# ---------------------------------------------------------------------------
# 3. Patch executor_state.json
# ---------------------------------------------------------------------------

def _trade_key(t: dict) -> tuple:
    return (t.get("coin"), t.get("time"), round(t.get("pnl", 0), 2))


def _ts_to_dt(ts_str: str | None):
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None


def _find_fuzzy_duplicate(history: list[dict], record: dict,
                          window_min: int = 6) -> dict | None:
    """Return existing history entry that likely refers to the same close.

    Match criteria: same coin AND time within window_min minutes AND same pnl sign.
    Used to detect when binance_reconcile would create a duplicate of an entry
    already added by trade_executor.check_position_status (or vice versa).
    """
    r_dt = _ts_to_dt(record.get("time"))
    if not r_dt:
        return None
    r_coin = record.get("coin")
    r_sign = 1 if (record.get("pnl") or 0) > 0 else (-1 if (record.get("pnl") or 0) < 0 else 0)
    for h in history:
        if h.get("coin") != r_coin:
            continue
        h_dt = _ts_to_dt(h.get("time"))
        if not h_dt:
            continue
        dt_min = abs((r_dt - h_dt).total_seconds()) / 60.0
        if dt_min > window_min:
            continue
        h_sign = 1 if (h.get("pnl") or 0) > 0 else (-1 if (h.get("pnl") or 0) < 0 else 0)
        if h_sign != 0 and r_sign != 0 and h_sign != r_sign:
            continue
        return h
    return None


def _determine_source(record: dict, trading_state: dict) -> str:
    """Cross-reference trading_state to classify a reconciled trade as auto vs manual.

    Logic:
    - If trading_state.states[coin] has a recently-closed entry (executed_at within
      ±2h of trade time) with state in {TP_HIT, SL_HIT, TIMEOUT_*, EMERGENCY_*}
      AND order_id was set by trade_executor → auto.
    - Else → manual (user opened via Binance UI / mobile, not via signal pipeline).
    """
    coin = record.get("coin")
    r_dt = _ts_to_dt(record.get("time"))
    if not coin or not r_dt:
        return "manual"
    cs = (trading_state.get("states") or {}).get(coin)
    if not cs:
        return "manual"
    # check executed_at / closed_at proximity
    for k in ("executed_at", "closed_at", "fill_time"):
        ts = _ts_to_dt(cs.get(k))
        if ts and abs((r_dt - ts).total_seconds()) <= 2 * 3600:
            order_id = cs.get("order_id") or ""
            if order_id and "manual" not in str(order_id).lower() and not cs.get("is_imported"):
                return "auto"
            break
    return "manual"


CONSEC_LOSS_STALE_HOURS = 72.0


def _count_auto_consec_losses(history: list[dict]) -> int:
    """Count consecutive losing trades from the tail, ONLY among auto trades.

    Manual trades are skipped (neither resets nor increments the streak) so
    that user's discretionary trades don't trip the auto-trade kill switch.

    Returns 0 if the oldest loss in the streak is older than
    CONSEC_LOSS_STALE_HOURS (prevents stuck halt when no new auto trades happen).
    Mirrors logic in risk_guardian._auto_only_consec_losses to stay consistent.
    """
    consec = 0
    oldest_loss_ts = None
    for t in reversed(history):
        src = t.get("source", "auto")
        if src == "manual":
            continue
        pnl = t.get("pnl") or 0
        if pnl < 0:
            consec += 1
            oldest_loss_ts = t.get("time")
        elif pnl > 0:
            break
    if oldest_loss_ts and consec > 0:
        try:
            oldest_dt = datetime.fromisoformat(oldest_loss_ts.replace("Z", "+00:00"))
            age_h = (datetime.now(timezone.utc) - oldest_dt).total_seconds() / 3600
            if age_h > CONSEC_LOSS_STALE_HOURS:
                return 0
        except Exception:
            pass
    return consec


def patch_executor_state(records: list[dict], apply: bool) -> dict:
    es = json.loads(EXEC_STATE.read_text()) if EXEC_STATE.exists() else {}
    history = list(es.get("trade_history", []))

    # Load trading_state for source classification
    ts_data = {}
    if TRADING_STATE.exists():
        try:
            ts_data = json.loads(TRADING_STATE.read_text())
        except Exception:
            ts_data = {}

    existing_keys = {_trade_key(t) for t in history}
    added = []
    skipped_duplicates = 0
    enriched = 0

    for r in records:
        source = _determine_source(r, ts_data)
        local = {"coin": r["coin"], "direction": r["direction"],
                 "entry": r["entry"], "close": r["close"],
                 "pnl": r["pnl"], "result": r["result"],
                 "time": r["time"], "qty": r["qty"], "fee": r["fee"],
                 "source": source}
        # 1. exact key dedup
        if _trade_key(local) in existing_keys:
            skipped_duplicates += 1
            continue
        # 2. fuzzy dedup (same coin within 6 min, same pnl sign)
        fuzzy = _find_fuzzy_duplicate(history, local)
        if fuzzy is not None:
            # Enrich existing entry with reconcile fields but DON'T add duplicate
            for field in ("qty", "fee", "entry", "close"):
                if not fuzzy.get(field) and local.get(field):
                    fuzzy[field] = local[field]
            # Preserve source: prefer existing if it's "auto" (since trade_executor sets that)
            if not fuzzy.get("source"):
                fuzzy["source"] = source
            enriched += 1
            skipped_duplicates += 1
            continue
        added.append(local)
        history.append(local)  # so subsequent fuzzy checks see it
        existing_keys.add(_trade_key(local))

    new_history = sorted(history, key=lambda t: t.get("time", ""))
    tracking_since = (es.get("tracking_since") or "")[:10]
    if tracking_since:
        session_history = [t for t in new_history
                           if (t.get("time") or "")[:10] >= tracking_since]
    else:
        session_history = new_history
    total_pnl = round(sum(t.get("pnl", 0) for t in session_history), 4)
    total_trades = len(session_history)
    today = datetime.now(timezone.utc).date().isoformat()
    daily_pnl = round(sum(t.get("pnl", 0) for t in new_history
                            if (t.get("time") or "").startswith(today)
                            and t.get("source") != "manual"), 4)
    consecutive = _count_auto_consec_losses(new_history)

    summary = {
        "added_count": len(added),
        "skipped_duplicates": skipped_duplicates,
        "enriched_existing": enriched,
        "total_trades_before": len(es.get("trade_history", [])),
        "total_trades_after": total_trades,
        "total_pnl_before": round(es.get("total_pnl", 0), 4),
        "total_pnl_after": total_pnl,
        "daily_pnl_after": daily_pnl,
        "consecutive_losses_after": consecutive,
        "added_sample": added[:5],
    }
    if apply:
        es["trade_history"] = new_history
        es["total_pnl"] = total_pnl
        es["total_trades"] = total_trades
        es["daily_pnl"] = daily_pnl
        es["consecutive_losses"] = consecutive
        es["daily_date"] = today
        EXEC_STATE.write_text(json.dumps(es, indent=2))
    return summary


# ---------------------------------------------------------------------------
# 4. Sync trading_state.json with real Binance positions
# ---------------------------------------------------------------------------

def patch_trading_state(positions: list[dict], records: list[dict],
                         apply: bool) -> dict:
    if not TRADING_STATE.exists():
        return {"skipped": "no trading_state.json"}
    ts = json.loads(TRADING_STATE.read_text())
    states = ts.get("states", {})
    open_coins = {normalise_coin(p["symbol"]): p for p in positions}
    pnl_by_coin: dict[str, list[dict]] = {}
    for r in records:
        pnl_by_coin.setdefault(r["coin"], []).append(r)

    changes = []
    for coin, s in states.items():
        if s.get("state") != "ACTIVE":
            continue
        if coin in open_coins:
            continue
        coin_records = pnl_by_coin.get(coin, [])
        last = coin_records[-1] if coin_records else None
        if last:
            new_state = last["result"]
        else:
            new_state = "MANUAL"
        changes.append({"coin": coin, "from": "ACTIVE", "to": new_state,
                          "last_pnl": last["pnl"] if last else None})
        if apply:
            s["state"] = new_state
            s["closed_at"] = (last["time"] if last else
                                datetime.now(timezone.utc).isoformat())
            s["close_reason"] = "binance_reconcile"
    if apply and changes:
        TRADING_STATE.write_text(json.dumps(ts, indent=2))
    return {"changed_count": len(changes), "changes": changes,
             "open_on_exchange": list(open_coins.keys())}


# ---------------------------------------------------------------------------
# 5. Backfill decisions.db
# ---------------------------------------------------------------------------

def backfill_db(records: list[dict], apply: bool) -> dict:
    decision_logger.init_db()
    inserted = 0
    skipped = 0
    with decision_logger._conn() as c:
        for r in records:
            row = c.execute(
                """SELECT id FROM trades
                    WHERE coin=? AND opened_at IS NOT NULL
                      AND ABS((julianday(closed_at) - julianday(?)) * 86400) < 120""",
                (r["coin"], r["time"]),
            ).fetchone()
            if row:
                skipped += 1
                continue
            if not apply:
                inserted += 1
                continue
            mode_row = c.execute(
                """SELECT mode FROM trades
                    WHERE coin=? AND mode IS NOT NULL
                      AND ABS((julianday(opened_at) - julianday(?)) * 86400) < 21600
                    ORDER BY opened_at DESC LIMIT 1""",
                (r["coin"], r["time"]),
            ).fetchone()
            inferred_mode = mode_row[0] if mode_row else None
            c.execute(
                """INSERT INTO trades (coin, direction, entry_price, sl_price,
                       tp_price, qty, position_usd, opened_at, closed_at,
                       close_price, result, pnl_usd, fee_usd, notes,
                       is_shadow, mode)
                    VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                (r["coin"], r["direction"], r["entry"], r["qty"],
                 (r["entry"] or 0) * (r["qty"] or 0), r["time"], r["time"],
                 r["close"], r["result"], r["pnl"], r.get("fee", 0),
                 "strategy=ema_trend_v1 source=binance_reconcile",
                 inferred_mode),
            )
            inserted += 1
    return {"db_inserted": inserted, "db_skipped_existing": skipped}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually write changes (otherwise dry-run)")
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()

    env = load_env()
    print(f"Lookback: {args.days} days  apply={args.apply}")

    print("[1/5] fetching balance / positions / income from Binance ...")
    balance = fetch_balance(env)
    positions = fetch_positions(env)
    income = fetch_income(env, args.days)
    print(f"  balance: total=${balance['total']:.2f} avail=${balance['available']:.2f}")
    print(f"  open positions on exchange: {len(positions)}")
    print(f"  income events: {len(income)}")

    print("[2/5] grouping into logical trades ...")
    records = build_trade_records(income, env, args.days)
    print(f"  reconstructed {len(records)} closed trades")

    print("[3/5] patch executor_state.json ...")
    es_summary = patch_executor_state(records, apply=args.apply)
    print(json.dumps(es_summary, indent=2, default=str))

    print("[4/5] sync trading_state.json ...")
    ts_summary = patch_trading_state(positions, records, apply=args.apply)
    print(json.dumps(ts_summary, indent=2, default=str))

    print("[5/5] backfill decisions.db trades ...")
    db_summary = backfill_db(records, apply=args.apply)
    print(json.dumps(db_summary, indent=2))

    print()
    print("=== Net summary ===")
    print(f"  Binance wallet:        ${balance['total']:.2f}")
    print(f"  trade_history total:   ${es_summary['total_pnl_after']:+.2f}  "
            f"({es_summary['total_trades_after']} trades)")
    print(f"  Open on exchange:      {len(positions)}")
    if not args.apply:
        print()
        print(">>> DRY-RUN. Re-run with --apply to persist these changes.")


if __name__ == "__main__":
    cli()
