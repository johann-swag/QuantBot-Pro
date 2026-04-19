"""
================================================================================
  QuantBot Pro — Telegram Alert Module
  Standalone — wird von bot.py (Paper Mode) importiert

  Verwendung:
    from telegram_bot import TelegramAlert, start_daily_summary_scheduler

    alert = TelegramAlert()
    alert.test_alert()
================================================================================
"""

import os
import json
import threading
import time
import urllib.request
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()


class TelegramAlert:
    """
    Sendet formatierte Alerts an einen Telegram-Bot.

    Konfiguration via .env:
      TELEGRAM_TOKEN=...
      TELEGRAM_CHAT_ID=...
    """

    def __init__(self, token: str = None, chat_id: str = None):
        self.token   = token   or os.getenv("TELEGRAM_TOKEN",   "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.token and self.chat_id
                            and self.token   != "dein_telegram_bot_token"
                            and self.chat_id != "deine_telegram_chat_id")

    # ── Interner HTTP-Send ────────────────────────────────────

    def send(self, text: str, parse_mode: str = "HTML") -> bool:
        """Sendet eine Nachricht. Gibt True zurück wenn erfolgreich."""
        if not self.enabled:
            return False
        try:
            url  = f"https://api.telegram.org/bot{self.token}/sendMessage"
            body = json.dumps({
                "chat_id":    self.chat_id,
                "text":       text,
                "parse_mode": parse_mode,
            }).encode()
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=8)
            return True
        except Exception as e:
            print(f"[Telegram] Fehler: {e}")
            return False

    # ── Trade Events ─────────────────────────────────────────

    def trade_entry(
        self,
        symbol: str,
        direction: str,
        price: float,
        stop_loss: float,
        take_profit: float,
        qty: float,
        risk_usdt: float,
    ):
        icon = "🟢" if direction == "LONG" else "🔴"
        rr   = abs(take_profit - price) / abs(price - stop_loss) if abs(price - stop_loss) > 0 else 0
        self.send(
            f"{icon} <b>Trade Entry — {symbol}</b>\n"
            f"{'─'*28}\n"
            f"Richtung:   <b>{direction}</b>\n"
            f"Entry:      <code>{price:.4f}</code>\n"
            f"Stop-Loss:  <code>{stop_loss:.4f}</code>\n"
            f"Take-Profit:<code>{take_profit:.4f}</code>\n"
            f"Menge:      {qty:.6f}\n"
            f"Risiko:     {risk_usdt:.2f} USDT\n"
            f"R/R-Ratio:  {rr:.2f}"
        )

    def trade_exit(
        self,
        symbol: str,
        direction: str,
        pnl: float,
        reason: str,
        balance: float,
    ):
        win  = pnl > 0
        icon = "✅" if win else "❌"
        sign = "+" if pnl >= 0 else ""
        reason_label = {
            "STOP_LOSS":   "Stop-Loss getroffen",
            "TAKE_PROFIT": "Take-Profit erreicht",
            "MANUAL":      "Manuell geschlossen",
        }.get(reason, reason)
        self.send(
            f"{icon} <b>Trade Exit — {symbol}</b>\n"
            f"{'─'*28}\n"
            f"Richtung:   {direction}\n"
            f"Grund:      {reason_label}\n"
            f"PnL:        <b>{sign}{pnl:.2f} USDT</b>\n"
            f"Balance:    {balance:.2f} USDT"
        )

    def circuit_breaker(
        self,
        reason: str,
        consecutive_losses: int,
        daily_pnl: float,
        balance: float,
    ):
        self.send(
            f"🛑 <b>CIRCUIT BREAKER AUSGELÖST</b>\n"
            f"{'─'*28}\n"
            f"Grund:           {reason}\n"
            f"Verluste in Folge: {consecutive_losses}\n"
            f"Tages-PnL:       {daily_pnl:+.2f} USDT\n"
            f"Balance:         {balance:.2f} USDT\n\n"
            f"⏸ Bot pausiert bis nächsten Tag."
        )

    def daily_summary(self, stats: dict):
        sym     = stats.get("symbol", "—")
        pnl     = stats.get("session_pnl", 0.0)
        wins    = stats.get("wins", 0)
        losses  = stats.get("losses", 0)
        balance = stats.get("balance", 0.0)
        pos     = stats.get("open_position")
        total   = wins + losses
        wr      = f"{wins/total*100:.0f}%" if total > 0 else "—"
        sign    = "+" if pnl >= 0 else ""
        pos_line = (
            f"Offene Position: {pos['direction']} @ {pos['entry_price']:.2f}"
            if pos else "Keine offene Position"
        )
        self.send(
            f"📊 <b>Tages-Zusammenfassung — {sym}</b>\n"
            f"{'─'*28}\n"
            f"Datum:     {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n"
            f"Tages-PnL: <b>{sign}{pnl:.2f} USDT</b>\n"
            f"Trades:    {total} (W:{wins} / L:{losses}) — {wr}\n"
            f"Balance:   {balance:.2f} USDT\n"
            f"{pos_line}"
        )

    def bot_started(self, symbols: list, mode: str):
        self.send(
            f"🚀 <b>QuantBot Pro gestartet</b>\n"
            f"{'─'*28}\n"
            f"Modus:    <b>{mode}</b>\n"
            f"Symbole:  {', '.join(symbols)}\n"
            f"Zeit:     {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )

    def bot_stopped(self, reason: str = "Manual"):
        self.send(f"⛔ <b>Bot gestoppt</b> — {reason}")

    def test_alert(self):
        ok = self.send(
            f"✅ <b>QuantBot Pro — Test Alert</b>\n"
            f"Telegram-Verbindung funktioniert.\n"
            f"Zeit: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        if ok:
            print("  [Telegram] Test-Alert gesendet ✅")
        else:
            print("  [Telegram] DEAKTIVIERT — Token/Chat-ID in .env prüfen")
        return ok


# ============================================================
# DAILY SUMMARY SCHEDULER (Hintergrund-Thread)
# ============================================================

def start_daily_summary_scheduler(
    alert: TelegramAlert,
    get_stats_fn,
    hour_utc: int = 20,
) -> threading.Thread:
    """
    Startet einen Daemon-Thread der täglich um `hour_utc` UTC
    die Tages-Zusammenfassung via Telegram sendet.

    get_stats_fn: Callable() → dict mit session_pnl, wins, losses,
                  balance, symbol, open_position
    """
    def _worker():
        sent_on = None
        while True:
            now = datetime.now(timezone.utc)
            if now.hour == hour_utc and now.date() != sent_on:
                try:
                    alert.daily_summary(get_stats_fn())
                    sent_on = now.date()
                except Exception as e:
                    print(f"[Telegram] Daily-Summary Fehler: {e}")
            time.sleep(60)

    t = threading.Thread(target=_worker, daemon=True, name="DailySummary")
    t.start()
    return t


# ============================================================
# CLI-TEST
# ============================================================

if __name__ == "__main__":
    print("\n  QuantBot Pro — Telegram Test\n")
    alert = TelegramAlert()

    if not alert.enabled:
        print("  TELEGRAM_TOKEN / TELEGRAM_CHAT_ID fehlen in .env")
        print("  Bitte .env ausfüllen und erneut versuchen.")
    else:
        print(f"  Token:   {alert.token[:8]}...")
        print(f"  Chat-ID: {alert.chat_id}")
        alert.test_alert()
