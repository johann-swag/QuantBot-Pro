"""
================================================================================
  QuantBot Pro — Web Dashboard
  Flask-Server (nur localhost:5000)

  Start: python3 dashboard.py
  Liest:  logs/backtest_BTC_USDT.json
          logs/walkforward_BTC_USDT_*.json  (neueste Datei)
================================================================================
"""

import json
import glob
import os
import socket
from pathlib import Path
from datetime import datetime, timezone

from flask import Flask, jsonify

app = Flask(__name__, static_folder=None)
LOG_DIR = Path("logs")

# ============================================================
# DATEN LADEN
# ============================================================

def load_env_config() -> dict:
    for candidate in [Path("/opt/quantbot/.env"), Path(".env")]:
        if candidate.exists():
            cfg = {}
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
            return cfg
    return {}

def load_backtest(symbol: str = "BTC_USDT") -> dict:
    path = LOG_DIR / f"backtest_{symbol}.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)

def load_walkforward(symbol: str = "BTC_USDT") -> dict:
    files = sorted(glob.glob(str(LOG_DIR / f"walkforward_{symbol}_*.json")))
    if not files:
        return None
    with open(files[-1]) as f:
        return json.load(f)

def build_backtest_stats(data: dict) -> dict:
    trades  = data.get("trades", [])
    equity  = data.get("equity", [])
    if not equity:
        return {}

    start = equity[0]
    end   = equity[-1]
    ret   = (end - start) / start * 100

    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0

    gp = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses)) if losses else 1e-9
    pf = round(gp / gl, 2)

    import pandas as pd
    eq = pd.Series(equity)
    max_dd = round(float(((eq - eq.cummax()) / eq.cummax() * 100).min()), 2)

    return {
        "start_balance": round(start, 2),
        "end_balance":   round(end, 2),
        "total_return":  round(ret, 2),
        "n_trades":      len(trades),
        "win_rate":      round(win_rate, 1),
        "profit_factor": pf,
        "max_drawdown":  max_dd,
        "trades":        trades,
        "equity":        equity,
    }

def load_portfolio() -> dict:
    path = LOG_DIR / "portfolio_trades.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)

def build_portfolio_stats(data: dict) -> dict:
    if not data:
        return {}
    strategies = data.get("strategies", {})
    trades     = data.get("trades", [])
    tf_trades  = [t for t in trades if t.get("strategy") == "TF"]
    mr_trades  = [t for t in trades if t.get("strategy") == "MR"]

    def slot_stats(strat_data, strat_trades):
        wins   = [t["pnl"] for t in strat_trades if t["pnl"] > 0]
        losses = [t["pnl"] for t in strat_trades if t["pnl"] <= 0]
        gp = sum(wins)   if wins   else 0
        gl = abs(sum(losses)) if losses else 1e-9
        return {
            "balance":       strat_data.get("balance", strat_data.get("start_balance", 0)),
            "pnl":           round(strat_data.get("pnl", 0), 2),
            "wins":          strat_data.get("wins", 0),
            "losses":        strat_data.get("losses", 0),
            "profit_factor": round(gp / gl, 2),
            "win_rate":      round(len(wins) / len(strat_trades) * 100, 1) if strat_trades else 0,
            "n_trades":      len(strat_trades),
        }

    # Combined equity curve: cumulative PnL over all trades sorted by exit time
    sorted_trades = sorted(trades, key=lambda t: t.get("exit_time", ""))
    cum_pnl = 0.0
    equity  = [data.get("capital_total", 10000)]
    for t in sorted_trades:
        cum_pnl += t["pnl"]
        equity.append(data.get("capital_total", 10000) + cum_pnl)

    total_pnl = strategies.get("TF", {}).get("pnl", 0) + strategies.get("MR", {}).get("pnl", 0)
    return {
        "symbol":         data.get("symbol", ""),
        "start_time":     data.get("start_time", ""),
        "last_update":    data.get("last_update", ""),
        "capital_total":  data.get("capital_total", 10000),
        "circuit_active": data.get("circuit_active", False),
        "combined_losses":data.get("combined_losses", 0),
        "total_pnl":      round(total_pnl, 2),
        "TF":             slot_stats(strategies.get("TF", {}), tf_trades),
        "MR":             slot_stats(strategies.get("MR", {}), mr_trades),
        "equity":         equity,
        "trades":         sorted_trades,
    }

