# OpenClaw — Self-Funding AI Trading Agent

> Multi-layer crypto portfolio with autonomous AI trading, monitored 24/7 and managed via Telegram chat.

**Status**: Production · 2026-05-08
**Owner**: Sư (PicoPiece)
**Goal**: AI Self-Sustainability Index (ASI) ≥ 2.0 — i.e. monthly trading profit covers ≥ 2× LLM infrastructure cost.

---

## 0. TL;DR — What this system does

OpenClaw is a fully autonomous trading agent that:

1. **Trades Binance Futures** automatically using a multi-timeframe EMA/RSI/ATR strategy with LLM signal review
2. **Operates Spot Grid bots** for sideways-market yield on selected coins
3. **Manages a multi-layer portfolio** (HODL · Grid · Active · Reserve) with strict risk rules
4. **Monitors itself** via 14 systemd services (price scan, position management, health checks, regime drift, etc.)
5. **Reports to user** via Telegram (proactive) and chat (bidirectional via DeepSeek bridge)
6. **Tracks self-sustainability** (ASI metric) — knows whether profits cover its own AI costs

**Brain stack**: DeepSeek (daily ops, $1-3/mo) + Claude via Cursor (strategy & dev, $20/mo Pro).

---

## 1. Portfolio architecture

### Multi-Layer Portfolio (4 layers)

| Layer | Capital target | Vehicle | Risk profile | Expected return |
|---|--:|---|---|---|
| 💎 **HODL Core** | ~$2,000 | Binance Simple Earn (BTC/ETH/USDT) | Long-term hold | 0.5-1% APY + capital appreciation |
| 🌐 **Grid Yield** | ~$1,500 | 4 Spot Grid bots (AAVE/DOT/XRP/AVAX) | Range-bound, with TP/SL | 1.5-4% per month gross |
| 🤖 **Active Futures** | ~$300 | OpenClaw AI bot, 11-coin allowlist | 3% risk/trade, 12% max DD | 5-10% per month if signals fire |
| 💵 **Reserve** | ~$500 | Spot USDT (auto-subscribed Flex Earn) | Emergency / opportunistic | 0.5% APY |
| **TOTAL** | **~$3,800** | | | **+$40-100/mo expected** |

### Why 4 layers?

- **Grid** = sideways-market yield (uncorrelated to direction)
- **Futures (OpenClaw)** = directional alpha (when signals are clean)
- **HODL** = long-term BTC/ETH/BNB exposure (capital appreciation)
- **Reserve** = liquidity buffer for emergencies / new opportunities

→ When one layer is dead (e.g. no Futures signals in low-vol week), other layers still earn.

---

## 2. Trading strategy (Active Futures / OpenClaw)

### Signal generation (`binance_price_alert.py`)

```
Scan every 60 seconds across 11-coin allowlist:
  AAVE · ETH · LINK · BNB · XRP · BTC · TRX · INJ · ORDI · ATOM · ENA

For each coin, check:
  1. EMA20 vs EMA50 (1h, with 4h MTF confirmation)
  2. EMA gap > 0.10%
  3. Volume ratio > 1.2× (vs 20-bar avg)
  4. ATR/price ≤ 2.5% (volatility regime filter)
  5. RSI not extreme (LONG: 40-65 ideal; SHORT: 35-60 ideal)
  6. Coin not in suspended list (Shield 1)
  7. Explosive breakout detection (Gap 4): 1h range > 1.5× ATR + vol > 3× avg

If all pass → emit signal → LLM review → trade executor.
```

### LLM review (DeepSeek)

For each candidate signal, DeepSeek reviews:
- Coin tier from rolling health (Shield 3)
- Recent win rate per coin / direction (RAG context)
- BTC regime alignment
- Mode classification (SWING / SCALP / QUICK / BREAKOUT)

Output: `CONFIRM` or `REJECT` with reason + confidence score.

### Position management (`position_manager.py`)

