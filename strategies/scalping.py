"""
================================================================================
  QuantBot Pro — 1h Scalping Strategy
  EMA(5/15)-Crossover + RSI(7) + ADX(14) Regime-Filter

  Timeframe:   1h
  Stop-Loss:   0.5%  (preis-basiert, enger als 4h-Strategien)
  Take-Profit: 1.0%  (2:1 RR)
  Risiko:      1% Kapital pro Trade

  Signal-Logik:
    LONG:  EMA5 kreuzt EMA15 von unten  AND  RSI > 45  AND  ADX > 15
    SHORT: EMA5 kreuzt EMA15 von oben   AND  RSI < 55  AND  ADX > 15
    EXIT:  Gegensignal (Kreuzung in Gegenrichtung) ODER SL / TP
================================================================================
"""

import numpy as np
import pandas as pd

DEFAULTS = {
    "EMA_FAST":           5,
    "EMA_SLOW":           15,
    "RSI_PERIOD":         7,
    "RSI_LONG_MIN":       45,
    "RSI_SHORT_MAX":      55,
    "ADX_PERIOD":         14,
    "ADX_THRESHOLD":      15,
    "ATR_PERIOD":         14,
    "STOP_LOSS_PCT":      0.005,   # 0.5%
    "TAKE_PROFIT_PCT":    0.010,   # 1.0%
    "RISK_PER_TRADE_PCT": 0.01,
}


