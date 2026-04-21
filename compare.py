"""
================================================================================
  QuantBot Pro — Container Vergleichs-Analyse (TICKET-17)

  Liest Logs aus /opt/quantbot-central-logs/<ctid>-<name>/YYYY-MM-DD/
  und zeigt Side-by-Side Kennzahlen mehrerer Container.

  Aufruf:
    python3 compare.py --containers 772 773
    python3 compare.py --containers 772 773 --days 7
    python3 compare.py --all
================================================================================
"""

import argparse
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

CENTRAL_LOG_DIR = Path("/opt/quantbot-central-logs")


# ============================================================
# 1. DATEN LADEN
# ============================================================

def _find_container_dirs(base: Path, ctids: list[str] | None) -> dict[str, Path]:
    """Gibt {ctid: path} zurück. ctids=None → alle quantbot-Verzeichnisse."""
    result = {}
    if not base.exists():
        print(f"  FEHLER: {base} nicht gefunden. Erst setup_collector.sh ausführen.")
        sys.exit(1)
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        parts = d.name.split("-", 1)
        if len(parts) < 2:
            continue
        ctid = parts[0]
        if ctids is None or ctid in ctids:
            result[ctid] = d
    return result


def _load_csv(container_dir: Path, filename: str, days: int | None) -> pd.DataFrame:
    """Lädt alle Tages-CSVs eines Typs, optional auf `days` begrenzt."""
    frames = []
    cutoff = None
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    for day_dir in sorted(container_dir.iterdir()):
        if not day_dir.is_dir():
            continue
        try:
            day_dt = datetime.strptime(day_dir.name, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if cutoff and day_dt < cutoff - timedelta(days=1):
            continue
        csv_path = day_dir / filename
        if csv_path.exists():
            try:
                df = pd.read_csv(csv_path)
                frames.append(df)
            except Exception:
                pass

    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)

    # Timestamp-Spalte parsen
    ts_col = next((c for c in df.columns if "timestamp" in c.lower()), None)
    if ts_col:
        df[ts_col] = pd.to_datetime(df[ts_col], errors="coerce", utc=True)
        if cutoff:
            df = df[df[ts_col] >= cutoff]
        df = df.sort_values(ts_col).reset_index(drop=True)

    return df


def load_container_data(container_dir: Path, days: int | None) -> dict[str, pd.DataFrame]:
    files = [
        "portfolio_snapshots.csv",
        "trade_quality.csv",
        "signals.csv",
        "system_health.csv",
        "market_snapshot.csv",
    ]
    return {f: _load_csv(container_dir, f, days) for f in files}


# ============================================================
# 2. KENNZAHLEN BERECHNEN
# ============================================================

def _pf(wins: float, losses: float) -> float:
    return round(wins / losses, 2) if losses > 0 else float("inf")


