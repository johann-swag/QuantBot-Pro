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
pct exec $CTID -- bash -c "
    apt update -qq
    apt install -y git python3 python3-venv curl htop -qq

    git clone https://github.com/johann-swag/QuantBot-Pro.git /opt/quantbot

    cd /opt/quantbot
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt -q

    cat > /opt/quantbot/.env << 'ENVEOF'
TELEGRAM_TOKEN=$TELEGRAM_TOKEN
TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID
STRATEGY=$STRATEGY
ENVEOF

    cp quantbot.service /etc/systemd/system/quantbot.service
    systemctl daemon-reload
    systemctl enable quantbot
    systemctl start quantbot
"

# IP holen
IP=$(pct exec $CTID -- hostname -I | tr -d ' ')

echo ""
echo "✅ Container $CTID bereit!"
echo "   Strategie:  $STRATEGY"
echo "   Dashboard:  http://$IP:5000"
echo "   Logs:       pct exec $CTID -- journalctl -u quantbot -f"
echo "   Stop:       pct stop $CTID"
