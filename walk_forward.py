"""
================================================================================
  WALK-FORWARD VALIDATION v1.0
  Robustheits-Test fuer die EMA/RSI/ADX Trendfolge-Strategie

  Was dieses Modul prueft:
  ─────────────────────────────────────────────────────────────────
  Ein normaler Backtest hat ein verstecktes Problem: Die Parameter
  (EMA 21/55, RSI 55, ADX 25) wurden anhand historischer Daten
  gewaehlt. Damit hat die Strategie diese Daten schon "gesehen" —
  ein fairer Test ist das nicht. Man nennt dieses Problem Overfitting.

  Walk-Forward loest das:
  - Die Daten werden in mehrere Zeitfenster aufgeteilt
  - Jedes Fenster hat einen TRAIN-Teil (70%) und einen TEST-Teil (30%)
  - Nur die TEST-Ergebnisse zaehlen (Out-of-Sample)
  - Wenn die Strategie auf ALLEN Test-Fenstern funktioniert,
    ist das ein echter Beweis fuer Robustheit

  Nutzung:
  ─────────────────────────────────────────────────────────────────
    # Standard: BTC/USDT, 2 Jahre, 4 Fenster
    python walk_forward.py

    # Eigene Parameter
    python walk_forward.py --symbol ETH/USDT --days 365 --windows 6

    # Alle Symbole aus bot.py CONFIG
    python walk_forward.py --all-symbols

    # Mit Parameter-Sweep (welche EMA-Combo ist am robustesten?)
    python walk_forward.py --optimize
================================================================================
"""

import json
import argparse
import itertools
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Optional

import sys

import ccxt
import pandas as pd
import numpy as np

# Strategien optional importieren (graceful fallback wenn Pfad fehlt)
sys.path.insert(0, ".")
try:
    from strategies.mean_reversion  import MeanReversionStrategy
    from strategies.trend_following import TrendFollowingStrategy
    _STRATEGIES_AVAILABLE = True
except ImportError:
    _STRATEGIES_AVAILABLE = False


# ============================================================
# 0. KONFIGURATION
# ============================================================

# Strategie-Parameter (muessen mit bot.py uebereinstimmen)
BASE_CONFIG = {
    "EMA_FAST":             10,
    "EMA_SLOW":             21,
    "RSI_PERIOD":           10,
    "RSI_LONG_MIN":         48,
    "RSI_SHORT_MAX":        52,
    "ATR_PERIOD":           14,
    "ATR_MULTIPLIER":       2.0,
    "ADX_PERIOD":           14,
    "ADX_THRESHOLD":        25,
    "VOLUME_FACTOR":        1.2,
    "REWARD_RISK_RATIO":    1.5,
    "RISK_PER_TRADE_PCT":   0.01,
    "LOSS_COOLDOWN_BARS":   3,
}

# Mean Reversion — optimierte Parameter (PO-Entscheidung Story-07-B-FIX)
MR_BASE_CONFIG = {
    "BB_PERIOD":          25,
    "BB_STD":             3.0,
    "RSI_PERIOD":         14,
    "RSI_LONG":           25,
    "RSI_SHORT":          65,
    # Risiko-Parameter identisch zu BASE_CONFIG
    "ATR_PERIOD":         14,
    "ATR_MULTIPLIER":     2.0,
    "REWARD_RISK_RATIO":  1.5,
    "RISK_PER_TRADE_PCT": 0.01,
    "LOSS_COOLDOWN_BARS": 3,
}

# Parameter-Bereiche fuer optionalen Sweep
PARAM_GRID = {
    "EMA_FAST":         [8, 10, 13],
    "EMA_SLOW":         [21, 26, 34],
    "RSI_LONG_MIN":     [48, 52, 55],
    "ADX_THRESHOLD":    [20, 25, 30],
}

WF_CONFIG = {
    "TRAIN_RATIO":      0.70,       # 70% Training, 30% Test
    "MIN_TRADES":       2,          # Mindest-Trades pro Fenster fuer Gueltigkeit
    "LOG_DIR":          "logs",
    "START_BALANCE":    10_000.0,
    "TIMEFRAME":        "4h",
    "DEFAULT_DAYS":     730,        # 2 Jahre
    "DEFAULT_WINDOWS":  4,
}

