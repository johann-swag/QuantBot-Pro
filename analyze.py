"""
================================================================================
  QuantBot Pro — Paper Trading Analyse-Framework (TICKET-12)

  Liest alle CSV-Dateien aus logs/YYYY-MM-DD/ und erstellt strukturierte
  Auswertung der Paper-Trading-Phase.

  Aufruf:
    python3 analyze.py --days 7
    python3 analyze.py --from 2026-04-20 --to 2026-04-27
================================================================================
"""

import argparse
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

pd.options.mode.chained_assignment = None

LOG_DIR = Path("logs")

# ============================================================
# DATEN LADEN
# ============================================================

_CSV_FILES = {
    "market":    "market_snapshot.csv",
    "signals":   "signals.csv",
    "quality":   "trade_quality.csv",
    "snapshots": "portfolio_snapshots.csv",
    "external":  "external_market.csv",
    "health":    "system_health.csv",
}


def load_date_range(start: datetime, end: datetime) -> dict[str, pd.DataFrame]:
    """Lädt alle CSVs aus dem angegebenen Datumsbereich."""
    buckets: dict[str, list] = {k: [] for k in _CSV_FILES}

    d = start
    while d <= end:
        day_dir = LOG_DIR / d.strftime("%Y-%m-%d")
        if day_dir.exists():
            for key, fname in _CSV_FILES.items():
                p = day_dir / fname
                if p.exists() and p.stat().st_size > 0:
                    try:
                        buckets[key].append(pd.read_csv(p))
                    except Exception:
                        pass
        d += timedelta(days=1)

    result = {}
    for k, frames in buckets.items():
        result[k] = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    return result


# ============================================================
# HILFS-FORMATIERUNGEN
# ============================================================

W1 = 62   # Breite der äußeren Box
W2 = 17   # Spaltenbreite für Regime-Tabelle

def sep(char="─", width=W1):
    print(char * width)

def header(title: str):
    sep("═")
    print(f"  {title}")
    sep("═")

def subheader(title: str):
    sep()
    print(f"  {title}")
    sep()

def _pnl(v) -> str:
    try:
        f = float(v)
        return f"{f:>+10.2f} USDT"
    except Exception:
        return "         — USDT"

def _pct(v) -> str:
    try:
        return f"{float(v):>+.1f}%"
    except Exception:
        return "  —%"

def _num(v, d=2) -> str:
    try:
        return f"{float(v):.{d}f}"
    except Exception:
        return "—"

def no_data(section: str):
    print(f"  [Keine Daten für {section} in diesem Zeitraum]\n")


# ============================================================
# SEKTION 1 — ÜBERSICHT
# ============================================================

def section_1(quality: pd.DataFrame, snapshots: pd.DataFrame):
    header("SEKTION 1 — Übersicht")

    if quality.empty:
        no_data("Trade-Qualität")
        return

    # Gesamt
    total_pnl  = quality["pnl"].sum()
    total_n    = len(quality)
    wins       = quality[quality["pnl"] > 0]
    losses     = quality[quality["pnl"] <= 0]
    win_rate   = len(wins) / total_n * 100 if total_n else 0
    gp         = wins["pnl"].sum()   if not wins.empty   else 0
    gl         = abs(losses["pnl"].sum()) if not losses.empty else 1e-9
    pf         = gp / gl

    print(f"  Gesamt-PnL:     {_pnl(total_pnl)}")
    print(f"  Trades:         {total_n:>5}  (W:{len(wins)}  L:{len(losses)})")
    print(f"  Win-Rate:       {win_rate:>7.1f}%")
    print(f"  Profit Factor:  {pf:>8.2f}")
    print()

    # TF vs MR nebeneinander
    print(f"  {'Kennzahl':<22} {'[TF] Trend':<20} {'[MR] Mean Rev'}")
    print(f"  {'─'*22} {'─'*20} {'─'*18}")
    for strat, label in [("TF", "Trend Following"), ("MR", "Mean Reversion")]:
        pass  # Tabelle wird direkt unten aufgebaut

    for col, fmt_fn in [
        ("PnL",        lambda d: _pnl(d["pnl"].sum())),
        ("Trades",     lambda d: f"{len(d):>10}"),
        ("Win-Rate",   lambda d: f"{len(d[d['pnl']>0])/len(d)*100:>9.1f}%"),
        ("Avg PnL",    lambda d: _pnl(d["pnl"].mean())),
        ("Avg Dauer",  lambda d: f"{d['duration_hours'].mean():>8.1f}h"),
    ]:
        tf_d = quality[quality["strategy"] == "TF"] if "strategy" in quality.columns else pd.DataFrame()
        mr_d = quality[quality["strategy"] == "MR"] if "strategy" in quality.columns else pd.DataFrame()
        tf_s = fmt_fn(tf_d) if not tf_d.empty else "         —"
        mr_s = fmt_fn(mr_d) if not mr_d.empty else "         —"
        print(f"  {col:<22} {tf_s:<20} {mr_s}")

    # Bester / Schlechtester Tag
    if "timestamp_exit" in quality.columns:
        try:
            quality["_day"] = pd.to_datetime(quality["timestamp_exit"]).dt.date
            daily = quality.groupby("_day")["pnl"].sum()
            if not daily.empty:
                print()
                print(f"  Bester Tag:      {daily.idxmax()}  {_pnl(daily.max())}")
                print(f"  Schlechtester:   {daily.idxmin()}  {_pnl(daily.min())}")
        except Exception:
            pass
    print()


