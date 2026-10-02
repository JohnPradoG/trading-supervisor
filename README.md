# Trading Supervisor

Supervisor independiente y **de solo lectura** para bots de trading en MT5: registra cada
operación con su vida completa, la analiza y permite comparar bots y versiones sin
sobrescribir nunca la historia. Arquitectura completa (Fase 1):
[Trading Supervisor · Fase 1: Arquitectura](https://claude.ai/code/artifact/f6bfc767-251d-469a-8b9b-d097bb0b2b6f).

## Estado

| Fase | Estado |
| --- | --- |
| 1 · Arquitectura | Aprobada |
| 2 · Base de datos | Hecha: 25 tablas, migraciones Alembic, reglas de inmutabilidad, backups |
| 3 · API | **Hecha**: ingesta idempotente, API keys por terminal, registro de bots, HTTPS con Caddy |
| 4 · Bridge MT5 (EA Monitor) | Siguiente |

## Estructura

```
backend/             paquete Python `supervisor` + migraciones + pruebas
  supervisor/models/ modelos SQLAlchemy (identidad, bots, trading, análisis, investigación)
  supervisor/api/    rutas FastAPI (ingesta del EA y registro)
  supervisor/services/ lógica de ingesta y registro
  supervisor/cli.py  alta de cuentas, terminales y API keys
  migrations/        0001 esquema inicial · 0002 reglas de inmutabilidad (triggers)
  tests/             pruebas contra PostgreSQL real
deploy/              Dockerfile, docker-compose.yml, Caddyfile, .env.example
deploy/vps/          harden.sh (seguridad del VPS), install-docker.sh
scripts/             backup.sh, restore.sh
mt5/                 EA Monitor (fase 4)
```

## Qué garantiza la base de datos (no solo el código)

- **Idempotencia:** `raw_events(terminal_id, idempotency_key)` es único; un evento reenviado
  por MT5 se descarta con `ON CONFLICT DO NOTHING`. `trades(account_id, position_id)` y
  `trade_events(trade_id, deal_ticket)` son únicos como segunda barrera.
- **Historia inmutable:** `bot_versions`, `trade_events`, `trade_analyses`,
  `analysis_findings`, `pattern_tests` y `data_splits` no admiten UPDATE ni DELETE.
  `bots`, `deployments`, `trades`, `accounts` y `raw_events` no se pueden borrar. De
  `raw_events` solo cambian `status`, `attempts`, `processed_at` y `error`.
- **Asignación sin ambigüedad:** un magic number solo puede tener un despliegue activo por
  cuenta y símbolo.
- **Validaciones:** versión semver, enums cerrados, operación cerrada con hora de cierre, velas
  con high ≥ low, cortes de validación en orden temporal.
- **UTC:** todas las fechas son `timestamptz` y las conexiones trabajan en UTC.

## Instalación en el VPS (Ubuntu 24.04)

```bash
# 0. Descargar el proyecto:
git clone https://github.com/JohnPradoG/trading-supervisor.git /opt/trading-supervisor
cd /opt/trading-supervisor
# 1. Seguridad, una vez (lee la cabecera del script antes):
sudo bash deploy/vps/harden.sh
passwd                                   # cambia la contraseña de root tú mismo
# 2. Docker:
sudo bash deploy/vps/install-docker.sh
# 3. Base de datos:
cd deploy && cp .env.example .env
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" .env
sed -i "s/^SUPERVISOR_ADMIN_TOKEN=.*/SUPERVISOR_ADMIN_TOKEN=$(openssl rand -hex 32)/" .env
nano .env                                # pon tu dominio en SUPERVISOR_DOMAIN
chmod 600 .env
docker compose up -d --build             # postgres, migrate, api, caddy
docker compose logs migrate              # debe terminar en "Running upgrade 0001 -> 0002"
curl https://TU_DOMINIO/health           # {"status":"ok","database":"up",...}
# 4. Backup diario a las 03:15 UTC:
( crontab -l 2>/dev/null; echo "15 3 * * * /opt/trading-supervisor/scripts/backup.sh >> /var/log/ts-backup.log 2>&1" ) | crontab -
```

## Dar de alta una cuenta y su terminal MT5

Las cuentas y las API keys se crean solo desde la CLI del VPS, nunca por HTTP:

```bash
cd /opt/trading-supervisor/deploy
alias ts='docker compose exec api supervisor-cli'
ts create-account --broker Exness --server <servidor MT5> --login <número de cuenta> \
   --currency USD --type REAL --margin-mode HEDGING
ts create-terminal --name exness-win-01 --server <servidor MT5> --login <número de cuenta>
ts create-api-key --terminal exness-win-01     # muestra la key UNA vez: pégala en el EA
ts list-terminals                               # última conexión de cada terminal
ts revoke-api-key --prefix <prefijo>            # si una key se filtra
```

## API

| Ruta | Quién | Para qué |
| --- | --- | --- |
| `GET /health` | cualquiera | Estado de la API y la base de datos |
| `POST /v1/ingest/events` | EA (`X-API-Key`) | Lote de eventos `DEAL`, `POSITION_MODIFY`, `POSITIONS_SNAPSHOT`. Responde `accepted`, `duplicate` o `rejected` por evento |
| `POST /v1/ingest/heartbeat` | EA | Latido del terminal |
| `POST /v1/ingest/account-snapshot` | EA | Balance, equity y margen |
| `POST /v1/ingest/bars` | EA | Velas M1 cerradas |
| `GET/POST /v1/bots`, `GET/PATCH /v1/bots/{id}` | admin (`Bearer`) | Bots |
| `POST /v1/bots/{id}/versions` | admin | Nueva versión (inmutable; repetir una versión da 409) |
| `POST /v1/deployments`, `POST /v1/deployments/{id}/end` | admin | Qué versión corre con qué magic en qué cuenta y símbolo |
| `GET /v1/raw-events` | admin | Últimos eventos recibidos |

No existe ninguna ruta que envíe órdenes a MT5. La clave de idempotencia la calcula el
servidor (`deal:<login>:<ticket>`, `mod:<login>:<posición>:<hora_ms>:<sl>:<tp>`), así que un
reenvío del EA nunca duplica datos. Un evento inválido se rechaza sin tumbar el resto del
lote. Las horas deben venir en UTC con zona horaria y no más de 5 minutos en el futuro.

Ejemplo de alta de un bot:

```bash
TOKEN=$(grep SUPERVISOR_ADMIN_TOKEN deploy/.env | cut -d= -f2)
curl -s -X POST https://TU_DOMINIO/v1/bots -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"name":"EA_Nasdaq_FVG_Retest","strategy":"Retesteo de FVG a favor de tendencia"}'
```

Un backup que solo vive en el VPS no protege si se pierde el VPS: copia
`/var/backups/trading-supervisor` a otro sitio (por ejemplo con `rclone` a Google Drive) y
prueba una restauración con `scripts/restore.sh <archivo.dump>`.

## Desarrollo y pruebas

```bash
cd backend
python3 -m pip install -e ".[dev]"
# Un PostgreSQL 16 donde el usuario pueda crear bases de datos:
export SUPERVISOR_TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:5432/postgres
export SUPERVISOR_DATABASE_URL=$SUPERVISOR_TEST_DATABASE_URL
export SUPERVISOR_ADMIN_TOKEN=$(openssl rand -hex 32)
python3 -m pytest          # 39 pruebas: esquema, inmutabilidad, API, autenticación, idempotencia
python3 -m ruff check . && python3 -m ruff format --check .
```

Cambios de esquema: editar los modelos y generar una migración nueva con
`alembic revision --autogenerate -m "descripcion"`, revisarla y añadirle pruebas. Nunca editar
una migración ya aplicada en producción.