def compute_metrics(data: dict[str, pd.DataFrame], ctid: str, name: str) -> dict:
    snap  = data["portfolio_snapshots.csv"]
    tq    = data["trade_quality.csv"]
    sig   = data["signals.csv"]
    health= data["system_health.csv"]
    mkt   = data["market_snapshot.csv"]

    m = {"ctid": ctid, "name": name}

    # ── Laufzeit ──────────────────────────────────────────────
    if not snap.empty and "timestamp" in snap.columns:
        t0 = snap["timestamp"].min()
        t1 = snap["timestamp"].max()
        m["runtime_h"] = round((t1 - t0).total_seconds() / 3600, 1)
        m["runtime_days"] = round(m["runtime_h"] / 24, 1)
    else:
        m["runtime_h"] = 0
        m["runtime_days"] = 0

    # ── PnL ───────────────────────────────────────────────────
    if not snap.empty and "total_pnl" in snap.columns:
        m["total_pnl"]     = round(float(snap["total_pnl"].iloc[-1]), 2)
        m["total_pnl_pct"] = round(float(snap["total_pnl_pct"].iloc[-1]), 2)
        m["tf_pnl"]        = round(float(snap["tf_pnl"].iloc[-1]), 2) if "tf_pnl" in snap.columns else 0
        m["mr_pnl"]        = round(float(snap["mr_pnl"].iloc[-1]), 2) if "mr_pnl" in snap.columns else 0
    else:
        m["total_pnl"] = m["total_pnl_pct"] = m["tf_pnl"] = m["mr_pnl"] = 0

    # ── Trades ────────────────────────────────────────────────
    if not tq.empty and "pnl" in tq.columns:
        m["trades"] = len(tq)
        wins_   = tq[tq["pnl"] > 0]["pnl"].sum()
        losses_ = tq[tq["pnl"] < 0]["pnl"].abs().sum()
        m["win_rate"]      = round(len(tq[tq["pnl"] > 0]) / len(tq) * 100, 1) if len(tq) else 0
        m["profit_factor"] = _pf(wins_, losses_)
        m["avg_pnl"]       = round(float(tq["pnl"].mean()), 2)
        m["trades_per_day"] = round(m["trades"] / max(m["runtime_days"], 1), 1)

        # TF / MR split
        if "strategy" in tq.columns:
            tf_t = tq[tq["strategy"] == "TF"]
            mr_t = tq[tq["strategy"] == "MR"]
            m["tf_trades"] = len(tf_t)
            m["mr_trades"] = len(mr_t)
        else:
            m["tf_trades"] = m["mr_trades"] = 0
    else:
        m["trades"] = m["win_rate"] = m["profit_factor"] = 0
        m["avg_pnl"] = m["trades_per_day"] = 0
        m["tf_trades"] = m["mr_trades"] = 0

    # ── Max Drawdown ──────────────────────────────────────────
    if not snap.empty and "total_balance" in snap.columns:
        bal = snap["total_balance"].dropna()
        if len(bal):
            peak    = bal.cummax()
            dd      = ((bal - peak) / peak * 100)
            m["max_dd"] = round(float(dd.min()), 2)
        else:
            m["max_dd"] = 0
    else:
        m["max_dd"] = 0

    # ── Signal-Frequenz ───────────────────────────────────────
    if not sig.empty and "signal_type" in sig.columns:
        m["signals_total"]  = len(sig)
        m["signals_fired"]  = len(sig[sig["signal_type"].isin(["LONG", "SHORT"])])
        m["signals_blocked"] = len(sig[sig.get("blocked_by", pd.Series()).ne("NONE")]) \
                                if "blocked_by" in sig.columns else 0
    else:
        m["signals_total"] = m["signals_fired"] = m["signals_blocked"] = 0

    # ── System-Health ─────────────────────────────────────────
    if not health.empty:
        if "api_latency_ms" in health.columns:
            lat = health["api_latency_ms"].dropna()
            m["avg_latency_ms"] = round(float(lat.mean()), 1) if len(lat) else 0
            m["max_latency_ms"] = round(float(lat.max()), 1)  if len(lat) else 0
        else:
            m["avg_latency_ms"] = m["max_latency_ms"] = 0
        if "ram_usage_mb" in health.columns:
            ram = health["ram_usage_mb"].dropna()
            m["avg_ram_mb"] = round(float(ram.mean()), 1) if len(ram) else 0
            m["max_ram_mb"] = round(float(ram.max()), 1)  if len(ram) else 0
        else:
            m["avg_ram_mb"] = m["max_ram_mb"] = 0
        if "bot_uptime_seconds" in health.columns and m["runtime_h"] > 0:
            uptime_s   = health["bot_uptime_seconds"].dropna()
            expected_s = m["runtime_h"] * 3600
            m["uptime_pct"] = round(
                min(float(uptime_s.max()) / expected_s * 100, 100), 1
            ) if len(uptime_s) else 0
        else:
            m["uptime_pct"] = 0
    else:
        m["avg_latency_ms"] = m["max_latency_ms"] = 0
        m["avg_ram_mb"] = m["max_ram_mb"] = 0
        m["uptime_pct"] = 0

    # ── Regime-Performance ────────────────────────────────────
    if not snap.empty and not tq.empty and not mkt.empty and "market_regime" in mkt.columns:
        regime_stats = {}
        for regime, grp in mkt.groupby("market_regime"):
            # Trades die in dieses Regime fallen (vereinfacht über Zeitstempel)
            regime_stats[regime] = len(grp)
        m["regime_dist"] = regime_stats
    else:
        m["regime_dist"] = {}

    return m


# ============================================================
# 3. AUSGABE
# ============================================================

W = 64  # Gesamtbreite der Box


def _box_top(title: str):
    inner = W - 2
    print(f"╔{'═' * inner}╗")
    print(f"║  {title:<{inner - 2}}║")
    print(f"╚{'═' * inner}╝")


def _row(label: str, *vals, w_label=22, w_val=18):
    cells = f"│ {label:<{w_label}} │"
    for v in vals:
        cells += f" {str(v):<{w_val}} │"
    print(cells)


