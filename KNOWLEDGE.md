# OpenClaw Knowledge Base

**Ground truth for the OpenClaw AI bot. Updated when system changes.**
Last updated: 2026-06-02

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

Spot Grid bots (started 2026-05-07 after closing legacy 8 bots):

| Symbol | Range | Grids | Invested | Status |
|---|---|---|---|---|
| AAVEUSDT | $82 - $118 | 60 | $450 | **CLOSED 2026-06-02** (stop hit, -$62) |
| DOTUSDT  | $1.10 - $1.50 | 50 | $400 | active |
| XRPUSDT  | $1.25 - $1.55 | 40 | $350 | active (below range) |
| AVAXUSDT | $8.50 - $10.50 | 35 | $300 | active (below range) |

Initial total invested: $1500. Expected ROI: 1.5-4%/mo realistic (NOT 25%/mo as
initially claimed — that was over-optimistic).

Grid warmup period: 7 days (`self_sustainability.WARMUP_DAYS`). Before that,
grid P&L is not projected to monthly run-rate (slippage skews it).

### REGIME-GATED GRID DEPLOYMENT (rule, 2026-06-02)

**Grid bots are a SIDEWAYS-market tool only.** Decision to run/deploy a grid
MUST be gated on BTC regime, NOT on coin selection:

| BTC 7d regime | Grid action |
|---|---|
| DOWNTREND (< -5%) | **DO NOT deploy new grids.** Park free capital in Earn. |
| SIDEWAYS (-5% to +5%) | Grid bots OK — this is their ideal regime. |
| UPTREND (> +5%) | Grids underperform HODL; prefer HODL/Futures-long. |

Anti-pattern to AVOID: "cut losing grid → rotate into a new grid → it also
loses → cut again". During a market-wide downtrend ALL coins fall together;
rotating coins just repeats buy-high-sell-low. The problem is regime, not coin.

Enforcement: `regime_drift_detector.py::check_grid_regime_gate()` runs every 6h,
writes `grid_gate` block into `data/regime_state.json`, and sends a Telegram
alert when (a) BTC DOWNTREND while bots are below range, or (b) any bot is
within 2% of its stop. `deploy_ok=false` means do not open new grids.

Legacy 8 bots (BTC/ETH/BNB) were closed 2026-04 after -$279 loss. AAVE grid
auto-stopped 2026-06-02 at -$62 during BTC -8% week. Both confirm the same
lesson: grids only profit in true sideways markets, never in trending markets.

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
- Volatility regime cap (ATR/price) — TIERED (fixed 2026-06-04):
  - chop / NEUTRAL allowlist: VOL_REGIME_MAX_PCT = 2.5% (noise-safe)
  - confirmed trend allowlist: TREND_VOL_REGIME_MAX_PCT = 8.0% (directional
    vol = momentum, ride multi-day grinds; risk capped by ATR position sizing)
  - off-allowlist breakout: BREAKOUT_VOL_REGIME_MAX_PCT = 5.0%
  - explosive burst: EXPLOSIVE_VOL_REGIME_MAX_PCT = 12.0%
  - Was a single 2.5%/5% cap that ran BEFORE the explosive/breakout paths and
    silently skipped high-vol pumps (ENA +33%, WLD +42% in June 2026). See
    LESSONS #13.

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

Pullback Re-Entry mode (added 2026-05-10):
- Env PULLBACK_REENTRY=1 enables a second-chance entry path.
- Trigger: when an EXPLOSIVE breakout signal is REJECTED by LLM with reason
  containing RSI keywords (extreme/exhaustion/overbought/oversold), the coin
  is added to data/pullback_watch.json with 90-min expiry.
- Fire conditions (all must pass on a later cycle):
  - Price has dropped >= PULLBACK_MIN_DROP_ATR (1.0× ATR) from rejected entry
  - RSI has cooled by >= PULLBACK_MIN_RSI_DELTA (8 pts)
  - Vol ratio normalised <= PULLBACK_MAX_VOL_RATIO (2.0x — FOMO faded)
  - 4h EMA trend still aligned with original direction
- Risk on fire: tight SL = PULLBACK_SL_ATR_MULT (0.5× ATR), TP = 2.5× ATR,
  R:R = 5.0. Risk uses tier classification (probe 1% if untested).
- LLM gets PULLBACK_REENTRY-specific prompt explaining the original reject
  and the cooled-down setup; biased to CONFIRM if RSI back to healthy zone.