class ScalpingStrategy:
    """
    1h Scalping-Strategie: EMA(5/15)-Crossover + RSI(7) + ADX-Regime.

    Gleiche Interface wie TrendFollowingStrategy und MeanReversionStrategy —
    austauschbar in portfolio.py, walk_forward.py und bot.py.

    Verwendung:
        strategy = ScalpingStrategy()
        df = strategy.compute(df)
        signal = strategy.signal(df)
        pos = strategy.position_size(balance, entry, atr, direction)
    """

    def __init__(self, params: dict = None, verbose: bool = True):
        p = {**DEFAULTS, **(params or {})}
        self.ema_fast        = p["EMA_FAST"]
        self.ema_slow        = p["EMA_SLOW"]
        self.rsi_period      = p["RSI_PERIOD"]
        self.rsi_long_min    = p["RSI_LONG_MIN"]
        self.rsi_short_max   = p["RSI_SHORT_MAX"]
        self.adx_period      = p["ADX_PERIOD"]
        self.adx_threshold   = p["ADX_THRESHOLD"]
        self.atr_period      = p["ATR_PERIOD"]
        self.stop_loss_pct   = p["STOP_LOSS_PCT"]
        self.take_profit_pct = p["TAKE_PROFIT_PCT"]
        self.risk_pct        = p["RISK_PER_TRADE_PCT"]

        if verbose:
            print(
                f"ScalpingStrategy initialisiert | "
                f"EMA({self.ema_fast}/{self.ema_slow}) | "
                f"RSI({self.rsi_period}) Long>{self.rsi_long_min} Short<{self.rsi_short_max} | "
                f"ADX>{self.adx_threshold} | "
                f"SL {self.stop_loss_pct*100:.1f}% TP {self.take_profit_pct*100:.1f}%"
            )

    # ── Indikatoren ──────────────────────────────────────────

    def _ema(self, s: pd.Series, period: int) -> pd.Series:
        return s.ewm(span=period, adjust=False).mean()

    def _rsi(self, s: pd.Series, period: int) -> pd.Series:
        d    = s.diff()
        gain = d.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
        loss = (-d.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
        rs   = gain / loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    def _atr(self, df: pd.DataFrame, period: int) -> pd.Series:
        h, l, cp = df["high"], df["low"], df["close"].shift(1)
        tr = pd.concat([(h - l), (h - cp).abs(), (l - cp).abs()], axis=1).max(axis=1)
        return tr.ewm(alpha=1/period, adjust=False).mean()

    def _adx(self, df: pd.DataFrame, period: int) -> pd.Series:
        h, l   = df["high"], df["low"]
        up, dn = h.diff(), -l.diff()
        pdm    = np.where((up > dn) & (up > 0), up, 0.0)
        mdm    = np.where((dn > up) & (dn > 0), dn, 0.0)
        atr    = self._atr(df, period)
        pdi    = 100 * pd.Series(pdm, index=df.index).ewm(alpha=1/period, adjust=False).mean() / atr
        mdi    = 100 * pd.Series(mdm, index=df.index).ewm(alpha=1/period, adjust=False).mean() / atr
        dx     = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
        return dx.ewm(alpha=1/period, adjust=False).mean()

    # ── Compute ──────────────────────────────────────────────

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Erwartet DataFrame mit OHLCV-Spalten.
        Gibt df zurück mit Spalten: ema_fast, ema_slow, rsi, atr, adx,
                                    signal_long, signal_short,
                                    signal_exit_long, signal_exit_short
        """
        df    = df.copy()
        close = df["close"]

        df["ema_fast"] = self._ema(close, self.ema_fast)
        df["ema_slow"] = self._ema(close, self.ema_slow)
        df["rsi"]      = self._rsi(close, self.rsi_period)
        df["atr"]      = self._atr(df, self.atr_period)
        df["adx"]      = self._adx(df, self.adx_period)

        cross_up   = (df["ema_fast"] > df["ema_slow"]) & (df["ema_fast"].shift(1) <= df["ema_slow"].shift(1))
        cross_down = (df["ema_fast"] < df["ema_slow"]) & (df["ema_fast"].shift(1) >= df["ema_slow"].shift(1))
        regime     = df["adx"] > self.adx_threshold

        df["signal_long"]  = cross_up   & (df["rsi"] > self.rsi_long_min)  & regime
        df["signal_short"] = cross_down & (df["rsi"] < self.rsi_short_max) & regime

        # Exit: Kreuzung in Gegenrichtung
        df["signal_exit_long"]  = cross_down
        df["signal_exit_short"] = cross_up

        return df

    # ── Signal der letzten abgeschlossenen Kerze ─────────────

    def signal(self, df: pd.DataFrame) -> dict:
        """Gibt Signal von iloc[-2] zurück (letzte abgeschlossene Kerze)."""
        row = df.iloc[-2]
        direction = (
            "LONG"  if row["signal_long"]  else
            "SHORT" if row["signal_short"] else
            "NONE"
        )
        return {
            "timestamp":   str(row.name),
            "close":       float(row["close"]),
            "ema_fast":    float(row["ema_fast"]),
            "ema_slow":    float(row["ema_slow"]),
            "rsi":         float(row["rsi"]),
            "adx":         float(row["adx"]),
            "atr":         float(row["atr"]),
            "direction":   direction,
            "exit_long":   bool(row["signal_exit_long"]),
            "exit_short":  bool(row["signal_exit_short"]),
        }

    # ── Positionsgröße (%-basierter Stop, kein ATR) ──────────

    def position_size(
        self,
        balance:   float,
        entry:     float,
        atr:       float,   # wird akzeptiert aber nicht verwendet
        direction: str,
    ) -> dict:
        """
        Stop-Loss und Take-Profit preis-basiert (nicht ATR).
        SL: 0.5% | TP: 1.0% | Risiko: 1% Kapital
        """
        stop_dist = entry * self.stop_loss_pct
        tp_dist   = entry * self.take_profit_pct
        risk_amt  = balance * self.risk_pct
        qty       = risk_amt / stop_dist

        sl = entry - stop_dist if direction == "LONG" else entry + stop_dist
        tp = entry + tp_dist   if direction == "LONG" else entry - tp_dist

        if sl <= 0:
            raise ValueError(f"Ungültiger Stop-Loss: {sl:.4f}")

        return {
            "direction":   direction,
            "entry_price": entry,
            "quantity":    qty,
            "stop_loss":   sl,
            "take_profit": tp,
            "risk_amount": risk_amt,
            "stop_dist":   stop_dist,
        }

    @property
    def warmup_candles(self) -> int:
        return self.ema_slow + self.rsi_period + 20


# ============================================================
# SELBST-TEST
# ============================================================

if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))

    strategy = ScalpingStrategy()

    n   = 200
    rng = np.random.default_rng(99)
    close = 80_000 + np.cumsum(rng.normal(0, 150, n))
    noise = rng.uniform(-50, 50, n)

    df = pd.DataFrame({
        "open":   close - noise,
        "high":   close + abs(noise) * 1.5,
        "low":    close - abs(noise) * 1.5,
        "close":  close,
        "volume": rng.uniform(500, 5000, n),
    })

    df_out = strategy.compute(df)
    sig    = strategy.signal(df_out)

    print(f"\n  Signal:      {sig['direction']}")
    print(f"  EMA {strategy.ema_fast}/{strategy.ema_slow}: {sig['ema_fast']:.2f} / {sig['ema_slow']:.2f}")
    print(f"  RSI:         {sig['rsi']:.1f}")
    print(f"  ADX:         {sig['adx']:.1f}")
    print(f"  Warmup:      {strategy.warmup_candles} Kerzen")

    pos = strategy.position_size(10_000, sig["close"], sig["atr"], "LONG")
    print(f"\n  Position (LONG, 10k USDT):")
    print(f"  Qty:         {pos['quantity']:.6f}")
    print(f"  SL / TP:     {pos['stop_loss']:.2f} / {pos['take_profit']:.2f}")
    print(f"  Risiko:      {pos['risk_amount']:.2f} USDT (1%)")
    print(f"  Stop-Dist:   {pos['stop_dist']:.2f} USDT ({strategy.stop_loss_pct*100:.1f}%)")

    required = {"ema_fast", "ema_slow", "rsi", "atr", "adx",
                "signal_long", "signal_short",
                "signal_exit_long", "signal_exit_short"}
    missing = required - set(df_out.columns)
    assert not missing, f"Fehlende Spalten: {missing}"
    print(f"\n  Alle Spalten vorhanden ✅")

    from strategies.scalping import ScalpingStrategy as _S
    _S(verbose=False)
    print(f"  Import-Test OK ✅")