# ============================================================
# SEKTION 2 — MARKT-REGIME ANALYSE
# ============================================================

def section_2(quality: pd.DataFrame, market: pd.DataFrame):
    header("SEKTION 2 — Markt-Regime Analyse")

    if quality.empty or market.empty:
        no_data("Regime-Analyse")
        return

    try:
        q = quality.copy()
        m = market.copy()
        m["timestamp"] = pd.to_datetime(m["timestamp"], utc=True)
        q["_ts"]       = pd.to_datetime(q["timestamp_entry"], utc=True)

        # Nächsten Markt-Snapshot zum Entry-Zeitpunkt finden
        m_sorted = m.sort_values("timestamp")
        q_sorted = q.sort_values("_ts")
        merged   = pd.merge_asof(
            q_sorted, m_sorted[["timestamp", "market_regime"]],
            left_on="_ts", right_on="timestamp",
            direction="backward",
        )

        regimes = ["TRENDING_UP", "TRENDING_DOWN", "RANGING", "HIGH_VOLA", "LOW_VOLA"]
        col_w = W2

        print(f"  ┌{'─'*17}┬{'─'*col_w}┬{'─'*col_w}┐")
        print(f"  │ {'Regime':<15} │ {'[TF] PnL':>{col_w-2}} │ {'[MR] PnL':>{col_w-2}} │")
        print(f"  ├{'─'*17}┼{'─'*col_w}┼{'─'*col_w}┤")

        for regime in regimes:
            rd = merged[merged["market_regime"] == regime]
            if rd.empty:
                tf_s = mr_s = "         —"
            else:
                tf_d  = rd[rd["strategy"] == "TF"]["pnl"]
                mr_d  = rd[rd["strategy"] == "MR"]["pnl"]
                tf_s  = f"{tf_d.sum():>+10.2f}" if not tf_d.empty else "         —"
                mr_s  = f"{mr_d.sum():>+10.2f}" if not mr_d.empty else "         —"
            print(f"  │ {regime:<15} │ {tf_s:>{col_w-2}} │ {mr_s:>{col_w-2}} │")

        print(f"  └{'─'*17}┴{'─'*col_w}┴{'─'*col_w}┘")
        print()

        # Empfehlung
        if not merged.empty:
            tf_by_regime = merged[merged["strategy"] == "TF"].groupby("market_regime")["pnl"].sum()
            mr_by_regime = merged[merged["strategy"] == "MR"].groupby("market_regime")["pnl"].sum()
            tf_best = tf_by_regime.idxmax() if not tf_by_regime.empty else "—"
            mr_best = mr_by_regime.idxmax() if not mr_by_regime.empty else "—"
            print(f"  → [TF] performt am besten in: {tf_best}")
            print(f"  → [MR] performt am besten in: {mr_best}")
            print()
    except Exception as e:
        print(f"  [Fehler in Regime-Analyse: {e}]\n")


