# OpenClaw Knowledge Base

**Ground truth for the OpenClaw AI bot. Updated when system changes.**
Last updated: 2026-05-08

---

## SECTION:CORE_IDENTITY

OpenClaw is a self-funding AI crypto trading agent. Goal: profit ≥ 2× LLM
infrastructure cost (ASI ≥ 2.0). Style: conservative, patient, evidence-based.
Owner: Sư (1 user).

---

## SECTION:PORTFOLIO_LAYERS

4-layer Multi-Layer Portfolio (after May-2026 rebalance):

1. HODL Core (Binance Earn) — long-term BTC/ETH/BNB stake. Yield ~0.5% APY.
2. Grid Yield (Spot Grid bots, 4 active) — sideways-market profit.
3. Active Futures (OpenClaw 11-coin allowlist) — directional trading 20x leverage default.
4. Reserve (Spot USDT) — emergency capital + opportunity buffer.

Capital flow: Salary → Reserve → top up other layers based on monthly review.
Profit flow: Futures profit → reinvest into Grid + Reserve. 1st of month split.

---

## SECTION:FUTURES_ALLOWLIST

11-coin allowlist (verified Futures contracts on fapi.binance.com):
AAVE, ETH, LINK, BNB, XRP, BTC, TRX, INJ, ORDI, ATOM, ENA

History:
- v1 (initial 6): AAVE, ETH, LINK, BNB, XRP, BTC
- v2 (May 2026): added TRX, INJ, ORDI, ATOM, ENA after backtest 30d/90d
- REMOVED: MKR (no Futures contract), RNDR (rebranded to RENDER, mismatch)

Override via env: COIN_ALLOWLIST="aave,eth,..."

---

## SECTION:GRID_BOTS

4 Spot Grid bots (active, started ~2026-04 after closing legacy 8 bots):

| Symbol | Range | Grids | Invested |
|---|---|---|---|
| AAVEUSDT | $82 - $118 | 60 | $450 |
| DOTUSDT  | $1.10 - $1.50 | 50 | $400 |
| XRPUSDT  | $1.25 - $1.55 | 40 | $350 |
| AVAXUSDT | $8.50 - $10.50 | 35 | $300 |

Total invested: $1500. Expected ROI: 1.5-4%/mo realistic (NOT 25%/mo as
initially claimed — that was over-optimistic).

Grid warmup period: 7 days (`self_sustainability.WARMUP_DAYS`). Before that,
grid P&L is not projected to monthly run-rate (slippage skews it).

Legacy 8 bots (BTC/ETH/BNB) were closed 2026-04 after -$279 loss. Lesson:
grids only profitable in true sideways markets, not in trending markets.

---

## SECTION:RISK_SHIELDS

Three independent risk shields (NOT daily-loss / consecutive-loss / ASI):

Shield 1 — Per-coin health auto-suspend (`coin_health_monitor.py`)
- Tracks recent win-rate per coin
- If a coin has 3 losses in a row OR win-rate < 30% over last 10 trades,
  auto-suspends signals for that coin for 24h
- State: `data/coin_suspensions.json`

Shield 2 — Regime drift detector (`regime_drift_detector.py`)
- Watches BTC 1h/4h trend regime (uptrend / downtrend / chop)
- Alerts if regime flips abruptly (e.g., uptrend → chop within 4 hours)
- State: `data/regime_drift_state.json`

Shield 3 — Coin tiering in LLM prompt (`prompt_registry.py`)
- LLM prompt explicitly tags each coin as TIER_A (BTC/ETH/BNB), TIER_B
  (AAVE/LINK/XRP/etc), TIER_C (smaller alts) so LLM can adjust confidence
- TIER_C coins require higher confidence to confirm.

Other safety (NOT shields, but circuit breakers):
- `risk_guardian.py`: drawdown limit 15%, consecutive losses limit 3
- `trading_control.json`: auto-trade ON/OFF, max_daily_loss=$25
- `binance_api_health.py`: API connectivity monitor

---

## SECTION:TRAILING_SL_TIERS

Mode-aware Trailing SL tiers (`position_manager.py:TRAIL_TIERS`):

ATR-based, profit-tier ladder (LONG; SHORT is mirrored):

