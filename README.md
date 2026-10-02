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
| 6 · Análisis | **Hecha**: análisis post-operación versionado (hechos medidos separados de hipótesis no validadas) y estadísticas con intervalos de confianza y avisos de muestra pequeña |
| 7 · Dashboard | Siguiente |

## Estructura

```
backend/             paquete Python `supervisor` + migraciones + pruebas
  supervisor/models/ modelos SQLAlchemy (identidad, bots, trading, análisis, investigación)
  supervisor/api/    rutas FastAPI (ingesta del EA, registro y consulta de operaciones)
  supervisor/services/ lógica de ingesta, registro, consulta y estadísticas
  supervisor/worker/ worker de operaciones: raw_events -> trades + trade_events (+ análisis)
  supervisor/analytics/ análisis post-operación (hechos, reglas de hipótesis) y métricas
  supervisor/cli.py  alta de cuentas, terminales y API keys; reprocess; trade-summary;
                     analyze; stats
  migrations/        0001 esquema inicial · 0002 reglas de inmutabilidad (triggers)
                     0003 columnas del worker (event_time, reintentos, estado vivo)
                     0004 análisis versionado (hash de entradas, FACT/HYPOTHESIS)
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
- **Hecho ≠ hipótesis:** un CHECK en `analysis_findings` impide que un FACT tenga confianza o
  soportes y que una HYPOTHESIS no tenga confianza, versión de regla y hechos de apoyo.
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
docker compose logs migrate              # debe terminar en "Running upgrade 0003 -> 0004"
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
ts analyze --trade <trade_id>                   # re-analizar una operación cerrada
ts analyze --all-closed                         # re-analizar todas (solo crea versiones si cambió algo)
ts stats --bot EA_Nasdaq_FVG_Retest --group-by version   # tabla de métricas
ts stats --symbol USTEC_x100 --from 2026-10-01 --group-by session,direction
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
| `GET /v1/trades/{trade_id}/analysis` | admin | Análisis vigente: `facts`, `hypotheses`, `data_quality` y `history` (todas las versiones). `version=N` para una anterior, `include_inputs=true` para ver los datos usados |
| `GET /v1/stats` | admin | Métricas de operaciones cerradas. Filtros: `bot_id`, `version_id`, `symbol`, `account_id`, `source`, `from`/`to` (hora de **cierre**). `group_by`: hasta 2 de `bot`, `version`, `symbol`, `direction`, `weekday`, `hour`, `session`, `timeframe`, `account`, `source`. Las operaciones sin bot van aparte en `unassigned` |
| `GET /v1/bots/{bot_id}/compare-versions` | admin | Métricas de cada versión del bot lado a lado, con avisos de muestra. Filtros: `symbol`, `account_id`, `source`, `from`/`to` |

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

## Análisis post-operación (fase 6)

Al cerrarse una operación el worker genera su análisis (`trade_analyses` + `analysis_findings`),
siempre separado en tres partes:

- **FACT (hecho):** solo datos medidos, con sus valores en `evidence` (JSON). Resultado y R
  (neto / riesgo), duración, motivo de salida, día/hora/sesión, MFE y MAE en puntos y en R,
  MFE frente a la distancia al TP, MAE frente a la distancia al SL, qué parte del MFE se
  capturó, si y cuándo se alcanzó el TP, SL movido a breakeven, cierres parciales, aumentos,
  reentrada tras SL, spread de la vela M1 de entrada, precio frente a EMA50/EMA200 de M15 y H1
  y percentil de la ATR(14) H1 frente a los 20 días anteriores.
- **HYPOTHESIS (hipótesis):** redactada como factor posible ("pudo haber..."), enlazada a los
  hechos que la apoyan (`supported_by`) y con confianza **`no_validada`** hasta que la fase 9
  la contraste fuera de muestra. Salen de reglas declarativas y versionadas
  (`supervisor/analytics/rules.py`): contra tendencia H1/M15, volatilidad alta (percentil
  ≥ 80), SL/TP posiblemente mal ubicados (MAE ≥ 90 % del SL con MFE ≥ 50 % del TP), pérdida
  que llegó a +1 R, ganadora que devolvió el MFE, TP tocado sin ganar, spread alto, reentrada
  precipitada, noticia cercana. La base de datos impide que un FACT lleve confianza o que una
  hipótesis no tenga hechos de apoyo (CHECK).
- **data_quality:** lo que no se pudo analizar y por qué. Nunca se adivina: sin SL no hay R,
  sin velas no hay MFE ni EMAs, y sin calendario de noticias se anota "sin datos de noticias"
  (no "no había noticia").

Reglas y convenciones:

- **Resultado:** `BREAKEVEN` si |neto| ≤ 5 % del riesgo inicial
  (`SUPERVISOR_BREAKEVEN_R_FRACTION`); sin riesgo, si |neto| ≤ |comisión|. Si no, `WIN` o
  `LOSS` por el signo del neto. Las estadísticas usan la misma regla.
- **Sesiones (UTC fijo, sin horario de verano):** Asia 00-09, Londres 07-16, Nueva York
  12-21. Para agrupar: `ASIA` 00-07, `LONDRES` 07-12, `LONDRES_NY` 12-16, `NUEVA_YORK` 16-21,
  `FUERA_SESION` 21-24, por la hora de entrada.
- **EMAs y ATR desde M1, sin lookahead:** las velas M15/H1 se agregan en PostgreSQL desde las
  M1 y solo cuentan las cerradas antes de la entrada. EMA sembrada con la media simple y
  exigiendo 2 × periodo velas (400 horas para la EMA200 H1) con ≥ 80 % de cobertura de M1;
  ATR de Wilder. El EA solo envía 24 h de velas al arrancar: sube `BarsBackfillHours` a 720
  para tener estos hechos desde el primer día.
- **Versiones e idempotencia:** el análisis guarda un `input_hash` (sha256 de operación,
  eventos, velas usadas, noticias, parámetros, versión del analizador y de las reglas). Solo se
  crea una versión nueva (`analysis_version` + 1, de solo inserción) si ese hash cambia; la
  vigente es la más alta y las anteriores quedan como historia. El repaso de cada 5 minutos
  re-analiza las operaciones de los últimos 7 días si cambió la operación, los parámetros o el
  analizador, o si faltaban velas y han llegado (como mucho 200 por pasada).

## Estadísticas

`GET /v1/stats`, `compare-versions` y `ts stats` calculan sobre operaciones **cerradas**:
número de operaciones, win/loss/breakeven rate, profit factor, expectancy (dinero y R), ganancia
y pérdida media y máxima, duración media, máximo drawdown (dinero y %) sobre la curva de neto
acumulado por hora de cierre, rachas máximas, Sharpe y Sortino, retorno acumulado (dinero, % y
R) e histogramas de R y de neto.

Salvaguardas contra conclusiones con poca muestra (sección 11):

- **Aviso de muestra** en cada grupo con menos de 30 operaciones (`SUPERVISOR_STATS_MIN_SAMPLE`).
- **Intervalos al 95 %:** win rate con Wilson; expectancy (dinero y R) con t de Student. Si los
  intervalos de dos versiones se solapan, la diferencia no está demostrada.
- **Sharpe y Sortino** por operación, sin anualizar, sobre R (o % del balance si falta el
  riesgo), y solo con n ≥ 30 y dispersión > 0; si no, `null` con el motivo.
- **Profit factor** sin pérdidas = infinito: se devuelve `null` con `profit_factor_note`.
- **Drawdown %** solo si todas las operaciones son de una cuenta y hay balance al entrar.
- Las operaciones **sin despliegue** nunca se mezclan con un bot: van en `unassigned`.
- **Límites del VPS:** la consulta carga solo columnas, como mucho 50 000 operaciones
  (`SUPERVISOR_STATS_MAX_TRADES`; si el filtro abarca más, pide acotarlo). Las métricas son
  Python puro (sin NumPy). El análisis agrega las velas en PostgreSQL y a Python llegan cientos
  de velas por operación.
- Agrupar por condiciones del **Trading DNA** llegará con la fase 8 (`group_by=dna` responde
  que aún no está disponible).

## Desarrollo y pruebas

```bash
cd backend
python3 -m pip install -e ".[dev]"
# Un PostgreSQL 16 donde el usuario pueda crear bases de datos:
export SUPERVISOR_TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:5432/postgres
export SUPERVISOR_DATABASE_URL=$SUPERVISOR_TEST_DATABASE_URL
export SUPERVISOR_ADMIN_TOKEN=$(openssl rand -hex 32)
python3 -m pytest          # 126 pruebas: esquema, inmutabilidad, API, EA, worker, análisis, métricas
python -m supervisor.worker.main   # el worker en local (Ctrl+C para pararlo)
python3 -m ruff check . && python3 -m ruff format --check .
```

Cambios de esquema: editar los modelos y generar una migración nueva con
`alembic revision --autogenerate -m "descripcion"`, revisarla y añadirle pruebas. Nunca editar
una migración ya aplicada en producción.
