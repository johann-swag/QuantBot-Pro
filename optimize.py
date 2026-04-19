"""
================================================================================
  QuantBot Pro — Parameter Optimizer
  Grid Search über EMA/RSI/ADX-Kombinationen via Backtest-Simulation

  WICHTIG: Dieses Script übernimmt KEINE Parameter automatisch.
  Der Output wird dem PO präsentiert — er entscheidet welche
  Kombination live geht.

  Nutzung:
    python3 optimize.py --symbol BTC/USDT --days 365
    python3 optimize.py --symbol ETH/USDT --days 180 --min-trades 5
================================================================================
"""

import os
import sys
import json
import argparse
import itertools
from datetime import datetime, timezone
from pathlib import Path

import ccxt
import pandas as pd
import numpy as np
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# PARAMETER-GRIDS
# ============================================================

# Trend-Following Grid (EMA/RSI/ADX)
PARAM_GRID = {
    "EMA_FAST":      [8, 10, 12, 20],
    "EMA_SLOW":      [21, 26, 50, 100],
    "RSI_PERIOD":    [10, 14, 21],
    "RSI_LONG_MIN":  [45, 48, 50, 52],
    "ADX_THRESHOLD": [20, 25, 30],
}

# Mean Reversion Grid (Bollinger Bands / RSI)
MR_PARAM_GRID = {
    "BB_PERIOD":  [15, 20, 25],
    "BB_STD":     [2.0, 2.5, 3.0],
    "RSI_PERIOD": [10, 14, 21],
    "RSI_LONG":   [25, 30, 35],
    "RSI_SHORT":  [65, 70, 75],
}

# Feste Parameter die nicht gesweept werden
FIXED = {
    "ATR_PERIOD":        14,
    "ATR_MULTIPLIER":    2.0,
    "REWARD_RISK_RATIO": 1.5,
    "RISK_PER_TRADE_PCT":0.01,
    "LOSS_COOLDOWN_BARS":3,
    "VOLUME_FACTOR":     1.2,
    "TIMEFRAME":         "4h",
}


# ============================================================
# INDIKATOREN
# ============================================================

def _ema(s: pd.Series, period: int) -> pd.Series:
    return s.ewm(span=period, adjust=False).mean()