```
Trailing Stop Loss tiers (move SL up as profit grows):
  EARLY_LOCK  +0.7 ATR → SL = entry - 0.5 ATR
  BREAKEVEN   +1.0 ATR → SL = entry
  TRAIL_1     +1.5 ATR → SL = entry + 0.5 ATR
  TRAIL_2     +2.0 ATR → SL = entry + 1.0 ATR
  TRAIL_3     +3.0 ATR → SL = entry + 1.5 ATR
  CHASE_TIGHT +5.0 ATR → SL = high - 0.8 ATR
  CHASE_WIDE  +7.0 ATR → SL = high - 1.5 ATR

Auto partial close:
  +2.0 ATR → close 30% (lock profit)
  +3.0 ATR → close another 30%

Time-based exit:
  SCALP    timeout 4h
  QUICK    timeout 8h
  SWING    timeout 24h
  BREAKOUT timeout 12h
  → Profit-aware: don't exit if currently winning
```

### Risk management

| Setting | Value | Why |
|---|--:|---|
| Risk per trade | 3% portfolio | Stable position sizing |
| Max portfolio risk | 12% | Cap on simultaneous open risk |
| Max concurrent positions | 4 | Allowed slots |
| Max daily loss | $25 (8.5% of $293) | Auto-pause trigger |
| Consecutive losses | 3 max | Auto-pause trigger |
| Drawdown limit | 15% from baseline | Catastrophic protection |

---

## 3. Grid Yield strategy (Spot Grid bots)

### Why grids on these 4 coins?

After analyzing 24 candidate coins (30d data), selected based on:
- High daily volatility (ideal for grid fills)
- Low directional bias (mean-reverting)
- Sufficient liquidity
- Tight bid-ask spreads

| Coin | Range | Grids | Invested | Vol 30d |
|---|---|--:|--:|--:|
| **AAVE** | $82-$118 | 60 | $450 | 35% (highest) |
| **DOT** | $1.10-$1.50 | 50 | $400 | 16% |
| **XRP** | $1.25-$1.55 | 40 | $350 | 14% |
| **AVAX** | $8.50-$10.50 | 35 | $300 | 12% |
| **TOTAL** | | **185** | **$1,500** | |

**All bots have**:
- Stop Loss enabled (Lower + Upper)
- "Sell all base on stop" → exit to USDT cleanly
- Manual mode (no Binance AI)
- Arithmetic spacing

### Lessons from -$283 loss (8 old bots, closed 2026-05-07)

Old setup had: BTC/ETH/BNB bots with tight ranges, no SL → got stuck during BTC uptrend, sold base too early, couldn't re-buy. Realized loss: **-$283 (-11.5% of invested)**.

New rules applied:
- ✅ High-volatility coins only (not BTC/ETH for grids)
- ✅ Wide ranges (±15-30%, not ±5%)
- ✅ Stop Loss mandatory on both ends
- ✅ Diversification (4 coins, not concentration)

---

## 4. Monitoring & Safety system

### 14 always-on / scheduled services

| Service | Frequency | Purpose |
|---|---|---|
| `binance-price-alert.service` | Continuous | Scan 11 coins, generate signals |
| `trade-executor.service` | Continuous | Execute confirmed signals on Binance |
| `position-manager.service` | Continuous | TSL + partial close + time exit |
| `trading-dashboard.service` | Continuous (port 8686) | Web UI with all metrics |
| `telegram-bridge.service` | Continuous | Bidirectional Telegram chat |
| `binance-api-health.timer` | Every 5 min | API connectivity check + IP whitelist alert |
| `coin-health-monitor.timer` | Every 30 min | Auto-suspend bad coins (Shield 1) |
| `wallet-tracker.timer` | Every 30 min | Snapshot all wallets (Spot/Futures/Earn/Bots) |
| `grid-monitor.timer` | Every 30 min | Grid bot state polling |
| `risk-guardian.timer` | Every 30 min | Drawdown alert (live wallet, not stale state) |
| `regime-drift-detector.timer` | Every 6h | BTC trend regime change alert (Shield 2) |
| `morning-briefing.timer` | Daily 08:00 ICT | Portfolio overview + outlook |
| `grid-daily-report.timer` | Daily 20:00 ICT | Grid daily P&L Telegram report |
| `weekly-analysis.timer` | Sunday 19:00 ICT | DeepSeek strategic review |

### 3 Safety Shields

**Shield 1 — Per-coin Health Monitor** (`coin_health_monitor.py`)
- Tracks 14-day rolling per-coin performance
- Auto-suspends coin from trading if:
  - 3+ losses in 14d, AND
  - Win rate < 30%, AND
  - Total P&L < -2R
