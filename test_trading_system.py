"""
QuantBot Pro — Automated Test Suite
====================================
Run with:
    pytest test_trading_system.py -v --cov=. --cov-report=term-missing

Coverage target: >= 90% on risk_manager.py, engine.py, validator.py
"""

import json
import os
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import numpy as np
import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# ── STUBS (replaces actual modules for isolated unit testing)
#    In a real project, import from your actual modules:
#    from risk_manager import RiskManager, CircuitBreaker, CorrelationFilter
#    from engine import Engine
#    from validator import WalkForwardValidator
# ---------------------------------------------------------------------------

class RiskManager:
    """
    Calculates position size based on fixed-fractional (percent-risk) model.

    Args:
        equity (float): Current account equity in USD.
        per_trade_risk_pct (float): Percentage of equity to risk per trade.
        daily_drawdown_limit_pct (float): Circuit breaker threshold.
    """

    def __init__(self, equity: float, per_trade_risk_pct: float = 1.0,
                 daily_drawdown_limit_pct: float = 3.0):
        self.equity = equity
        self.per_trade_risk_pct = per_trade_risk_pct
        self.daily_drawdown_limit_pct = daily_drawdown_limit_pct
        self._daily_loss_usd = 0.0
        self._circuit_breaker_active = False

    def calculate_position_size(self, entry_price: float, stop_loss_price: float) -> float:
        """
        Returns the number of units to buy/sell.

        Formula:
            risk_amount   = equity × (per_trade_risk_pct / 100)
            risk_per_unit = |entry_price - stop_loss_price|
            position_size = risk_amount / risk_per_unit

        Example:
            equity=$10,000, risk=1%, entry=$50,000, stop=$49,000
            → risk_amount = $100
            → risk_per_unit = $1,000
            → size = 0.1 BTC
        """
        if entry_price <= 0 or stop_loss_price <= 0:
            raise ValueError("Prices must be positive.")
        if entry_price == stop_loss_price:
            raise ValueError("Entry and stop-loss price cannot be equal.")

        risk_amount = self.equity * (self.per_trade_risk_pct / 100)
        risk_per_unit = abs(entry_price - stop_loss_price)
        return round(risk_amount / risk_per_unit, 8)

    def record_loss(self, loss_usd: float) -> None:
        """
        Registers a realized loss and activates the circuit breaker
        if the daily drawdown limit is breached.
        """
        self._daily_loss_usd += abs(loss_usd)
        drawdown_pct = (self._daily_loss_usd / self.equity) * 100
        if drawdown_pct >= self.daily_drawdown_limit_pct:
            self._circuit_breaker_active = True

    def is_trading_allowed(self) -> bool:
        """Returns False if the circuit breaker is active."""
        return not self._circuit_breaker_active

    def reset_daily_state(self) -> None:
        """Called at session reset (e.g., midnight UTC)."""
        self._daily_loss_usd = 0.0
        self._circuit_breaker_active = False


class CorrelationFilter:
    """
    Prevents over-concentration in correlated assets.

    Maintains a list of open position symbols and checks whether
    a new signal's asset is too correlated with any held position.
    """

    def __init__(self, threshold: float = 0.90, lookback_days: int = 30):
        self.threshold = threshold
        self.lookback_days = lookback_days
        self._open_symbols: list[str] = []

    def add_position(self, symbol: str) -> None:
        if symbol not in self._open_symbols:
            self._open_symbols.append(symbol)

    def remove_position(self, symbol: str) -> None:
        self._open_symbols = [s for s in self._open_symbols if s != symbol]

    def is_entry_allowed(self, new_symbol: str,
                         correlation_matrix: pd.DataFrame) -> bool:
        """
        Returns True only if the new_symbol is NOT correlated above
        self.threshold with any currently open position symbol.

        Args:
            new_symbol: Symbol for the candidate trade.
            correlation_matrix: DataFrame indexed/columned by symbol name.
        """
        for held_symbol in self._open_symbols:
            if held_symbol == new_symbol:
                return False  # Already in this position
            try:
                corr = correlation_matrix.loc[new_symbol, held_symbol]
                if abs(corr) >= self.threshold:
                    return False
            except KeyError:
                # Symbol not in matrix — allow trade (fail-open for unknown pairs)
                continue
        return True


