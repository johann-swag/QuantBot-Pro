#!/bin/bash
# ============================================================
#  QuantBot Pro — Zentraler Log-Collector (TICKET-17)
#  Läuft auf Proxmox HOST als Cronjob alle 15 Minuten.
#  Sammelt Logs aller laufenden quantbot Container.
#
#  Cronjob: */15 * * * * /opt/quantbot-central-logs/log_collector.sh
# ============================================================

CENTRAL_LOG_DIR="/opt/quantbot-central-logs"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

mkdir -p "$CENTRAL_LOG_DIR"

for CTID in $(pct list | grep 'quantbot' | awk '{print $1}'); do
    CONTAINER_NAME=$(pct list | grep "^$CTID" | awk '{print $4}')
    TARGET_DIR="$CENTRAL_LOG_DIR/$CTID-$CONTAINER_NAME"
    mkdir -p "$TARGET_DIR"

    if pct status "$CTID" | grep -q "running"; then
        # Logs im Container packen
        pct exec "$CTID" -- tar -czf /tmp/logs_snapshot.tar.gz \
            /opt/quantbot/logs 2>/dev/null

        # Auf Host ziehen
        pct pull "$CTID" /tmp/logs_snapshot.tar.gz \
            "$TARGET_DIR/logs_snapshot.tar.gz" 2>/dev/null

        # Entpacken: /opt/quantbot/logs/YYYY-MM-DD/ → TARGET_DIR/YYYY-MM-DD/
        tar -xzf "$TARGET_DIR/logs_snapshot.tar.gz" \
            -C "$TARGET_DIR" --strip-components=3 2>/dev/null

        # Aufräumen
        rm -f "$TARGET_DIR/logs_snapshot.tar.gz"
        pct exec "$CTID" -- rm -f /tmp/logs_snapshot.tar.gz 2>/dev/null

        echo "$TIMESTAMP OK   $CTID $CONTAINER_NAME" >> "$CENTRAL_LOG_DIR/collector.log"
    else
        echo "$TIMESTAMP SKIP $CTID $CONTAINER_NAME (stopped)" >> "$CENTRAL_LOG_DIR/collector.log"
    fi
done

# Log auf 1000 Zeilen begrenzen
tail -1000 "$CENTRAL_LOG_DIR/collector.log" > "$CENTRAL_LOG_DIR/collector.log.tmp" \
    && mv "$CENTRAL_LOG_DIR/collector.log.tmp" "$CENTRAL_LOG_DIR/collector.log"