def build_wf_stats(data: dict) -> dict:
    if not data:
        return {}
    windows = data.get("windows", [])
    return {
        "symbol":            data.get("symbol", ""),
        "total_days":        data.get("total_days", 0),
        "n_windows":         data.get("n_windows", 0),
        "consistency_rate":  round(data.get("consistency_rate", 0) * 100, 0),
        "total_test_pnl":    round(data.get("total_test_pnl", 0), 2),
        "total_test_trades": data.get("total_test_trades", 0),
        "overall_winrate":   round(data.get("overall_winrate", 0), 1),
        "avg_profit_factor": round(data.get("avg_profit_factor", 0), 2),
        "overall_maxdd":     round(data.get("overall_maxdd", 0), 2),
        "efficiency_ratio":  round(data.get("efficiency_ratio", 0), 2),
        "verdict":           data.get("verdict", ""),
        "windows":           windows,
    }

# ============================================================
# API ENDPOINTS
# ============================================================

@app.route("/api/backtest")
def api_backtest():
    raw = load_backtest()
    if raw is None:
        return jsonify({"error": "Keine Backtest-Daten gefunden"}), 404
    return jsonify(build_backtest_stats(raw))

@app.route("/api/portfolio")
def api_portfolio():
    raw = load_portfolio()
    if raw is None:
        return jsonify({"error": "Keine Portfolio-Daten gefunden (portfolio.py --paper starten)"}), 404
    return jsonify(build_portfolio_stats(raw))

@app.route("/api/walkforward")
def api_walkforward():
    raw = load_walkforward()
    if raw is None:
        return jsonify({"error": "Keine Walk-Forward-Daten gefunden"}), 404
    return jsonify(build_wf_stats(raw))

@app.route("/api/config")
def api_config():
    env  = load_env_config()
    port = load_portfolio()
    uptime_str = "—"
    if port and port.get("start_time"):
        try:
            start = datetime.fromisoformat(port["start_time"])
            secs  = int((datetime.now(timezone.utc) - start).total_seconds())
            h, m  = secs // 3600, (secs % 3600) // 60
            uptime_str = f"{h}h {m:02d}m"
        except Exception:
            pass
    return jsonify({
        "container":    socket.gethostname(),
        "symbol":       env.get("SYMBOL", "BTC/USDT"),
        "capital":      float(env.get("START_CAPITAL", 10000)),
        "backtest_days": int(env.get("BACKTEST_DAYS", 0)),
        "strategy":     env.get("STRATEGY", "portfolio"),
        "uptime":       uptime_str,
    })

# ============================================================
# HTML (inline — kein separates templates/ Verzeichnis nötig)
# ============================================================

HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="30">
<title>QuantBot Pro Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg:      #0f1117;
    --card:    #1a1d26;
    --border:  #2a2d3a;
    --text:    #e2e8f0;
    --muted:   #64748b;
    --green:   #22c55e;
    --red:     #ef4444;
    --yellow:  #eab308;
    --blue:    #3b82f6;
    --accent:  #6366f1;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; }

  header { background: var(--card); border-bottom: 1px solid var(--border); padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
  header h1 { font-size: 18px; font-weight: 600; color: var(--accent); letter-spacing: .5px; }
  #refresh-info { color: var(--muted); font-size: 12px; }
  #last-updated { color: var(--text); }
  #config-bar { width: 100%; background: rgba(99,102,241,.08); border: 1px solid rgba(99,102,241,.2); border-radius: 6px; padding: 6px 14px; font-size: 12px; color: var(--muted); display: flex; gap: 20px; flex-wrap: wrap; }
  #config-bar span { white-space: nowrap; }
  #config-bar b { color: var(--text); }

  main { padding: 24px; display: grid; gap: 20px; max-width: 1400px; margin: 0 auto; }

  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  .grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
  @media (max-width: 900px) { .grid-2 { grid-template-columns: 1fr; } .grid-4 { grid-template-columns: repeat(2,1fr); } }

  .card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 20px; }
  .card h2 { font-size: 13px; font-weight: 500; color: var(--muted); text-transform: uppercase; letter-spacing: .8px; margin-bottom: 16px; }

  .kpi { display: flex; flex-direction: column; gap: 4px; }
  .kpi .value { font-size: 28px; font-weight: 700; line-height: 1; }
  .kpi .label { font-size: 11px; color: var(--muted); }
  .pos { color: var(--green); }
  .neg { color: var(--red); }
  .neu { color: var(--text); }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { color: var(--muted); text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border); font-weight: 500; font-size: 11px; text-transform: uppercase; }
  td { padding: 7px 10px; border-bottom: 1px solid #1f2230; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(99,102,241,.06); }

  .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; }
  .badge-green { background: rgba(34,197,94,.15); color: var(--green); }
  .badge-red   { background: rgba(239,68,68,.15);  color: var(--red); }
  .badge-yellow{ background: rgba(234,179,8,.15);  color: var(--yellow); }
  .badge-blue  { background: rgba(59,130,246,.15); color: var(--blue); }

  .verdict-robust    { border-left: 3px solid var(--green); padding-left: 10px; }
  .verdict-fragil    { border-left: 3px solid var(--yellow); padding-left: 10px; }
  .verdict-overfitted{ border-left: 3px solid var(--red); padding-left: 10px; }
  .verdict-unknown   { border-left: 3px solid var(--muted); padding-left: 10px; }

  .wf-window { padding: 10px 12px; border: 1px solid var(--border); border-radius: 8px; margin-bottom: 8px; }
  .wf-window .wf-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
  .wf-window .wf-label { font-weight: 600; font-size: 13px; }
  .wf-window .wf-dates { color: var(--muted); font-size: 11px; }
  .wf-window .wf-metrics { display: grid; grid-template-columns: repeat(3,1fr); gap: 8px; font-size: 12px; }
  .wf-window .wf-metric { display: flex; flex-direction: column; gap: 2px; }
  .wf-window .wf-metric .m-val { font-weight: 600; }
  .wf-window .wf-metric .m-lbl { color: var(--muted); font-size: 10px; }

  .oos-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
  .oos-item { background: rgba(255,255,255,.03); border-radius: 6px; padding: 10px 12px; }
  .oos-item .ov { font-size: 20px; font-weight: 700; }
  .oos-item .ol { font-size: 11px; color: var(--muted); margin-top: 2px; }

  canvas { max-height: 220px; }
  .empty { color: var(--muted); font-style: italic; padding: 20px 0; text-align: center; }
  .error-box { background: rgba(239,68,68,.1); border: 1px solid rgba(239,68,68,.3); border-radius: 8px; padding: 12px 16px; color: var(--red); font-size: 13px; }

  /* Portfolio */
  .section-title { font-size: 15px; font-weight: 600; color: var(--accent); letter-spacing: .3px; padding: 8px 0 4px; border-bottom: 1px solid var(--border); margin-bottom: 16px; }
  .strategy-card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 20px; }
  .strategy-card h3 { font-size: 12px; text-transform: uppercase; letter-spacing: .8px; color: var(--muted); margin-bottom: 14px; }
  .strategy-card .badge-label { font-size: 11px; font-weight: 700; padding: 3px 10px; border-radius: 6px; display: inline-block; margin-bottom: 10px; }
  .tf-badge { background: rgba(59,130,246,.2); color: var(--blue); }
  .mr-badge { background: rgba(234,179,8,.2);  color: var(--yellow); }
  .stat-row { display: flex; justify-content: space-between; padding: 5px 0; border-bottom: 1px solid #1f2230; font-size: 13px; }
  .stat-row:last-child { border-bottom: none; }
  .cb-banner { background: rgba(239,68,68,.12); border: 1px solid rgba(239,68,68,.3); border-radius: 8px; padding: 10px 14px; color: var(--red); font-weight: 600; font-size: 13px; margin-bottom: 12px; }
</style>
</head>
<body>

<header>
  <h1>⚡ QuantBot Pro Dashboard</h1>
  <div id="refresh-info">Auto-Refresh alle 30s &nbsp;|&nbsp; Zuletzt: <span id="last-updated">—</span></div>
  <div id="config-bar">
    <span>Container: <b id="cfg-container">—</b></span>
    <span>Kapital: <b id="cfg-capital">—</b></span>
    <span>Symbol: <b id="cfg-symbol">—</b></span>
    <span>Strategie: <b id="cfg-strategy">—</b></span>
    <span>Backtest: <b id="cfg-backtest">—</b></span>
    <span>Laufzeit: <b id="cfg-uptime">—</b></span>
  </div>
</header>

<main>
  <!-- KPI Row -->
  <div class="grid-4" id="kpi-row">
    <div class="card kpi"><div class="value neu" id="kpi-start">—</div><div class="label">Startkapital</div></div>
    <div class="card kpi"><div class="value neu" id="kpi-end">—</div><div class="label">Endkapital</div></div>
    <div class="card kpi"><div class="value" id="kpi-ret">—</div><div class="label">Gesamt-Return</div></div>
    <div class="card kpi"><div class="value" id="kpi-dd">—</div><div class="label">Max. Drawdown</div></div>
  </div>

  <!-- Equity Chart + Trade Stats -->
  <div class="grid-2">
    <div class="card">
      <h2>Equity Curve</h2>
      <canvas id="equityChart"></canvas>
    </div>
    <div class="card">
      <h2>Backtest Kennzahlen</h2>
      <div id="bt-stats"></div>
    </div>
  </div>

  <!-- Trades Tabelle -->
  <div class="card">
    <h2>Trade-Log</h2>
    <div id="trades-table"></div>
  </div>

  <!-- Walk-Forward -->
  <div class="grid-2">
    <div class="card">
      <h2>Walk-Forward — Fenster</h2>
      <div id="wf-windows"></div>
    </div>
    <div class="card">
      <h2>Out-of-Sample Kennzahlen</h2>
      <div id="wf-oos"></div>
    </div>
  </div>

  <!-- Portfolio Live -->
  <div class="section-title">⚡ Live Portfolio (Paper Mode)</div>
  <div id="portfolio-cb"></div>

  <!-- Portfolio KPI Row -->
  <div class="grid-4" id="port-kpi-row">
    <div class="card kpi"><div class="value neu" id="port-kpi-total">—</div><div class="label">Gesamt-PnL</div></div>
    <div class="card kpi"><div class="value neu" id="port-kpi-trades">—</div><div class="label">Trades gesamt</div></div>
    <div class="card kpi"><div class="value neu" id="port-kpi-wr">—</div><div class="label">Win-Rate gesamt</div></div>
    <div class="card kpi"><div class="value neu" id="port-kpi-updated">—</div><div class="label">Letztes Update</div></div>
  </div>

  <!-- TF + MR nebeneinander -->
  <div class="grid-2">
    <div class="strategy-card">
      <span class="badge-label tf-badge">[TF] Trend Following</span>
      <div id="port-tf-stats"></div>
    </div>
    <div class="strategy-card">
      <span class="badge-label mr-badge">[MR] Mean Reversion</span>
      <div id="port-mr-stats"></div>
    </div>
  </div>

  <!-- Portfolio Equity Curve + Trade Log -->
  <div class="grid-2">
    <div class="card">
      <h2>Portfolio Equity Curve (kombiniert)</h2>
      <canvas id="portEquityChart"></canvas>
    </div>
    <div class="card">
      <h2>Portfolio Trade-Log</h2>
      <div id="port-trades"></div>
    </div>
  </div>
</main>

<script>
// ── Hilfsfunktionen ──────────────────────────────────────────
const fmt  = (n, d=2) => n == null ? '—' : n.toFixed(d)
const fmtK = n => n == null ? '—' : n.toLocaleString('de-DE', {minimumFractionDigits:2, maximumFractionDigits:2}) + ' USDT'
const sign = n => n > 0 ? '+' : ''
const cls  = n => n > 0 ? 'pos' : n < 0 ? 'neg' : 'neu'

let equityChartInstance = null

// ── Backtest laden ───────────────────────────────────────────
async function loadBacktest() {
  try {
    const r = await fetch('/api/backtest')
    if (!r.ok) throw new Error(await r.text())
    const d = await r.json()
    if (d.error) { showBtError(d.error); return }

    // KPIs
    document.getElementById('kpi-start').textContent = fmtK(d.start_balance)
    document.getElementById('kpi-end').textContent   = fmtK(d.end_balance)

    const retEl = document.getElementById('kpi-ret')
    retEl.textContent = `${sign(d.total_return)}${fmt(d.total_return)}%`
    retEl.className = `value ${cls(d.total_return)}`

    const ddEl = document.getElementById('kpi-dd')
    ddEl.textContent = `${sign(d.max_drawdown)}${fmt(d.max_drawdown)}%`
    ddEl.className = `value ${cls(d.max_drawdown)}`

    // Equity Chart
    drawEquityChart(d.equity)

    // Stats Box
    document.getElementById('bt-stats').innerHTML = `
      <table>
        <tr><td>Trades gesamt</td><td><b>${d.n_trades}</b></td></tr>
        <tr><td>Win-Rate</td><td><b>${fmt(d.win_rate)}%</b></td></tr>
        <tr><td>Profit Factor</td><td><b class="${d.profit_factor>=1.3?'pos':d.profit_factor>=1?'yellow':'neg'}">${fmt(d.profit_factor)}</b></td></tr>
        <tr><td>Max. Drawdown</td><td><b class="${cls(d.max_drawdown)}">${sign(d.max_drawdown)}${fmt(d.max_drawdown)}%</b></td></tr>
        <tr><td>Startkapital</td><td>${fmtK(d.start_balance)}</td></tr>
        <tr><td>Endkapital</td><td>${fmtK(d.end_balance)}</td></tr>
      </table>`

    // Trades Tabelle
    if (!d.trades || d.trades.length === 0) {
      document.getElementById('trades-table').innerHTML = '<p class="empty">Keine Trades vorhanden.</p>'
      return
    }
    const rows = d.trades.map(t => {
      const win = t.pnl > 0
      return `<tr>
        <td>${t.entry_time ? t.entry_time.slice(0,16) : '—'}</td>
        <td>${t.exit_time  ? t.exit_time.slice(0,16)  : '—'}</td>
        <td>${t.symbol || '—'}</td>
        <td><span class="badge ${t.direction==='LONG'?'badge-blue':'badge-yellow'}">${t.direction||'—'}</span></td>
        <td>${fmt(t.entry,2)}</td>
        <td>${fmt(t.exit,2)}</td>
        <td class="${cls(t.pnl)}">${sign(t.pnl)}${fmt(t.pnl,2)} USDT</td>
        <td><span class="badge ${win?'badge-green':'badge-red'}">${win?'WIN':'LOSS'}</span></td>
        <td>${t.reason||'—'}</td>
      </tr>`
    }).join('')
    document.getElementById('trades-table').innerHTML = `
      <table>
        <thead><tr>
          <th>Entry</th><th>Exit</th><th>Symbol</th><th>Dir</th>
          <th>Entry Px</th><th>Exit Px</th><th>PnL</th><th>Result</th><th>Grund</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>`

  } catch(e) {
    showBtError(e.message)
  }
}

function showBtError(msg) {
  document.getElementById('bt-stats').innerHTML = `<div class="error-box">${msg}</div>`
}

// ── Equity Chart ─────────────────────────────────────────────
function drawEquityChart(equity) {
  if (!equity || equity.length < 2) return
  const ctx = document.getElementById('equityChart').getContext('2d')
  if (equityChartInstance) equityChartInstance.destroy()

  const start = equity[0]
  const labels = equity.map((_, i) => i)
  const color  = equity[equity.length-1] >= start ? '#22c55e' : '#ef4444'

  equityChartInstance = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: 'Equity (USDT)',
        data: equity,
        borderColor: color,
        backgroundColor: color + '18',
        borderWidth: 2,
        pointRadius: 0,
        fill: true,
        tension: 0.3,
      }]
    },
    options: {
      responsive: true,
      animation: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: { label: ctx => ' ' + ctx.parsed.y.toFixed(2) + ' USDT' }
        }
      },
      scales: {
        x: { display: false },
        y: {
          grid: { color: '#2a2d3a' },
          ticks: { color: '#64748b', callback: v => v.toFixed(0) }
        }
      }
    }
  })
}

