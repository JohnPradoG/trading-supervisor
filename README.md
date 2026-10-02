# Trading Supervisor

Supervisor independiente y **de solo lectura** para bots de trading en MT5: registra cada
operación con su vida completa, la analiza y permite comparar bots y versiones sin
sobrescribir nunca la historia. Arquitectura completa (Fase 1):
[Trading Supervisor · Fase 1: Arquitectura](https://claude.ai/code/artifact/f6bfc767-251d-469a-8b9b-d097bb0b2b6f).

## Estado

| Fase | Estado |
| --- | --- |
| 1 · Arquitectura | Aprobada |
| 2 · Base de datos | Hecha: 25 tablas (26 con `pattern_runs` de la fase 9), migraciones Alembic, reglas de inmutabilidad, backups |
| 3 · API | Hecha: ingesta idempotente, API keys por terminal, registro de bots, HTTPS con Caddy |
| 4 · Bridge MT5 (EA Monitor) | Hecha: EA de solo lectura con cola en disco, ver [mt5/README.md](mt5/README.md) |
| 5 · Registro de operaciones | Hecha: worker que convierte deals, SL/TP y fotos de posiciones en operaciones con riesgo, motivo de salida, MFE/MAE y reentradas |
| 6 · Análisis | **Hecha**: análisis post-operación versionado (hechos medidos separados de hipótesis no validadas) y estadísticas con intervalos de confianza y avisos de muestra pequeña |
| 7 · Dashboard | **Hecha (núcleo)**: resumen, operaciones con filtros, detalle con gráfico, bots y sistema en `/dashboard`. Pendiente: patrones, experimentos y alertas (llegan con sus fases) |
| 8 · Trading DNA | **Hecha**: condiciones de mercado al entrar (tendencia por timeframe, indicadores, estructura, liquidez, FVG, order blocks, volatilidad, tiempo; noticias NULL hasta tener calendario), versionadas y sin lookahead, ver [docs/trading-dna.md](docs/trading-dna.md) |
| 9 · Detección de patrones | **Hecha**: búsqueda de trampas (condiciones de entrada con expectativa negativa) y ventajas sobre el DNA y los hechos de la fase 6, con split cronológico congelado, FDR, validación y fuera de muestra evaluado una sola vez; página "Trampas" del dashboard e informe por versión, ver [Cómo se valida una trampa](#cómo-se-valida-una-trampa) |
| 10 · Laboratorio | Siguiente |

## Estructura

```
backend/             paquete Python `supervisor` + migraciones + pruebas
  supervisor/models/ modelos SQLAlchemy (identidad, bots, trading, análisis, investigación)
  supervisor/api/    rutas FastAPI (ingesta del EA, registro y consulta de operaciones)
  supervisor/services/ lógica de ingesta, registro, consulta, estadísticas y dashboard
  supervisor/worker/ worker de operaciones: raw_events -> trades + trade_events (+ análisis)
  supervisor/analytics/ análisis post-operación (hechos, reglas de hipótesis), Trading DNA
                     (indicadores, estructura) y métricas
  supervisor/dashboard/ dashboard web: rutas, sesión/CSRF, plantillas Jinja2, htmx y Chart.js
  supervisor/cli.py  alta de cuentas, terminales y API keys; reprocess; trade-summary;
                     analyze; dna; stats; patterns
  migrations/        0001 esquema inicial · 0002 reglas de inmutabilidad (triggers)
                     0003 columnas del worker (event_time, reintentos, estado vivo)
                     0004 análisis versionado (hash de entradas, FACT/HYPOTHESIS)
                     0005 Trading DNA versionado y catálogo de variables con tipos
                     0006 patrones: hipótesis con estado, pattern_runs, pruebas OOS únicas
  tests/             pruebas contra PostgreSQL real
docs/                trading-dna.md (definición de cada variable del DNA), guía del VPS
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
  `analysis_findings`, `trade_dna`, `feature_definitions`, `pattern_tests`, `pattern_runs` y
  `data_splits` no admiten UPDATE ni DELETE.
  `bots`, `deployments`, `trades`, `accounts` y `raw_events` no se pueden borrar. De
  `raw_events` solo cambian `status`, `attempts`, `processed_at`, `error` y
  `next_attempt_at`.
- **Hecho ≠ hipótesis:** un CHECK en `analysis_findings` impide que un FACT tenga confianza o
  soportes y que una HYPOTHESIS no tenga confianza, versión de regla y hechos de apoyo.
- **Asignación sin ambigüedad:** un magic number solo puede tener un despliegue activo por
  cuenta y símbolo.
- **Validaciones:** versión semver, enums cerrados, operación cerrada con hora de cierre, velas
  con high ≥ low, cortes de validación en orden temporal.
- **Fuera de muestra una sola vez:** un índice único en `pattern_tests` impide una segunda
  prueba TRAIN, VALIDATION u OOS de la misma hipótesis.
- **Sin lookahead en el DNA:** un CHECK impide guardar un DNA cuya última vela M1 termine
  después de la entrada.
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
docker compose logs migrate              # debe terminar en "Running upgrade 0004 -> 0005"
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
ts dna --trade <trade_id>                       # Trading DNA de una operación (lo calcula si falta)
ts dna --all                                    # recalcular todos (solo crea versiones si cambió algo)
ts stats --bot EA_Nasdaq_FVG_Retest --group-by version   # tabla de métricas
ts stats --symbol USTEC_x100 --from 2026-10-01 --group-by session,direction
ts stats --bot EA_Nasdaq_FVG_Retest --group-by dna:tendencia_h1_rel   # por una variable del DNA
ts patterns --version <bot_version_id>          # buscar/validar trampas e imprimir el informe
ts patterns --version <bot_version_id> --symbol USTEC_x100 --report-only   # solo el informe
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
| `GET /v1/trades/{trade_id}/analysis` | admin | Análisis vigente: `facts`, `hypotheses` (con `validation`: estado de su regla en la fase 9), `data_quality` y `history` (todas las versiones). `version=N` para una anterior, `include_inputs=true` para ver los datos usados |
| `GET /v1/trades/{trade_id}/dna` | admin | Trading DNA vigente por secciones, con el motivo de cada NULL, cobertura de velas y `history` (todas las versiones). `version=N` para una anterior |
| `GET /v1/dna/features` | admin | Catálogo de variables del DNA: tipo, timeframe, unidad, definición y política de NULL |
| `GET /v1/stats` | admin | Métricas de operaciones cerradas. Filtros: `bot_id`, `version_id`, `symbol`, `account_id`, `source`, `from`/`to` (hora de **cierre**). `group_by`: hasta 2 de `bot`, `version`, `symbol`, `direction`, `weekday`, `hour`, `session`, `timeframe`, `account`, `source`, `dna:<variable>`. Las operaciones sin bot van aparte en `unassigned` |
| `GET /v1/patterns` | admin | Trampas y ventajas, validadas primero. Filtros: `bot_id`, `version_id`, `symbol`, `status` (`PROPOSED`, `TESTING`, `VALIDATED`, `REJECTED`, `DECAYED`), `kind` (`trap`/`edge`). `label` = "trampa validada" solo si pasó fuera de muestra; si no, "candidata, no validada", "rechazada" o "caducada". Métricas por tramo: `entrenamiento`, `validacion`, `fuera_de_muestra`, `forward` |
| `GET /v1/patterns/{id}` | admin | Un patrón con todas sus pruebas, su split y la ejecución que lo propuso (candidatas probadas) |
| `GET /v1/bots/{bot_id}/versions/{version_id}/report` | admin | Informe de la versión (sección 10): ganadoras frente a perdedoras, peores y mejores condiciones, mejor combinación, condiciones que aumentan el drawdown (MAE en R) y reglas de la fase 6, con muestra, win rate, expectativa, PF e IC dentro y fuera de muestra. `symbol` opcional |
| `GET /v1/bots/{bot_id}/compare-versions` | admin | Métricas de cada versión del bot lado a lado, con avisos de muestra. Filtros: `symbol`, `account_id`, `source`, `from`/`to` |
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
| Trampas | Por versión de bot: primero las **trampas validadas** (fuera de muestra, entrenamiento y forward con n, expectativa en R e IC, win rate y PF), después las candidatas marcadas "candidata, no validada", cuántas candidatas se probaron y, con pocos datos, "Con N operaciones cerradas todavía no se puede validar ninguna trampa; se necesitan al menos 100…". El Resumen muestra las trampas activas |
| Operaciones | Abiertas y cerradas con filtros (bot o "sin asignar", versión, símbolo, estado, dirección, fechas de entrada) que se aplican sin recargar (HTMX), 50 por página, con neto, puntos, R y motivo de salida |
| Detalle | Todos los campos, historia de eventos, gráfico de cierres M1 de entrada − 30 min a cierre + 30 min con entrada, SL/TP inicial y final y cierre, MFE/MAE, notas de calidad de datos, el análisis (fase 6) y el Trading DNA por secciones ("sin dato" con su motivo y cobertura de velas por timeframe) |
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

## Trading DNA (fase 8)

Cada operación guarda las condiciones de mercado **al entrar** (`trade_dna`), calculadas solo
con velas cerradas antes de la entrada. Definición de cada variable:
[docs/trading-dna.md](docs/trading-dna.md).

- **Cuándo:** el worker lo calcula al crear la operación (la entrada ya se conoce) y si la
  entrada cambia al recomponer deals desordenados. El repaso de cada 5 minutos lo recalcula
  (operaciones de los últimos 7 días, como mucho 100 por pasada, `SUPERVISOR_DNA_MAX_PER_PASS`)
  si falta, si cambió la operación o el conjunto de variables, o si le faltaban velas y han
  llegado.
- **Versiones:** de solo inserción con `input_hash` (operación, velas usadas, noticias,
  parámetros y versión del conjunto de variables): repetir el cálculo no crea nada; más
  velas anteriores a la entrada crean `dna_version + 1` y la anterior queda como historia.
- **Secciones:** tendencia M1/M5/M15/H1/H4/D1 (y relativa a la dirección), EMA20/50/200 en
  ATR, RSI, ATR, ROC y ADX en el timeframe principal del bot, estructura (swings, HH/HL,
  BOS/CHOCH, rango), liquidez (barridos, máximos/mínimos iguales, día y sesión anteriores),
  FVG, order blocks, volatilidad (spread, rango reciente, percentil de ATR), tiempo y
  noticias (NULL "sin fuente de noticias" mientras no haya calendario).
- **NULL con motivo, nunca inventado:** sin velas suficientes, sin cobertura de M1 >= 80 % o
  con datos de más de 96 h antes de la entrada, la variable queda NULL con la explicación.
- **Límites del VPS:** H1 consulta como mucho 35 días de M1 (`SUPERVISOR_DNA_HISTORY_DAYS`)
  y de ahí salen H4 y D1; M1/M5/M15 solo las velas que necesitan. Para tener todo desde el
  primer día sube `BarsBackfillHours` del EA a **720**.

## Cómo se valida una trampa

Una **trampa** es una condición de entrada en la que un bot pierde de forma sistemática
(expectativa negativa en R). Encontrarlas es lo primero; las ventajas, lo segundo. Código:
`supervisor/analytics/patterns.py` (estadística pura, sin NumPy ni SciPy) y
`pattern_search.py` (datos, splits y persistencia).

1. **Alcance:** una versión de bot (nunca se mezclan versiones) y, opcional, un símbolo. Solo
   operaciones cerradas con riesgo conocido (R = neto / riesgo inicial).
2. **Split cronológico congelado:** con al menos 100 operaciones
   (`SUPERVISOR_PATTERNS_MIN_TRADES`) se ordenan por hora de cierre y se cortan sin barajar:
   60 % entrenamiento, 20 % validación, 20 % fuera de muestra
   (`SUPERVISOR_PATTERNS_TRAIN_FRACTION`, `..._VALIDATION_FRACTION`). El corte se guarda en
   `data_splits` (de solo inserción). Lo que cierra después es **forward**.
3. **Búsqueda solo en entrenamiento:** condiciones simples sobre las variables buscables del
   DNA, la dirección, la reentrada y los hechos previos a la entrada de la fase 6 (precio frente
   a EMA200/EMA50 en H1 y M15); las numéricas en cuartiles calculados **solo con
   entrenamiento**. Además pares (A y B) de variables distintas con muestra (como mucho 3000).
   Cada candidata necesita 30 operaciones y 30 en su complemento.
4. **Prueba y corrección:** test t de Welch (condición frente a complemento) y FDR de
   Benjamini-Hochberg a q = 0.05 sobre **todas** las candidatas probadas; el número total de
   candidatas generadas, probadas y sin muestra queda en `pattern_runs`. Las que pasan se
   guardan como hipótesis (`PROPOSED`) con su prueba de entrenamiento: trampa si la expectativa
   es menor que la del complemento, ventaja si es mayor.
5. **Validación:** mismo sentido, al menos 15 operaciones y el IC95 de la expectativa sin
   cruzar 0 (por debajo para una trampa). Si falla: `REJECTED`; si pasa: `TESTING`.
6. **Fuera de muestra, una sola vez:** el mismo criterio con al menos 15 operaciones:
   `VALIDATED` o `REJECTED`. Si aún no hay 15, se espera y se evalúa **una** vez con el tramo
   fuera de muestra más el forward. Un índice único impide repetirlo.
7. **Forward:** las validadas se comprueban con lo que cierra después; si con al menos 20
   operaciones la expectativa pasa a ser significativamente contraria, `DECAYED`.
8. **Repetición:** el worker repasa una vez al día (`SUPERVISOR_PATTERNS_INTERVAL_SECONDS`, como
   mucho 5 versiones por pasada) y solo repite la búsqueda con 20 operaciones cerradas nuevas
   (`SUPERVISOR_PATTERNS_NEW_TRADES`): split nuevo con todos los datos, sin duplicar hipótesis
   vivas. Sin datos nuevos no se vuelve a mirar el fuera de muestra.
9. **Reglas de la fase 6:** las que dependen solo de hechos previos a la entrada (contra
   tendencia, volatilidad, spread, reentrada, noticia) pasan por el mismo circuito con su propia
   familia FDR. Los hallazgos del análisis no se modifican: `GET /v1/trades/{id}/analysis` y el
   dashboard muestran el estado de la regla por consulta.

Solo lo que pasó fuera de muestra se llama **"trampa validada"**; todo lo demás se muestra
como "candidata, no validada", con cuántas candidatas se probaron. Las secciones descriptivas
del informe (ganadoras frente a perdedoras, condiciones que aumentan el MAE) se marcan como no
validadas.

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
- Agrupar por una variable del **Trading DNA** (fase 8): `group_by=dna:<variable>`.
  Categóricas y booleanas por valor; numéricas por tramos fijos (RSI, ADX, volatilidad
  relativa) o cuartiles de las operaciones del filtro. Sin DNA o NULL = "sin dato". Agrupar
  no es validar: la fase 9 contrasta los patrones fuera de muestra.

## Desarrollo y pruebas

```bash
cd backend
python3 -m pip install -e ".[dev]"
# Un PostgreSQL 16 donde el usuario pueda crear bases de datos:
export SUPERVISOR_TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:5432/postgres
export SUPERVISOR_DATABASE_URL=$SUPERVISOR_TEST_DATABASE_URL
export SUPERVISOR_ADMIN_TOKEN=$(openssl rand -hex 32)
python3 -m pytest          # pruebas: esquema, inmutabilidad, API, EA, worker, análisis, métricas, dashboard
python -m supervisor.worker.main   # el worker en local (Ctrl+C para pararlo)
# El dashboard en local (http://127.0.0.1:8000/dashboard; sin HTTPS hay que desactivar Secure):
SUPERVISOR_DASHBOARD_COOKIE_SECURE=false uvicorn supervisor.main:create_app --factory
python3 -m ruff check . && python3 -m ruff format --check .
```

Cambios de esquema: editar los modelos y generar una migración nueva con
`alembic revision --autogenerate -m "descripcion"`, revisarla y añadirle pruebas. Nunca editar
una migración ya aplicada en producción.