SYMBOLS_DEFAULT = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]


# ============================================================
# 1. DATENKLASSEN — saubere Typen fuer alle Ergebnisse
# ============================================================

@dataclass
class TradeRecord:
    entry_time:  str
    exit_time:   str
    direction:   str
    entry:       float
    exit:        float
    pnl:         float
    reason:      str
    window_id:   int
    phase:       str        # "TRAIN" oder "TEST"

@dataclass
class WindowResult:
    window_id:      int
    train_start:    str
    train_end:      str
    test_start:     str
    test_end:       str
    train_trades:   int
    train_pnl:      float
    train_winrate:  float
    test_trades:    int
    test_pnl:       float
    test_winrate:   float
    test_maxdd:     float
    test_pf:        float       # Profit Factor
    is_valid:       bool        # Genuegend Trades?
    is_profitable:  bool

@dataclass
class WalkForwardResult:
    symbol:             str
    timeframe:          str
    total_days:         int
    n_windows:          int
    params:             dict
    windows:            list[WindowResult] = field(default_factory=list)
    all_test_trades:    list[TradeRecord]  = field(default_factory=list)

    # Aggregate (werden nach Berechnung gesetzt)
    consistency_rate:   float = 0.0    # Anteil profitabler Test-Fenster
    efficiency_ratio:   float = 0.0    # Out-of-Sample / In-Sample Performance
    avg_test_pnl:       float = 0.0
    total_test_pnl:     float = 0.0
    total_test_trades:  int   = 0
    overall_winrate:    float = 0.0
    overall_maxdd:      float = 0.0
    avg_profit_factor:  float = 0.0
    stability_score:    float = 0.0    # 1 - (StdDev / Mean) der Fenster-Returns
    verdict:            str   = ""     # ROBUST / FRAGIL / OVERFITTED


# ============================================================
# 2. SIGNAL-GENERATOR (eigenstaendig, unabhaengig von bot.py)
# ============================================================