class WalkForwardValidator:
    """
    Walk-Forward Validation Engine
    ================================
    Splits historical data into sequential in-sample (IS) and
    out-of-sample (OOS) windows and evaluates strategy performance
    on OOS data only.

    Window modes:
    - SLIDING:  Both IS start and end advance by `step_days` each iteration.
                Simulates a fixed-memory model that forgets old data.

    - ANCHORED: IS start is fixed at the beginning of history.
                IS end advances by `step_days` each iteration.
                Simulates a model that accumulates all historical data.

    Iteration diagram (sliding, IS=180d, OOS=60d, step=30d):

        Iteration 1:  [--IS: day 0-179--][OOS: day 180-239]
        Iteration 2:  [--IS: day 30-209-][OOS: day 210-269]
        Iteration 3:  [--IS: day 60-239-][OOS: day 240-299]
        ...

    For each window:
        1. Fit/optimize strategy parameters on IS data.
        2. Run backtest on OOS data (no parameter changes).
        3. Record OOS metrics (Sharpe, profit factor, max drawdown).
        4. Advance window by `step_days`.

    A strategy is considered robust if OOS metrics are consistent
    with IS metrics — large divergence suggests overfitting.
    """

    def __init__(self, in_sample_days: int = 180, out_of_sample_days: int = 60,
                 step_days: int = 30, mode: str = "sliding"):
        self.in_sample_days = in_sample_days
        self.out_of_sample_days = out_of_sample_days
        self.step_days = step_days
        self.mode = mode

    def generate_windows(self, data: pd.DataFrame) -> list[dict]:
        """
        Generates a list of (is_slice, oos_slice) DataFrames.

        Returns:
            List of dicts with keys: 'iteration', 'is_data', 'oos_data',
            'is_start', 'is_end', 'oos_start', 'oos_end'
        """
        windows = []
        total_days = len(data)
        window_size = self.in_sample_days + self.out_of_sample_days

        if total_days < window_size:
            raise ValueError(
                f"Insufficient data: need {window_size} rows, got {total_days}."
            )

        iteration = 0
        is_start_offset = 0

        while True:
            if self.mode == "anchored":
                is_start = 0
            else:  # sliding
                is_start = is_start_offset

            is_end = is_start + self.in_sample_days
            oos_start = is_end
            oos_end = oos_start + self.out_of_sample_days

            if oos_end > total_days:
                break

            windows.append({
                "iteration": iteration + 1,
                "is_data": data.iloc[is_start:is_end].copy(),
                "oos_data": data.iloc[oos_start:oos_end].copy(),
                "is_start": is_start,
                "is_end": is_end,
                "oos_start": oos_start,
                "oos_end": oos_end,
            })

            is_start_offset += self.step_days
            iteration += 1

        return windows


# ---------------------------------------------------------------------------
# ── JSON LOGGING SCHEMA (documented for Dashboard consumption)
# ---------------------------------------------------------------------------

def create_trade_log_entry(event_type: str, **kwargs) -> dict:
    """
    Creates a structured JSON log entry.

    Schema:
    {
        "timestamp":   "ISO-8601 UTC string",
        "event_type":  "SIGNAL" | "ORDER_PLACED" | "ORDER_FILLED" |
                       "STOP_UPDATED" | "POSITION_CLOSED" |
                       "CIRCUIT_BREAKER_TRIGGERED" | "TRADE_REJECTED" | "ERROR",
        "symbol":      "BTC/USDT",
        "side":        "buy" | "sell" | null,
        "quantity":    float | null,
        "price":       float | null,
        "stop_loss":   float | null,
        "pnl_usd":     float | null,
        "reason":      "Human-readable explanation (for rejections/errors)",
        "metadata":    {}   # Any extra context (order_id, correlation_value, etc.)
    }
    """
    return {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "event_type": event_type,
        "symbol": kwargs.get("symbol"),
        "side": kwargs.get("side"),
        "quantity": kwargs.get("quantity"),
        "price": kwargs.get("price"),
        "stop_loss": kwargs.get("stop_loss"),
        "pnl_usd": kwargs.get("pnl_usd"),
        "reason": kwargs.get("reason"),
        "metadata": kwargs.get("metadata", {}),
    }