// ── Walk-Forward laden ───────────────────────────────────────
async function loadWalkForward() {
  try {
    const r = await fetch('/api/walkforward')
    if (!r.ok) throw new Error(await r.text())
    const d = await r.json()
    if (d.error) {
      document.getElementById('wf-windows').innerHTML = `<div class="error-box">${d.error}</div>`
      return
    }

    // Fenster
    const wfEl = document.getElementById('wf-windows')
    if (!d.windows || d.windows.length === 0) {
      wfEl.innerHTML = '<p class="empty">Keine Fenster-Daten.</p>'
    } else {
      wfEl.innerHTML = d.windows.map(w => {
        const icon   = w.is_profitable ? '✅' : w.is_valid ? '⚠️' : '❌'
        const pnlCls = cls(w.test_pnl)
        return `<div class="wf-window">
          <div class="wf-header">
            <span class="wf-label">${icon} Fenster ${w.window_id}</span>
            <span class="badge ${w.is_profitable?'badge-green':w.is_valid?'badge-yellow':'badge-red'}">
              ${w.is_profitable?'Profitabel':w.is_valid?'Gültig':'Ungültig'}
            </span>
          </div>
          <div class="wf-dates">Train: ${w.train_start} → ${w.train_end} &nbsp;|&nbsp; Test: ${w.test_start} → ${w.test_end}</div>
          <br>
          <div class="wf-metrics">
            <div class="wf-metric"><span class="m-val">${w.train_trades}</span><span class="m-lbl">Train Trades</span></div>
            <div class="wf-metric"><span class="m-val ${pnlCls}">${sign(w.test_pnl)}${w.test_pnl.toFixed(2)}</span><span class="m-lbl">Test PnL (USDT)</span></div>
            <div class="wf-metric"><span class="m-val">${w.test_trades}</span><span class="m-lbl">Test Trades</span></div>
            <div class="wf-metric"><span class="m-val">${w.test_winrate.toFixed(1)}%</span><span class="m-lbl">Test Win-Rate</span></div>
            <div class="wf-metric"><span class="m-val ${cls(w.test_maxdd)}">${sign(w.test_maxdd)}${w.test_maxdd.toFixed(2)}%</span><span class="m-lbl">Test Max-DD</span></div>
            <div class="wf-metric"><span class="m-val">${w.test_pf.toFixed(2)}</span><span class="m-lbl">Profit Factor</span></div>
          </div>
        </div>`
      }).join('')
    }

    // OOS Zusammenfassung
    const verdict  = d.verdict || ''
    const vClass   = verdict.includes('ROBUST') ? 'verdict-robust'
                   : verdict.includes('FRAGIL') ? 'verdict-fragil'
                   : verdict.includes('OVERFITTED') ? 'verdict-overfitted'
                   : 'verdict-unknown'
    const vIcon    = verdict.includes('ROBUST') ? '✅' : verdict.includes('FRAGIL') ? '⚠️' : '❌'
    const vLabel   = verdict.includes('ROBUST') ? 'ROBUST — Live-Test vertretbar'
                   : verdict.includes('FRAGIL') ? 'FRAGIL — Parameter überdenken'
                   : verdict.includes('OVERFITTED') ? 'OVERFITTED — Nicht live!'
                   : verdict

    document.getElementById('wf-oos').innerHTML = `
      <div class="${vClass}" style="margin-bottom:16px">
        <div style="font-size:16px;font-weight:700">${vIcon} ${vLabel}</div>
        <div style="color:var(--muted);font-size:12px;margin-top:4px">${d.total_days} Tage | ${d.n_windows} Fenster</div>
      </div>
      <div class="oos-grid">
        <div class="oos-item"><div class="ov">${d.consistency_rate}%</div><div class="ol">Konsistenz</div></div>
        <div class="oos-item"><div class="ov ${cls(d.total_test_pnl)}">${sign(d.total_test_pnl)}${d.total_test_pnl.toFixed(2)}</div><div class="ol">OOS PnL (USDT)</div></div>
        <div class="oos-item"><div class="ov">${d.total_test_trades}</div><div class="ol">OOS Trades</div></div>
        <div class="oos-item"><div class="ov">${d.overall_winrate.toFixed(1)}%</div><div class="ol">Win-Rate OOS</div></div>
        <div class="oos-item"><div class="ov ${d.avg_profit_factor>=1.3?'pos':d.avg_profit_factor>=1?'':'neg'}">${d.avg_profit_factor.toFixed(2)}</div><div class="ol">Profit Factor OOS</div></div>
        <div class="oos-item"><div class="ov ${cls(d.overall_maxdd)}">${sign(d.overall_maxdd)}${d.overall_maxdd.toFixed(2)}%</div><div class="ol">Max. Drawdown OOS</div></div>
      </div>`

  } catch(e) {
    document.getElementById('wf-windows').innerHTML = `<div class="error-box">${e.message}</div>`
  }
}

