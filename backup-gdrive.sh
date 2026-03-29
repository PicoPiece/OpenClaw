#!/bin/bash
set -euo pipefail

RCLONE="/home/picopiece/.local/bin/rclone"
BACKUP_DIR="/home/picopiece/backup/openclaw"
SOURCE_DIR="/home/picopiece/openclaw/data"
GDRIVE_FOLDER="gdrive:OpenClaw-Backup"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/openclaw_backup_${TIMESTAMP}.tar.gz"
MAX_LOCAL=7
MAX_REMOTE=30

mkdir -p "$BACKUP_DIR"

echo "[$(date)] Creating local backup..."
tar -czf "$BACKUP_FILE" -C "$(dirname "$SOURCE_DIR")" "$(basename "$SOURCE_DIR")"
LOCAL_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "[$(date)] Local backup: $BACKUP_FILE ($LOCAL_SIZE)"

ls -1t "$BACKUP_DIR"/openclaw_backup_*.tar.gz 2>/dev/null | tail -n +$((MAX_LOCAL + 1)) | xargs -r rm -f
echo "[$(date)] Local backups kept: $(ls -1 "$BACKUP_DIR"/openclaw_backup_*.tar.gz 2>/dev/null | wc -l)"

if ! $RCLONE listremotes 2>/dev/null | grep -q "gdrive:"; then
    echo "[$(date)] WARN: Google Drive not configured. Local backup only."
    exit 0
fi

echo "[$(date)] Uploading to Google Drive..."
if $RCLONE copy "$BACKUP_FILE" "$GDRIVE_FOLDER/" --progress 2>&1; then
    echo "[$(date)] Upload complete."
else
    echo "[$(date)] WARN: Upload failed. Run setup-gdrive.sh to authorize Google Drive."
    exit 0
fi

REMOTE_FILES=$($RCLONE lsf "$GDRIVE_FOLDER/" --files-only 2>/dev/null | sort -r)
REMOTE_COUNT=$(echo "$REMOTE_FILES" | wc -l)
if [ "$REMOTE_COUNT" -gt "$MAX_REMOTE" ]; then
    echo "$REMOTE_FILES" | tail -n +$((MAX_REMOTE + 1)) | while read -r f; do
        $RCLONE deletefile "$GDRIVE_FOLDER/$f" 2>/dev/null
        echo "[$(date)] Deleted old remote backup: $f"
    done
fi
echo "[$(date)] Remote backups: $($RCLONE lsf "$GDRIVE_FOLDER/" --files-only 2>/dev/null | wc -l)"

echo "[$(date)] DONE."
