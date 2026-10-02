"""MFE/MAE de una operación cerrada a partir de las velas M1 guardadas (price_bars).

Convenciones:
- Se usan las velas M1 del broker de la cuenta y del mismo símbolo desde la vela que contiene
  la entrada hasta la que contiene el cierre, ambas incluidas. Es una aproximación: esas dos
  velas incluyen precios de antes de la entrada y de después del cierre.
- Las velas de MT5 son de precio Bid; no se corrige por spread en las ventas.
- mfe_points y mae_points son el mejor y el peor resultado flotante en puntos, con signo de
  beneficio según la dirección y relativos a entry_price: mfe_points >= 0 y mae_points <= 0
  (en el momento de la entrada el resultado flotante es 0).
- max_floating_profit / max_floating_drawdown = distancia / tick_size * tick_value *
  initial_volume. Se usa el volumen inicial, el mismo del riesgo, para que excursión y riesgo
  sean comparables (MAE en R); con aumentos o parciales es una aproximación.
- Si faltan velas se calcula con las que hay y la cobertura queda en
  data_quality["excursion"]; el repaso periódico lo recalcula si llegan más velas.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from supervisor.models import PriceBar, Trade
from supervisor.models.enums import Direction, ExcursionSource
from supervisor.worker.common import quantize, set_quality

MAX_GAPS_LISTED = 10


def _floor_minute(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(second=0, microsecond=0)


def _q4(value: Decimal) -> Decimal:
    return quantize(value, 4)


def _gaps(start: datetime, end: datetime, present: set[datetime]) -> list[list[str]]:
    gaps: list[list[str]] = []
    current = start
    gap_start: datetime | None = None
    while current <= end:
        if current not in present:
            gap_start = gap_start or current
        elif gap_start is not None:
            gaps.append([gap_start.isoformat(), (current - timedelta(minutes=1)).isoformat()])
            gap_start = None
        current += timedelta(minutes=1)
    if gap_start is not None:
        gaps.append([gap_start.isoformat(), end.isoformat()])
    return gaps


def bars_available(session: Session, trade: Trade, broker_id) -> int:
    """Velas M1 disponibles en el rango de la operación (para saber si vale la pena
    recalcular)."""
    return session.scalar(
        select(func.count()).where(
            PriceBar.broker_id == broker_id,
            PriceBar.symbol == trade.symbol,
            PriceBar.timeframe == "M1",
            PriceBar.bar_time >= _floor_minute(trade.entry_time),
            PriceBar.bar_time <= _floor_minute(trade.close_time),
        )
    )


def compute_excursion(session: Session, trade: Trade, broker_id) -> dict[str, Any]:
    """Calcula y guarda las excursiones. Devuelve la información de cobertura."""
    if trade.close_time is None:
        return {}
    start = _floor_minute(trade.entry_time)
    end = _floor_minute(trade.close_time)
    in_range = (
        PriceBar.broker_id == broker_id,
        PriceBar.symbol == trade.symbol,
        PriceBar.timeframe == "M1",
        PriceBar.bar_time >= start,
        PriceBar.bar_time <= end,
    )
    found, high, low = session.execute(
        select(func.count(), func.max(PriceBar.high), func.min(PriceBar.low)).where(*in_range)
    ).one()
    expected = int((end - start).total_seconds() // 60) + 1
    info: dict[str, Any] = {
        "fuente": ExcursionSource.M1_BARS.value,
        "velas_esperadas": expected,
        "velas_encontradas": found,
        "cobertura": round(found / expected, 4),
        "completa": found >= expected,
    }
    if found < expected:
        times = session.scalars(select(PriceBar.bar_time).where(*in_range))
        present = {t.astimezone(UTC) for t in times}
        gaps = _gaps(start, end, present)
        info["huecos"] = gaps[:MAX_GAPS_LISTED]
        info["num_huecos"] = len(gaps)

    if found == 0:
        info["nota"] = "sin velas M1 del símbolo durante la operación"
        trade.mfe_points = trade.mae_points = None
        trade.max_floating_profit = trade.max_floating_drawdown = None
        trade.excursion_source = None
        set_quality(trade, "excursion", info)
        return info

    entry = trade.entry_price
    if trade.direction == Direction.BUY:
        favorable, adverse = high - entry, low - entry
    else:
        favorable, adverse = entry - low, entry - high
    favorable = max(favorable, Decimal(0))
    adverse = min(adverse, Decimal(0))

    if trade.symbol_point:
        trade.mfe_points = _q4(favorable / trade.symbol_point)
        trade.mae_points = _q4(adverse / trade.symbol_point)
    else:
        trade.mfe_points = trade.mae_points = None
        info["nota"] = "sin point del símbolo"
    if trade.tick_size and trade.tick_value is not None:
        per_price = trade.tick_value / trade.tick_size * trade.initial_volume
        trade.max_floating_profit = _q4(favorable * per_price)
        trade.max_floating_drawdown = _q4(adverse * per_price)
    else:
        trade.max_floating_profit = trade.max_floating_drawdown = None
        info["nota_dinero"] = "sin tick_size/tick_value del símbolo"
    trade.excursion_source = ExcursionSource.M1_BARS
    set_quality(trade, "excursion", info)
    return info