# ============================================================
# SEKTION 3 — TRADE QUALITÄT (MAE/MFE)
# ============================================================

def section_3(quality: pd.DataFrame):
    header("SEKTION 3 — Trade Qualität (MAE / MFE)")

    if quality.empty:
        no_data("Trade-Qualität")
        return

    for strat in ["TF", "MR"]:
        d = quality[quality["strategy"] == strat] if "strategy" in quality.columns else pd.DataFrame()
        if d.empty:
            print(f"  [{strat}] Keine Trades\n")
            continue

        avg_mae = d["mae"].mean()
        avg_mfe = d["mfe"].mean()
        avg_eff = d["efficiency"].mean()
        avg_pnl = d["pnl"].mean()
        avg_dur = d["duration_hours"].mean()

        # SL-Empfehlung basierend auf MAE
        entry_prices = d["entry_price"].mean()
        mae_pct = (avg_mae / entry_prices * 100) if entry_prices > 0 else 0
        sl_pct  = abs(d["entry_price"].mean() - d["stop_loss"].mean()) / d["entry_price"].mean() * 100 if "stop_loss" in d.columns and d["entry_price"].mean() > 0 else 0

        print(f"  [{strat}]")
        print(f"    Avg MAE:          {_num(avg_mae)} USDT/Unit   (Stop zu früh getriggert?)")
        print(f"    Avg MFE:          {_num(avg_mfe)} USDT/Unit   (Gewinnpotenzial)")
        print(f"    Avg Effizienz:    {_num(avg_eff, 3)}              (PnL / MFE — Ziel: >0.5)")
        print(f"    Avg PnL:          {_num(avg_pnl)} USDT")
        print(f"    Avg Haltedauer:   {_num(avg_dur)} Stunden")

        if avg_eff < 0.3 and avg_mfe > 0:
            print(f"    → Take-Profit zu eng — nur {avg_eff*100:.0f}% des Potenzials realisiert")
        if mae_pct > 0 and sl_pct > 0 and mae_pct > sl_pct * 0.8:
            print(f"    → Stop-Loss ({sl_pct:.2f}%) nahe MAE ({mae_pct:.2f}%) — ggf. um 15-20% weiter setzen")
        print()


# ============================================================
# SEKTION 4 — SIGNAL ANALYSE
# ============================================================

def section_4(signals: pd.DataFrame):
    header("SEKTION 4 — Signal Analyse")

    if signals.empty:
        no_data("Signale")
        return

    total = len(signals)
    print(f"  Gesamt Signale:  {total}")

    for strat in ["TF", "MR"]:
        sd = signals[signals["strategy"] == strat] if "strategy" in signals.columns else pd.DataFrame()
        if sd.empty:
            continue
        n_long  = len(sd[sd["signal_type"] == "LONG"])
        n_short = len(sd[sd["signal_type"] == "SHORT"])
        n_none  = len(sd[sd["signal_type"] == "NONE"])
        blocked_cb  = len(sd[sd["blocked_by"] == "CIRCUIT_BREAKER"])
        blocked_pos = len(sd[sd["blocked_by"] == "POSITION_OPEN"])
        avg_str = sd[sd["signal_type"] != "NONE"]["signal_strength"].mean()

        print()
        print(f"  [{strat}] {len(sd)} Signale pro Kerze")
        print(f"    LONG:            {n_long:>5}  /  SHORT: {n_short:>5}  /  NONE: {n_none:>5}")
        print(f"    Geblockt CB:     {blocked_cb:>5}   Geblockt Position: {blocked_pos:>5}")
        if not pd.isna(avg_str):
            print(f"    Avg Stärke:      {avg_str:>6.1f}/100  (nur Signale ≠ NONE)")

        # Signal-Stärke Verteilung für Empfehlung
        actual_signals = sd[sd["signal_type"] != "NONE"]
        if len(actual_signals) >= 5:
            strong = actual_signals[actual_signals["signal_strength"] >= 60]
            strong_wr = None
            # Wäre schön wenn wir Trades matchen könnten, aber ohne Join hier
            print(f"    Starke Signale (≥60): {len(strong):>4}  ({len(strong)/len(actual_signals)*100:.0f}% aller Signale)")
            if len(strong) / max(len(actual_signals), 1) < 0.3:
                print(f"    → Empfehlung: Signal-Stärke-Filter ≥60 könnte Trades auf Qualität selektieren")

    print()


