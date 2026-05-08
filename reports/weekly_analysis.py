#!/usr/bin/env python3
"""
Weekly Analysis — DeepSeek-powered strategic review.

Runs Sunday 19:00 ICT. Collects week's data, sends to DeepSeek for insights,
posts to Telegram.

Cost: ~$0.05 per call × 4-5 calls/month = ~$0.25/mo

Reads:
  - Trade history last 7d (executor_state.json)
  - Wallet snapshots last 7d (wallet_balance_history.json)
  - Grid bot config + wallet (grid_config.json)
  - ASI calculator
  - LLM decisions stats (decisions.db)
"""
from __future__ import annotations
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT))

ENV_FILE = ROOT / ".env"
DEEPSEEK_API = "https://api.deepseek.com/v1/chat/completions"


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


def send_telegram(text: str, parse_mode: str = "HTML"):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[WARN] no Telegram creds")
        return
    body = {"chat_id": chat_id, "text": text}
    if parse_mode: body["parse_mode"] = parse_mode
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r: return json.loads(r.read())
    except Exception as e:
        if parse_mode:
            return send_telegram(text, parse_mode=None)
        print(f"[ERR] {e}")


def deepseek_chat(prompt: str, system: str = "") -> str:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return "[ERR] No DEEPSEEK_API_KEY"
    messages = []
    if system: messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = {
        "model": "deepseek-chat",
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 1500,
    }
    req = urllib.request.Request(
        DEEPSEEK_API,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
        return d["choices"][0]["message"]["content"]
    except Exception as e:
        return f"[DeepSeek ERR] {e}"


def collect_week_data() -> dict:
    """Aggregate everything that happened in last 7 days."""
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    out = {"week_start": week_ago.isoformat(timespec="seconds"),
           "week_end": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    # Executor stats
    state = safe_load(DATA / "executor_state.json") or {}
    history = state.get("trade_history", [])
    week_trades = []
    for t in history:
        try:
            ts = datetime.fromisoformat(t.get("time", "").replace("Z","+00:00"))
            if ts >= week_ago:
                week_trades.append(t)
        except Exception: pass

    wins = [t for t in week_trades if t.get("pnl", 0) > 0]
    losses = [t for t in week_trades if t.get("pnl", 0) <= 0]
    out["futures"] = {
        "trades": len(week_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(week_trades) * 100) if week_trades else 0,
        "total_pnl": sum(t.get("pnl", 0) for t in week_trades),
        "avg_win": (sum(t.get("pnl", 0) for t in wins) / len(wins)) if wins else 0,
        "avg_loss": (sum(t.get("pnl", 0) for t in losses) / len(losses)) if losses else 0,
        "by_coin": {},
    }
    for t in week_trades:
        c = t.get("coin", "?").upper()
        if c not in out["futures"]["by_coin"]:
            out["futures"]["by_coin"][c] = {"n": 0, "pnl": 0, "wins": 0}
        out["futures"]["by_coin"][c]["n"] += 1
        out["futures"]["by_coin"][c]["pnl"] += t.get("pnl", 0)
        if t.get("pnl", 0) > 0:
            out["futures"]["by_coin"][c]["wins"] += 1

    # Wallet evolution
    wallet_h = safe_load(DATA / "wallet_balance_history.json") or {}
    snaps = wallet_h.get("snapshots", [])
    week_snaps = []
    for s in snaps:
        try:
            ts = datetime.fromisoformat(s["ts"].replace("Z","+00:00"))
            if ts >= week_ago:
                week_snaps.append(s)
        except Exception: pass
    if len(week_snaps) >= 2:
        first = week_snaps[0]; last = week_snaps[-1]
        out["wallet"] = {
            "first_total": first.get("total", 0),
            "last_total": last.get("total", 0),
            "delta": last.get("total", 0) - first.get("total", 0),
            "first_grid_bots": first.get("wallets", {}).get("Trading Bots", 0),
            "last_grid_bots": last.get("wallets", {}).get("Trading Bots", 0),
            "snapshots": len(week_snaps),
        }
    elif snaps:
        last = snaps[-1]
        out["wallet"] = {
            "last_total": last.get("total", 0),
            "snapshots": len(week_snaps),
            "note": "Insufficient week-over-week snapshots",
        }

    # Grid config
    grid_cfg = safe_load(DATA / "grid_config.json") or {}
    grids = []
    for sym, c in grid_cfg.items():
        if sym.startswith("_") or not isinstance(c, dict): continue
        grids.append({
            "symbol": sym,
            "invested": c.get("invested_usd", 0),
            "range": [c.get("lower"), c.get("upper")],
            "started": c.get("started_at"),
        })
    out["grids"] = grids

    # ASI
    try:
        import self_sustainability
        asi = self_sustainability.compute_asi()
        out["asi"] = {
            "score": asi["asi"], "label": asi["label"],
            "profit_monthly": asi["profit_monthly"],
            "cost_monthly": asi["cost_monthly"],
            "net_monthly": asi["net_monthly"],
        }
    except Exception as e:
        out["asi"] = {"error": str(e)}

    # LLM decisions stats
    db = DATA / "decisions.db"
    if db.exists():
        try:
            conn = sqlite3.connect(db)
            cur = conn.cursor()
            cur.execute("SELECT decision, COUNT(*) FROM llm_decisions WHERE ts >= ? GROUP BY decision",
                        (week_ago.isoformat(),))
            decisions = dict(cur.fetchall())
            cur.execute("SELECT coin, COUNT(*) FROM llm_decisions WHERE ts >= ? AND decision='CONFIRM' GROUP BY coin",
                        (week_ago.isoformat(),))
            confirms_per_coin = dict(cur.fetchall())
            conn.close()
            out["llm"] = {"decisions": decisions, "confirms_per_coin": confirms_per_coin}
        except Exception as e:
            out["llm"] = {"error": str(e)}

    return out


def build_prompt(data: dict) -> str:
    """Build prompt for DeepSeek."""
    return f"""Bạn là Trading Strategist senior phân tích portfolio crypto của một AI agent self-funding.

CONTEXT:
Hệ thống là Multi-Layer Portfolio gồm 4 layers:
1. HODL Core (Earn) - passive growth
2. Grid Yield (Spot Grid bots: AAVE, DOT, XRP, AVAX, $1500)
3. Active Futures (OpenClaw AI bot) - 3% risk/trade, 11-coin allowlist
4. Reserve (Spot USDT)

Mục tiêu cuối: ASI ≥ 2.0 (profit/cost ratio) để hệ thống tự nuôi sống được chi phí AI infra.

TUẦN VỪA RỒI (data JSON):
{json.dumps(data, indent=2, default=str)}

Hãy phân tích NGẮN GỌN (max 12 dòng) và trả lời:

1. TUẦN NÀY THẾ NÀO? (1 câu summary - tốt/bình thường/yếu)
2. ĐIỂM SÁNG: Best performer (coin/layer)
3. ĐIỂM YẾU: Vấn đề lớn nhất tuần
4. ROOT CAUSE: Nguyên nhân chính của vấn đề
5. ACTION RECOMMENDATIONS (max 3, ưu tiên impact cao):
   - Action 1: [cụ thể] → impact dự đoán
   - Action 2: ...
6. RISK FLAGS: Có gì cần watch out tuần tới không?

Trả lời tiếng Việt, dùng emoji, format dễ đọc cho Telegram. KHÔNG dùng markdown asterisks/underscores. KHÔNG explain quá dài.
"""


def main():
    load_env()
    print("[1/3] Collecting week data...")
    data = collect_week_data()

    print("[2/3] Calling DeepSeek for analysis...")
    prompt = build_prompt(data)
    analysis = deepseek_chat(prompt, system="Bạn là chuyên gia phân tích trading crypto, response ngắn gọn và actionable.")

    print("[3/3] Posting to Telegram...")
    now = datetime.now()
    header = f"📊 <b>WEEKLY ANALYSIS — {now.strftime('%a %d/%m/%Y')}</b>\n"
    header += f"<i>(7 days ending {now.strftime('%H:%M ICT')})</i>\n\n"
    full = header + analysis
    if len(full) > 4000:
        full = full[:3990] + "..."

    result = send_telegram(full)
    if result and result.get("ok"):
        print("[ok] Weekly analysis sent")
    else:
        print(f"[err] {result}")
    print()
    print("=== Analysis preview ===")
    print(analysis[:500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
