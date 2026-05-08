#!/usr/bin/env python3
"""
Decision Logger — central audit trail for all LLM calls and trades.

Schema:
  llm_decisions: every LLM call with full prompt/response/decision/indicators
  trades:        opened/closed positions linked to the decision that opened them
  slippage_log:  expected vs actual fill prices (Phase 4)
  shadow_trades: paper trades for new strategies pre-deployment (Phase 4)
  ai_budget:     monthly token budget tracking (Phase 5)

Public API (used by other scripts):
  log_decision(...)        -> int (decision_id)
  log_trade_open(...)      -> int (trade_id)
  log_trade_close(...)
  log_slippage(...)
  log_shadow_trade(...)
  query_decisions(...)
  query_trades(...)
  query_similar_trades(...)  # SQL-based in Phase 1, vector in Phase 3

The DB lives at data/decisions.db and is also symlinked into the
finance workspace so OpenClaw chat can read it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / "data" / "decisions.db"

# Pricing used to estimate cost when the API does not return billing.
# Updated from DeepSeek + provider rate cards (USD per 1M tokens).
COST_PER_M_TOKENS = {
    "deepseek-chat": {"in": 0.14, "out": 0.28},
    "deepseek-reasoner": {"in": 0.55, "out": 2.19},
    "claude-opus-4": {"in": 15.0, "out": 75.0},
    "gpt-4o": {"in": 2.5, "out": 10.0},
    "gemini-2.0-flash": {"in": 0.075, "out": 0.30},
    "qwen-max": {"in": 1.6, "out": 6.4},
}

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    source TEXT NOT NULL,
    coin TEXT,
    direction TEXT,
    model TEXT,
    prompt TEXT NOT NULL,
    response TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT,
    confidence INTEGER,
    indicators_json TEXT,
    market_state_json TEXT,
    trade_id INTEGER,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_usd REAL,
    prompt_variant TEXT DEFAULT 'A',
    prompt_version TEXT,
    rag_used INTEGER DEFAULT 0,
    rag_context_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_decisions_ts ON llm_decisions(ts);
CREATE INDEX IF NOT EXISTS idx_decisions_coin ON llm_decisions(coin);
CREATE INDEX IF NOT EXISTS idx_decisions_source ON llm_decisions(source);
CREATE INDEX IF NOT EXISTS idx_decisions_trade ON llm_decisions(trade_id);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_decision_id INTEGER,
    coin TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price REAL NOT NULL,
    sl_price REAL,
    tp_price REAL,
    qty REAL,
    position_usd REAL,
    risk_usd REAL,
    leverage INTEGER,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    close_price REAL,
    result TEXT,
    pnl_usd REAL,
    r_multiple REAL,
    fee_usd REAL,
    notes TEXT,
    is_shadow INTEGER DEFAULT 0,
    indicators_open_json TEXT,
    market_state_open_json TEXT,
    FOREIGN KEY(signal_decision_id) REFERENCES llm_decisions(id)
);

CREATE INDEX IF NOT EXISTS idx_trades_coin ON trades(coin);
CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(closed_at);
CREATE INDEX IF NOT EXISTS idx_trades_result ON trades(result);
CREATE INDEX IF NOT EXISTS idx_trades_shadow ON trades(is_shadow);

CREATE TABLE IF NOT EXISTS slippage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    trade_id INTEGER,
    coin TEXT,
    side TEXT,
    expected_price REAL NOT NULL,
    actual_price REAL NOT NULL,
    slippage_bps REAL,
    qty REAL,
    FOREIGN KEY(trade_id) REFERENCES trades(id)
);

CREATE INDEX IF NOT EXISTS idx_slippage_coin ON slippage_log(coin);

CREATE TABLE IF NOT EXISTS ai_budget (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period TEXT NOT NULL,
    budget_usd REAL NOT NULL,
    spent_usd REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(period)
);

CREATE TABLE IF NOT EXISTS profit_splits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period TEXT NOT NULL,
    gross_pnl_usd REAL NOT NULL,
    reinvest_usd REAL,
    ai_budget_usd REAL,
    withdraw_usd REAL,
    created_at TEXT NOT NULL,
    UNIQUE(period)
);

CREATE TABLE IF NOT EXISTS coin_blacklist (
    coin TEXT PRIMARY KEY,
    reason TEXT,
    added_at TEXT NOT NULL,
    expires_at TEXT
);
"""


