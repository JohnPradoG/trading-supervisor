"""Trading DNA (fase 8): catálogo de variables y cálculo puro a partir de velas cerradas.

`build_dna(inputs)` es una función pura: recibe la operación (dirección, hora y precio de
entrada) y las velas cerradas ANTES de la entrada por timeframe, y devuelve el valor de cada
variable del catálogo `FEATURES`, el motivo de cada NULL y la cobertura de velas. Nunca
inventa un valor: si faltan velas, o la variable no aplica (p. ej. no hay ningún FVG cerca),
queda None con su motivo.

Timeframes y velas:
- M1, M5, M15, H1, H4 y D1 (y el principal del bot si es otro, como M30) se agregan desde M1
  con velas ya cerradas: M1 con bar_time + 1 min <= entrada y cada vela agregada solo si su
  intervalo terminó antes de la entrada. H4 y D1 se agrupan desde H1 (horas UTC; el D1 es el
  día UTC, no el del servidor del broker).
- Exigencias (las mismas que el análisis de la fase 6): las N velas que necesita cada cálculo
  con cobertura de M1 >= SUPERVISOR_ANALYSIS_MIN_BAR_COVERAGE y la última terminada como mucho
  96 h antes de la entrada. Si no, NULL con el motivo.

Definiciones (los parámetros están en PARAMS y entran en el hash del DNA):
- Tendencia por timeframe: EMA rápida y lenta del cierre (EMA20/EMA50; en D1 EMA5/EMA10
  porque 100 días de M1 no caben en la ventana de historia) y pendiente de la lenta en las
  últimas 5 velas. ALCISTA si rápida > lenta y la lenta sube; BAJISTA si rápida < lenta y la
  lenta baja; si no, LATERAL. Se exigen warmup x periodo lento velas (100; 20 en D1).
  `_rel`: A_FAVOR si coincide con la dirección de la operación, EN_CONTRA si es la opuesta,
  LATERAL si no hay tendencia.
- Indicadores en el timeframe principal de la versión del bot (M15 si la operación no tiene
  bot o su timeframe no es uno de M1, M5, M15, M30, H1, H4, D1): EMA20/50/200, distancia del
  precio de entrada a cada EMA en ATR ((entrada - EMA) / ATR; `_rel` multiplicada por +1 en
  compras y -1 en ventas, positiva = precio del lado que favorece la operación), RSI(14) de
  Wilder (`_rel`: 100 - RSI en ventas), ATR(14) de Wilder, ROC(10) en %, ADX(14) de Wilder.
- Estructura (timeframe principal, últimas 100 velas): swings fractales de 2 velas,
  estructura HH_HL / LH_LL / RANGO, BOS y CHOCH en las últimas 20 velas, última ruptura,
  distancia al último swing en ATR y régimen de rango (ADX(14) < 20). Ver
  supervisor.analytics.structure.
- Liquidez: barrido de máximos/mínimos en las últimas 20 velas; máximos/mínimos iguales
  (tolerancia 0.1 ATR, últimas 50 velas, sin barrer); distancia al máximo y mínimo del día
  UTC anterior (última vela D1 con cobertura suficiente) y de la sesión anterior (bloques de
  la fase 6: ASIA, LONDRES, LONDRES_NY, NUEVA_YORK, FUERA_SESION), en ATR(14) de H1.
- FVG y order blocks: el más cercano a la entrada, sin rellenar / sin invalidar, formado en
  las últimas 50 velas y a menos de 3 ATR de la entrada.
- Volatilidad: ATR, spread de la última vela M1 cerrada antes de la entrada (como mucho 5 min
  antes; MT5 informa el spread de la vela en puntos), rango de las últimas 20 velas en ATR y
  volatilidad relativa = percentil de la ATR(14) H1 frente a los 20 días anteriores (la
  misma medida que la fase 6).
- Tiempo: día de la semana, hora UTC y sesión de la hora de entrada (sesiones de la fase 6).
- Noticias: NULL "sin fuente de noticias" mientras no haya calendario cargado (ninguna
  noticia en los 7 días alrededor de la entrada). Si lo hay: noticia a menos de 60 min,
  minutos hasta la más cercana (negativo = ya había pasado), su impacto y su título. Las
  noticias de calendario se publican de antemano, así que conocerlas no es lookahead.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from supervisor.analytics import structure as st
from supervisor.analytics.indicators import (
    Bar,
    adx_series,
    atr_series,
    ema_series,
    percentile_rank,
    roc,
    rsi_series,
)
from supervisor.analytics.sessions import SESSION_BLOCKS, WEEKDAYS, session_flags, session_label

FEATURE_SET_VERSION = 1

TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}
TREND_TIMEFRAMES = ("M1", "M5", "M15", "H1", "H4", "D1")
DEFAULT_MAIN_TF = "M15"
# Parámetros de las definiciones (cambiarlos = subir FEATURE_SET_VERSION).
PARAMS: dict[str, Any] = {
    "tendencia_emas": {tf: [20, 50] for tf in TREND_TIMEFRAMES[:-1]} | {"D1": [5, 10]},
    "tendencia_pendiente_velas": 5,
    "ema_periodos": [20, 50, 200],
    "rsi": 14,
    "atr": 14,
    "roc": 10,
    "adx": 14,
    "adx_rango": 20,
    "swing_n": 2,
    "estructura_velas": 100,
    "reciente_velas": 20,
    "iguales_tolerancia_atr": 0.1,
    "iguales_velas": 50,
    "zonas_velas": 50,
    "zonas_cerca_atr": 3.0,
    "fvg_minimo_atr": 0.1,
    "ob_busqueda_velas": 10,
    "rango_reciente_velas": 20,
    "spread_max_minutos": 5,
    "noticias_ventana_min": 60,
    "noticias_fuente_dias": 7,
    "dias_sesion_anterior": 3,
}
MAX_STALE = timedelta(hours=96)
NO_NEWS_SOURCE = "sin fuente de noticias"


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    group: str
    value_type: str  # numeric, boolean, categorical
    label: str
    formula: str
    timeframe: str | None = None
    unit: str | None = None
    searchable: bool = True
    available: bool = True
    version: int = 1
    categories: tuple[str, ...] = ()
    # Tramos fijos para agrupar (group_by=dna:...); sin ellos, cuartiles.
    bins: tuple[float, ...] = ()
    null_policy: str = "NULL si faltan velas cerradas suficientes antes de la entrada."


TREND_VALUES = ("ALCISTA", "BAJISTA", "LATERAL")
REL_VALUES = ("A_FAVOR", "EN_CONTRA", "LATERAL")
SIDE_VALUES = ("A_FAVOR", "EN_CONTRA", "NINGUNO")
MAIN = "timeframe principal del bot"


def _trend_specs() -> list[FeatureSpec]:
    out = []
    for tf in TREND_TIMEFRAMES:
        fast, slow = PARAMS["tendencia_emas"][tf]
        out.append(
            FeatureSpec(
                f"tendencia_{tf.lower()}",
                "tendencia",
                "categorical",
                f"Tendencia {tf}",
                f"EMA{fast} vs EMA{slow} de {tf} y pendiente de la EMA{slow} en 5 velas: "
                "ALCISTA, BAJISTA o LATERAL.",
                timeframe=tf,
                categories=TREND_VALUES,
            )
        )
        out.append(
            FeatureSpec(
                f"tendencia_{tf.lower()}_rel",
                "tendencia",
                "categorical",
                f"Tendencia {tf} respecto a la operación",
                f"A_FAVOR si la tendencia {tf} coincide con la dirección de la operación, "
                "EN_CONTRA si es la contraria, LATERAL si no hay tendencia.",
                timeframe=tf,
                categories=REL_VALUES,
            )
        )
    return out


FEATURES: tuple[FeatureSpec, ...] = (
    *_trend_specs(),
    # Indicadores -----------------------------------------------------------------------------
    FeatureSpec(
        "tf_indicadores",
        "indicadores",
        "categorical",
        "Timeframe de indicadores y estructura",
        "Timeframe principal de la versión del bot (M15 si no tiene bot o no se reconoce).",
        searchable=False,
        null_policy="Nunca NULL.",
    ),
    *(
        FeatureSpec(
            f"ema{p}",
            "indicadores",
            "numeric",
            f"EMA{p}",
            f"EMA{p} del cierre en el {MAIN}, sembrada con la media simple.",
            unit="precio",
            searchable=False,
            null_policy=f"NULL con menos de {2 * p} velas cerradas.",
        )
        for p in (20, 50, 200)
    ),
    *(
        FeatureSpec(
            f"ema{p}_dist_atr{suffix}",
            "indicadores",
            "numeric",
            f"Distancia a la EMA{p} en ATR" + (" (a favor +)" if suffix else ""),
            f"(entrada - EMA{p}) / ATR(14) en el {MAIN}"
            + (", x -1 en ventas: positiva = precio del lado favorable." if suffix else "."),
            unit="ATR",
            searchable=bool(suffix),
            null_policy=f"NULL sin EMA{p} o sin ATR(14).",
        )
        for p in (20, 50, 200)
        for suffix in ("", "_rel")
    ),
    FeatureSpec(
        "rsi14",
        "indicadores",
        "numeric",
        "RSI(14)",
        f"RSI(14) de Wilder en el {MAIN}.",
        bins=(30, 50, 70),
    ),
    FeatureSpec(
        "rsi14_rel",
        "indicadores",
        "numeric",
        "RSI(14) a favor",
        "RSI(14) en compras y 100 - RSI(14) en ventas: alto = impulso a favor.",
        bins=(30, 50, 70),
    ),
    FeatureSpec(
        "atr14",
        "indicadores",
        "numeric",
        "ATR(14)",
        f"ATR(14) de Wilder en el {MAIN}, en precio.",
        unit="precio",
        searchable=False,
    ),
    FeatureSpec(
        "atr14_puntos",
        "indicadores",
        "numeric",
        "ATR(14) en puntos",
        "ATR(14) / point del símbolo.",
        unit="puntos",
        searchable=False,
        null_policy="NULL sin ATR o sin point del símbolo.",
    ),
    FeatureSpec(
        "roc10",
        "indicadores",
        "numeric",
        "ROC(10) %",
        f"(cierre - cierre de hace 10 velas) / cierre de hace 10 velas x 100, {MAIN}.",
        unit="%",
        searchable=False,
    ),
    FeatureSpec(
        "roc10_rel",
        "indicadores",
        "numeric",
        "ROC(10) a favor %",
        "ROC(10) x -1 en ventas: positivo = el precio venía moviéndose a favor.",
        unit="%",
    ),
    FeatureSpec(
        "adx14",
        "indicadores",
        "numeric",
        "ADX(14)",
        f"ADX(14) de Wilder en el {MAIN}.",
        bins=(20, 25, 40),
        null_policy="NULL con menos de 43 velas cerradas.",
    ),
    # Estructura ------------------------------------------------------------------------------
    FeatureSpec(
        "estructura",
        "estructura",
        "categorical",
        "Estructura de mercado",
        "Dos últimos máximos y mínimos de swing (fractal de 2 velas, últimas 100 velas): "
        "HH_HL, LH_LL o RANGO.",
        categories=("HH_HL", "LH_LL", "RANGO"),
        null_policy="NULL con menos de 100 velas o menos de 2 máximos y 2 mínimos de swing.",
    ),
    FeatureSpec(
        "estructura_rel",
        "estructura",
        "categorical",
        "Estructura respecto a la operación",
        "A_FAVOR (HH_HL en compras, LH_LL en ventas), EN_CONTRA o LATERAL (RANGO).",
        categories=REL_VALUES,
        null_policy="NULL si no hay estructura.",
    ),
    FeatureSpec(
        "swing_alto",
        "estructura",
        "numeric",
        "Último máximo de swing",
        "Precio del último máximo de swing confirmado.",
        unit="precio",
        searchable=False,
    ),
    FeatureSpec(
        "swing_bajo",
        "estructura",
        "numeric",
        "Último mínimo de swing",
        "Precio del último mínimo de swing confirmado.",
        unit="precio",
        searchable=False,
    ),
    FeatureSpec(
        "dist_swing_objetivo_atr",
        "estructura",
        "numeric",
        "Distancia al swing en la dirección (ATR)",
        "Compras: (último máximo de swing - entrada) / ATR; ventas: (entrada - último mínimo) "
        "/ ATR. Negativa si la entrada ya lo superó.",
        unit="ATR",
    ),
    FeatureSpec(
        "dist_swing_proteccion_atr",
        "estructura",
        "numeric",
        "Distancia al swing de protección (ATR)",
        "Compras: (entrada - último mínimo de swing) / ATR; ventas: (último máximo - entrada) "
        "/ ATR.",
        unit="ATR",
    ),
    FeatureSpec(
        "bos_reciente",
        "estructura",
        "boolean",
        "BOS en las últimas 20 velas",
        "Hubo un BOS (ruptura en la dirección de la estructura previa) en las últimas 20 velas.",
    ),
    FeatureSpec(
        "choch_reciente",
        "estructura",
        "boolean",
        "CHOCH en las últimas 20 velas",
        "Hubo un CHOCH (ruptura contra la estructura previa) en las últimas 20 velas.",
    ),
    FeatureSpec(
        "ultima_ruptura",
        "estructura",
        "categorical",
        "Última ruptura (20 velas)",
        "BOS_ALCISTA, BOS_BAJISTA, CHOCH_ALCISTA, CHOCH_BAJISTA o NINGUNA en las últimas 20 velas.",
        searchable=False,
    ),
    FeatureSpec(
        "ultima_ruptura_rel",
        "estructura",
        "categorical",
        "Última ruptura respecto a la operación",
        "BOS_A_FAVOR, BOS_EN_CONTRA, CHOCH_A_FAVOR, CHOCH_EN_CONTRA o NINGUNA.",
        categories=("BOS_A_FAVOR", "BOS_EN_CONTRA", "CHOCH_A_FAVOR", "CHOCH_EN_CONTRA", "NINGUNA"),
    ),
    FeatureSpec(
        "es_rango",
        "estructura",
        "boolean",
        "Mercado en rango (ADX < 20)",
        "ADX(14) del timeframe principal < 20 (umbral clásico de Wilder).",
        null_policy="NULL sin ADX(14).",
    ),
    # Liquidez --------------------------------------------------------------------------------
    FeatureSpec(
        "barrido_maximos",
        "liquidez",
        "boolean",
        "Barrido de máximos (20 velas)",
        "Una vela superó el último máximo de swing y cerró por debajo, en las últimas 20 velas.",
        searchable=False,
    ),
    FeatureSpec(
        "barrido_minimos",
        "liquidez",
        "boolean",
        "Barrido de mínimos (20 velas)",
        "Una vela perforó el último mínimo de swing y cerró por encima, en las últimas 20 velas.",
        searchable=False,
    ),
    FeatureSpec(
        "barrido_rel",
        "liquidez",
        "categorical",
        "Barrido de liquidez respecto a la operación",
        "A_FAVOR (barrido de mínimos antes de comprar o de máximos antes de vender), "
        "EN_CONTRA (el opuesto), AMBOS o NINGUNO.",
        categories=("A_FAVOR", "EN_CONTRA", "AMBOS", "NINGUNO"),
    ),
    FeatureSpec(
        "maximos_iguales",
        "liquidez",
        "boolean",
        "Máximos iguales",
        "Dos máximos de swing a menos de 0.1 ATR en las últimas 50 velas, sin barrer.",
    ),
    FeatureSpec(
        "minimos_iguales",
        "liquidez",
        "boolean",
        "Mínimos iguales",
        "Dos mínimos de swing a menos de 0.1 ATR en las últimas 50 velas, sin barrer.",
    ),
    FeatureSpec(
        "dist_max_dia_anterior_atr",
        "liquidez",
        "numeric",
        "Distancia al máximo del día anterior (ATR H1)",
        "(máximo del día UTC anterior - entrada) / ATR(14) H1: negativa = por encima.",
        timeframe="D1",
        unit="ATR H1",
    ),
    FeatureSpec(
        "dist_min_dia_anterior_atr",
        "liquidez",
        "numeric",
        "Distancia al mínimo del día anterior (ATR H1)",
        "(entrada - mínimo del día UTC anterior) / ATR(14) H1: negativa = por debajo.",
        timeframe="D1",
        unit="ATR H1",
    ),
    FeatureSpec(
        "dist_max_dia_anterior_puntos",
        "liquidez",
        "numeric",
        "Distancia al máximo del día anterior (puntos)",
        "(máximo del día anterior - entrada) / point.",
        timeframe="D1",
        unit="puntos",
        searchable=False,
    ),
    FeatureSpec(
        "dist_min_dia_anterior_puntos",
        "liquidez",
        "numeric",
        "Distancia al mínimo del día anterior (puntos)",
        "(entrada - mínimo del día anterior) / point.",
        timeframe="D1",
        unit="puntos",
        searchable=False,
    ),
    FeatureSpec(
        "sesion_anterior",
        "liquidez",
        "categorical",
        "Sesión anterior",
        "Último bloque de sesión completo antes del de la entrada que tiene velas.",
        timeframe="H1",
        searchable=False,
    ),
    FeatureSpec(
        "dist_max_sesion_anterior_atr",
        "liquidez",
        "numeric",
        "Distancia al máximo de la sesión anterior (ATR H1)",
        "(máximo de la sesión anterior - entrada) / ATR(14) H1.",
        timeframe="H1",
        unit="ATR H1",
    ),
    FeatureSpec(
        "dist_min_sesion_anterior_atr",
        "liquidez",
        "numeric",
        "Distancia al mínimo de la sesión anterior (ATR H1)",
        "(entrada - mínimo de la sesión anterior) / ATR(14) H1.",
        timeframe="H1",
        unit="ATR H1",
    ),
    # FVG -------------------------------------------------------------------------------------
    FeatureSpec(
        "fvg_cercano",
        "fvg",
        "boolean",
        "FVG sin rellenar cerca",
        "Hay un FVG sin rellenar (>= 0.1 ATR, últimas 50 velas) a menos de 3 ATR de la entrada.",
    ),
    FeatureSpec(
        "fvg_direccion",
        "fvg",
        "categorical",
        "Dirección del FVG",
        "ALCISTA, BAJISTA o NINGUNO (el más cercano).",
        searchable=False,
    ),
    FeatureSpec(
        "fvg_rel",
        "fvg",
        "categorical",
        "FVG respecto a la operación",
        "A_FAVOR (FVG alcista en compras, bajista en ventas), EN_CONTRA o NINGUNO.",
        categories=SIDE_VALUES,
    ),
    FeatureSpec(
        "fvg_tamano_atr",
        "fvg",
        "numeric",
        "Tamaño del FVG (ATR)",
        "Altura del FVG más cercano / ATR.",
        unit="ATR",
        null_policy="NULL si no hay FVG cerca (no aplica) o faltan velas.",
    ),
    FeatureSpec(
        "fvg_distancia_atr",
        "fvg",
        "numeric",
        "Distancia al FVG (ATR)",
        "Distancia de la entrada al borde más cercano del FVG / ATR (0 = dentro).",
        unit="ATR",
        null_policy="NULL si no hay FVG cerca (no aplica) o faltan velas.",
    ),
    # Order blocks ----------------------------------------------------------------------------
    FeatureSpec(
        "ob_cercano",
        "order_blocks",
        "boolean",
        "Order block válido cerca",
        "Hay un order block sin invalidar (rupturas de las últimas 50 velas) a menos de 3 ATR.",
    ),
    FeatureSpec(
        "ob_direccion",
        "order_blocks",
        "categorical",
        "Dirección del order block",
        "ALCISTA, BAJISTA o NINGUNO (el más cercano).",
        searchable=False,
    ),
    FeatureSpec(
        "ob_rel",
        "order_blocks",
        "categorical",
        "Order block respecto a la operación",
        "A_FAVOR (alcista en compras, bajista en ventas), EN_CONTRA o NINGUNO.",
        categories=SIDE_VALUES,
    ),
    FeatureSpec(
        "ob_distancia_atr",
        "order_blocks",
        "numeric",
        "Distancia al order block (ATR)",
        "Distancia de la entrada a la zona del order block más cercano / ATR (0 = dentro).",
        unit="ATR",
        null_policy="NULL si no hay order block cerca (no aplica) o faltan velas.",
    ),
    # Volatilidad -----------------------------------------------------------------------------
    FeatureSpec(
        "spread_puntos",
        "volatilidad",
        "numeric",
        "Spread al entrar (puntos)",
        "Spread de la última vela M1 cerrada antes de la entrada (máx. 5 min antes).",
        timeframe="M1",
        unit="puntos",
        null_policy="NULL sin vela M1 en los 5 minutos anteriores o sin spread.",
    ),
    FeatureSpec(
        "spread_atr",
        "volatilidad",
        "numeric",
        "Spread / ATR",
        "spread x point / ATR(14) del timeframe principal.",
        unit="ATR",
        null_policy="NULL sin spread, point o ATR.",
    ),
    FeatureSpec(
        "rango_reciente_atr",
        "volatilidad",
        "numeric",
        "Rango de las últimas 20 velas (ATR)",
        "(máximo - mínimo de las últimas 20 velas del timeframe principal) / ATR.",
        unit="ATR",
    ),
    FeatureSpec(
        "rango_reciente_puntos",
        "volatilidad",
        "numeric",
        "Rango de las últimas 20 velas (puntos)",
        "(máximo - mínimo de las últimas 20 velas) / point.",
        unit="puntos",
        searchable=False,
    ),
    FeatureSpec(
        "volatilidad_relativa",
        "volatilidad",
        "numeric",
        "Volatilidad relativa (percentil ATR H1)",
        "Percentil de la ATR(14) H1 al entrar frente a las de los 20 días anteriores.",
        timeframe="H1",
        unit="percentil",
        bins=(20, 50, 80),
        null_policy="NULL con menos de 100 valores de ATR H1 en los 20 días anteriores.",
    ),
    # Tiempo ----------------------------------------------------------------------------------
    FeatureSpec(
        "dia_semana",
        "tiempo",
        "categorical",
        "Día de la semana",
        "Día de la semana de la entrada (UTC).",
        categories=WEEKDAYS,
        null_policy="Nunca NULL.",
    ),
    FeatureSpec(
        "hora_utc",
        "tiempo",
        "numeric",
        "Hora UTC",
        "Hora de la entrada en UTC (0-23).",
        unit="h",
        null_policy="Nunca NULL.",
    ),
    FeatureSpec(
        "sesion",
        "tiempo",
        "categorical",
        "Sesión",
        "ASIA 00-07, LONDRES 07-12, LONDRES_NY 12-16, NUEVA_YORK 16-21, FUERA_SESION 21-24 UTC.",
        null_policy="Nunca NULL.",
    ),
    *(
        FeatureSpec(
            f"sesion_{key}",
            "tiempo",
            "boolean",
            f"Sesión {name} activa",
            f"La entrada cae en la sesión {name} ({start:02d}-{end:02d} UTC).",
            searchable=False,
            null_policy="Nunca NULL.",
        )
        for key, name, start, end in (
            ("asia", "de Asia", 0, 9),
            ("londres", "de Londres", 7, 16),
            ("nueva_york", "de Nueva York", 12, 21),
        )
    ),
    # Noticias --------------------------------------------------------------------------------
    FeatureSpec(
        "noticia_cercana",
        "noticias",
        "boolean",
        "Noticia a menos de 60 min",
        "Hay una noticia de calendario a menos de 60 minutos de la entrada.",
        available=False,
        null_policy=f"NULL '{NO_NEWS_SOURCE}' mientras no haya calendario cargado.",
    ),
    FeatureSpec(
        "minutos_a_noticia",
        "noticias",
        "numeric",
        "Minutos a la noticia más cercana",
        "Minutos desde la entrada hasta la noticia más cercana (negativo = ya pasó).",
        unit="min",
        searchable=False,
        available=False,
        null_policy=f"NULL '{NO_NEWS_SOURCE}' o si no hay noticia a menos de 60 minutos.",
    ),
    FeatureSpec(
        "impacto_noticia",
        "noticias",
        "categorical",
        "Impacto de la noticia",
        "Impacto de la noticia más cercana (a menos de 60 minutos).",
        available=False,
        null_policy=f"NULL '{NO_NEWS_SOURCE}' o si no hay noticia a menos de 60 minutos.",
    ),
    FeatureSpec(
        "tipo_noticia",
        "noticias",
        "categorical",
        "Noticia",
        "Título de la noticia más cercana (a menos de 60 minutos).",
        searchable=False,
        available=False,
        null_policy=f"NULL '{NO_NEWS_SOURCE}' o si no hay noticia a menos de 60 minutos.",
    ),
)
FEATURES_BY_NAME = {f.name: f for f in FEATURES}
GROUPS = (
    ("tendencia", "Tendencia"),
    ("indicadores", "Indicadores"),
    ("estructura", "Estructura"),
    ("liquidez", "Liquidez"),
    ("fvg", "FVG"),
    ("order_blocks", "Order blocks"),
    ("volatilidad", "Volatilidad"),
    ("tiempo", "Tiempo"),
    ("noticias", "Noticias"),
)


# Entradas ------------------------------------------------------------------------------------


@dataclass
class DnaInputs:
    direction: str  # BUY / SELL
    entry_time: datetime
    entry_price: float
    point: float | None
    main_tf: str
    main_tf_note: str | None
    bars: dict[str, list[Bar]]  # velas cerradas por timeframe
    cutoffs: dict[str, datetime]  # inicio de la vela de cada timeframe que contiene la entrada
    spread: dict[str, Any] | None  # {"puntos": int | None, "vela": iso} o None
    news: list[dict[str, Any]] | None  # None = sin fuente de noticias
    min_coverage: float = 0.8
    warmup: float = 2.0
    volatility_days: int = 20
    volatility_min_samples: int = 100
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class DnaResult:
    features: dict[str, Any]
    null_reasons: dict[str, str]
    quality: dict[str, Any]


class _Out:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.reasons: dict[str, str] = {}

    def set(self, name: str, value: Any) -> None:
        if name not in FEATURES_BY_NAME:
            raise KeyError(f"variable no registrada: {name}")
        if isinstance(value, float):
            value = round(value, 6)
        self.values[name] = value

    def null(self, reason: str, *names: str) -> None:
        for name in names:
            self.set(name, None)
            self.reasons[name] = reason


def _coverage(bars: Sequence[Bar], minutes: int) -> float:
    return sum(b.m1_count for b in bars) / (len(bars) * minutes) if bars else 0.0


def _problem(
    bars: Sequence[Bar], needed: int, tf: str, cutoff: datetime, min_coverage: float
) -> str | None:
    minutes = TF_MINUTES[tf]
    if len(bars) < needed:
        return f"{len(bars)} velas {tf} cerradas antes de la entrada; hacen falta {needed}"
    coverage = _coverage(bars[-needed:], minutes)
    if coverage < min_coverage:
        return (
            f"cobertura de velas M1 {coverage:.0%} en las últimas {needed} velas {tf} "
            f"(mínimo {min_coverage:.0%})"
        )
    last_end = bars[-1].time + timedelta(minutes=minutes)
    if cutoff - last_end > MAX_STALE:
        return f"la última vela {tf} cerrada termina {last_end.isoformat()}, demasiado antes"
    return None


def trend_label(closes: Sequence[float], fast: int, slow: int, slope_bars: int) -> str:
    fast_ema = ema_series(closes, fast)[-1]
    slow_series = ema_series(closes, slow)
    now, before = slow_series[-1], slow_series[-1 - slope_bars]
    if fast_ema is None or now is None or before is None:
        raise ValueError("velas insuficientes para la tendencia")
    if fast_ema > now and now > before:
        return "ALCISTA"
    if fast_ema < now and now < before:
        return "BAJISTA"
    return "LATERAL"


def _rel(sign: int, direction: int | None, flat: str = "LATERAL") -> str:
    if direction is None or direction == 0:
        return flat
    return "A_FAVOR" if direction == sign else "EN_CONTRA"


_TREND_DIR = {"ALCISTA": 1, "BAJISTA": -1, "LATERAL": 0}
_DIR_NAME = {st.UP: "ALCISTA", st.DOWN: "BAJISTA"}


def needed_bars(tf: str, main_tf: str, warmup: float) -> int:
    """Velas cerradas que pide cada timeframe (para acotar las consultas)."""
    need = 0
    if tf in TREND_TIMEFRAMES:
        need = math.ceil(PARAMS["tendencia_emas"][tf][1] * warmup)
    if tf == main_tf:
        need = max(need, math.ceil(max(PARAMS["ema_periodos"]) * warmup))
    return need


def build_dna(inp: DnaInputs) -> DnaResult:
    out = _Out()
    sign = 1 if inp.direction == "BUY" else -1
    entry = inp.entry_price
    quality: dict[str, Any] = {"timeframes": {}}
    for tf, bars in inp.bars.items():
        quality["timeframes"][tf] = {
            "velas": len(bars),
            "cobertura": round(_coverage(bars, TF_MINUTES[tf]), 4) if bars else None,
            "primera": bars[0].time.isoformat() if bars else None,
            "ultima": bars[-1].time.isoformat() if bars else None,
            "corte": inp.cutoffs[tf].isoformat(),
        }
    if inp.main_tf_note:
        quality["timeframe_principal"] = inp.main_tf_note

    # Tendencia -------------------------------------------------------------------------------
    for tf in TREND_TIMEFRAMES:
        fast, slow = PARAMS["tendencia_emas"][tf]
        bars = inp.bars.get(tf, [])
        name = f"tendencia_{tf.lower()}"
        reason = _problem(bars, math.ceil(slow * inp.warmup), tf, inp.cutoffs[tf], inp.min_coverage)
        if reason:
            out.null(reason, name, name + "_rel")
            continue
        label = trend_label(
            [b.close for b in bars], fast, slow, PARAMS["tendencia_pendiente_velas"]
        )
        out.set(name, label)
        out.set(name + "_rel", _rel(sign, _TREND_DIR[label]))

    # Indicadores en el timeframe principal ---------------------------------------------------
    tf = inp.main_tf
    bars = inp.bars.get(tf, [])
    cutoff = inp.cutoffs[tf]
    closes = [b.close for b in bars]
    out.set("tf_indicadores", tf)

    def problem(needed: int) -> str | None:
        return _problem(bars, needed, tf, cutoff, inp.min_coverage)

    atr_period = PARAMS["atr"]
    atr_reason = problem(math.ceil(atr_period * inp.warmup) + 1)
    atr = None if atr_reason else atr_series(bars, atr_period)[-1]
    if atr is not None and atr <= 0:
        atr, atr_reason = None, "ATR(14) = 0: velas sin rango"
    if atr is None:
        out.null(atr_reason or "sin ATR", "atr14", "atr14_puntos")
    else:
        out.set("atr14", atr)
        if inp.point:
            out.set("atr14_puntos", atr / inp.point)
        else:
            out.null("sin point del símbolo", "atr14_puntos")
    no_atr = f"sin ATR(14) del {MAIN}: {atr_reason}"

    for period in PARAMS["ema_periodos"]:
        reason = problem(math.ceil(period * inp.warmup))
        names = (f"ema{period}", f"ema{period}_dist_atr", f"ema{period}_dist_atr_rel")
        if reason:
            out.null(reason, *names)
            continue
        value = ema_series(closes, period)[-1]
        out.set(names[0], value)
        if atr is None:
            out.null(no_atr, *names[1:])
        else:
            distance = (entry - value) / atr
            out.set(names[1], distance)
            out.set(names[2], distance * sign)

    rsi_period = PARAMS["rsi"]
    reason = problem(math.ceil(rsi_period * inp.warmup) + 1)
    if reason:
        out.null(reason, "rsi14", "rsi14_rel")
    else:
        value = rsi_series(closes, rsi_period)[-1]
        out.set("rsi14", value)
        out.set("rsi14_rel", value if sign == 1 else 100 - value)

    roc_period = PARAMS["roc"]
    reason = problem(roc_period + 1)
    value = None if reason else roc(closes, roc_period)
    if value is None:
        out.null(reason or "cierre de referencia 0", "roc10", "roc10_rel")
    else:
        out.set("roc10", value)
        out.set("roc10_rel", value * sign)

    adx_period = PARAMS["adx"]
    adx_reason = problem(3 * adx_period + 1)
    adx = None if adx_reason else adx_series(bars, adx_period)[-1].adx
    if adx is None:
        out.null(adx_reason or "sin ADX", "adx14", "es_rango")
    else:
        out.set("adx14", adx)
        out.set("es_rango", adx < PARAMS["adx_rango"])

    # Estructura, liquidez, FVG y order blocks ------------------------------------------------
    structure_names = (
        "estructura",
        "estructura_rel",
        "swing_alto",
        "swing_bajo",
        "dist_swing_objetivo_atr",
        "dist_swing_proteccion_atr",
        "bos_reciente",
        "choch_reciente",
        "ultima_ruptura",
        "ultima_ruptura_rel",
        "barrido_maximos",
        "barrido_minimos",
        "barrido_rel",
        "maximos_iguales",
        "minimos_iguales",
        "fvg_cercano",
        "fvg_direccion",
        "fvg_rel",
        "fvg_tamano_atr",
        "fvg_distancia_atr",
        "ob_cercano",
        "ob_direccion",
        "ob_rel",
        "ob_distancia_atr",
        "rango_reciente_atr",
        "rango_reciente_puntos",
    )
    window = PARAMS["estructura_velas"]
    reason = problem(window)
    if reason:
        out.null(reason, *structure_names)
    elif atr is None:
        out.null(no_atr, *structure_names)
    else:
        _structure(out, bars[-window:], entry, sign, atr, inp.point)

    # Volatilidad -----------------------------------------------------------------------------
    if inp.spread is None:
        out.null(
            f"sin vela M1 en los {PARAMS['spread_max_minutos']} minutos anteriores a la entrada",
            "spread_puntos",
            "spread_atr",
        )
    elif inp.spread.get("puntos") is None:
        out.null("la vela M1 anterior a la entrada no trae spread", "spread_puntos", "spread_atr")
    else:
        spread = inp.spread["puntos"]
        out.set("spread_puntos", spread)
        if atr is None:
            out.null(no_atr, "spread_atr")
        elif not inp.point:
            out.null("sin point del símbolo", "spread_atr")
        else:
            out.set("spread_atr", spread * inp.point / atr)

    h1 = inp.bars.get("H1", [])
    h1_cutoff = inp.cutoffs["H1"]
    h1_reason = _problem(
        h1, math.ceil(atr_period * inp.warmup) + 1, "H1", h1_cutoff, inp.min_coverage
    )
    h1_atrs = [] if h1_reason else atr_series(h1, atr_period)
    atr_h1 = h1_atrs[-1] if h1_atrs else None
    _relative_volatility(out, h1, h1_atrs, h1_cutoff, h1_reason, inp)

    # Día y sesión anteriores -----------------------------------------------------------------
    no_atr_h1 = f"sin ATR(14) H1: {h1_reason}"
    _previous_day(out, inp, entry, atr_h1, no_atr_h1)
    _previous_session(out, inp, entry, atr_h1, no_atr_h1)

    # Tiempo ----------------------------------------------------------------------------------
    when = inp.entry_time.astimezone(UTC)
    out.set("dia_semana", WEEKDAYS[when.weekday()])
    out.set("hora_utc", when.hour)
    out.set("sesion", session_label(when))
    flags = session_flags(when)
    out.set("sesion_asia", flags["asia"])
    out.set("sesion_londres", flags["londres"])
    out.set("sesion_nueva_york", flags["nueva_york"])

    # Noticias --------------------------------------------------------------------------------
    news_names = ("noticia_cercana", "minutos_a_noticia", "impacto_noticia", "tipo_noticia")
    if inp.news is None:
        out.null(NO_NEWS_SOURCE, *news_names)
    else:
        window_min = PARAMS["noticias_ventana_min"]
        near = [n for n in inp.news if abs(n["minutos"]) <= window_min]
        out.set("noticia_cercana", bool(near))
        if near:
            closest = min(near, key=lambda n: (abs(n["minutos"]), n["minutos"]))
            out.set("minutos_a_noticia", closest["minutos"])
            out.set("impacto_noticia", closest["impacto"])
            out.set("tipo_noticia", closest["titulo"][:64])
        else:
            out.null(
                f"no aplica: ninguna noticia a menos de {window_min} minutos",
                *news_names[1:],
            )

    missing = [f.name for f in FEATURES if f.name not in out.values]
    if missing:
        raise AssertionError(f"variables sin calcular: {missing}")  # pragma: no cover
    return DnaResult(out.values, out.reasons, quality)


def _structure(
    out: _Out, bars: Sequence[Bar], entry: float, sign: int, atr: float, point: float | None
) -> None:
    n = len(bars)
    scan = st.scan_structure(bars, PARAMS["swing_n"])
    recent = n - PARAMS["reciente_velas"]

    ms = st.market_structure(scan.swings)
    if ms is None:
        out.null(
            f"menos de 2 máximos y 2 mínimos de swing en las últimas {n} velas",
            "estructura",
            "estructura_rel",
        )
    else:
        out.set("estructura", ms)
        out.set("estructura_rel", _rel(sign, {"HH_HL": 1, "LH_LL": -1, "RANGO": 0}[ms]))

    highs = [s for s in scan.swings if s.kind == "HIGH"]
    lows = [s for s in scan.swings if s.kind == "LOW"]
    if highs:
        out.set("swing_alto", highs[-1].price)
    else:
        out.null(f"sin máximos de swing en las últimas {n} velas", "swing_alto")
    if lows:
        out.set("swing_bajo", lows[-1].price)
    else:
        out.null(f"sin mínimos de swing en las últimas {n} velas", "swing_bajo")
    target = highs[-1] if sign == 1 and highs else (lows[-1] if sign == -1 and lows else None)
    guard = lows[-1] if sign == 1 and lows else (highs[-1] if sign == -1 and highs else None)
    if target is None:
        out.null("sin swing en la dirección de la operación", "dist_swing_objetivo_atr")
    else:
        out.set("dist_swing_objetivo_atr", (target.price - entry) * sign / atr)
    if guard is None:
        out.null("sin swing de protección", "dist_swing_proteccion_atr")
    else:
        out.set("dist_swing_proteccion_atr", (entry - guard.price) * sign / atr)

    recent_breaks = [b for b in scan.breaks if b.index >= recent]
    out.set("bos_reciente", any(b.kind == "BOS" for b in recent_breaks))
    out.set("choch_reciente", any(b.kind == "CHOCH" for b in recent_breaks))
    if recent_breaks:
        last = recent_breaks[-1]
        out.set("ultima_ruptura", f"{last.kind}_{_DIR_NAME[last.direction]}")
        out.set("ultima_ruptura_rel", f"{last.kind}_{_rel(sign, last.direction)}")
    else:
        out.set("ultima_ruptura", "NINGUNA")
        out.set("ultima_ruptura_rel", "NINGUNA")

    sweeps = [s for s in scan.sweeps if s.index >= recent]
    up = any(s.direction == st.UP for s in sweeps)
    down = any(s.direction == st.DOWN for s in sweeps)
    out.set("barrido_maximos", up)
    out.set("barrido_minimos", down)
    favor, against = (down, up) if sign == 1 else (up, down)
    out.set(
        "barrido_rel",
        "AMBOS"
        if favor and against
        else "A_FAVOR"
        if favor
        else "EN_CONTRA"
        if against
        else "NINGUNO",
    )
    tolerance = PARAMS["iguales_tolerancia_atr"] * atr
    lookback = PARAMS["iguales_velas"]
    out.set("maximos_iguales", st.equal_levels(bars, scan.swings, "HIGH", tolerance, lookback))
    out.set("minimos_iguales", st.equal_levels(bars, scan.swings, "LOW", tolerance, lookback))

    near = PARAMS["zonas_cerca_atr"] * atr
    zones_back = PARAMS["zonas_velas"]
    fvgs = [
        z
        for z in st.open_fvgs(bars, zones_back, PARAMS["fvg_minimo_atr"] * atr)
        if z.distance(entry) <= near
    ]
    fvg = st.nearest_zone(fvgs, entry)
    out.set("fvg_cercano", fvg is not None)
    if fvg is None:
        out.set("fvg_direccion", "NINGUNO")
        out.set("fvg_rel", "NINGUNO")
        out.null(
            "no aplica: no hay FVG sin rellenar a menos de 3 ATR",
            "fvg_tamano_atr",
            "fvg_distancia_atr",
        )
    else:
        out.set("fvg_direccion", _DIR_NAME[fvg.direction])
        out.set("fvg_rel", _rel(sign, fvg.direction))
        out.set("fvg_tamano_atr", fvg.size / atr)
        out.set("fvg_distancia_atr", fvg.distance(entry) / atr)

    blocks = [
        z
        for z in st.order_blocks(bars, scan.breaks, zones_back, PARAMS["ob_busqueda_velas"])
        if z.distance(entry) <= near
    ]
    ob = st.nearest_zone(blocks, entry)
    out.set("ob_cercano", ob is not None)
    if ob is None:
        out.set("ob_direccion", "NINGUNO")
        out.set("ob_rel", "NINGUNO")
        out.null("no aplica: no hay order block válido a menos de 3 ATR", "ob_distancia_atr")
    else:
        out.set("ob_direccion", _DIR_NAME[ob.direction])
        out.set("ob_rel", _rel(sign, ob.direction))
        out.set("ob_distancia_atr", ob.distance(entry) / atr)

    last = bars[-PARAMS["rango_reciente_velas"] :]
    span = max(b.high for b in last) - min(b.low for b in last)
    out.set("rango_reciente_atr", span / atr)
    if point:
        out.set("rango_reciente_puntos", span / point)
    else:
        out.null("sin point del símbolo", "rango_reciente_puntos")


def _relative_volatility(
    out: _Out,
    h1: Sequence[Bar],
    atrs: Sequence[float | None],
    cutoff: datetime,
    reason: str | None,
    inp: DnaInputs,
) -> None:
    if reason or not atrs or atrs[-1] is None:
        out.null(f"sin ATR(14) H1: {reason}", "volatilidad_relativa")
        return
    since = cutoff - timedelta(days=inp.volatility_days)
    sample = [
        a for b, a in zip(h1[:-1], atrs[:-1], strict=True) if a is not None and b.time >= since
    ]
    if len(sample) < inp.volatility_min_samples:
        out.null(
            f"{len(sample)} valores de ATR H1 en los {inp.volatility_days} días anteriores; "
            f"hacen falta {inp.volatility_min_samples}",
            "volatilidad_relativa",
        )
        return
    out.set("volatilidad_relativa", percentile_rank(sample, atrs[-1]))


def _previous_day(
    out: _Out, inp: DnaInputs, entry: float, atr_h1: float | None, no_atr_h1: str
) -> None:
    names = (
        "dist_max_dia_anterior_atr",
        "dist_min_dia_anterior_atr",
        "dist_max_dia_anterior_puntos",
        "dist_min_dia_anterior_puntos",
    )
    days = inp.bars.get("D1", [])
    cutoff = inp.cutoffs["D1"]
    day = next(
        (
            b
            for b in reversed(days[-3:])
            if b.m1_count / TF_MINUTES["D1"] >= inp.min_coverage
            and cutoff - (b.time + timedelta(days=1)) <= MAX_STALE
        ),
        None,
    )
    if day is None:
        out.null(
            "sin un día UTC anterior con velas suficientes en las últimas 96 h "
            f"(cobertura mínima {inp.min_coverage:.0%})",
            *names,
        )
        return
    if atr_h1 is None:
        out.null(no_atr_h1, *names[:2])
    else:
        out.set(names[0], (day.high - entry) / atr_h1)
        out.set(names[1], (entry - day.low) / atr_h1)
    if inp.point:
        out.set(names[2], (day.high - entry) / inp.point)
        out.set(names[3], (entry - day.low) / inp.point)
    else:
        out.null("sin point del símbolo", *names[2:])


def _block_start(when: datetime) -> tuple[datetime, int]:
    midnight = when.replace(hour=0, minute=0, second=0, microsecond=0)
    for i, (start, end, _) in enumerate(SESSION_BLOCKS):
        if start <= when.hour < end:
            return midnight + timedelta(hours=start), i
    raise AssertionError("hora fuera de rango")  # pragma: no cover


def _previous_session(
    out: _Out, inp: DnaInputs, entry: float, atr_h1: float | None, no_atr_h1: str
) -> None:
    names = ("sesion_anterior", "dist_max_sesion_anterior_atr", "dist_min_sesion_anterior_atr")
    h1 = inp.bars.get("H1", [])
    start, index = _block_start(inp.entry_time.astimezone(UTC))
    limit = start - timedelta(days=PARAMS["dias_sesion_anterior"])
    end = start
    while start > limit:
        index -= 1
        day_start = start.replace(hour=0)
        if index < 0:
            index = len(SESSION_BLOCKS) - 1
            day_start -= timedelta(days=1)
        block_start = day_start + timedelta(hours=SESSION_BLOCKS[index][0])
        block_end = day_start + timedelta(hours=SESSION_BLOCKS[index][1])
        members = [b for b in h1 if block_start <= b.time < block_end]
        start, end = block_start, block_end
        if not members:
            continue
        coverage = sum(b.m1_count for b in members) / ((end - start).total_seconds() / 60)
        if coverage < inp.min_coverage:
            out.null(
                f"la sesión anterior ({SESSION_BLOCKS[index][2]} {start.isoformat()}) tiene "
                f"cobertura de M1 {coverage:.0%} (mínimo {inp.min_coverage:.0%})",
                *names,
            )
            return
        out.set(names[0], SESSION_BLOCKS[index][2])
        if atr_h1 is None:
            out.null(no_atr_h1, *names[1:])
        else:
            out.set(names[1], (max(b.high for b in members) - entry) / atr_h1)
            out.set(names[2], (entry - min(b.low for b in members)) / atr_h1)
        return
    out.null(
        f"sin velas H1 de ninguna sesión en los {PARAMS['dias_sesion_anterior']} días anteriores",
        *names,
    )