- Suspension lifted after 7d if metrics improve

**Shield 2 — Regime Drift Detector** (`regime_drift_detector.py`)
- Monitors BTC 7-day momentum vs EMA7
- Alerts on regime flip (UPTREND ↔ DOWNTREND)
- Alerts on live 7-day WR drop > 20% from baseline

**Shield 3 — Coin Tier in LLM Prompt**
- Health monitor exports tier (A/B/C/D) per coin
- LLM sees tier in prompt → applies stricter confidence thresholds for lower tiers

### Risk Guardian (`risk_guardian.py`)

Checks every 30 min using **live Binance wallet** (not stale state file):
- Daily loss > 50% of limit → warning
- Consecutive losses ≥ 2 → early warning
- Drawdown > 10% → serious watch
- Drawdown > 15% → **AUTO-PAUSE** (writes `trading_control.json`)

---

## 5. AI brain architecture

```
                    USER (Sư)
              /        |        \
        Telegram    Cursor    Cursor (Dev)
            |          |          |
        DeepSeek    Claude     Claude
       ($1-3/mo)  ($20/mo Pro)
            |          |          |
       OpenClaw    Strategist  Engineer
            \         |         /
             \        |        /
              OpenClaw System
            (state files, DB)
```

### Brain 1: OpenClaw Bot (DeepSeek via Telegram)
- **90% of daily interaction**
- Always-on daemon (`telegram_bridge.py`)
- Slash commands: `/status /wallet /asi /positions /signals /grids /pause /resume /tradectrl /briefing /memory /forget /knowledge /help`
- Free-text chat → DeepSeek with real-time system context
- **Persistent conversation memory** (`data/telegram_memory.json`):
  - Last 12 turns kept verbatim
  - Older turns auto-compressed into bullet-point summary via DeepSeek
  - Session timeout: 30 min idle → marked as "resumed" (history kept, just flagged)
  - Use `/memory` to inspect, `/forget` to wipe
- **Knowledge base** (`KNOWLEDGE.md` + `knowledge_loader.py`):
  - Ground truth document split into sections (CORE_IDENTITY, FUTURES_ALLOWLIST,
    GRID_BOTS, RISK_SHIELDS, TRAILING_SL_TIERS, RISK_PARAMS, ASI_FORMULA,
    LESSONS_LEARNED, OPERATIONAL_PLAYBOOK, FILE_PATHS, COMMON_QUESTIONS,
    CONSTRAINTS)
  - Always-included base pack (~1K tokens) injected into every prompt
  - On-demand retrieval (~500 tokens) when query keywords match other sections
  - Eliminates hallucination on system facts (allowlist coins, shield logic, etc.)
  - Use `/knowledge` to inspect; edit `KNOWLEDGE.md` to update truth
- Cost: ~$1-3/mo (knowledge adds ~$1/mo for extra prompt tokens)

### Brain 2: Strategist (Claude via Cursor)
- **5-10% interaction** — major decisions
- Backtest analysis, capital rebalance, strategy reviews
- Open Cursor when needed
- Cost: included in $20/mo Pro

### Brain 3: Engineer (Claude/GPT via Cursor)
- **1% interaction** — coding work
- Build features, fix bugs, deploy services
- Open Cursor when coding
- Cost: included in $20/mo Pro

### Proactive AI reports (no user prompt needed)

| When | What | Brain | Cost |
|---|---|---|--:|
| 08:00 daily | Morning briefing (rule-based) | Python | $0 |
| 20:00 daily | Grid bot daily report | Python | $0 |
| 21:00 daily | Daily PnL report (FinanceBot OpenClaw cron) | Claude | varies |
| Sunday 19:00 | Weekly strategic analysis | DeepSeek | $0.05 |
| Sunday 21:00 | LLM weekly review | OpenClaw cron | varies |
| 1st of month | Monthly strategy review + profit split | OpenClaw cron | varies |

---

## 6. Self-Sustainability Index (ASI)

### Definition

```
ASI = monthly_profit / monthly_LLM_cost

ASI < 1.0  → 🔴 DEFICIT (system burning more than earning)
ASI 1.0-1.5 → 🟡 BREAK_EVEN (covers cost, no surplus)
ASI 1.5-2.0 → 🟢 SURPLUS (slow capital growth)
ASI ≥ 2.0  → 🚀 SELF_SUSTAINING_PLUS (scales)
```