@contextmanager
def _conn():
    """Thread-safe connection (sqlite is single-writer per file)."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        c = sqlite3.connect(str(DB_PATH), timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        finally:
            c.close()


def init_db():
    """Create tables if missing. Safe to call repeatedly. Also runs lightweight migrations."""
    with _conn() as c:
        c.executescript(SCHEMA)
        existing = {r[1] for r in c.execute("PRAGMA table_info(trades)").fetchall()}
        for col, ddl in (
            ("mode", "ALTER TABLE trades ADD COLUMN mode TEXT"),
            ("timeout_h", "ALTER TABLE trades ADD COLUMN timeout_h REAL"),
            ("sl_mult", "ALTER TABLE trades ADD COLUMN sl_mult REAL"),
            ("tp_mult", "ALTER TABLE trades ADD COLUMN tp_mult REAL"),
        ):
            if col not in existing:
                try:
                    c.execute(ddl)
                except Exception:
                    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------

def estimate_cost(model: str, tokens_in: int | None, tokens_out: int | None) -> float | None:
    if not model or tokens_in is None or tokens_out is None:
        return None
    rate = COST_PER_M_TOKENS.get(model)
    if not rate:
        return None
    return (tokens_in * rate["in"] + tokens_out * rate["out"]) / 1_000_000


# ---------------------------------------------------------------------------
# Logging API
# ---------------------------------------------------------------------------

def log_decision(
    *,
    source: str,
    coin: str | None,
    prompt: str,
    response: str,
    decision: str,
    reason: str = "",
    confidence: int | None = None,
    direction: str | None = None,
    model: str | None = None,
    indicators: dict | None = None,
    market_state: dict | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    prompt_variant: str = "A",
    prompt_version: str | None = None,
    rag_context: list | None = None,
) -> int:
    """Log one LLM decision. Returns decision_id."""
    init_db()
    cost = estimate_cost(model or "", tokens_in, tokens_out)
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO llm_decisions
               (ts, source, coin, direction, model, prompt, response,
                decision, reason, confidence, indicators_json, market_state_json,
                tokens_in, tokens_out, cost_usd, prompt_variant, prompt_version,
                rag_used, rag_context_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _now_iso(), source, coin, direction, model,
                prompt, response, decision, reason, confidence,
                json.dumps(indicators or {}, default=str),
                json.dumps(market_state or {}, default=str),
                tokens_in, tokens_out, cost,
                prompt_variant, prompt_version,
                1 if rag_context else 0,
                json.dumps(rag_context, default=str) if rag_context else None,
            ),
        )
        return cur.lastrowid


def log_trade_open(
    *,
    coin: str,
    direction: str,
    entry_price: float,
    sl_price: float | None,
    tp_price: float | None,
    qty: float | None,
    position_usd: float | None,
    risk_usd: float | None = None,
    leverage: int | None = None,
    signal_decision_id: int | None = None,
    notes: str = "",
    is_shadow: bool = False,
    indicators: dict | None = None,
    market_state: dict | None = None,
    mode: str | None = None,
    timeout_h: float | None = None,
    sl_mult: float | None = None,
    tp_mult: float | None = None,
) -> int:
    """Log a newly opened (or shadow-opened) trade. Returns trade_id."""
    init_db()
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO trades
               (signal_decision_id, coin, direction, entry_price, sl_price, tp_price,
                qty, position_usd, risk_usd, leverage, opened_at, notes, is_shadow,
                indicators_open_json, market_state_open_json,
                mode, timeout_h, sl_mult, tp_mult)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                signal_decision_id, coin, direction, entry_price, sl_price, tp_price,
                qty, position_usd, risk_usd, leverage, _now_iso(), notes,
                1 if is_shadow else 0,
                json.dumps(indicators or {}, default=str),
                json.dumps(market_state or {}, default=str),
                mode, timeout_h, sl_mult, tp_mult,
            ),
        )
        trade_id = cur.lastrowid
        # Backfill the FK on the decision row if we have one
        if signal_decision_id:
            c.execute(
                "UPDATE llm_decisions SET trade_id=? WHERE id=?",
                (trade_id, signal_decision_id),
            )
        return trade_id


def log_trade_close(
    *,
    trade_id: int,
    close_price: float,
    result: str,
    pnl_usd: float,
    r_multiple: float | None = None,
    fee_usd: float | None = None,
    notes: str = "",
):
    """Mark a trade closed with outcome."""
    init_db()
    with _conn() as c:
        if r_multiple is None:
            row = c.execute(
                "SELECT entry_price, sl_price, direction FROM trades WHERE id=?",
                (trade_id,),
            ).fetchone()
            if row and row["sl_price"] and row["entry_price"]:
                risk_per_unit = abs(row["entry_price"] - row["sl_price"])
                if risk_per_unit > 0:
                    if row["direction"] == "LONG":
                        r_multiple = (close_price - row["entry_price"]) / risk_per_unit
                    else:
                        r_multiple = (row["entry_price"] - close_price) / risk_per_unit
        existing_notes = c.execute("SELECT notes FROM trades WHERE id=?", (trade_id,)).fetchone()
        merged = (existing_notes["notes"] or "") if existing_notes else ""
        if notes:
            merged = (merged + " | " + notes).strip(" |")
        c.execute(
            """UPDATE trades
               SET closed_at=?, close_price=?, result=?, pnl_usd=?,
                   r_multiple=?, fee_usd=?, notes=?
               WHERE id=?""",
            (_now_iso(), close_price, result, pnl_usd,
             r_multiple, fee_usd, merged, trade_id),
        )


def log_slippage(*, trade_id: int | None, coin: str, side: str,
                 expected_price: float, actual_price: float, qty: float):
    init_db()
    if expected_price <= 0:
        return
    slippage_bps = (actual_price - expected_price) / expected_price * 10000
    if side == "SELL":
        slippage_bps = -slippage_bps
    with _conn() as c:
        c.execute(
            """INSERT INTO slippage_log
               (ts, trade_id, coin, side, expected_price, actual_price, slippage_bps, qty)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (_now_iso(), trade_id, coin, side, expected_price, actual_price,
             slippage_bps, qty),
        )


