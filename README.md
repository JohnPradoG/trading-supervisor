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
| 3 · API | Hecha: ingesta idempotente, API keys por terminal, registro de bots, HTTPS con Caddy |
| 4 · Bridge MT5 (EA Monitor) | Hecha: EA de solo lectura con cola en disco, ver [mt5/README.md](mt5/README.md) |
| 5 · Registro de operaciones | Hecha: worker que convierte deals, SL/TP y fotos de posiciones en operaciones con riesgo, motivo de salida, MFE/MAE y reentradas |
| 6 · Análisis por operación | En curso (rama `fase-6-analisis`) |
| 7 · Dashboard | **Hecha (núcleo)**: resumen, operaciones con filtros, detalle con gráfico, bots y sistema en `/dashboard`. Pendiente: Trading DNA, patrones, experimentos y alertas (llegan con sus fases) |

## Estructura

```
backend/             paquete Python `supervisor` + migraciones + pruebas
  supervisor/models/ modelos SQLAlchemy (identidad, bots, trading, análisis, investigación)
  supervisor/api/    rutas FastAPI (ingesta del EA, registro y consulta de operaciones)
  supervisor/services/ lógica de ingesta, registro y consulta (dashboard.py: consultas del dashboard)
  supervisor/worker/ worker de operaciones: raw_events -> trades + trade_events
  supervisor/dashboard/ dashboard web: rutas, sesión/CSRF, plantillas Jinja2, htmx y Chart.js
  supervisor/cli.py  alta de cuentas, terminales y API keys; reprocess; trade-summary
  migrations/        0001 esquema inicial · 0002 reglas de inmutabilidad (triggers)
                     0003 columnas del worker (event_time, reintentos, estado vivo)
  tests/             pruebas contra PostgreSQL real
deploy/              Dockerfile, docker-compose.yml, Caddyfile, .env.example
deploy/vps/          harden.sh (seguridad del VPS), install-docker.sh
scripts/             backup.sh, restore.sh
mt5/                 EA Monitor de solo lectura (SupervisorMonitor.mq5) y su guía
```

## Qué garantiza la base de datos (no solo el código)

- **Idempotencia:** `raw_events(terminal_id, idempotency_key)` es único; un evento reenviado
  por MT5 se descarta con `ON CONFLICT DO NOTHING`. `trades(account_id, position_id)` y
  `trade_events(trade_id, deal_ticket)` son únicos como segunda barrera.
