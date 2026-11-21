#!/usr/bin/env bash
# Backs up the MediFlow Postgres database. Works against docker-compose's
# db service or any Postgres reachable via DATABASE_URL.
#
# Usage:
#   ./scripts/backup.sh                    # docker-compose db service
#   DATABASE_URL=postgresql://... ./scripts/backup.sh   # any Postgres
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-./backups}"
TIMESTAMP=$(date -u +%Y%m%d_%H%M%S)
FILE="${BACKUP_DIR}/mediflow_${TIMESTAMP}.sql.gz"

mkdir -p "$BACKUP_DIR"

if [ -n "${DATABASE_URL:-}" ]; then
  echo "Backing up via DATABASE_URL -> ${FILE}"
  pg_dump "$DATABASE_URL" | gzip > "$FILE"
else
  echo "Backing up docker-compose 'db' service -> ${FILE}"
  docker compose exec -T db pg_dump -U mediflow mediflow | gzip > "$FILE"
fi

echo "Done: ${FILE} ($(du -h "$FILE" | cut -f1))"

# Keep the last 14 backups, prune older ones.
ls -1t "${BACKUP_DIR}"/mediflow_*.sql.gz 2>/dev/null | tail -n +15 | xargs -r rm --