| Profit (ATR mult) | SL position | Tier name |
|---|---|---|
| ≥ 5.0 ATR | current ± 0.8 ATR (CHASE) | CHASE_TIGHT |
| ≥ 3.0 ATR | current ± 1.5 ATR (CHASE) | CHASE_WIDE |
| ≥ 2.5 ATR | entry ± 1.5 ATR | TRAIL_3 |
| ≥ 2.0 ATR | entry ± 1.0 ATR | TRAIL_2 |
| ≥ 1.5 ATR | entry ± 0.5 ATR | TRAIL_1 |
| ≥ 1.0 ATR | entry + fee buffer (BREAKEVEN) | BREAKEVEN |
| ≥ 0.7 ATR | entry - 0.5 ATR (EARLY_LOCK) | EARLY_LOCK |

CHASE tiers anchor to CURRENT price (continuous trail).
TRAIL/BREAKEVEN/EARLY_LOCK anchor to ENTRY (lock partial profit).

EARLY_LOCK added in Gap 1 (2026-05-05) to reduce "no man's land" risk.
CHASE_TIGHT tightened in Gap 3 (was 4 ATR / 1.0 ATR).

---

## SECTION:RISK_PARAMS

Position sizing (`binance_price_alert.py`):
- RISK_PER_TRADE_PCT = 3.0% (raised from 2% on 2026-05-05 after WR 69% over 12d/13t)
- MAX_PORTFOLIO_RISK_PCT = 12.0% (allows 4 concurrent positions)
- Default leverage: 20x
- MIN_VOLUME_USD = $10M (filters out illiquid coins)
- VOL_REGIME_MAX_PCT = 2.5% (ATR/price cap)

Breakout Off-Allowlist mode (added 2026-05-10):
- Env BREAKOUT_OFFLIST=1 enables explosive burst signals on coins NOT in
  the 11-coin allowlist (scans full top-20 by volume).
- BREAKOUT_RISK_PCT = 1.5% (half of standard)
- BREAKOUT_SL_ATR_MULT = 0.6 (vs 0.8 for allowlist breakouts)
- BREAKOUT_TP_ATR_MULT = 2.0 (vs 2.5 for allowlist breakouts)
- BREAKOUT_VOL_REGIME_MAX_PCT = 5.0% (vs 2.5%, since high vol IS the signal)
- Standard EMA-cross signals are STILL allowlist-only (only explosive bursts)
- LLM gets stricter review prompt with mode_hint=BREAKOUT_OFFLIST

Probe Trade mode (added 2026-05-10):
- Env PROBE_TRADE=1 enables tiny "probe" trades on untested/thin-history coins.
- Goal: build trade outcome dataset for RAG memory + decision learning.
- Tier classification (auto, from decisions.db trades count per coin):
  - OFFLIST_UNTESTED (0 trades): probe risk 1.0%, mode_hint=BREAKOUT_PROBE
  - OFFLIST_THIN (1-3 trades): probe risk 1.0%, mode_hint=BREAKOUT_PROBE
  - OFFLIST_ESTABLISHED (>=4 trades): risk 1.5%, mode_hint=BREAKOUT_OFFLIST
  - ALLOWLIST: risk 3.0%, mode_hint=BREAKOUT
- PROBE_GRADUATION_TRADES = 4 (after 4 closed trades, coin auto-graduates)
- PROBE_DAILY_CAP_PER_COIN = 1 (max 1 probe per coin per 24h, anti-spam in chop)
- LLM prompt for PROBE: bias toward CONFIRM (data acquisition is worth $2-3),
  reject only on extreme RSI / fading vol / strong 4h conflict.

RSI thresholds:
- RSI_OVERBOUGHT = 70, RSI_OVERSOLD = 30
- RSI_LONG_MIN = 45 (need momentum for long)
- RSI_SHORT_MAX = 55 (need momentum for short)
- RSI_MOMENTUM_DELTA = 3

Trading control (`data/workspace-finance/trading_control.json`):
- auto_trade_enabled (true/false)
- max_daily_loss = $25 (raised from $10 after capital +$200)
- emergency_close (manual flag)

---

## SECTION:ASI_FORMULA

ASI = monthly_profit / monthly_LLM_cost

Monthly profit components (`self_sustainability.py`):
- Futures: executor_state.total_pnl / days_active × 30
- Grid: (current_trading_bots_wallet - sum_invested) projected to 30d, after 7d warmup
- Earn: balance × 0.5% APY / 12

