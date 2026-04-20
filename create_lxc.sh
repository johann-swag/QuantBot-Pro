#!/bin/bash
set -e

# Parameter
CTID=$1
HOSTNAME=$2
STRATEGY=$3

if [ -z "$CTID" ] || [ -z "$HOSTNAME" ] || [ -z "$STRATEGY" ]; then
    echo "Verwendung: bash create_lxc.sh <CTID> <HOSTNAME> <STRATEGIE>"
    echo "Beispiel:   bash create_lxc.sh 101 quantbot-trend trend"
    echo "            bash create_lxc.sh 102 quantbot-meanrev mean_reversion"
    echo "            bash create_lxc.sh 103 quantbot-portfolio portfolio"
    exit 1
fi

echo "╔══════════════════════════════════════════╗"
echo "║   QuantBot Pro — LXC Creator             ║"
echo "║   Container: $CTID | $HOSTNAME           ║"
echo "╚══════════════════════════════════════════╝"

# Eingaben
read -s -p "Telegram Token: " TELEGRAM_TOKEN
echo
read -p "Telegram Chat ID: " TELEGRAM_CHAT_ID

# Template prüfen
TEMPLATE="local:vztmpl/ubuntu-22.04-standard_22.04-1_amd64.tar.zst"
if ! pveam list local | grep -q "ubuntu-22.04"; then
    echo "Lade Ubuntu 22.04 Template..."
    pveam update
    pveam download local ubuntu-22.04-standard_22.04-1_amd64.tar.zst
fi

# Container erstellen
echo "Erstelle Container $CTID..."
pct create $CTID $TEMPLATE \
    --hostname $HOSTNAME \
    --memory 512 \
    --rootfs local-lvm:8 \
    --cores 1 \
    --net0 name=eth0,bridge=vmbr0,ip=dhcp \
    --unprivileged 1 \
    --features nesting=1 \
    --password quantbot123

# Starten
pct start $CTID
echo "Warte auf Container..."
sleep 10

# Setup im Container
echo "Installiere QuantBot..."
pct exec $CTID -- bash << SCRIPT
set -e

# FIX 2: Pakete + Repo
apt update -qq
apt install -y git python3 python3-venv curl htop -qq

git clone https://github.com/johann-swag/QuantBot-Pro.git /opt/quantbot

# FIX 2: venv explizit erstellen und aktivieren
cd /opt/quantbot
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -q

# FIX 3: logs Ordner
mkdir -p /opt/quantbot/logs

# FIX 1: doppelte Anführungszeichen für Variable-Expansion
cat > /opt/quantbot/.env << EOF
TELEGRAM_TOKEN=$TELEGRAM_TOKEN
TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID
STRATEGY=$STRATEGY
EOF

# FIX 4: Service direkt schreiben statt cp
cat > /etc/systemd/system/quantbot.service << 'EOF'
[Unit]
Description=QuantBot Pro Paper Trading
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/quantbot
Environment=PATH=/opt/quantbot/.venv/bin:/usr/bin:/bin
EnvironmentFile=/opt/quantbot/.env
ExecStart=/opt/quantbot/.venv/bin/python3 portfolio.py --paper --symbol BTC/USDT
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

# FIX 5: Dashboard Service
cat > /etc/systemd/system/quantbot-dashboard.service << 'EOF'
[Unit]
Description=QuantBot Pro Dashboard
After=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/quantbot
Environment=PATH=/opt/quantbot/.venv/bin:/usr/bin:/bin
EnvironmentFile=/opt/quantbot/.env
ExecStart=/opt/quantbot/.venv/bin/python3 dashboard.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable quantbot
systemctl enable quantbot-dashboard
systemctl start quantbot
systemctl start quantbot-dashboard
SCRIPT

# FIX 6: IP aus dem Container holen
IP=$(pct exec $CTID -- hostname -I | tr -d ' \n')

echo ""
echo "✅ Container $CTID bereit!"
echo "   Strategie:  $STRATEGY"
echo "   Dashboard:  http://$IP:5000"
echo "   Logs Bot:   pct exec $CTID -- journalctl -u quantbot -f"
echo "   Logs Dash:  pct exec $CTID -- journalctl -u quantbot-dashboard -f"
echo "   Stop:       pct stop $CTID"