# ============================================================
# SEKTION 5 — FEAR & GREED KORRELATION
# ============================================================

def section_5(quality: pd.DataFrame, external: pd.DataFrame):
    header("SEKTION 5 — Fear & Greed Korrelation")

    if quality.empty or external.empty:
        no_data("Fear & Greed Daten")
        return

    try:
        mr = quality[quality["strategy"] == "MR"].copy()
        if mr.empty:
            print("  Keine MR-Trades für Korrelationsanalyse.\n")
            return

        ext = external.dropna(subset=["fear_greed_value"]).copy()
        ext["timestamp"] = pd.to_datetime(ext["timestamp"], utc=True)
        mr["_ts"]        = pd.to_datetime(mr["timestamp_entry"], utc=True)

        mr_sorted  = mr.sort_values("_ts")
        ext_sorted = ext.sort_values("timestamp")
        merged     = pd.merge_asof(
            mr_sorted, ext_sorted[["timestamp", "fear_greed_value", "fear_greed_label"]],
            left_on="_ts", right_on="timestamp", direction="backward",
        )

        if merged.empty or merged["fear_greed_value"].isna().all():
            print("  Nicht genug Daten für Korrelation.\n")
            return

        corr = merged["pnl"].corr(merged["fear_greed_value"])
        print(f"  Korrelation MR-PnL × Fear&Greed:  {corr:>+.3f}")
        print()

        # Tabelle: F&G-Bereiche vs MR-Trades
        bins   = [0, 25, 45, 55, 75, 100]
        labels = ["Extreme Fear (0-25)", "Fear (25-45)", "Neutral (45-55)",
                  "Greed (55-75)", "Extreme Greed (75-100)"]
        merged["fg_bin"] = pd.cut(merged["fear_greed_value"], bins=bins, labels=labels, right=True)

        print(f"  {'F&G Bereich':<25} {'Trades':>7} {'PnL':>12} {'Win-Rate':>10}")
        print(f"  {'─'*25} {'─'*7} {'─'*12} {'─'*10}")
        for label in labels:
            bd = merged[merged["fg_bin"] == label]
            if bd.empty:
                continue
            wr  = len(bd[bd["pnl"] > 0]) / len(bd) * 100
            pnl = bd["pnl"].sum()
            print(f"  {label:<25} {len(bd):>7} {pnl:>+11.2f} {wr:>9.0f}%")

        print()
        # Empfehlung
        best_bucket = merged.groupby("fg_bin", observed=True)["pnl"].mean()
        if not best_bucket.empty:
            best = best_bucket.idxmax()
            print(f"  → [MR] Beste Performance bei: {best}")
            if corr < -0.3:
                print(f"  → Negative Korrelation ({corr:.2f}) — MR profitiert bei Extreme Fear")
            elif corr > 0.3:
                print(f"  → Positive Korrelation ({corr:.2f}) — MR profitiert bei Greed")
        print()
    except Exception as e:
        print(f"  [Fehler in F&G Analyse: {e}]\n")


# ============================================================
# SEKTION 6 — SYSTEM STABILITÄT
# ============================================================

