#!/bin/bash
echo "=== QuantBot Pro Update ==="

cd /opt/quantbot

# Bot stoppen
systemctl stop quantbot

# Neuesten Code holen
git pull origin main

# Dependencies updaten
source .venv/bin/activate
pip install -r requirements.txt --quiet

# Bot neu starten
systemctl start quantbot

echo "Update abgeschlossen"
systemctl status quantbot --no-pager
