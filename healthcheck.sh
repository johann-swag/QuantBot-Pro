#!/bin/bash
SERVICE="quantbot"
LOG="/opt/quantbot/logs/healthcheck.log"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

if systemctl is-active --quiet $SERVICE; then
    echo "$TIMESTAMP OK — Service läuft" >> $LOG
else
    echo "$TIMESTAMP FEHLER — Service gestoppt, starte neu..." >> $LOG
    systemctl restart $SERVICE
    echo "$TIMESTAMP Neustart ausgeführt" >> $LOG
fi

# Log auf 1000 Zeilen begrenzen
tail -1000 $LOG > $LOG.tmp && mv $LOG.tmp $LOG
