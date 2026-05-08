#!/usr/bin/env python3
"""
RAG Memory — vectorized similarity search over past trades.

Default backend: pure numpy on a small feature vector built from indicators.
This is intentionally simple and dependency-free — for trading, the relevant
"embedding" is the numerical feature space (RSI, EMA gap, volume ratio,
ATR %, hour-of-day, BTC trend). Text embeddings add noise.

Optional backend: chromadb (only loaded if installed). Provides text-based
semantic search over decision reasons. Use for "explain why" v2 queries.

Storage:
  - Numeric vectors: persisted in decisions.db as a small BLOB column on
    `trades` (added on demand via ALTER TABLE).
  - chromadb: data/vector_db/ when available.

Public API:
  rebuild_index()             -> reindex all closed trades
  query(signal_features, k)   -> list of similar past trades + outcome stats
  query_text(text, k)         -> semantic text query (chromadb only)
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import decision_logger  # noqa: E402

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore

try:
    import chromadb  # type: ignore
    _chroma_client = chromadb.PersistentClient(path=str(SCRIPT_DIR / "data" / "vector_db"))
    _chroma_collection = _chroma_client.get_or_create_collection("trades")
except Exception:
    _chroma_client = None
    _chroma_collection = None


# Feature order (must stay stable for stored vectors)
FEATURE_NAMES = [
    "direction_long",   # 1.0 if LONG else 0
    "direction_short",  # 1.0 if SHORT else 0
    "rsi_norm",         # rsi / 100
    "ema_gap_pct",      # raw % (signed)
    "vol_ratio",        # raw
    "atr_pct",          # atr / entry * 100
    "rr",
    "trend_up",         # 1 if UPTREND else 0
    "trend_down",       # 1 if DOWNTREND else 0
    "hour_sin",         # sin(2pi*hour/24) — captures time of day
    "hour_cos",
]


def _hour_from_ts(ts: str | None) -> int:
    if not ts:
        return 0
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).hour
    except Exception:
        return 0


def build_feature_vector(*, direction: str, rsi: float | None,
                          ema_gap_pct: float | None, vol_ratio: float | None,
                          atr: float | None, entry: float | None,
                          rr: float | None, trend: str | None,
                          opened_at: str | None = None) -> list[float]:
    is_long = 1.0 if (direction or "").upper() == "LONG" else 0.0
    is_short = 1.0 - is_long if (direction or "").upper() == "SHORT" else 0.0
    rsi_v = (rsi or 50.0) / 100.0
    ema_v = (ema_gap_pct or 0.0)
    vol_v = (vol_ratio or 1.0)
    atr_pct = ((atr or 0.0) / (entry or 1.0)) * 100 if entry else 0.0
    rr_v = rr or 0.0
    trend_up = 1.0 if "UPTREND" in (trend or "") else 0.0
    trend_down = 1.0 if "DOWNTREND" in (trend or "") else 0.0
    hour = _hour_from_ts(opened_at)
    h_sin = math.sin(2 * math.pi * hour / 24)
    h_cos = math.cos(2 * math.pi * hour / 24)
    return [is_long, is_short, rsi_v, ema_v, vol_v, atr_pct, rr_v,
            trend_up, trend_down, h_sin, h_cos]


def _ensure_embedding_column():
    decision_logger.init_db()
    with decision_logger._conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(trades)").fetchall()}
        if "embedding_json" not in cols:
            c.execute("ALTER TABLE trades ADD COLUMN embedding_json TEXT")


def _features_from_trade(trade_row) -> list[float] | None:
    try:
        ind = json.loads(trade_row.get("indicators_open_json") or "{}")
    except Exception:
        ind = {}
    return build_feature_vector(
        direction=trade_row.get("direction"),
        rsi=ind.get("rsi"),
        ema_gap_pct=ind.get("ema_gap_pct"),
        vol_ratio=ind.get("vol_ratio"),
        atr=ind.get("atr"),
        entry=trade_row.get("entry_price"),
        rr=ind.get("rr"),
        trend=ind.get("trend"),
        opened_at=trade_row.get("opened_at"),
    )


def rebuild_index() -> dict:
    """Embed all closed trades into the trades.embedding_json column +
    optionally into chromadb."""
    _ensure_embedding_column()
    indexed = 0
    chroma_added = 0
    with decision_logger._conn() as c:
        rows = c.execute(
            "SELECT * FROM trades WHERE closed_at IS NOT NULL AND is_shadow=0"
        ).fetchall()
        for r in rows:
            trade = dict(r)
            features = _features_from_trade(trade)
            if features is None:
                continue
            c.execute(
                "UPDATE trades SET embedding_json=? WHERE id=?",
                (json.dumps(features), trade["id"]),
            )
            indexed += 1

            if _chroma_collection is not None:
                try:
                    text = (
                        f"{trade.get('coin','').upper()} {trade.get('direction','')} "
                        f"result={trade.get('result','')} "
                        f"pnl=${trade.get('pnl_usd',0):+.2f} "
                        f"R={trade.get('r_multiple') or 0:+.2f}"
                    )
                    _chroma_collection.upsert(
                        ids=[str(trade["id"])],
                        documents=[text],
                        metadatas=[{
                            "coin": trade.get("coin",""),
                            "direction": trade.get("direction",""),
                            "result": trade.get("result",""),
                            "pnl_usd": float(trade.get("pnl_usd") or 0),
                            "r_multiple": float(trade.get("r_multiple") or 0),
                        }],
                    )
                    chroma_added += 1
                except Exception:
                    pass

    return {"indexed": indexed, "chroma_added": chroma_added,
            "chroma_enabled": _chroma_collection is not None}


def _cosine(a, b) -> float:
    if np is None:
        # Fallback pure python
        s = sum(x*y for x, y in zip(a, b))
        na = math.sqrt(sum(x*x for x in a)) or 1e-9
        nb = math.sqrt(sum(y*y for y in b)) or 1e-9
        return s / (na * nb)
    a_arr = np.asarray(a, dtype=float)
    b_arr = np.asarray(b, dtype=float)
    na = np.linalg.norm(a_arr) or 1e-9
    nb = np.linalg.norm(b_arr) or 1e-9
    return float(a_arr.dot(b_arr) / (na * nb))


def query(*, direction: str, rsi: float | None = None,
          ema_gap_pct: float | None = None, vol_ratio: float | None = None,
          atr: float | None = None, entry: float | None = None,
          rr: float | None = None, trend: str | None = None,
          coin: str | None = None, k: int = 10) -> dict:
    """Return top-k similar closed trades + outcome aggregate."""
    _ensure_embedding_column()
    qv = build_feature_vector(
        direction=direction, rsi=rsi, ema_gap_pct=ema_gap_pct,
        vol_ratio=vol_ratio, atr=atr, entry=entry, rr=rr, trend=trend,
    )

    sql = ("SELECT * FROM trades WHERE closed_at IS NOT NULL AND is_shadow=0 "
           "AND embedding_json IS NOT NULL")
    params: list[Any] = []
    if coin:
        sql += " AND coin=?"
        params.append(coin.lower())
    sql += " AND direction=?"
    params.append(direction)
    sql += " ORDER BY closed_at DESC LIMIT 500"

    with decision_logger._conn() as c:
        rows = c.execute(sql, params).fetchall()

    scored = []
    for r in rows:
        try:
            v = json.loads(r["embedding_json"])
            sim = _cosine(qv, v)
            scored.append((sim, dict(r)))
        except Exception:
            continue
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:k]
    if not top:
        return {"k": 0, "win_rate": None, "avg_r": None, "matches": []}

    wins = sum(1 for _s, t in top if (t.get("pnl_usd") or 0) > 0)
    avg_r = sum((t.get("r_multiple") or 0) for _s, t in top) / len(top)
    pnl_total = sum((t.get("pnl_usd") or 0) for _s, t in top)
    return {
        "k": len(top),
        "win_rate": round(wins / len(top) * 100, 1),
        "avg_r": round(avg_r, 3),
        "total_pnl": round(pnl_total, 4),
        "best": max(top, key=lambda x: x[1]["pnl_usd"] or 0)[1],
        "worst": min(top, key=lambda x: x[1]["pnl_usd"] or 0)[1],
        "matches": [
            {
                "trade_id": t["id"], "coin": t["coin"], "result": t.get("result"),
                "pnl_usd": t.get("pnl_usd"), "r_multiple": t.get("r_multiple"),
                "similarity": round(s, 4),
                "opened_at": t["opened_at"],
            }
            for s, t in top
        ],
    }


def query_text(text: str, k: int = 5) -> list[dict]:
    """Semantic text search via chromadb. Returns [] if not available."""
    if _chroma_collection is None:
        return []
    try:
        res = _chroma_collection.query(query_texts=[text], n_results=k)
        out = []
        for i, doc in enumerate(res.get("documents", [[]])[0]):
            meta = (res.get("metadatas", [[]])[0] or [{}])[i]
            out.append({"text": doc, "meta": meta,
                        "id": res.get("ids", [[]])[0][i]})
        return out
    except Exception:
        return []


def cli():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--query", action="store_true")
    ap.add_argument("--coin", type=str)
    ap.add_argument("--direction", type=str, default="LONG")
    ap.add_argument("--rsi", type=float)
    ap.add_argument("--ema-gap", type=float, dest="ema_gap")
    ap.add_argument("--vol-ratio", type=float, dest="vol_ratio")
    ap.add_argument("--text", type=str, help="text query (chromadb)")
    ap.add_argument("--k", type=int, default=10)
    args = ap.parse_args()

    if args.rebuild:
        print(json.dumps(rebuild_index(), indent=2))
        return
    if args.text:
        print(json.dumps(query_text(args.text, k=args.k), indent=2))
        return
    if args.query:
        out = query(direction=args.direction, rsi=args.rsi,
                    ema_gap_pct=args.ema_gap, vol_ratio=args.vol_ratio,
                    coin=args.coin, k=args.k)
        print(json.dumps(out, indent=2, default=str))
        return
    ap.print_help()


if __name__ == "__main__":
    cli()
