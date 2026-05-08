#!/usr/bin/env python3
"""
Knowledge Loader for OpenClaw bot.

Reads `KNOWLEDGE.md` (sections delimited by `## SECTION:NAME`) and provides:
  - get_base_pack(): always-included sections (CORE_IDENTITY, PORTFOLIO_LAYERS,
    FUTURES_ALLOWLIST, GRID_BOTS, ASI_FORMULA, CONSTRAINTS) compact for system prompt.
  - retrieve(query): keyword-based retrieval of relevant sections to augment prompt.
  - all_sections(): for debugging / inspection.

Design choices:
  - No external deps (no chromadb, no embeddings) — pure Python, deterministic.
  - Section keys are stable (manually curated keywords).
  - Token budget: base pack ≤ ~1500 tokens, retrieved chunks ≤ 1 section each.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KNOWLEDGE_FILE = ROOT / "KNOWLEDGE.md"

SECTION_RE = re.compile(r"^##\s+SECTION:([A-Z0-9_]+)\s*$", re.M)

# Sections always included in system prompt (compact ground truth)
BASE_SECTIONS = [
    "CORE_IDENTITY",
    "PORTFOLIO_LAYERS",
    "FUTURES_ALLOWLIST",
    "GRID_BOTS",
    "ASI_FORMULA",
    "CONSTRAINTS",
]

# Keyword -> section mapping for dynamic retrieval.
# Order matters: first match wins per section. Multi-section retrieval allowed.
RETRIEVAL_KEYWORDS = {
    "RISK_SHIELDS": [
        "shield", "shields", "lá chắn", "la chan", "circuit breaker",
        "auto-suspend", "drawdown", "regime drift", "tier_a", "tier_b",
        "coin tier", "rủi ro", "rui ro", "risk system",
    ],
    "TRAILING_SL_TIERS": [
        "trail", "trailing", "tsl", "stop loss tier", "atr",
        "chase", "breakeven", "early lock", "tier", "sl tier",
    ],
    "RISK_PARAMS": [
        "leverage", "đòn bẩy", "don bay", "risk per trade", "rsi",
        "max risk", "position sizing", "max daily loss", "max_daily_loss",
        "vol regime", "min volume", "rủi ro mỗi lệnh",
    ],
    "LESSONS_LEARNED": [
        "lesson", "lessons", "bài học", "bai hoc", "mkr", "rndr", "render",
        "ip whitelist", "ip ban", "dead market", "thị trường chết",
        "fail", "failure", "thất bại", "that bai", "rút kinh nghiệm",
    ],
    "OPERATIONAL_PLAYBOOK": [
        "playbook", "routine", "daily", "weekly", "monthly", "lịch",
        "schedule", "cron", "morning briefing", "weekly analysis",
        "operational", "vận hành", "van hanh",
    ],
    "FILE_PATHS": [
        "file path", "where is", "ở đâu", "o dau", "code structure",
        "executor_state", "trading_state", "decisions.db", "grid_config",
        "module", "script",
    ],
    "COMMON_QUESTIONS": [
        "tại sao", "tai sao", "vì sao", "vi sao", "lý do", "ly do",
        "why", "should i", "có nên", "co nen",
    ],
    # The 3 base sections above are always included so they're not retrieval-eligible.
}

# Cache parsed sections at module load time
_sections_cache: dict[str, str] | None = None


def _parse_sections() -> dict[str, str]:
    """Parse KNOWLEDGE.md into {section_name: body}."""
    if not KNOWLEDGE_FILE.exists():
        return {}
    text = KNOWLEDGE_FILE.read_text(encoding="utf-8")
    sections: dict[str, str] = {}
    matches = list(SECTION_RE.finditer(text))
    for i, m in enumerate(matches):
        name = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        body = re.sub(r"\n---\s*$", "", body).strip()
        sections[name] = body
    return sections


def _load() -> dict[str, str]:
    global _sections_cache
    if _sections_cache is None:
        _sections_cache = _parse_sections()
    return _sections_cache


def reload():
    """Force re-read of KNOWLEDGE.md (for dev/testing)."""
    global _sections_cache
    _sections_cache = None
    return _load()


def all_sections() -> dict[str, str]:
    return dict(_load())


def get_section(name: str) -> str:
    return _load().get(name, "")


def get_base_pack() -> str:
    """Return the always-included compact knowledge base."""
    secs = _load()
    parts = []
    for name in BASE_SECTIONS:
        body = secs.get(name)
        if not body:
            continue
        parts.append(f"### {name}\n{body}")
    return "\n\n".join(parts)


def retrieve(query: str, max_sections: int = 3) -> list[tuple[str, str]]:
    """
    Return list of (section_name, body) relevant to the query.
    Uses simple keyword matching (case-insensitive, accent-insensitive-ish).
    """
    if not query:
        return []
    q = query.lower()
    secs = _load()
    hits: list[tuple[int, str]] = []
    for sec_name, kws in RETRIEVAL_KEYWORDS.items():
        if sec_name not in secs:
            continue
        score = sum(1 for kw in kws if kw in q)
        if score > 0:
            hits.append((score, sec_name))
    hits.sort(reverse=True)
    return [(name, secs[name]) for _, name in hits[:max_sections]]


def build_knowledge_block(query: str | None = None,
                          include_base: bool = True,
                          max_retrieved: int = 3) -> str:
    """
    Build a single knowledge block ready to inject into system prompt.

    Returns markdown-formatted text:
        BASE PACK (always-included sections)
        + RETRIEVED (query-relevant sections)
    """
    parts: list[str] = []
    if include_base:
        parts.append("# OPENCLAW KNOWLEDGE BASE (ground truth — do NOT contradict)\n")
        parts.append(get_base_pack())
    if query:
        retrieved = retrieve(query, max_sections=max_retrieved)
        if retrieved:
            parts.append("\n\n# RELEVANT DETAIL FOR THIS QUERY\n")
            for name, body in retrieved:
                parts.append(f"### {name}\n{body}")
    return "\n\n".join(parts)


# =============================================================================
# CLI for inspection
# =============================================================================
if __name__ == "__main__":
    import argparse, sys
    p = argparse.ArgumentParser()
    p.add_argument("--list", action="store_true", help="List all section names")
    p.add_argument("--section", help="Print one section by name")
    p.add_argument("--retrieve", help="Test retrieval for a query")
    p.add_argument("--block", help="Print full block built for a query")
    args = p.parse_args()

    if args.list:
        secs = _load()
        for name in secs:
            chars = len(secs[name])
            tag = " (base)" if name in BASE_SECTIONS else ""
            print(f"  {name:25s} {chars:5d} chars{tag}")
        sys.exit(0)
    if args.section:
        body = get_section(args.section)
        print(body or f"(section {args.section} not found)")
        sys.exit(0)
    if args.retrieve:
        hits = retrieve(args.retrieve)
        print(f"Query: {args.retrieve}")
        for name, _ in hits:
            print(f"  -> {name}")
        sys.exit(0)
    if args.block:
        block = build_knowledge_block(args.block)
        print(f"--- BLOCK for query: {args.block} ---")
        print(block)
        print(f"--- ({len(block)} chars, ~{len(block)//4} tokens) ---")
        sys.exit(0)
    print("Use --list / --section / --retrieve / --block")