# ---------------------------------------------------------------------------
# Query API
# ---------------------------------------------------------------------------

def query_decisions(
    *,
    coin: str | None = None,
    source: str | None = None,
    since: str | None = None,
    until: str | None = None,
    decision: str | None = None,
    limit: int = 50,
) -> list[dict]:
    init_db()
    sql = "SELECT * FROM llm_decisions WHERE 1=1"
    params: list[Any] = []
    if coin:
        sql += " AND coin=?"
        params.append(coin.lower())
    if source:
        sql += " AND source=?"
        params.append(source)
    if since:
        sql += " AND ts>=?"
        params.append(since)
    if until:
        sql += " AND ts<=?"
        params.append(until)
    if decision:
        sql += " AND decision=?"
        params.append(decision)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def query_trades(
    *,
    coin: str | None = None,
    direction: str | None = None,
    closed_only: bool = False,
    is_shadow: bool | None = False,
    since: str | None = None,
    limit: int = 50,
) -> list[dict]:
    init_db()
    sql = "SELECT * FROM trades WHERE 1=1"
    params: list[Any] = []
    if coin:
        sql += " AND coin=?"
        params.append(coin.lower())
    if direction:
        sql += " AND direction=?"
        params.append(direction)
    if closed_only:
        sql += " AND closed_at IS NOT NULL"
    if is_shadow is not None:
        sql += " AND is_shadow=?"
        params.append(1 if is_shadow else 0)
    if since:
        sql += " AND opened_at>=?"
        params.append(since)
    sql += " ORDER BY opened_at DESC LIMIT ?"
    params.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def query_similar_trades(
    *,
    coin: str,
    direction: str,
    rsi: float | None = None,
    ema_gap_pct: float | None = None,
    vol_ratio: float | None = None,
    limit: int = 10,
) -> list[dict]:
    """Phase 1: SQL filter by coin+direction. Phase 3: replaced by vector search."""
    init_db()
    rows = query_trades(coin=coin, direction=direction, closed_only=True,
                        is_shadow=False, limit=200)
    if not rows:
        return []
    if rsi is None and ema_gap_pct is None and vol_ratio is None:
        return rows[:limit]

    def dist(t):
        ind = json.loads(t.get("indicators_open_json") or "{}")
        d = 0.0
        if rsi is not None and ind.get("rsi") is not None:
            d += abs(rsi - ind["rsi"]) / 50
        if ema_gap_pct is not None and ind.get("ema_gap_pct") is not None:
            d += abs(ema_gap_pct - ind["ema_gap_pct"]) * 10
        if vol_ratio is not None and ind.get("vol_ratio") is not None:
            d += abs(vol_ratio - ind["vol_ratio"]) / 5
        return d

    rows.sort(key=dist)
    return rows[:limit]


