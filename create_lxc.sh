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

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   QuantBot Pro — Setup Konfiguration     ║"
echo "╚══════════════════════════════════════════╝"

echo ""
echo "Paper Trading Konfiguration:"
echo "  1) Schnelltest    — 90 Tage Backtest  (wenige Minuten)"
echo "  2) Standard       — 365 Tage Backtest (empfohlen)"
echo "  3) Langzeittest   — 730 Tage Backtest (dauert länger)"
echo "  4) Kein Backtest  — Dashboard startet leer"
echo ""
read -p "Wahl [1-4]: " BACKTEST_CHOICE

case $BACKTEST_CHOICE in
    1) BACKTEST_DAYS=90 ;;
    2) BACKTEST_DAYS=365 ;;
    3) BACKTEST_DAYS=730 ;;
    4) BACKTEST_DAYS=0 ;;
    *) BACKTEST_DAYS=365 ;;
esac

echo ""
echo "Startkapital (Standard: 10000):"
read -p "Kapital in USDT [10000]: " START_CAPITAL
START_CAPITAL=${START_CAPITAL:-10000}

echo ""
echo "Symbol:"
echo "  1) BTC/USDT (Standard)"
echo "  2) ETH/USDT"
echo "  3) BTC/USDT + ETH/USDT (Multi-Symbol)"
read -p "Wahl [1-3]: " SYMBOL_CHOICE

case $SYMBOL_CHOICE in
    1) SYMBOL="BTC/USDT" ;;
    2) SYMBOL="ETH/USDT" ;;
    3) SYMBOL="BTC/USDT ETH/USDT" ;;
    *) SYMBOL="BTC/USDT" ;;
esac

echo ""
echo "╔══════════════════════════════════════════╗"
printf "║   Konfiguration Zusammenfassung          ║\n"
printf "║                                          ║\n"
printf "║   Container:    %-25s║\n" "$CTID — $HOSTNAME"
printf "║   Strategie:    %-25s║\n" "$STRATEGY"
printf "║   Symbol:       %-25s║\n" "$SYMBOL"
printf "║   Kapital:      %-25s║\n" "$START_CAPITAL USDT"
printf "║   Backtest:     %-25s║\n" "$BACKTEST_DAYS Tage"
echo "╚══════════════════════════════════════════╝"
echo ""
read -p "Starten? [j/n]: " CONFIRM
if [ "$CONFIRM" != "j" ]; then
    echo "Abgebrochen."
    exit 0
fi

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
START_CAPITAL=$START_CAPITAL
SYMBOL=$SYMBOL
BACKTEST_DAYS=$BACKTEST_DAYS
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
ExecStart=/opt/quantbot/.venv/bin/python3 portfolio.py --paper
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

if [ $BACKTEST_DAYS -gt 0 ]; then
    echo "Generiere initiale Backtest-Daten ($BACKTEST_DAYS Tage)..."
    cd /opt/quantbot
    . .venv/bin/activate
    for SYM in $SYMBOL; do
        python3 bot.py --backtest --symbol "\$SYM" --days $BACKTEST_DAYS --strategy trend
        python3 bot.py --backtest --symbol "\$SYM" --days $BACKTEST_DAYS --strategy mean_reversion
    done
    python3 walk_forward.py --strategy trend
    python3 walk_forward.py --strategy mean_reversion
    echo "✅ Initiale Daten generiert"
fi

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