- **Historia inmutable:** `bot_versions`, `trade_events`, `trade_analyses`,
  `analysis_findings`, `pattern_tests` y `data_splits` no admiten UPDATE ni DELETE.
  `bots`, `deployments`, `trades`, `accounts` y `raw_events` no se pueden borrar. De
  `raw_events` solo cambian `status`, `attempts`, `processed_at`, `error` y
  `next_attempt_at`.
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
docker compose up -d --build             # postgres, migrate, api, worker, caddy
docker compose logs migrate              # debe terminar en "Running upgrade 0002 -> 0003"
docker compose logs worker               # {"msg": "worker iniciado", ...}
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
ts trade-summary                                # operaciones abiertas/cerradas, cola del worker
ts reprocess [--terminal exness-win-01]         # reintentar los eventos FAILED
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
| `GET /v1/trades` | admin | Operaciones, más recientes primero. Filtros: `bot_id`, `version_id`, `symbol`, `status` (`OPEN`/`CLOSED`), `from`/`to` (hora de entrada, UTC), `limit` (≤ 500), `offset` |
| `GET /v1/trades/{trade_id}` | admin | Una operación con toda su historia (`events`) |
| `/dashboard` | navegador (sesión) | Dashboard web de solo lectura, ver [Dashboard](#dashboard) |

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

## Dashboard

Entra en **https://johntrading.duckdns.org/dashboard** (o `https://TU_DOMINIO/dashboard`) y
pega el valor de `SUPERVISOR_ADMIN_TOKEN` de `deploy/.env`:

```bash
grep SUPERVISOR_ADMIN_TOKEN /opt/trading-supervisor/deploy/.env | cut -d= -f2
```

| Página | Qué muestra |
| --- | --- |
| Resumen | Latido de cada terminal (online si < 2 min), cola del worker (PENDING/FAILED) y último evento procesado, balance y equity por cuenta, gráfico de balance/equity (24h/7d/30d, máximo 500 puntos), operaciones abiertas, cerradas hoy y en 7 días (número, neto, win rate) |
| Operaciones | Abiertas y cerradas con filtros (bot o "sin asignar", versión, símbolo, estado, dirección, fechas de entrada) que se aplican sin recargar (HTMX), 50 por página, con neto, puntos, R y motivo de salida |
| Detalle | Todos los campos, historia de eventos, gráfico de cierres M1 de entrada − 30 min a cierre + 30 min con entrada, SL/TP inicial y final y cierre, MFE/MAE y notas de calidad de datos. Hueco "Análisis" para la Fase 6 |
| Bots | Bots → versiones → despliegues, con operaciones, win rate, neto y profit factor por versión (las estadísticas completas llegan con la Fase 6). Solo lectura: el registro sigue por API/CLI |
| Sistema | Terminales y versión del EA, últimos 50 eventos FAILED con su error, operaciones sin despliegue agrupadas por cuenta + magic + símbolo (lo que falta registrar) |

Funciona en el móvil (tablas con desplazamiento horizontal) y es de solo lectura. **Todas las
horas están en UTC** (elegir zona horaria queda fuera de esta fase).

Seguridad:

- **Sesión:** cookie `ts_session` firmada con HMAC-SHA256 (clave derivada del token de
  administración), caduca a las 12 h (`SUPERVISOR_DASHBOARD_SESSION_HOURS`), `HttpOnly`,
  `Secure`, `SameSite=Strict`, `Path=/dashboard`. Sin sesión, toda página redirige al login y
  los JSON del dashboard responden 401. "Salir" borra la cookie; para invalidar **todas** las
  sesiones (p. ej. si pierdes el móvil) rota `SUPERVISOR_ADMIN_TOKEN` y reinicia la API
  (`docker compose up -d api worker`). El EA no se ve afectado: usa su propia API key.
- **CSRF:** los POST (login y logout) llevan un token sincronizado (el de la sesión firmada o,
  antes del login, una cookie de doble envío), además de SameSite=Strict y comprobación de
  `Sec-Fetch-Site`/`Origin`. Comparaciones en tiempo constante.
- **Fuerza bruta:** 5 fallos por IP en 15 min bloquean el login (configurable con
  `SUPERVISOR_DASHBOARD_LOGIN_MAX_FAILURES` y `..._WINDOW_SECONDS`). El contador está en
  memoria y es **por proceso**: con 2 workers de uvicorn el límite real es el doble y se
  reinicia al reiniciar la API.
- **CSP:** la aplicación envía `script-src 'self'; style-src 'self'` sin `unsafe-inline` ni
  `unsafe-eval`: no hay scripts ni estilos en línea, htmx y Chart.js se sirven desde
  `/dashboard/static/vendor` (htmx 2.0.4 y Chart.js 4.4.7, sin CDN) y los gráficos leen sus
  datos de `/dashboard/api/...`. Caddy envía `Referrer-Policy: same-origin` (con
  `no-referrer` los navegadores mandan `Origin: null` en los formularios propios).
- Las rutas `/v1` no cambian y siguen usando `Authorization: Bearer`.

## Worker de operaciones

El servicio `worker` (`supervisor-worker`) lee los `raw_events` pendientes y construye
`trades` (una fila por posición de MT5: cuenta + `position_id`) y `trade_events` (su historia,
de solo inserción):

- **Orden y transacciones:** procesa por la hora del hecho en MT5 (`event_time`), en lotes de
  50 con `FOR UPDATE SKIP LOCKED` y un savepoint por evento. Resultado: `PROCESSED`,
  `IGNORED` (modificación sin cambios, posterior al cierre o de una posición desconocida pasado
  el plazo; deals tras una inversión INOUT) o `FAILED` con el error. La foto de posiciones
  siempre queda `PROCESSED`; si trae posiciones sin operación lo anota en `error`
  (`aviso: ...`).
- **Deals:** `IN` abre (`TRADE_OPENED`) o aumenta (`POSITION_INCREASED`); `OUT`/`OUT_BY` con
  volumen restante es `PARTIAL_CLOSE` y el que lo deja a 0 es `TRADE_CLOSED`. Los números se
  recalculan siempre desde todos los deals: precio de cierre medio ponderado, beneficio bruto,
  comisión (entrada + salida + fee), swap, neto, puntos, duración y motivo de salida (`SL`,
  `TP`, `EXPERT`, `MANUAL` para CLIENT/MOBILE/WEB, `STOP_OUT`, `MIXED` si los deals de salida
  difieren, `UNKNOWN`).
- **Precio de entrada:** `entry_price` es el del primer deal (define SL inicial y riesgo). Con
  aumentos, el medio ponderado va a `data_quality.precio_medio_entrada` y es el que usan los
  puntos.
- **Fuera de orden:** un cierre que llega antes que su apertura (backfill) queda `PENDING`
  con reintentos espaciados (`next_attempt_at`, de 30 s a 15 min). Al llegar la apertura se
  reactiva al momento. Si pasan 6 h (`SUPERVISOR_WORKER_DEFER_MAX_SECONDS`) queda `FAILED`
  como `huérfano:` y aun así se reactiva solo si la apertura aparece más tarde.
- **INOUT (netting):** cierra el volumen abierto; el volumen reabierto conserva el mismo
  `position_id` en MT5 y no puede ser otra operación, así que se anota en
  `data_quality.inversion_inout` y los deals posteriores de esa posición quedan `IGNORED`.
- **Asignación:** despliegue activo de cuenta + magic + símbolo a la hora de entrada. Si no
  hay, `deployment_id` queda NULL con el motivo en `data_quality.asignacion`; si el despliegue
  se registra después, el repaso periódico lo asigna.
- **Riesgo:** `|entrada − SL| / tick_size × tick_value × volumen inicial`, y en % del balance
  de la foto de cuenta más cercana anterior a la entrada. Sin SL: NULL y `data_quality.riesgo`.
- **SL/TP:** `POSITION_MODIFY` genera `SL_MODIFIED` y/o `TP_MODIFIED` comparando con los
  niveles vigentes a esa hora. La foto de posiciones detecta cambios que el EA no vio
  (`is_inferred = true`) y marca operaciones abiertas que ya no aparecen
  (`data_quality.ausente_en_foto`). Nunca inventa operaciones.
- **Reentrada:** si la última operación cerrada del mismo bot (cuenta + símbolo + magic) cerró
  por SL menos de 30 min antes (`SUPERVISOR_REENTRY_WINDOW_MINUTES`), se enlaza
  `reentry_of_trade_id` y se añade un evento `REENTRY` inferido.
- **MFE/MAE:** al cerrar, con las velas M1 del broker y símbolo entre la vela de entrada y la
  de cierre. `mfe_points ≥ 0` y `mae_points ≤ 0` respecto a `entry_price` según la dirección;
  en dinero con el volumen inicial (como el riesgo). Si faltan velas calcula con las que hay y
  deja la cobertura y los huecos en `data_quality.excursion`; cada 5 min un repaso recalcula
  las de los últimos 7 días si llegan más velas.
- **Parada:** con `SIGTERM` termina el lote en curso y sale. Logs en JSON por stdout.

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
python3 -m pytest          # 85 pruebas: esquema, inmutabilidad, API, contrato con el EA, worker, dashboard
python -m supervisor.worker.main   # el worker en local (Ctrl+C para pararlo)
# El dashboard en local (http://127.0.0.1:8000/dashboard; sin HTTPS hay que desactivar Secure):
SUPERVISOR_DASHBOARD_COOKIE_SECURE=false uvicorn supervisor.main:create_app --factory
python3 -m ruff check . && python3 -m ruff format --check .
```

Cambios de esquema: editar los modelos y generar una migración nueva con
`alembic revision --autogenerate -m "descripcion"`, revisarla y añadirle pruebas. Nunca editar
una migración ya aplicada en producción.