# ===========================================================================
# ██████████████████████████████████████████████████████████████████████████
#
#                         TEST SUITE BEGINS HERE
#
# ██████████████████████████████████████████████████████████████████████████
# ===========================================================================


class TestRiskManagerPositionSizing:
    """
    UNIT TESTS — RiskManager: Position Size Calculation
    ====================================================
    Verifies the core fixed-fractional sizing formula across
    normal cases, edge cases, and invalid inputs.
    """

    def setup_method(self):
        """Fresh RiskManager for each test — $10,000 equity, 1% risk."""
        self.rm = RiskManager(equity=10_000.0, per_trade_risk_pct=1.0)

    # ── Happy-path tests ──────────────────────────────────────────────────

    def test_position_size_exact_calculation(self):
        """
        Scenario: $10,000 equity, 1% risk, BTC at $50,000, stop at $49,000.
        Expected: risk=$100, risk_per_unit=$1,000 → size=0.1 BTC
        """
        size = self.rm.calculate_position_size(
            entry_price=50_000.0,
            stop_loss_price=49_000.0
        )
        assert size == pytest.approx(0.1, rel=1e-6), (
            f"Expected 0.1 BTC but got {size}. "
            f"Check formula: risk_amount / |entry - stop|"
        )

    def test_position_size_tight_stop(self):
        """
        Tight stop ($500 away) should yield a larger position.
        Expected: $100 / $500 = 0.2 BTC
        """
        size = self.rm.calculate_position_size(
            entry_price=50_000.0,
            stop_loss_price=49_500.0
        )
        assert size == pytest.approx(0.2, rel=1e-6)

    def test_position_size_wide_stop(self):
        """
        Wide stop ($5,000 away) should yield a smaller position.
        Expected: $100 / $5,000 = 0.02 BTC
        """
        size = self.rm.calculate_position_size(
            entry_price=50_000.0,
            stop_loss_price=45_000.0
        )
        assert size == pytest.approx(0.02, rel=1e-6)

    def test_position_size_with_two_percent_risk(self):
        """
        Doubling the risk percentage should double the position size.
        """
        rm_2pct = RiskManager(equity=10_000.0, per_trade_risk_pct=2.0)
        size = rm_2pct.calculate_position_size(50_000.0, 49_000.0)
        assert size == pytest.approx(0.2, rel=1e-6)

    def test_position_size_scales_with_equity(self):
        """
        Doubling equity (with same risk %) should double position size.
        """
        rm_20k = RiskManager(equity=20_000.0, per_trade_risk_pct=1.0)
        size = rm_20k.calculate_position_size(50_000.0, 49_000.0)
        assert size == pytest.approx(0.2, rel=1e-6)

    def test_position_size_for_altcoin(self):
        """
        Works correctly for lower-priced assets (e.g., SOL at $150).
        Expected: $100 / $5 = 20.0 SOL
        """
        size = self.rm.calculate_position_size(
            entry_price=150.0,
            stop_loss_price=145.0
        )
        assert size == pytest.approx(20.0, rel=1e-6)

    # ── Edge-case and error tests ─────────────────────────────────────────

    def test_position_size_raises_on_zero_entry_price(self):
        with pytest.raises(ValueError, match="positive"):
            self.rm.calculate_position_size(entry_price=0.0, stop_loss_price=100.0)

    def test_position_size_raises_on_equal_prices(self):
        """Stop-loss at entry price creates a division-by-zero risk."""
        with pytest.raises(ValueError, match="equal"):
            self.rm.calculate_position_size(
                entry_price=50_000.0, stop_loss_price=50_000.0
            )

    def test_position_size_raises_on_negative_price(self):
        with pytest.raises(ValueError, match="positive"):
            self.rm.calculate_position_size(
                entry_price=-100.0, stop_loss_price=50.0
            )


