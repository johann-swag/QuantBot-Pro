"""
================================================================================
  DISCIPLINED TREND-FOLLOWING TRADING BOT v2.0
  Strategie: EMA-Crossover + RSI + ADX-Regime + ATR-Trailing-Stop

  NEU in v2.0:
  ✅ Backtesting-Modul       — Historische Validierung vor Live-Einsatz
  ✅ Telegram Notifications  — Echtzeit-Alerts für alle Trade-Events
  ✅ Circuit Breaker         — Tägliches Drawdown-Limit (automatischer Stopp)
  ✅ Reconnect-Logic         — Automatische Wiederverbindung bei Netzausfall
  ✅ Multi-Symbol Support    — Mehrere Paare gleichzeitig handeln
  ✅ Performance Dashboard   — Live-Statistiken im Terminal

  WICHTIG: DRY_RUN = True ist standardmäßig AKTIV.
================================================================================
"""

import os
import sys
import time
import json
import logging
import traceback
import argparse
from datetime import datetime, timezone, date
from pathlib import Path

import ccxt
import pandas as pd
import numpy as np
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# 0. KONFIGURATION
# ============================================================

CONFIG = {
    # --- Modus ---
    "DRY_RUN":              True,

    # --- Exchange ---
    "EXCHANGE_ID":          "binance",
    "MARKET_TYPE":          "spot",

    # --- Symbole (Multi-Symbol) ---
    "SYMBOLS": [
        "BTC/USDT",
        "ETH/USDT",
        "SOL/USDT",
    ],

    # --- Timeframe ---
    "TIMEFRAME":            "4h",

    # --- Risiko (KERN-SICHERHEIT) ---
    "RISK_PER_TRADE_PCT":   0.01,       # 1% pro Trade
    "ATR_MULTIPLIER":       2.0,
    "REWARD_RISK_RATIO":    1.5,
    "MAX_OPEN_TRADES":      3,          # Max. 1 pro Symbol
    "LOSS_COOLDOWN_BARS":   3,

    # --- Circuit Breaker ---
    "DAILY_DRAWDOWN_LIMIT":     0.03,   # Bot stoppt bei -3% Tagesverlust
    "MAX_CONSECUTIVE_LOSSES":   3,      # Stopp nach 3 Verlusten in Folge

    # --- Strategie ---
    "EMA_FAST":             10,
    "EMA_SLOW":             21,
    "RSI_PERIOD":           10,
    "RSI_LONG_MIN":         48,
    "RSI_SHORT_MAX":        52,
    "ATR_PERIOD":           14,
    "ADX_PERIOD":           14,
    "ADX_THRESHOLD":        25,
    "VOLUME_FACTOR":        1.2,

    # --- Reconnect ---
    "MAX_RETRIES":          5,
    "RETRY_DELAY_SEC":      30,

    # --- Technisches ---
    "WARMUP_CANDLES":       200,
    "LOOP_INTERVAL_SEC":    60,
    "LOG_DIR":              "logs",
    "MIN_ACCOUNT_BALANCE":  100,
    "SIM_START_BALANCE":    10_000.0,
}


# ============================================================
# 1. STRUCTURED LOGGER
# ============================================================

