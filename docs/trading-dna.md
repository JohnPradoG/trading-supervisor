# Trading DNA (fase 8)

El Trading DNA es la foto de las condiciones de mercado **en el momento de entrar** en cada
operación. Se guarda en `trade_dna` (una fila por versión, de solo inserción) y su catálogo
de variables en `feature_definitions` (nombre, tipo, timeframe, definición, cuándo es NULL).
El código está en `backend/supervisor/analytics/dna_features.py` (definiciones, cálculo
puro), `structure.py` (swings, BOS/CHOCH, barridos, FVG, order blocks), `indicators.py`
(EMA, ATR, RSI, ROC, ADX) y `dna.py` (velas, versiones, repaso).

## Reglas que no se negocian

- **Sin lookahead.** Solo se usan velas M1 con `bar_time + 1 min <= entrada` y velas
  agregadas (M5, M15, M30, H1, H4, D1) cuyo intervalo terminó antes de la entrada. Una vela
  que llega después y cae en o tras la entrada no cambia nada. La base de datos lo comprueba
  para la última vela M1 usada (`CHECK no_lookahead`).
- **Nunca se inventa un dato.** Una variable que no se puede calcular vale `null` y su motivo
  queda en `null_reasons` (p. ej. "40 velas H4 cerradas antes de la entrada; hacen falta
  100", "sin fuente de noticias", "no aplica: no hay FVG sin rellenar a menos de 3 ATR").
- **Versionado.** `input_hash` = sha256 de la versión del conjunto de variables, sus
  parámetros, la operación (dirección, hora y precio de entrada, point, timeframe principal),
  todas las velas usadas, el spread y las noticias. Solo se crea una versión nueva
  (`dna_version + 1`) si el hash cambia: más velas anteriores a la entrada (backfill), otro
  timeframe principal (la operación se asignó después a un bot) o un conjunto de variables
  nuevo (`FEATURE_SET_VERSION`). Las versiones anteriores quedan como historia.

## Velas y exigencias

- Todo se agrega desde M1 en PostgreSQL. H4 y D1 salen de las velas H1 (horas UTC; el D1 es
  el día UTC, no el del servidor del broker).
- Cada cálculo exige sus N velas cerradas con cobertura de M1 >= 80 %
  (`SUPERVISOR_ANALYSIS_MIN_BAR_COVERAGE`) y que la última termine como mucho 96 h antes de
  la entrada. EMAs: `warmup x periodo` velas (2 x por defecto).
- **Límites del VPS:** M1, M5 y M15 consultan solo las velas que piden (unos 9 días para
  M15); H1 consulta `SUPERVISOR_DNA_HISTORY_DAYS` días (35). Con 35 días caben la tendencia
  H4 (100 velas) y D1 (20 velas) pero no la EMA200 de H4 o D1 (quedan NULL con el motivo).
- El EA solo envía 24 h de historia al arrancar: **sube `BarsBackfillHours` a 720** para
  tener todas las variables desde el primer día. Mientras faltan velas, el repaso del worker
  recalcula el DNA cuando llegan (operaciones de los últimos 7 días, 100 por pasada).

## Timeframe principal

Indicadores, estructura, liquidez local, FVG y order blocks se calculan en el timeframe
principal de la versión del bot (`main_timeframe`: M1, M5, M15, M30, H1, H4 o D1). Si la
operación no tiene bot o el timeframe no se reconoce, en M15 (y se anota en la calidad de
datos). `tf_indicadores` dice cuál se usó.

## Variables

"`_rel`" = relativa a la dirección de la operación: en ventas se invierte el signo (o
`100 - RSI`), de modo que positivo / `A_FAVOR` siempre significa "a favor de la operación".

| Sección | Variable | Tipo | Definición |
| --- | --- | --- | --- |
| Tendencia | `tendencia_m1` … `tendencia_d1` | categórica | EMA20 vs EMA50 del cierre (D1: EMA5 vs EMA10) y pendiente de la lenta en 5 velas: `ALCISTA` si rápida > lenta y la lenta sube, `BAJISTA` al revés, si no `LATERAL` |
| | `tendencia_<tf>_rel` | categórica | `A_FAVOR`, `EN_CONTRA` o `LATERAL` respecto a la dirección |
| Indicadores | `ema20`, `ema50`, `ema200` | numérica | EMA del cierre sembrada con la media simple |
| | `ema<p>_dist_atr` (+ `_rel`) | numérica | (entrada − EMA) / ATR(14) |
| | `rsi14` (+ `_rel`) | numérica | RSI(14) de Wilder |
| | `atr14`, `atr14_puntos` | numérica | ATR(14) de Wilder, en precio y en puntos |
| | `roc10` (+ `_rel`) | numérica | (cierre − cierre de hace 10) / cierre de hace 10 × 100 |
| | `adx14` | numérica | ADX(14) de Wilder (necesita 43 velas) |
| Estructura | `estructura` (+ `_rel`) | categórica | Swings fractales de 2 velas (alto estrictamente mayor que los 2 anteriores y los 2 posteriores; se conocen 2 velas después) en las últimas 100 velas: `HH_HL`, `LH_LL` o `RANGO` |
| | `swing_alto`, `swing_bajo` | numérica | Último máximo / mínimo de swing confirmado |
| | `dist_swing_objetivo_atr`, `dist_swing_proteccion_atr` | numérica | Distancia en ATR al swing en la dirección de la operación y al de protección |
| | `bos_reciente`, `choch_reciente` | booleana | Ruptura por cierre del último swing confirmado en las últimas 20 velas: BOS si va en la dirección de la ruptura anterior (o es la primera), CHOCH si va en contra |
| | `ultima_ruptura` (+ `_rel`) | categórica | `BOS_ALCISTA`, `CHOCH_BAJISTA`… / `BOS_A_FAVOR`, `CHOCH_EN_CONTRA`… o `NINGUNA` |
| | `es_rango` | booleana | ADX(14) < 20 |
| Liquidez | `barrido_maximos`, `barrido_minimos`, `barrido_rel` | booleana / categórica | Una vela pasa el último swing confirmado (no roto) y cierra de vuelta, en las últimas 20 velas. `A_FAVOR` = barrido de mínimos antes de comprar o de máximos antes de vender |
| | `maximos_iguales`, `minimos_iguales` | booleana | Dos swings a menos de 0.1 ATR en las últimas 50 velas que nadie ha barrido después |
| | `dist_max_dia_anterior_atr`, `dist_min_dia_anterior_atr` (+ `_puntos`) | numérica | Distancia al máximo/mínimo del último día UTC con cobertura suficiente, en ATR(14) H1 y en puntos |
| | `sesion_anterior`, `dist_max_sesion_anterior_atr`, `dist_min_sesion_anterior_atr` | categórica / numérica | Último bloque de sesión (ASIA, LONDRES, LONDRES_NY, NUEVA_YORK, FUERA_SESION) anterior al de la entrada con velas, y la distancia a su máximo/mínimo en ATR H1 |
| FVG | `fvg_cercano`, `fvg_direccion`, `fvg_rel`, `fvg_tamano_atr`, `fvg_distancia_atr` | | Hueco de 3 velas (bajo_i > alto_(i−2) alcista; alto_i < bajo_(i−2) bajista) de al menos 0.1 ATR, formado en las últimas 50 velas, sin rellenar (ninguna vela posterior llegó al borde lejano) y a menos de 3 ATR de la entrada; el más cercano. Distancia 0 = la entrada está dentro |
| Order blocks | `ob_cercano`, `ob_direccion`, `ob_rel`, `ob_distancia_atr` | | En cada BOS/CHOCH de las últimas 50 velas, la última vela de color contrario de las 10 anteriores a la vela que rompió; zona [bajo, alto]; inválido si después un cierre la cruza. El más cercano a menos de 3 ATR |
| Volatilidad | `spread_puntos`, `spread_atr` | numérica | Spread de la última vela M1 cerrada antes de la entrada (máx. 5 min antes), en puntos y en ATR |
| | `rango_reciente_atr`, `rango_reciente_puntos` | numérica | Máximo − mínimo de las últimas 20 velas |
| | `volatilidad_relativa` | numérica | Percentil de la ATR(14) H1 frente a las de los 20 días anteriores (como la fase 6) |
| Tiempo | `dia_semana`, `hora_utc`, `sesion`, `sesion_asia/londres/nueva_york` | | De la hora de entrada en UTC, con las sesiones de la fase 6 |
| Noticias | `noticia_cercana`, `minutos_a_noticia`, `impacto_noticia`, `tipo_noticia` | | NULL "sin fuente de noticias" mientras no haya calendario (ninguna fila en `news_events` en los 7 días alrededor de la entrada). Con calendario: noticia a ≤ 60 min, minutos hasta la más cercana (negativo = ya pasó), su impacto y título |

El catálogo completo, con unidades, tramos y política de NULL, está en
`GET /v1/dna/features`. Para la fase 9 solo son "buscables" las variables comparables entre
operaciones (no los precios absolutos como el nivel de una EMA).

## Dónde se ve

- `GET /v1/trades/{id}/dna` (y `?version=N`), `supervisor-cli dna --trade <id>`.
- `supervisor-cli dna --all` recalcula todas (solo crea versiones si algo cambió).
- Dashboard: sección "Trading DNA" del detalle de cada operación, con "sin dato" y el motivo.
- Estadísticas: `GET /v1/stats?group_by=dna:tendencia_h1_rel` o
  `ts stats --group-by dna:rsi14`. Categóricas y booleanas por valor; numéricas por sus
  tramos fijos (RSI 30/50/70, ADX 20/25/40, volatilidad relativa 20/50/80) o por cuartiles
  de las operaciones del filtro. Sin DNA o NULL = "sin dato".