- Real-world motivation (2026-05-10): user manually re-entered TAO LONG at
  $324 after the system rejected the breakout at $329 for RSI 78. Price had
  pulled back from $329 high, RSI cooled, then ran to $338. This mode
  systematizes that pattern.
- Each coin can only fire once per watch cycle (fired flag prevents dup).

Downtrend Continuation Short mode (added 2026-06-02, default OFF):
- Env DOWNTREND_SHORT=1 enables a 2nd short path: "sell the rally into
  resistance" in an established downtrend. Standard short rules miss this
  because they need an RSI cross-down from overbought + volume spike, which
  never fires during a slow grind-down (e.g. BTC -10%/week, low volume).
- Fire conditions (allowlist-only, half size 1.5%):
  - 4h EMA cross = BEARISH (regime confirmed)
  - 4h EMA gap >= 1.5% (DT_SHORT_MIN_GAP4_PCT — STRONG downtrend, not chop)
  - 1h EMA bearish + 1h gap >= 1.0% (DT_SHORT_MIN_GAP1_PCT)
  - RSI 40-65 (selling a bounce, NOT shorting into oversold)
  - vol_ratio >= 1.2 (volume confirmation REQUIRED)
  - price within 1.5% of EMA20 (at resistance) + RSI rolling over
  - SL 1.0×ATR, TP 2.0×ATR (R:R 2.0)
- BACKTEST 45d/11-coin sweep (backtest_dt_short.py): the ORIGINAL premise
  ("relax volume to catch low-vol grind shorts") was REFUTED — loose config
  got WR 28% / -62R. Low-volume shorts LOSE. Only a strict config (strong 4h
  gap + volume>=1.2) was +EV: WR 41% / +5R / +0.23R per trade, but small
  sample (N=22). Defaults = validated strict config. Flag stays OFF until
  more live data confirms the thin edge.
- Lesson: backtest BEFORE enabling. This saved deploying a -62R strategy.

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

13. **Volatility cap blocked the explosive path it was meant to feed (June 2026)**
    - Symptom: ENA (+33% range, in allowlist) and WLD (+42%, off-allowlist)
      pumped hard but the engine generated ZERO signals. User: "tại sao window
      miss ENA, WLD tăng 30%?"
    - Cause: a single volatility regime filter (FILTER 2: ATR/price <= 2.5%
      allowlist / 5% off-list) ran BEFORE the explosive-burst and breakout
      paths and did `continue` on any high-vol coin. ENA ATR% was 7.6%, WLD
      9.0% — both skipped before any LONG/explosive logic could evaluate them.
      The filter meant to avoid "extreme bars" was killing exactly the
      explosive moves the breakout path exists to ride.
    - Fix: tiered the cap by context — chop 2.5%, confirmed trend 8%, off-list
      5%, explosive burst 12%. High ATR in a confirmed trend or burst is
      directional momentum, not noise; ATR-based position sizing already caps
      risk. Explosive/trend paths are now reachable on 30%-pump coins.
    - Note: explosive_burst gate still needs a 3x-volume 1.5x-ATR candle, so
      multi-day grind-up pumps are best caught by the trend tier (standard
      EMA-cross LONG), not the burst path.
    - Lesson: filter ORDER matters. A blanket pre-filter can silently starve a
      specialized path downstream. Check the full pipeline, not just the rule.

12. **Discretionary strategies don't survive mechanization (June 2026)**
    - Context: User shared a "Sneaky Pivot" price-action strategy (26yr trader,
      minimalist: 15m chart, prior-day Range High/Low + Swing levels, 3-candle
      reject→confirm pattern, "location > signal", sideways mean-reversion).
    - Tested it: built `backtest_level_reversion.py` (15m, prior-day levels,
      rejection-wick + confirmation-candle entry, BTC-regime gate). 45d/11-coin.
    - Result: BREAKEVEN gross (best +0.007R/trade over 405 trades, WR 32.6% ~=
      RR2 breakeven 33%). After fees it's strongly NEGATIVE: 15m trading =
      ~0.13R/trade in fees -> gross +3R becomes net ~-50R. The sideways regime
      gate added NO edge (sideway +2R == all-regimes +2R).
    - Lesson: the real edge in a discretionary price-action method is the
      trader's contextual judgment (order flow, structure, experience), NOT the
      written rules. Mechanizing "trustworthy candle / wick strength" loses the
      edge. High-frequency mean-reversion (15m) also bleeds fees. Our 1h/4h
      momentum/trend-following does NOT have this fee problem — patience in
      sideways (few signals) beats forcing low-edge 15m reversals.
    - 2nd backtest in one day to PREVENT a losing deploy (after DT_SHORT -62R).
      Process: quantify -> backtest -> reject if no edge. Keeps capital safe.

