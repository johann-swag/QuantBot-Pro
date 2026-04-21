#!/bin/bash
# ============================================================
#  QuantBot Pro — Master Installer (TICKET-18)
#  Läuft auf Proxmox HOST.
#
#  Aufruf:
#    bash -c "$(curl -fsSL https://raw.githubusercontent.com/johann-swag/QuantBot-Pro/main/install.sh)"
# ============================================================

set -e

REPO_URL="https://raw.githubusercontent.com/johann-swag/QuantBot-Pro/main"
SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo /tmp/quantbot-install)"
PROXMOX_IP=$(hostname -I | awk '{print $1}')

# ── Config-Datei laden (überschreibt ENV, wird von Prompts überschrieben) ──
# Pfad: ./quantbot.conf  oder  /opt/quantbot.conf  oder  --config <pfad>
CONFIG_FILE=""
if [ "$1" = "--config" ] && [ -n "$2" ]; then
    CONFIG_FILE="$2"
elif [ -f "./quantbot.conf" ]; then
    CONFIG_FILE="./quantbot.conf"
elif [ -f "/opt/quantbot.conf" ]; then
    CONFIG_FILE="/opt/quantbot.conf"
fi

if [ -n "$CONFIG_FILE" ]; then
    echo "  → Lade Konfiguration aus: $CONFIG_FILE"
    # Nur KEY=VALUE Zeilen laden, Kommentare (#) und Leerzeilen ignorieren
    while IFS='=' read -r KEY VAL; do
        [[ "$KEY" =~ ^#.*$ || -z "$KEY" ]] && continue
        VAL="${VAL%%#*}"   # inline Kommentare entfernen
        VAL="${VAL%"${VAL##*[![:space:]]}"}"  # trailing whitespace
        export "$KEY"="$VAL"
    done < "$CONFIG_FILE"
fi

# ── SCHRITT 1 — Banner ────────────────────────────────────────

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   QuantBot Pro — Master Installer            ║"
echo "║   Proxmox LXC Bot Swarm Setup                ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ── SCHRITT 2 — Voraussetzungen ───────────────────────────────

echo "=== Voraussetzungen prüfen ==="

if ! command -v pct &>/dev/null; then
    echo "  ❌ FEHLER: pct nicht gefunden — läuft dieses Script auf einem Proxmox Host?"
    exit 1
fi
echo "  ✓ Proxmox erkannt"

if ! ping -c 1 -W 3 8.8.8.8 > /dev/null 2>&1; then
    echo "  ❌ FEHLER: Kein Internet (ping 8.8.8.8 fehlgeschlagen)"
    exit 1
fi
echo "  ✓ Internet erreichbar"

if ! pveam list local 2>/dev/null | grep -q "ubuntu-22.04"; then
    echo "  Template nicht gefunden — lade Ubuntu 22.04..."
    pveam update
    pveam download local ubuntu-22.04-standard_22.04-1_amd64.tar.zst
fi
echo "  ✓ Ubuntu 22.04 Template vorhanden"

# create_lxc.sh verfügbar machen
if [ ! -f "$SCRIPT_DIR/create_lxc.sh" ]; then
    echo "  Lade create_lxc.sh..."
    mkdir -p "$SCRIPT_DIR"
    curl -fsSL "$REPO_URL/create_lxc.sh" -o "$SCRIPT_DIR/create_lxc.sh"
    chmod +x "$SCRIPT_DIR/create_lxc.sh"
fi
echo "  ✓ create_lxc.sh bereit"
echo ""

# ── SCHRITT 3 — Globale Konfiguration ────────────────────────

echo "=== Telegram ==="
if [ -z "$TELEGRAM_TOKEN" ]; then
    read -s -p "  Telegram Token: " TELEGRAM_TOKEN
    echo
    TELEGRAM_TOKEN="${TELEGRAM_TOKEN#*=}"
else
    echo "  ✓ Telegram Token aus ENV"
fi
if [ -z "$TELEGRAM_CHAT_ID" ]; then
    read -p "  Telegram Chat ID: " TELEGRAM_CHAT_ID
    TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID#*=}"
else
    echo "  ✓ Telegram Chat ID aus ENV"
fi

echo ""
echo "=== Binance API (optional — nur für Live Trading) ==="
echo "  Für Paper Trading einfach ENTER drücken"
if [ -z "$BINANCE_API_KEY" ]; then
    read -p "  Binance API Key [leer = Paper]: " BINANCE_API_KEY
fi
if [ -n "$BINANCE_API_KEY" ] && [ -z "$BINANCE_API_SECRET" ]; then
    read -s -p "  Binance API Secret: " BINANCE_API_SECRET
    echo
fi
if [ -z "$BINANCE_API_KEY" ]; then
    echo "  → Paper Trading Modus"
else
    echo "  ✓ Binance API gesetzt"
fi

echo ""
echo "=== Backtest Konfiguration ==="
if [ -z "$BACKTEST_DAYS" ]; then
    echo "  1) Schnelltest  —  90 Tage"
    echo "  2) Standard     — 365 Tage (empfohlen)"
    echo "  3) Langzeittest — 730 Tage"
    read -p "  Wahl [1-3]: " BACKTEST_CHOICE
    case $BACKTEST_CHOICE in
        1) BACKTEST_DAYS=90 ;;
        3) BACKTEST_DAYS=730 ;;
        *) BACKTEST_DAYS=365 ;;
    esac
