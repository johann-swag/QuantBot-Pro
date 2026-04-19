# QuantBot Pro — Technical Documentation & Release Notes

**Version:** 2.0.0 | **Status:** Production-Ready | **License:** Proprietary

---

## Overview

QuantBot Pro is a modular, risk-first algorithmic trading system designed for systematic crypto and equity markets. The architecture enforces strict separation between signal generation, risk validation, and order execution. Every trade passes through a multi-layer gate before reaching the exchange API.

```
Market Data → Engine → Validator → RiskManager → Executor → Exchange API
                                        ↓
                               CircuitBreaker / Correlation Filter
                                        ↓
                               session.json (State Log)
```

---

## Feature List

### Signal Engine
| Feature | Description |
|---|---|
| **EMA Crossover** | Fast/Slow exponential moving average crossover as primary trend signal |
| **RSI Filter** | Prevents entries in overbought/oversold extremes (configurable thresholds) |
| **ADX Regime Filter** | Rejects trades when ADX < threshold — avoids choppy, ranging markets |
| **Cooldown Timer** | Enforces a mandatory pause (in minutes) between consecutive trades on the same symbol |

### Risk Management
| Feature | Description |
|---|---|
| **Position Sizing** | Kelly-derived sizing based on account equity and per-trade risk % |
| **Trailing Stop-Loss** | Dynamic stop that ratchets up with price; never moves against the position |
| **Daily Drawdown Circuit Breaker** | Halts all trading for the session if daily loss exceeds configured threshold (e.g., 3%) |
| **Correlation Filter** | Blocks new entries in assets correlated above a threshold (e.g., r > 0.90) to prevent exposure concentration |

### Infrastructure
| Feature | Description |
|---|---|
| **Walk-Forward Validator** | Rolling-window backtester that prevents overfitting; uses anchored or sliding windows |
| **JSON Structured Logging** | Every event (signal, order, rejection, error) logged with timestamp and context |
| **Health Checker** | Polls API connectivity, data freshness, and process health every N seconds |
| **Live Dashboard** | Terminal-based or web dashboard reading from `session.json` in real time |

---

## Security Architecture

### State Management — `session.json`

`session.json` is the single source of truth for the current trading session. It is written atomically (write-to-tmp, then rename) to prevent corruption on crash.

```json
{
  "session_id": "2024-01-15T08:00:00Z",
  "equity_start": 10000.00,
  "equity_current": 9850.00,
  "daily_drawdown_pct": 1.50,
  "circuit_breaker_active": false,
  "open_positions": {},
  "trade_log": [],
  "last_updated": "2024-01-15T10:32:11Z"
}
```

**Key properties:**
- Written after every state-changing event (order fill, stop update, drawdown check)
- Never contains API credentials — only trade state
- Read by Dashboard and HealthChecker without write access (reader/writer separation enforced by file permissions)
- Backed up with timestamp prefix on session start (`session_2024-01-15.json.bak`)

### API Isolation — `.env`

All exchange credentials and sensitive parameters are stored exclusively in `.env`. The application loads them once at startup via `python-dotenv`. They are **never** written to logs, `session.json`, or `config.yaml`.

```
# .env (never commit to version control)
EXCHANGE_API_KEY=xxxxxxxxxxxx
EXCHANGE_API_SECRET=xxxxxxxxxxxx
TELEGRAM_BOT_TOKEN=xxxxxxxxxxxx   # optional alerting
LOG_LEVEL=INFO
```

**Enforcement rules:**
1. `.env` is in `.gitignore` — enforced via pre-commit hook
2. API client is instantiated in an isolated `ExchangeClient` module; the key/secret never leave that module
3. All other modules receive a pre-authenticated `client` object — no module outside `exchange_client.py` ever reads `os.environ` for credentials

---

## Installation

### Requirements

```
Python >= 3.10
```

### Dependencies

```bash
pip install -r requirements.txt
```

**`requirements.txt`:**
```
ccxt>=4.2.0              # Exchange connectivity
pandas>=2.0.0            # Data manipulation
numpy>=1.26.0            # Numerical computation
ta>=0.11.0               # Technical indicators (EMA, RSI, ADX)
pyyaml>=6.0.1            # Config loading
python-dotenv>=1.0.0     # Credential management
pytest>=7.4.0            # Test framework
pytest-mock>=3.12.0      # Mocking utilities
pytest-cov>=4.1.0        # Coverage reports
rich>=13.7.0             # Dashboard terminal UI
schedule>=1.2.0          # Health check scheduler
scipy>=1.11.0            # Correlation calculations
```

### Setup Steps

```bash
# 1. Clone and enter project
git clone https://github.com/your-org/quantbot-pro.git
cd quantbot-pro

# 2. Create virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure credentials
cp .env.example .env
nano .env                        # Insert your API keys

# 5. Review and customize strategy parameters
nano config.yaml

# 6. Run the test suite (all tests must pass before live trading)
pytest test_trading_system.py -v --cov=. --cov-report=term-missing

# 7. Run walk-forward validation on historical data
python validator.py --mode walkforward --symbol BTC/USDT --start 2023-01-01

# 8. Start the bot (paper trading mode first!)
python engine.py --mode paper

# 9. In a second terminal: launch the dashboard
python dashboard.py
```

### Directory Structure

```
quantbot-pro/
├── engine.py               # Main trading loop
├── risk_manager.py         # Position sizing, drawdown, correlation
├── validator.py            # Walk-forward backtester
├── dashboard.py            # Live terminal dashboard
├── health_checker.py       # System health polling
├── exchange_client.py      # API isolation layer
├── config.yaml             # All strategy parameters
├── session.json            # Live session state (auto-generated)
├── logs/
│   └── trades.jsonl        # Append-only structured trade log
├── test_trading_system.py  # Full PyTest suite
├── .env                    # Credentials (never commit)
├── .env.example            # Template (safe to commit)
├── .gitignore
└── requirements.txt
```

---

## Quick Reference: Risk Parameters

| Parameter | Default | Description |
|---|---|---|
| `risk.per_trade_pct` | 1.0% | Max loss per trade as % of equity |
| `risk.daily_drawdown_limit` | 3.0% | Circuit breaker threshold |
| `risk.correlation_threshold` | 0.90 | Max correlation for simultaneous positions |
| `risk.trailing_stop_pct` | 2.0% | Trailing stop distance |
| `engine.cooldown_minutes` | 30 | Pause between trades per symbol |
| `adx.min_threshold` | 25 | Minimum ADX for trade entry |

---

*For architecture questions, see inline documentation in each module. For test coverage details, run `pytest --cov-report=html` and open `htmlcov/index.html`.*
