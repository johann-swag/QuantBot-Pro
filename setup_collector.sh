#!/bin/bash
# ============================================================
#  QuantBot Pro — Setup Log-Collector auf Proxmox HOST (TICKET-17)
#  Einmalig auf dem Proxmox Host ausführen.
# ============================================================

set -e

CENTRAL_LOG_DIR="/opt/quantbot-central-logs"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "============================================================"
echo "  QuantBot Pro — Log-Collector Setup"
echo "============================================================"

# Verzeichnis anlegen
mkdir -p "$CENTRAL_LOG_DIR"

# Scripts kopieren
cp "$SCRIPT_DIR/log_collector.sh" "$CENTRAL_LOG_DIR/log_collector.sh"
cp "$SCRIPT_DIR/compare.py"       "$CENTRAL_LOG_DIR/compare.py"
chmod +x "$CENTRAL_LOG_DIR/log_collector.sh"

echo "  [1/4] Scripts kopiert nach $CENTRAL_LOG_DIR"

# Python + pandas installieren
apt install -y python3 python3-pip -q 2>/dev/null || true
pip3 install pandas --break-system-packages -q 2>/dev/null \
    || pip3 install pandas -q 2>/dev/null \
    || true

echo "  [2/4] Python-Abhängigkeiten installiert"

# Cronjob einrichten (Duplikate vermeiden)
CRON_ENTRY="*/15 * * * * $CENTRAL_LOG_DIR/log_collector.sh >> $CENTRAL_LOG_DIR/collector.log 2>&1"
( crontab -l 2>/dev/null | grep -v "log_collector.sh"; echo "$CRON_ENTRY" ) | crontab -

echo "  [3/4] Cronjob eingerichtet (*/15 * * * *)"

# Einmal manuell ausführen
echo "  [4/4] Erster Lauf..."
bash "$CENTRAL_LOG_DIR/log_collector.sh"

echo ""
echo "✅ Log-Collector aktiv!"
echo "   Logs in:    $CENTRAL_LOG_DIR/"
echo "   Vergleich:  python3 $CENTRAL_LOG_DIR/compare.py --containers 772 773"
echo "   Alle:       python3 $CENTRAL_LOG_DIR/compare.py --all"
echo "   7-Tage:     python3 $CENTRAL_LOG_DIR/compare.py --containers 772 773 --days 7"
