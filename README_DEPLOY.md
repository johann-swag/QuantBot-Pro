# QuantBot Pro — Proxmox LXC Deploy

## Voraussetzungen
- Proxmox VE installiert
- Ubuntu 22.04 LXC Template verfügbar

## Schritt 1 — LXC Container erstellen (Proxmox UI)
```
Template:   Ubuntu 22.04
RAM:        512MB
Disk:       8GB
CPU:        1 Core
Netzwerk:   vmbr0, DHCP
Hostname:   quantbot
```

## Schritt 2 — Im Container
```bash
curl -O https://raw.githubusercontent.com/johann-swag/quantbot-pro/main/deploy.sh
chmod +x deploy.sh

# .env erstellen BEVOR deploy.sh läuft
mkdir -p /opt/quantbot
nano /opt/quantbot/.env
```

Inhalt der `.env`:
```
TELEGRAM_TOKEN=dein_token
TELEGRAM_CHAT_ID=deine_chat_id
```

```bash
bash deploy.sh
```

## Schritt 3 — Verifizieren
```bash
systemctl status quantbot
journalctl -u quantbot -f
# Telegram Alert muss kommen
```

## Schritt 4 — Cronjob für Healthcheck
```bash
crontab -e
# Einfügen:
*/5 * * * * /opt/quantbot/healthcheck.sh
```

## Nützliche Befehle
```bash
systemctl status quantbot       # Status prüfen
systemctl restart quantbot      # Neustart
bash /opt/quantbot/update.sh    # Code updaten
journalctl -u quantbot -f       # Live Logs
tail -f /opt/quantbot/logs/service.log          # Service Log
tail -f /opt/quantbot/logs/healthcheck.log      # Healthcheck Log
cd /opt/quantbot && source .venv/bin/activate
python3 analyze.py --days 1     # Schnelle Analyse
python3 analyze.py --days 7     # 7-Tage Auswertung
```

## Auto-Restart Verhalten
- Bei Absturz: Neustart nach 30 Sekunden (`RestartSec=30`)
- Maximal 5 Neustarts in 5 Minuten, dann dauerhafter Stop
- Healthcheck-Cronjob startet in diesem Fall manuell neu

## Logs
```
logs/service.log         — stdout (Terminal-Box, Portfolio-Events)
logs/service_error.log   — stderr (Fehler, Exceptions)
logs/healthcheck.log     — Healthcheck-Protokoll
logs/YYYY-MM-DD/         — CSV-Daten (logger.py)
logs/portfolio_trades.json
```