### Tracker (`self_sustainability.py`)

Reads:
- Futures profit: `executor_state.total_pnl / days × 30`
- Grid profit: `current Trading Bots wallet - sum(grid invested_usd)`
- Earn yield: `Earn balance × 0.5% APY / 12`

Costs:
- DeepSeek: `cumulative spend / days × 30`
- Cursor Pro: $20/mo
- Anthropic: $0 (not used directly)

### Dashboard

Live at `http://localhost:8686` — top card shows ASI with color-coded status.

### Current state (2026-05-08)

```
ASI = 0.81 (DEFICIT)
Profit: +$23/mo (Futures only, grids in 7-day warmup)
Cost: $28/mo
Net: -$5/mo
```

**Path to ASI ≥ 2.0**:
- T+7d: Grid warmup ends → unrealized P&L counts → ASI ~1.2-1.6
- T+30d: Full grid yield + occasional Futures signals → ASI ~2.0-2.5
- T+90d: Compound + capital scaling → ASI ~3.0+

---

## 7. File structure

```
openclaw/
├── README.md, MASTER_ARCHITECTURE.md       Documentation
├── KNOWLEDGE.md                            Bot ground-truth knowledge base
├── docker-compose.yml, Dockerfile          Container setup
├── .env, .gitignore                        Config / git
│
├── binance_price_alert.py                  Signal generator (always-on)
├── trade_executor.py                       Trade execution on Binance
├── position_manager.py                     TSL + partial close + time exit
├── binance_reconcile.py                    Sync local state with Binance
│
├── coin_health_monitor.py                  Shield 1: per-coin auto-suspend
├── regime_drift_detector.py                Shield 2: BTC regime alerts
├── risk_guardian.py                        Drawdown + circuit breaker
├── binance_api_health.py                   API connectivity monitor
│
├── self_sustainability.py                  ASI calculator
├── wallet_tracker.py                       Multi-wallet snapshots
├── grid_monitor.py                         Grid bot state polling
│
├── decision_logger.py                      LLM decision DB logger
├── decision_query.py                       DB query CLI
├── prompt_registry.py                      Versioned LLM prompts
├── rag_memory.py                           Retrieval augmented context
├── multi_llm_escalator.py                  Fallback LLM chain
├── token_budget_guard.py                   LLM budget enforcement
├── shadow_trader.py                        Paper-trading parallel mode
├── outcome_linker.py                       Link decisions to trade outcomes
├── deepseek_cost_tracker.py                DeepSeek spend tracker
│
├── capital_scaling.py                      Performance-tier scaling proposals
├── strategy_portfolio.py                   Multi-strategy capital allocation
├── monthly_profit_split.py                 Reinvest/withdraw split (1st of month)
├── monthly_strategy_review.py              Strategy proposal script
├── weekly_llm_review.py                    LLM tweaks proposal
│
├── telegram_bridge.py                      Bidirectional Telegram <-> AI chat
├── knowledge_loader.py                     Bot knowledge base loader (static + retrieval)
├── reports/
│   ├── morning_briefing.py                 Daily 08:00 portfolio summary
│   └── weekly_analysis.py                  Sunday 19:00 DeepSeek review
│
├── dashboard.py                            Flask web UI port 8686
├── forex_research.py                       Forex daily research
│
├── backtest_v3_v4.py                       Original backtest engine
├── backtest_v5.py                          Mode-aware SL/TP
├── backtest_v6.py                          Multi-optimization stack
├── backtest_v7_gaps.py                     4-gap upgrade backtest
├── backtest_candidates.py                  Coin candidate scoring
├── backtest_regime_split.py                Regime-aware backtest
├── BACKTEST_*_REPORT.md                    Backtest analysis docs
│
└── data/                                   State files (gitignored)
    ├── executor_state.json                 Futures trade history + P&L
    ├── trading_state.json                  Active positions
    ├── wallet_balance_history.json         Multi-wallet snapshots
    ├── grid_config.json                    Grid bot configurations
    ├── grid_monitor_state.json             Grid trade fills
    ├── coin_suspensions.json               Shield 1 state
    ├── regime_drift_state.json             Shield 2 state
    ├── decisions.db                        SQLite LLM decisions log
    ├── deepseek_cost_state.json            DeepSeek spend tracking
    ├── pending_signal.json                 Latest signal awaiting review
    ├── telegram_bridge_state.json          Telegram update_id offset
    ├── telegram_memory.json                Conversation memory (recent + summary)
    ├── workspace-finance/                  FinanceBot agent (OpenClaw cron)
    │   ├── CLAUDE.md                       FinanceBot instructions
    │   └── trading_control.json            Auto-trade enable + thresholds
    └── cron/jobs.json                      OpenClaw scheduled jobs
```

