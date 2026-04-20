"""
================================================================================
  QuantBot Pro — Dual-Strategy Portfolio (Paper Mode Only)

  Strategie 1: Trend Following  EMA(10/21) + ADX>25  — 5.000 USDT
  Strategie 2: Mean Reversion   BB(25/3.0) + RSI(14)  — 5.000 USDT

  Start: python3 portfolio.py --paper --symbol BTC/USDT

  Sicherheit:
    • DRY_RUN = True  erzwungen — kein echter Order möglich
    • Kein API-Key erforderlich (nur öffentliche Binance-Endpoints)
================================================================================
"""

import os
import sys
import json
import time
import signal as _signal
import argparse
import threading
from pathlib import Path
from datetime import datetime, timezone

import ccxt
import pandas as pd

sys.path.insert(0, ".")
from strategies.trend_following import TrendFollowingStrategy
from strategies.mean_reversion  import MeanReversionStrategy
from telegram_bot import TelegramAlert
from logger       import QuantBotLogger

# KRITISCH: Immer True — niemals auf False setzen
DRY_RUN = True

# ── Portfolio-Konfiguration ───────────────────────────────────

_START_CAPITAL = float(os.getenv("START_CAPITAL", 10_000))

CFG = {
    "CAPITAL_TF":              _START_CAPITAL / 2,
    "CAPITAL_MR":              _START_CAPITAL / 2,
    "CIRCUIT_BREAKER_LOSSES":  3,
    "DISPLAY_INTERVAL":        30,    # Sekunden zwischen Display-Updates
    "TELEGRAM_INTERVAL":       3600,  # 60 Minuten zwischen Portfolio-Updates
    "TIMEFRAME":               "4h",
    "LOG_FILE":                "logs/portfolio_trades.json",
    "CANDLE_LIMIT":            300,
}

TF_PARAMS = {
    "EMA_FAST": 10, "EMA_SLOW": 21, "RSI_PERIOD": 10,
    "RSI_LONG_MIN": 48, "RSI_SHORT_MAX": 52,
    "ATR_PERIOD": 14, "ATR_MULTIPLIER": 2.0,
    "ADX_PERIOD": 14, "ADX_THRESHOLD": 25,
    "VOLUME_FACTOR": 1.2, "REWARD_RISK_RATIO": 1.5, "RISK_PER_TRADE_PCT": 0.01,
}

MR_PARAMS = {
    "BB_PERIOD": 25, "BB_STD": 3.0, "RSI_PERIOD": 14,
    "RSI_LONG": 25, "RSI_SHORT": 65,
    "ATR_MULTIPLIER": 2.0, "REWARD_RISK_RATIO": 1.5, "RISK_PER_TRADE_PCT": 0.01,
}

BOX_W = 49  # Innere Breite der Terminal-Box (Zeichen)


# ============================================================
# PORTFOLIO SLOT — eine Strategie mit Kapital und Trade-State
# ============================================================

class PortfolioSlot:
    """Hält eine Strategie mit ihrem Kapitalanteil und Trade-Zustand."""

    def __init__(self, label: str, strategy, capital: float):
        self.label         = label      # "TF" | "MR"
        self.strategy      = strategy
        self.balance       = capital
        self.start_balance = capital
        self.position      = None       # dict wenn Position offen
        self.trades: list  = []
        self.wins          = 0
        self.losses        = 0
        self._status       = "Kein Trade"

    @property
    def pnl(self) -> float:
        return self.balance - self.start_balance

    def status_str(self) -> str:
        if self.position:
            d = self.position["direction"]
            e = self.position["entry"]
            return f"{d} @ {e:,.2f}"
        return self._status


# ============================================================
# PORTFOLIO ENGINE — steuert beide Slots + Circuit Breaker
# ============================================================

