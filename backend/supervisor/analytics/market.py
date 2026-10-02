"""Contexto de mercado de una operación a partir de las velas M1 guardadas (price_bars).

Sin lookahead: las velas M15 y H1 se agregan en SQL desde M1 y solo se usan las que ya
estaban cerradas al entrar (empiezan antes del inicio de la vela que contiene la entrada).
Una vela agregada se forma con las M1 que existan en su intervalo (los fines de semana y
cierres de mercado simplemente no tienen velas, como en MT5).

Cuándo hay "suficientes" velas (si no, no se calcula nada y queda una nota de calidad):
- EMA(n): al menos warmup_factor x n velas agregadas cerradas, con una cobertura de M1 dentro
  de ellas >= analysis_min_bar_coverage, y la última terminada como mucho MAX_STALE antes de
  la entrada.
- ATR(14) H1 y su percentil: la ATR de la última vela H1 cerrada se compara con las ATR de las
  velas H1 de los analysis_volatility_lookback_days días anteriores; hacen falta al menos
  analysis_volatility_min_samples valores y la misma cobertura y antigüedad máximas.

Las velas se buscan en una ventana de calendario acotada (ver `_span`), así que la consulta
nunca recorre la tabla entera: con la configuración por defecto, ~27 días de M1 de un símbolo
(~39 000 filas, por la clave primaria) agregadas en PostgreSQL. A Python solo llegan cientos
de velas agregadas.

Con los valores por defecto la EMA200 H1 necesita 400 horas de velas (~17 días de mercado): el
EA solo envía 24 h de historia al arrancar (BarsBackfillHours), así que conviene subirlo a 720
para tener estos hechos desde el primer día. Mientras falten, el análisis lo dice.
"""

import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from supervisor.analytics.indicators import Bar, atr_series, ema_series, percentile_rank
from supervisor.config import Settings
from supervisor.models import NewsEvent, PriceBar

TIMEFRAMES = {"M15": 15, "H1": 60}
EMA_SPECS = (("H1", 200), ("H1", 50), ("M15", 200), ("M15", 50))
ATR_PERIOD = 14
# La última vela agregada no puede terminar más de esto antes de la entrada (cubre fines de
# semana largos y festivos; más allá, los datos no describen el mercado de la entrada).
MAX_STALE = timedelta(hours=96)

_AGGREGATE = text(
    """
    SELECT date_bin(make_interval(mins => :minutes), bar_time,
                    TIMESTAMPTZ '2000-01-03 00:00:00+00') AS bucket,
           (array_agg(open ORDER BY bar_time))[1] AS open,
           max(high) AS high,
           min(low) AS low,
           (array_agg(close ORDER BY bar_time DESC))[1] AS close,
           count(*) AS m1_count
    FROM price_bars
    WHERE broker_id = :broker_id AND symbol = :symbol AND timeframe = 'M1'
      AND bar_time >= :start AND bar_time < :cutoff
    GROUP BY bucket
    ORDER BY bucket
    """
)