def get_decision(decision_id: int) -> dict | None:
    init_db()
    with _conn() as c:
        row = c.execute("SELECT * FROM llm_decisions WHERE id=?", (decision_id,)).fetchone()
        return dict(row) if row else None


def get_trade(trade_id: int) -> dict | None:
    init_db()
    with _conn() as c:
        row = c.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Aggregations for dashboard / weekly review
# ---------------------------------------------------------------------------

def llm_accuracy_stats(since: str | None = None) -> dict:
    """Returns: per-source decision counts and accuracy where outcome is known."""
    init_db()
    with _conn() as c:
        if since:
            decisions = c.execute(
                "SELECT * FROM llm_decisions WHERE ts >= ?", (since,)
            ).fetchall()
        else:
            decisions = c.execute("SELECT * FROM llm_decisions").fetchall()

    by_source: dict[str, dict] = {}
    for d in decisions:
        src = d["source"]
        s = by_source.setdefault(src, {"total": 0, "by_decision": {},
                                        "with_outcome": 0, "tp": 0, "sl": 0,
                                        "manual": 0, "timeout": 0})
        s["total"] += 1
        s["by_decision"][d["decision"]] = s["by_decision"].get(d["decision"], 0) + 1
        if d["trade_id"]:
            with _conn() as c2:
                trade = c2.execute(
                    "SELECT result FROM trades WHERE id=? AND closed_at IS NOT NULL",
                    (d["trade_id"],),
                ).fetchone()
            if trade and trade["result"]:
                s["with_outcome"] += 1
                r = trade["result"].lower()
                if "tp" in r:
                    s["tp"] += 1
                elif "sl" in r:
                    s["sl"] += 1
                elif "manual" in r:
                    s["manual"] += 1
                elif "timeout" in r:
                    s["timeout"] += 1
    return by_source


