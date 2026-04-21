"""
================================================================================
  QuantBot Pro — Zentrales Logging-Modul (TICKET-12)

  Schreibt 6 CSV-Dateien mit täglicher Rotation in logs/YYYY-MM-DD/

  Hintergrund-Threads:
    • portfolio_snapshots  — stündlich
    • external_market      — stündlich  (Fear & Greed + BTC Volume)
    • system_health        — alle 15 Minuten

  Verwendung (in portfolio.py):
    from logger import QuantBotLogger
    logger = QuantBotLogger(exchange=exchange)
    logger.start_background_threads(get_state_fn)
================================================================================
"""

import csv
import json
import os
import sys
import time
import threading
import urllib.request
from pathlib import Path
from datetime import datetime, timezone
from typing import Callable, Optional

_RAM_WARN_MB = 400

import pandas as pd

try:
    import psutil as _psutil
    _PSUTIL_OK = True
except ImportError:
    _PSUTIL_OK = False

# ── CSV-Header-Definitionen ──────────────────────────────────

_HEADERS = {
    "market_snapshot.csv": [
        "timestamp", "btc_price", "open", "high", "low", "close", "volume",
        "ema_10", "ema_21", "ema_distance_pct",
        "rsi_tf", "rsi_mr", "adx", "atr",
        "bb_upper", "bb_middle", "bb_lower", "bb_width",
        "market_regime",
    ],
    "signals.csv": [
        "timestamp", "strategy", "signal_type", "signal_strength",
        "condition_1_met", "condition_2_met", "condition_3_met",
        "blocked_by",
        "rsi_value", "adx_value", "bb_position",
    ],
    "trade_quality.csv": [
        "timestamp_entry", "timestamp_exit", "strategy", "direction",
        "entry_price", "exit_price", "stop_loss", "take_profit",
        "pnl", "pnl_pct", "win",
        "mae", "mfe", "efficiency",
        "duration_hours", "exit_reason",
    ],
    "portfolio_snapshots.csv": [
        "timestamp",
        "total_balance", "total_pnl", "total_pnl_pct",
        "tf_balance", "tf_pnl", "tf_trades", "tf_wins",
        "mr_balance", "mr_pnl", "mr_trades", "mr_wins",
        "circuit_breaker_count", "active_positions",
        "btc_price",
    ],
    "external_market.csv": [
        "timestamp", "fear_greed_value", "fear_greed_label",
        "btc_24h_volume", "btc_24h_change_pct",
    ],
    "system_health.csv": [
        "timestamp", "api_latency_ms", "last_successful_fetch",
        "ram_usage_mb", "cpu_pct",
        "error_count_total", "last_error", "bot_uptime_seconds",
    ],
}


# ============================================================
# HAUPTKLASSE
# ============================================================

