#!/bin/bash
set -euo pipefail

BACKUP_DIR="/home/picopiece/backup/openclaw"
SOURCE_DIR="/home/picopiece/openclaw/data"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/openclaw_backup_${TIMESTAMP}.tar.gz"
MAX_BACKUPS=7

mkdir -p "$BACKUP_DIR"

tar -czf "$BACKUP_FILE" -C "$(dirname "$SOURCE_DIR")" "$(basename "$SOURCE_DIR")"

echo "[$(date)] Backup created: $BACKUP_FILE ($(du -h "$BACKUP_FILE" | cut -f1))"

ls -1t "$BACKUP_DIR"/openclaw_backup_*.tar.gz 2>/dev/null | tail -n +$((MAX_BACKUPS + 1)) | xargs -r rm -f

echo "[$(date)] Active backups: $(ls -1 "$BACKUP_DIR"/openclaw_backup_*.tar.gz 2>/dev/null | wc -l)"
