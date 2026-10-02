"""Indicadores sobre series de velas, en Python puro (sin NumPy: el VPS es pequeño y las
series que se usan son de cientos de velas).

- EMA(n): se siembra con la media simple de las n primeras velas y después
  EMA_t = cierre_t x k + EMA_(t-1) x (1 - k), con k = 2 / (n + 1). El peso de la semilla cae
  a (1 - k)^m tras m velas más: con 2n velas en total queda por debajo del 14 %, por eso el
  análisis exige al menos warmup_factor x n velas (2n por defecto).
- ATR(n) de Wilder: TR_t = max(alto - bajo, |alto - cierre_(t-1)|, |bajo - cierre_(t-1)|);
  la primera ATR es la media de los n primeros TR y después
  ATR_t = (ATR_(t-1) x (n - 1) + TR_t) / n.
- RSI(n) de Wilder (fase 8): cambios c_t - c_(t-1); la primera media de subidas y bajadas
  es la simple de los n primeros cambios y después media_t = (media_(t-1) x (n - 1) + x_t) / n.
  RSI = 100 - 100 / (1 + subidas / bajadas); sin bajadas vale 100 (y 50 si tampoco hubo
  subidas). El primer valor aparece en la vela n.
- ROC(n) (fase 8): (c_t - c_(t-n)) / c_(t-n) x 100.
- ADX(n) de Wilder (fase 8): +DM = alto_t - alto_(t-1) si es mayor que bajo_(t-1) - bajo_t y
  positivo (si no 0); -DM al revés. TR, +DM y -DM se suavizan con Wilder (primera suma de n
  valores y después S_t = S_(t-1) - S_(t-1) / n + x_t); +DI = 100 x +DM_s / TR_s, -DI igual,
  DX = 100 x |+DI - -DI| / (+DI + -DI). El primer ADX es la media de los n primeros DX (vela
  2n - 1) y después ADX_t = (ADX_(t-1) x (n - 1) + DX_t) / n.
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


def rsi_series(values: Sequence[float], period: int) -> list[float | None]:
    """RSI de Wilder alineado con `values`: None hasta el índice `period`."""
    if period < 1:
        raise ValueError("period debe ser >= 1")
    result: list[float | None] = [None] * len(values)
    if len(values) < period + 1:
        return result
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gain = sum(max(c, 0.0) for c in changes[:period]) / period
    loss = sum(max(-c, 0.0) for c in changes[:period]) / period

    def _rsi(g: float, lo: float) -> float:
        if lo == 0:
            return 50.0 if g == 0 else 100.0
        return 100.0 - 100.0 / (1.0 + g / lo)

    result[period] = _rsi(gain, loss)
    for i in range(period, len(changes)):
        c = changes[i]
        gain = (gain * (period - 1) + max(c, 0.0)) / period
        loss = (loss * (period - 1) + max(-c, 0.0)) / period
        result[i + 1] = _rsi(gain, loss)
    return result


def roc(values: Sequence[float], period: int) -> float | None:
    """Rate of change en % de la última vela frente a la de hace `period` velas."""
    if period < 1:
        raise ValueError("period debe ser >= 1")
    if len(values) < period + 1 or values[-1 - period] == 0:
        return None
    base = values[-1 - period]
    return (values[-1] - base) / base * 100.0


@dataclass(frozen=True)
class AdxPoint:
    adx: float | None
    plus_di: float | None
    minus_di: float | None


def adx_series(bars: Sequence[Bar], period: int) -> list[AdxPoint]:
    """ADX, +DI y -DI de Wilder alineados con `bars`. +DI/-DI desde el índice `period` y el
    ADX desde el índice 2 x period - 1."""
    if period < 1:
        raise ValueError("period debe ser >= 1")
    empty = AdxPoint(None, None, None)
    result = [empty] * len(bars)
    if len(bars) < period + 1:
        return result
    ranges = true_ranges(bars)
    plus_dm = [0.0] * len(bars)
    minus_dm = [0.0] * len(bars)
    for i in range(1, len(bars)):
        up = bars[i].high - bars[i - 1].high
        down = bars[i - 1].low - bars[i].low
        plus_dm[i] = up if up > down and up > 0 else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0
    tr_s = sum(ranges[1 : period + 1])
    p_s = sum(plus_dm[1 : period + 1])
    m_s = sum(minus_dm[1 : period + 1])
    dxs: list[float] = []
    adx: float | None = None
    for i in range(period, len(bars)):
        if i > period:
            tr_s = tr_s - tr_s / period + ranges[i]
            p_s = p_s - p_s / period + plus_dm[i]
            m_s = m_s - m_s / period + minus_dm[i]
        plus_di = 100.0 * p_s / tr_s if tr_s else 0.0
        minus_di = 100.0 * m_s / tr_s if tr_s else 0.0
        total = plus_di + minus_di
        dx = 100.0 * abs(plus_di - minus_di) / total if total else 0.0
        dxs.append(dx)
        if len(dxs) == period:
            adx = sum(dxs) / period
        elif len(dxs) > period:
            adx = (adx * (period - 1) + dx) / period  # type: ignore[operator]
        result[i] = AdxPoint(adx, plus_di, minus_di)
    return result