def _rsi(s: pd.Series, period: int) -> pd.Series:
    d    = s.diff()
    gain = d.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs   = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    h, l, cp = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([(h - l), (h - cp).abs(), (l - cp).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def _adx(df: pd.DataFrame, period: int) -> pd.Series:
    h, l   = df["high"], df["low"]
    up, dn = h.diff(), -l.diff()
    pdm    = np.where((up > dn) & (up > 0), up, 0.0)
    mdm    = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr    = _atr(df, period)
    pdi    = 100 * pd.Series(pdm, index=df.index).ewm(alpha=1/period, adjust=False).mean() / atr
    mdi    = 100 * pd.Series(mdm, index=df.index).ewm(alpha=1/period, adjust=False).mean() / atr
    dx     = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1/period, adjust=False).mean()


# ============================================================
# SIGNALE BERECHNEN
# ============================================================

def compute_signals(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = _ema(df["close"], p["EMA_FAST"])
    df["ema_slow"] = _ema(df["close"], p["EMA_SLOW"])
    df["rsi"]      = _rsi(df["close"], p["RSI_PERIOD"])
    df["atr"]      = _atr(df, FIXED["ATR_PERIOD"])
    df["adx"]      = _adx(df, p["ADX_THRESHOLD"])   # ADX_PERIOD == ADX_THRESHOLD hier
    df["vol_ma20"] = df["volume"].rolling(20).mean()

    cross_up   = (df["ema_fast"] > df["ema_slow"]) & (df["ema_fast"].shift(1) <= df["ema_slow"].shift(1))
    cross_down = (df["ema_fast"] < df["ema_slow"]) & (df["ema_fast"].shift(1) >= df["ema_slow"].shift(1))
    regime     = df["adx"] > p["ADX_THRESHOLD"]
    vol_ok     = df["volume"] > df["vol_ma20"] * FIXED["VOLUME_FACTOR"]

    rsi_long_ok  = df["rsi"] > p["RSI_LONG_MIN"]
    rsi_short_ok = df["rsi"] < (100 - p["RSI_LONG_MIN"])  # symmetrisch gespiegelt

    df["signal_long"]  = cross_up   & rsi_long_ok  & regime & vol_ok
    df["signal_short"] = cross_down & rsi_short_ok & regime & vol_ok
    return df


# ============================================================
# BACKTEST-SIMULATION (ein Parameterset, auf vorberechnetem DF)
# ============================================================

def simulate(df: pd.DataFrame, p: dict, start_balance: float = 10_000.0) -> dict:
    """
    Simuliert die vollständige Strategie auf df mit Parameterset p.
    Gibt Performance-Kennzahlen zurück.
    Läuft rein in-memory — kein API-Aufruf.
    """
    warmup   = max(p["EMA_SLOW"], p["RSI_PERIOD"]) + 20
    balance  = start_balance
    equity   = [balance]
    trades   = []
    position = None
    cooldown = 0

    atr_mult  = FIXED["ATR_MULTIPLIER"]
    rr_ratio  = FIXED["REWARD_RISK_RATIO"]
    risk_pct  = FIXED["RISK_PER_TRADE_PCT"]
    cooldown_bars = FIXED["LOSS_COOLDOWN_BARS"]

    for i in range(warmup, len(df) - 1):
        row   = df.iloc[i]
        price = float(row["close"])
        lo    = float(row["low"])
        hi    = float(row["high"])

        if position:
            sl, tp = position["sl"], position["tp"]
            hit_sl = (lo <= sl) if position["long"] else (hi >= sl)
            hit_tp = (hi >= tp) if position["long"] else (lo <= tp)

            if hit_sl or hit_tp:
                exit_px = sl if hit_sl else tp
                pnl = ((exit_px - position["entry"]) * position["qty"]
                       if position["long"]
                       else (position["entry"] - exit_px) * position["qty"])
                balance += pnl
                trades.append({
                    "pnl":         round(pnl, 4),
                    "risk_amount": position["risk_amount"],
                    "win":         pnl > 0,
                })
                if hit_sl:
                    cooldown = cooldown_bars
                position = None
            else:
                # Trailing Stop
                atr    = float(row["atr"])
                new_sl = (price - atr * atr_mult if position["long"]
                          else price + atr * atr_mult)
                if position["long"] and new_sl > position["sl"]:
                    position["sl"] = new_sl
                elif not position["long"] and new_sl < position["sl"]:
                    position["sl"] = new_sl

        elif cooldown > 0:
            cooldown -= 1

        elif row["signal_long"] or row["signal_short"]:
            is_long   = bool(row["signal_long"])
            atr       = float(row["atr"])
            if atr <= 0:
                continue
            stop_dist = atr * atr_mult
            risk_amt  = balance * risk_pct
            qty       = risk_amt / stop_dist
            sl = price - stop_dist if is_long else price + stop_dist
            tp = price + stop_dist * rr_ratio if is_long else price - stop_dist * rr_ratio
            if sl <= 0:
                continue
            position = {
                "long": is_long, "entry": price, "qty": qty,
                "sl": sl, "tp": tp, "risk_amount": risk_amt,
            }

        equity.append(balance)

    if not trades:
        return None

    pnls   = [t["pnl"] for t in trades]
    wins   = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]

    gp = sum(t["pnl"] for t in wins)   if wins   else 0.0
    gl = abs(sum(t["pnl"] for t in losses)) if losses else 1e-9
    pf = gp / gl

    # RR: durchschnittlicher Gewinner in Einheiten des initialen Risikos
    avg_rr = (sum(t["pnl"] / t["risk_amount"] for t in wins) / len(wins)
              if wins else 0.0)

    eq  = pd.Series(equity)
    dd  = ((eq - eq.cummax()) / eq.cummax() * 100).min()

    total_ret   = (balance - start_balance) / start_balance * 100

    return {
        "n_trades":   len(trades),
        "win_rate":   len(wins) / len(trades) * 100,
        "total_pnl":  round(balance - start_balance, 2),
        "total_ret":  round(total_ret, 2),
        "profit_factor": round(pf, 3),
        "avg_rr":     round(avg_rr, 3),
        "max_dd":     round(float(dd), 2),
        "end_balance":round(balance, 2),
    }


# ============================================================
# MEAN REVERSION SIMULATION
# ============================================================

def simulate_mr(df: pd.DataFrame, params: dict, start_balance: float = 10_000.0):
    """
    Simuliert MeanReversionStrategy auf einem vorbereiteten DataFrame.
    Primärer Exit: Mittellinie (SMA) — SL/TP als Safety-Net.
    Kein Trailing Stop.
    """
    sys.path.insert(0, ".")
    from strategies.mean_reversion import MeanReversionStrategy

    strategy = MeanReversionStrategy(
        bb_period  = params["BB_PERIOD"],
        bb_std     = params["BB_STD"],
        rsi_period = params["RSI_PERIOD"],
        rsi_long   = params["RSI_LONG"],
        rsi_short  = params["RSI_SHORT"],
        verbose    = False,
    )
    df = strategy.compute(df)

    warmup    = strategy.warmup_candles
    atr_mult  = FIXED["ATR_MULTIPLIER"]
    rr_ratio  = FIXED["REWARD_RISK_RATIO"]
    risk_pct  = FIXED["RISK_PER_TRADE_PCT"]
    cooldown_bars = FIXED["LOSS_COOLDOWN_BARS"]

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
            sl, tp = position["sl"], position["tp"]
            is_long = position["long"]

            hit_sl = (lo <= sl) if is_long else (hi >= sl)
            hit_tp = (hi >= tp) if is_long else (lo <= tp)
            hit_mid = (
                (is_long  and bool(row["signal_exit_long"]))  or
                (not is_long and bool(row["signal_exit_short"]))
            )

            if hit_sl or hit_tp or hit_mid:
                if hit_mid and not hit_sl:
                    exit_px = price
                    reason  = "MEAN_EXIT"
                else:
                    exit_px = sl if hit_sl else tp
                    reason  = "STOP_LOSS" if hit_sl else "TAKE_PROFIT"

                pnl = ((exit_px - position["entry"]) * position["qty"] if is_long
                       else (position["entry"] - exit_px) * position["qty"])
                balance += pnl
                trades.append({
                    "pnl":         round(pnl, 4),
                    "risk_amount": position["risk_amount"],
                    "win":         pnl > 0,
                    "reason":      reason,
                })
                if hit_sl:
                    cooldown = cooldown_bars
                position = None

        elif cooldown > 0:
            cooldown -= 1

        elif row["signal_long"] or row["signal_short"]:
            is_long   = bool(row["signal_long"])
            atr       = float(row["atr"])
            if atr <= 0:
                continue
            stop_dist = atr * atr_mult
            risk_amt  = balance * risk_pct
            qty       = risk_amt / stop_dist
            sl = price - stop_dist if is_long else price + stop_dist
            tp = price + stop_dist * rr_ratio if is_long else price - stop_dist * rr_ratio
            if sl <= 0:
                continue
            position = {
                "long": is_long, "entry": price, "qty": qty,
                "sl": sl, "tp": tp, "risk_amount": risk_amt,
            }

        equity.append(balance)

    if not trades:
        return None

    pnls   = [t["pnl"] for t in trades]
    wins   = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]

    gp = sum(t["pnl"] for t in wins)          if wins   else 0.0
    gl = abs(sum(t["pnl"] for t in losses))   if losses else 1e-9
    pf = gp / gl

    avg_rr = (sum(t["pnl"] / t["risk_amount"] for t in wins) / len(wins)
              if wins else 0.0)

    eq  = pd.Series(equity)
    dd  = ((eq - eq.cummax()) / eq.cummax() * 100).min()

    return {
        "n_trades":      len(trades),
        "win_rate":      len(wins) / len(trades) * 100,
        "total_pnl":     round(balance - start_balance, 2),
        "total_ret":     round((balance - start_balance) / start_balance * 100, 2),
        "profit_factor": round(pf, 3),
        "avg_rr":        round(avg_rr, 3),
        "max_dd":        round(float(dd), 2),
    }