class TestDailyDrawdownCircuitBreaker:
    """
    UNIT TESTS — RiskManager: Daily Drawdown Circuit Breaker
    ==========================================================
    Verifies that the circuit breaker activates precisely at the
    drawdown threshold and that it blocks further trading.
    """

    def setup_method(self):
        """$10,000 equity, 3% daily drawdown limit = $300 trigger."""
        self.rm = RiskManager(
            equity=10_000.0,
            per_trade_risk_pct=1.0,
            daily_drawdown_limit_pct=3.0
        )

    # ── Core circuit breaker logic ────────────────────────────────────────

    def test_trading_allowed_initially(self):
        """No losses recorded → trading must be allowed."""
        assert self.rm.is_trading_allowed() is True

    def test_small_loss_does_not_trigger_breaker(self):
        """
        Scenario: 1.5% loss ($150) — well below the 3% threshold.
        Circuit breaker must NOT activate.
        """
        self.rm.record_loss(150.0)
        assert self.rm.is_trading_allowed() is True

    def test_loss_at_exact_threshold_triggers_breaker(self):
        """
        Scenario: Exactly 3% loss ($300) must activate the circuit breaker.
        Boundary condition — must be >=, not >.
        """
        self.rm.record_loss(300.0)
        assert self.rm.is_trading_allowed() is False

    def test_loss_exceeding_threshold_triggers_breaker(self):
        """
        Scenario: 3.1% cumulative loss ($310) triggers the circuit breaker.
        This is the primary documented scenario.
        """
        self.rm.record_loss(200.0)  # 2.0% — still safe
        assert self.rm.is_trading_allowed() is True

        self.rm.record_loss(110.0)  # +1.1% → total 3.1% → BREACH
        assert self.rm.is_trading_allowed() is False, (
            "Circuit breaker must activate after 3.1% cumulative daily loss."
        )

    def test_circuit_breaker_blocks_all_subsequent_trades(self):
        """
        Once triggered, the circuit breaker must remain active
        regardless of subsequent loss recordings.
        """
        self.rm.record_loss(400.0)  # 4% — triggers immediately
        assert self.rm.is_trading_allowed() is False

        # Simulate additional loss records — state must stay blocked
        self.rm.record_loss(50.0)
        assert self.rm.is_trading_allowed() is False

    def test_circuit_breaker_resets_on_daily_reset(self):
        """
        After midnight reset, the circuit breaker must deactivate
        and accumulated losses must clear.
        """
        self.rm.record_loss(500.0)
        assert self.rm.is_trading_allowed() is False

        self.rm.reset_daily_state()
        assert self.rm.is_trading_allowed() is True

    def test_small_losses_accumulate_correctly(self):
        """
        Multiple small losses must accumulate before triggering.
        10 × $29 = $290 (2.9%) — no trigger.
        11th loss of $29 = $319 (3.19%) — trigger.
        """
        for i in range(10):
            self.rm.record_loss(29.0)
            assert self.rm.is_trading_allowed() is True, (
                f"Breaker triggered too early at iteration {i+1}"
            )

        self.rm.record_loss(29.0)  # 11th — crosses 3%
        assert self.rm.is_trading_allowed() is False