11. **Grid rotation trap during downtrend (June 2026)**
    - Symptom: Closed 4 legacy coins → opened 4 grid bots (AAVE/DOT/XRP/AVAX).
      26 days later BTC dropped -8%/week, 3 of 4 bots fell below range, AAVE
      auto-stopped at -$62 (-14%). User asked: "we keep cutting losses to
      rotate, is this OK?"
    - Cause: Grid bots only profit in SIDEWAYS markets. In a downtrend they
      keep buying the dip all the way down. Rotating to different coins does
      NOT help — during a market-wide drawdown all coins fall together (beta).
      The problem is regime mismatch, not coin selection.
    - Fix: Regime-gated grid deployment (see SECTION:GRID_BOTS). Added
      `check_grid_regime_gate()` to `regime_drift_detector.py` — blocks new
      grid deployment in DOWNTREND and alerts when bots near stop. In
      downtrend, park capital in Earn and WAIT for sideways regime.
    - Lesson: Don't fix a regime problem by changing coins. Match the tool to
      the market: downtrend→cash, sideways→grid, uptrend→HODL/futures-long.

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
- `pullback_watch.json` — coins flagged for pullback re-entry monitoring
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

Q: "Grid bot đang lỗ, có nên cắt và mở grid coin khác không?"
A: KHÔNG nếu BTC đang DOWNTREND. Grid bot chỉ sinh lời khi thị trường SIDEWAY.
   Trong downtrend mọi coin rớt cùng nhau (beta) — đổi coin chỉ lặp lại
   "mua cao bán thấp". Vấn đề là REGIME, không phải coin. Giải pháp: dừng
   grid, để tiền trong Earn, chờ thị trường đi ngang trở lại. Khi đó mới
   deploy grid. Xem SECTION:GRID_BOTS (regime-gated deployment) + LESSONS #11.

Q: "Khi nào nên deploy grid bot mới?"
A: Chỉ khi BTC 7d regime = SIDEWAYS (-5% đến +5%). DOWNTREND → để tiền trong
   Earn. UPTREND → ưu tiên HODL/Futures-long. Check `grid_gate.deploy_ok`
   trong data/regime_state.json (cập nhật mỗi 6h).

Q: "Hệ thống có biết vào lệnh sau khi giá hồi (pullback)?"
A: Có — Pullback Re-Entry mode. Khi LLM REJECT một explosive breakout vì RSI
   quá cao, coin được watch 90 phút. Nếu giá rớt >=1 ATR + RSI cool >=8 pts +
   vol về <2x + 4h trend còn nguyên → fire PULLBACK_REENTRY signal với SL siêu
   chặt 0.5×ATR và TP 2.5×ATR (R:R 5.0). Inspired by user's TAO trade 2026-05-10.

Q: "Tại sao breakout bị reject mà giá vẫn lên?"
A: Reject thường vì RSI extreme (>72 LONG) — đó là cảnh báo "đỉnh ngắn hạn".
   Hệ thống không đoán sai trend mà chỉ ngại entry tệ. Pullback Re-Entry mode
   sẽ catch lại nếu giá hồi về vùng support — đây là pattern textbook.

Q: "Có nên dùng chiến lược price-action / Sneaky Pivot (level reversion 15m)?"
A: KHÔNG. Đã backtest (backtest_level_reversion.py): breakeven gross
   (+0.007R/lệnh), âm nặng sau phí (15m = ~0.13R phí/lệnh → ~-50R net). Regime
   gate không tạo edge. Edge thật của price-action thủ công nằm ở phán đoán
   trader, không cơ giới hóa được. Xem LESSONS #12.

Q: "Tại sao downtrend mạnh mà không có lệnh short?"
A: Short tiêu chuẩn cần RSI cross xuống từ >70 + volume spike >=1.2x. Trong
   downtrend "grind chậm" (volume thấp) cả hai không fire → miss. Có
   DOWNTREND_SHORT mode (mặc định TẮT) để "bán hồi vào kháng cự", NHƯNG
   backtest cho thấy biên lợi nhuận mỏng (WR 41%, +0.23R, N nhỏ) và short
   volume thấp THUA nặng. Vì vậy chỉ bật khi có thêm data live xác nhận.
   Quan trọng: KHÔNG cố short vào vùng oversold (RSI<35) — đó là vùng dễ bật.

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