def _sep(w_label=22, w_val=18, n_vals=2):
    line = f"├{'─' * (w_label + 2)}┼"
    line += f"{'┼'.join(['─' * (w_val + 2)] * n_vals)}┤"
    print(line)


def _head(w_label=22, w_val=18, n_vals=2):
    line = f"┌{'─' * (w_label + 2)}┬"
    line += f"{'┬'.join(['─' * (w_val + 2)] * n_vals)}┐"
    print(line)


def _foot(w_label=22, w_val=18, n_vals=2):
    line = f"└{'─' * (w_label + 2)}┴"
    line += f"{'┴'.join(['─' * (w_val + 2)] * n_vals)}┘"
    print(line)


def _sign(v: float) -> str:
    return f"+{v}" if v > 0 else str(v)


def _winner(metrics: list[dict]) -> str:
    best = max(metrics, key=lambda m: m["total_pnl"])
    if all(m["total_pnl"] == best["total_pnl"] for m in metrics):
        return "Unentschieden"
    others = [m for m in metrics if m != best]
    pnl_diff = best["total_pnl"] - min(m["total_pnl"] for m in others)
    return f"{best['ctid']} {best['name']} (+{pnl_diff:.2f} USDT mehr PnL)"


def print_comparison(all_metrics: list[dict], days: int | None):
    names  = [f"{m['ctid']} {m['name']}" for m in all_metrics]
    n      = len(all_metrics)
    W_LABEL = 22
    W_VAL   = max(18, max(len(n) + 2 for n in names))

    period = f"{days} Tage" if days else "gesamt"

    # ── Header ────────────────────────────────────────────────
    print()
    _box_top(f"QUANTBOT PRO — CONTAINER VERGLEICH ({period})")
    print()

    # ── Sektion 1: Side-by-Side Übersicht ─────────────────────
    print("  SEKTION 1 — PERFORMANCE ÜBERSICHT")
    print()
    _head(W_LABEL, W_VAL, n)
    _row("Kennzahl", *[f"{m['ctid']} {m['name']}" for m in all_metrics],
         w_label=W_LABEL, w_val=W_VAL)
    _sep(W_LABEL, W_VAL, n)
    _row("Laufzeit",       *[f"{m['runtime_days']}d ({m['runtime_h']}h)" for m in all_metrics],
         w_label=W_LABEL, w_val=W_VAL)
    _row("Gesamt PnL",     *[f"{_sign(m['total_pnl'])} USDT" for m in all_metrics],
         w_label=W_LABEL, w_val=W_VAL)
    _row("Gesamt PnL %",   *[f"{_sign(m['total_pnl_pct'])}%" for m in all_metrics],
         w_label=W_LABEL, w_val=W_VAL)
    _row("Trades gesamt",  *[str(m["trades"]) for m in all_metrics],
         w_label=W_LABEL, w_val=W_VAL)
    _row("Trades/Tag",     *[str(m["trades_per_day"]) for m in all_metrics],
         w_label=W_LABEL, w_val=W_VAL)
    _row("Win-Rate",       *[f"{m['win_rate']}%" for m in all_metrics],
         w_label=W_LABEL, w_val=W_VAL)
    _row("Profit Factor",  *[str(m["profit_factor"]) for m in all_metrics],
         w_label=W_LABEL, w_val=W_VAL)
    _row("Max Drawdown",   *[f"{m['max_dd']}%" for m in all_metrics],
         w_label=W_LABEL, w_val=W_VAL)
    _row("[TF] PnL",       *[f"{_sign(m['tf_pnl'])} USDT" for m in all_metrics],
         w_label=W_LABEL, w_val=W_VAL)
    _row("[TF] Trades",    *[str(m["tf_trades"]) for m in all_metrics],
         w_label=W_LABEL, w_val=W_VAL)
    _row("[MR] PnL",       *[f"{_sign(m['mr_pnl'])} USDT" for m in all_metrics],
         w_label=W_LABEL, w_val=W_VAL)
    _row("[MR] Trades",    *[str(m["mr_trades"]) for m in all_metrics],
         w_label=W_LABEL, w_val=W_VAL)
    _row("Uptime",         *[f"{m['uptime_pct']}%" for m in all_metrics],
         w_label=W_LABEL, w_val=W_VAL)
    _row("Avg API Latenz", *[f"{m['avg_latency_ms']}ms" for m in all_metrics],
         w_label=W_LABEL, w_val=W_VAL)
    _foot(W_LABEL, W_VAL, n)

    winner = _winner(all_metrics)
    print(f"\n  🏆 GEWINNER: {winner}")

    # ── Sektion 2: Markt-Regime ────────────────────────────────
    print("\n" + "─" * W)
    print("  SEKTION 2 — MARKT-REGIME (gleicher Zeitraum, gleiches Symbol)")
    print()
    all_regimes = set()
    for m in all_metrics:
        all_regimes |= set(m["regime_dist"].keys())

    if all_regimes:
        _head(W_LABEL, W_VAL, n)
        _row("Regime (Kerzen)", *[m["ctid"] for m in all_metrics],
             w_label=W_LABEL, w_val=W_VAL)
        _sep(W_LABEL, W_VAL, n)
        for regime in sorted(all_regimes):
            _row(regime,
                 *[str(m["regime_dist"].get(regime, 0)) for m in all_metrics],
                 w_label=W_LABEL, w_val=W_VAL)
        _foot(W_LABEL, W_VAL, n)
    else:
        print("  (keine Markt-Regime Daten)")

    # ── Sektion 3: Trade-Frequenz ─────────────────────────────
    print("\n" + "─" * W)
    print("  SEKTION 3 — TRADE-FREQUENZ & SIGNAL-ANALYSE")
    print()
    _head(W_LABEL, W_VAL, n)
    _row("Kennzahl", *[m["ctid"] for m in all_metrics],
         w_label=W_LABEL, w_val=W_VAL)
    _sep(W_LABEL, W_VAL, n)
    _row("Trades/Tag",      *[str(m["trades_per_day"]) for m in all_metrics],
         w_label=W_LABEL, w_val=W_VAL)
    _row("Signale gesamt",  *[str(m["signals_fired"]) for m in all_metrics],
         w_label=W_LABEL, w_val=W_VAL)
    _row("Signale blockiert",*[str(m["signals_blocked"]) for m in all_metrics],
         w_label=W_LABEL, w_val=W_VAL)
    _row("Trade/Signal %",
         *[
             f"{round(m['trades'] / m['signals_fired'] * 100, 1)}%"
             if m["signals_fired"] else "n/a"
             for m in all_metrics
         ],
         w_label=W_LABEL, w_val=W_VAL)
    _row("Avg PnL/Trade",   *[f"{_sign(m['avg_pnl'])} USDT" for m in all_metrics],
         w_label=W_LABEL, w_val=W_VAL)
    _foot(W_LABEL, W_VAL, n)

    # ── Sektion 4: Stabilität ─────────────────────────────────
    print("\n" + "─" * W)
    print("  SEKTION 4 — STABILITÄT & SYSTEM-HEALTH")
    print()
    _head(W_LABEL, W_VAL, n)
    _row("Kennzahl", *[m["ctid"] for m in all_metrics],
         w_label=W_LABEL, w_val=W_VAL)
    _sep(W_LABEL, W_VAL, n)
    _row("Uptime",          *[f"{m['uptime_pct']}%" for m in all_metrics],
         w_label=W_LABEL, w_val=W_VAL)
    _row("Avg API Latenz",  *[f"{m['avg_latency_ms']}ms" for m in all_metrics],
         w_label=W_LABEL, w_val=W_VAL)
    _row("Max API Latenz",  *[f"{m['max_latency_ms']}ms" for m in all_metrics],
         w_label=W_LABEL, w_val=W_VAL)
    _row("Avg RAM",         *[f"{m['avg_ram_mb']}MB" for m in all_metrics],
         w_label=W_LABEL, w_val=W_VAL)
    _row("Max RAM",         *[f"{m['max_ram_mb']}MB" for m in all_metrics],
         w_label=W_LABEL, w_val=W_VAL)
    _foot(W_LABEL, W_VAL, n)

    # ── Sektion 5: Empfehlung ─────────────────────────────────
    print("\n" + "─" * W)
    print("  SEKTION 5 — EMPFEHLUNG")
    print()
    _generate_recommendation(all_metrics)
    print()