Monthly cost components:
- DeepSeek: cumulative_spend / days × 30
- Cursor Pro: $20/mo (flat)
- Anthropic: $0 (not used directly outside Cursor)

Status bands:
- < 1.0  → DEFICIT (burning more than earning)
- 1.0-1.5 → BREAK_EVEN
- 1.5-2.0 → SURPLUS
- ≥ 2.0  → SELF_SUSTAINING_PLUS

---

## SECTION:LESSONS_LEARNED

Top lessons (chronological, key learnings from real failures):

1. **Binance API IP whitelist (April 2026)**
   - Symptom: API auth fails silently, signals stop
   - Cause: Binance auto-rotated IP, our key wasn't whitelisted for new range
   - Fix: Built `binance_api_health.py` to monitor and alert. Use IP-restricted
     keys carefully.

2. **Grid Bot ROI over-optimism**
   - Initial estimate: 25-30%/mo. Reality: 1.5-4%/mo.
   - Fix: Anchor grid P&L to `grid_config.invested_usd`, not raw wallet delta.
     Add 7-day warmup before projecting monthly run-rate.

3. **Spot vs Futures backtest mismatch**
   - Symptom: MKR backtest looked great, but no Futures contract exists.
     RNDR backtest used Spot prices, but Futures uses RENDER (rebranded).
   - Cause: backtest uses api.binance.com (Spot), live trades use fapi (Futures).
   - Fix: Verify Futures listing for every candidate before adding to allowlist.
     TODO: integrate `verify_futures_listed()` into `backtest_candidates.py`.

4. **Dead market = no signals (May 2026)**
   - Symptom: Optimized filters → no Futures signals for days.
   - Cause: Low volume + tight EMA gaps + neutral RSI = "dead market".
   - Fix: Confirmed filters working AS INTENDED. Patience > forcing trades.

5. **Telegram Markdown parsing failures**
   - Symptom: messages from morning_briefing fail to send
   - Cause: underscores/asterisks in LLM output break Markdown
   - Fix: Use HTML parse_mode + html_escape; strip markdown chars from LLM
     output.

6. **Stateless Telegram bridge (May 2026, fixed same day)**
   - Symptom: bot didn't remember previous chats, felt impersonal
   - Cause: free-text handler was stateless by design (cost concern)
   - Fix: Added smart memory (12 recent + DeepSeek summary). +$0.5/mo cost.

7. **Bot hallucinating system facts (May 2026, this fix)**
   - Symptom: when asked about allowlist, shields, lessons → bot bịa
   - Cause: system prompt only had 4 lines; bot used pretraining knowledge
   - Fix: This KNOWLEDGE.md + dynamic retrieval into prompt.

8. **Allowlist too strict, missed breakouts on alts (May 2026)**
   - Symptom: User went manual on CHIP/ONDO/SAHARA breakouts (off-allowlist).
     CHIP/ONDO winners +$13, but follow-up SLs lost $-21. Risk Guardian
     auto-paused the system.
   - Cause: COIN_ALLOWLIST + VOL_REGIME_MAX_PCT 2.5% blocked all alt breakouts.
     CHIP was rank-5 by volume ($248M) but never considered.
   - Fix: Added BREAKOUT_OFFLIST mode — explosive burst path now scans full
     top-20 universe with tighter risk (1.5%) and stricter SL/TP. Standard
     EMA-cross signals still allowlist-only.
   - Lesson: USER manual chasing burns counter; system catching the same
     signal applies risk discipline (smaller position, tighter SL, time exit).

9. **Telegram bot conflict (May 2026)**
   - Symptom: 2 bots (Docker openclaw + external telegram_bridge.service)
     polled same token → 409 Conflict every 30s.
   - Fix: Disabled external telegram_bridge.service. Docker openclaw owns
     Telegram. (KNOWLEDGE.md and knowledge_loader.py remain for future use.)

10. **Cold-start dataset problem (May 2026)**
    - Symptom: RAG memory + decision_logger had rich data only for 11 allowlist
      coins. New coins (CHIP, SAHARA, ONDO, etc.) had 0-3 trades each, so LLM
      review couldn't draw on similar past trades.
    - Fix: Added PROBE_TRADE mode — small 1.0% risk trades on
      untested/thin-history coins to build dataset. Auto-graduates to standard
      breakout off-list (1.5%) after 4 closed trades.
    - Trade-off: Accept ~$2-3 expected loss per probe in exchange for data
      that improves future signal quality on these coins.

