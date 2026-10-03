"""Réplica de salidas con velas M1 sobre las entradas REALES (función pura, sin base de datos).

Pregunta: "con esta misma entrada, ¿qué habría pasado con otra regla de salida?". La entrada
(hora, precio, dirección y riesgo inicial) es la real; solo cambia la salida. Por eso una
variante de salida no depende de entradas alternativas desconocidas (un filtro sí: ver
supervisor.analytics.diagnosis).

Unidades: todo se mide en R de la operación real = distancia entre la entrada y el SL inicial
(el mismo lote). Una variante "SL a 1.5R" pone el stop a 1.5 veces esa distancia con el mismo
lote: su pérdida máxima es -1.5 R de la operación original. Si el EA calcula el lote por riesgo
fijo, con un SL k veces más ancho el lote baja k veces: el resultado en R nuevas es el de la
tabla dividido por k (el signo no cambia).

Reglas de la réplica (documentadas en el README, "Diagnóstico de bots"):

- **Velas:** M1 de precio Bid del broker, desde la primera vela COMPLETA después del minuto de
  entrada (la vela del minuto de entrada contiene precios anteriores a la entrada y se omite:
  un toque dentro del primer minuto no se ve) hasta el horizonte.
- **Horizonte:** como mucho `horizon_factor` x la duración real (con un mínimo y un máximo en
  minutos); si al llegar no se ha cerrado, sale al cierre de la última vela (HORIZONTE).
- **Compras y ventas:** las compras cierran al Bid (las velas), así que SL y TP se comparan con
  el mínimo y el máximo de la vela. Las ventas cierran al Ask = Bid + spread: a los precios de
  la vela se les suma el spread al entrar (el del DNA, última vela M1 cerrada antes de la
  entrada; si no hay, el de la primera vela de la réplica; si tampoco, 0 y se anota).
- **Ambigüedad dentro de una vela:** si en la misma vela se tocan el stop y el objetivo, se
  supone lo peor: primero el stop. Los niveles se ejecutan a su precio exacto (sin
  deslizamiento ni huecos de precio).
- **Breakeven y trailing:** se actualizan con el máximo favorable al CIERRE de cada vela y
  valen desde la vela siguiente (nunca dentro de la misma vela en que se alcanzó el disparo).
  Breakeven = stop en el precio de entrada (0 R antes de costes). Trailing = stop a d R del
  máximo favorable, sin bajar nunca del stop inicial.
- **Stop por tiempo:** si tras t minutos desde la entrada sigue abierta, sale al cierre de esa
  vela (después de comprobar el stop y el objetivo de la vela).
- **Salidas del propio EA:** si la operación real no cerró por un nivel (SL, TP o stop out)
  sino por el EA o a mano, esa decisión se respeta: en la vela del cierre real la variante sale
  al precio de cierre real, salvo que su stop se toque en esa vela (lo peor) o algo antes.
  Si cerró por un nivel, la variante sigue con sus propios niveles hasta el horizonte.
- **Costes:** se suma a cada variante el coste real de la operación en R ((comisión + swap) /
  riesgo). La comisión no depende de la salida; el swap se mantiene como el real (aproximación
  si la variante dura más o menos noches).

Resultado de una variante en una operación: R total = R de precio + coste. Gana si supera la
tolerancia de breakeven (0.05 R por defecto), como en las estadísticas.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

LEVEL_EXITS = {"SL", "TP", "STOP_OUT"}

SL, TP, BE, TRAIL, TIME, EA, HORIZON = (
    "SL",
    "TP",
    "BREAKEVEN",
    "TRAILING",
    "TIEMPO",
    "SALIDA_DEL_EA",
    "HORIZONTE",
)


@dataclass(frozen=True)
class M1Bar:
    time: datetime  # inicio de la vela (UTC)
    high: float
    low: float
    close: float
    spread: int | None = None  # puntos


@dataclass(frozen=True)
class ExitRule:
    """Una regla de salida. tp_m None = TP original (o sin TP si la operación no tenía)."""

    key: str
    family: str
    sl_k: float = 1.0
    tp_m: float | None = None
    be_r: float | None = None
    time_min: int | None = None
    trail_r: float | None = None

    def spec(self) -> dict:
        return {
            "clave": self.key,
            "familia": self.family,
            "sl_k": self.sl_k,
            "tp_m": self.tp_m,
            "breakeven_r": self.be_r,
            "tiempo_min": self.time_min,
            "trailing_r": self.trail_r,
        }


BASELINE = ExitRule("actual", "actual")


@dataclass(frozen=True)
class ReplayTrade:
    trade_id: str
    buy: bool
    entry_time: datetime
    entry_price: float
    risk_dist: float  # |entrada - SL inicial| en precio (> 0)
    tp_dist: float | None  # |TP inicial - entrada| en precio, o None sin TP
    spread_price: float  # spread en precio que se suma a las ventas (0 en compras)
    cost_r: float  # (comisión + swap) / riesgo, normalmente <= 0
    real_exit: str | None
    real_close_time: datetime
    real_close_price: float
    bars: tuple[M1Bar, ...]


@dataclass(frozen=True)
class ReplayResult:
    r: float  # R total (precio + coste)
    r_price: float
    reason: str
    exit_time: datetime
    bars_used: int


def _minute(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(second=0, microsecond=0)


def horizon_minutes(
    duration_seconds: int, factor: float, min_minutes: int, max_minutes: int
) -> int:
    """Minutos de réplica tras la entrada: factor x duración real, acotado."""
    return int(min(max_minutes, max(min_minutes, factor * duration_seconds / 60)))


def _r_bar(t: ReplayTrade, bar: M1Bar) -> tuple[float, float, float]:
    """(máximo favorable, mínimo favorable, cierre) de la vela en R de precio."""
    if t.buy:
        hi, lo, cl = bar.high - t.entry_price, bar.low - t.entry_price, bar.close - t.entry_price
    else:
        s = t.spread_price
        hi = t.entry_price - (bar.low + s)
        lo = t.entry_price - (bar.high + s)
        cl = t.entry_price - (bar.close + s)
    return hi / t.risk_dist, lo / t.risk_dist, cl / t.risk_dist


def _real_close_r(t: ReplayTrade) -> float:
    """El precio real de cierre ya es el ejecutado (Ask en ventas): sin spread añadido."""
    move = t.real_close_price - t.entry_price if t.buy else t.entry_price - t.real_close_price
    return move / t.risk_dist


def replay(t: ReplayTrade, rule: ExitRule) -> ReplayResult | None:
    """Aplica una regla de salida vela a vela. None sin velas."""
    if not t.bars or t.risk_dist <= 0:
        return None
    stop = -rule.sl_k
    stop_reason = SL
    if rule.tp_m is not None:
        target = rule.tp_m
    elif t.tp_dist:
        target = t.tp_dist / t.risk_dist
    else:
        target = None
    respect_ea = t.real_exit not in LEVEL_EXITS
    real_close_minute = _minute(t.real_close_time)
    deadline = (
        t.entry_time + timedelta(minutes=rule.time_min) if rule.time_min is not None else None
    )
    best = 0.0
    for i, bar in enumerate(t.bars, start=1):
        hi, lo, cl = _r_bar(t, bar)
        end = bar.time + timedelta(minutes=1)
        if lo <= stop:
            return _done(t, stop, stop_reason, end, i)
        if respect_ea and bar.time >= real_close_minute:
            return _done(t, _real_close_r(t), EA, t.real_close_time, i)
        if target is not None and hi >= target:
            return _done(t, target, TP, end, i)
        if deadline is not None and end >= deadline:
            return _done(t, cl, TIME, end, i)
        best = max(best, hi)
        if rule.be_r is not None and best >= rule.be_r and stop < 0.0:
            stop, stop_reason = 0.0, BE
        if rule.trail_r is not None and best - rule.trail_r > stop:
            stop, stop_reason = best - rule.trail_r, TRAIL
    last = t.bars[-1]
    _, _, cl = _r_bar(t, last)
    return _done(t, cl, HORIZON, last.time + timedelta(minutes=1), len(t.bars))


def _done(t: ReplayTrade, r_price: float, reason: str, when: datetime, used: int) -> ReplayResult:
    return ReplayResult(round(r_price + t.cost_r, 6), round(r_price, 6), reason, when, used)


def reached_after(t: ReplayTrade, level_r: float, after: datetime) -> datetime | None:
    """Primera vela (posterior al minuto de `after`) en la que el precio alcanza `level_r` R a
    favor (con el spread en ventas). Para "SL demasiado ajustado": ¿llegó luego al TP?"""
    start = _minute(after)
    for bar in t.bars:
        if bar.time <= start:
            continue
        hi, _, _ = _r_bar(t, bar)
        if hi >= level_r:
            return bar.time
    return None


# Rejilla fija de variantes -------------------------------------------------------------------

FAMILY_TEXT = {
    "sl": "Distancia del SL",
    "tp": "Distancia del TP",
    "breakeven": "Mover a breakeven",
    "tiempo": "Stop por tiempo",
    "trailing": "Trailing stop",
    "sl_tp": "SL y TP combinados",
}


def _k(value: float) -> str:
    return f"{value:g}".replace(".", "_")


def exit_grid() -> list[ExitRule]:
    """La rejilla de variantes de salida, fija (no se ajusta a los datos). Cada familia cambia
    una sola cosa respecto a las reglas actuales; "sl_tp" combina las dos más típicas (SL más
    ancho con TP más corto: suele subir el % de aciertos, no siempre el resultado)."""
    out: list[ExitRule] = []
    for k in (0.75, 1.25, 1.5, 2.0):
        out.append(ExitRule(f"sl_{_k(k)}", "sl", sl_k=k))
    for m in (1.0, 1.5, 2.0, 3.0):
        out.append(ExitRule(f"tp_{_k(m)}", "tp", tp_m=m))
    for x in (0.5, 1.0):
        out.append(ExitRule(f"be_{_k(x)}", "breakeven", be_r=x))
    for minutes in (30, 60, 120, 240):
        out.append(ExitRule(f"tiempo_{minutes}", "tiempo", time_min=minutes))
    for d in (1.0, 1.5):
        out.append(ExitRule(f"trailing_{_k(d)}", "trailing", trail_r=d))
    for k, m in ((1.5, 1.0), (2.0, 1.0), (1.5, 1.5), (2.0, 2.0)):
        out.append(ExitRule(f"sl_{_k(k)}_tp_{_k(m)}", "sl_tp", sl_k=k, tp_m=m))
    return out


def rule_text(rule: ExitRule) -> str:
    """La regla en castellano llano, para quien edita el EA."""
    if rule.family == "sl":
        return f"Poner el SL a {rule.sl_k:g}R ({rule.sl_k:g} veces la distancia actual al SL)"
    if rule.family == "tp":
        return f"Poner el TP a {rule.tp_m:g}R ({rule.tp_m:g} veces la distancia al SL)"
    if rule.family == "breakeven":
        return f"Mover el SL a breakeven (precio de entrada) cuando vaya +{rule.be_r:g}R a favor"
    if rule.family == "tiempo":
        return f"Cerrar la operación si sigue abierta tras {rule.time_min} minutos"
    if rule.family == "trailing":
        return f"Usar un trailing stop a {rule.trail_r:g}R del máximo favorable"
    if rule.family == "sl_tp":
        return f"Poner el SL a {rule.sl_k:g}R y el TP a {rule.tp_m:g}R"
    return "Mantener las reglas de salida actuales"


def replay_all(
    trades: Sequence[ReplayTrade], rules: Sequence[ExitRule]
) -> dict[str, dict[str, ReplayResult]]:
    """{trade_id: {clave de regla: resultado}} (las operaciones sin velas no aparecen)."""
    out: dict[str, dict[str, ReplayResult]] = {}
    for t in trades:
        results = {}
        for rule in rules:
            res = replay(t, rule)
            if res is not None:
                results[rule.key] = res
        if results:
            out[t.trade_id] = results
    return out