class QuantBotLogger:
    """
    Zentrales Logging-Modul. Instanziierung reicht — Hintergrund-Threads
    starten via start_background_threads().
    """

    def __init__(self, exchange=None, notifier=None):
        self.exchange        = exchange
        self._notifier       = notifier
        self._active_trades  = {}          # trade_id → tracking-dict
        self._error_count    = 0
        self._last_error     = ""
        self._start_time     = datetime.now(timezone.utc)
        self._last_fetch_ok  = datetime.now(timezone.utc).isoformat()
        self._write_lock     = threading.Lock()

    # ── Verzeichnis & CSV ─────────────────────────────────────

    def _day_dir(self) -> Path:
        d = Path("logs") / datetime.now(timezone.utc).strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _append_csv(self, filename: str, row: dict):
        """Hängt eine Zeile an die Tages-CSV an. Erstellt Header bei neuer Datei."""
        path    = self._day_dir() / filename
        headers = _HEADERS[filename]
        with self._write_lock:
            new_file = not path.exists()
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
                if new_file:
                    w.writeheader()
                w.writerow(row)

    def _err(self, msg: str):
        self._error_count += 1
        self._last_error   = str(msg)[:200]

    # ── Markt-Regime ─────────────────────────────────────────

    @staticmethod
    def _regime(adx: float, ema_f: float, ema_s: float,
                atr: float, atr_avg: float) -> str:
        if atr_avg > 0:
            if atr > 2.0 * atr_avg:
                return "HIGH_VOLA"
            if atr < 0.5 * atr_avg:
                return "LOW_VOLA"
        if adx > 25:
            return "TRENDING_UP" if ema_f > ema_s else "TRENDING_DOWN"
        return "RANGING"

    # ── DATEI 1: market_snapshot.csv ─────────────────────────

    def log_market_snapshot(self, df_tf: pd.DataFrame, df_mr: pd.DataFrame):
        """
        Bei jeder neuen Kerze aufrufen.
        df_tf: TrendFollowingStrategy.compute() Ergebnis
        df_mr: MeanReversionStrategy.compute()  Ergebnis
        """
        try:
            row_tf  = df_tf.iloc[-2]
            row_mr  = df_mr.iloc[-2]
            atr     = float(row_tf.get("atr",      0))
            atr_avg = float(df_tf["atr"].rolling(20).mean().iloc[-2])
            ema_f   = float(row_tf.get("ema_fast", 0))
            ema_s   = float(row_tf.get("ema_slow", 0))
            adx     = float(row_tf.get("adx",      0))
            close   = float(row_tf["close"])
            bb_u    = float(row_mr.get("bb_upper",  0))
            bb_m    = float(row_mr.get("bb_middle", 0))
            bb_l    = float(row_mr.get("bb_lower",  0))
            bb_w    = ((bb_u - bb_l) / bb_m * 100) if bb_m > 0 else 0.0
            ema_d   = ((ema_f - ema_s) / ema_s * 100) if ema_s > 0 else 0.0

            self._append_csv("market_snapshot.csv", {
                "timestamp":        str(row_tf.name),
                "btc_price":        round(close, 2),
                "open":             round(float(row_tf["open"]),   2),
                "high":             round(float(row_tf["high"]),   2),
                "low":              round(float(row_tf["low"]),    2),
                "close":            round(close,                   2),
                "volume":           round(float(row_tf["volume"]), 4),
                "ema_10":           round(ema_f, 2),
                "ema_21":           round(ema_s, 2),
                "ema_distance_pct": round(ema_d, 4),
                "rsi_tf":           round(float(row_tf.get("rsi", 0)), 2),
                "rsi_mr":           round(float(row_mr.get("rsi", 0)), 2),
                "adx":              round(adx, 2),
                "atr":              round(atr, 2),
                "bb_upper":         round(bb_u, 2),
                "bb_middle":        round(bb_m, 2),
                "bb_lower":         round(bb_l, 2),
                "bb_width":         round(bb_w, 4),
                "market_regime":    self._regime(adx, ema_f, ema_s, atr, atr_avg),
            })
        except Exception as e:
            self._err(f"market_snapshot: {e}")

    # ── DATEI 2: signals.csv ─────────────────────────────────

    def log_signal(
        self,
        strategy_label: str,   # "TF" | "MR"
        sig:            dict,
        df_c:           pd.DataFrame,
        blocked_by:     str = "NONE",
    ):
        """Pro Kerze und Strategie aufrufen, auch ohne Signal."""
        try:
            row   = df_c.iloc[-2]
            rsi   = float(row.get("rsi",    0))
            adx   = float(row.get("adx",    0)) if strategy_label == "TF" else 0.0
            close = float(row["close"])
            bb_u  = float(row.get("bb_upper",  0))
            bb_l  = float(row.get("bb_lower",  0))
            bb_pos= max(0.0, min(1.0, (close - bb_l) / (bb_u - bb_l))) \
                    if (bb_u - bb_l) > 0 else 0.5

            direction = sig.get("direction", "NONE")

            if strategy_label == "TF":
                ema_f  = float(row.get("ema_fast", 0))
                ema_s  = float(row.get("ema_slow", 0))
                prev_f = float(df_c["ema_fast"].shift(1).iloc[-2])
                prev_s = float(df_c["ema_slow"].shift(1).iloc[-2])
                cross  = ((ema_f > ema_s and prev_f <= prev_s) or
                          (ema_f < ema_s and prev_f >= prev_s))
                c1 = bool(cross)
                c2 = bool(rsi > 48) if direction == "LONG" else bool(rsi < 52)
                c3 = bool(adx > 25)
                if direction == "LONG":
                    strength = max(0.0, min(100.0,
                        (rsi - 48) / 52 * 50 + max(0.0, adx - 25) / 75 * 50))
                elif direction == "SHORT":
                    strength = max(0.0, min(100.0,
                        (52 - rsi) / 52 * 50 + max(0.0, adx - 25) / 75 * 50))
                else:
                    strength = 0.0
            else:  # MR
                c1 = bool(close < bb_l) if direction == "LONG" else bool(close > bb_u)
                c2 = bool(rsi < 25)     if direction == "LONG" else bool(rsi > 65)
                c3 = bb_pos < 0.1       if direction == "LONG" else bb_pos > 0.9
                if direction == "LONG":
                    strength = max(0.0, min(100.0,
                        (25 - rsi) / 25 * 50 + (1.0 - bb_pos) * 50))
                elif direction == "SHORT":
                    strength = max(0.0, min(100.0,
                        (rsi - 65) / 35 * 50 + bb_pos * 50))
                else:
                    strength = 0.0

            self._append_csv("signals.csv", {
                "timestamp":        str(row.name),
                "strategy":         strategy_label,
                "signal_type":      direction,
                "signal_strength":  round(strength, 2),
                "condition_1_met":  c1,
                "condition_2_met":  c2,
                "condition_3_met":  c3,
                "blocked_by":       blocked_by,
                "rsi_value":        round(rsi, 2),
                "adx_value":        round(adx, 2),
                "bb_position":      round(bb_pos, 4),
            })
        except Exception as e:
            self._err(f"log_signal: {e}")

    # ── DATEI 3: trade_quality.csv ───────────────────────────

    def start_trade_tracking(self, trade_id: str, entry_data: dict):
        """Bei Trade-Entry aufrufen. Initiiert MAE/MFE-Tracking."""
        self._active_trades[trade_id] = {
            "entry":    entry_data,
            "mae":      0.0,
            "mfe":      0.0,
            "entry_ts": datetime.now(timezone.utc),
        }

    def update_trade_tracking(self, trade_id: str, high: float, low: float):
        """Jede Kerze für offene Position aufrufen (MAE/MFE aktualisieren)."""
        t = self._active_trades.get(trade_id)
        if not t:
            return
        entry     = t["entry"]["entry"]
        direction = t["entry"]["direction"]
        adverse   = (entry - low)  if direction == "LONG" else (high - entry)
        favorable = (high - entry) if direction == "LONG" else (entry - low)
        t["mae"] = max(t["mae"], adverse)
        t["mfe"] = max(t["mfe"], favorable)

    def log_trade_quality(self, trade_id: str, exit_data: dict):
        """Bei Trade-Close aufrufen."""
        t = self._active_trades.pop(trade_id, None)
        if not t:
            return
        try:
            ed      = t["entry"]
            pnl     = exit_data["pnl"]
            qty     = ed.get("qty", 1)
            entry_p = ed["entry"]
            mae     = t["mae"]
            mfe     = t["mfe"]
            duration= (datetime.now(timezone.utc) - t["entry_ts"]).total_seconds() / 3600
            pnl_pct = (pnl / (entry_p * qty) * 100) if entry_p > 0 and qty > 0 else 0.0
            eff     = round(pnl / (mfe * qty), 4) if mfe > 0 and qty > 0 else 0.0

            _rmap = {"STOP_LOSS": "SL", "TAKE_PROFIT": "TP", "MEAN_EXIT": "SIGNAL"}
            self._append_csv("trade_quality.csv", {
                "timestamp_entry": ed.get("entry_time", ""),
                "timestamp_exit":  exit_data.get("exit_time", ""),
                "strategy":        ed.get("label", ""),
                "direction":       ed.get("direction", ""),
                "entry_price":     round(entry_p, 4),
                "exit_price":      round(exit_data.get("exit_price", 0), 4),
                "stop_loss":       round(ed.get("stop_loss",   0), 4),
                "take_profit":     round(ed.get("take_profit", 0), 4),
                "pnl":             round(pnl,     4),
                "pnl_pct":         round(pnl_pct, 4),
                "win":             pnl > 0,
                "mae":             round(mae, 4),
                "mfe":             round(mfe, 4),
                "efficiency":      eff,
                "duration_hours":  round(duration, 2),
                "exit_reason":     _rmap.get(exit_data.get("reason", ""), exit_data.get("reason", "")),
            })
        except Exception as e:
            self._err(f"log_trade_quality: {e}")

    # ── DATEI 4: portfolio_snapshots.csv (Thread, stündlich) ──

    def _snapshot_loop(self, get_state_fn: Callable):
        last = None
        while True:
            try:
                now = datetime.now(timezone.utc)
                if last is None or (now - last).total_seconds() >= 3600:
                    state = get_state_fn()
                    price = 0.0
                    if self.exchange:
                        try:
                            price = float(
                                self.exchange.fetch_ticker(
                                    state.get("symbol", "BTC/USDT")
                                )["last"]
                            )
                        except Exception:
                            pass
                    cap   = state.get("capital_total", 10000)
                    t_bal = state["tf"]["balance"] + state["mr"]["balance"]
                    t_pnl = t_bal - cap
                    act   = sum(
                        1 for k in ("tf", "mr")
                        if state[k].get("has_position", False)
                    )
                    self._append_csv("portfolio_snapshots.csv", {
                        "timestamp":             now.isoformat(),
                        "total_balance":         round(t_bal, 4),
                        "total_pnl":             round(t_pnl, 4),
                        "total_pnl_pct":         round(t_pnl / cap * 100, 4) if cap else 0,
                        "tf_balance":            round(state["tf"]["balance"], 4),
                        "tf_pnl":                round(state["tf"]["pnl"],     4),
                        "tf_trades":             state["tf"].get("trades", 0),
                        "tf_wins":               state["tf"].get("wins", 0),
                        "mr_balance":            round(state["mr"]["balance"], 4),
                        "mr_pnl":                round(state["mr"]["pnl"],     4),
                        "mr_trades":             state["mr"].get("trades", 0),
                        "mr_wins":               state["mr"].get("wins", 0),
                        "circuit_breaker_count": state.get("combined_losses", 0),
                        "active_positions":      act,
                        "btc_price":             round(price, 2),
                    })
                    last = now
            except Exception as e:
                self._err(f"snapshot_loop: {e}")
            time.sleep(60)

    # ── DATEI 5: external_market.csv (Thread, stündlich) ──────

    def _external_loop(self):
        last = None
        while True:
            try:
                now = datetime.now(timezone.utc)
                if last is None or (now - last).total_seconds() >= 3600:
                    fg_value = fg_label = btc_vol = btc_chg = None

                    # Fear & Greed Index
                    try:
                        req = urllib.request.Request(
                            "https://api.alternative.me/fng/",
                            headers={"User-Agent": "QuantBotPro/1.0"},
                        )
                        with urllib.request.urlopen(req, timeout=10) as resp:
                            data     = json.loads(resp.read())
                            fg_value = int(data["data"][0]["value"])
                            fg_label = data["data"][0]["value_classification"]
                    except Exception as e:
                        self._err(f"FearGreed API: {e}")

                    # BTC 24h Volume & Change via ccxt
                    if self.exchange:
                        try:
                            ticker  = self.exchange.fetch_ticker("BTC/USDT")
                            btc_vol = float(ticker.get("quoteVolume") or 0)
                            btc_chg = float(ticker.get("percentage")  or 0)
                        except Exception as e:
                            self._err(f"BTC ticker: {e}")

                    self._append_csv("external_market.csv", {
                        "timestamp":          now.isoformat(),
                        "fear_greed_value":   fg_value,
                        "fear_greed_label":   fg_label,
                        "btc_24h_volume":     round(btc_vol, 2) if btc_vol is not None else None,
                        "btc_24h_change_pct": round(btc_chg, 4) if btc_chg is not None else None,
                    })
                    last = now
            except Exception as e:
                self._err(f"external_loop: {e}")
            time.sleep(60)

    # ── DATEI 6: system_health.csv (Thread, alle 15 Min) ──────

    def _health_loop(self):
        last = None
        while True:
            try:
                now = datetime.now(timezone.utc)
                if last is None or (now - last).total_seconds() >= 900:
                    ram_mb = cpu_pct = latency_ms = None

                    if _PSUTIL_OK:
                        try:
                            proc    = _psutil.Process()
                            ram_mb  = round(proc.memory_info().rss / 1024 / 1024, 2)
                            cpu_pct = round(proc.cpu_percent(interval=0.5), 2)
                        except Exception:
                            pass

                    if ram_mb is not None and ram_mb > _RAM_WARN_MB:
                        msg = f"⚠️ RAM kritisch: {ram_mb:.0f}MB — Bot wird neu gestartet"
                        print(f"  [Logger] {msg}", flush=True)
                        if self._notifier:
                            try:
                                self._notifier.send(msg)
                            except Exception:
                                pass
                        sys.exit(1)

                    if self.exchange:
                        try:
                            t0 = time.monotonic()
                            self.exchange.fetch_ohlcv("BTC/USDT", "4h", limit=5)
                            latency_ms = round((time.monotonic() - t0) * 1000, 1)
                            self._last_fetch_ok = now.isoformat()
                        except Exception as e:
                            self._err(f"API health check: {e}")

                    uptime = int((now - self._start_time).total_seconds())
                    self._append_csv("system_health.csv", {
                        "timestamp":             now.isoformat(),
                        "api_latency_ms":        latency_ms,
                        "last_successful_fetch": self._last_fetch_ok,
                        "ram_usage_mb":          ram_mb,
                        "cpu_pct":               cpu_pct,
                        "error_count_total":     self._error_count,
                        "last_error":            self._last_error,
                        "bot_uptime_seconds":    uptime,
                    })
                    last = now
            except Exception as e:
                self._err(f"health_loop: {e}")
            time.sleep(60)

    # ── Hintergrund-Threads starten ───────────────────────────

    def start_background_threads(self, get_state_fn: Callable):
        """
        Startet portfolio_snapshots, external_market und system_health Threads.
        get_state_fn: Callable() → dict (tf/mr balances, pnl, combined_losses etc.)
        """
        threads = [
            threading.Thread(
                target=self._snapshot_loop, args=(get_state_fn,),
                daemon=True, name="Log-Snapshots",
            ),
            threading.Thread(
                target=self._external_loop,
                daemon=True, name="Log-External",
            ),
            threading.Thread(
                target=self._health_loop,
                daemon=True, name="Log-Health",
            ),
        ]
        for t in threads:
            t.start()
        print(f"  [Logger] 6 CSV-Dateien aktiv | 3 Hintergrund-Threads gestartet")
        print(f"  [Logger] Logs in: logs/{datetime.now(timezone.utc).strftime('%Y-%m-%d')}/")
