#!/bin/bash
echo "=== QuantBot Pro Update ==="

cd /opt/quantbot

# Services stoppen
systemctl stop quantbot quantbot-dashboard

# Neuesten Code holen
git pull origin main

# Dependencies updaten
source .venv/bin/activate
pip install -r requirements.txt --quiet

# Services neu starten
systemctl start quantbot quantbot-dashboard

echo "Update abgeschlossen"
systemctl status quantbot quantbot-dashboard --no-pager
