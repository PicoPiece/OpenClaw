#!/usr/bin/env python3
"""
Prompt Registry — versioned prompt management with A/B testing.

The active variant + variant assignments live in
data/prompts/signal_review_active.json. Prompt bodies live as .md templates
beside that config.

Usage from scripts:
    from prompt_registry import resolve_variant, ab_compare
    variant_name, version, body = resolve_variant("signal_review", coin="sol")
    # body still uses python format-string placeholders {coin}, {entry}, etc.

Standalone CLI:
    python3 prompt_registry.py --status
    python3 prompt_registry.py --compare signal_review --since 2026-04-01
    python3 prompt_registry.py --activate signal_review B
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = SCRIPT_DIR / "data" / "prompts"


def load_active(family: str) -> dict:
    cfg_path = PROMPTS_DIR / f"{family}_active.json"
    if not cfg_path.exists():
        return {"active_variant": "A", "ab_split_enabled": False, "variants": {}}
    return json.loads(cfg_path.read_text())


def save_active(family: str, cfg: dict):
    cfg_path = PROMPTS_DIR / f"{family}_active.json"
    cfg_path.write_text(json.dumps(cfg, indent=2))


def _hash_to_unit(seed: str) -> float:
    """Stable 0-1 hash used for deterministic A/B assignment."""
    h = hashlib.sha256(seed.encode()).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


def resolve_variant(family: str, key: str = "") -> tuple[str, str, str]:
    """Return (variant_name, version, prompt_body).

    If A/B split enabled, the variant is chosen deterministically from
    sha256(key) so the same coin/signal repeatedly gets the same variant
    until the registry is changed.
    """
    cfg = load_active(family)
    variants = cfg.get("variants", {})
    if not variants:
        raise RuntimeError(f"No variants defined for {family}")

    if cfg.get("ab_split_enabled") and key and "B" in variants:
        unit = _hash_to_unit(f"{family}:{key}")
        chosen = "B" if unit < cfg.get("ab_split_pct", 0.5) else "A"
    else:
        chosen = cfg.get("active_variant", "A")

    info = variants.get(chosen) or variants.get("A")
    body_path = PROMPTS_DIR / info["file"]
    body = body_path.read_text() if body_path.exists() else ""
    body = "\n".join(line for line in body.splitlines() if not line.startswith("> "))
    body = "\n".join(line for line in body.splitlines() if not line.startswith("# "))
    return chosen, info["version"], body.strip()


def ab_compare(family: str, since: str | None = None) -> dict:
    """Return win-rate comparison between variants A and B for closed trades."""
    sys.path.insert(0, str(SCRIPT_DIR))
    import decision_logger

    out: dict = {}
    with decision_logger._conn() as c:
        sql = """SELECT d.prompt_variant,
                        COUNT(t.id) AS n,
                        SUM(CASE WHEN t.pnl_usd > 0 THEN 1 ELSE 0 END) AS wins,
                        SUM(t.pnl_usd) AS pnl,
                        SUM(t.r_multiple) AS r
                 FROM llm_decisions d
                 JOIN trades t ON t.id = d.trade_id
                 WHERE t.closed_at IS NOT NULL AND t.is_shadow=0"""
        params = []
        if since:
            sql += " AND d.ts >= ?"
            params.append(since)
        sql += " GROUP BY d.prompt_variant"
        for row in c.execute(sql, params).fetchall():
            v = row["prompt_variant"] or "A"
            n = row["n"] or 0
            out[v] = {
                "n": n,
                "wins": row["wins"] or 0,
                "win_rate": round((row["wins"] or 0) / n * 100, 2) if n else 0,
                "total_pnl": round(row["pnl"] or 0, 4),
                "total_r": round(row["r"] or 0, 2),
            }
    return out


def cli():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--compare", type=str, help="family name (e.g. signal_review)")
    ap.add_argument("--since", type=str, default=None)
    ap.add_argument("--activate", nargs=2, metavar=("FAMILY", "VARIANT"))
    ap.add_argument("--enable-ab", type=str, metavar="FAMILY")
    ap.add_argument("--disable-ab", type=str, metavar="FAMILY")
    args = ap.parse_args()

    if args.status:
        for f in PROMPTS_DIR.glob("*_active.json"):
            family = f.stem.replace("_active", "")
            cfg = load_active(family)
            print(f"\n[{family}]")
            print(f"  active: {cfg.get('active_variant')}  |  A/B: {cfg.get('ab_split_enabled')}")
            for k, v in cfg.get("variants", {}).items():
                print(f"  {k}: {v['version']} -> {v['file']}")
        return

    if args.compare:
        result = ab_compare(args.compare, since=args.since)
        print(json.dumps(result, indent=2))
        return

    if args.activate:
        family, variant = args.activate
        cfg = load_active(family)
        if variant not in cfg.get("variants", {}):
            print(f"variant {variant!r} not defined")
            return
        cfg["active_variant"] = variant
        save_active(family, cfg)
        print(f"activated {family} -> {variant}")
        return

    if args.enable_ab:
        cfg = load_active(args.enable_ab)
        cfg["ab_split_enabled"] = True
        save_active(args.enable_ab, cfg)
        print(f"A/B split enabled for {args.enable_ab}")
        return

    if args.disable_ab:
        cfg = load_active(args.disable_ab)
        cfg["ab_split_enabled"] = False
        save_active(args.disable_ab, cfg)
        print(f"A/B split disabled for {args.disable_ab}")
        return

    ap.print_help()


if __name__ == "__main__":
    cli()