def trade_pnl_stats(since: str | None = None, is_shadow: bool = False) -> dict:
    init_db()
    sql = ("SELECT coin, direction, result, pnl_usd, r_multiple FROM trades "
           "WHERE closed_at IS NOT NULL AND is_shadow=?")
    params: list[Any] = [1 if is_shadow else 0]
    if since:
        sql += " AND closed_at >= ?"
        params.append(since)
    with _conn() as c:
        rows = c.execute(sql, params).fetchall()
    if not rows:
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "pnl": 0,
                "total_r": 0, "avg_r": 0, "by_coin": {}}
    wins = sum(1 for r in rows if (r["pnl_usd"] or 0) > 0)
    losses = len(rows) - wins
    total_pnl = sum(r["pnl_usd"] or 0 for r in rows)
    total_r = sum(r["r_multiple"] or 0 for r in rows)
    by_coin: dict[str, dict] = {}
    for r in rows:
        c_ = by_coin.setdefault(r["coin"], {"trades": 0, "wins": 0, "pnl": 0, "r": 0})
        c_["trades"] += 1
        if (r["pnl_usd"] or 0) > 0:
            c_["wins"] += 1
        c_["pnl"] += r["pnl_usd"] or 0
        c_["r"] += r["r_multiple"] or 0
    for k, v in by_coin.items():
        v["win_rate"] = v["wins"] / v["trades"] * 100 if v["trades"] else 0
    return {
        "trades": len(rows),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(rows) * 100,
        "pnl": round(total_pnl, 4),
        "total_r": round(total_r, 2),
        "avg_r": round(total_r / len(rows), 3),
        "by_coin": by_coin,
    }


def confidence_calibration(since: str | None = None) -> list[dict]:
    """Group decisions into confidence buckets and compute outcome accuracy."""
    init_db()
    sql = """SELECT d.confidence, t.result, t.pnl_usd
             FROM llm_decisions d JOIN trades t ON d.trade_id = t.id
             WHERE d.confidence IS NOT NULL AND t.closed_at IS NOT NULL"""
    params: list[Any] = []
    if since:
        sql += " AND d.ts >= ?"
        params.append(since)
    with _conn() as c:
        rows = c.execute(sql, params).fetchall()
    buckets = [(0, 50), (50, 70), (70, 85), (85, 100)]
    out = []
    for lo, hi in buckets:
        items = [r for r in rows if lo <= r["confidence"] < hi]
        if not items:
            out.append({"bucket": f"{lo}-{hi}", "n": 0, "win_rate": None, "avg_pnl": None})
            continue
        wins = sum(1 for r in items if (r["pnl_usd"] or 0) > 0)
        out.append({
            "bucket": f"{lo}-{hi}",
            "n": len(items),
            "win_rate": round(wins / len(items) * 100, 1),
            "avg_pnl": round(sum(r["pnl_usd"] or 0 for r in items) / len(items), 4),
        })
    return out


# ---------------------------------------------------------------------------
# Backfill from executor_state.json
# ---------------------------------------------------------------------------

def backfill_from_executor_state():
    """Seed trades table from existing executor_state.json trade_history."""
    es_path = SCRIPT_DIR / "data" / "executor_state.json"
    if not es_path.exists():
        return 0
    data = json.loads(es_path.read_text())
    history = data.get("trade_history", [])
    inserted = 0
    init_db()
    for t in history:
        coin = t.get("coin")
        if not coin:
            continue
        with _conn() as c:
            existing = c.execute(
                "SELECT id FROM trades WHERE coin=? AND opened_at=?",
                (coin, t.get("time", "")),
            ).fetchone()
            if existing:
                continue
            entry = float(t.get("entry", 0))
            close = float(t.get("close", 0))
            direction = t.get("direction", "LONG")
            result = t.get("result", "MANUAL")
            pnl = float(t.get("pnl", 0))
            cur = c.execute(
                """INSERT INTO trades
                   (coin, direction, entry_price, opened_at,
                    closed_at, close_price, result, pnl_usd, notes, is_shadow)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (coin, direction, entry, t.get("time", _now_iso()),
                 t.get("time", _now_iso()), close, result, pnl,
                 t.get("note", "backfill")),
            )
            inserted += 1
    return inserted


# ---------------------------------------------------------------------------
# Self-test entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    init_db()
    print(f"DB initialized at {DB_PATH}")
    if "--backfill" in sys.argv:
        n = backfill_from_executor_state()
        print(f"Backfilled {n} trades from executor_state.json")
    if "--stats" in sys.argv:
        s = trade_pnl_stats()
        print(f"Trade stats: {json.dumps(s, indent=2)}")
        a = llm_accuracy_stats()
        print(f"LLM accuracy: {json.dumps(a, indent=2)}")