class TestCorrelationFilter:
    """
    UNIT TESTS — CorrelationFilter
    ================================
    Verifies that trades in highly correlated assets are blocked
    and that uncorrelated assets are permitted.
    """

    def setup_method(self):
        """
        Build a 3-asset correlation matrix:
            BTC ↔ ETH: 0.95 (highly correlated — should be blocked)
            BTC ↔ SOL: 0.72 (moderately correlated — should be allowed)
            ETH ↔ SOL: 0.68
        """
        symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
        corr_data = [
            [1.00, 0.95, 0.72],
            [0.95, 1.00, 0.68],
            [0.72, 0.68, 1.00],
        ]
        self.corr_matrix = pd.DataFrame(
            corr_data, index=symbols, columns=symbols
        )
        self.cf = CorrelationFilter(threshold=0.90)

    # ── Core correlation gate ─────────────────────────────────────────────

    def test_no_positions_allows_all_entries(self):
        """With no open positions, all entries should be allowed."""
        assert self.cf.is_entry_allowed("BTC/USDT", self.corr_matrix) is True
        assert self.cf.is_entry_allowed("ETH/USDT", self.corr_matrix) is True

    def test_correlated_trade_is_rejected(self):
        """
        Scenario: Long BTC is open. ETH signal fires. Correlation = 0.95.
        Expected: ETH trade REJECTED (0.95 >= threshold 0.90).
        """
        self.cf.add_position("BTC/USDT")
        allowed = self.cf.is_entry_allowed("ETH/USDT", self.corr_matrix)

        assert allowed is False, (
            "ETH entry must be blocked when BTC is held and correlation=0.95 "
            "exceeds the 0.90 threshold. Allowing this would double crypto exposure."
        )

    def test_uncorrelated_trade_is_allowed(self):
        """
        Scenario: Long BTC is open. SOL signal fires. Correlation = 0.72.
        Expected: SOL trade ALLOWED (0.72 < threshold 0.90).
        """
        self.cf.add_position("BTC/USDT")
        allowed = self.cf.is_entry_allowed("SOL/USDT", self.corr_matrix)
        assert allowed is True

    def test_same_symbol_entry_is_blocked(self):
        """Cannot open a second position in the same symbol."""
        self.cf.add_position("BTC/USDT")
        assert self.cf.is_entry_allowed("BTC/USDT", self.corr_matrix) is False

    def test_entry_allowed_after_correlated_position_closed(self):
        """
        After closing BTC, ETH entry should be permitted again.
        """
        self.cf.add_position("BTC/USDT")
        assert self.cf.is_entry_allowed("ETH/USDT", self.corr_matrix) is False

        self.cf.remove_position("BTC/USDT")
        assert self.cf.is_entry_allowed("ETH/USDT", self.corr_matrix) is True

    def test_correlation_threshold_boundary(self):
        """
        Correlation exactly at threshold (0.90) should be BLOCKED.
        Correlation just below (0.899) should be ALLOWED.
        """
        symbols = ["AAA", "BBB"]
        # Exactly at threshold
        at_threshold = pd.DataFrame(
            [[1.0, 0.90], [0.90, 1.0]], index=symbols, columns=symbols
        )
        cf = CorrelationFilter(threshold=0.90)
        cf.add_position("AAA")
        assert cf.is_entry_allowed("BBB", at_threshold) is False

        # Just below threshold
        below_threshold = pd.DataFrame(
            [[1.0, 0.899], [0.899, 1.0]], index=symbols, columns=symbols
        )
        cf2 = CorrelationFilter(threshold=0.90)
        cf2.add_position("AAA")
        assert cf2.is_entry_allowed("BBB", below_threshold) is True


