#!/usr/bin/env bash
# Backup diario de la base de datos en formato custom de pg_dump (comprimido).
# Uso: scripts/backup.sh            (desde la raíz del proyecto, en el VPS)
# Cron sugerido (root):  15 3 * * * /opt/trading-supervisor/scripts/backup.sh >> /var/log/ts-backup.log 2>&1
#
# Importante: un backup que solo vive en el VPS no protege si se pierde el VPS.
# Copia BACKUP_DIR fuera (p. ej. con rclone a Google Drive/S3) (ver README).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT/deploy/.env"
[[ -f "$ENV_FILE" ]] || { echo "Falta $ENV_FILE" >&2; exit 1; }
set -a; source "$ENV_FILE"; set +a

BACKUP_DIR="${BACKUP_DIR:-/var/backups/trading-supervisor}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="$BACKUP_DIR/supervisor_${STAMP}.dump"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

docker compose -f "$ROOT/deploy/docker-compose.yml" --env-file "$ENV_FILE" exec -T postgres \
  pg_dump -U "${POSTGRES_USER:-supervisor}" -d "${POSTGRES_DB:-supervisor}" -Fc --no-owner \
  > "$TARGET.partial"

# Verifica que el archivo es un dump válido antes de darlo por bueno.
docker compose -f "$ROOT/deploy/docker-compose.yml" --env-file "$ENV_FILE" exec -T postgres \
  pg_restore --list < "$TARGET.partial" > /dev/null
mv "$TARGET.partial" "$TARGET"
chmod 600 "$TARGET"

find "$BACKUP_DIR" -name 'supervisor_*.dump' -mtime +"$KEEP_DAYS" -delete
echo "$(date -u +%FT%TZ) backup OK: $TARGET ($(du -h "$TARGET" | cut -f1))"
