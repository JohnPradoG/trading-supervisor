"""Datos sintéticos para el diagnóstico: operaciones cerradas con sus velas M1 reales (una cada
3 horas para que sus horizontes no se pisen) y un problema plantado o solo ruido.

Unidades: precio de entrada 100, SL a 1 (1 R = 1.00 de precio), TP a 2 R. Las velas son Bid y
el spread es 0, así que compras y ventas son simétricas.

- "sl_ajustado": el 55 % baja a -1.2 R en el minuto 5 (toca el SL real) y luego sube hasta el
  TP (+2.2 R en el minuto 25); el 25 % cae directo a -2.5 R; el 20 % sube directo al TP. Las
  reglas actuales pierden; un SL más ancho (1.5R o 2R) gana.
- "ruido": paseo aleatorio; la operación real cierra por SL, TP o a los 60 minutos por el EA.
- "win_rate_trampa": las que tocarían +1 R casi siempre siguen hasta el TP (2 R), así que un TP
  más corto sube los aciertos y empeora la expectativa.
"""

import random
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import insert
from sqlalchemy.orm import Session

from supervisor.models import Account, PriceBar, Trade
from supervisor.models.enums import Direction, ExitReason, TradeSource, TradeStatus
from tests.integration.pattern_helpers import Scope

START = datetime(2026, 3, 2, 0, 0, 30, tzinfo=UTC)  # lunes; entradas a los 30 s del minuto
ENTRY = 100.0


def _path_sl_tight(rng: random.Random) -> list[float]:
    """Cierres en R (favorable) minuto a minuto tras la entrada."""
    kind = rng.random()
    if kind < 0.55:
        down = [-0.24 * m for m in range(1, 6)]  # -1.2 en el minuto 5
        up = [-1.2 + 3.4 * m / 20 for m in range(1, 21)]  # +2.2 en el minuto 25
        return down + up + [2.2 + 0.05 * m for m in range(1, 100)]
    if kind < 0.8:
        return [-2.5 * m / 15 for m in range(1, 16)] + [-2.5 - 0.01 * m for m in range(1, 100)]
    return [2.2 * m / 10 for m in range(1, 11)] + [2.2 + 0.01 * m for m in range(1, 100)]


def _path_noise(rng: random.Random) -> list[float]:
    out, f = [], 0.0
    for _ in range(240):
        f += rng.gauss(0, 0.25)
        out.append(f)
    return out


def _path_win_rate_trap(rng: random.Random) -> list[float]:
    """+1R casi siempre se alcanza y el TP de 2R también el 70 %: TP 1R sube aciertos pero
    pierde la mitad de cada ganadora."""
    kind = rng.random()
    if kind < 0.7:
        return [2.2 * m / 20 for m in range(1, 21)] + [2.2] * 100
    if kind < 0.85:
        up = [1.1 * m / 10 for m in range(1, 11)]
        return up + [1.1 - 2.3 * m / 10 for m in range(1, 11)] + [-1.2] * 100
    return [-1.2 * m / 10 for m in range(1, 11)] + [-1.2] * 100


PATHS = {
    "sl_ajustado": _path_sl_tight,
    "ruido": _path_noise,
    "win_rate_trampa": _path_win_rate_trap,
}


def _bar(f_prev: float, f: float, buy: bool, wiggle: float) -> tuple[float, float, float]:
    """(alto, bajo, cierre) Bid de la vela que va de f_prev a f (en R)."""
    hi_f, lo_f = max(f_prev, f) + wiggle, min(f_prev, f) - wiggle
    if buy:
        return ENTRY + hi_f, ENTRY + lo_f, ENTRY + f
    return ENTRY - lo_f, ENTRY - hi_f, ENTRY - f


def insert_diag_trades(
    session: Session,
    scope: Scope,
    n: int,
    *,
    seed: int,
    kind: str = "sl_ajustado",
    start: datetime = START,
    spacing_hours: int = 3,
) -> list[uuid.UUID]:
    rng = random.Random(seed)
    broker_id = session.get(Account, scope.account_id).broker_id
    ids, bars = [], []
    for i in range(n):
        entry = start + timedelta(hours=spacing_hours * i)
        buy = rng.random() < 0.5
        path = PATHS[kind](rng)
        # Operación real: SL -1, TP +2 (los toques intrabarra se resuelven como la réplica: SL
        # primero); por el EA a los 60 minutos.
        close_min, result, reason = None, None, ExitReason.EXPERT
        prev = 0.0
        for m, f in enumerate(path, start=1):
            if min(prev, f) <= -1.0:
                close_min, result, reason = m, -1.0, ExitReason.SL
                break
            if max(prev, f) >= 2.0:
                close_min, result, reason = m, 2.0, ExitReason.TP
                break
            if m == 60:
                close_min, result = m, f
                break
            prev = f
        close_time = entry.replace(second=0) + timedelta(minutes=close_min, seconds=40)
        mfe = max([0.0, *path[:close_min]])
        duration = int((close_time - entry).total_seconds())
        horizon = max(60, 3 * duration / 60)
        sl = ENTRY - 1 if buy else ENTRY + 1
        tp = ENTRY + 2 if buy else ENTRY - 2
        close_price = ENTRY + result if buy else ENTRY - result
        trade = Trade(
            account_id=scope.account_id,
            bot_version_id=scope.version_id,
            position_id=random.randint(1, 2**62),
            magic_number=909,
            symbol=scope.symbol,
            direction=Direction.BUY if buy else Direction.SELL,
            order_type="MARKET",
            source=TradeSource.DEMO,
            status=TradeStatus.CLOSED,
            symbol_point=Decimal("0.01"),
            symbol_digits=2,
            entry_time=entry,
            entry_price=Decimal(str(ENTRY)),
            initial_sl=Decimal(str(sl)),
            initial_tp=Decimal(str(tp)),
            initial_volume=Decimal("0.10"),
            max_volume=Decimal("0.10"),
            risk_amount=Decimal("100"),
            close_time=close_time,
            close_price=Decimal(str(round(close_price, 4))),
            gross_profit=Decimal(str(round(result * 100, 2))),
            commission=Decimal("0"),
            swap=Decimal("0"),
            net_profit=Decimal(str(round(result * 100, 2))),
            duration_seconds=duration,
            exit_reason=reason,
            mfe_points=Decimal(str(round(mfe / 0.01, 2))),
            data_quality={},
        )
        session.add(trade)
        ids.append(trade)
        prev = 0.0
        first = entry.replace(second=0) + timedelta(minutes=1)
        for m, f in enumerate(path[: min(int(horizon) + 1, spacing_hours * 60)]):
            high, low, close = _bar(prev, f, buy, 0.02)
            bars.append(
                {
                    "broker_id": broker_id,
                    "symbol": scope.symbol,
                    "timeframe": "M1",
                    "bar_time": first + timedelta(minutes=m),
                    "open": round(ENTRY + prev if buy else ENTRY - prev, 6),
                    "high": round(max(high, close, ENTRY + prev if buy else ENTRY - prev), 6),
                    "low": round(min(low, close, ENTRY + prev if buy else ENTRY - prev), 6),
                    "close": round(close, 6),
                    "tick_volume": 10,
                    "spread": 0,
                }
            )
            prev = f
    session.flush()
    for start_i in range(0, len(bars), 5000):
        session.execute(insert(PriceBar), bars[start_i : start_i + 5000])
    session.flush()
    return [t.trade_id for t in ids]