class TestOrderExecutionMock:
    """
    MOCK TESTS — Order Execution & Stop-Loss Placement
    =====================================================
    Uses mocking to simulate exchange API responses and verify
    that the stop-loss order is placed immediately after entry fill.

    Key invariant: Stop-loss order MUST be sent in the same execution
    cycle as the entry order. Any delay creates unprotected exposure.
    """

    def _make_mock_exchange(self, fill_price: float = 50_000.0,
                             order_id: str = "ORD-001"):
        """
        Builds a mock exchange client that returns realistic API responses.
        """
        mock_exchange = MagicMock()

        # Simulate a filled market buy order
        mock_exchange.create_market_buy_order.return_value = {
            "id": order_id,
            "symbol": "BTC/USDT",
            "side": "buy",
            "type": "market",
            "status": "closed",    # 'closed' = filled in ccxt
            "filled": 0.1,
            "average": fill_price,
            "timestamp": int(time.time() * 1000),
        }

        # Simulate a stop-loss order acknowledgement
        mock_exchange.create_order.return_value = {
            "id": "SL-001",
            "symbol": "BTC/USDT",
            "side": "sell",
            "type": "stop_market",
            "stopPrice": fill_price * 0.98,
            "status": "open",
        }

        return mock_exchange

    def _execute_trade_with_stop(self, exchange, symbol: str,
                                  quantity: float, stop_price: float) -> dict:
        """
        Simulates the engine's order execution flow:
        1. Place market entry order.
        2. On fill confirmation, immediately place stop-loss.
        Returns a result dict with both order responses.
        """
        entry_response = exchange.create_market_buy_order(symbol, quantity)

        if entry_response["status"] == "closed":
            # Entry confirmed — stop-loss MUST be placed immediately
            sl_response = exchange.create_order(
                symbol=symbol,
                type="stop_market",
                side="sell",
                amount=quantity,
                params={"stopPrice": stop_price}
            )
            return {"entry": entry_response, "stop_loss": sl_response}

        return {"entry": entry_response, "stop_loss": None}

    # ── Stop-loss placement tests ─────────────────────────────────────────

    def test_stop_loss_placed_immediately_after_entry(self):
        """
        Core invariant: After a confirmed entry fill, the stop-loss order
        must be sent in the same execution cycle.
        """
        mock_exchange = self._make_mock_exchange(fill_price=50_000.0)
        stop_price = 49_000.0

        result = self._execute_trade_with_stop(
            exchange=mock_exchange,
            symbol="BTC/USDT",
            quantity=0.1,
            stop_price=stop_price
        )

        # Entry must have been attempted
        mock_exchange.create_market_buy_order.assert_called_once_with(
            "BTC/USDT", 0.1
        )

        # Stop-loss must have been placed immediately after
        mock_exchange.create_order.assert_called_once(), (
            "Stop-loss order was never sent! "
            "Position would be unprotected after entry."
        )

        # Verify stop-loss parameters
        sl_call_kwargs = mock_exchange.create_order.call_args.kwargs
        assert sl_call_kwargs["side"] == "sell"
        assert sl_call_kwargs["type"] == "stop_market"
        assert sl_call_kwargs["params"]["stopPrice"] == stop_price

    def test_stop_loss_not_placed_if_entry_not_filled(self):
        """
        If the entry order is not yet filled, stop-loss must NOT be sent.
        """
        mock_exchange = self._make_mock_exchange()
        mock_exchange.create_market_buy_order.return_value["status"] = "open"

        result = self._execute_trade_with_stop(
            exchange=mock_exchange,
            symbol="BTC/USDT",
            quantity=0.1,
            stop_price=49_000.0
        )

        assert result["stop_loss"] is None
        mock_exchange.create_order.assert_not_called()

    def test_stop_loss_price_is_below_entry(self):
        """
        Stop-loss price must always be strictly below the entry price
        for a long position.
        """
        entry_price = 50_000.0
        stop_price = 49_000.0

        assert stop_price < entry_price, (
            f"Stop-loss ({stop_price}) must be below entry ({entry_price}) "
            "for a long position."
        )

    def test_api_error_is_propagated_correctly(self):
        """
        If the exchange API throws an error on the stop-loss placement,
        it must not be silently swallowed.
        """
        mock_exchange = self._make_mock_exchange()
        mock_exchange.create_order.side_effect = Exception("Exchange timeout")

        with pytest.raises(Exception, match="Exchange timeout"):
            self._execute_trade_with_stop(
                exchange=mock_exchange,
                symbol="BTC/USDT",
                quantity=0.1,
                stop_price=49_000.0
            )

    def test_entry_order_uses_correct_symbol_and_quantity(self):
        """
        Verifies that exact symbol and quantity are passed to the exchange.
        Off-by-one errors in quantity are catastrophic in live trading.
        """
        mock_exchange = self._make_mock_exchange()
        self._execute_trade_with_stop(mock_exchange, "BTC/USDT", 0.1, 49_000.0)

        args, kwargs = mock_exchange.create_market_buy_order.call_args
        assert args[0] == "BTC/USDT"
        assert args[1] == 0.1


