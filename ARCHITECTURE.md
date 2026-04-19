# QuantBot Pro — Architecture Documentation
## Walk-Forward Validation & JSON Logging Schema

---

## 1. Walk-Forward Validation

### Purpose

Walk-forward validation (WFV) is the standard methodology for evaluating algorithmic trading strategies without overfitting. Unlike a single train/test split, WFV simulates real-world deployment by repeatedly training on historical data and testing on data the model has never seen — advancing through time sequentially.

**The core problem it solves:** A strategy that is optimized on a fixed historical period learns the noise of that specific period, not the underlying signal. WFV forces the strategy to prove itself across many unseen market regimes.

---

### Window Modes

#### Mode 1: Sliding Window

Both the start and end of the in-sample (IS) window advance by `step_days` each iteration.

```
Total data: 365 days | IS=180 | OOS=60 | step=30

Iter 1:  [████████████████████ IS (0-179) ████████████████████][OOS (180-239)]
Iter 2:          [████████████████████ IS (30-209) ████████████████████][OOS (210-269)]
Iter 3:                  [████████████████████ IS (60-239) ████████████████████][OOS (240-299)]
Iter 4:                          [████████████████████ IS (90-269) ████████████████████][OOS (270-329)]

Legend: ████ = In-Sample (training)   [OOS] = Out-of-Sample (test, never touched during training)
```

**Use when:** You want to model how a production system with a fixed-memory lookback would perform. Older data is forgotten.

#### Mode 2: Anchored Window

The IS start is fixed at the very beginning of the dataset. Only the IS end advances, so the model always has access to all historical data up to the current point.

```
Total data: 365 days | IS_initial=180 | OOS=60 | step=30

Iter 1:  [IS (0-179)            ][OOS (180-239)]
Iter 2:  [IS (0-209)                    ][OOS (210-269)]
Iter 3:  [IS (0-239)                            ][OOS (240-299)]
Iter 4:  [IS (0-269)                                    ][OOS (270-329)]
```

**Use when:** Your strategy retrains periodically with all available history (e.g., monthly refit). IS windows grow larger over time.

---

### Execution Flow

```
generate_windows(data)
        │
        ▼
for each (is_slice, oos_slice):
        │
        ├─► [1] OPTIMIZE on is_slice
        │       - Grid search / genetic algorithm over EMA, RSI, ADX params
        │       - Select best parameter set by target_metric (e.g., Sharpe)
        │
        ├─► [2] BACKTEST on oos_slice  (params FROZEN — no changes)
        │       - Run strategy with IS-optimized parameters
        │       - Record: return, Sharpe, max drawdown, profit factor, # trades
        │
        └─► [3] RECORD results
                - If OOS metrics >> IS metrics: likely lucky period (suspect)
                - If OOS metrics << IS metrics: overfitting detected (suspect)
                - Consistent IS ≈ OOS: robust strategy
```

### Robustness Criteria

After all windows complete, evaluate:

| Metric | Healthy Signal | Warning Sign |
|---|---|---|
| OOS Sharpe / IS Sharpe | 0.60 – 1.00 | < 0.50 (overfitting) |
| % of OOS windows profitable | > 60% | < 50% |
| OOS Max Drawdown | ≤ IS Max Drawdown | Significantly worse |
| OOS Trade Count | ≥ `min_trades_per_window` | Below — discard window |

### Key Parameters (from `config.yaml`)

```yaml
validator:
  mode: sliding                  # "sliding" | "anchored"
  in_sample_days: 180            # IS window length
  out_of_sample_days: 60         # OOS window length
  step_days: 30                  # Window advance per iteration
  min_trades_per_window: 20      # Minimum trades for statistical validity
  target_metric: sharpe          # IS optimization target
```

### Minimum Data Requirements

```
Sliding:  total_data ≥ in_sample_days + out_of_sample_days
Anchored: same requirement for first window

Recommended minimum: 3× the IS window to get at least 3 OOS windows.
With IS=180, OOS=60, step=30: need 365+ days for 4 OOS windows.
```

---

## 2. JSON Logging Schema

### Design Goals

1. **Append-only JSONL format** — each event is one line; no file locking needed
2. **Self-describing entries** — every line has full context; no join needed
3. **Dashboard-friendly** — Dashboard reads the last N lines of `trades.jsonl`; `session.json` holds live state
4. **Grep-able** — `grep "CIRCUIT_BREAKER" logs/trades.jsonl` works immediately

---

### Master Event Schema

Every log entry follows this structure:

```json
{
  "timestamp":  "2024-01-15T10:32:11.421Z",
  "event_type": "<EVENT_TYPE>",
  "symbol":     "BTC/USDT | null",
  "side":       "buy | sell | null",
  "quantity":   0.1,
  "price":      50000.0,
  "stop_loss":  49000.0,
  "pnl_usd":    null,
  "reason":     "Human-readable context string",
  "metadata":   {}
}
```

---

### Event Type Reference

#### `SIGNAL`
Fired when the engine detects a potential trade signal (before risk checks).

```json
{
  "timestamp": "2024-01-15T10:30:00.000Z",
  "event_type": "SIGNAL",
  "symbol": "BTC/USDT",
  "side": "buy",
  "quantity": null,
  "price": 50000.0,
  "stop_loss": null,
  "pnl_usd": null,
  "reason": "EMA crossover confirmed, RSI=58, ADX=32",
  "metadata": {
    "ema_fast": 49800.0,
    "ema_slow": 49500.0,
    "rsi": 58.2,
    "adx": 32.1
  }
}
```

#### `TRADE_REJECTED`
Fired when a signal passes detection but fails a risk gate.