def section_6(health: pd.DataFrame, start: datetime, end: datetime):
    header("SEKTION 6 — System Stabilität")

    if health.empty:
        no_data("System Health")
        return

    total_hours   = (end - start).total_seconds() / 3600
    records       = len(health)
    expected_15m  = total_hours * 4            # 4x pro Stunde
    uptime_pct    = min(100.0, records / max(expected_15m, 1) * 100)

    avg_latency   = health["api_latency_ms"].dropna().mean()
    max_latency   = health["api_latency_ms"].dropna().max()
    avg_ram       = health["ram_usage_mb"].dropna().mean()
    avg_cpu       = health["cpu_pct"].dropna().mean()
    total_errors  = health["error_count_total"].max() if "error_count_total" in health.columns else 0
    last_error    = health["last_error"].dropna().iloc[-1] if "last_error" in health.columns and not health["last_error"].dropna().empty else "—"

    verdict = "✅ STABIL" if uptime_pct >= 99 else ("⚠️  GRENZWERTIG" if uptime_pct >= 95 else "❌ INSTABIL")

    print(f"  Uptime:           {uptime_pct:>6.1f}%   ({records}/{expected_15m:.0f} Health-Checks)  {verdict}")
    print(f"  Avg API-Latenz:   {_num(avg_latency, 0)} ms   (Max: {_num(max_latency, 0)} ms)")
    print(f"  Avg RAM:          {_num(avg_ram, 1)} MB")
    print(f"  Avg CPU:          {_num(avg_cpu, 1)}%")
    print(f"  Fehler gesamt:    {int(total_errors) if not pd.isna(total_errors) else 0}")
    print(f"  Letzter Fehler:   {str(last_error)[:60]}")
    print()

    if uptime_pct < 99:
        gaps = expected_15m - records
        print(f"  → {gaps:.0f} Health-Check-Lücken — Neustart oder Netzwerkausfall prüfen")
    print()


# ============================================================
# SEKTION 7 — AUTOMATISCHE EMPFEHLUNGEN
# ============================================================

def section_7(quality: pd.DataFrame, signals: pd.DataFrame,
              market: pd.DataFrame, external: pd.DataFrame):
    header("SEKTION 7 — Automatische Empfehlungen")

    recommendations = []

    # MAE vs Stop-Loss Analyse
    if not quality.empty and "mae" in quality.columns and "stop_loss" in quality.columns:
        for strat in ["TF", "MR"]:
            d = quality[quality["strategy"] == strat]
            if len(d) >= 3:
                avg_mae = d["mae"].mean()
                avg_sl  = abs(d["entry_price"] - d["stop_loss"]).mean()
                if avg_sl > 0 and avg_mae > avg_sl * 0.8:
                    pct = round((avg_mae / avg_sl - 1) * 100)
                    recommendations.append(
                        f"→ [{strat}] Stop-Loss um ~{pct}% weiter setzen "
                        f"(MAE={avg_mae:.2f} überschreitet SL-Distanz={avg_sl:.2f})"
                    )
                avg_eff = d["efficiency"].mean()
                if 0 < avg_eff < 0.4:
                    recommendations.append(
                        f"→ [{strat}] Take-Profit verlängern "
                        f"(Effizienz {avg_eff:.2f} — nur {avg_eff*100:.0f}% des MFE realisiert)"
                    )

    # Circuit Breaker Häufigkeit
    if not signals.empty and "blocked_by" in signals.columns:
        cb_blocks = len(signals[signals["blocked_by"] == "CIRCUIT_BREAKER"])
        if cb_blocks > 5:
            recommendations.append(
                f"→ Circuit Breaker Schwellwert auf 4 erhöhen "
                f"({cb_blocks} blockierte Signale — möglicherweise zu sensitiv)"
            )

    # Fear & Greed Empfehlung
    if not quality.empty and not external.empty:
        try:
            mr  = quality[quality["strategy"] == "MR"].copy()
            ext = external.dropna(subset=["fear_greed_value"]).copy()
            if not mr.empty and not ext.empty:
                ext["timestamp"] = pd.to_datetime(ext["timestamp"], utc=True)
                mr["_ts"]        = pd.to_datetime(mr["timestamp_entry"], utc=True)
                merged = pd.merge_asof(
                    mr.sort_values("_ts"), ext.sort_values("timestamp"),
                    left_on="_ts", right_on="timestamp", direction="backward",
                )
                if not merged.empty and not merged["fear_greed_value"].isna().all():
                    fear_trades = merged[merged["fear_greed_value"] < 30]
                    all_mr_wr   = len(merged[merged["pnl"] > 0]) / max(len(merged), 1)
                    fear_wr     = len(fear_trades[fear_trades["pnl"] > 0]) / max(len(fear_trades), 1)
                    if len(fear_trades) >= 2 and fear_wr > all_mr_wr + 0.1:
                        recommendations.append(
                            f"→ [MR] bevorzugt bei Fear&Greed < 30 handeln "
                            f"(Win-Rate Fear: {fear_wr*100:.0f}% vs Gesamt: {all_mr_wr*100:.0f}%)"
                        )
        except Exception:
            pass

    # Regime-basierte Empfehlung
    if not quality.empty and not market.empty:
        try:
            q = quality.copy()
            m = market.copy()
            m["timestamp"] = pd.to_datetime(m["timestamp"], utc=True)
            q["_ts"]       = pd.to_datetime(q["timestamp_entry"], utc=True)
            merged = pd.merge_asof(
                q.sort_values("_ts"), m.sort_values("timestamp")[["timestamp", "market_regime"]],
                left_on="_ts", right_on="timestamp", direction="backward",
            )
            for strat in ["TF", "MR"]:
                sd = merged[merged["strategy"] == strat]
                if len(sd) < 3:
                    continue
                regime_pnl = sd.groupby("market_regime")["pnl"].sum()
                worst = regime_pnl.idxmin()
                if regime_pnl[worst] < -50:
                    recommendations.append(
                        f"→ [{strat}] im Regime '{worst}' pausieren "
                        f"(PnL: {regime_pnl[worst]:+.2f} USDT)"
                    )
        except Exception:
            pass

    if not recommendations:
        recommendations.append("→ Noch zu wenig Daten für automatische Empfehlungen (min. 5 Trades pro Strategie)")

    for r in recommendations:
        print(f"  {r}")
    print()