def floor_time(value: datetime, minutes: int) -> datetime:
    """Inicio de la vela de `minutes` minutos que contiene `value` (alineada a medianoche)."""
    value = value.astimezone(UTC)
    midnight = value.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = int((value - midnight).total_seconds() // 60)
    return midnight + timedelta(minutes=elapsed - elapsed % minutes)


def _span(buckets: int, minutes: int) -> timedelta:
    """Ventana de calendario para encontrar `buckets` velas: x 7/5 por los fines de semana y
    3 días más de margen."""
    return timedelta(minutes=math.ceil(buckets * minutes * 7 / 5)) + timedelta(days=3)


def ema_required(period: int, settings: Settings) -> int:
    return math.ceil(period * settings.analysis_ema_warmup_factor)


def window_start(entry: datetime, settings: Settings) -> datetime:
    """Primera hora que puede consultar el análisis de una operación (para la huella de
    velas M1)."""
    starts = [
        floor_time(entry, TIMEFRAMES[tf]) - _span(ema_required(p, settings), TIMEFRAMES[tf])
        for tf, p in EMA_SPECS
    ]
    starts.append(floor_time(entry, 60) - _volatility_span(settings))
    return min(starts)


def _volatility_span(settings: Settings) -> timedelta:
    return timedelta(days=settings.analysis_volatility_lookback_days) + _span(
        ATR_PERIOD * 3, TIMEFRAMES["H1"]
    )


def aggregated_bars(
    session: Session,
    broker_id: uuid.UUID,
    symbol: str,
    minutes: int,
    start: datetime,
    cutoff: datetime,
) -> list[Bar]:
    rows = session.execute(
        _AGGREGATE,
        {
            "minutes": minutes,
            "broker_id": broker_id,
            "symbol": symbol,
            "start": start,
            "cutoff": cutoff,
        },
    ).all()
    return [
        Bar(
            time=row.bucket.astimezone(UTC),
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            m1_count=row.m1_count,
        )
        for row in rows
    ]


def _coverage(bars: list[Bar], minutes: int) -> float:
    return sum(b.m1_count for b in bars) / (len(bars) * minutes) if bars else 0.0


def _check(
    bars: list[Bar], needed: int, minutes: int, cutoff: datetime, settings: Settings
) -> str | None:
    """Motivo por el que las velas no bastan, o None si bastan."""
    if len(bars) < needed:
        return f"{len(bars)} velas cerradas antes de la entrada; hacen falta {needed}"
    coverage = _coverage(bars[-needed:], minutes)
    if coverage < settings.analysis_min_bar_coverage:
        return (
            f"cobertura de velas M1 {coverage:.0%} en las últimas {needed} velas "
            f"(mínimo {settings.analysis_min_bar_coverage:.0%})"
        )
    last_end = bars[-1].time + timedelta(minutes=minutes)
    if cutoff - last_end > MAX_STALE:
        return f"la última vela cerrada termina {last_end.isoformat()}, demasiado antes de entrar"
    return None


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def ema_context(
    bars_by_tf: dict[str, list[Bar]], cutoffs: dict[str, datetime], settings: Settings
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for tf, period in EMA_SPECS:
        bars, minutes = bars_by_tf[tf], TIMEFRAMES[tf]
        needed = ema_required(period, settings)
        key = f"ema{period}_{tf.lower()}"
        reason = _check(bars, needed, minutes, cutoffs[tf], settings)
        if reason:
            result[key] = {"ok": False, "motivo": reason, "velas": len(bars)}
            continue
        value = ema_series([b.close for b in bars], period)[-1]
        result[key] = {
            "ok": True,
            "valor": round(value, 10),
            "velas": len(bars),
            "cobertura": round(_coverage(bars[-needed:], minutes), 4),
            "ultima_vela": _iso(bars[-1].time),
            "corte": _iso(cutoffs[tf]),
        }
    return result


def volatility_context(bars: list[Bar], cutoff: datetime, settings: Settings) -> dict[str, Any]:
    minutes = TIMEFRAMES["H1"]
    atrs = atr_series(bars, ATR_PERIOD)
    since = cutoff - timedelta(days=settings.analysis_volatility_lookback_days)
    current = atrs[-1] if atrs else None
    sample = [
        a for b, a in zip(bars[:-1], atrs[:-1], strict=True) if a is not None and b.time >= since
    ]
    base = {"periodo": ATR_PERIOD, "dias": settings.analysis_volatility_lookback_days}
    if current is None:
        return {**base, "ok": False, "motivo": f"{len(bars)} velas H1: no hay ATR({ATR_PERIOD})"}
    if len(sample) < settings.analysis_volatility_min_samples:
        return {
            **base,
            "ok": False,
            "motivo": f"{len(sample)} valores de ATR en los {base['dias']} días anteriores; "
            f"hacen falta {settings.analysis_volatility_min_samples}",
        }
    window = [b for b in bars if b.time >= since]
    reason = _check(window, len(window), minutes, cutoff, settings)
    if reason:
        return {**base, "ok": False, "motivo": reason}
    return {
        **base,
        "ok": True,
        "atr": round(current, 10),
        "percentil": round(percentile_rank(sample, current), 2),
        "muestras": len(sample),
        "mediana": round(sorted(sample)[len(sample) // 2], 10),
        "ultima_vela": _iso(bars[-1].time),
        "corte": _iso(cutoff),
    }


def market_context(
    session: Session,
    broker_id: uuid.UUID,
    symbol: str,
    entry: datetime,
    settings: Settings,
) -> dict[str, Any]:
    """EMAs de M15/H1 y volatilidad ATR H1 con las velas cerradas antes de la entrada."""
    cutoffs = {tf: floor_time(entry, minutes) for tf, minutes in TIMEFRAMES.items()}
    bars_by_tf: dict[str, list[Bar]] = {}
    for tf, minutes in TIMEFRAMES.items():
        needed = max(ema_required(p, settings) for t, p in EMA_SPECS if t == tf)
        span = _span(needed, minutes)
        if tf == "H1":
            span = max(span, _volatility_span(settings))
        bars_by_tf[tf] = aggregated_bars(
            session, broker_id, symbol, minutes, cutoffs[tf] - span, cutoffs[tf]
        )
    return {
        "emas": ema_context(bars_by_tf, cutoffs, settings),
        "volatilidad": volatility_context(bars_by_tf["H1"], cutoffs["H1"], settings),
    }


def entry_bar(
    session: Session, broker_id: uuid.UUID, symbol: str, entry: datetime
) -> dict[str, Any] | None:
    """Vela M1 que contiene la entrada (su spread es el que informa MT5 para esa vela)."""
    row = session.execute(
        select(PriceBar.bar_time, PriceBar.spread).where(
            PriceBar.broker_id == broker_id,
            PriceBar.symbol == symbol,
            PriceBar.timeframe == "M1",
            PriceBar.bar_time == floor_time(entry, 1),
        )
    ).first()
    if row is None:
        return None
    return {"hora": _iso(row.bar_time), "spread": row.spread}


def first_touch(
    session: Session,
    broker_id: uuid.UUID,
    symbol: str,
    start: datetime,
    end: datetime,
    level: float,
    buy: bool,
) -> str | None:
    """Primera vela M1 entre start y end (incluidas) cuyo alto (BUY) o bajo (SELL) alcanza
    `level`."""
    touch = PriceBar.high >= level if buy else PriceBar.low <= level
    value = session.scalar(
        select(func.min(PriceBar.bar_time)).where(
            PriceBar.broker_id == broker_id,
            PriceBar.symbol == symbol,
            PriceBar.timeframe == "M1",
            PriceBar.bar_time >= floor_time(start, 1),
            PriceBar.bar_time <= floor_time(end, 1),
            touch,
        )
    )
    return _iso(value) if value is not None else None


def m1_count(
    session: Session, broker_id: uuid.UUID, symbol: str, start: datetime, end: datetime
) -> int:
    """Velas M1 entre start y end (incluidas): huella barata para saber si llegaron velas."""
    return session.scalar(
        select(func.count()).where(
            PriceBar.broker_id == broker_id,
            PriceBar.symbol == symbol,
            PriceBar.timeframe == "M1",
            PriceBar.bar_time >= start,
            PriceBar.bar_time <= floor_time(end, 1),
        )
    )


def news_near(session: Session, entry: datetime, settings: Settings) -> list[dict[str, Any]]:
    window = timedelta(minutes=settings.analysis_news_window_minutes)
    rows = session.scalars(
        select(NewsEvent)
        .where(NewsEvent.time_utc >= entry - window, NewsEvent.time_utc <= entry + window)
        .order_by(NewsEvent.time_utc)
        .limit(20)
    ).all()
    return [
        {
            "hora": _iso(n.time_utc),
            "minutos_desde_entrada": round((n.time_utc - entry).total_seconds() / 60, 1),
            "moneda": n.currency,
            "impacto": n.impact,
            "titulo": n.title,
            "fuente": n.source,
        }
        for n in rows
    ]
