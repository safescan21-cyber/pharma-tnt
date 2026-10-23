#!/bin/sh
# Nightly hot backup with 30-day retention. Example cron: 15 2 * * * /opt/tnt/deploy/backup.sh
set -eu
DEST=${BACKUP_DIR:-/backups}
mkdir -p "$DEST"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
docker compose exec -T tnt python -m tnt backup "/data/backup-$STAMP.db"
docker compose cp "tnt:/data/backup-$STAMP.db" "$DEST/tnt-$STAMP.db"
docker compose exec -T tnt rm "/data/backup-$STAMP.db"
docker compose exec -T tnt python -m tnt verify-audit
find "$DEST" -name 'tnt-*.db' -mtime +30 -delete
