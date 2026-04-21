#!/usr/bin/env python3
"""
Trading Dashboard — Real-time monitoring & historical analysis
Port 8686 — reads from JSON state files, no database needed.
"""

import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
TRADING_STATE_FILE = SCRIPT_DIR / "data" / "workspace-finance" / "trading_state.json"
EXECUTOR_STATE_FILE = SCRIPT_DIR / "data" / "executor_state.json"
ALERT_STATE_FILE = SCRIPT_DIR / "data" / "price_alert_state.json"
PENDING_SIGNAL_FILE = SCRIPT_DIR / "data" / "pending_signal.json"
SIGNAL_LOG_FILE = SCRIPT_DIR / "data" / "signal_log.json"
ENV_FILE = SCRIPT_DIR / ".env"

BINANCE_FUTURES_PRICE_API = "https://fapi.binance.com/fapi/v1/ticker/price"
BINANCE_SPOT_PRICE_API = "https://api.binance.com/api/v3/ticker/price"

_price_cache: dict = {}
_price_cache_ts: float = 0


def load_json(path: Path) -> dict | list:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def fetch_prices(symbols: list[str]) -> dict:
    global _price_cache, _price_cache_ts
    now = time.time()
    if now - _price_cache_ts < 5 and _price_cache:
        return {s: _price_cache.get(s, 0) for s in symbols}
    try:
        req = urllib.request.Request(BINANCE_FUTURES_PRICE_API, headers={"User-Agent": "PicoDash/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        all_prices = {d["symbol"]: float(d["price"]) for d in data}
        _price_cache = all_prices
        _price_cache_ts = now
        return {s: all_prices.get(s, 0) for s in symbols}
    except Exception:
        return {s: _price_cache.get(s, 0) for s in symbols}


def get_signal_log() -> list:
    if SIGNAL_LOG_FILE.exists():
        return json.loads(SIGNAL_LOG_FILE.read_text())
    return []


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/dashboard")
def api_dashboard():
    trading_state = load_json(TRADING_STATE_FILE)
    executor_state = load_json(EXECUTOR_STATE_FILE)
    pending = load_json(PENDING_SIGNAL_FILE)
    signal_log = get_signal_log()
    env = load_env()
    states = trading_state.get("states", {})

    SYMBOL_MAP = {
        "pepe": "1000PEPEUSDT",
        "shib": "1000SHIBUSDT",
    }

    symbols = []
    sym_map = {}
    for coin, s in states.items():
        sym = s.get("binance_symbol") or SYMBOL_MAP.get(coin, coin.upper() + "USDT")
        symbols.append(sym)
        sym_map[sym] = coin

    prices = fetch_prices(symbols) if symbols else {}

    active_positions = []
    watching = []
    for coin, s in sorted(states.items()):
        sym = s.get("binance_symbol") or SYMBOL_MAP.get(coin, coin.upper() + "USDT")
        price = prices.get(sym, 0)
        entry = s.get("fill_price") or s.get("entry_price", 0)
        direction = s.get("direction", "")

        pnl = 0
        pnl_pct = 0
        if entry and price and direction:
            if direction == "LONG":
                pnl_pct = (price - entry) / entry * 100
            else:
                pnl_pct = (entry - price) / entry * 100
            qty = s.get("fill_qty", 0)
            pnl = pnl_pct / 100 * entry * qty if qty else 0

        item = {
            "coin": coin.upper(),
            "state": s.get("state", "WATCHING"),
            "direction": direction,
            "entry": entry,
            "sl": s.get("sl_price", 0),
            "tp": s.get("tp_price", 0),
            "price": price,
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 2),
            "order_id": s.get("order_id", ""),
            "fill_qty": s.get("fill_qty", 0),
            "signal_strength": s.get("signal_strength", ""),
        }

        if s.get("state") == "ACTIVE":
            active_positions.append(item)
        else:
            watching.append(item)

    trade_history = executor_state.get("trade_history", [])

    wins = [t for t in trade_history if t.get("result") == "TP_HIT"]
    losses = [t for t in trade_history if t.get("result") == "SL_HIT"]
    total_trades = len(trade_history)
    win_rate = len(wins) / total_trades * 100 if total_trades else 0
    avg_win = sum(t.get("pnl", 0) for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t.get("pnl", 0) for t in losses) / len(losses) if losses else 0
    expectancy = (win_rate / 100 * avg_win + (1 - win_rate / 100) * avg_loss) if total_trades else 0

    equity_curve = []
    running_pnl = 0
    for t in trade_history:
        running_pnl += t.get("pnl", 0)
        equity_curve.append({
            "time": t.get("time", ""),
            "pnl": round(running_pnl, 4),
            "trade_pnl": round(t.get("pnl", 0), 4),
            "coin": t.get("coin", "").upper(),
            "direction": t.get("direction", ""),
            "result": t.get("result", ""),
        })

    coin_stats = {}
    for t in trade_history:
        c = t.get("coin", "unknown").upper()
        if c not in coin_stats:
            coin_stats[c] = {"wins": 0, "losses": 0, "total_pnl": 0}
        if t.get("result") == "TP_HIT":
            coin_stats[c]["wins"] += 1
        else:
            coin_stats[c]["losses"] += 1
        coin_stats[c]["total_pnl"] += t.get("pnl", 0)

    coin_perf = [{"coin": c, **s, "total_pnl": round(s["total_pnl"], 4)} for c, s in sorted(coin_stats.items(), key=lambda x: x[1]["total_pnl"], reverse=True)]

    balance = float(env.get("PORTFOLIO_BALANCE", "100"))
    auto_trade = env.get("AUTO_TRADE_ENABLED", "false").lower() in ("true", "1", "yes")
    leverage = int(env.get("FUTURES_LEVERAGE", "5"))
    daily_limit = float(env.get("DAILY_LOSS_LIMIT", "10"))

    pending_info = None
    if pending and pending.get("status") in ("pending_review", "auto_confirmed"):
        pending_info = {
            "coin": pending.get("coin", "").upper(),
            "direction": pending.get("direction", ""),
            "entry": pending.get("entry", 0),
            "strength": pending.get("strength", ""),
            "status": pending.get("status", ""),
            "timestamp": pending.get("timestamp", ""),
        }

    return jsonify({
        "portfolio": {
            "balance": balance,
            "leverage": leverage,
            "daily_loss_limit": daily_limit,
            "auto_trade": auto_trade,
            "daily_pnl": round(executor_state.get("daily_pnl", 0), 4),
            "total_pnl": round(executor_state.get("total_pnl", 0), 4),
            "total_trades": executor_state.get("total_trades", 0),
            "consecutive_losses": executor_state.get("consecutive_losses", 0),
            "paused_until": executor_state.get("paused_until"),
        },
        "stats": {
            "total_trades": total_trades,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 1),
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "expectancy": round(expectancy, 4),
            "best_trade": round(max((t.get("pnl", 0) for t in trade_history), default=0), 4),
            "worst_trade": round(min((t.get("pnl", 0) for t in trade_history), default=0), 4),
        },
        "active_positions": active_positions,
        "watching_count": len(watching),
        "pending_signal": pending_info,
        "trade_history": list(reversed(trade_history[-50:])),
        "equity_curve": equity_curve,
        "coin_performance": coin_perf,
        "last_update": datetime.now(timezone.utc).isoformat(),
    })


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PicoTrader Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace; background: #0a0e17; color: #e1e5ee; font-size: 14px; }
.container { max-width: 1400px; margin: 0 auto; padding: 16px; }
header { display: flex; justify-content: space-between; align-items: center; padding: 12px 0; border-bottom: 1px solid #1e2a3a; margin-bottom: 16px; }
header h1 { font-size: 20px; color: #00d4aa; font-weight: 600; }
.status-badge { padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: 600; }
.badge-live { background: #1a3a2a; color: #00d4aa; }
.badge-paused { background: #3a2a1a; color: #ffaa00; }
.badge-auto { background: #1a2a3a; color: #00aaff; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; margin-bottom: 16px; }
.card { background: #111827; border: 1px solid #1e2a3a; border-radius: 8px; padding: 16px; }
.card-title { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: #6b7280; margin-bottom: 8px; }
.card-value { font-size: 28px; font-weight: 700; }
.card-sub { font-size: 12px; color: #6b7280; margin-top: 4px; }
.positive { color: #00d4aa; }
.negative { color: #ff4757; }
.neutral { color: #6b7280; }
.chart-container { background: #111827; border: 1px solid #1e2a3a; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
.chart-container h2 { font-size: 14px; color: #9ca3af; margin-bottom: 12px; }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; padding: 8px 12px; font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: #6b7280; border-bottom: 1px solid #1e2a3a; }
td { padding: 8px 12px; border-bottom: 1px solid #0d1117; font-size: 13px; }
tr:hover { background: #151d2b; }
.dir-long { color: #00d4aa; }
.dir-short { color: #ff4757; }
.tp-hit { color: #00d4aa; background: #0a2a1a; padding: 2px 8px; border-radius: 4px; font-size: 11px; }
.sl-hit { color: #ff4757; background: #2a0a0a; padding: 2px 8px; border-radius: 4px; font-size: 11px; }
.signal-pending { color: #ffaa00; background: #2a2a0a; padding: 2px 8px; border-radius: 4px; font-size: 11px; }
.coin-tag { background: #1e2a3a; padding: 2px 8px; border-radius: 4px; font-weight: 600; font-size: 12px; }
.progress-bar { height: 6px; background: #1e2a3a; border-radius: 3px; overflow: hidden; margin-top: 6px; }
.progress-fill { height: 100%; border-radius: 3px; transition: width 0.5s; }
.two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px; }
@media (max-width: 768px) { .two-col { grid-template-columns: 1fr; } }
.refresh-timer { font-size: 11px; color: #4b5563; }
.positions-section { margin-bottom: 16px; }
.no-data { text-align: center; padding: 40px; color: #4b5563; }
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>PicoTrader</h1>
    <div>
      <span id="modeBadge" class="status-badge badge-auto">AUTO</span>
      <span id="statusBadge" class="status-badge badge-live">LIVE</span>
      <span class="refresh-timer" id="timer">--</span>
    </div>
  </header>

  <div class="grid" id="statsGrid"></div>

  <div id="pendingAlert" style="display:none; background:#2a2a0a; border:1px solid #554400; border-radius:8px; padding:12px 16px; margin-bottom:16px;">
    <span style="color:#ffaa00; font-weight:600;">PENDING SIGNAL</span>
    <span id="pendingText"></span>
  </div>

  <div class="positions-section">
    <div class="card">
      <div class="card-title">Active Positions</div>
      <table>
        <thead><tr><th>Coin</th><th>Dir</th><th>Entry</th><th>Price</th><th>SL</th><th>TP</th><th>P&L</th><th>Qty</th></tr></thead>
        <tbody id="positionsTable"></tbody>
      </table>
      <div class="no-data" id="noPositions" style="display:none;">No active positions</div>
    </div>
  </div>

  <div class="two-col">
    <div class="chart-container">
      <h2>Equity Curve</h2>
      <canvas id="equityChart" height="200"></canvas>
      <div class="no-data" id="noEquity" style="display:none;">No trades yet</div>
    </div>
    <div class="chart-container">
      <h2>Performance by Coin</h2>
      <canvas id="coinChart" height="200"></canvas>
      <div class="no-data" id="noCoinData" style="display:none;">No trades yet</div>
    </div>
  </div>

  <div class="card" style="margin-bottom: 16px;">
    <div class="card-title">Trade History</div>
    <table>
      <thead><tr><th>Time</th><th>Coin</th><th>Dir</th><th>Entry</th><th>Close</th><th>P&L</th><th>Result</th></tr></thead>
      <tbody id="historyTable"></tbody>
    </table>
    <div class="no-data" id="noHistory" style="display:none;">No trades yet — signals are being monitored</div>
  </div>
</div>

<script>
let equityChartInstance = null;
let coinChartInstance = null;

function fmt(val) {
  if (Math.abs(val) >= 100) return '$' + val.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
  if (Math.abs(val) >= 1) return '$' + val.toFixed(4);
  if (Math.abs(val) >= 0.01) return '$' + val.toFixed(6);
  return '$' + val.toFixed(8);
}

function pnlClass(v) { return v > 0 ? 'positive' : v < 0 ? 'negative' : 'neutral'; }

function update() {
  fetch('/api/dashboard')
    .then(r => r.json())
    .then(d => render(d))
    .catch(e => console.error('Fetch error:', e));
}

function render(d) {
  const p = d.portfolio;
  const s = d.stats;

  document.getElementById('modeBadge').textContent = p.auto_trade ? 'AUTO' : 'MANUAL';
  document.getElementById('modeBadge').className = 'status-badge ' + (p.auto_trade ? 'badge-auto' : 'badge-paused');

  const paused = p.paused_until || p.consecutive_losses >= 3;
  document.getElementById('statusBadge').textContent = paused ? 'PAUSED' : 'LIVE';
  document.getElementById('statusBadge').className = 'status-badge ' + (paused ? 'badge-paused' : 'badge-live');

  document.getElementById('statsGrid').innerHTML = `
    <div class="card">
      <div class="card-title">Portfolio</div>
      <div class="card-value">$${p.balance.toFixed(0)}</div>
      <div class="card-sub">${p.leverage}x leverage | Daily limit: -$${p.daily_loss_limit.toFixed(0)}</div>
    </div>
    <div class="card">
      <div class="card-title">Total P&L</div>
      <div class="card-value ${pnlClass(p.total_pnl)}">$${p.total_pnl >= 0 ? '+' : ''}${p.total_pnl.toFixed(2)}</div>
      <div class="card-sub">Today: <span class="${pnlClass(p.daily_pnl)}">$${p.daily_pnl >= 0 ? '+' : ''}${p.daily_pnl.toFixed(2)}</span> | Trades: ${p.total_trades}</div>
    </div>
    <div class="card">
      <div class="card-title">Win Rate</div>
      <div class="card-value">${s.total_trades ? s.win_rate.toFixed(1) + '%' : '--'}</div>
      <div class="card-sub">${s.wins}W / ${s.losses}L | Expectancy: $${s.expectancy.toFixed(2)}</div>
      <div class="progress-bar"><div class="progress-fill" style="width:${s.win_rate}%; background:${s.win_rate >= 50 ? '#00d4aa' : s.win_rate >= 40 ? '#ffaa00' : '#ff4757'};"></div></div>
    </div>
    <div class="card">
      <div class="card-title">Avg Win / Loss</div>
      <div class="card-value positive">+$${s.avg_win.toFixed(2)}</div>
      <div class="card-sub">Avg loss: <span class="negative">$${s.avg_loss.toFixed(2)}</span> | Best: $${s.best_trade.toFixed(2)} | Worst: $${s.worst_trade.toFixed(2)}</div>
    </div>
    <div class="card">
      <div class="card-title">Active Positions</div>
      <div class="card-value">${d.active_positions.length}</div>
      <div class="card-sub">Watching: ${d.watching_count} coins | Losses streak: ${p.consecutive_losses}/3</div>
    </div>
  `;

  // Pending signal
  const pa = document.getElementById('pendingAlert');
  if (d.pending_signal) {
    pa.style.display = 'block';
    const ps = d.pending_signal;
    document.getElementById('pendingText').innerHTML = ` — <span class="coin-tag">${ps.coin}</span> <span class="${ps.direction === 'LONG' ? 'dir-long' : 'dir-short'}">${ps.direction}</span> @ ${fmt(ps.entry)} (${ps.strength}) <span class="signal-pending">${ps.status}</span>`;
  } else {
    pa.style.display = 'none';
  }

  // Positions
  const tbody = document.getElementById('positionsTable');
  const noPosEl = document.getElementById('noPositions');
  if (d.active_positions.length === 0) {
    tbody.innerHTML = '';
    noPosEl.style.display = 'block';
  } else {
    noPosEl.style.display = 'none';
    tbody.innerHTML = d.active_positions.map(p => `
      <tr>
        <td><span class="coin-tag">${p.coin}</span></td>
        <td class="${p.direction === 'LONG' ? 'dir-long' : 'dir-short'}">${p.direction}</td>
        <td>${fmt(p.entry)}</td>
        <td>${fmt(p.price)}</td>
        <td>${fmt(p.sl)}</td>
        <td>${fmt(p.tp)}</td>
        <td class="${pnlClass(p.pnl_pct)}">${p.pnl_pct >= 0 ? '+' : ''}${p.pnl_pct.toFixed(2)}%</td>
        <td>${p.fill_qty ? p.fill_qty.toFixed(6) : '--'}</td>
      </tr>
    `).join('');
  }

  // Equity chart
  const ec = d.equity_curve;
  if (ec.length === 0) {
    document.getElementById('equityChart').style.display = 'none';
    document.getElementById('noEquity').style.display = 'block';
  } else {
    document.getElementById('equityChart').style.display = 'block';
    document.getElementById('noEquity').style.display = 'none';
    const labels = ec.map(e => e.time ? e.time.slice(5, 16).replace('T', ' ') : '');
    const data = ec.map(e => e.pnl);
    if (equityChartInstance) equityChartInstance.destroy();
    equityChartInstance = new Chart(document.getElementById('equityChart'), {
      type: 'line',
      data: {
        labels: labels,
        datasets: [{
          label: 'Cumulative P&L ($)',
          data: data,
          borderColor: data[data.length-1] >= 0 ? '#00d4aa' : '#ff4757',
          backgroundColor: (data[data.length-1] >= 0 ? 'rgba(0,212,170,' : 'rgba(255,71,87,') + '0.1)',
          fill: true,
          tension: 0.3,
          pointRadius: 3,
          pointBackgroundColor: ec.map(e => e.result === 'TP_HIT' ? '#00d4aa' : '#ff4757'),
        }]
      },
      options: {
        responsive: true,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: (items) => ec[items[0].dataIndex]?.time?.slice(0,16) || '',
              label: (item) => {
                const e = ec[item.dataIndex];
                return `${e.coin} ${e.direction} | Trade: $${e.trade_pnl.toFixed(2)} | Total: $${e.pnl.toFixed(2)}`;
              }
            }
          }
        },
        scales: {
          x: { ticks: { color: '#4b5563', maxTicksLimit: 10 }, grid: { color: '#1e2a3a' } },
          y: { ticks: { color: '#4b5563', callback: v => '$' + v.toFixed(2) }, grid: { color: '#1e2a3a' } }
        }
      }
    });
  }

  // Coin performance chart
  const cp = d.coin_performance;
  if (cp.length === 0) {
    document.getElementById('coinChart').style.display = 'none';
    document.getElementById('noCoinData').style.display = 'block';
  } else {
    document.getElementById('coinChart').style.display = 'block';
    document.getElementById('noCoinData').style.display = 'none';
    if (coinChartInstance) coinChartInstance.destroy();
    coinChartInstance = new Chart(document.getElementById('coinChart'), {
      type: 'bar',
      data: {
        labels: cp.map(c => c.coin),
        datasets: [
          { label: 'Wins', data: cp.map(c => c.wins), backgroundColor: '#00d4aa' },
          { label: 'Losses', data: cp.map(c => -c.losses), backgroundColor: '#ff4757' },
        ]
      },
      options: {
        responsive: true,
        plugins: { legend: { labels: { color: '#9ca3af' } } },
        scales: {
          x: { ticks: { color: '#4b5563' }, grid: { color: '#1e2a3a' }, stacked: true },
          y: { ticks: { color: '#4b5563' }, grid: { color: '#1e2a3a' }, stacked: true }
        }
      }
    });
  }

  // Trade history
  const hTbody = document.getElementById('historyTable');
  const noHist = document.getElementById('noHistory');
  if (d.trade_history.length === 0) {
    hTbody.innerHTML = '';
    noHist.style.display = 'block';
  } else {
    noHist.style.display = 'none';
    hTbody.innerHTML = d.trade_history.map(t => `
      <tr>
        <td>${t.time ? t.time.slice(0, 16).replace('T', ' ') : '--'}</td>
        <td><span class="coin-tag">${(t.coin || '').toUpperCase()}</span></td>
        <td class="${t.direction === 'LONG' ? 'dir-long' : 'dir-short'}">${t.direction}</td>
        <td>${fmt(t.entry || 0)}</td>
        <td>${fmt(t.close || 0)}</td>
        <td class="${pnlClass(t.pnl)}">$${t.pnl >= 0 ? '+' : ''}${(t.pnl || 0).toFixed(2)}</td>
        <td><span class="${t.result === 'TP_HIT' ? 'tp-hit' : 'sl-hit'}">${t.result}</span></td>
      </tr>
    `).join('');
  }

  document.getElementById('timer').textContent = 'Updated ' + new Date().toLocaleTimeString();
}

update();
setInterval(update, 10000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8686, debug=False)