---

## SECTION:OPERATIONAL_PLAYBOOK

Daily routine:
- 08:00 ICT: morning_briefing arrives (rule-based, $0)
- 20:00 ICT: grid_daily_report arrives (Python, $0)
- 21:00 ICT: FinanceBot OpenClaw daily PnL report (Claude via cron)

Weekly routine:
- Sunday 19:00 ICT: weekly_analysis (DeepSeek strategic review, ~$0.05)
- Sunday 21:00 ICT: LLM weekly review (OpenClaw cron)

Monthly routine (1st of month):
- Strategy review (`monthly_strategy_review.py`)
- Profit/withdrawal split (`monthly_profit_split.py`)
- Capital tier scaling proposal (`capital_scaling.py`)

Anytime user can:
- Type `/status`, `/asi`, `/wallet`, `/positions`, `/grids`, `/signals` etc.
- Type `/pause` to halt auto-trade, `/resume` to re-enable
- Type `/memory` to inspect chat memory, `/forget` to wipe
- Free-text chat → DeepSeek with full context (this knowledge + live state + memory)

---

## SECTION:FILE_PATHS

Key state files (all in `data/` unless noted):
- `executor_state.json` — Futures trade history + total_pnl + starting_balance ($293.62)
- `trading_state.json` — active positions
- `wallet_balance_history.json` — multi-wallet snapshots (every 30min)
- `grid_config.json` — grid bot configs (lower/upper/grids/invested_usd)
- `coin_suspensions.json` — Shield 1 state
- `regime_drift_state.json` — Shield 2 state
- `decisions.db` — SQLite of all LLM decisions (signal review)
- `deepseek_cost_state.json` — DeepSeek API spend tracking
- `pending_signal.json` — latest signal awaiting LLM review
- `telegram_memory.json` — bot conversation memory (this file's purpose)
- `workspace-finance/trading_control.json` — auto-trade enable + thresholds
- `workspace-finance/CLAUDE.md` — FinanceBot agent instructions

Code modules (in repo root):
- `binance_price_alert.py` — signal generator daemon
- `trade_executor.py` — places orders on Binance
- `position_manager.py` — TSL + partial close + time-exit
- `binance_reconcile.py` — sync state with exchange
- `dashboard.py` — Flask UI on port 8686
- `telegram_bridge.py` — Telegram chat daemon (this bot)
- `self_sustainability.py` — ASI calculator
- `wallet_tracker.py` — wallet balance snapshots
- `MASTER_ARCHITECTURE.md` — full system doc

---

## SECTION:COMMON_QUESTIONS

Q: "Tại sao chọn coin X?"
A: Coin must be in 11-coin allowlist. Allowlist chosen by 30d+90d backtest with
   filter: Sharpe > 1.0, max_drawdown < 15%, win_rate > 55%, Futures contract
   verified on fapi.binance.com. See SECTION:FUTURES_ALLOWLIST history.

Q: "Tại sao không có lệnh?"
A: Likely "dead market" — low volume / tight EMAs / neutral RSI. Filters working
   as intended. See LESSONS_LEARNED #4.

Q: "ASI bao nhiêu là tốt?"
A: ≥ 2.0 = self-sustaining + reinvest budget. 1.5-2.0 = surplus but slow growth.
   < 1.0 = burning capital on LLM costs. See SECTION:ASI_FORMULA.

Q: "Có nên tăng leverage?"
A: Default 20x. Higher leverage doesn't improve EV, only amplifies variance.
   Stick with 20x unless backtest shows strict improvement at sample size > 100.

Q: "Grid bot ROI thực tế?"
A: 1.5-4%/mo realistic. Initial 25%/mo claim was wrong. See LESSONS #2.

---

## SECTION:CONSTRAINTS

What OpenClaw bot must NOT do:
- Place orders directly (only `trade_executor.py` via auto-trade pipeline)
- Modify `trading_control.json` except via `/pause` and `/resume`
- Promise specific returns (use ranges with confidence levels)
- Use markdown asterisks/underscores in Telegram (breaks parsing)
- Hallucinate facts — if unsure, say "không chắc, kiểm tra `/grids` hoặc `/asi`"

What user must NOT expect:
- Bot to remember every detail across `/forget`
- Profit guarantees
- 24/7 perfect uptime (single-server, single-IP)