# ============================================================
# EINSTIEGSPUNKT
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="QuantBot Pro — Paper Trading Analyse",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Beispiele:
  python3 analyze.py --days 7
  python3 analyze.py --from 2026-04-20 --to 2026-04-27
  python3 analyze.py --days 1   # Schnell-Check nach 24h
        """,
    )
    parser.add_argument("--days", type=int, help="Letzten N Tage analysieren")
    parser.add_argument("--from", dest="from_date", help="Startdatum (YYYY-MM-DD)")
    parser.add_argument("--to",   dest="to_date",   help="Enddatum (YYYY-MM-DD)")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)

    if args.days:
        start = (now - timedelta(days=args.days)).replace(hour=0, minute=0, second=0)
        end   = now
    elif args.from_date and args.to_date:
        start = datetime.strptime(args.from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end   = datetime.strptime(args.to_date,   "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc
        )
    else:
        print("Bitte --days N oder --from/--to angeben.")
        sys.exit(1)

    print(f"""
╔{'═'*W1}╗
║   QUANTBOT PRO — PAPER TRADING ANALYSE{' '*(W1-40)}║
║   Zeitraum: {start.strftime('%Y-%m-%d')} bis {end.strftime('%Y-%m-%d')}{' '*(W1-37)}║
╚{'═'*W1}╝
    """)

    print("  Lade CSV-Dateien...", end=" ", flush=True)
    data = load_date_range(start, end)

    # Kurze Statistik der geladenen Daten
    loaded = {k: len(v) for k, v in data.items() if not v.empty}
    if not loaded:
        print("\n  Keine Daten gefunden. Bitte portfolio.py zuerst starten.\n")
        sys.exit(0)
    print(f"OK  ({', '.join(f'{k}:{n}' for k, n in loaded.items())})")
    print()

    section_1(data["quality"], data["snapshots"])
    section_2(data["quality"], data["market"])
    section_3(data["quality"])
    section_4(data["signals"])
    section_5(data["quality"], data["external"])
    section_6(data["health"], start, end)
    section_7(data["quality"], data["signals"], data["market"], data["external"])

    print(f"{'═'*W1}")
    print(f"  Analyse abgeschlossen. Log-Verzeichnis: {LOG_DIR}/")
    print(f"{'═'*W1}\n")


if __name__ == "__main__":
    main()