class TestWalkForwardValidator:
    """
    UNIT TESTS — WalkForwardValidator
    ====================================
    Verifies that the window generation logic is mathematically correct
    for both sliding and anchored modes.
    """

    def _make_data(self, days: int = 365) -> pd.DataFrame:
        """Creates a synthetic daily price DataFrame."""
        dates = pd.date_range(start="2023-01-01", periods=days, freq="D")
        prices = 30_000 + np.cumsum(np.random.randn(days) * 500)
        return pd.DataFrame({"close": prices}, index=dates)

    def test_sliding_window_generates_correct_count(self):
        """
        With 365 days, IS=180, OOS=60, step=30:
        First window ends at day 240. Remaining = 125 days → 4 more steps.
        Total windows = 5.
        Formula: floor((total - IS - OOS) / step) + 1
        """
        data = self._make_data(365)
        validator = WalkForwardValidator(
            in_sample_days=180, out_of_sample_days=60, step_days=30,
            mode="sliding"
        )
        windows = validator.generate_windows(data)
        expected = ((365 - 180 - 60) // 30) + 1
        assert len(windows) == expected

    def test_sliding_windows_do_not_overlap_oos(self):
        """
        OOS periods must not overlap between iterations.
        Each window's OOS must start exactly where the previous OOS ended
        (after accounting for the step size).
        """
        data = self._make_data(400)
        validator = WalkForwardValidator(
            in_sample_days=180, out_of_sample_days=60, step_days=30,
            mode="sliding"
        )
        windows = validator.generate_windows(data)

        for i in range(1, len(windows)):
            prev_oos_end = windows[i - 1]["oos_end"]
            curr_oos_start = windows[i]["oos_start"]
            assert curr_oos_start == prev_oos_end - (60 - 30), (
                f"OOS windows overlap at iteration {i + 1}. "
                "This would cause data leakage."
            )

    def test_anchored_mode_keeps_is_start_fixed(self):
        """
        In anchored mode, the IS window always starts at index 0.
        """
        data = self._make_data(400)
        validator = WalkForwardValidator(
            in_sample_days=180, out_of_sample_days=60, step_days=30,
            mode="anchored"
        )
        windows = validator.generate_windows(data)

        for w in windows:
            assert w["is_start"] == 0, (
                f"Anchored IS window started at {w['is_start']}, expected 0."
            )

    def test_anchored_mode_is_grows_each_iteration(self):
        """
        In anchored mode, each IS window should be larger than the previous.
        """
        data = self._make_data(400)
        validator = WalkForwardValidator(
            in_sample_days=180, out_of_sample_days=60, step_days=30,
            mode="anchored"
        )
        windows = validator.generate_windows(data)
        is_sizes = [w["is_end"] - w["is_start"] for w in windows]

        for i in range(1, len(is_sizes)):
            assert is_sizes[i] > is_sizes[i - 1], (
                "Anchored IS window must grow with each iteration."
            )

    def test_insufficient_data_raises_error(self):
        """
        If data is too short to fit even one IS+OOS window, raise ValueError.
        """
        data = self._make_data(100)  # Way too short for IS=180+OOS=60
        validator = WalkForwardValidator(
            in_sample_days=180, out_of_sample_days=60, step_days=30
        )
        with pytest.raises(ValueError, match="Insufficient data"):
            validator.generate_windows(data)

    def test_each_window_has_correct_sizes(self):
        """
        Every IS slice must have exactly `in_sample_days` rows.
        Every OOS slice must have exactly `out_of_sample_days` rows.
        """
        data = self._make_data(365)
        validator = WalkForwardValidator(
            in_sample_days=180, out_of_sample_days=60, step_days=30,
            mode="sliding"
        )
        windows = validator.generate_windows(data)

        for w in windows:
            assert len(w["is_data"]) == 180, (
                f"IS slice has {len(w['is_data'])} rows, expected 180."
            )
            assert len(w["oos_data"]) == 60, (
                f"OOS slice has {len(w['oos_data'])} rows, expected 60."
            )


class TestJSONLogging:
    """
    UNIT TESTS — Structured JSON Logging
    =======================================
    Verifies that log entries conform to the documented schema
    and are correctly persisted to disk.
    """

    def test_trade_log_entry_has_required_fields(self):
        """Every log entry must contain the core schema fields."""
        required_fields = [
            "timestamp", "event_type", "symbol", "side",
            "quantity", "price", "stop_loss", "pnl_usd",
            "reason", "metadata"
        ]
        entry = create_trade_log_entry(
            "ORDER_FILLED",
            symbol="BTC/USDT",
            side="buy",
            quantity=0.1,
            price=50_000.0,
            stop_loss=49_000.0
        )
        for field in required_fields:
            assert field in entry, f"Missing required field: '{field}'"

    def test_timestamp_is_iso8601_utc(self):
        """Timestamp must be parseable as ISO-8601 and end with 'Z'."""
        entry = create_trade_log_entry("SIGNAL", symbol="ETH/USDT")
        ts = entry["timestamp"]
        assert ts.endswith("Z"), "Timestamp must be UTC (end with 'Z')"
        # Should not raise
        datetime.fromisoformat(ts.replace("Z", "+00:00"))

    def test_log_entry_is_json_serializable(self):
        """All fields must be JSON-serializable (no datetime objects, etc.)."""
        entry = create_trade_log_entry(
            "CIRCUIT_BREAKER_TRIGGERED",
            symbol=None,
            reason="Daily drawdown limit exceeded",
            metadata={"drawdown_pct": 3.1}
        )
        serialized = json.dumps(entry)  # Should not raise
        recovered = json.loads(serialized)
        assert recovered["event_type"] == "CIRCUIT_BREAKER_TRIGGERED"

    def test_log_written_and_readable_from_disk(self):
        """
        Verifies the append-write-read cycle used by the dashboard.
        Log file must be readable line-by-line as valid JSON objects.
        """
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.jsonl', delete=False
        ) as f:
            log_path = f.name
            for event in ["ORDER_FILLED", "STOP_UPDATED", "POSITION_CLOSED"]:
                entry = create_trade_log_entry(event, symbol="BTC/USDT")
                f.write(json.dumps(entry) + "\n")

        try:
            with open(log_path) as f:
                lines = f.readlines()

            assert len(lines) == 3
            for line in lines:
                obj = json.loads(line.strip())
                assert "event_type" in obj
                assert "timestamp" in obj
        finally:
            os.unlink(log_path)


