#!/usr/bin/env bash
# scripts/backup.sh
# Dump the wins database and keep 7 days of backups.
#
# VPS crontab setup (runs nightly at 02:00):
#   0 2 * * * /path/to/wins/scripts/backup.sh >> /var/log/wins-backup.log 2>&1
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/wins}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUT="$BACKUP_DIR/wins_${TIMESTAMP}.sql.gz"

mkdir -p "$BACKUP_DIR"
docker exec wins-db pg_dump -d wins | gzip > "$OUT"
echo "[$(date -u +%FT%TZ)] Backup written: $OUT ($(du -sh "$OUT" | cut -f1))"

# Prune backups older than 7 days
find "$BACKUP_DIR" -name "wins_*.sql.gz" -mtime +7 -delete
echo "[$(date -u +%FT%TZ)] Old backups pruned."
