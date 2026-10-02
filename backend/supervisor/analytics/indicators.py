"""Indicadores sobre series de velas, en Python puro (sin NumPy: el VPS es pequeño y las
series que se usan son de cientos de velas).

- EMA(n): se siembra con la media simple de las n primeras velas y después
  EMA_t = cierre_t x k + EMA_(t-1) x (1 - k), con k = 2 / (n + 1). El peso de la semilla cae
  a (1 - k)^m tras m velas más: con 2n velas en total queda por debajo del 14 %, por eso el
  análisis exige al menos warmup_factor x n velas (2n por defecto).
- ATR(n) de Wilder: TR_t = max(alto - bajo, |alto - cierre_(t-1)|, |bajo - cierre_(t-1)|);
  la primera ATR es la media de los n primeros TR y después
  ATR_t = (ATR_(t-1) x (n - 1) + TR_t) / n.
- Percentil: porcentaje de la muestra con valor <= al valor dado (0-100).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Bar:
    time: datetime  # inicio de la vela, UTC
    open: float
    high: float
    low: float
    close: float
    m1_count: int = 1  # velas M1 que la forman (para medir cobertura)


def ema_series(values: Sequence[float], period: int) -> list[float | None]:
    """EMA alineada con `values`: None hasta tener `period` valores."""
    if period < 1:
        raise ValueError("period debe ser >= 1")
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    current = sum(values[:period]) / period
    result[period - 1] = current
    k = 2 / (period + 1)
    for i in range(period, len(values)):
        current = values[i] * k + current * (1 - k)
        result[i] = current
    return result


def last_ema(values: Sequence[float], period: int) -> float | None:
    series = ema_series(values, period)
    return series[-1] if series else None


def true_ranges(bars: Sequence[Bar]) -> list[float]:
    ranges: list[float] = []
    for i, bar in enumerate(bars):
        if i == 0:
            ranges.append(bar.high - bar.low)
        else:
            prev = bars[i - 1].close
            ranges.append(max(bar.high - bar.low, abs(bar.high - prev), abs(bar.low - prev)))
    return ranges


def atr_series(bars: Sequence[Bar], period: int) -> list[float | None]:
    """ATR de Wilder alineada con `bars`. El TR de la primera vela no tiene cierre anterior y
    no se usa: la primera ATR aparece en la vela `period` (índice period)."""
    if period < 1:
        raise ValueError("period debe ser >= 1")
    result: list[float | None] = [None] * len(bars)
    ranges = true_ranges(bars)
    if len(bars) < period + 1:
        return result
    current = sum(ranges[1 : period + 1]) / period
    result[period] = current
    for i in range(period + 1, len(bars)):
        current = (current * (period - 1) + ranges[i]) / period
        result[i] = current
    return result


def percentile_rank(sample: Sequence[float], value: float) -> float:
    """Porcentaje (0-100) de la muestra con valor <= value."""
    if not sample:
        raise ValueError("muestra vacía")
    return 100.0 * sum(1 for x in sample if x <= value) / len(sample)
