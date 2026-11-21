#!/usr/bin/env bash
# Restores a MediFlow Postgres backup created by backup.sh.
#
# Usage:
#   ./scripts/restore.sh backups/mediflow_20260101_000000.sql.gz
#   DATABASE_URL=postgresql://... ./scripts/restore.sh <file>
set -euo pipefail

FILE="${1:?Usage: restore.sh <backup-file.sql.gz>}"
[ -f "$FILE" ] || { echo "File not found: $FILE"; exit 1; }

echo "This will OVERWRITE the target database with the contents of ${FILE}."
read -p "Type 'yes' to continue: " CONFIRM
[ "$CONFIRM" = "yes" ] || { echo "Aborted."; exit 1; }

if [ -n "${DATABASE_URL:-}" ]; then
  gunzip -c "$FILE" | psql "$DATABASE_URL"
else
  gunzip -c "$FILE" | docker compose exec -T db psql -U mediflow mediflow
fi

echo "Restore complete."