class SignalEngine:
    """
    Berechnet alle Indikatoren und Signale.
    Akzeptiert ein params-Dict damit Walk-Forward verschiedene
    Parameter-Sets testen kann.
    """
    def __init__(self, params: dict):
        self.p = params

    def _ema(self, s, period):
        return s.ewm(span=period, adjust=False).mean()

    def _rsi(self, s, period):
        d    = s.diff()
        gain = d.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
        loss = (-d.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
        rs   = gain / loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    def _atr(self, df, period):
        h, l, cp = df["high"], df["low"], df["close"].shift(1)
        tr = pd.concat([(h-l), (h-cp).abs(), (l-cp).abs()], axis=1).max(axis=1)
        return tr.ewm(alpha=1/period, adjust=False).mean()

    def _adx(self, df, period):
        h, l   = df["high"], df["low"]
        up, dn = h.diff(), -l.diff()
        pdm    = np.where((up > dn) & (up > 0), up, 0.0)
        mdm    = np.where((dn > up) & (dn > 0), dn, 0.0)
        atr    = self._atr(df, period)
        pdi    = 100 * pd.Series(pdm, index=df.index).ewm(alpha=1/period, adjust=False).mean() / atr
        mdi    = 100 * pd.Series(mdm, index=df.index).ewm(alpha=1/period, adjust=False).mean() / atr
        dx     = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
        return dx.ewm(alpha=1/period, adjust=False).mean()

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        p  = self.p
        df["ema_fast"] = self._ema(df["close"], p["EMA_FAST"])
        df["ema_slow"] = self._ema(df["close"], p["EMA_SLOW"])
        df["rsi"]      = self._rsi(df["close"], p["RSI_PERIOD"])
        df["atr"]      = self._atr(df, p["ATR_PERIOD"])
        df["adx"]      = self._adx(df, p["ADX_PERIOD"])
        df["vol_ma20"] = df["volume"].rolling(20).mean()

        cross_up   = (df["ema_fast"] > df["ema_slow"]) & (df["ema_fast"].shift(1) <= df["ema_slow"].shift(1))
        cross_down = (df["ema_fast"] < df["ema_slow"]) & (df["ema_fast"].shift(1) >= df["ema_slow"].shift(1))
        regime     = df["adx"] > p["ADX_THRESHOLD"]
        vol_ok     = df["volume"] > df["vol_ma20"] * p["VOLUME_FACTOR"]

        df["signal_long"]  = cross_up   & (df["rsi"] > p["RSI_LONG_MIN"])  & regime & vol_ok
        df["signal_short"] = cross_down & (df["rsi"] < p["RSI_SHORT_MAX"]) & regime & vol_ok
        return df


# ============================================================
# 3. SIMULATIONS-KERN — laeuft auf einem beliebigen DF-Ausschnitt
# ============================================================

def simulate_window(
    df: pd.DataFrame,
    params: dict,
    window_id: int,
    phase: str,
    start_balance: float = 10_000.0,
    strategy=None,          # None → TrendFollowing via SignalEngine (default)
) -> tuple[list[TradeRecord], list[float]]:
    """
    Simuliert die Handelsstrategie auf einem DataFrame-Ausschnitt.

    strategy=None → bestehende SignalEngine (Trend-Following)
    strategy=<Objekt> → strategy.compute(df) + optionale Band-Exits
    """
    if strategy is not None:
        df     = strategy.compute(df)
        warmup = strategy.warmup_candles
        has_band_exit = "signal_exit_long" in df.columns
    else:
        engine = SignalEngine(params)
        df     = engine.compute(df)
        warmup = params["EMA_SLOW"] + 20
        has_band_exit = False

    balance  = start_balance
    equity   = [balance]
    trades   = []
    position = None
    cooldown = 0

    for i in range(warmup, len(df) - 1):
        row   = df.iloc[i]
        lo    = float(row["low"])
        hi    = float(row["high"])
        price = float(row["close"])

        # --- Offene Position managen ---
        if position:
            sl, tp = position["stop_loss"], position["take_profit"]

            hit_sl = ((position["direction"] == "LONG"  and lo <= sl) or
                      (position["direction"] == "SHORT" and hi >= sl))
            hit_tp = ((position["direction"] == "LONG"  and hi >= tp) or
                      (position["direction"] == "SHORT" and lo <= tp))

            hit_mid = (
                has_band_exit and not hit_sl and not hit_tp and (
                    (position["direction"] == "LONG"  and bool(row["signal_exit_long"]))  or
                    (position["direction"] == "SHORT" and bool(row["signal_exit_short"]))
                )
            )

            if hit_sl or hit_tp or hit_mid:
                if hit_mid:
                    exit_price = price
                    reason     = "MEAN_EXIT"
                else:
                    exit_price = sl if hit_sl else tp
                    reason     = "STOP_LOSS" if hit_sl else "TAKE_PROFIT"
                pnl = ((exit_price - position["entry"]) * position["qty"]
                       if position["direction"] == "LONG"
                       else (position["entry"] - exit_price) * position["qty"])
                balance += pnl
                trades.append(TradeRecord(
                    entry_time = position["entry_time"],
                    exit_time  = str(row.name),
                    direction  = position["direction"],
                    entry      = round(position["entry"], 4),
                    exit       = round(exit_price, 4),
                    pnl        = round(pnl, 4),
                    reason     = reason,
                    window_id  = window_id,
                    phase      = phase,
                ))
                if hit_sl:
                    cooldown = params["LOSS_COOLDOWN_BARS"]
                position = None
            elif not has_band_exit:
                # Trailing Stop nur für Trend-Following
                atr    = float(row["atr"])
                new_sl = (price - atr * params["ATR_MULTIPLIER"]
                          if position["direction"] == "LONG"
                          else price + atr * params["ATR_MULTIPLIER"])
                if position["direction"] == "LONG" and new_sl > position["stop_loss"]:
                    position["stop_loss"] = new_sl
                elif position["direction"] == "SHORT" and new_sl < position["stop_loss"]:
                    position["stop_loss"] = new_sl

        # --- Neuen Trade eroeffnen? ---
        elif cooldown > 0:
            cooldown -= 1

        elif row["signal_long"] or row["signal_short"]:
            direction = "LONG" if row["signal_long"] else "SHORT"
            atr       = float(row["atr"])
            if atr <= 0:
                continue
            stop_dist = atr * params["ATR_MULTIPLIER"]
            qty       = (balance * params["RISK_PER_TRADE_PCT"]) / stop_dist
            sl  = price - stop_dist if direction == "LONG" else price + stop_dist
            tp  = (price + stop_dist * params["REWARD_RISK_RATIO"] if direction == "LONG"
                   else price - stop_dist * params["REWARD_RISK_RATIO"])
            if sl <= 0:
                continue
            position = {
                "direction":  direction, "entry": price,
                "entry_time": str(row.name), "qty": qty,
                "stop_loss":  sl,          "take_profit": tp,
            }

        equity.append(balance)

    return trades, equity


# ============================================================
# 4. KENNZAHLEN-BERECHNUNG
# ============================================================

def compute_metrics(trades: list[TradeRecord], equity: list[float]) -> dict:
    """Berechnet alle Performance-Kennzahlen fuer einen Trades-Satz."""
    if not trades:
        return {
            "n_trades": 0, "win_rate": 0.0, "total_pnl": 0.0,
            "max_drawdown": 0.0, "profit_factor": 0.0, "avg_rr": 0.0,
        }

    pnls  = [t.pnl for t in trades]
    wins  = [p for p in pnls if p > 0]
    losses= [p for p in pnls if p <= 0]

    # Max Drawdown
    eq    = pd.Series(equity)
    peak  = eq.cummax()
    dd    = ((eq - peak) / peak * 100)
    maxdd = float(dd.min())

    # Profit Factor
    gp = sum(wins)   if wins   else 0
    gl = abs(sum(losses)) if losses else 1e-9
    pf = gp / gl

    # Avg RR
    avg_w = (sum(wins) / len(wins))     if wins   else 0
    avg_l = (abs(sum(losses))/len(losses)) if losses else 1e-9
    avg_rr = avg_w / avg_l

    return {
        "n_trades":      len(trades),
        "win_rate":      len(wins) / len(trades) * 100,
        "total_pnl":     round(sum(pnls), 4),
        "max_drawdown":  round(maxdd, 2),
        "profit_factor": round(pf, 3),
        "avg_rr":        round(avg_rr, 3),
    }


# ============================================================
# 5. WALK-FORWARD ENGINE
# ============================================================

class WalkForwardEngine:
    """
    Fuehrt den vollstaendigen Walk-Forward-Test durch.

    Algorithmus:
    1. Gesamtdaten laden
    2. In N Fenster aufteilen (rollierend, 50% Overlap)
    3. Jedes Fenster: 70% Train simulieren, 30% Test simulieren
    4. Nur Test-Ergebnisse aggregieren
    5. Verdikt berechnen
    """
    def __init__(self, exchange: ccxt.Exchange):
        self.exchange = exchange

    def _load_data(self, symbol: str, timeframe: str, days: int) -> pd.DataFrame:
        """Laedt historische Daten via Pagination (kein 1000-Kerzen-Limit)."""
        candles_per_day = {"1h": 24, "4h": 6, "1d": 1}.get(timeframe, 6)
        needed  = days * candles_per_day + 300
        batch   = 1000
        tf_ms   = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}.get(timeframe, 14_400_000)
        since   = self.exchange.milliseconds() - needed * tf_ms

        print(f"  Lade ~{needed} Kerzen fuer {symbol} [{timeframe}]...", end=" ", flush=True)
        all_rows = []
        while True:
            chunk = self.exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=batch)
            if not chunk:
                break
            all_rows.extend(chunk)
            if len(chunk) < batch:
                break
            since = chunk[-1][0] + tf_ms

        df = pd.DataFrame(all_rows, columns=["timestamp","open","high","low","close","volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.set_index("timestamp").sort_index()
        df = df[~df.index.duplicated(keep="last")]
        print(f"OK ({len(df)} Kerzen)")
        return df

    def _split_windows(self, df: pd.DataFrame, n_windows: int) -> list[dict]:
        """
        Erstellt N rollierende Fenster mit 50% Overlap.
        Jedes Fenster: [train_start, train_end, test_start, test_end]
        """
        total  = len(df)
        # Fenstergroesse so berechnen dass alle N Fenster die Daten abdecken
        window_size = total // (n_windows // 2 + 1)
        step        = window_size // 2

        windows = []
        for i in range(n_windows):
            start = i * step
            end   = start + window_size
            if end > total:
                break
            split = start + int((end - start) * WF_CONFIG["TRAIN_RATIO"])
            windows.append({
                "id":          i + 1,
                "train_df":    df.iloc[start:split],
                "test_df":     df.iloc[split:end],
                "train_start": str(df.index[start].date()),
                "train_end":   str(df.index[split - 1].date()),
                "test_start":  str(df.index[split].date()),
                "test_end":    str(df.index[min(end - 1, total - 1)].date()),
            })
        return windows

    def run(
        self,
        symbol:     str,
        timeframe:  str,
        days:       int,
        n_windows:  int,
        params:     dict,
        label:      str = "",
        strategy=None,      # None → Trend-Following via SignalEngine
    ) -> WalkForwardResult:

        print(f"\n{'─'*60}")
        print(f"  Walk-Forward: {symbol} | {timeframe} | {days}d | {n_windows} Fenster")
        if label:
            print(f"  Parameter-Set: {label}")
        print(f"{'─'*60}")

        df      = self._load_data(symbol, timeframe, days)
        windows = self._split_windows(df, n_windows)

        result = WalkForwardResult(
            symbol    = symbol,
            timeframe = timeframe,
            total_days= days,
            n_windows = len(windows),
            params    = params,
        )

        all_test_trades = []

        for w in windows:
            wid = w["id"]
            print(f"\n  Fenster {wid}/{len(windows)}: "
                  f"Train [{w['train_start']} → {w['train_end']}] "
                  f"Test [{w['test_start']} → {w['test_end']}]")

            # TRAIN-Phase
            train_trades, train_equity = simulate_window(
                w["train_df"], params, wid, "TRAIN",
                WF_CONFIG["START_BALANCE"], strategy=strategy,
            )
            tm = compute_metrics(train_trades, train_equity)
            print(f"    Train: {tm['n_trades']:>3} Trades | "
                  f"PnL: {tm['total_pnl']:>+8.2f} | "
                  f"Win: {tm['win_rate']:>5.1f}%")

            # TEST-Phase (Out-of-Sample — das einzige was zaehlt!)
            test_trades, test_equity = simulate_window(
                w["test_df"], params, wid, "TEST",
                WF_CONFIG["START_BALANCE"], strategy=strategy,
            )
            xm = compute_metrics(test_trades, test_equity)
            valid     = xm["n_trades"] >= WF_CONFIG["MIN_TRADES"]
            profitable= xm["total_pnl"] > 0

            status = "✅" if profitable else ("⚠️ " if valid else "❌ (zu wenig Trades)")
            print(f"    Test:  {xm['n_trades']:>3} Trades | "
                  f"PnL: {xm['total_pnl']:>+8.2f} | "
                  f"Win: {xm['win_rate']:>5.1f}% | "
                  f"DD: {xm['max_drawdown']:>+6.2f}% {status}")

            wr = WindowResult(
                window_id     = wid,
                train_start   = w["train_start"],
                train_end     = w["train_end"],
                test_start    = w["test_start"],
                test_end      = w["test_end"],
                train_trades  = tm["n_trades"],
                train_pnl     = tm["total_pnl"],
                train_winrate = tm["win_rate"],
                test_trades   = xm["n_trades"],
                test_pnl      = xm["total_pnl"],
                test_winrate  = xm["win_rate"],
                test_maxdd    = xm["max_drawdown"],
                test_pf       = xm["profit_factor"],
                is_valid      = valid,
                is_profitable = profitable and valid,
            )
            result.windows.append(wr)
            all_test_trades.extend(test_trades)

        result.all_test_trades = all_test_trades
        self._aggregate(result)
        return result

    def _aggregate(self, r: WalkForwardResult):
        """Berechnet uebergreifende Kennzahlen und das finale Verdikt."""

        # OOS-Aggregate immer aus ALLEN Test-Trades — unabhaengig von Fenster-Validitaet.
        # So gehen keine Trades durch den "invalid window" Filter verloren.
        all_pnls = [t.pnl for t in r.all_test_trades]
        r.total_test_trades = len(all_pnls)

        if all_pnls:
            wins   = [p for p in all_pnls if p > 0]
            losses = [p for p in all_pnls if p <= 0]
            r.total_test_pnl    = round(float(sum(all_pnls)), 4)
            r.overall_winrate   = len(wins) / len(all_pnls) * 100
            gp = sum(wins) if wins else 0
            gl = abs(sum(losses)) if losses else 1e-9
            r.avg_profit_factor = gp / gl

            eq_series = pd.Series(all_pnls).cumsum() + WF_CONFIG["START_BALANCE"]
            peak      = eq_series.cummax()
            r.overall_maxdd = float(((eq_series - peak) / peak * 100).min())

        valid_windows = [w for w in r.windows if w.is_valid]
        if not valid_windows:
            r.verdict = "UNGUELTIG (zu wenig Trades)"
            return

        profitable = [w for w in valid_windows if w.is_profitable]
        r.consistency_rate = len(profitable) / len(valid_windows)

        test_pnls  = [w.test_pnl  for w in valid_windows]
        train_pnls = [w.train_pnl for w in valid_windows]

        r.avg_test_pnl = float(np.mean(test_pnls))

        # Effizienz: Wie gut ist Out-of-Sample vs In-Sample?
        avg_train = float(np.mean(train_pnls))
        r.efficiency_ratio = (r.avg_test_pnl / abs(avg_train)
                              if abs(avg_train) > 1.0 else 0.0)

        # Stabilitaet: Niedrige StdDev der Test-Returns = konsistent
        if len(test_pnls) > 1 and abs(np.mean(test_pnls)) > 0:
            r.stability_score = max(0, 1 - (np.std(test_pnls) / abs(np.mean(test_pnls))))
        else:
            r.stability_score = 0.0

        # VERDIKT
        if (r.consistency_rate >= 0.60 and
            r.overall_maxdd > -15 and
            r.avg_profit_factor >= 1.3 and
            r.total_test_pnl > 0):
            r.verdict = "ROBUST"
        elif r.total_test_pnl > 0 and r.consistency_rate >= 0.40:
            r.verdict = "FRAGIL"
        else:
            r.verdict = "OVERFITTED"


# ============================================================
# 6. PARAMETER-SWEEP (optional)
# ============================================================

class ParameterOptimizer:
    """
    Testet alle Kombinationen aus PARAM_GRID via Walk-Forward.
    
    WICHTIG: Das ist kein klassisches Curve-Fitting!
    Jede Kombination wird walk-forward getestet, nicht auf
    dem gesamten Datensatz. Trotzdem: Das beste Ergebnis
    immer noch auf echtem Paper-Trading validieren.
    """
    def __init__(self, engine: WalkForwardEngine):
        self.engine = engine

    def run(self, symbol: str, timeframe: str, days: int, n_windows: int) -> list[dict]:
        # Alle Kombinationen aus dem Grid generieren
        keys   = list(PARAM_GRID.keys())
        values = list(PARAM_GRID.values())
        combos = list(itertools.product(*values))

        print(f"\n{'='*60}")
        print(f"  PARAMETER-SWEEP: {len(combos)} Kombinationen")
        print(f"  Symbol: {symbol} | Fenster: {n_windows}")
        print(f"{'='*60}")

        results = []
        for i, combo in enumerate(combos):
            params = {**BASE_CONFIG}
            label_parts = []
            for key, val in zip(keys, combo):
                params[key] = val
                label_parts.append(f"{key}={val}")
            label = " | ".join(label_parts)

            print(f"\n  [{i+1}/{len(combos)}] {label}")

            try:
                r = self.engine.run(symbol, timeframe, days, n_windows, params, label)
                results.append({
                    "params":           params,
                    "label":            label,
                    "verdict":          r.verdict,
                    "consistency_rate": round(r.consistency_rate, 3),
                    "total_test_pnl":   round(r.total_test_pnl, 2),
                    "overall_winrate":  round(r.overall_winrate, 1),
                    "avg_profit_factor":round(r.avg_profit_factor, 3),
                    "overall_maxdd":    round(r.overall_maxdd, 2),
                    "efficiency_ratio": round(r.efficiency_ratio, 3),
                    "stability_score":  round(r.stability_score, 3),
                })
            except Exception as e:
                print(f"    Fehler: {e}")

        # Sortieren: Erst nach Verdict, dann nach Konsistenz
        verdict_order = {"ROBUST": 0, "FRAGIL": 1, "OVERFITTED": 2, "UNGUELTIG": 3}
        results.sort(key=lambda x: (
            verdict_order.get(x["verdict"], 9),
            -x["consistency_rate"],
            -x["total_test_pnl"]
        ))

        return results


# ============================================================
# 7. REPORTING — saubere Ausgabe & JSON-Export
# ============================================================

def print_single_result(r: WalkForwardResult):
    """Gibt das Ergebnis eines einzelnen Walk-Forward-Tests aus."""
    verdict_display = {
        "ROBUST":     "✅  ROBUST     — Live-Test vertretbar",
        "FRAGIL":     "⚠️   FRAGIL     — Parameter ueberdenken",
        "OVERFITTED": "❌  OVERFITTED — Nicht live einsetzen!",
    }.get(r.verdict, f"❓  {r.verdict}")

    print(f"\n{'='*60}")
    print(f"  WALK-FORWARD ERGEBNIS — {r.symbol}")
    print(f"{'='*60}")
    print(f"  Zeitraum:            {r.total_days} Tage | {r.n_windows} Fenster")
    print(f"  Gueltige Fenster:    {sum(1 for w in r.windows if w.is_valid)}/{r.n_windows}")
    print()
    print(f"  Out-of-Sample Kennzahlen (nur TEST-Phasen):")
    print(f"  {'─'*48}")
    print(f"  Konsistenz-Rate:     {r.consistency_rate:.0%}  (Ziel: >60%)")
    print(f"  Gesamt Test-PnL:     {r.total_test_pnl:>+10.2f} USDT")
    print(f"  Gesamt Test-Trades:  {r.total_test_trades:>10}")
    print(f"  Win-Rate (OOS):      {r.overall_winrate:>9.1f}%")
    print(f"  Profit Factor (OOS): {r.avg_profit_factor:>10.2f}  (Ziel: >1.3)")
    print(f"  Max. Drawdown (OOS): {r.overall_maxdd:>+9.2f}%  (Ziel: >-15%)")
    print(f"  Effizienz-Ratio:     {r.efficiency_ratio:>10.2f}  (Ziel: >0.5)")
    print(f"  Stabilitaets-Score:  {r.stability_score:>10.2f}  (Ziel: >0.5)")
    print(f"  {'─'*48}")
    print()
    print(f"  VERDIKT: {verdict_display}")
    print(f"{'='*60}\n")


def print_sweep_results(results: list[dict]):
    """Gibt die Top-5 Parameter-Kombinationen aus dem Sweep aus."""
    print(f"\n{'='*60}")
    print(f"  PARAMETER-SWEEP ERGEBNISSE — Top Kombinationen")
    print(f"{'='*60}")

    for i, r in enumerate(results[:5]):
        print(f"\n  #{i+1} [{r['verdict']}] {r['label']}")
        print(f"       Konsistenz: {r['consistency_rate']:.0%} | "
              f"PnL: {r['total_test_pnl']:+.2f} | "
              f"PF: {r['avg_profit_factor']:.2f} | "
              f"DD: {r['overall_maxdd']:+.1f}%")

    if results:
        best = results[0]
        print(f"\n  Empfohlene Parameter:")
        for k in PARAM_GRID.keys():
            print(f"    {k}: {best['params'][k]}")
    print()


def save_results(data: dict, filename: str):
    Path(WF_CONFIG["LOG_DIR"]).mkdir(exist_ok=True)
    path = f"{WF_CONFIG['LOG_DIR']}/{filename}"
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"  Gespeichert: {path}")


# ============================================================
# 8. EINSTIEGSPUNKT & CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Walk-Forward Validation fuer den Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Beispiele:
  python walk_forward.py                          # BTC/USDT, 2 Jahre, 4 Fenster
  python walk_forward.py --symbol ETH/USDT        # anderes Symbol
  python walk_forward.py --days 365 --windows 6   # kuerzerer Zeitraum, mehr Fenster
  python walk_forward.py --all-symbols             # alle Symbole aus SYMBOLS_DEFAULT
  python walk_forward.py --optimize                # Parameter-Sweep (langsamer)
        """
    )
    parser.add_argument("--symbol",      default="BTC/USDT",              help="Handelspaar")
    parser.add_argument("--days",        type=int, default=WF_CONFIG["DEFAULT_DAYS"],    help="Zeitraum in Tagen")
    parser.add_argument("--windows",     type=int, default=WF_CONFIG["DEFAULT_WINDOWS"], help="Anzahl Fenster")
    parser.add_argument("--timeframe",   default=WF_CONFIG["TIMEFRAME"],  help="Kerzen-Zeitrahmen")
    parser.add_argument("--all-symbols", action="store_true",             help="Alle Standard-Symbole testen")
    parser.add_argument("--optimize",    action="store_true",             help="Parameter-Sweep aktivieren")
    parser.add_argument("--strategy",    default="trend",
                        choices=["trend", "mean_reversion"],
                        help="Strategie: trend (default) | mean_reversion")
    args = parser.parse_args()

    print("""
+============================================================+
|   WALK-FORWARD VALIDATION v1.0                             |
|   Out-of-Sample Robustheits-Test                           |
+============================================================+
    """)

    # Strategie-Objekt und zugehörige Parameter wählen
    active_strategy = None
    active_params   = BASE_CONFIG

    if args.strategy == "mean_reversion":
        if not _STRATEGIES_AVAILABLE:
            print("  FEHLER: strategies/ Paket nicht gefunden. Abbruch.")
            sys.exit(1)
        active_strategy = MeanReversionStrategy(
            bb_period  = MR_BASE_CONFIG["BB_PERIOD"],
            bb_std     = MR_BASE_CONFIG["BB_STD"],
            rsi_period = MR_BASE_CONFIG["RSI_PERIOD"],
            rsi_long   = MR_BASE_CONFIG["RSI_LONG"],
            rsi_short  = MR_BASE_CONFIG["RSI_SHORT"],
            verbose    = True,
        )
        active_params = MR_BASE_CONFIG
        print(f"  Strategie: Mean Reversion (BB {MR_BASE_CONFIG['BB_PERIOD']}/{MR_BASE_CONFIG['BB_STD']}, RSI {MR_BASE_CONFIG['RSI_PERIOD']})")
    elif args.strategy == "trend":
        if _STRATEGIES_AVAILABLE:
            active_strategy = TrendFollowingStrategy(params=BASE_CONFIG, verbose=True)
        # active_params bleibt BASE_CONFIG; SignalEngine als Fallback falls Import fehlt
        print(f"  Strategie: Trend Following (EMA {BASE_CONFIG['EMA_FAST']}/{BASE_CONFIG['EMA_SLOW']}, ADX {BASE_CONFIG['ADX_THRESHOLD']})")

    exchange = ccxt.binance({"enableRateLimit": True})
    engine   = WalkForwardEngine(exchange)

    symbols = SYMBOLS_DEFAULT if args.all_symbols else [args.symbol]
    ts      = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    if args.optimize:
        # Parameter-Sweep (nur auf einem Symbol, weil rechenintensiv)
        optimizer = ParameterOptimizer(engine)
        sweep_results = optimizer.run(
            args.symbol, args.timeframe, args.days, args.windows
        )
        print_sweep_results(sweep_results)
        save_results(sweep_results, f"sweep_{args.symbol.replace('/', '_')}_{ts}.json")

    else:
        # Standard Walk-Forward pro Symbol
        all_results = {}
        for symbol in symbols:
            try:
                result = engine.run(
                    symbol, args.timeframe, args.days,
                    args.windows, active_params,
                    strategy=active_strategy,
                )
                print_single_result(result)

                # Serialisierbar machen
                result_dict = asdict(result)
                result_dict["all_test_trades"] = [asdict(t) for t in result.all_test_trades]
                result_dict["windows"]         = [asdict(w) for w in result.windows]
                all_results[symbol] = result_dict

                safe_sym = symbol.replace("/", "_")
                save_results(result_dict, f"walkforward_{safe_sym}_{ts}.json")

            except Exception as e:
                print(f"\n  Fehler bei {symbol}: {e}")

        # Zusammenfassung wenn mehrere Symbole
        if len(symbols) > 1:
            print(f"\n{'='*60}")
            print(f"  ZUSAMMENFASSUNG — Alle Symbole")
            print(f"{'='*60}")
            for sym, r in all_results.items():
                print(f"  {sym:<12} {r['verdict']:<12} "
                      f"Konsistenz: {r['consistency_rate']:.0%} | "
                      f"PnL: {r['total_test_pnl']:>+8.2f}")
            print()


if __name__ == "__main__":
    main()