def run_mr_grid_search(
    df:            pd.DataFrame,
    min_trades:    int,
    max_dd:        float,
    min_pf:        float,
    start_balance: float = 10_000.0,
) -> list[dict]:

    keys   = list(MR_PARAM_GRID.keys())
    combos = list(itertools.product(*MR_PARAM_GRID.values()))

    print(f"  Grid: {len(combos)} Kombinationen (3×3×3×3×3)")
    print(f"  Filter: Trades >= {min_trades}  |  Max-DD >= {max_dd}%  |  PF > {min_pf}")
    print(f"  Simuliere...\n")

    results  = []
    filtered = 0
    for i, combo in enumerate(combos, 1):
        if i % 40 == 0 or i == len(combos):
            print(f"  [{i:>3}/{len(combos)}] {i/len(combos)*100:.0f}%",
                  end="\r", flush=True)

        params = dict(zip(keys, combo))
        try:
            metrics = simulate_mr(df, params, start_balance)
        except Exception:
            continue

        if metrics is None:
            continue
        if metrics["n_trades"] < min_trades:
            filtered += 1
            continue
        if metrics["max_dd"] < max_dd:
            filtered += 1
            continue
        if metrics["profit_factor"] <= min_pf:
            filtered += 1
            continue

        results.append({
            "BB_PERIOD":  params["BB_PERIOD"],
            "BB_STD":     params["BB_STD"],
            "RSI_PERIOD": params["RSI_PERIOD"],
            "RSI_LONG":   params["RSI_LONG"],
            "RSI_SHORT":  params["RSI_SHORT"],
            **metrics,
        })

    print(f"\n  Fertig. {len(results)} Kombinationen bestehen alle Filter "
          f"({filtered} gefiltert).\n")
    results.sort(key=lambda r: (-r["profit_factor"], -r["total_pnl"]))
    return results