else
    echo "  ✓ Backtest-Tage aus ENV: $BACKTEST_DAYS"
fi

if [ -z "$START_CAPITAL" ]; then
    read -p "  Startkapital pro Container in USDT [10000]: " START_CAPITAL
    START_CAPITAL=${START_CAPITAL:-10000}
else
    echo "  ✓ Kapital aus ENV: $START_CAPITAL USDT"
fi

echo ""
echo "=== Symbol ==="
if [ -z "$SYMBOL" ]; then
    echo "  1) BTC/USDT (Standard)"
    echo "  2) ETH/USDT"
    echo "  3) BTC/USDT + ETH/USDT"
    read -p "  Wahl [1-3]: " SYMBOL_CHOICE
    case $SYMBOL_CHOICE in
        2) SYMBOL="ETH/USDT" ;;
        3) SYMBOL="BTC/USDT ETH/USDT" ;;
        *) SYMBOL="BTC/USDT" ;;
    esac
else
    echo "  ✓ Symbol aus ENV: $SYMBOL"
fi

# ── SCHRITT 4 — Container Auswahl ────────────────────────────

echo ""
echo "=== Bot Swarm Konfiguration ==="
echo "  Welche Container sollen erstellt werden?"
echo ""
echo "  [1] Nur Konservativ   — 772 (ADX>25, RSI<25)"
echo "  [2] Nur Moderat       — 773 (ADX>20, RSI<30)"
echo "  [3] Beide — A/B Test  — 772 + 773 (empfohlen)"
echo "  [4] Custom            — eigene CTID + Parameter"
read -p "  Wahl [1-4]: " SWARM_CHOICE

CONTAINERS=()

case $SWARM_CHOICE in
    1)
        CONTAINERS=("772:quantbot-konservativ:portfolio:25:25:65")
        ;;
    2)
        CONTAINERS=("773:quantbot-moderat:portfolio:20:30:60")
        ;;
    3)
        CONTAINERS=(
            "772:quantbot-konservativ:portfolio:25:25:65"
            "773:quantbot-moderat:portfolio:20:30:60"
        )
        ;;
    4)
        echo ""
        read -p "  Container ID (z.B. 774): " CUSTOM_CTID
        read -p "  Hostname (z.B. quantbot-custom): " CUSTOM_HOST
        echo "  Strategie: 1) portfolio  2) trend  3) mean_reversion"
        read -p "  Wahl [1-3]: " STR_CHOICE
        case $STR_CHOICE in
            2) CUSTOM_STRATEGY="trend" ;;
            3) CUSTOM_STRATEGY="mean_reversion" ;;
            *) CUSTOM_STRATEGY="portfolio" ;;
        esac
        read -p "  TF ADX-Threshold [25]: " CUSTOM_ADX
        CUSTOM_ADX=${CUSTOM_ADX:-25}
        read -p "  MR RSI Long-Grenze [25]: " CUSTOM_RSI_L
        CUSTOM_RSI_L=${CUSTOM_RSI_L:-25}
        read -p "  MR RSI Short-Grenze [65]: " CUSTOM_RSI_S
        CUSTOM_RSI_S=${CUSTOM_RSI_S:-65}
        CONTAINERS=("$CUSTOM_CTID:$CUSTOM_HOST:$CUSTOM_STRATEGY:$CUSTOM_ADX:$CUSTOM_RSI_L:$CUSTOM_RSI_S")
        ;;
    *)
        echo "  Ungültige Eingabe — beende."
        exit 1
        ;;