def _generate_recommendation(metrics: list[dict]):
    if len(metrics) < 2:
        print("  (Mindestens 2 Container für Empfehlung nötig)")
        return

    a, b = metrics[0], metrics[1]

    lines = []

    # PnL-Vergleich
    if a["total_pnl"] != b["total_pnl"]:
        better_pnl = a if a["total_pnl"] > b["total_pnl"] else b
        worse_pnl  = b if better_pnl == a else a
        diff_pct   = abs(better_pnl["total_pnl"] - worse_pnl["total_pnl"])
        lines.append(
            f"  → {better_pnl['ctid']} hat {diff_pct:.2f} USDT mehr PnL "
            f"({_sign(better_pnl['total_pnl_pct'])}% vs {_sign(worse_pnl['total_pnl_pct'])}%)"
        )

    # Trade-Frequenz vs Drawdown
    trade_ratio = b["trades_per_day"] / a["trades_per_day"] if a["trades_per_day"] else 0
    dd_diff     = abs(b["max_dd"]) - abs(a["max_dd"])

    if trade_ratio > 1.5 and abs(dd_diff) < 2.0:
        lines.append(
            f"  → {b['ctid']} hat {trade_ratio:.1f}x mehr Trades/Tag bei "
            f"nur {abs(dd_diff):.1f}% mehr Drawdown — Moderate Parameter empfohlen"
        )
    elif trade_ratio > 1.5 and dd_diff > 3.0:
        lines.append(
            f"  → {b['ctid']} hat {trade_ratio:.1f}x mehr Trades, aber "
            f"{dd_diff:.1f}% höheren Drawdown — Risiko zu hoch"
        )
    elif trade_ratio < 0.7:
        lines.append(
            f"  → {a['ctid']} hat deutlich mehr Trades trotz konservativer Parameter"
        )

    # Profit Factor
    better_pf = a if a["profit_factor"] >= b["profit_factor"] else b
    worse_pf  = b if better_pf == a else a
    if isinstance(better_pf["profit_factor"], float) and better_pf["profit_factor"] > 1.0:
        lines.append(
            f"  → {better_pf['ctid']} hat besseren Profit Factor "
            f"({better_pf['profit_factor']} vs {worse_pf['profit_factor']}) — "
            f"robustere Strategie"
        )

    # Fazit
    if not lines:
        lines.append("  → Zu wenig Daten für eindeutige Empfehlung — nach 7 Tagen erneut prüfen")

    # Mindestens 3 Tage Daten?
    min_days = min(m["runtime_days"] for m in metrics)
    if min_days < 3:
        lines.append(
            f"  ⚠️  Nur {min_days:.1f} Tage Daten — Empfehlung nach 7 Tagen zuverlässiger"
        )

    for line in lines:
        print(line)


