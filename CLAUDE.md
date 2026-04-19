# QuantBot Pro — Architect Context

## Architektur-Regeln (NICHT verletzen)
- Max. Risiko pro Trade: 1% des Kapitals
- Circuit Breaker: bei 3 Verlust-Trades in Folge → Stop
- Jede neue Strategie als separates Modul (kein Monolith)

## Offene Tickets
- TICKET-01: Entry-Logik zu restriktiv → zu wenig Trades
- TICKET-02: RR-Berechnung buggy (77x unrealistisch)
- TICKET-03: Walk-Forward lädt nur 1000 statt max. Kerzen

## Stack
- Python 3, ccxt, pandas, ta-lib
- Backtest: bot.py --backtest
- Validation: walk_forward.py

## Aktueller Stand
- v2.0 läuft, aber generiert kaum Trades
- Ziel: v3.0 Multi-Strategy (Trend + Mean Reversion + Breakout)