esac

# ── SCHRITT 5 — Zusammenfassung ───────────────────────────────

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   Setup Zusammenfassung                      ║"
echo "╠══════════════════════════════════════════════╣"
printf "║  Telegram:   %-31s║\n" "konfiguriert ✓"
if [ -z "$BINANCE_API_KEY" ]; then
    printf "║  Binance:    %-31s║\n" "Paper Trading (kein Key)"
else
    printf "║  Binance:    %-31s║\n" "API Key gesetzt ✓"
fi
printf "║  Backtest:   %-31s║\n" "$BACKTEST_DAYS Tage"
printf "║  Kapital:    %-31s║\n" "$START_CAPITAL USDT"
printf "║  Symbol:     %-31s║\n" "$SYMBOL"
echo "║                                              ║"
echo "║  Container:                                  ║"
for C in "${CONTAINERS[@]}"; do
    IFS=':' read -r CTID CHOST CSTRAT CADX CRSI_L CRSI_S <<< "$C"
    printf "║    %s  %-37s║\n" "$CTID" "$CHOST ($CSTRAT)"
done
echo "╚══════════════════════════════════════════════╝"
echo ""
read -p "Starten? [j/n]: " CONFIRM
if [ "$CONFIRM" != "j" ]; then
    echo "Abgebrochen."
    exit 0
fi

# ── SCHRITT 6 — Container erstellen ───────────────────────────

export TELEGRAM_TOKEN TELEGRAM_CHAT_ID
export BINANCE_API_KEY BINANCE_API_SECRET
export BACKTEST_DAYS START_CAPITAL SYMBOL
export AUTO_CONFIRM=1

declare -A CONTAINER_IPS

for C in "${CONTAINERS[@]}"; do
    IFS=':' read -r CTID CHOST CSTRAT CADX CRSI_L CRSI_S <<< "$C"

    echo ""
    echo "════════════════════════════════════════════════"
    echo "  Erstelle Container $CTID — $CHOST"
    echo "════════════════════════════════════════════════"

    export TF_ADX_THRESHOLD=$CADX
    export MR_RSI_LONG=$CRSI_L
    export MR_RSI_SHORT=$CRSI_S

    bash "$SCRIPT_DIR/create_lxc.sh" "$CTID" "$CHOST" "$CSTRAT"

    IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')
    CONTAINER_IPS[$CTID]=$IP
done

# ── SCHRITT 7 — Central Dashboard + Log Collector ─────────────

echo ""
echo "════════════════════════════════════════════════"
echo "  Setup Central Dashboard + Log Collector"
echo "════════════════════════════════════════════════"

if [ ! -f "$SCRIPT_DIR/setup_collector.sh" ]; then
    curl -fsSL "$REPO_URL/setup_collector.sh" -o "$SCRIPT_DIR/setup_collector.sh"
fi
if [ ! -f "$SCRIPT_DIR/compare.py" ]; then
    curl -fsSL "$REPO_URL/compare.py" -o "$SCRIPT_DIR/compare.py"
fi
if [ ! -f "$SCRIPT_DIR/log_collector.sh" ]; then
    curl -fsSL "$REPO_URL/log_collector.sh" -o "$SCRIPT_DIR/log_collector.sh"
fi

bash "$SCRIPT_DIR/setup_collector.sh" || echo "  ⚠️  Log-Collector Setup übersprungen (nicht kritisch)"

# ── SCHRITT 8 — Abschlussmeldung ──────────────────────────────

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   ✅ QuantBot Pro Swarm läuft!               ║"
echo "╠══════════════════════════════════════════════╣"
for CTID in "${!CONTAINER_IPS[@]}"; do
    IP="${CONTAINER_IPS[$CTID]}"
    printf "║  Container %s:  %-28s║\n" "$CTID" "http://$IP:5000"
done
printf "║  Log-Collector: %-28s║\n" "Cronjob aktiv (*/15 min)"
printf "║  Vergleich:     %-28s║\n" "compare.py --all"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "  Nächste Schritte:"
echo "  → Nach 7 Tagen: python3 /opt/quantbot-central-logs/compare.py --all --days 7"
echo "  → Logs live:    pct exec 772 -- journalctl -u quantbot -f"
echo ""