# ============================================================
# 4. MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="QuantBot Pro — Container Vergleichs-Analyse"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--containers", nargs="+", metavar="CTID",
                       help="Container-IDs (z.B. --containers 772 773)")
    group.add_argument("--all", action="store_true",
                       help="Alle quantbot Container vergleichen")
    parser.add_argument("--days", type=int, default=None,
                        help="Nur letzte N Tage analysieren (z.B. --days 7)")
    parser.add_argument("--dir", default=str(CENTRAL_LOG_DIR),
                        help=f"Zentrales Log-Verzeichnis (default: {CENTRAL_LOG_DIR})")
    args = parser.parse_args()

    base    = Path(args.dir)
    ctids   = args.containers if not args.all else None
    days    = args.days

    container_dirs = _find_container_dirs(base, ctids)

    if not container_dirs:
        print("  Keine Container-Daten gefunden.")
        print(f"  Erwartet in: {base}/<ctid>-<name>/YYYY-MM-DD/")
        sys.exit(1)

    print(f"\n  Lade Daten für {len(container_dirs)} Container"
          + (f" (letzte {days} Tage)" if days else "") + "...")

    all_metrics = []
    for ctid, cdir in container_dirs.items():
        name = cdir.name.split("-", 1)[1] if "-" in cdir.name else cdir.name
        data = load_container_data(cdir, days)
        nonempty = sum(1 for df in data.values() if not df.empty)
        if nonempty == 0:
            print(f"  ⚠️  {ctid} {name}: keine CSV-Daten gefunden — übersprungen")
            continue
        print(f"  ✓  {ctid} {name}: {nonempty}/5 CSV-Typen geladen")
        all_metrics.append(compute_metrics(data, ctid, name))

    if len(all_metrics) < 1:
        print("\n  Keine Daten vorhanden. log_collector.sh schon ausgeführt?")
        sys.exit(1)

    print_comparison(all_metrics, days)


if __name__ == "__main__":
    main()