// ── Portfolio ─────────────────────────────────────────────────
let portChartInstance = null

async function loadPortfolio() {
  try {
    const r = await fetch('/api/portfolio')
    if (!r.ok) {
      const d = await r.json()
      document.getElementById('portfolio-cb').innerHTML =
        `<div class="error-box">${d.error || r.statusText}</div>`
      return
    }
    const d = await r.json()
    if (d.error) {
      document.getElementById('portfolio-cb').innerHTML = `<div class="error-box">${d.error}</div>`
      return
    }

    // Circuit breaker banner
    document.getElementById('portfolio-cb').innerHTML = d.circuit_active
      ? `<div class="cb-banner">⛔ Circuit Breaker aktiv — Portfolio gestoppt (${d.combined_losses} Verluste)</div>`
      : ''

    // KPIs
    const totEl = document.getElementById('port-kpi-total')
    totEl.textContent = `${sign(d.total_pnl)}${fmt(d.total_pnl)} USDT`
    totEl.className = `value ${cls(d.total_pnl)}`

    const allTrades = d.trades || []
    const allWins   = allTrades.filter(t => t.pnl > 0).length
    const totWR     = allTrades.length > 0 ? allWins / allTrades.length * 100 : 0
    document.getElementById('port-kpi-trades').textContent  = allTrades.length
    document.getElementById('port-kpi-wr').textContent      = `${fmt(totWR)}%`
    document.getElementById('port-kpi-updated').textContent =
      d.last_update ? d.last_update.slice(11,19) + ' UTC' : '—'

    // Strategy stat helper
    function renderSlotStats(slotData, elId) {
      const pnlCls = cls(slotData.pnl)
      document.getElementById(elId).innerHTML = `
        <div class="stat-row"><span>Balance</span><span><b>${fmtK(slotData.balance)}</b></span></div>
        <div class="stat-row"><span>PnL</span><span class="${pnlCls}"><b>${sign(slotData.pnl)}${fmt(slotData.pnl)} USDT</b></span></div>
        <div class="stat-row"><span>Trades</span><span>${slotData.n_trades} (W:${slotData.wins} / L:${slotData.losses})</span></div>
        <div class="stat-row"><span>Win-Rate</span><span>${fmt(slotData.win_rate)}%</span></div>
        <div class="stat-row"><span>Profit Factor</span><span class="${slotData.profit_factor>=1.3?'pos':slotData.profit_factor>=1?'':'neg'}">${fmt(slotData.profit_factor)}</span></div>`
    }
    renderSlotStats(d.TF, 'port-tf-stats')
    renderSlotStats(d.MR, 'port-mr-stats')

    // Equity chart
    if (d.equity && d.equity.length > 1) {
      const ctx = document.getElementById('portEquityChart').getContext('2d')
      if (portChartInstance) portChartInstance.destroy()
      const start = d.equity[0]
      const color = d.equity[d.equity.length-1] >= start ? '#22c55e' : '#ef4444'
      portChartInstance = new Chart(ctx, {
        type: 'line',
        data: {
          labels: d.equity.map((_,i) => i),
          datasets: [{
            label: 'Portfolio Equity (USDT)',
            data: d.equity,
            borderColor: color,
            backgroundColor: color + '18',
            borderWidth: 2,
            pointRadius: 0,
            fill: true,
            tension: 0.3,
          }]
        },
        options: {
          responsive: true, animation: false,
          plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => ' ' + c.parsed.y.toFixed(2) + ' USDT' } } },
          scales: {
            x: { display: false },
            y: { grid: { color: '#2a2d3a' }, ticks: { color: '#64748b', callback: v => v.toFixed(0) } }
          }
        }
      })
    } else {
      document.getElementById('portEquityChart').parentElement.innerHTML +=
        '<p class="empty" style="margin-top:8px">Noch keine Trades — Portfolio starten.</p>'
    }

    // Trade log
    if (!allTrades.length) {
      document.getElementById('port-trades').innerHTML = '<p class="empty">Noch keine Trades.</p>'
      return
    }
    const rows = allTrades.slice(-20).reverse().map(t => {
      const win = t.pnl > 0
      const badge = t.strategy === 'TF'
        ? '<span class="badge badge-blue">TF</span>'
        : '<span class="badge badge-yellow">MR</span>'
      return `<tr>
        <td>${badge}</td>
        <td>${t.exit_time ? t.exit_time.slice(0,16) : '—'}</td>
        <td><span class="badge ${t.direction==='LONG'?'badge-blue':'badge-yellow'}">${t.direction}</span></td>
        <td>${fmt(t.entry,2)}</td>
        <td>${fmt(t.exit,2)}</td>
        <td class="${cls(t.pnl)}">${sign(t.pnl)}${fmt(t.pnl,2)}</td>
        <td><span class="badge ${win?'badge-green':'badge-red'}">${win?'WIN':'LOSS'}</span></td>
        <td>${t.reason||'—'}</td>
      </tr>`
    }).join('')
    document.getElementById('port-trades').innerHTML = `
      <table>
        <thead><tr>
          <th>Strat</th><th>Exit</th><th>Dir</th>
          <th>Entry</th><th>Exit Px</th><th>PnL</th><th>Result</th><th>Grund</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>`

  } catch(e) {
    document.getElementById('portfolio-cb').innerHTML = `<div class="error-box">${e.message}</div>`
  }
}