---

## 8. Operational playbook

### Daily

- **08:00**: Morning briefing arrives in Telegram → glance and continue day
- **20:00**: Grid daily report arrives → check P&L
- **Anytime**: If curious, type `/status` or `/asi` in Telegram → instant answer

### Weekly

- **Sunday 19:00**: Strategic review arrives → read recommendations
- **Sunday 21:00**: LLM review arrives → see suggested prompt tweaks
- Decide if any action needed (usually no)

### Monthly

- **1st of month**: Strategy review + profit split proposals
- Decide if rebalance is warranted
- Consider scaling up Futures capital if performance hits criteria

### Emergency

- API outage / IP whitelist issue → `binance-api-health` alerts within 5 min
- Drawdown > 15% → `risk-guardian` auto-pauses, alerts immediately
- Grid Stop Loss triggered → manual notification from Binance
- BTC regime flip → `regime-drift-detector` alerts within 6h

### How to make a major change

1. Open Cursor → describe the change to Claude
2. Claude analyzes, proposes, drafts implementation
3. User approves
4. Claude edits code, restarts services
5. Verify via dashboard or `/status` command

---

## 9. Lessons learned

### Lesson 1: Backtest must use the right data source
- **Bug**: Backtest used SPOT klines (api.binance.com), but live trades on FUTURES (fapi.binance.com)
- **Symptom**: MKR rated TIER S in backtest, but no Futures contract exists
- **Fix**: Verify Futures listing before adding coin
- **TODO**: Add `verify_futures_listed()` step to `backtest_candidates.py`

### Lesson 2: Grid bots need volatility, not just sideway
- **Bug**: 8 old BTC/ETH/BNB bots had average yield 0.44%/mo (under-performing)
- **Cause**: BTC vol too low, range too tight, no SL
- **Fix**: Switch to AAVE (35% vol), wider range, mandatory SL
- **Result**: -$283 realized loss on old bots, but new bots designed for actual market

### Lesson 3: Risk per trade scales with capital
- **Bug**: After $200 top-up, `starting_balance` still $86 → drawdown calc wrong
- **Fix**: Reset baseline after every capital injection. Add audit trail.

### Lesson 4: One LLM doesn't fit all use cases
- **Insight**: Real-time signal review (50/day) → DeepSeek (cheap, fast)
- Complex strategy decisions (5/month) → Claude (deep reasoning)
- Code editing → Claude with editor tools
- Don't pay Claude prices for simple Q&A

### Lesson 5: Patient money beats clever money
- User skipped Futures Grid Bot temptation (would have liquidated easily)
- Kept $447 in Reserve instead of deploying immediately
- Wait for grid validation (7d warmup) before scaling

---

## 10. Future enhancements (backlog)

- [ ] Add `verify_futures_listed()` to backtest scripts
- [x] Memory layer for Telegram bridge (recent turns + DeepSeek summary, May 2026)
- [ ] Voice input via Telegram (Whisper → DeepSeek)
- [ ] Pyramiding / DCA-in for winning Futures positions (after live data validates)
- [ ] Auto-rotation of grid coins (monthly re-score top sideways winners)
- [ ] Multi-account support (separate API keys per layer)
- [ ] Dashboard mobile-responsive layout
- [ ] Integration with TradingView alerts for additional signal sources
- [ ] LLM-driven dynamic position sizing (currently fixed 3% risk)
- [ ] Telegram inline buttons for /pause /resume confirmation

---

## Author

**Sư** (PicoPiece) — owner, decision maker
**OpenClaw Bot** (DeepSeek-powered) — daily co-pilot
**Claude** (via Cursor) — strategist + engineer

System built collaboratively April-May 2026.

License: Personal use.