class PortfolioEngine:

    def __init__(
        self,
        symbol:   str,
        exchange: ccxt.Exchange,
        slots:    list,
        alert:    TelegramAlert,
    ):
        self.symbol          = symbol
        self.exchange        = exchange
        self.slots           = slots      # [TF-Slot, MR-Slot]
        self.alert           = alert
        self.running         = True
        self.combined_losses = 0
        self.circuit_active  = False
        self.start_time      = datetime.now(timezone.utc)
        self._last_candle_ts = None
        self._display_lines  = 0
        self._lock           = threading.Lock()
        self.logger: QuantBotLogger | None = None
        self._ohlcv_init     = True

    # ── Daten ────────────────────────────────────────────────

    def _fetch_df(self) -> pd.DataFrame:
        ohlcv = self.exchange.fetch_ohlcv(
            self.symbol, CFG["TIMEFRAME"], limit=CFG["CANDLE_LIMIT"]
        )
        df = pd.DataFrame(
            ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df.set_index("timestamp")

    def _current_price(self) -> float:
        try:
            return float(self.exchange.fetch_ticker(self.symbol)["last"])
        except Exception:
            return 0.0

    # ── Trade-Logik ──────────────────────────────────────────

    def _get_portfolio_state(self) -> dict:
        return {
            "symbol":        self.symbol,
            "capital_total": sum(s.start_balance for s in self.slots),
            "combined_losses": self.combined_losses,
            "tf": {
                "balance":      self.slots[0].balance,
                "pnl":          self.slots[0].pnl,
                "trades":       len(self.slots[0].trades),
                "wins":         self.slots[0].wins,
                "has_position": self.slots[0].position is not None,
            },
            "mr": {
                "balance":      self.slots[1].balance,
                "pnl":          self.slots[1].pnl,
                "trades":       len(self.slots[1].trades),
                "wins":         self.slots[1].wins,
                "has_position": self.slots[1].position is not None,
            },
        }

    def _process_candle(self, df: pd.DataFrame):
        """Wird einmal pro neuer geschlossener Kerze aufgerufen."""
        # Indikatoren für beide Strategien einmalig berechnen
        df_by = {slot.label: slot.strategy.compute(df) for slot in self.slots}

        for slot in self.slots:
            df_c = df_by[slot.label]
            sig  = slot.strategy.signal(df_c)

            # MAE/MFE-Tracking für offene Position
            if slot.position and self.logger:
                row = df_c.iloc[-2]
                tid = f"{slot.label}_{slot.position['entry_time']}"
                self.logger.update_trade_tracking(tid, float(row["high"]), float(row["low"]))

            # blocked_by VOR Exit-Check bestimmen
            blocked = ("CIRCUIT_BREAKER" if self.circuit_active
                       else "POSITION_OPEN" if slot.position else "NONE")

            # Erst Exit-Check, dann Entry
            if slot.position:
                self._check_exit(slot, df_c, sig)

            if not slot.position and not self.circuit_active and sig["direction"] != "NONE":
                self._enter(slot, sig)

            if self.logger:
                self.logger.log_signal(slot.label, sig, df_c, blocked)

        # Markt-Snapshot einmal pro Kerze
        if self.logger and len(self.slots) >= 2:
            self.logger.log_market_snapshot(df_by[self.slots[0].label], df_by[self.slots[1].label])

    def _enter(self, slot: PortfolioSlot, sig: dict):
        direction = sig["direction"]
        entry     = sig["close"]
        atr       = sig["atr"]

        if atr <= 0:
            return

        pos = slot.strategy.position_size(slot.balance, entry, atr, direction)
        slot.position = {
            "direction":   direction,
            "entry":       entry,
            "entry_time":  sig["timestamp"],
            "qty":         pos["quantity"],
            "stop_loss":   pos["stop_loss"],
            "take_profit": pos["take_profit"],
            "risk_amount": pos["risk_amount"],
        }
        slot._status = f"{direction} @ {entry:,.2f}"

        if self.logger:
            tid = f"{slot.label}_{sig['timestamp']}"
            self.logger.start_trade_tracking(tid, {
                "label":       slot.label,
                "entry":       entry,
                "direction":   direction,
                "stop_loss":   pos["stop_loss"],
                "take_profit": pos["take_profit"],
                "qty":         pos["quantity"],
                "entry_time":  sig["timestamp"],
            })

        icon = "🟢" if direction == "LONG" else "🔴"
        self.alert.send(
            f"{icon} <b>[{slot.label}] Trade Entry — {self.symbol}</b>\n"
            f"Richtung:    <b>{direction}</b>\n"
            f"Entry:       <code>{entry:,.2f}</code>\n"
            f"Stop-Loss:   <code>{pos['stop_loss']:,.2f}</code>\n"
            f"Take-Profit: <code>{pos['take_profit']:,.2f}</code>\n"
            f"Risiko:      {pos['risk_amount']:.2f} USDT"
        )

    def _check_exit(self, slot: PortfolioSlot, df: pd.DataFrame, sig: dict):
        pos  = slot.position
        row  = df.iloc[-2]
        hi   = float(row["high"])
        lo   = float(row["low"])
        price= float(row["close"])

        hit_sl = (
            (pos["direction"] == "LONG"  and lo <= pos["stop_loss"]) or
            (pos["direction"] == "SHORT" and hi >= pos["stop_loss"])
        )
        hit_tp = (
            (pos["direction"] == "LONG"  and hi >= pos["take_profit"]) or
            (pos["direction"] == "SHORT" and lo <= pos["take_profit"])
        )
        hit_band = (
            not hit_sl and not hit_tp and (
                (pos["direction"] == "LONG"  and sig.get("exit_long",  False)) or
                (pos["direction"] == "SHORT" and sig.get("exit_short", False))
            )
        )

        if not (hit_sl or hit_tp or hit_band):
            return

        exit_price = (pos["stop_loss"]  if hit_sl else
                      pos["take_profit"] if hit_tp else price)
        reason     = ("STOP_LOSS"  if hit_sl else
                      "TAKE_PROFIT" if hit_tp else "MEAN_EXIT")

        pnl = (
            (exit_price - pos["entry"]) * pos["qty"]
            if pos["direction"] == "LONG"
            else (pos["entry"] - exit_price) * pos["qty"]
        )

        slot.balance += pnl
        slot._status  = f"Exit {'+'if pnl>=0 else ''}{pnl:.2f}"

        if pnl > 0:
            slot.wins += 1
        else:
            slot.losses += 1
            with self._lock:
                self.combined_losses += 1
                if self.combined_losses >= CFG["CIRCUIT_BREAKER_LOSSES"]:
                    self._trigger_circuit_breaker()

        trade = {
            "strategy":   slot.label,
            "symbol":     self.symbol,
            "direction":  pos["direction"],
            "entry_time": pos["entry_time"],
            "exit_time":  str(row.name),
            "entry":      round(pos["entry"],      4),
            "exit":       round(exit_price,         4),
            "pnl":        round(pnl,                4),
            "reason":     reason,
            "balance":    round(slot.balance,       4),
        }
        slot.trades.append(trade)

        if self.logger:
            tid = f"{slot.label}_{pos['entry_time']}"
            self.logger.log_trade_quality(tid, {
                "pnl":        pnl,
                "exit_price": exit_price,
                "exit_time":  str(row.name),
                "reason":     reason,
            })

        slot.position = None

        icon = "✅" if pnl > 0 else "❌"
        s    = "+" if pnl >= 0 else ""
        self.alert.send(
            f"{icon} <b>[{slot.label}] Trade Exit — {self.symbol}</b>\n"
            f"Richtung: {pos['direction']}\n"
            f"Grund:    {reason}\n"
            f"PnL:      <b>{s}{pnl:.2f} USDT</b>\n"
            f"Balance:  {slot.balance:.2f} USDT"
        )

        self._save_trades()

    def _trigger_circuit_breaker(self):
        if self.circuit_active:
            return
        self.circuit_active = True
        total_pnl = sum(s.pnl for s in self.slots)
        for slot in self.slots:
            slot.position = None
            slot._status  = "⛔ Gestoppt"
        print(f"\n  ⛔ Circuit Breaker ausgelöst — {self.combined_losses} kombinierte Verluste")
        self.alert.send(
            f"⛔ <b>Circuit Breaker — Portfolio gestoppt</b>\n"
            f"{'─'*28}\n"
            f"Verluste gesamt: {self.combined_losses}\n"
            f"Gesamt-PnL:      {total_pnl:+.2f} USDT\n\n"
            f"⏸ Beide Strategien pausiert."
        )

    # ── Terminal-Display ─────────────────────────────────────

    def _display(self, price: float):
        tf  = self.slots[0]
        mr  = self.slots[1]
        tot_pnl  = tf.pnl + mr.pnl
        tot_w    = tf.wins   + mr.wins
        tot_l    = tf.losses + mr.losses
        cb_str   = f"CB:{self.combined_losses}/{CFG['CIRCUIT_BREAKER_LOSSES']}"
        up_secs  = (datetime.now(timezone.utc) - self.start_time).seconds
        up_str   = f"{up_secs//3600:02d}:{(up_secs%3600)//60:02d}:{up_secs%60:02d}"
        state    = "⛔ CIRCUIT BREAKER" if self.circuit_active else "🟢 Aktiv"

        def row(content: str) -> str:
            # pad / truncate to exactly BOX_W chars
            content = content[:BOX_W]
            return f"│ {content.ljust(BOX_W)} │"

        sep = "─" * BOX_W
        box = [
            f"┌{'─'*(BOX_W+2)}┐",
            row(f"PORTFOLIO  {self.symbol}  {price:>12,.3f} USDT"),
            row(sep),
            row(f"[TF] {tf.status_str():<22} PnL: {tf.pnl:>+9.2f}"),
            row(f"[MR] {mr.status_str():<22} PnL: {mr.pnl:>+9.2f}"),
            row(sep),
            row(f"GESAMT: {tot_pnl:>+9.2f} USDT  W:{tot_w}  L:{tot_l}  {cb_str}"),
            row(f"Uptime: {up_str}  {state}"),
            f"└{'─'*(BOX_W+2)}┘",
        ]

        # Overwrite previous box in-place
        if self._display_lines > 0:
            print(f"\033[{self._display_lines}A\033[J", end="", flush=True)
        for line in box:
            print(line)
        self._display_lines = len(box)

    # ── OHLCV-Cache für Dashboard-Chart ─────────────────────

    def _save_ohlcv_cache(self, df_tf: pd.DataFrame, df_mr: pd.DataFrame):
        import math

        def safe(v):
            try:
                x = float(v)
                return None if math.isnan(x) or math.isinf(x) else round(x, 2)
            except Exception:
                return None

        def col(row, c):
            try:    return safe(row[c])
            except: return None

        out = []
        for ts in df_tf.index[-200:]:
            rtf = df_tf.loc[ts]
            rmr = df_mr.loc[ts] if ts in df_mr.index else pd.Series(dtype=float)
            out.append({
                "t":   str(ts)[:16],
                "c":   col(rtf, "close"),
                "e10": col(rtf, "ema_fast"),
                "e21": col(rtf, "ema_slow"),
                "rsi": col(rtf, "rsi"),
                "adx": col(rtf, "adx"),
                "bbu": col(rmr, "bb_upper"),
                "bbm": col(rmr, "bb_middle"),
                "bbl": col(rmr, "bb_lower"),
            })
        Path("logs").mkdir(exist_ok=True)
        with open("logs/ohlcv_cache.json", "w") as f:
            json.dump({
                "updated": datetime.now(timezone.utc).isoformat(),
                "symbol":  self.symbol,
                "candles": out,
            }, f)

    # ── Persistenz ───────────────────────────────────────────

    def _save_trades(self):
        Path("logs").mkdir(exist_ok=True)
        all_trades = []
        for slot in self.slots:
            all_trades.extend(slot.trades)
        data = {
            "symbol":          self.symbol,
            "start_time":      self.start_time.isoformat(),
            "last_update":     datetime.now(timezone.utc).isoformat(),
            "capital_total":   CFG["CAPITAL_TF"] + CFG["CAPITAL_MR"],
            "circuit_active":  self.circuit_active,
            "combined_losses": self.combined_losses,
            "strategies": {
                "TF": {
                    "start_balance": self.slots[0].start_balance,
                    "balance":       round(self.slots[0].balance, 4),
                    "pnl":           round(self.slots[0].pnl, 4),
                    "wins":          self.slots[0].wins,
                    "losses":        self.slots[0].losses,
                },
                "MR": {
                    "start_balance": self.slots[1].start_balance,
                    "balance":       round(self.slots[1].balance, 4),
                    "pnl":           round(self.slots[1].pnl, 4),
                    "wins":          self.slots[1].wins,
                    "losses":        self.slots[1].losses,
                },
            },
            "trades": all_trades,
        }
        with open(CFG["LOG_FILE"], "w") as f:
            json.dump(data, f, indent=2, default=str)

    # ── Telegram 60-Minuten-Update ────────────────────────────

    def _start_hourly_telegram(self):
        def _worker():
            last_sent = None
            while self.running:
                elapsed = (
                    (datetime.now(timezone.utc) - last_sent).total_seconds()
                    if last_sent else float("inf")
                )
                if elapsed >= CFG["TELEGRAM_INTERVAL"]:
                    tot_pnl = sum(s.pnl for s in self.slots)
                    tot_w   = sum(s.wins   for s in self.slots)
                    tot_l   = sum(s.losses for s in self.slots)
                    s       = "+" if tot_pnl >= 0 else ""
                    self.alert.send(
                        f"📊 <b>Portfolio Update — {self.symbol}</b>\n"
                        f"{'─'*28}\n"
                        f"[TF] {self.slots[0].balance:.2f} USDT  PnL: {self.slots[0].pnl:+.2f}\n"
                        f"[MR] {self.slots[1].balance:.2f} USDT  PnL: {self.slots[1].pnl:+.2f}\n"
                        f"{'─'*28}\n"
                        f"Gesamt-PnL: <b>{s}{tot_pnl:.2f} USDT</b>\n"
                        f"Trades:     W:{tot_w} / L:{tot_l}\n"
                        f"Zeit:       {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                    )
                    last_sent = datetime.now(timezone.utc)
                time.sleep(60)

        t = threading.Thread(target=_worker, daemon=True, name="HourlyTelegram")
        t.start()

    # ── Haupt-Loop ───────────────────────────────────────────

    def run(self):
        print("  Lade initiale Daten...", flush=True)
        self._save_trades()

        # Logging-System starten
        self.logger = QuantBotLogger(exchange=self.exchange)
        self.logger.start_background_threads(self._get_portfolio_state)

        self._start_hourly_telegram()

        capital_tf = self.slots[0].start_balance
        capital_mr = self.slots[1].start_balance
        self.alert.send(
            f"🚀 <b>Portfolio gestartet — {self.symbol}</b>\n"
            f"[TF] EMA(10/21) ADX>25  — {capital_tf:,.0f} USDT\n"
            f"[MR] BB(25/3.0) RSI(14) — {capital_mr:,.0f} USDT\n"
            f"Modus: PAPER (DRY_RUN=True)\n"
            f"Zeit:  {self.start_time.strftime('%Y-%m-%d %H:%M UTC')}"
        )

        try:
            while self.running:
                try:
                    df    = self._fetch_df()
                    price = self._current_price()

                    # OHLCV-Cache: einmalig beim Start + bei jeder neuen Kerze
                    candle_ts = str(df.index[-2])
                    if self._ohlcv_init or candle_ts != self._last_candle_ts:
                        try:
                            df_tf = self.slots[0].strategy.compute(df.copy())
                            df_mr = self.slots[1].strategy.compute(df.copy())
                            self._save_ohlcv_cache(df_tf, df_mr)
                        except Exception:
                            pass
                        self._ohlcv_init = False

                    if candle_ts != self._last_candle_ts:
                        self._last_candle_ts = candle_ts
                        if not self.circuit_active:
                            self._process_candle(df)

                    self._display(price)

                except ccxt.NetworkError as e:
                    print(f"\n  [Netzwerk] {e} — Retry in {CFG['DISPLAY_INTERVAL']}s")
                except Exception as e:
                    print(f"\n  [Fehler] {type(e).__name__}: {e}")

                time.sleep(CFG["DISPLAY_INTERVAL"])

        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            self._save_trades()
            tot_pnl = sum(s.pnl for s in self.slots)
            print(f"\n\n  Portfolio gestoppt.")
            print(f"  [TF] PnL: {self.slots[0].pnl:+.2f} | W:{self.slots[0].wins} L:{self.slots[0].losses}")
            print(f"  [MR] PnL: {self.slots[1].pnl:+.2f} | W:{self.slots[1].wins} L:{self.slots[1].losses}")
            print(f"  Gesamt:   {tot_pnl:+.2f} USDT")
            print(f"  Log:      {CFG['LOG_FILE']}")
            self.alert.send(
                f"⛔ <b>Portfolio gestoppt — {self.symbol}</b>\n"
                f"[TF] PnL: {self.slots[0].pnl:+.2f} USDT\n"
                f"[MR] PnL: {self.slots[1].pnl:+.2f} USDT\n"
                f"Gesamt-PnL: {tot_pnl:+.2f} USDT"
            )


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="QuantBot Pro — Dual-Strategy Portfolio (Paper Mode)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Beispiel:
  python3 portfolio.py --paper --symbol BTC/USDT
  python3 portfolio.py --paper --symbol ETH/USDT
        """,
    )
    parser.add_argument(
        "--paper", action="store_true", required=True,
        help="Paper-Trading-Modus (PFLICHT — schützt vor versehentlichem Live-Handel)",
    )
    _env_symbol   = os.getenv("SYMBOL",   "BTC/USDT").split()[0]
    _env_strategy = os.getenv("STRATEGY", "portfolio")
    parser.add_argument("--symbol",   default=_env_symbol,
                        help=f"Handelspaar (default aus .env: {_env_symbol})")
    parser.add_argument("--strategy", default=_env_strategy,
                        choices=["portfolio", "trend", "mean_reversion"],
                        help="Strategie-Modus: portfolio (beide) | trend | mean_reversion")
    args = parser.parse_args()

    print("""
+============================================================+
|   QUANTBOT PRO — DUAL-STRATEGY PORTFOLIO                   |
|   Trend Following + Mean Reversion  |  PAPER MODE          |
+============================================================+
    """)
    print(f"  Symbol:         {args.symbol}")
    print(f"  [TF] EMA(10/21) | ADX>25  | Kapital: {CFG['CAPITAL_TF']:,.0f} USDT")
    print(f"  [MR] BB(25/3.0) | RSI(14) | Kapital: {CFG['CAPITAL_MR']:,.0f} USDT")
    print(f"  Gesamt:                     {_START_CAPITAL:,.0f} USDT")
    print(f"  Circuit Breaker: {CFG['CIRCUIT_BREAKER_LOSSES']} kombinierte Verluste → beide stoppen")
    print(f"\n  ✅ DRY_RUN = True — kein echter Order möglich\n")

    tf_strategy = TrendFollowingStrategy(params=TF_PARAMS, verbose=True)
    mr_strategy = MeanReversionStrategy(
        bb_period  = MR_PARAMS["BB_PERIOD"],
        bb_std     = MR_PARAMS["BB_STD"],
        rsi_period = MR_PARAMS["RSI_PERIOD"],
        rsi_long   = MR_PARAMS["RSI_LONG"],
        rsi_short  = MR_PARAMS["RSI_SHORT"],
        verbose    = True,
    )

    slots = [
        PortfolioSlot("TF", tf_strategy, CFG["CAPITAL_TF"]),
        PortfolioSlot("MR", mr_strategy, CFG["CAPITAL_MR"]),
    ]

    alert    = TelegramAlert()
    exchange = ccxt.binance({"enableRateLimit": True})

    engine = PortfolioEngine(args.symbol, exchange, slots, alert)

    def _shutdown(sig, frame):
        print("\n  Shutdown-Signal empfangen...")
        engine.running = False

    _signal.signal(_signal.SIGINT,  _shutdown)
    _signal.signal(_signal.SIGTERM, _shutdown)

    engine.run()


if __name__ == "__main__":
    main()