# ── MR-spezifischer Output ───────────────────────────────────

MR_HEADER = (
    f"  {'#':<3} {'BB-P':>5} {'BB-S':>5} {'RSI-P':>6} "
    f"{'RSI-L':>6} {'RSI-S':>6} {'Trades':>7} {'Win%':>6} "
    f"{'PF':>5} {'PnL':>9} {'Ret%':>7} {'DD%':>7} {'RR':>5}"
)
MR_ROW = (
    "  {rank:<3} {BB_PERIOD:>5} {BB_STD:>5.1f} {RSI_PERIOD:>6} "
    "{RSI_LONG:>6} {RSI_SHORT:>6} {n_trades:>7} {win_rate:>5.1f}% "
    "{profit_factor:>5.2f} {total_pnl:>+9.2f} {total_ret:>+6.2f}% "
    "{max_dd:>+6.2f}% {avg_rr:>5.2f}"
)

def print_mr_results(results: list[dict], top_n: int = 5):
    sep = "  " + "─" * 96
    print(f"\n{'='*100}")
    print(f"  TOP-{min(top_n, len(results))} MEAN REVERSION PARAMETER-SETS  "
          f"(sortiert nach Profit Factor)")
    print(f"{'='*100}")
    print(f"  Legende: BB-P=BB Periode  BB-S=BB StdDev  RSI-P=RSI Periode  "
          f"RSI-L=Long Schwelle  RSI-S=Short Schwelle")
    print(sep)
    print(MR_HEADER)
    print(sep)
    for i, r in enumerate(results[:top_n], 1):
        print(MR_ROW.format(rank=f"#{i}", **r))
    print(sep)
    print()
    print("  ⚠️  HINWEIS: Diese Ergebnisse sind rein informativ.")
    print("  Die TOP-1 Kombination wird NICHT automatisch übernommen.")
    print("  Der PO entscheidet, welche Parameter live gehen.\n")


# ============================================================
# DATEN LADEN (mit Pagination)
# ============================================================

