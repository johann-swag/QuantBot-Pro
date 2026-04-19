"""
================================================================================
  QuantBot Pro — Mean Reversion Strategy
  Strategie: Bollinger Bands (20, 2.0) + RSI (14)

  Signal-Logik:
    LONG:  close < lower_band  AND  rsi < 35
    SHORT: close > upper_band  AND  rsi > 65
    EXIT:  close kreuzt middle_band (SMA 20)

  Risiko: 1% Kapital pro Trade (identisch zu bot.py)
================================================================================
"""

import numpy as np
import pandas as pd


RISK_PER_TRADE_PCT = 0.01   # 1% — spiegelt bot.py CONFIG
ATR_MULTIPLIER     = 2.0
REWARD_RISK_RATIO  = 1.5


class MeanReversionStrategy:
    """
    Mean-Reversion-Strategie auf Basis von Bollinger Bands und RSI.

    Verwendung:
        strategy = MeanReversionStrategy()
        df = strategy.compute(df)          # Indikatoren hinzufügen
        signal = strategy.signal(df)       # "LONG" | "SHORT" | "NONE"
        pos = strategy.position_size(balance, entry, atr)
    """

    def __init__(
        self,
        bb_period:   int   = 20,
        bb_std:      float = 2.0,
        rsi_period:  int   = 14,
        rsi_long:    float = 35.0,
        rsi_short:   float = 65.0,
        verbose:     bool  = True,
    ):
        self.bb_period  = bb_period
        self.bb_std     = bb_std
        self.rsi_period = rsi_period
        self.rsi_long   = rsi_long
        self.rsi_short  = rsi_short

        if verbose:
            print(
                f"MeanReversionStrategy initialisiert | "
                f"BB({bb_period}, {bb_std}) | "
                f"RSI({rsi_period}) | "
                f"Long<{rsi_long} Short>{rsi_short}"
            )

    # ── Indikatoren ──────────────────────────────────────────

    def _bollinger(self, close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
        """Gibt (upper, middle, lower) zurück."""
        middle = close.rolling(self.bb_period).mean()
        std    = close.rolling(self.bb_period).std(ddof=0)
        upper  = middle + self.bb_std * std
        lower  = middle - self.bb_std * std
        return upper, middle, lower

    def _rsi(self, close: pd.Series) -> pd.Series:
        d    = close.diff()
        gain = d.clip(lower=0).ewm(alpha=1 / self.rsi_period, adjust=False).mean()
        loss = (-d.clip(upper=0)).ewm(alpha=1 / self.rsi_period, adjust=False).mean()
        rs   = gain / loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    def _atr(self, df: pd.DataFrame) -> pd.Series:
        h, l, cp = df["high"], df["low"], df["close"].shift(1)
        tr = pd.concat([(h - l), (h - cp).abs(), (l - cp).abs()], axis=1).max(axis=1)
        return tr.ewm(alpha=1 / 14, adjust=False).mean()

    # ── Compute — fügt alle Indikatoren in df ein ────────────

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Erwartet DataFrame mit OHLCV-Spalten.
        Gibt df zurück mit zusätzlichen Spalten:
          bb_upper, bb_middle, bb_lower, rsi, atr,
          signal_long, signal_short, signal_exit
        """
        df = df.copy()
        close = df["close"]

        df["bb_upper"], df["bb_middle"], df["bb_lower"] = self._bollinger(close)
        df["rsi"] = self._rsi(close)
        df["atr"] = self._atr(df)

        # Einstieg: Preis außerhalb der Bänder + RSI-Bestätigung
        df["signal_long"]  = (close < df["bb_lower"]) & (df["rsi"] < self.rsi_long)
        df["signal_short"] = (close > df["bb_upper"]) & (df["rsi"] > self.rsi_short)

        # Exit: Kreuzung der Mittellinie (SMA 20)
        cross_up   = (close > df["bb_middle"]) & (close.shift(1) <= df["bb_middle"].shift(1))
        cross_down = (close < df["bb_middle"]) & (close.shift(1) >= df["bb_middle"].shift(1))
        df["signal_exit_long"]  = cross_up    # Long-Exit wenn Preis über Mittellinie steigt
        df["signal_exit_short"] = cross_down  # Short-Exit wenn Preis unter Mittellinie fällt

        return df

    # ── Signal der letzten abgeschlossenen Kerze ─────────────

    def signal(self, df: pd.DataFrame) -> dict:
        """
        Gibt Signal der letzten ABGESCHLOSSENEN Kerze zurück.
        Index -2 (nicht -1) weil die aktuelle Kerze noch läuft.
        """
        row = df.iloc[-2]
        direction = (
            "LONG"  if row["signal_long"]  else
            "SHORT" if row["signal_short"] else
            "NONE"
        )
        exit_long  = bool(row["signal_exit_long"])
        exit_short = bool(row["signal_exit_short"])

        return {
            "timestamp":   str(row.name),
            "close":       float(row["close"]),
            "bb_upper":    float(row["bb_upper"]),
            "bb_middle":   float(row["bb_middle"]),
            "bb_lower":    float(row["bb_lower"]),
            "rsi":         float(row["rsi"]),
            "atr":         float(row["atr"]),
            "direction":   direction,
            "exit_long":   exit_long,
            "exit_short":  exit_short,
        }

    # ── Positionsgröße (identisch zu bot.py Risiko-Logik) ────

    def position_size(
        self,
        balance:   float,
        entry:     float,
        atr:       float,
        direction: str,
    ) -> dict:
        """
        Berechnet Qty, Stop-Loss und Take-Profit.
        Risiko: 1% des Kapitals (RISK_PER_TRADE_PCT aus bot.py).
        """
        if atr <= 0:
            raise ValueError(f"ATR muss > 0 sein, ist: {atr}")

        stop_dist = atr * ATR_MULTIPLIER
        risk_amt  = balance * RISK_PER_TRADE_PCT
        qty       = risk_amt / stop_dist

        if direction == "LONG":
            sl = entry - stop_dist
            tp = entry + stop_dist * REWARD_RISK_RATIO
        else:
            sl = entry + stop_dist
            tp = entry - stop_dist * REWARD_RISK_RATIO

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

    # ── Warmup-Bedarf ─────────────────────────────────────────

    @property
    def warmup_candles(self) -> int:
        """Minimale Kerzen für zuverlässige Indikatoren."""
        return self.bb_period + self.rsi_period + 10


# ============================================================
# SELBST-TEST
# ============================================================

if __name__ == "__main__":
    import sys
    from pathlib import Path
    # Projektroot in Pfad damit `from strategies.mean_reversion import ...` klappt
    sys.path.insert(0, str(Path(__file__).parent.parent))

    strategy = MeanReversionStrategy()

    # Synthetisches OHLCV-DataFrame zum Testen
    n = 100
    rng   = np.random.default_rng(42)
    close = 30_000 + np.cumsum(rng.normal(0, 200, n))
    noise = rng.uniform(-50, 50, n)

    df = pd.DataFrame({
        "open":   close - noise,
        "high":   close + abs(noise) * 2,
        "low":    close - abs(noise) * 2,
        "close":  close,
        "volume": rng.uniform(100, 1000, n),
    })

    df_out = strategy.compute(df)

    sig = strategy.signal(df_out)
    print(f"\n  Letztes Signal:  {sig['direction']}")
    print(f"  Close:           {sig['close']:.2f}")
    print(f"  BB upper/mid/low:{sig['bb_upper']:.2f} / {sig['bb_middle']:.2f} / {sig['bb_lower']:.2f}")
    print(f"  RSI:             {sig['rsi']:.1f}")

    pos = strategy.position_size(10_000, sig["close"], sig["atr"], "LONG")
    print(f"\n  Positions-Beispiel (LONG, Balance 10.000 USDT):")
    print(f"  Qty:       {pos['quantity']:.6f}")
    print(f"  Stop-Loss: {pos['stop_loss']:.2f}")
    print(f"  Take-Profit:{pos['take_profit']:.2f}")
    print(f"  Risiko:    {pos['risk_amount']:.2f} USDT (1%)\n")

    # Prüfe dass alle Pflicht-Spalten vorhanden sind
    required = {"bb_upper", "bb_middle", "bb_lower", "rsi", "atr",
                "signal_long", "signal_short", "signal_exit_long", "signal_exit_short"}
    missing = required - set(df_out.columns)
    assert not missing, f"Fehlende Spalten: {missing}"
    print("  Alle Spalten vorhanden ✅")
    print("  Import-Test: ", end="")

    from strategies.mean_reversion import MeanReversionStrategy as _Test
    _Test()
    print("OK ✅")
