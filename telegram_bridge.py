#!/usr/bin/env python3
"""
Telegram Bridge — Bidirectional chat between user and AI agent.

User chats with bot via Telegram; bot answers using:
  - Slash commands (FREE, structured): /status /positions /grids /asi /wallet /signals /help /pause /resume
  - Free-text → DeepSeek-powered AI response with system context

Cost: ~$0.001 per chat × 30-50 chats/day ≈ $1-3/mo

Usage:
    python3 telegram_bridge.py               # daemon mode (poll every 3s)
    python3 telegram_bridge.py --test "/status"   # test single command

Auth: Only responds to TELEGRAM_CHAT_ID owner (whitelist single user).
"""
from __future__ import annotations
import argparse
import json
import os
import sqlite3
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

ENV_FILE = ROOT / ".env"
STATE_FILE = DATA / "telegram_bridge_state.json"
MEMORY_FILE = DATA / "telegram_memory.json"
DEEPSEEK_API = "https://api.deepseek.com/v1/chat/completions"
TG_API_BASE = "https://api.telegram.org/bot{token}"

POLL_INTERVAL = 3.0
LONG_POLL_TIMEOUT = 25

# Memory config
RECENT_TURNS_LIMIT = 10  # keep last N turns verbatim
SUMMARIZE_TRIGGER = 12   # when recent_turns exceeds this, summarize oldest
SUMMARIZE_BATCH = 6      # how many oldest turns to summarize at once
SESSION_IDLE_MIN = 30    # minutes of idle → consider new session (informational)


def load_env():
    if not ENV_FILE.exists(): return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def safe_load(p: Path):
    if not p.exists(): return None
    try: return json.loads(p.read_text())
    except Exception: return None


def save_state(state: dict):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def load_state() -> dict:
    return safe_load(STATE_FILE) or {"last_update_id": 0}


# =============================================================================
# Memory layer — persistent multi-turn conversation
# =============================================================================

def load_memory() -> dict:
    mem = safe_load(MEMORY_FILE) or {}
    return {
        "summary": mem.get("summary", ""),
        "recent_turns": mem.get("recent_turns", []),
        "last_active_ts": mem.get("last_active_ts"),
        "total_turns": mem.get("total_turns", 0),
    }


def save_memory(mem: dict):
    MEMORY_FILE.parent.mkdir(exist_ok=True)
    MEMORY_FILE.write_text(json.dumps(mem, indent=2, ensure_ascii=False))


def summarize_old_turns(turns: list, current_summary: str) -> str:
    """Compress old turns into a bullet-point summary via DeepSeek."""
    if not turns:
        return current_summary
    convo_text = "\n".join(
        f"{'USER' if t['role']=='user' else 'BOT'}: {t['content'][:300]}"
        for t in turns
    )
    sys_msg = ("Bạn là tóm tắt viên. Tóm tắt cuộc hội thoại sau thành "
               "BULLET POINTS ngắn gọn (5-8 bullets), giữ lại facts/decisions/"
               "context quan trọng cho follow-up. KHÔNG bịa thêm gì.")
    user_msg = ""
    if current_summary:
        user_msg += f"BẢN TÓM TẮT TRƯỚC:\n{current_summary}\n\n"
    user_msg += f"CUỘC HỘI THOẠI MỚI CẦN TÓM TẮT VÀ MERGE:\n{convo_text}\n\n"
    user_msg += "Trả về bản tóm tắt MỚI hợp nhất (tối đa 800 từ)."
    msgs = [{"role": "system", "content": sys_msg},
            {"role": "user", "content": user_msg}]
    return deepseek_chat(msgs, max_tokens=600)


