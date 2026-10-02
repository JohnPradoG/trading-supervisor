#!/usr/bin/env bash
# Restaura un backup en una base NUEVA (no toca la base en uso) para verificarlo o migrar.
# Uso: scripts/restore.sh /var/backups/trading-supervisor/supervisor_YYYYMMDDTHHMMSSZ.dump [nombre_base]
set -euo pipefail

DUMP="${1:?Indica el archivo .dump}"
DB_NAME="${2:-supervisor_restore_$(date -u +%Y%m%d%H%M%S)}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT/deploy/.env"
set -a; source "$ENV_FILE"; set +a
COMPOSE=(docker compose -f "$ROOT/deploy/docker-compose.yml" --env-file "$ENV_FILE")
USER_="${POSTGRES_USER:-supervisor}"

"${COMPOSE[@]}" exec -T postgres createdb -U "$USER_" "$DB_NAME"
"${COMPOSE[@]}" exec -T postgres pg_restore -U "$USER_" -d "$DB_NAME" --no-owner < "$DUMP"
TABLES=$("${COMPOSE[@]}" exec -T postgres psql -U "$USER_" -d "$DB_NAME" -tAc \
  "select count(*) from information_schema.tables where table_schema='public'")
echo "Restaurado en la base '$DB_NAME' ($TABLES tablas). Revísala y bórrala con:"
echo "  ${COMPOSE[*]} exec postgres dropdb -U $USER_ $DB_NAME"