def load_data(exchange: ccxt.Exchange, symbol: str, days: int) -> pd.DataFrame:
    timeframe = FIXED["TIMEFRAME"]
    candles_per_day = {"1h": 24, "4h": 6, "1d": 1}.get(timeframe, 6)
    needed  = days * candles_per_day + 200   # +200 für Warmup-Buffer
    tf_ms   = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}.get(timeframe, 14_400_000)
    since   = exchange.milliseconds() - needed * tf_ms

    print(f"  Lade ~{needed} Kerzen für {symbol} [{timeframe}]...", end=" ", flush=True)
    all_rows = []
    while True:
        chunk = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        if not chunk:
            break
        all_rows.extend(chunk)
        if len(chunk) < 1000:
            break
        since = chunk[-1][0] + tf_ms

    df = pd.DataFrame(all_rows, columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    print(f"OK ({len(df)} Kerzen)\n")
    return df


# ============================================================
# GRID SEARCH
# ============================================================

def run_grid_search(
    df: pd.DataFrame,
    min_trades: int,
    start_balance: float = 10_000.0,
) -> list[dict]:

    keys   = list(PARAM_GRID.keys())
    combos = list(itertools.product(*PARAM_GRID.values()))
    total  = len(combos)

    # Ungültige Combos rausfiltern (EMA_FAST muss < EMA_SLOW sein)
    valid_combos = [
        c for c in combos
        if c[keys.index("EMA_FAST")] < c[keys.index("EMA_SLOW")]
    ]
    skipped = total - len(valid_combos)

    print(f"  Grid: {total} Kombinationen "
          f"({skipped} übersprungen wegen EMA_FAST >= EMA_SLOW)")
    print(f"  Simuliere {len(valid_combos)} Kombinationen...\n")

    results = []
    for i, combo in enumerate(valid_combos, 1):
        params = dict(zip(keys, combo))

        # Fortschrittsanzeige alle 50 Combos
        if i % 50 == 0 or i == len(valid_combos):
            pct = i / len(valid_combos) * 100
            print(f"  [{i:>3}/{len(valid_combos)}] {pct:.0f}%", end="\r", flush=True)

        try:
            df_sig = compute_signals(df, params)
            metrics = simulate(df_sig, params, start_balance)
        except Exception:
            continue

        if metrics is None:
            continue
        if metrics["n_trades"] < min_trades:
            continue

        results.append({
            "EMA_FAST":      params["EMA_FAST"],
            "EMA_SLOW":      params["EMA_SLOW"],
            "RSI_PERIOD":    params["RSI_PERIOD"],
            "RSI_LONG_MIN":  params["RSI_LONG_MIN"],
            "ADX_THRESHOLD": params["ADX_THRESHOLD"],
            **metrics,
        })

    print(f"\n  Fertig. {len(results)} Kombinationen erfüllen Mindest-Trades >= {min_trades}.\n")
    results.sort(key=lambda r: (-r["profit_factor"], -r["total_pnl"]))
    return results


# ============================================================
# AUSGABE
# ============================================================

HEADER = (
    f"  {'#':<3} {'EF':>3} {'ES':>4} {'RSI-P':>6} {'RSI-L':>6} "
    f"{'ADX':>4} {'Trades':>7} {'Win%':>6} "
    f"{'PF':>5} {'PnL':>9} {'Ret%':>7} {'DD%':>7} {'RR':>5}"
)
ROW = (
    "  {rank:<3} {EMA_FAST:>3} {EMA_SLOW:>4} {RSI_PERIOD:>6} {RSI_LONG_MIN:>6} "
    "{ADX_THRESHOLD:>4} {n_trades:>7} {win_rate:>5.1f}% "
    "{profit_factor:>5.2f} {total_pnl:>+9.2f} {total_ret:>+6.2f}% {max_dd:>+6.2f}% {avg_rr:>5.2f}"
)

def print_results(results: list[dict], top_n: int = 5):
    sep = "  " + "─" * 88
    print(f"\n{'='*92}")
    print(f"  TOP-{min(top_n, len(results))} PARAMETER-SETS  "
          f"(sortiert nach Profit Factor)")
    print(f"{'='*92}")
    print(f"  Legende: EF=EMA Fast  ES=EMA Slow  RSI-P=RSI Periode  "
          f"RSI-L=RSI Long Min  PF=Profit Factor")
    print(sep)
    print(HEADER)
    print(sep)
    for i, r in enumerate(results[:top_n], 1):
        print(ROW.format(rank=f"#{i}", **r))
    print(sep)
    print()

    # ⚠️  Expliziter Hinweis: KEINE automatische Übernahme
    print("  ⚠️  HINWEIS: Diese Ergebnisse sind rein informativ.")
    print("  Die TOP-1 Kombination wird NICHT automatisch in bot.py übernommen.")
    print("  Der PO entscheidet, welche Parameter live gehen.\n")


def save_results(results: list[dict], symbol: str):
    Path("logs").mkdir(exist_ok=True)
    ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe = symbol.replace("/", "_")
    path = f"logs/optimize_{safe}_{ts}.json"
    with open(path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"  Alle Ergebnisse gespeichert: {path}\n")


# ============================================================
# EINSTIEGSPUNKT
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="QuantBot Pro — Parameter Grid Search"
    )
    parser.add_argument("--symbol",     default="BTC/USDT",   help="Handelspaar")
    parser.add_argument("--days",       type=int, default=365, help="Backtest-Zeitraum in Tagen")
    parser.add_argument("--min-trades", type=int, default=8,   help="Mindest-Trades pro Jahr")
    parser.add_argument("--top",        type=int, default=5,   help="Anzahl Top-Ergebnisse")
    parser.add_argument("--balance",    type=float, default=10_000.0, help="Startkapital")
    parser.add_argument("--strategy",   default="trend",
                        choices=["trend", "mean_reversion"],
                        help="Strategie: trend (default) | mean_reversion")
    parser.add_argument("--max-dd",     type=float, default=-8.0,
                        help="Max. erlaubter Drawdown in %% (nur mean_reversion, default: -8.0)")
    parser.add_argument("--min-pf",     type=float, default=1.0,
                        help="Mindest Profit Factor (nur mean_reversion, default: 1.0)")
    args = parser.parse_args()

    is_mr = args.strategy == "mean_reversion"

    print("""
+============================================================+
|   QUANTBOT PRO — PARAMETER OPTIMIZER                       |
|   Grid Search | Backtest | Kein Auto-Apply                 |
+============================================================+
    """)
    print(f"  Strategie:     {args.strategy}")
    print(f"  Symbol:        {args.symbol}")
    print(f"  Zeitraum:      {args.days} Tage")
    print(f"  Min. Trades:   {args.min_trades}/Jahr")
    print(f"  Startkapital:  {args.balance:,.0f} USDT")
    if is_mr:
        mr_total = 1
        for v in MR_PARAM_GRID.values():
            mr_total *= len(v)
        print(f"  Grid-Größe:    3×3×3×3×3 = {mr_total} Kombinationen")
        print(f"  Max. Drawdown: {args.max_dd}%  |  Min. PF: {args.min_pf}\n")
    else:
        print(f"  Grid-Größe:    4×4×3×4×3 = 576 Kombinationen\n")

    exchange = ccxt.binance({"enableRateLimit": True})
    df = load_data(exchange, args.symbol, args.days)

    if is_mr:
        results = run_mr_grid_search(
            df,
            min_trades = args.min_trades,
            max_dd     = args.max_dd,
            min_pf     = args.min_pf,
            start_balance = args.balance,
        )
    else:
        results = run_grid_search(df, min_trades=args.min_trades, start_balance=args.balance)

    if not results:
        print(f"  ❌ Keine Kombinationen bestehen alle Filter.")
        print("  Tipps: --min-trades senken, --max-dd lockern (z.B. -12), --days erhöhen.\n")
        sys.exit(0)

    if is_mr:
        print_mr_results(results, top_n=args.top)
    else:
        print_results(results, top_n=args.top)

    safe_strat = args.strategy.replace("_", "-")
    save_results(results, f"{args.symbol.replace('/', '_')}_{safe_strat}")