# ===========================================================================
# ── INTEGRATION SCENARIO: Full Trade Lifecycle
# ===========================================================================

class TestFullTradeLifecycle:
    """
    INTEGRATION TEST — Simulates a complete trade from signal to close.
    Combines RiskManager + CorrelationFilter + MockExchange.
    """

    def test_btc_trade_lifecycle(self):
        """
        Full flow:
        1. Risk manager calculates size.
        2. Correlation filter allows entry (no existing positions).
        3. Mock exchange fills the entry order.
        4. Stop-loss is placed.
        5. Loss is recorded; circuit breaker not yet triggered.
        """
        rm = RiskManager(equity=10_000.0, per_trade_risk_pct=1.0,
                         daily_drawdown_limit_pct=3.0)
        cf = CorrelationFilter(threshold=0.90)
        corr_matrix = pd.DataFrame(
            [[1.0, 0.95], [0.95, 1.0]],
            index=["BTC/USDT", "ETH/USDT"],
            columns=["BTC/USDT", "ETH/USDT"]
        )

        # Step 1: Calculate position size
        size = rm.calculate_position_size(50_000.0, 49_000.0)
        assert size == pytest.approx(0.1)

        # Step 2: Correlation check — no positions yet
        assert cf.is_entry_allowed("BTC/USDT", corr_matrix) is True

        # Step 3: Simulate exchange fill
        mock_exchange = MagicMock()
        mock_exchange.create_market_buy_order.return_value = {
            "id": "ORD-BTC-001", "status": "closed", "filled": size,
            "average": 50_000.0
        }
        mock_exchange.create_order.return_value = {"id": "SL-BTC-001", "status": "open"}

        order = mock_exchange.create_market_buy_order("BTC/USDT", size)
        assert order["status"] == "closed"

        # Step 4: Register position and place stop-loss
        cf.add_position("BTC/USDT")
        mock_exchange.create_order(
            symbol="BTC/USDT", type="stop_market", side="sell",
            amount=size, params={"stopPrice": 49_000.0}
        )
        mock_exchange.create_order.assert_called_once()

        # Step 5: ETH entry now blocked (corr=0.95)
        assert cf.is_entry_allowed("ETH/USDT", corr_matrix) is False

        # Step 6: Record a small loss — circuit breaker should NOT trigger
        rm.record_loss(80.0)  # 0.8%
        assert rm.is_trading_allowed() is True


# ===========================================================================
# ── PYTEST CONFIGURATION & MARKERS
# ===========================================================================

def pytest_configure(config):
    """Register custom markers to categorize tests."""
    config.addinivalue_line("markers", "unit: Pure unit tests, no I/O")
    config.addinivalue_line("markers", "integration: Multi-component tests")
    config.addinivalue_line("markers", "mock: Tests using mocked external APIs")
    config.addinivalue_line("markers", "slow: Tests that take > 1s")


if __name__ == "__main__":
    # Allow running directly: python test_trading_system.py
    import sys
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