def append_turn(role: str, content: str):
    """Add turn to memory; auto-summarize if recent_turns gets too big."""
    mem = load_memory()
    mem["recent_turns"].append({
        "role": role,
        "content": content,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    mem["last_active_ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    mem["total_turns"] = mem.get("total_turns", 0) + 1
    if len(mem["recent_turns"]) > SUMMARIZE_TRIGGER:
        old = mem["recent_turns"][:SUMMARIZE_BATCH]
        kept = mem["recent_turns"][SUMMARIZE_BATCH:]
        new_summary = summarize_old_turns(old, mem["summary"])
        mem["summary"] = new_summary
        mem["recent_turns"] = kept
        print(f"[memory] summarized {SUMMARIZE_BATCH} oldest turns, kept {len(kept)} recent")
    save_memory(mem)


def get_session_status() -> str:
    mem = load_memory()
    last_ts = mem.get("last_active_ts")
    if not last_ts:
        return "new"
    try:
        last = datetime.fromisoformat(last_ts.replace("Z","+00:00"))
        idle_min = (datetime.now(timezone.utc) - last).total_seconds() / 60
        if idle_min > SESSION_IDLE_MIN:
            return f"resumed_after_{int(idle_min)}min"
        return "active"
    except Exception:
        return "unknown"


def reset_memory():
    mem = {"summary": "", "recent_turns": [], "last_active_ts": None, "total_turns": 0}
    save_memory(mem)


# =============================================================================
# Telegram API
# =============================================================================

def tg_request(method: str, params: dict = None, timeout: int = 30):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = TG_API_BASE.format(token=token) + f"/{method}"
    if params:
        data = json.dumps(params).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def tg_send(text: str, parse_mode: str = "HTML", reply_to: int = None):
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    body = {"chat_id": chat_id, "text": text}
    if parse_mode: body["parse_mode"] = parse_mode
    if reply_to: body["reply_to_message_id"] = reply_to
    try:
        return tg_request("sendMessage", body, timeout=15)
    except Exception as e:
        if parse_mode:
            return tg_send(text, parse_mode=None, reply_to=reply_to)
        print(f"[ERR send] {e}")
        return None


def tg_get_updates(offset: int = 0):
    return tg_request("getUpdates", {"offset": offset, "timeout": LONG_POLL_TIMEOUT,
                                       "allowed_updates": ["message"]},
                       timeout=LONG_POLL_TIMEOUT + 5)


# =============================================================================
# DeepSeek
# =============================================================================

def deepseek_chat(messages: list, max_tokens: int = 800) -> str:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return "❌ DEEPSEEK_API_KEY chưa cấu hình"
    body = {
        "model": "deepseek-chat",
        "messages": messages,
        "temperature": 0.4,
        "max_tokens": max_tokens,
    }
    try:
        req = urllib.request.Request(
            DEEPSEEK_API,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
        return d["choices"][0]["message"]["content"]
    except Exception as e:
        return f"❌ DeepSeek error: {e}"


# =============================================================================
# Helpers
# =============================================================================

def b(s) -> str: return f"<b>{html_escape(s)}</b>"
def i(s) -> str: return f"<i>{html_escape(s)}</i>"
def code(s) -> str: return f"<code>{html_escape(s)}</code>"

def html_escape(s) -> str:
    if s is None: return ""
    return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")


def fmt_money(v: float, plus_sign=False) -> str:
    sign = "+" if (v >= 0 and plus_sign) else ("-" if v < 0 else "")
    return f"{sign}${abs(v):,.2f}"


# =============================================================================
# Slash commands (FREE — no LLM)
# =============================================================================

def cmd_help() -> str:
    lines = [
        b("🤖 OpenClaw AI Bot — Commands"),
        "",
        b("Portfolio:"),
        "  /status     — Tổng quan portfolio + ASI",
        "  /wallet     — Chi tiết all wallets + 24h delta",
        "  /asi        — AI Self-Sustainability Index detail",
        "",
        b("Trading:"),
        "  /positions  — Active futures positions",
        "  /signals    — Recent signals + last decision",
        "  /grids      — Grid bots P&amp;L summary",
        "",
        b("Control:"),
        "  /pause      — Pause auto-trade",
        "  /resume     — Resume auto-trade",
        "  /tradectrl  — Show trading control config",
        "",
        b("Memory & Knowledge:"),
        "  /memory     — Xem conversation memory (summary + recent)",
        "  /forget     — Reset memory (start fresh chat)",
        "  /knowledge  — Inspect knowledge base sections",
        "",
        b("Other:"),
        "  /briefing   — Generate morning briefing now",
        "  /help       — This menu",
        "",
        i("Hoặc chat free-text bằng tiếng Việt → mình nhớ context conversation trước"),
    ]
    return "\n".join(lines)


def cmd_status() -> str:
    wallet_h = safe_load(DATA / "wallet_balance_history.json") or {}
    snaps = wallet_h.get("snapshots", [])
    if not snaps: return "❌ No wallet snapshots yet"
    latest = snaps[-1]
    total = latest.get("total", 0)

    try:
        import self_sustainability
        asi = self_sustainability.compute_asi()
    except Exception as e:
        asi = None

    lines = [b("📊 Portfolio Status")]
    lines.append(f"Total: {b(fmt_money(total))}")
    lines.append("")
    layer_map = {
        "Earn": "💎 HODL Core",
        "Trading Bots": "🌐 Grid Yield",
        "USDⓈ-M Futures": "🤖 Active Futures",
        "Spot": "💵 Reserve",
    }
    for w_name, label in layer_map.items():
        v = latest.get("wallets", {}).get(w_name, 0)
        lines.append(f"  {html_escape(label)}: ${v:,.2f}")
    lines.append("")
    if asi:
        asi_val = f"{asi['asi']:.2f}"
        asi_label = asi['label'].replace('_', ' ')
        lines.append(f"ASI: {asi['status']} {b(asi_val)} ({html_escape(asi_label)})")
        lines.append(f"Net: {fmt_money(asi['net_monthly'], True)}/mo")
    return "\n".join(lines)


def cmd_wallet() -> str:
    wallet_h = safe_load(DATA / "wallet_balance_history.json") or {}
    snaps = wallet_h.get("snapshots", [])
    if not snaps: return "❌ No snapshots"
    latest = snaps[-1]
    cutoff = datetime.fromisoformat(latest["ts"].replace("Z","+00:00")) - timedelta(hours=24)
    snap_24h = None
    for s in reversed(snaps):
        try:
            if datetime.fromisoformat(s["ts"].replace("Z","+00:00")) <= cutoff:
                snap_24h = s; break
        except Exception: pass

    lines = [b("💰 Wallet Overview")]
    lines.append(f"Total: {b(fmt_money(latest.get('total',0)))}")
    lines.append("")
    lines.append(code(f"{'Wallet':22s} {'Now':>10s} {'24h Δ':>10s}"))
    lines.append(code("─" * 44))
    for w, v in sorted(latest.get("wallets", {}).items(), key=lambda x: -x[1]):
        d24 = (v - snap_24h.get("wallets", {}).get(w, 0)) if snap_24h else 0
        sign = "+" if d24 > 0 else ""
        lines.append(code(f"{w:22s} {v:>10.2f} {sign}{d24:>9.2f}"))
    lines.append("")
    lines.append(i(f"Snapshot: {latest['ts'][:19]}  ·  {len(snaps)} total"))
    return "\n".join(lines)


def cmd_asi() -> str:
    try:
        import self_sustainability
        asi = self_sustainability.compute_asi()
    except Exception as e:
        return f"❌ Compute err: {e}"
    pb = asi["profit_breakdown"]; cb = asi["cost_breakdown"]
    asi_val = f"{asi['asi']:.2f}"
    asi_label = asi['label'].replace('_', ' ')
    lines = [
        b("📊 AI Self-Sustainability Index"),
        f"{asi['status']} ASI = {b(asi_val)} ({html_escape(asi_label)})",
        "",
        b("Profit (monthly run-rate):"),
        f"  Futures:  {fmt_money(pb['futures'], True)}",
        f"  Grid:     {fmt_money(pb['grid'], True)}",
        f"  Earn:     {fmt_money(pb['earn'], True)}",
        f"  {b('Total: ')} {fmt_money(asi['profit_monthly'], True)}",
        "",
        b("Cost (monthly):"),
        f"  DeepSeek: ${cb['deepseek']:.2f}",
        f"  Cursor:   ${cb['cursor']:.2f}",
        f"  Anthropic:${cb['anthropic']:.2f}",
        f"  {b('Total: ')} ${asi['cost_monthly']:.2f}",
        "",
        f"{b('NET:')} {fmt_money(asi['net_monthly'], True)}/mo",
        "",
        i(f"Target: ASI ≥ 2.0 for self-sustaining + reinvest"),
    ]
    return "\n".join(lines)


def cmd_positions() -> str:
    state = safe_load(DATA / "workspace-finance" / "trading_state.json") or {}
    positions = [p for p in state.get("positions", []) if p.get("status") == "ACTIVE"]
    if not positions: return f"📌 {b('Active Positions')}: 0\n\n" + i("Hệ thống đang chờ signal phù hợp")
    lines = [b(f"📌 Active Positions ({len(positions)})")]
    for p in positions:
        coin = p.get("coin", "?").upper()
        dir_ = p.get("direction", "?")
        entry = p.get("entry", "?")
        sl = p.get("sl", "?")
        tp = p.get("tp", "?")
        qty = p.get("qty", "?")
        lines.append("")
        lines.append(f"{coin}/{dir_}")
        lines.append(f"  Entry: ${entry}  Qty: {qty}")
        lines.append(f"  SL: ${sl}  TP: ${tp}")
    return "\n".join(lines)


def cmd_signals() -> str:
    db = DATA / "decisions.db"
    if not db.exists(): return "❌ No decisions DB"
    try:
        conn = sqlite3.connect(db); cur = conn.cursor()
        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        cur.execute("SELECT decision, COUNT(*) FROM llm_decisions WHERE ts >= ? GROUP BY decision", (since,))
        counts = dict(cur.fetchall())
        cur.execute("SELECT ts, coin, direction, decision, confidence FROM llm_decisions WHERE ts >= ? ORDER BY ts DESC LIMIT 5", (since,))
        recent = cur.fetchall()
        conn.close()
    except Exception as e:
        return f"❌ DB err: {e}"
    lines = [b("🔔 Signals (last 7 days)")]
    if counts:
        for k, v in counts.items():
            lines.append(f"  {k}: {v}")
    else:
        lines.append("  No signals (market dead?)")
    lines.append("")
    if recent:
        lines.append(b("Last 5 decisions:"))
        for r in recent:
            ts = r[0][:16] if r[0] else "?"
            lines.append(f"  {html_escape(ts)} {html_escape(r[1])} {html_escape(r[2])} → {html_escape(r[3])} c={r[4]}")
    return "\n".join(lines)


def cmd_grids() -> str:
    cfg = safe_load(DATA / "grid_config.json") or {}
    wallet_h = safe_load(DATA / "wallet_balance_history.json") or {}
    snaps = wallet_h.get("snapshots", [])
    bots_now = float(snaps[-1].get("wallets", {}).get("Trading Bots", 0)) if snaps else 0

    grids = []
    invested_total = 0
    for sym, c in cfg.items():
        if sym.startswith("_") or not isinstance(c, dict): continue
        invested = float(c.get("invested_usd", 0))
        invested_total += invested
        grids.append((sym, c, invested))

    lines = [b(f"🌐 Spot Grid Bots ({len(grids)})")]
    lines.append("")
    lines.append(f"Invested:  ${invested_total:,.2f}")
    lines.append(f"Now:       ${bots_now:,.2f}")
    if invested_total > 0:
        unrealized = bots_now - invested_total
        lines.append(f"Unrealized: {fmt_money(unrealized, True)}")
    lines.append("")
    for sym, c, inv in grids:
        lines.append(f"{html_escape(sym)}")
        lower = c.get("lower"); upper = c.get("upper"); ng = c.get("grids")
        lines.append(f"  Range: ${lower}-${upper}  Grids: {ng}  Inv: ${inv:.0f}")
    return "\n".join(lines)


def cmd_pause() -> str:
    p = DATA / "workspace-finance" / "trading_control.json"
    cfg = safe_load(p) or {}
    cfg["auto_trade_enabled"] = False
    cfg["reason"] = "Manual pause via Telegram bridge"
    cfg["updated_by"] = "telegram_bridge"
    cfg["updated_at"] = datetime.now(timezone.utc).isoformat()
    p.write_text(json.dumps(cfg, indent=4))
    return "⏸ " + b("Auto-trade PAUSED") + "\n\nUse /resume to re-enable."


def cmd_resume() -> str:
    p = DATA / "workspace-finance" / "trading_control.json"
    cfg = safe_load(p) or {}
    cfg["auto_trade_enabled"] = True
    cfg["reason"] = "Manual resume via Telegram bridge"
    cfg["updated_by"] = "telegram_bridge"
    cfg["updated_at"] = datetime.now(timezone.utc).isoformat()
    p.write_text(json.dumps(cfg, indent=4))
    return "▶ " + b("Auto-trade RESUMED")


def cmd_tradectrl() -> str:
    cfg = safe_load(DATA / "workspace-finance" / "trading_control.json") or {}
    lines = [b("⚙ Trading Control Config")]
    for k, v in cfg.items():
        lines.append(f"  {html_escape(k)}: {html_escape(v)}")
    return "\n".join(lines)


def cmd_briefing() -> str:
    """Run morning briefing now."""
    try:
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("morning_briefing", ROOT / "reports" / "morning_briefing.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.build_briefing()
    except Exception as e:
        return f"❌ Briefing err: {e}\n{html_escape(traceback.format_exc()[:500])}"


# NOTE: COMMANDS dict is defined below, after cmd_memory and cmd_forget.


# =============================================================================
# Free-text → DeepSeek with system context
# =============================================================================

def build_ai_context() -> str:
    """Compact system context for DeepSeek free-text chat."""
    parts = []
    wallet_h = safe_load(DATA / "wallet_balance_history.json") or {}
    snaps = wallet_h.get("snapshots", [])
    if snaps:
        latest = snaps[-1]
        parts.append(f"Wallet: total ${latest.get('total',0):.0f}, " +
                     ", ".join(f"{w}=${v:.0f}" for w,v in latest.get("wallets",{}).items()))

    state = safe_load(DATA / "executor_state.json") or {}
    parts.append(f"Futures total_pnl=${state.get('total_pnl',0):.2f}, " +
                 f"trades={state.get('total_trades',0)}, conseq_losses={state.get('consecutive_losses',0)}")

    cfg = safe_load(DATA / "grid_config.json") or {}
    grids = [s for s in cfg if not s.startswith("_") and isinstance(cfg[s], dict)]
    parts.append(f"Grid bots: {len(grids)} ({', '.join(grids)})")

    try:
        import self_sustainability
        a = self_sustainability.compute_asi()
        parts.append(f"ASI={a['asi']:.2f} ({a['label']}), profit/mo=${a['profit_monthly']:.0f}, cost/mo=${a['cost_monthly']:.0f}")
    except Exception: pass

    tc = safe_load(DATA / "workspace-finance" / "trading_control.json") or {}
    parts.append(f"Auto-trade: {'ON' if tc.get('auto_trade_enabled') else 'OFF'}, max_daily_loss=${tc.get('max_daily_loss','?')}")

    db = DATA / "decisions.db"
    if db.exists():
        try:
            conn = sqlite3.connect(db); cur = conn.cursor()
            since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            cur.execute("SELECT decision, COUNT(*) FROM llm_decisions WHERE ts >= ? GROUP BY decision", (since,))
            counts = dict(cur.fetchall())
            conn.close()
            parts.append(f"Signals 7d: {counts}")
        except Exception: pass

    return "\n".join(parts)


def free_text_response(user_msg: str) -> str:
    context = build_ai_context()
    mem = load_memory()
    sess_status = get_session_status()

    try:
        import knowledge_loader as kl
        knowledge_block = kl.build_knowledge_block(user_msg, max_retrieved=2)
        retrieved = [n for n, _ in kl.retrieve(user_msg, max_sections=2)]
    except Exception as e:
        knowledge_block = ""
        retrieved = []
        print(f"[knowledge load err] {e}")

    system_parts = [
        "Bạn là OpenClaw, AI trading co-pilot của user. Trả lời tiếng Việt, ngắn gọn.",
        "",
        "CRITICAL RULES:",
        "- KHÔNG bịa fact. Mọi số liệu, coin, shield, tier phải khớp với KNOWLEDGE BASE bên dưới.",
        "- Nếu user hỏi gì không có trong knowledge base hoặc live state, nói thẳng 'không chắc, kiểm tra /XXX command'.",
        "- KHÔNG suy đoán dựa trên kiến thức chung về crypto khi mâu thuẫn với knowledge base.",
        "",
    ]
    if knowledge_block:
        system_parts.append(knowledge_block)
        system_parts.append("")
    system_parts += [
        "# LIVE STATE (refreshed mỗi turn):",
        context,
        "",
    ]
    if mem.get("summary"):
        system_parts.append("# PREVIOUS CONVERSATION SUMMARY (older turns, compressed):")
        system_parts.append(mem["summary"])
        system_parts.append("")
    if sess_status.startswith("resumed"):
        system_parts.append(f"NOTE: User vừa quay lại sau idle ({sess_status}). Có thể greet ngắn nếu phù hợp.")
        system_parts.append("")
    system_parts += [
        "# RESPONSE GUIDELINES:",
        "- Trả lời <300 từ.",
        "- Dùng emoji vừa phải. KHÔNG markdown asterisks/underscores. KHÔNG HTML tags.",
        "- Khi propose action, đưa concrete numbers và reasoning.",
        "- Khi user hỏi confused, ask clarifying question.",
        "- Tham chiếu recent_turns + summary để follow-up tự nhiên.",
        "- Nếu retrieved section không đủ trả lời, gợi ý slash command phù hợp.",
    ]
    system = "\n".join(system_parts)
    if retrieved:
        print(f"[knowledge] retrieved sections: {retrieved}")

    msgs = [{"role": "system", "content": system}]
    for t in mem.get("recent_turns", []):
        msgs.append({"role": t["role"], "content": t["content"]})
    msgs.append({"role": "user", "content": user_msg})

    response = deepseek_chat(msgs, max_tokens=600)

    if not response.startswith("❌"):
        append_turn("user", user_msg)
        append_turn("assistant", response)
    return response


# =============================================================================
# Memory management commands
# =============================================================================

def cmd_memory() -> str:
    mem = load_memory()
    sess = get_session_status()
    summary = mem.get("summary") or "(empty)"
    recent = mem.get("recent_turns", [])
    lines = [
        b("🧠 Conversation Memory"),
        f"Session: {html_escape(sess)}",
        f"Total turns: {mem.get('total_turns', 0)}",
        f"Recent turns kept: {len(recent)}/{SUMMARIZE_TRIGGER}",
        f"Summary length: {len(summary)} chars",
        "",
        b("Summary preview:"),
        html_escape(summary[:500] + ("..." if len(summary) > 500 else "")),
        "",
        b("Last 3 turns:"),
    ]
    for t in recent[-3:]:
        role = "👤" if t["role"] == "user" else "🤖"
        lines.append(f"{role} {html_escape(t['content'][:120])}")
    lines.append("")
    lines.append(i("Use /forget to wipe memory."))
    return "\n".join(lines)


def cmd_forget() -> str:
    mem = load_memory()
    n = len(mem.get("recent_turns", []))
    has_summary = bool(mem.get("summary"))
    reset_memory()
    return ("🧹 " + b("Memory wiped") +
            f"\n\nCleared {n} recent turns" +
            (" + summary" if has_summary else "") +
            "\n\n" + i("Next chat starts fresh (system context vẫn có, history thì không)."))


def cmd_knowledge() -> str:
    """Inspect knowledge base sections."""
    try:
        import knowledge_loader as kl
        secs = kl.all_sections()
    except Exception as e:
        return f"❌ Knowledge load err: {e}"
    if not secs:
        return "❌ KNOWLEDGE.md not found or empty"
    lines = [b("📚 Knowledge Base")]
    base_set = set(kl.BASE_SECTIONS)
    total = 0
    for name, body in secs.items():
        chars = len(body)
        total += chars
        tag = " (always)" if name in base_set else " (on-demand)"
        lines.append(f"  • {html_escape(name)}: {chars} chars{tag}")
    lines.append("")
    lines.append(f"Total: {total} chars (~{total//4} tokens)")
    lines.append(i("Bot dùng base sections + retrieve query-relevant sections."))
    return "\n".join(lines)


# =============================================================================
# Commands registry (must be after all cmd_* defs)
# =============================================================================

COMMANDS = {
    "/help": cmd_help, "/start": cmd_help,
    "/status": cmd_status,
    "/wallet": cmd_wallet,
    "/asi": cmd_asi,
    "/positions": cmd_positions,
    "/signals": cmd_signals,
    "/grids": cmd_grids,
    "/pause": cmd_pause,
    "/resume": cmd_resume,
    "/tradectrl": cmd_tradectrl,
    "/briefing": cmd_briefing,
    "/memory": cmd_memory,
    "/forget": cmd_forget,
    "/knowledge": cmd_knowledge,
    "/kb": cmd_knowledge,
}


# =============================================================================
# Message handler
# =============================================================================

def handle_message(msg: dict):
    text = (msg.get("text") or "").strip()
    msg_id = msg.get("message_id")
    chat_id = str(msg.get("chat", {}).get("id"))
    expected = os.environ.get("TELEGRAM_CHAT_ID")
    if chat_id != expected:
        print(f"[skip] foreign chat_id={chat_id} (expect {expected})")
        return

    if not text:
        return

    print(f"[msg] {text[:60]}")

    # Slash command?
    cmd = text.split()[0].lower() if text.startswith("/") else None
    if cmd in COMMANDS:
        try:
            response = COMMANDS[cmd]()
        except Exception as e:
            response = f"❌ Cmd err: {e}\n{html_escape(traceback.format_exc()[:300])}"
        tg_send(response, reply_to=msg_id)
        return

    if cmd:
        tg_send(f"❓ Unknown command {code(cmd)}\nUse /help to see available commands.", reply_to=msg_id)
        return

    # Free text → DeepSeek
    response = free_text_response(text)
    # Plain text (no HTML formatting from LLM output for safety)
    tg_send(response, parse_mode=None, reply_to=msg_id)


# =============================================================================
# Main loop
# =============================================================================

def daemon():
    state = load_state()
    offset = state.get("last_update_id", 0) + 1
    print(f"[daemon] Telegram bridge started, offset={offset}, chat_id={os.environ.get('TELEGRAM_CHAT_ID')}")
    tg_send(b("🚀 OpenClaw AI Bot online") + "\n\n" + i("Use /help to see commands"), parse_mode="HTML")

    while True:
        try:
            updates = tg_get_updates(offset=offset)
            if not updates.get("ok"):
                print(f"[err] {updates}")
                time.sleep(5)
                continue
            for u in updates.get("result", []):
                offset = u["update_id"] + 1
                if "message" in u:
                    try:
                        handle_message(u["message"])
                    except Exception as e:
                        print(f"[handler err] {e}\n{traceback.format_exc()}")
                        try: tg_send(f"❌ Internal error: {html_escape(e)}")
                        except Exception: pass
            state["last_update_id"] = offset - 1
            save_state(state)
        except KeyboardInterrupt:
            print("[stop]")
            break
        except Exception as e:
            print(f"[loop err] {e}")
            time.sleep(10)


def main():
    load_env()
    p = argparse.ArgumentParser()
    p.add_argument("--test", help="Test single command/text and print response")
    args = p.parse_args()

    if args.test:
        text = args.test
        if text.startswith("/"):
            cmd = text.split()[0].lower()
            if cmd in COMMANDS:
                print(COMMANDS[cmd]())
                return 0
            print(f"Unknown {cmd}")
            return 1
        print(free_text_response(text))
        return 0

    daemon()
    return 0


if __name__ == "__main__":
    sys.exit(main())