```json
{
  "timestamp": "2024-01-15T10:30:01.100Z",
  "event_type": "TRADE_REJECTED",
  "symbol": "ETH/USDT",
  "side": "buy",
  "quantity": null,
  "price": 3100.0,
  "stop_loss": null,
  "pnl_usd": null,
  "reason": "Correlation filter blocked: ETH/USDT corr with BTC/USDT = 0.95",
  "metadata": {
    "reject_reason": "correlation_filter",
    "correlated_symbol": "BTC/USDT",
    "correlation_value": 0.95,
    "threshold": 0.90
  }
}
```

Possible `reject_reason` values:
- `correlation_filter` — correlated asset already held
- `circuit_breaker` — daily drawdown limit hit
- `adx_filter` — market in choppy regime (ADX too low)
- `cooldown` — cooldown timer not expired
- `max_positions` — maximum simultaneous positions reached
- `rsi_filter` — RSI in overbought/oversold zone

#### `ORDER_PLACED`
Fired immediately when an order is sent to the exchange.

```json
{
  "timestamp": "2024-01-15T10:30:02.200Z",
  "event_type": "ORDER_PLACED",
  "symbol": "BTC/USDT",
  "side": "buy",
  "quantity": 0.1,
  "price": 50000.0,
  "stop_loss": 49000.0,
  "pnl_usd": null,
  "reason": null,
  "metadata": {
    "order_id": "ORD-001",
    "order_type": "market",
    "sl_order_id": "SL-001"
  }
}
```

#### `ORDER_FILLED`
Fired when the exchange confirms a fill.

```json
{
  "timestamp": "2024-01-15T10:30:02.850Z",
  "event_type": "ORDER_FILLED",
  "symbol": "BTC/USDT",
  "side": "buy",
  "quantity": 0.1,
  "price": 50012.0,
  "stop_loss": 49012.0,
  "pnl_usd": null,
  "reason": null,
  "metadata": {
    "order_id": "ORD-001",
    "slippage_pct": 0.024,
    "fill_timestamp_exchange": 1705311002843
  }
}
```

#### `STOP_UPDATED`
Fired when the trailing stop ratchets to a new, higher price.

```json
{
  "timestamp": "2024-01-15T11:00:00.000Z",
  "event_type": "STOP_UPDATED",
  "symbol": "BTC/USDT",
  "side": null,
  "quantity": 0.1,
  "price": 51000.0,
  "stop_loss": 49980.0,
  "pnl_usd": null,
  "reason": "Trailing stop updated: new high 51000.0, trail=2.0%",
  "metadata": {
    "previous_stop": 49000.0,
    "new_stop": 49980.0,
    "current_price": 51000.0,
    "trail_pct": 2.0
  }
}
```

#### `POSITION_CLOSED`
Fired when a position is fully closed (stop hit or manual close).

```json
{
  "timestamp": "2024-01-15T14:15:33.000Z",
  "event_type": "POSITION_CLOSED",
  "symbol": "BTC/USDT",
  "side": "sell",
  "quantity": 0.1,
  "price": 49980.0,
  "stop_loss": null,
  "pnl_usd": -2.0,
  "reason": "Trailing stop triggered",
  "metadata": {
    "entry_price": 50012.0,
    "exit_price": 49980.0,
    "hold_duration_minutes": 225,
    "close_type": "stop_loss"
  }
}
```

#### `CIRCUIT_BREAKER_TRIGGERED`
Fired once when the daily drawdown limit is breached.

```json
{
  "timestamp": "2024-01-15T15:00:00.000Z",
  "event_type": "CIRCUIT_BREAKER_TRIGGERED",
  "symbol": null,
  "side": null,
  "quantity": null,
  "price": null,
  "stop_loss": null,
  "pnl_usd": -310.0,
  "reason": "Daily drawdown limit reached: 3.10% >= 3.00% threshold",
  "metadata": {
    "equity_start": 10000.0,
    "equity_current": 9690.0,
    "drawdown_pct": 3.10,
    "limit_pct": 3.00,
    "reset_time_utc": "00:00"
  }
}
```

#### `ERROR`
Fired on any unhandled exception in the trading loop.

```json
{
  "timestamp": "2024-01-15T16:00:00.000Z",
  "event_type": "ERROR",
  "symbol": "BTC/USDT",
  "side": null,
  "quantity": null,
  "price": null,
  "stop_loss": null,
  "pnl_usd": null,
  "reason": "Exchange API timeout after 10s",
  "metadata": {
    "exception_type": "ccxt.NetworkError",
    "traceback_snippet": "...",
    "retry_count": 3
  }
}
```

---

### How the Dashboard Reads the Log

```python
# dashboard.py — simplified reader
import json

def load_recent_trades(log_path: str, n: int = 20) -> list[dict]:
    """Read last N trade events from the JSONL log."""
    with open(log_path) as f:
        lines = f.readlines()
    return [json.loads(line) for line in lines[-n:] if line.strip()]

def load_session_state(session_path: str) -> dict:
    """Read current live state from session.json."""
    with open(session_path) as f:
        return json.load(f)
```

### session.json — Live State Structure

```json
{
  "session_id": "2024-01-15T08:00:00Z",
  "equity_start": 10000.00,
  "equity_current": 9850.00,
  "daily_pnl_usd": -150.00,
  "daily_drawdown_pct": 1.50,
  "circuit_breaker_active": false,
  "open_positions": {
    "BTC/USDT": {
      "side": "buy",
      "quantity": 0.1,
      "entry_price": 50012.0,
      "current_stop": 49980.0,
      "unrealized_pnl_usd": -3.20,
      "opened_at": "2024-01-15T10:30:02Z"
    }
  },
  "trade_count_today": 2,
  "last_updated": "2024-01-15T14:00:00Z"
}
```

---

*Document maintained by the QuantBot Pro architecture team. Update this file whenever a new event type is added to the engine.*
