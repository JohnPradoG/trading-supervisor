"""Estructura de mercado sobre velas cerradas (fase 8), en Python puro.

Todas las funciones reciben solo velas ya cerradas antes de la entrada, de la más antigua a
la más reciente, así que nada de lo que calculan puede mirar el futuro. Definiciones:

- Swing (fractal de N velas, N = 2 por defecto): la vela i es un máximo de swing si su alto
  es ESTRICTAMENTE mayor que el de las N velas anteriores y las N posteriores (un empate no
  es swing); mínimo de swing igual con el bajo. Un swing solo se conoce cuando han cerrado
  sus N velas posteriores: se "confirma" en la vela i + N, y antes no se usa.
- Ruptura de estructura: recorriendo las velas en orden, cuando un cierre supera el último
  máximo de swing confirmado y aún no roto es una ruptura alcista (cierre por debajo del
  último mínimo, bajista). Si va en la misma dirección que la ruptura anterior (o es la
  primera de la ventana) es un BOS (break of structure); si va en la dirección contraria es
  un CHOCH (change of character). Cada nivel se rompe una sola vez.
- Estructura de mercado: con los dos últimos máximos y los dos últimos mínimos de swing
  confirmados: máximo y mínimo más altos (HH + HL) = HH_HL; más bajos (LH + LL) = LH_LL;
  cualquier otra combinación = RANGO.
- Barrido de liquidez: una vela cuyo alto supera el último máximo de swing confirmado (aún no
  roto por un cierre) pero cierra en o por debajo de él (barrido de máximos); al revés con
  el mínimo (barrido de mínimos).
- Máximos (mínimos) iguales: dos máximos de swing confirmados dentro de las últimas
  `lookback` velas que difieren como mucho tolerancia x ATR y que ningún alto posterior ha
  superado (la liquidez sigue ahí).
- FVG (fair value gap) de 3 velas: alcista si bajo_i > alto_(i-2) (hueco [alto_(i-2),
  bajo_i]); bajista si alto_i < bajo_(i-2) (hueco [alto_i, bajo_(i-2)]). Solo cuenta si mide
  al menos `min_size` y sigue sin rellenar: ninguna vela posterior llegó al borde lejano (el
  inferior en un FVG alcista, el superior en uno bajista).
- Order block: en cada ruptura (BOS o CHOCH) alcista, la última vela bajista (cierre <
  apertura) de las `search_back` velas anteriores a la vela que rompió; en una ruptura bajista,
  la última vela alcista. Su zona es [bajo, alto] de esa vela. Deja de ser válido si una vela
  posterior a la ruptura cierra al otro lado de la zona (por debajo del bajo en uno alcista,
  por encima del alto en uno bajista).
- Distancia de un precio a una zona: 0 si está dentro; si no, al borde más cercano.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from supervisor.analytics.indicators import Bar

UP, DOWN = 1, -1


@dataclass(frozen=True)
class Swing:
    index: int  # vela del swing
    confirmed_at: int  # vela en la que se conoce (index + N)
    price: float
    kind: str  # "HIGH" o "LOW"


@dataclass(frozen=True)
class Break:
    index: int  # vela cuyo cierre rompe
    direction: int  # UP o DOWN
    kind: str  # "BOS" o "CHOCH"
    level: float
    swing_index: int


@dataclass(frozen=True)
class Sweep:
    index: int
    direction: int  # UP = barrido de máximos, DOWN = barrido de mínimos
    level: float


@dataclass(frozen=True)
class Zone:
    index: int  # vela en la que se forma (la tercera del FVG, la del order block)
    direction: int
    low: float
    high: float

    @property
    def size(self) -> float:
        return self.high - self.low

    def distance(self, price: float) -> float:
        if self.low <= price <= self.high:
            return 0.0
        return min(abs(price - self.low), abs(price - self.high))


def find_swings(bars: Sequence[Bar], n: int = 2) -> list[Swing]:
    """Swings confirmados (ver cabecera), ordenados por la vela en que se confirman."""
    swings: list[Swing] = []
    for i in range(n, len(bars) - n):
        neighbours = [*range(i - n, i), *range(i + 1, i + n + 1)]
        if all(bars[i].high > bars[j].high for j in neighbours):
            swings.append(Swing(i, i + n, bars[i].high, "HIGH"))
        if all(bars[i].low < bars[j].low for j in neighbours):
            swings.append(Swing(i, i + n, bars[i].low, "LOW"))
    swings.sort(key=lambda s: (s.confirmed_at, s.kind))
    return swings


@dataclass(frozen=True)
class StructureScan:
    swings: list[Swing]
    breaks: list[Break]
    sweeps: list[Sweep]


def scan_structure(bars: Sequence[Bar], n: int = 2) -> StructureScan:
    """Recorre las velas en orden y registra rupturas (BOS/CHOCH) y barridos usando en cada
    vela solo los swings confirmados ANTES de ella."""
    swings = find_swings(bars, n)
    by_confirmation: dict[int, list[Swing]] = {}
    for s in swings:
        by_confirmation.setdefault(s.confirmed_at, []).append(s)
    breaks: list[Break] = []
    sweeps: list[Sweep] = []
    last_high: Swing | None = None
    last_low: Swing | None = None
    high_broken = low_broken = False
    state: int | None = None
    for i, bar in enumerate(bars):
        if last_high is not None and not high_broken:
            if bar.close > last_high.price:
                kind = "BOS" if state in (None, UP) else "CHOCH"
                breaks.append(Break(i, UP, kind, last_high.price, last_high.index))
                state, high_broken = UP, True
            elif bar.high > last_high.price:
                sweeps.append(Sweep(i, UP, last_high.price))
        if last_low is not None and not low_broken:
            if bar.close < last_low.price:
                kind = "BOS" if state in (None, DOWN) else "CHOCH"
                breaks.append(Break(i, DOWN, kind, last_low.price, last_low.index))
                state, low_broken = DOWN, True
            elif bar.low < last_low.price:
                sweeps.append(Sweep(i, DOWN, last_low.price))
        # Los swings que se confirman al cerrar esta vela cuentan desde la siguiente.
        for s in by_confirmation.get(i, []):
            if s.kind == "HIGH":
                last_high, high_broken = s, False
            else:
                last_low, low_broken = s, False
    return StructureScan(swings, breaks, sweeps)


def market_structure(swings: Sequence[Swing]) -> str | None:
    """HH_HL, LH_LL o RANGO con los dos últimos máximos y mínimos; None si faltan swings."""
    highs = [s.price for s in swings if s.kind == "HIGH"]
    lows = [s.price for s in swings if s.kind == "LOW"]
    if len(highs) < 2 or len(lows) < 2:
        return None
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return "HH_HL"
    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return "LH_LL"
    return "RANGO"


def equal_levels(
    bars: Sequence[Bar], swings: Sequence[Swing], kind: str, tolerance: float, lookback: int
) -> bool:
    """¿Hay dos swings `kind` ("HIGH"/"LOW") iguales dentro de la tolerancia (en precio) en
    las últimas `lookback` velas, sin barrer después?"""
    start = len(bars) - lookback
    levels = [s for s in swings if s.kind == kind and s.index >= start]
    for b_pos in range(1, len(levels)):
        later = levels[b_pos]
        for earlier in levels[:b_pos]:
            if abs(later.price - earlier.price) > tolerance:
                continue
            after = bars[later.index + 1 :]
            if kind == "HIGH":
                edge = max(later.price, earlier.price)
                if not any(b.high > edge for b in after):
                    return True
            else:
                edge = min(later.price, earlier.price)
                if not any(b.low < edge for b in after):
                    return True
    return False


def open_fvgs(bars: Sequence[Bar], lookback: int, min_size: float) -> list[Zone]:
    """FVG sin rellenar formados en las últimas `lookback` velas."""
    zones: list[Zone] = []
    for i in range(max(2, len(bars) - lookback), len(bars)):
        first, last = bars[i - 2], bars[i]
        if last.low > first.high:
            zone = Zone(i, UP, first.high, last.low)
            filled = any(b.low <= zone.low for b in bars[i + 1 :])
        elif last.high < first.low:
            zone = Zone(i, DOWN, last.high, first.low)
            filled = any(b.high >= zone.high for b in bars[i + 1 :])
        else:
            continue
        if zone.size >= min_size and not filled:
            zones.append(zone)
    return zones


def order_blocks(
    bars: Sequence[Bar], breaks: Sequence[Break], lookback: int, search_back: int
) -> list[Zone]:
    """Order blocks válidos de las rupturas ocurridas en las últimas `lookback` velas."""
    zones: list[Zone] = []
    for brk in breaks:
        if brk.index < len(bars) - lookback:
            continue
        candle = None
        for k in range(brk.index - 1, max(-1, brk.index - 1 - search_back), -1):
            b = bars[k]
            if (brk.direction == UP and b.close < b.open) or (
                brk.direction == DOWN and b.close > b.open
            ):
                candle = k
                break
        if candle is None:
            continue
        zone = Zone(candle, brk.direction, bars[candle].low, bars[candle].high)
        after = bars[brk.index + 1 :]
        if brk.direction == UP:
            invalid = any(b.close < zone.low for b in after)
        else:
            invalid = any(b.close > zone.high for b in after)
        if not invalid:
            zones.append(zone)
    return zones


def nearest_zone(zones: Sequence[Zone], price: float) -> Zone | None:
    """La zona más cercana al precio (en empate, la más reciente)."""
    if not zones:
        return None
    return min(zones, key=lambda z: (z.distance(price), -z.index))
