#!/bin/bash
set -e  # Bei Fehler sofort stoppen

echo "=== QuantBot Pro Deploy ==="

# 1. System Update
apt update && apt upgrade -y

# 2. Dependencies
apt install -y python3 python3-venv git curl htop

# 3. Repo klonen (falls nicht vorhanden)
if [ ! -d "/opt/quantbot" ]; then
    git clone https://github.com/johann-swag/quantbot-pro.git /opt/quantbot
fi

# 4. Venv + Python Dependencies
cd /opt/quantbot
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 5. .env prüfen
if [ ! -f "/opt/quantbot/.env" ]; then
    echo "FEHLER: .env fehlt!"
    echo "Bitte /opt/quantbot/.env erstellen:"
    echo "  TELEGRAM_TOKEN=xxx"
    echo "  TELEGRAM_CHAT_ID=xxx"
    exit 1
fi

# 6. Logs Verzeichnis
mkdir -p /opt/quantbot/logs

# 7. Systemd Service installieren
cp /opt/quantbot/quantbot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable quantbot
systemctl start quantbot

echo "=== Deploy abgeschlossen ==="
echo "Status: systemctl status quantbot"
echo "Logs:   journalctl -u quantbot -f"