class StructuredLogger:
    def __init__(self, log_dir: str, symbol: str = "SYSTEM"):
        Path(log_dir).mkdir(exist_ok=True)
        safe_symbol   = symbol.replace("/", "_")
        self.log_file = f"{log_dir}/{safe_symbol}_log.json"

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        self.console = logging.getLogger(f"Bot.{safe_symbol}")

    def _write(self, event_type: str, data: dict):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event":     event_type,
            "dry_run":   CONFIG["DRY_RUN"],
            **data,
        }
        with open(self.log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def info(self, msg, **kw):
        self.console.info(msg)
        self._write("INFO", {"message": msg, **kw})

    def warning(self, msg, **kw):
        self.console.warning(msg)
        self._write("WARNING", {"message": msg, **kw})

    def error(self, msg, **kw):
        self.console.error(msg)
        self._write("ERROR", {"message": msg, **kw})

    def trade(self, action, **kw):
        self.console.info(f"TRADE [{action}] {kw}")
        self._write("TRADE", {"action": action, **kw})

    def signal(self, direction, **kw):
        self.console.info(f"SIGNAL [{direction}] {kw}")
        self._write("SIGNAL", {"direction": direction, **kw})


# ============================================================
# 2. TELEGRAM NOTIFIER
# ============================================================

class TelegramNotifier:
    """
    Sendet Echtzeit-Benachrichtigungen via Telegram Bot API.

    Setup:
      1. BotFather auf Telegram -> /newbot -> Token kopieren
      2. Bot anschreiben -> Chat-ID via @userinfobot ermitteln
      3. In .env eintragen:
           TELEGRAM_TOKEN=dein_token
           TELEGRAM_CHAT_ID=deine_chat_id
    """
    def __init__(self, logger: StructuredLogger):
        self.logger  = logger
        self.token   = os.getenv("TELEGRAM_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.token and self.chat_id)

        status = "aktiviert" if self.enabled else "deaktiviert (Token/Chat-ID fehlt)"
        logger.info(f"Telegram Notifier: {status}")

    def send(self, message: str, level: str = "INFO"):
        if not self.enabled:
            return
        try:
            import urllib.request
            icons = {"INFO": "i", "TRADE": "[T]", "WARNING": "!", "ERROR": "X", "CIRCUIT": "[STOP]"}
            icon  = icons.get(level, "*")
            text  = f"{icon} TradingBot\n{message}"
            url   = f"https://api.telegram.org/bot{self.token}/sendMessage"
            data  = json.dumps({"chat_id": self.chat_id, "text": text}).encode()
            req   = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            self.logger.warning(f"Telegram-Fehler: {e}")

    def trade_opened(self, symbol, direction, entry, stop, tp, qty, risk_usdt):
        self.send(
            f"Trade eroeffnet: {symbol}\n"
            f"Richtung: {direction}\n"
            f"Entry: {entry:.4f} | SL: {stop:.4f} | TP: {tp:.4f}\n"
            f"Menge: {qty:.6f} | Risiko: {risk_usdt:.2f} USDT",
            "TRADE"
        )

    def trade_closed(self, symbol, reason, pnl, balance):
        sign = "+" if pnl >= 0 else ""
        self.send(
            f"Trade geschlossen: {symbol}\n"
            f"Grund: {reason}\n"
            f"PnL: {sign}{pnl:.2f} USDT | Balance: {balance:.2f} USDT",
            "TRADE"
        )

    def circuit_breaker(self, reason, daily_pnl, balance):
        self.send(
            f"CIRCUIT BREAKER AUSGELOEST\n"
            f"Grund: {reason}\n"
            f"Tages-PnL: {daily_pnl:+.2f} USDT | Balance: {balance:.2f} USDT\n"
            f"Bot pausiert bis morgen.",
            "CIRCUIT"
        )

    def bot_started(self, symbols, mode):
        self.send(
            f"Bot gestartet\nModus: {mode}\n"
            f"Symbole: {', '.join(symbols)}\nTimeframe: {CONFIG['TIMEFRAME']}",
            "INFO"
        )

    def reconnect_attempt(self, attempt, max_retries):
        self.send(f"Reconnect-Versuch {attempt}/{max_retries}...", "WARNING")


# ============================================================
# 3. CIRCUIT BREAKER
# ============================================================

class CircuitBreaker:
    """
    Tagesbasiertes Sicherheitsnetz.

    Loest aus wenn:
    - Tagesverlust >= DAILY_DRAWDOWN_LIMIT (Standard: -3%)
    - Verlust-Serie >= MAX_CONSECUTIVE_LOSSES (Standard: 3 in Folge)

    Reset: Automatisch um Mitternacht UTC.
    """
    def __init__(self, logger: StructuredLogger, notifier: TelegramNotifier, start_balance: float):
        self.logger              = logger
        self.notifier            = notifier
        self.start_balance       = start_balance
        self.daily_pnl           = 0.0
        self.consecutive_losses  = 0
        self.tripped             = False
        self.trip_date           = None

    def reset_if_new_day(self):
        today = date.today()
        if self.trip_date and self.trip_date != today:
            self.logger.info("Neuer Tag — Circuit Breaker zurueckgesetzt")
            self.daily_pnl  = 0.0
            self.tripped    = False
            self.trip_date  = None

    def record_trade(self, pnl: float, balance: float) -> bool:
        """Gibt True zurueck wenn weiter gehandelt werden darf."""
        self.daily_pnl += pnl

        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

        # Pruefung 1: Tageslosst
        if self.daily_pnl < 0:
            loss_pct = abs(self.daily_pnl) / self.start_balance
            if loss_pct >= CONFIG["DAILY_DRAWDOWN_LIMIT"]:
                self._trip(f"Tages-Drawdown {loss_pct:.1%} ueberschritten", balance)
                return False

        # Pruefung 2: Verlustserie
        if self.consecutive_losses >= CONFIG["MAX_CONSECUTIVE_LOSSES"]:
            self._trip(f"{self.consecutive_losses} Verluste in Folge", balance)
            return False

        return True

    def _trip(self, reason: str, balance: float):
        self.tripped   = True
        self.trip_date = date.today()
        self.logger.warning(f"CIRCUIT BREAKER: {reason}")
        self.notifier.circuit_breaker(reason, self.daily_pnl, balance)

    def is_tripped(self) -> bool:
        self.reset_if_new_day()
        return self.tripped


# ============================================================
# 4. DATA INGESTION
# ============================================================

class DataIngestion:
    def __init__(self, exchange: ccxt.Exchange, logger: StructuredLogger):
        self.exchange = exchange
        self.logger   = logger

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        raw = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        if not raw or len(raw) < limit // 2:
            raise ValueError(f"Zu wenige Kerzen: {len(raw)}")

        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.set_index("timestamp").sort_index()

        if df[["open", "high", "low", "close", "volume"]].isnull().any().any():
            raise ValueError("NaN-Werte in OHLCV!")
        if (df["high"] < df["low"]).any():
            raise ValueError("Korrumpierte Daten: High < Low")

        return df


# ============================================================
# 5. SIGNAL GENERATOR
# ============================================================

class SignalGenerator:
    def _ema(self, s, p):
        return s.ewm(span=p, adjust=False).mean()

    def _rsi(self, s, p):
        d    = s.diff()
        gain = d.clip(lower=0).ewm(alpha=1/p, adjust=False).mean()
        loss = (-d.clip(upper=0)).ewm(alpha=1/p, adjust=False).mean()
        rs   = gain / loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    def _atr(self, df, p):
        h, l, cp = df["high"], df["low"], df["close"].shift(1)
        tr = pd.concat([(h - l), (h - cp).abs(), (l - cp).abs()], axis=1).max(axis=1)
        return tr.ewm(alpha=1/p, adjust=False).mean()

    def _adx(self, df, p):
        h, l   = df["high"], df["low"]
        up, dn = h.diff(), -l.diff()
        pdm    = np.where((up > dn) & (up > 0), up, 0.0)
        mdm    = np.where((dn > up) & (dn > 0), dn, 0.0)
        atr    = self._atr(df, p)
        pdi    = 100 * pd.Series(pdm, index=df.index).ewm(alpha=1/p, adjust=False).mean() / atr
        mdi    = 100 * pd.Series(mdm, index=df.index).ewm(alpha=1/p, adjust=False).mean() / atr
        dx     = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
        return dx.ewm(alpha=1/p, adjust=False).mean()

    def generate(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["ema_fast"] = self._ema(df["close"], CONFIG["EMA_FAST"])
        df["ema_slow"] = self._ema(df["close"], CONFIG["EMA_SLOW"])
        df["rsi"]      = self._rsi(df["close"], CONFIG["RSI_PERIOD"])
        df["atr"]      = self._atr(df, CONFIG["ATR_PERIOD"])
        df["adx"]      = self._adx(df, CONFIG["ADX_PERIOD"])
        df["vol_ma20"] = df["volume"].rolling(20).mean()

        cross_up   = (df["ema_fast"] > df["ema_slow"]) & (df["ema_fast"].shift(1) <= df["ema_slow"].shift(1))
        cross_down = (df["ema_fast"] < df["ema_slow"]) & (df["ema_fast"].shift(1) >= df["ema_slow"].shift(1))
        regime     = df["adx"] > CONFIG["ADX_THRESHOLD"]
        vol_ok     = df["volume"] > df["vol_ma20"] * CONFIG["VOLUME_FACTOR"]

        df["signal_long"]  = cross_up   & (df["rsi"] > CONFIG["RSI_LONG_MIN"])  & regime & vol_ok
        df["signal_short"] = cross_down & (df["rsi"] < CONFIG["RSI_SHORT_MAX"]) & regime & vol_ok
        return df

    def get_latest(self, df: pd.DataFrame) -> dict:
        # WICHTIG: Index -2 = letzte ABGESCHLOSSENE Kerze (nicht die laufende!)
        row = df.iloc[-2]
        return {
            "timestamp": str(row.name),
            "close":     float(row["close"]),
            "ema_fast":  float(row["ema_fast"]),
            "ema_slow":  float(row["ema_slow"]),
            "rsi":       float(row["rsi"]),
            "adx":       float(row["adx"]),
            "atr":       float(row["atr"]),
            "direction": "LONG" if row["signal_long"] else ("SHORT" if row["signal_short"] else "NONE"),
        }


# ============================================================
# 6. RISK MANAGER
# ============================================================

class RiskManager:
    def __init__(self, exchange: ccxt.Exchange, logger: StructuredLogger):
        self.exchange = exchange
        self.logger   = logger

    def get_balance(self, quote="USDT") -> float:
        bal = self.exchange.fetch_balance()
        return float(bal.get("free", {}).get(quote, 0.0))

    def calculate_position(self, balance, entry, atr, direction) -> dict:
        risk_amt  = balance * CONFIG["RISK_PER_TRADE_PCT"]
        stop_dist = atr * CONFIG["ATR_MULTIPLIER"]
        qty       = risk_amt / stop_dist

        sl = entry - stop_dist if direction == "LONG" else entry + stop_dist
        tp = entry + stop_dist * CONFIG["REWARD_RISK_RATIO"] if direction == "LONG" \
             else entry - stop_dist * CONFIG["REWARD_RISK_RATIO"]

        if sl <= 0:
            raise ValueError(f"Ungültiger Stop-Loss: {sl}")

        self.logger.info(
            f"Position: {direction} | Qty: {qty:.6f} | Entry: {entry:.2f} | "
            f"SL: {sl:.2f} | TP: {tp:.2f} | Risiko: {risk_amt:.2f} USDT"
        )
        return {
            "direction": direction, "entry_price": entry,
            "quantity": qty, "stop_loss": sl, "take_profit": tp,
            "risk_amount": risk_amt, "stop_distance": stop_dist, "atr": atr,
        }

    def update_trailing_stop(self, pos: dict, price: float) -> dict:
        new_sl = (price - pos["atr"] * CONFIG["ATR_MULTIPLIER"] if pos["direction"] == "LONG"
                  else price + pos["atr"] * CONFIG["ATR_MULTIPLIER"])
        if pos["direction"] == "LONG" and new_sl > pos["stop_loss"]:
            pos["stop_loss"] = new_sl
        elif pos["direction"] == "SHORT" and new_sl < pos["stop_loss"]:
            pos["stop_loss"] = new_sl
        return pos

    def is_stop_hit(self, pos, price):
        return price <= pos["stop_loss"] if pos["direction"] == "LONG" else price >= pos["stop_loss"]

    def is_tp_hit(self, pos, price):
        return price >= pos["take_profit"] if pos["direction"] == "LONG" else price <= pos["take_profit"]


# ============================================================
# 7. EXECUTION ENGINE
# ============================================================

class ExecutionEngine:
    def __init__(self, exchange: ccxt.Exchange, logger: StructuredLogger, notifier: TelegramNotifier):
        self.exchange = exchange
        self.logger   = logger
        self.notifier = notifier

    def open_position(self, symbol, signal, position):
        qty, price = position["quantity"], signal["close"]
        side = "buy" if position["direction"] == "LONG" else "sell"

        if qty <= 0 or price <= 0:
            self.logger.error("Ungültige Order-Parameter")
            return None

        if CONFIG["DRY_RUN"]:
            order = {"id": f"DRY_{int(time.time())}", "dry_run": True,
                     "symbol": symbol, "side": side, "amount": qty, "price": price}
        else:
            try:
                order = self.exchange.create_market_order(symbol, side, qty)
            except ccxt.InsufficientFunds:
                self.logger.error("Unzureichendes Kapital!")
                return None
            except ccxt.ExchangeError as e:
                self.logger.error(f"Exchange-Fehler beim Öffnen: {e}")
                return None

        self.logger.trade("OPEN", symbol=symbol, side=side, qty=qty, price=price)
        self.notifier.trade_opened(
            symbol, position["direction"], price,
            position["stop_loss"], position["take_profit"],
            qty, position["risk_amount"]
        )
        return order

    def close_position(self, symbol, position, reason, price, balance):
        qty  = position["quantity"]
        side = "sell" if position["direction"] == "LONG" else "buy"
        pnl  = ((price - position["entry_price"]) * qty if position["direction"] == "LONG"
                else (position["entry_price"] - price) * qty)

        if CONFIG["DRY_RUN"]:
            order = {"id": f"DRY_C_{int(time.time())}", "dry_run": True,
                     "reason": reason, "pnl": round(pnl, 4)}
        else:
            try:
                order = self.exchange.create_market_order(symbol, side, qty)
            except ccxt.ExchangeError as e:
                self.logger.error(f"Exchange-Fehler beim Schließen: {e}")
                return None

        self.logger.trade("CLOSE", symbol=symbol, reason=reason, pnl=round(pnl, 4))
        self.notifier.trade_closed(symbol, reason, pnl, balance)
        return order, pnl


# ============================================================
# 8. BACKTESTER
# ============================================================

class Backtester:
    """
    Historischer Backtest der kompletten Strategie.

    Simuliert alle Signale, Stop-Loss, Take-Profit und Trailing Stop
    auf echten historischen Daten. Berechnet vollstaendige Performance-
    Kennzahlen inkl. Max. Drawdown und Profit Factor.

    Nutzung:
      python bot.py --backtest
      python bot.py --backtest --symbol ETH/USDT --days 180
    """
    def __init__(self, exchange: ccxt.Exchange, strategy=None):
        self.exchange   = exchange
        self.signal_gen = SignalGenerator()
        self.strategy   = strategy   # None → Trend-Following (default)

    def run(self, symbol: str, timeframe: str, days: int, start_balance: float = 10_000.0):
        strategy_label = (
            type(self.strategy).__name__ if self.strategy else "TrendFollowing"
        )
        print(f"\n{'='*60}")
        print(f"  BACKTEST: {symbol} | {timeframe} | {days} Tage")
        print(f"  Strategie:    {strategy_label}")
        print(f"  Startkapital: {start_balance:.2f} USDT")
        print(f"{'='*60}\n")

        # Daten laden via Pagination (kein 1000-Kerzen-Limit)
        candles_per_day = {"1h": 24, "4h": 6, "1d": 1}.get(timeframe, 6)
        needed  = days * candles_per_day + 100
        tf_ms   = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}.get(timeframe, 14_400_000)
        since   = self.exchange.milliseconds() - needed * tf_ms
        all_rows = []
        while True:
            chunk = self.exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
            if not chunk:
                break
            all_rows.extend(chunk)
            if len(chunk) < 1000:
                break
            since = chunk[-1][0] + tf_ms
        df = pd.DataFrame(all_rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df    = df.set_index("timestamp").sort_index()
        df    = df[~df.index.duplicated(keep="last")]

        # Strategie-agnostische Signal-Berechnung
        if self.strategy is not None:
            df     = self.strategy.compute(df)
            warmup = self.strategy.warmup_candles
            has_band_exit = "signal_exit_long" in df.columns
        else:
            df     = self.signal_gen.generate(df)
            warmup = CONFIG["EMA_SLOW"] + 20
            has_band_exit = False

        # Simulation
        balance  = start_balance
        equity   = [balance]
        trades   = []
        position = None
        cooldown = 0

        for i in range(warmup, len(df) - 1):
            row   = df.iloc[i]
            price = float(row["close"])
            lo    = float(row["low"])
            hi    = float(row["high"])

            if position:
                sl, tp = position["stop_loss"], position["take_profit"]
                hit_sl = ((position["direction"] == "LONG"  and lo <= sl) or
                          (position["direction"] == "SHORT" and hi >= sl))
                hit_tp = ((position["direction"] == "LONG"  and hi >= tp) or
                          (position["direction"] == "SHORT" and lo <= tp))

                # Mean-Reversion: Mittellinie erreicht → Exit zum Close-Preis
                hit_band_exit = (
                    has_band_exit and not hit_sl and not hit_tp and (
                        (position["direction"] == "LONG"  and bool(row["signal_exit_long"]))  or
                        (position["direction"] == "SHORT" and bool(row["signal_exit_short"]))
                    )
                )

                if hit_sl or hit_tp or hit_band_exit:
                    if hit_band_exit:
                        exit_price = price
                        reason     = "MEAN_EXIT"
                    else:
                        exit_price = sl if hit_sl else tp
                        reason     = "STOP_LOSS" if hit_sl else "TAKE_PROFIT"
                    pnl = ((exit_price - position["entry"]) * position["qty"] if position["direction"] == "LONG"
                           else (position["entry"] - exit_price) * position["qty"])
                    balance += pnl
                    trades.append({
                        "entry_time":  position["entry_time"],
                        "exit_time":   str(row.name),
                        "symbol":      symbol,
                        "direction":   position["direction"],
                        "entry":       round(position["entry"], 4),
                        "exit":        round(exit_price, 4),
                        "pnl":         round(pnl, 4),
                        "risk_amount": round(position["risk_amount"], 4),
                        "reason":      reason,
                    })
                    if hit_sl:
                        cooldown = CONFIG["LOSS_COOLDOWN_BARS"]
                    position = None
                else:
                    # Trailing Stop (nur Trend-Following; Mean Reversion nutzt festen SL)
                    if not has_band_exit:
                        atr    = float(row["atr"])
                        new_sl = (price - atr * CONFIG["ATR_MULTIPLIER"] if position["direction"] == "LONG"
                                  else price + atr * CONFIG["ATR_MULTIPLIER"])
                        if position["direction"] == "LONG" and new_sl > position["stop_loss"]:
                            position["stop_loss"] = new_sl
                        elif position["direction"] == "SHORT" and new_sl < position["stop_loss"]:
                            position["stop_loss"] = new_sl

            elif cooldown > 0:
                cooldown -= 1

            elif row["signal_long"] or row["signal_short"]:
                direction = "LONG" if row["signal_long"] else "SHORT"
                atr       = float(row["atr"])
                stop_dist = atr * CONFIG["ATR_MULTIPLIER"]
                qty       = (balance * CONFIG["RISK_PER_TRADE_PCT"]) / stop_dist
                sl  = price - stop_dist if direction == "LONG" else price + stop_dist
                tp  = price + stop_dist * CONFIG["REWARD_RISK_RATIO"] if direction == "LONG" \
                      else price - stop_dist * CONFIG["REWARD_RISK_RATIO"]
                position = {
                    "direction":   direction, "entry": price,
                    "entry_time":  str(row.name), "qty": qty,
                    "stop_loss":   sl, "take_profit": tp,
                    "risk_amount": balance * CONFIG["RISK_PER_TRADE_PCT"],
                }

            equity.append(balance)

        self._print_results(trades, equity, start_balance, balance, symbol, days)
        self._save_results(trades, equity, symbol)

    def _print_results(self, trades, equity, start_bal, end_bal, symbol, days):
        if not trades:
            print("  Keine Trades im Backtest-Zeitraum.\n")
            return

        df_t     = pd.DataFrame(trades)
        wins     = df_t[df_t["pnl"] > 0]
        losses   = df_t[df_t["pnl"] <= 0]
        win_rate = len(wins) / len(df_t) * 100

        eq_series   = pd.Series(equity)
        roll_max    = eq_series.cummax()
        max_dd      = ((eq_series - roll_max) / roll_max * 100).min()

        gp = wins["pnl"].sum() if len(wins) > 0 else 0
        gl = abs(losses["pnl"].sum()) if len(losses) > 0 else 1
        pf = gp / gl if gl > 0 else float("inf")

        # RR = avg winner PnL / initial risk taken (1R = one unit of risk)
        if len(wins) > 0 and "risk_amount" in wins.columns:
            avg_rr = (wins["pnl"] / wins["risk_amount"]).mean()
        elif len(wins) > 0 and len(losses) > 0:
            avg_l  = abs(losses["pnl"].mean())
            avg_rr = wins["pnl"].mean() / avg_l if avg_l > 0.01 else 0
        else:
            avg_rr = 0

        total_ret   = (end_bal - start_bal) / start_bal * 100
        monthly_ret = total_ret / (days / 30)

        print(f"  {'─'*48}")
        print(f"  BACKTEST ERGEBNISSE — {symbol}")
        print(f"  {'─'*48}")
        print(f"  Zeitraum:              {days} Tage")
        print(f"  Startkapital:    {start_bal:>12.2f} USDT")
        print(f"  Endkapital:      {end_bal:>12.2f} USDT")
        print(f"  Gesamt-Return:   {total_ret:>+11.2f}%")
        print(f"  Monatl. Return:  {monthly_ret:>+11.2f}%")
        print(f"  {'─'*48}")
        print(f"  Trades:          {len(df_t):>12}")
        print(f"  Gewinner:        {len(wins):>7} ({win_rate:.1f}%)")
        print(f"  Verlierer:       {len(losses):>12}")
        print(f"  Avg Reward/Risk: {avg_rr:>12.2f}")
        print(f"  Profit Factor:   {pf:>12.2f}")
        print(f"  Max. Drawdown:   {max_dd:>+11.2f}%")
        print(f"  {'─'*48}")

        if pf >= 1.5 and max_dd > -15 and win_rate >= 40:
            print("  ✅ Strategie zeigt robuste Kennzahlen")
        elif pf >= 1.0:
            print("  ⚠️  Profitabel aber optimierungswuerdig")
        else:
            print("  ❌ Nicht profitabel — Parameter ueberpruefen!")
        print()

    def _save_results(self, trades, equity, symbol):
        safe = symbol.replace("/", "_")
        Path("logs").mkdir(exist_ok=True)
        path = f"logs/backtest_{safe}.json"
        with open(path, "w") as f:
            json.dump({"trades": trades, "equity": equity}, f, indent=2)
        print(f"  Gespeichert: {path}\n")


# ============================================================
# 9. PERFORMANCE DASHBOARD
# ============================================================

class Dashboard:
    """Live-Terminal-Dashboard, wird nach jedem Zyklus neu gezeichnet."""
    @staticmethod
    def print(symbol_states: dict, circuit_br: CircuitBreaker):
        os.system("clear" if os.name == "posix" else "cls")
        now  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        mode = "SIMULATION" if CONFIG["DRY_RUN"] else "ECHTHANDEL [LIVE]"
        w    = 62

        print(f"+{'=' * w}+")
        print(f"|  TRADING BOT v2.0  [{mode}]{' ' * (w - 22 - len(mode))}|")
        print(f"|  {now}{' ' * (w - len(now) - 2)}|")
        print(f"+{'=' * w}+")

        for sym, state in symbol_states.items():
            bal    = state.get("balance", 0)
            pnl    = state.get("session_pnl", 0)
            wins   = state.get("wins", 0)
            losses = state.get("losses", 0)
            pos    = state.get("position")
            sign   = "+" if pnl >= 0 else ""
            wr     = f"{wins/(wins+losses)*100:.0f}%" if (wins + losses) > 0 else "--"

            line1 = f"  {sym:<12} Bal: {bal:>10.2f} USDT  PnL: {sign}{pnl:.2f}"
            line2 = f"  {'':12} Trades W{wins}/L{losses} ({wr})"
            print(f"|{line1:<{w}}|")
            print(f"|{line2:<{w}}|")

            if pos:
                ep  = pos.get("entry_price", 0)
                sl  = pos.get("stop_loss", 0)
                tp  = pos.get("take_profit", 0)
                dr  = pos.get("direction", "?")
                line3 = f"  {'':12} OPEN {dr} @ {ep:.2f} | SL:{sl:.2f} TP:{tp:.2f}"
            else:
                line3 = f"  {'':12} Keine offene Position"
            print(f"|{line3:<{w}}|")
            print(f"|{'-' * w}|")

        cb_status = "AUSGELOEST" if circuit_br.is_tripped() else "OK"
        daily     = circuit_br.daily_pnl
        consec    = circuit_br.consecutive_losses
        cb_line   = f"  Circuit Breaker: [{cb_status}]  Tages-PnL: {daily:+.2f}  Verluste: {consec}"
        print(f"|{cb_line:<{w}}|")
        print(f"+{'=' * w}+")


# ============================================================
# 10. RECONNECT WRAPPER
# ============================================================

def with_reconnect(func, logger: StructuredLogger, notifier: TelegramNotifier, *args, **kwargs):
    """
    Wrapper fuer Netzwerk-resiliente API-Aufrufe.
    Nutzt exponentielles Backoff: 30s, 60s, 90s, 120s, 150s.
    """
    for attempt in range(1, CONFIG["MAX_RETRIES"] + 1):
        try:
            return func(*args, **kwargs)
        except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
            delay = CONFIG["RETRY_DELAY_SEC"] * attempt
            logger.warning(f"Netzwerkfehler (Versuch {attempt}/{CONFIG['MAX_RETRIES']}): {e}. Retry in {delay}s")
            notifier.reconnect_attempt(attempt, CONFIG["MAX_RETRIES"])
            if attempt == CONFIG["MAX_RETRIES"]:
                raise
            time.sleep(delay)


# ============================================================
# 11. HEALTH CHECK
# ============================================================

def run_health_check(exchange, logger, notifier) -> bool:
    logger.info("HEALTH CHECK START")

    try:
        exchange.load_markets()
        logger.info("API-Verbindung: OK")
    except Exception as e:
        logger.error(f"API-Verbindung fehlgeschlagen: {e}")
        return False

    for sym in CONFIG["SYMBOLS"]:
        if sym not in exchange.markets:
            logger.error(f"Symbol {sym} nicht verfuegbar!")
            return False
        logger.info(f"{sym}: verfuegbar")

    mode = "SIMULATION" if CONFIG["DRY_RUN"] else "ECHTHANDEL"
    logger.info(f"Modus: {mode}")
    notifier.bot_started(CONFIG["SYMBOLS"], mode)
    logger.info("HEALTH CHECK BESTANDEN")
    return True


# ============================================================
# 12. HAUPT-TRADING-LOOP (MULTI-SYMBOL)
# ============================================================

def run_bot():
    """
    Multi-Symbol Trading Loop.
    Jedes Symbol wird pro Zyklus unabhaengig geprueft.

    State pro Symbol:
    - Eigener Logger, Signal-Generator, Risk-Manager, Executor
    - Unabhaengige Position, Cooldown, Balance-Tracking
    """
    sys_logger = StructuredLogger(CONFIG["LOG_DIR"], "SYSTEM")
    notifier   = TelegramNotifier(sys_logger)
    circuit_br = CircuitBreaker(sys_logger, notifier, CONFIG["SIM_START_BALANCE"])
    dashboard  = Dashboard()

    exchange = ccxt.binance({
        "apiKey":          os.getenv("API_KEY", ""),
        "secret":          os.getenv("API_SECRET", ""),
        "enableRateLimit": True,
        "options":         {"defaultType": CONFIG["MARKET_TYPE"]},
    })

    if not run_health_check(exchange, sys_logger, notifier):
        sys_logger.error("Health Check fehlgeschlagen. Abbruch.")
        return

    # State-Dictionary pro Symbol
    states = {}
    for sym in CONFIG["SYMBOLS"]:
        lg = StructuredLogger(CONFIG["LOG_DIR"], sym)
        states[sym] = {
            "logger":      lg,
            "signal_gen":  SignalGenerator(),
            "risk_mgr":    RiskManager(exchange, lg),
            "executor":    ExecutionEngine(exchange, lg, notifier),
            "ingestion":   DataIngestion(exchange, lg),
            "position":    None,
            "cooldown":    0,
            "last_bar":    None,
            "balance":     CONFIG["SIM_START_BALANCE"],
            "session_pnl": 0.0,
            "wins":        0,
            "losses":      0,
        }

    sys_logger.info(f"Loop gestartet: {CONFIG['SYMBOLS']}")

    while True:
        try:
            # Circuit Breaker aktiv?
            if circuit_br.is_tripped():
                sys_logger.warning("Circuit Breaker aktiv — kein neuer Trade")
                dashboard.print(states, circuit_br)
                time.sleep(CONFIG["LOOP_INTERVAL_SEC"])
                continue

            for sym, st in states.items():
                logger   = st["logger"]
                try:
                    # Daten mit Reconnect-Schutz abrufen
                    df = with_reconnect(
                        st["ingestion"].fetch_ohlcv, logger, notifier,
                        sym, CONFIG["TIMEFRAME"], CONFIG["WARMUP_CANDLES"]
                    )

                    current_bar = df.index[-2]
                    if current_bar == st["last_bar"]:
                        continue
                    st["last_bar"]  = current_bar
                    current_price   = float(df["close"].iloc[-1])
                    df_sig          = st["signal_gen"].generate(df)
                    signal          = st["signal_gen"].get_latest(df_sig)

                    # Offene Position managen
                    if st["position"]:
                        pos = st["risk_mgr"].update_trailing_stop(st["position"], current_price)
                        pos["atr"]      = signal["atr"]
                        st["position"]  = pos

                        reason = None
                        if st["risk_mgr"].is_stop_hit(pos, current_price):
                            reason = "STOP_LOSS"
                        elif st["risk_mgr"].is_tp_hit(pos, current_price):
                            reason = "TAKE_PROFIT"

                        if reason:
                            result = st["executor"].close_position(
                                sym, pos, reason, current_price, st["balance"]
                            )
                            if result:
                                _, pnl       = result
                                st["balance"]    += pnl
                                st["session_pnl"] += pnl
                                st["position"]    = None
                                if pnl > 0:
                                    st["wins"] += 1
                                else:
                                    st["losses"]   += 1
                                    st["cooldown"]  = CONFIG["LOSS_COOLDOWN_BARS"]
                                circuit_br.record_trade(pnl, st["balance"])

                    # Neue Position?
                    elif signal["direction"] != "NONE":
                        if st["cooldown"] > 0:
                            st["cooldown"] -= 1
                        else:
                            bal = (st["balance"] if CONFIG["DRY_RUN"]
                                   else st["risk_mgr"].get_balance(sym.split("/")[1]))
                            pos = st["risk_mgr"].calculate_position(
                                bal, signal["close"], signal["atr"], signal["direction"]
                            )
                            order = st["executor"].open_position(sym, signal, pos)
                            if order:
                                st["position"] = pos

                except Exception as e:
                    logger.error(f"Fehler bei {sym}: {e}\n{traceback.format_exc()}")

            dashboard.print(states, circuit_br)

        except KeyboardInterrupt:
            sys_logger.info("Bot gestoppt (CTRL+C)")
            notifier.send("Bot manuell gestoppt.", "WARNING")
            for sym, st in states.items():
                if st["position"]:
                    sys_logger.warning(f"WARNUNG: Offene Position in {sym}! Bitte manuell pruefen.")
            break
        except Exception as e:
            sys_logger.error(f"Kritischer Fehler: {e}\n{traceback.format_exc()}")

        time.sleep(CONFIG["LOOP_INTERVAL_SEC"])


# ============================================================
# 13. PAPER TRADING MODE
# ============================================================

def run_paper_trading(symbol: str, start_balance: float = 10_000.0):
    """
    Paper Trading: Echte Live-Preise von Binance (kein API-Key nötig),
    simulierte Order-Ausführung, Telegram Alerts, Circuit Breaker.

    SICHERHEITS-GARANTIEN:
    - Kein API-Key / Secret → kein echter Order möglich
    - DRY_RUN wird programmatisch erzwungen
    - Paper-Trades werden in logs/paper_trades.json gespeichert
    """
    # Telegram aus dem dedizierten Modul
    try:
        from telegram_bot import TelegramAlert, start_daily_summary_scheduler
        alert = TelegramAlert()
    except ImportError:
        # Fallback: interner Notifier (kein Daily-Summary)
        alert = None

    # DRY_RUN zwingend aktiv — kann nicht überschrieben werden
    CONFIG["DRY_RUN"] = True

    Path(CONFIG["LOG_DIR"]).mkdir(exist_ok=True)
    paper_log_path = Path(CONFIG["LOG_DIR"]) / "paper_trades.json"

    logger    = StructuredLogger(CONFIG["LOG_DIR"], symbol)
    sig_gen   = SignalGenerator()
    ingestion = DataIngestion(
        ccxt.binance({"enableRateLimit": True}),  # KEIN API-Key → read-only
        logger,
    )

    # Lokaler State
    state = {
        "balance":     start_balance,
        "session_pnl": 0.0,
        "wins":        0,
        "losses":      0,
        "position":    None,
        "cooldown":    0,
        "last_bar":    None,
        "symbol":      symbol,
        "open_position": None,
    }

    consecutive_losses = 0
    daily_pnl          = 0.0
    circuit_tripped    = False
    paper_trades       = []

    def _get_stats():
        return {
            "symbol":      symbol,
            "session_pnl": state["session_pnl"],
            "wins":        state["wins"],
            "losses":      state["losses"],
            "balance":     state["balance"],
            "open_position": state["position"],
        }

    def _save_paper_trade(trade: dict):
        paper_trades.append(trade)
        with open(paper_log_path, "w") as f:
            json.dump(paper_trades, f, indent=2)

    # Daily-Summary Thread starten (20:00 UTC)
    if alert:
        start_daily_summary_scheduler(alert, _get_stats, hour_utc=20)
        alert.bot_started([symbol], "PAPER TRADING")

    logger.info(f"Paper Trading gestartet: {symbol} | Balance: {start_balance:.2f} USDT")
    logger.info("SICHERHEIT: DRY_RUN=True erzwungen — kein echter Order möglich")

    print(f"\n  Symbol:    {symbol}")
    print(f"  Balance:   {start_balance:,.2f} USDT (simuliert)")
    print(f"  Telegram:  {'aktiv' if (alert and alert.enabled) else 'deaktiviert'}")
    print(f"  Log:       {paper_log_path}")
    print(f"  Stop:      CTRL+C\n")

    while True:
        try:
            if circuit_tripped:
                logger.warning("Circuit Breaker aktiv — warte auf naechsten Tag")
                time.sleep(CONFIG["LOOP_INTERVAL_SEC"])
                continue

            df     = ingestion.fetch_ohlcv(symbol, CONFIG["TIMEFRAME"], CONFIG["WARMUP_CANDLES"])
            df_sig = sig_gen.generate(df)
            signal = sig_gen.get_latest(df_sig)

            current_bar = df.index[-2]
            if current_bar == state["last_bar"]:
                time.sleep(CONFIG["LOOP_INTERVAL_SEC"])
                continue
            state["last_bar"] = current_bar

            price = signal["close"]

            # ── Offene Position überwachen ──────────────────
            pos = state["position"]
            if pos:
                hit_sl = (price <= pos["stop_loss"]  if pos["direction"] == "LONG"
                          else price >= pos["stop_loss"])
                hit_tp = (price >= pos["take_profit"] if pos["direction"] == "LONG"
                          else price <= pos["take_profit"])

                # Trailing Stop aktualisieren
                atr    = signal["atr"]
                new_sl = (price - atr * CONFIG["ATR_MULTIPLIER"] if pos["direction"] == "LONG"
                          else price + atr * CONFIG["ATR_MULTIPLIER"])
                if pos["direction"] == "LONG" and new_sl > pos["stop_loss"]:
                    pos["stop_loss"] = new_sl
                elif pos["direction"] == "SHORT" and new_sl < pos["stop_loss"]:
                    pos["stop_loss"] = new_sl

                if hit_sl or hit_tp:
                    reason     = "STOP_LOSS" if hit_sl else "TAKE_PROFIT"
                    exit_price = pos["stop_loss"] if hit_sl else pos["take_profit"]
                    pnl        = ((exit_price - pos["entry_price"]) * pos["quantity"]
                                  if pos["direction"] == "LONG"
                                  else (pos["entry_price"] - exit_price) * pos["quantity"])
                    state["balance"]     += pnl
                    state["session_pnl"] += pnl
                    daily_pnl            += pnl

                    if pnl > 0:
                        state["wins"] += 1
                        consecutive_losses = 0
                    else:
                        state["losses"]    += 1
                        consecutive_losses += 1
                        state["cooldown"]   = CONFIG["LOSS_COOLDOWN_BARS"]

                    trade_record = {
                        "timestamp":   datetime.now(timezone.utc).isoformat(),
                        "symbol":      symbol,
                        "direction":   pos["direction"],
                        "entry_price": round(pos["entry_price"], 4),
                        "exit_price":  round(exit_price, 4),
                        "quantity":    round(pos["quantity"], 6),
                        "pnl":         round(pnl, 4),
                        "reason":      reason,
                        "balance":     round(state["balance"], 2),
                    }
                    _save_paper_trade(trade_record)

                    if alert:
                        alert.trade_exit(symbol, pos["direction"], pnl,
                                         reason, state["balance"])

                    logger.trade("CLOSE", symbol=symbol, pnl=round(pnl, 4),
                                 reason=reason, balance=round(state["balance"], 2))
                    state["position"] = None

                    # Circuit Breaker prüfen
                    if consecutive_losses >= CONFIG["MAX_CONSECUTIVE_LOSSES"]:
                        circuit_tripped = True
                        cb_reason = f"{consecutive_losses} Verluste in Folge"
                        logger.warning(f"CIRCUIT BREAKER: {cb_reason}")
                        if alert:
                            alert.circuit_breaker(cb_reason, consecutive_losses,
                                                  daily_pnl, state["balance"])

            # ── Neuen Trade eröffnen? ───────────────────────
            elif signal["direction"] != "NONE" and state["cooldown"] <= 0:
                direction = signal["direction"]
                atr       = signal["atr"]
                if atr > 0:
                    stop_dist = atr * CONFIG["ATR_MULTIPLIER"]
                    risk_amt  = state["balance"] * CONFIG["RISK_PER_TRADE_PCT"]
                    qty       = risk_amt / stop_dist
                    sl  = price - stop_dist if direction == "LONG" else price + stop_dist
                    tp  = price + stop_dist * CONFIG["REWARD_RISK_RATIO"] if direction == "LONG" \
                          else price - stop_dist * CONFIG["REWARD_RISK_RATIO"]

                    if sl > 0:
                        state["position"] = {
                            "direction":   direction,
                            "entry_price": price,
                            "quantity":    qty,
                            "stop_loss":   sl,
                            "take_profit": tp,
                            "risk_amount": risk_amt,
                        }
                        if alert:
                            alert.trade_entry(symbol, direction, price,
                                              sl, tp, qty, risk_amt)
                        logger.trade("OPEN", symbol=symbol, direction=direction,
                                     price=price, sl=sl, tp=tp)

            elif state["cooldown"] > 0:
                state["cooldown"] -= 1

            # ── Terminal-Status ─────────────────────────────
            pos_str = (f"OPEN {state['position']['direction']} @ "
                       f"{state['position']['entry_price']:.2f}"
                       if state["position"] else "Keine Position")
            pnl_sign = "+" if state["session_pnl"] >= 0 else ""
            print(f"\r  [{datetime.now(timezone.utc).strftime('%H:%M:%S')}]  "
                  f"{symbol}  {price:.2f}  |  "
                  f"PnL: {pnl_sign}{state['session_pnl']:.2f} USDT  |  "
                  f"W:{state['wins']} L:{state['losses']}  |  "
                  f"{pos_str}      ", end="", flush=True)

        except KeyboardInterrupt:
            print("\n")
            logger.info("Paper Trading gestoppt (CTRL+C)")
            if alert:
                open_pos = state["position"]
                if open_pos:
                    logger.warning(f"Offene Paper-Position: {open_pos['direction']} @ "
                                   f"{open_pos['entry_price']:.2f}")
                alert.bot_stopped("Manuell gestoppt (CTRL+C)")
            print(f"\n  Session-PnL:  {state['session_pnl']:+.2f} USDT")
            print(f"  Trades:       {state['wins'] + state['losses']} "
                  f"(W:{state['wins']} / L:{state['losses']})")
            print(f"  Endbalance:   {state['balance']:.2f} USDT")
            print(f"  Log gespeichert: {paper_log_path}\n")
            break
        except Exception as e:
            logger.error(f"Paper-Trading Fehler: {e}\n{traceback.format_exc()}")

        time.sleep(CONFIG["LOOP_INTERVAL_SEC"])


# ============================================================
# EINSTIEGSPUNKT & CLI
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Disciplined Trading Bot v2.0")
    parser.add_argument("--backtest", action="store_true",          help="Backtest starten")
    parser.add_argument("--paper",    action="store_true",          help="Paper Trading mit Live-Preisen")
    parser.add_argument("--symbol",   default="BTC/USDT",           help="Symbol fuer Backtest / Paper Trading")
    parser.add_argument("--days",     type=int,   default=365,      help="Backtest-Zeitraum in Tagen")
    parser.add_argument("--balance",  type=float, default=10_000.0, help="Startkapital")
    parser.add_argument("--strategy", default="trend",
                        choices=["trend", "mean_reversion"],
                        help="Strategie fuer Backtest: trend (default) | mean_reversion")
    args = parser.parse_args()

    print("""
+============================================================+
|       DISCIPLINED TREND-FOLLOWING BOT v2.0                 |
|                                                            |
|  Backtest | Telegram | Circuit Breaker | Multi-Symbol      |
+============================================================+
    """)

    if args.backtest:
        print(f"Starte Backtest: {args.symbol} | {args.days} Tage | {args.balance:.0f} USDT\n")
        ex = ccxt.binance({"enableRateLimit": True})

        strategy_obj = None
        if args.strategy == "mean_reversion":
            try:
                import sys as _sys
                _sys.path.insert(0, ".")
                from strategies.mean_reversion import MeanReversionStrategy
                strategy_obj = MeanReversionStrategy()
            except ImportError as e:
                print(f"Fehler beim Laden der Strategie: {e}")
                sys.exit(1)

        Backtester(ex, strategy=strategy_obj).run(
            args.symbol, CONFIG["TIMEFRAME"], args.days, args.balance
        )
    elif args.paper:
        print("PAPER TRADING MODUS — Echte Live-Preise, simulierte Orders\n")
        print("SICHERHEIT: Kein API-Key verwendet. Kein echter Order möglich.\n")
        run_paper_trading(args.symbol, args.balance)
    else:
        if CONFIG["DRY_RUN"]:
            print("SIMULATIONSMODUS AKTIV — Kein echtes Geld wird verwendet.\n")
        else:
            confirm = input("ECHTHANDEL MODUS! Bist du sicher? Tippe 'ja': ")
            if confirm.strip().lower() != "ja":
                print("Abgebrochen.")
                sys.exit(0)
        run_bot()