// ── Config Info Bar ──────────────────────────────────────────
async function loadConfig() {
  try {
    const r = await fetch('/api/config')
    if (!r.ok) return
    const d = await r.json()
    document.getElementById('cfg-container').textContent = d.container || '—'
    document.getElementById('cfg-capital').textContent   = d.capital != null
      ? d.capital.toLocaleString('de-DE') + ' USDT' : '—'
    document.getElementById('cfg-symbol').textContent    = d.symbol || '—'
    document.getElementById('cfg-strategy').textContent  = d.strategy || '—'
    document.getElementById('cfg-backtest').textContent  = d.backtest_days > 0
      ? d.backtest_days + ' Tage' : 'Kein Backtest'
    document.getElementById('cfg-uptime').textContent    = d.uptime || '—'
  } catch(e) { /* ignore */ }
}

// ── Init ─────────────────────────────────────────────────────
async function init() {
  document.getElementById('last-updated').textContent =
    new Date().toLocaleTimeString('de-DE')
  await Promise.all([loadBacktest(), loadWalkForward(), loadPortfolio(), loadConfig()])
}

init()
</script>
</body>
</html>"""

@app.route("/")
def index():
    return HTML

# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    print("\n+============================================================+")
    print("|   QUANTBOT PRO — WEB DASHBOARD                             |")
    print("|   http://localhost:5000                                     |")
    print("+============================================================+\n")
    print("  Liest:  logs/backtest_BTC_USDT.json")
    print("          logs/walkforward_BTC_USDT_*.json (neueste)")
    print("  Stop:   CTRL+C\n")
    app.run(host="127.0.0.1", port=5000, debug=False)
