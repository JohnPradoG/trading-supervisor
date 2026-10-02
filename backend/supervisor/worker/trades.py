"""Vida de una operación a partir de deals, modificaciones de SL/TP y fotos de posiciones.

Una operación (trade) = una posición de MT5, identificada por (account_id, position_id).

Decisiones:
- Los números de la operación se recalculan siempre desde todos sus deals registrados en
  trade_events (ordenados por hora y ticket). Así el resultado no depende del orden de llegada
  y reprocesar no cambia nada.
- entry_price es el precio del PRIMER deal de entrada (es el que define el SL inicial y el
  riesgo). Si hubo aumentos, el precio medio ponderado de todas las entradas se guarda en
  data_quality["precio_medio_entrada"] y es el que se usa para calcular `points`.
- commission incluye las comisiones de entrada y de salida, más DEAL_FEE de MT5.
- INOUT (inversión en cuentas netting): cierra el volumen abierto y la operación queda
  CLOSED. El volumen reabierto en sentido contrario conserva el mismo position_id en MT5 y no
  puede ser otra fila de trades (unicidad); se deja constancia en
  data_quality["inversion_inout"] y los deals posteriores de esa posición quedan IGNORED.
- order_type: el EA no informa el tipo de orden, se guarda "UNKNOWN".
- source: cuenta REAL -> LIVE; DEMO y CONTEST -> DEMO.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, or_, select, true, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from supervisor.config import Settings
from supervisor.models import AccountSnapshot, Deployment, RawEvent, Terminal, Trade, TradeEvent
from supervisor.models.enums import (
    AccountType,
    Direction,
    ExitReason,
    RawEventStatus,
    TradeEventType,
    TradeSource,
    TradeStatus,
)
from supervisor.schemas.ingest import (
    DealEntry,
    DealEvent,
    PositionModifyEvent,
    PositionsSnapshotEvent,
)
from supervisor.worker.common import add_warning, quantize, set_quality
from supervisor.worker.excursions import compute_excursion
from supervisor.worker.outcomes import (
    ORPHAN_PREFIX,
    PROCESSED,
    WAITING_PREFIX,
    Deferred,
    Outcome,
    ProcessingError,
)

ORDER_TYPE_UNKNOWN = "UNKNOWN"
LEVEL_EVENTS = (TradeEventType.TRADE_OPENED, TradeEventType.SL_MODIFIED, TradeEventType.TP_MODIFIED)
EXIT_ENTRIES = (DealEntry.OUT.value, DealEntry.OUT_BY.value, DealEntry.INOUT.value)
# Un balance más antiguo que esto respecto a la entrada se usa, pero se avisa.
BALANCE_MAX_AGE = timedelta(minutes=10)

_REASON_MAP = {
    "SL": ExitReason.SL,
    "TP": ExitReason.TP,
    "EXPERT": ExitReason.EXPERT,
    "CLIENT": ExitReason.MANUAL,
    "MOBILE": ExitReason.MANUAL,
    "WEB": ExitReason.MANUAL,
    "SO": ExitReason.STOP_OUT,
}


@dataclass(frozen=True)
class AccountInfo:
    account_id: uuid.UUID
    broker_id: uuid.UUID
    login: int
    account_type: AccountType


# Utilidades ---------------------------------------------------------------------------------


def exit_reason(reasons: list[str]) -> ExitReason | None:
    """Motivo de salida a partir del DEAL_REASON de cada deal de salida. Si los deals de salida
    tienen motivos distintos (p. ej. parcial del EA y resto por SL) el resultado es MIXED."""
    if not reasons:
        return None
    mapped = {_REASON_MAP.get(reason, ExitReason.UNKNOWN) for reason in reasons}
    return mapped.pop() if len(mapped) == 1 else ExitReason.MIXED


def trade_source(account_type: AccountType) -> TradeSource:
    return TradeSource.LIVE if account_type == AccountType.REAL else TradeSource.DEMO


def server_time(msc: int) -> datetime:
    """Hora del servidor del broker tal como la da MT5, sin zona."""
    return datetime.fromtimestamp(msc / 1000, UTC).replace(tzinfo=None)


def _dec(value: Any) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal(0)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


# Consultas ----------------------------------------------------------------------------------


def lock_trade(session: Session, account_id: uuid.UUID, position_id: int) -> Trade | None:
    return session.scalar(
        select(Trade)
        .where(Trade.account_id == account_id, Trade.position_id == position_id)
        .with_for_update()
    )


def deal_events(session: Session, trade: Trade) -> list[TradeEvent]:
    return list(
        session.scalars(
            select(TradeEvent)
            .where(TradeEvent.trade_id == trade.trade_id, TradeEvent.deal_ticket.is_not(None))
            .order_by(TradeEvent.event_time_utc, TradeEvent.deal_ticket)
        )
    )


def _has_events_from(session: Session, trade: Trade, raw_event_id: int) -> bool:
    return (
        session.scalar(
            select(TradeEvent.event_id)
            .where(TradeEvent.trade_id == trade.trade_id, TradeEvent.raw_event_id == raw_event_id)
            .limit(1)
        )
        is not None
    )


def levels_at(session: Session, trade: Trade, when: datetime) -> tuple[Decimal | None, ...]:
    """SL y TP vigentes en un momento: el último evento de apertura o modificación hasta esa
    hora. Cada uno de esos eventos guarda el estado completo (sl y tp) tras el cambio."""
    row = session.execute(
        select(TradeEvent.sl, TradeEvent.tp)
        .where(
            TradeEvent.trade_id == trade.trade_id,
            TradeEvent.event_type.in_(LEVEL_EVENTS),
            TradeEvent.event_time_utc <= when,
        )
        .order_by(TradeEvent.event_time_utc.desc(), TradeEvent.created_at.desc())
        .limit(1)
    ).first()
    return (row.sl, row.tp) if row else (None, None)


def _refresh_current_levels(session: Session, trade: Trade) -> None:
    far_future = datetime(9999, 1, 1, tzinfo=UTC)
    trade.current_sl, trade.current_tp = levels_at(session, trade, far_future)


def _insert_event(session: Session, **values: Any) -> bool:
    """Inserta un trade_event. Un deal repetido en la misma operación se descarta."""
    values.setdefault("extra", {})
    stmt = insert(TradeEvent).values(**values)
    if values.get("deal_ticket") is not None:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["trade_id", "deal_ticket"],
            index_where=TradeEvent.deal_ticket.is_not(None),
        )
    return session.execute(stmt.returning(TradeEvent.event_id)).scalar() is not None


# Asignación, riesgo y reentrada ---------------------------------------------------------------


def resolve_deployment(session: Session, trade: Trade) -> None:
    """Asigna el despliegue activo de cuenta + magic + símbolo a la hora de entrada."""
    deployment = session.scalar(
        select(Deployment)
        .where(
            Deployment.account_id == trade.account_id,
            Deployment.magic_number == trade.magic_number,
            Deployment.symbol == trade.symbol,
            Deployment.started_at <= trade.entry_time,
            or_(Deployment.ended_at.is_(None), Deployment.ended_at > trade.entry_time),
        )
        .order_by(Deployment.started_at.desc())
        .limit(1)
    )
    if deployment is not None:
        trade.deployment_id = deployment.id
        trade.bot_version_id = deployment.bot_version_id
        set_quality(trade, "asignacion", None)
        return
    trade.deployment_id = None
    trade.bot_version_id = None
    detail = (
        "magic 0: operación manual o de un EA sin magic"
        if trade.magic_number == 0
        else f"ningún despliegue activo del magic {trade.magic_number} en {trade.symbol} "
        "a la hora de entrada"
    )
    set_quality(trade, "asignacion", {"estado": "sin_despliegue", "detalle": detail})


def compute_risk(session: Session, trade: Trade, first_entry: TradeEvent | None) -> None:
    """Riesgo inicial = |entrada - SL| / tick_size * tick_value * volumen inicial.

    El balance es el del AccountSnapshot más cercano en o antes de la entrada; si no hay, el
    balance_after del deal de entrada (aproximado: ya descuenta la comisión de entrada)."""
    notes: dict[str, Any] = {}
    snapshot = session.execute(
        select(AccountSnapshot.balance, AccountSnapshot.ts)
        .where(
            AccountSnapshot.account_id == trade.account_id,
            AccountSnapshot.ts <= trade.entry_time,
        )
        .order_by(AccountSnapshot.ts.desc())
        .limit(1)
    ).first()
    balance: Decimal | None = None
    if snapshot is not None:
        balance = snapshot.balance
        if trade.entry_time - snapshot.ts > BALANCE_MAX_AGE:
            notes["balance"] = f"foto de cuenta antigua ({_iso(snapshot.ts)})"
    elif first_entry is not None and first_entry.extra.get("balance_after") is not None:
        balance = _dec(first_entry.extra["balance_after"])
        notes["balance"] = "sin foto de cuenta: se usa balance_after del deal de entrada"
    else:
        notes["balance"] = "sin foto de cuenta anterior a la entrada"
    trade.balance_at_entry = balance

    trade.risk_amount = None
    trade.risk_percent = None
    if trade.initial_sl is None:
        notes["estado"] = "sin_sl"
    elif not trade.tick_size or trade.tick_value is None or trade.tick_value <= 0:
        notes["estado"] = "sin_especificacion_simbolo"
    else:
        distance = abs(trade.entry_price - trade.initial_sl)
        risk = distance / trade.tick_size * trade.tick_value * trade.initial_volume
        trade.risk_amount = quantize(risk, 4)
        if balance is not None and balance > 0:
            trade.risk_percent = quantize(risk / balance * 100, 4)
        else:
            notes["estado"] = "sin_balance"
    set_quality(trade, "riesgo", notes or None)


def link_reentry(session: Session, trade: Trade, settings: Settings) -> bool:
    """Marca la operación como reentrada si la última operación cerrada antes de su entrada en
    la misma cuenta, símbolo y magic cerró por SL dentro de la ventana. Solo cuenta la última:
    si entre medias hubo otra que cerró por TP, ya no es reentrada. Es una inferencia: el
    evento REENTRY lleva is_inferred."""
    if trade.reentry_of_trade_id is not None or settings.reentry_window_minutes <= 0:
        return False
    window = timedelta(minutes=settings.reentry_window_minutes)
    previous = session.scalar(
        select(Trade)
        .where(
            Trade.account_id == trade.account_id,
            Trade.symbol == trade.symbol,
            Trade.magic_number == trade.magic_number,
            Trade.trade_id != trade.trade_id,
            Trade.status == TradeStatus.CLOSED,
            Trade.close_time <= trade.entry_time,
        )
        .order_by(Trade.close_time.desc())
        .limit(1)
    )
    if (
        previous is None
        or previous.exit_reason != ExitReason.SL
        or previous.close_time < trade.entry_time - window
    ):
        return False
    trade.reentry_of_trade_id = previous.trade_id
    minutes = (trade.entry_time - previous.close_time).total_seconds() / 60
    _insert_event(
        session,
        trade_id=trade.trade_id,
        event_type=TradeEventType.REENTRY,
        event_time_utc=trade.entry_time,
        price=trade.entry_price,
        volume=trade.initial_volume,
        is_inferred=True,
        extra={
            "operacion_previa": str(previous.trade_id),
            "posicion_previa": previous.position_id,
            "minutos_desde_cierre_por_sl": round(minutes, 2),
            "ventana_minutos": settings.reentry_window_minutes,
        },
    )
    return True


def _link_followers(session: Session, closed: Trade, settings: Settings) -> None:
    """Al cerrarse una operación por SL, revisa aperturas posteriores ya registradas (deals
    procesados fuera de orden) que caigan en la ventana de reentrada."""
    if closed.exit_reason != ExitReason.SL or settings.reentry_window_minutes <= 0:
        return
    window = timedelta(minutes=settings.reentry_window_minutes)
    followers = session.scalars(
        select(Trade)
        .where(
            Trade.account_id == closed.account_id,
            Trade.symbol == closed.symbol,
            Trade.magic_number == closed.magic_number,
            Trade.trade_id != closed.trade_id,
            Trade.reentry_of_trade_id.is_(None),
            Trade.entry_time >= closed.close_time,
            Trade.entry_time <= closed.close_time + window,
        )
        .with_for_update()
    ).all()
    for follower in followers:
        link_reentry(session, follower, settings)


# Recalcular la operación desde sus deals -------------------------------------------------------


def recompute(session: Session, trade: Trade, acc: AccountInfo, settings: Settings) -> None:
    deals = deal_events(session, trade)
    entries = [d for d in deals if d.extra.get("entry") == DealEntry.IN.value]
    if not entries:
        raise ProcessingError(f"la operación de la posición {trade.position_id} no tiene entradas")
    was_closed = trade.status == TradeStatus.CLOSED
    previous_close = trade.close_time

    running = in_volume = in_value = out_volume = out_value = Decimal(0)
    gross = commission = swap = max_volume = Decimal(0)
    reasons: list[str] = []
    last_exit: TradeEvent | None = None
    for deal in deals:
        entry = deal.extra.get("entry")
        volume, price = deal.volume or Decimal(0), deal.price or Decimal(0)
        gross += deal.profit or 0
        commission += (deal.commission or 0) + _dec(deal.extra.get("fee"))
        swap += deal.swap or 0
        if entry == DealEntry.IN.value:
            running += volume
            in_volume += volume
            in_value += volume * price
        elif entry in EXIT_ENTRIES:
            closed = min(volume, max(running, Decimal(0))) if entry == "INOUT" else volume
            running -= closed
            out_volume += closed
            out_value += closed * price
            reasons.append(deal.extra.get("reason", ""))
            last_exit = deal
        max_volume = max(max_volume, running)

    first = entries[0]
    entry_changed = (trade.entry_time, trade.entry_price) != (first.event_time_utc, first.price)
    trade.entry_time = first.event_time_utc
    trade.entry_price = first.price
    trade.initial_volume = first.volume
    trade.initial_sl = first.sl
    trade.initial_tp = first.tp
    trade.max_volume = max_volume
    trade.current_volume = max(running, Decimal(0))
    if running < 0:
        add_warning(trade, "los deals de salida suman más volumen que los de entrada")
    avg_entry = in_value / in_volume
    set_quality(
        trade,
        "precio_medio_entrada",
        str(quantize(avg_entry, 10)) if len(entries) > 1 else None,
    )

    if out_volume > 0 and running <= 0 and last_exit is not None:
        trade.status = TradeStatus.CLOSED
        trade.close_time = last_exit.event_time_utc
        trade.close_price = quantize(out_value / out_volume, 10)
        trade.gross_profit = quantize(gross, 4)
        trade.commission = quantize(commission, 4)
        trade.swap = quantize(swap, 4)
        trade.net_profit = quantize(gross + commission + swap, 4)
        trade.duration_seconds = int((trade.close_time - trade.entry_time).total_seconds())
        trade.exit_reason = exit_reason(reasons)
        sign = 1 if trade.direction == Direction.BUY else -1
        if trade.symbol_point:
            trade.points = quantize((trade.close_price - avg_entry) / trade.symbol_point * sign, 4)
        else:
            trade.points = None
            add_warning(trade, "sin point del símbolo: no se calculan puntos")
        absent = (trade.data_quality or {}).get("ausente_en_foto")
        if absent and trade.close_time <= datetime.fromisoformat(absent):
            set_quality(trade, "ausente_en_foto", None)
    else:
        trade.status = TradeStatus.OPEN
        for field in (
            "close_time",
            "close_price",
            "gross_profit",
            "commission",
            "swap",
            "net_profit",
            "points",
            "duration_seconds",
            "exit_reason",
            "mfe_points",
            "mae_points",
            "max_floating_profit",
            "max_floating_drawdown",
            "excursion_source",
        ):
            setattr(trade, field, None)
        set_quality(trade, "excursion", None)

    if entry_changed:
        resolve_deployment(session, trade)
        compute_risk(session, trade, first)
    session.flush()

    if trade.status == TradeStatus.CLOSED:
        if not was_closed or previous_close != trade.close_time:
            _ensure_closed_event(session, trade, out_volume)
            compute_excursion(session, trade, acc.broker_id)
        if not was_closed:
            _link_followers(session, trade, settings)


def _ensure_closed_event(session: Session, trade: Trade, out_volume: Decimal) -> None:
    """Si los deals llegaron desordenados, puede que ningún deal se haya registrado como
    TRADE_CLOSED; se añade uno inferido para que la vida de la operación quede completa."""
    exists = session.scalar(
        select(TradeEvent.event_id)
        .where(
            TradeEvent.trade_id == trade.trade_id,
            TradeEvent.event_type == TradeEventType.TRADE_CLOSED,
        )
        .limit(1)
    )
    if exists is None:
        _insert_event(
            session,
            trade_id=trade.trade_id,
            event_type=TradeEventType.TRADE_CLOSED,
            event_time_utc=trade.close_time,
            price=trade.close_price,
            volume=out_volume,
            is_inferred=True,
            extra={"motivo": "cierre detectado al recomponer deals recibidos fuera de orden"},
        )


# Deals --------------------------------------------------------------------------------------


def _deal_event_values(trade: Trade, raw: RawEvent, ev: DealEvent, kind: TradeEventType) -> dict:
    extra: dict[str, Any] = {
        "entry": ev.entry.value,
        "reason": ev.reason.value,
        "deal_type": ev.deal_type.value,
    }
    if ev.fee:
        extra["fee"] = str(ev.fee)
    if ev.balance_after is not None:
        extra["balance_after"] = str(ev.balance_after)
    if ev.comment:
        extra["comment"] = ev.comment
    return {
        "trade_id": trade.trade_id,
        "raw_event_id": raw.id,
        "event_type": kind,
        "event_time_utc": ev.time_utc,
        "event_time_server": server_time(ev.time_server_msc),
        "price": ev.price,
        "volume": ev.volume,
        "sl": ev.sl,
        "tp": ev.tp,
        "deal_ticket": ev.deal_ticket,
        "order_ticket": ev.order_ticket or None,
        "profit": ev.profit,
        "commission": ev.commission,
        "swap": ev.swap,
        "extra": extra,
    }


def _create_trade(session: Session, acc: AccountInfo, ev: DealEvent) -> Trade | None:
    """Crea la operación desde su deal de entrada. Devuelve None si otra transacción la creó
    a la vez (la unicidad de account_id + position_id decide)."""
    spec = ev.symbol_spec
    values = {
        "trade_id": uuid.uuid4(),
        "account_id": acc.account_id,
        "position_id": ev.position_id,
        "magic_number": ev.magic,
        "symbol": ev.symbol,
        "direction": Direction(ev.deal_type.value),
        "order_type": ORDER_TYPE_UNKNOWN,
        "source": trade_source(acc.account_type),
        "status": TradeStatus.OPEN,
        "comment": ev.comment or None,
        "symbol_digits": spec.digits if spec else None,
        "symbol_point": spec.point if spec else None,
        "tick_size": spec.tick_size if spec else None,
        "tick_value": spec.tick_value if spec else None,
        "contract_size": spec.contract_size if spec else None,
        "entry_time": ev.time_utc,
        "entry_price": ev.price,
        "initial_sl": ev.sl,
        "initial_tp": ev.tp,
        "initial_volume": ev.volume,
        "max_volume": ev.volume,
        "current_volume": ev.volume,
        "current_sl": ev.sl,
        "current_tp": ev.tp,
        "data_quality": {} if spec else {"simbolo": "el deal de entrada no trae symbol_spec"},
    }
    stmt = (
        insert(Trade)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["account_id", "position_id"])
        .returning(Trade.trade_id)
    )
    trade_id = session.execute(stmt).scalar()
    return session.get(Trade, trade_id) if trade_id else None


def wake_waiting(session: Session, acc: AccountInfo, position_id: int) -> int:
    """Reactiva los eventos de esta posición que esperaban a su apertura."""
    terminals = select(Terminal.id).where(Terminal.account_id == acc.account_id)
    stmt = (
        update(RawEvent)
        .where(
            RawEvent.terminal_id.in_(terminals),
            RawEvent.event_type.in_(("DEAL", "POSITION_MODIFY")),
            RawEvent.payload["position_id"].astext == str(position_id),
            or_(
                and_(
                    RawEvent.status == RawEventStatus.PENDING,
                    RawEvent.error.like(f"{WAITING_PREFIX}%"),
                ),
                and_(
                    RawEvent.status.in_((RawEventStatus.FAILED, RawEventStatus.IGNORED)),
                    RawEvent.error.like(f"{ORPHAN_PREFIX}%"),
                ),
            ),
        )
        .values(status=RawEventStatus.PENDING, next_attempt_at=None)
        .execution_options(synchronize_session=False)
    )
    return session.execute(stmt).rowcount


def handle_deal(
    session: Session, acc: AccountInfo, raw: RawEvent, ev: DealEvent, settings: Settings
) -> Outcome:
    trade = lock_trade(session, acc.account_id, ev.position_id)
    created = False
    if trade is None and ev.entry == DealEntry.IN:
        trade = _create_trade(session, acc, ev)
        created = trade is not None
        if trade is None:
            trade = lock_trade(session, acc.account_id, ev.position_id)
    if trade is None:
        raise Deferred(f"sin deal de entrada de la posición {ev.position_id}")

    if session.scalar(
        select(TradeEvent.event_id).where(
            TradeEvent.trade_id == trade.trade_id, TradeEvent.deal_ticket == ev.deal_ticket
        )
    ):
        return Outcome(RawEventStatus.PROCESSED, "deal ya registrado")

    reversal = (trade.data_quality or {}).get("inversion_inout")
    if reversal and ev.time_utc >= datetime.fromisoformat(reversal["hora"]):
        add_warning(trade, f"deal {ev.deal_ticket} posterior a la inversión INOUT no aplicado")
        return Outcome(
            RawEventStatus.IGNORED,
            "posición invertida por INOUT: el volumen reabierto no se registra como operación",
        )

    existing = deal_events(session, trade)
    kind, extra = _classify(trade, ev, existing, created)
    values = _deal_event_values(trade, raw, ev, kind)
    values["extra"].update(extra)
    if not _insert_event(session, **values):
        return Outcome(RawEventStatus.PROCESSED, "deal ya registrado")

    if trade.symbol_point is None and ev.symbol_spec is not None:
        spec = ev.symbol_spec
        trade.symbol_digits, trade.symbol_point = spec.digits, spec.point
        trade.tick_size, trade.tick_value = spec.tick_size, spec.tick_value
        trade.contract_size = spec.contract_size
        set_quality(trade, "simbolo", None)
    if ev.entry == DealEntry.INOUT:
        set_quality(
            trade,
            "inversion_inout",
            {
                "deal_ticket": ev.deal_ticket,
                "hora": _iso(ev.time_utc),
                "volumen_cerrado": extra["volumen_cerrado"],
                "volumen_reabierto": extra["volumen_reabierto"],
                "nueva_direccion": ev.deal_type.value,
                "nota": "inversión netting: el volumen reabierto no se registra como operación",
            },
        )

    if created:
        resolve_deployment(session, trade)
        session.flush()
        first = session.scalar(
            select(TradeEvent).where(
                TradeEvent.trade_id == trade.trade_id, TradeEvent.deal_ticket == ev.deal_ticket
            )
        )
        compute_risk(session, trade, first)
        link_reentry(session, trade, settings)
        wake_waiting(session, acc, ev.position_id)
    recompute(session, trade, acc, settings)
    return PROCESSED


def _classify(
    trade: Trade, ev: DealEvent, existing: list[TradeEvent], created: bool
) -> tuple[TradeEventType, dict]:
    key = (ev.time_utc, ev.deal_ticket)
    before = [d for d in existing if (d.event_time_utc, d.deal_ticket) < key]
    later = len(before) < len(existing)
    running = Decimal(0)
    for deal in before:
        entry = deal.extra.get("entry")
        if entry == "IN":
            running += deal.volume
        elif entry == "INOUT":
            running = Decimal(0)
        elif entry in EXIT_ENTRIES:
            running -= deal.volume
    extra: dict[str, Any] = {}
    if later:
        add_warning(trade, f"deal {ev.deal_ticket} recibido después de deals posteriores")

    if ev.entry == DealEntry.IN:
        if created:
            return TradeEventType.TRADE_OPENED, extra
        if ev.time_utc < trade.entry_time:
            add_warning(trade, f"entrada {ev.deal_ticket} anterior a la apertura registrada")
        return TradeEventType.POSITION_INCREASED, extra
    if ev.entry == DealEntry.INOUT:
        if running <= 0:
            raise ProcessingError(f"deal INOUT {ev.deal_ticket} sin volumen abierto que invertir")
        closed = min(running, ev.volume)
        extra["volumen_cerrado"] = str(closed)
        extra["volumen_reabierto"] = str(ev.volume - closed)
        return TradeEventType.TRADE_CLOSED, extra
    remaining = running - ev.volume
    extra["volumen_restante"] = str(max(remaining, Decimal(0)))
    if remaining <= 0:
        return TradeEventType.TRADE_CLOSED, extra
    return TradeEventType.PARTIAL_CLOSE, extra


# SL/TP ---------------------------------------------------------------------------------------


def record_level_change(
    session: Session,
    trade: Trade,
    raw: RawEvent,
    when: datetime,
    sl: Decimal | None,
    tp: Decimal | None,
    *,
    inferred: bool,
    when_server: datetime | None = None,
) -> bool:
    """Registra SL_MODIFIED y/o TP_MODIFIED comparando con los niveles vigentes a esa hora."""
    old_sl, old_tp = levels_at(session, trade, when)
    kinds = []
    if sl != old_sl:
        kinds.append(TradeEventType.SL_MODIFIED)
    if tp != old_tp:
        kinds.append(TradeEventType.TP_MODIFIED)
    for kind in kinds:
        _insert_event(
            session,
            trade_id=trade.trade_id,
            raw_event_id=raw.id,
            event_type=kind,
            event_time_utc=when,
            event_time_server=when_server,
            sl=sl,
            tp=tp,
            is_inferred=inferred,
            extra={
                "sl_anterior": None if old_sl is None else str(old_sl),
                "tp_anterior": None if old_tp is None else str(old_tp),
                **({"origen": "foto de posiciones"} if inferred else {}),
            },
        )
    if kinds:
        session.flush()
        _refresh_current_levels(session, trade)
    return bool(kinds)


def handle_modify(
    session: Session,
    acc: AccountInfo,
    raw: RawEvent,
    ev: PositionModifyEvent,
    settings: Settings,
) -> Outcome:
    trade = lock_trade(session, acc.account_id, ev.position_id)
    if trade is None:
        raise Deferred(f"posición {ev.position_id} desconocida", RawEventStatus.IGNORED)
    if ev.time_utc < trade.entry_time:
        return Outcome(RawEventStatus.IGNORED, "modificación anterior a la apertura")
    if trade.status == TradeStatus.CLOSED and ev.time_utc >= trade.close_time:
        return Outcome(RawEventStatus.IGNORED, "la operación ya estaba cerrada")
    if _has_events_from(session, trade, raw.id):
        return Outcome(RawEventStatus.PROCESSED, "modificación ya registrada")
    changed = record_level_change(
        session,
        trade,
        raw,
        ev.time_utc,
        ev.sl,
        ev.tp,
        inferred=False,
        when_server=server_time(ev.time_server_msc),
    )
    return PROCESSED if changed else Outcome(RawEventStatus.IGNORED, "SL/TP sin cambios")


# Foto de posiciones ----------------------------------------------------------------------------


def handle_snapshot(
    session: Session, acc: AccountInfo, raw: RawEvent, ev: PositionsSnapshotEvent
) -> Outcome:
    """Reconcilia: detecta cambios de SL/TP que el EA no vio (eventos is_inferred) y anota
    posiciones sin operación u operaciones abiertas que ya no aparecen. No crea operaciones."""
    when = ev.time_utc
    missing: list[int] = []
    seen: set[int] = set()
    for position in ev.positions:
        seen.add(position.position_id)
        trade = lock_trade(session, acc.account_id, position.position_id)
        if trade is None:
            missing.append(position.position_id)
            continue
        if when < trade.entry_time:
            continue
        if trade.status == TradeStatus.CLOSED:
            if when >= trade.close_time:
                add_warning(trade, f"la foto de posiciones de {_iso(when)} aún la muestra abierta")
            continue
        if _has_events_from(session, trade, raw.id):
            continue
        record_level_change(session, trade, raw, when, position.sl, position.tp, inferred=True)

    absent = session.scalars(
        select(Trade)
        .where(
            Trade.account_id == acc.account_id,
            Trade.status == TradeStatus.OPEN,
            Trade.entry_time < when,
            Trade.position_id.not_in(seen) if seen else true(),
        )
        .with_for_update()
    ).all()
    for trade in absent:
        if not (trade.data_quality or {}).get("ausente_en_foto"):
            set_quality(trade, "ausente_en_foto", _iso(when))

    if missing:
        listed = ", ".join(str(p) for p in sorted(missing)[:50])
        return Outcome(RawEventStatus.PROCESSED, f"aviso: posiciones sin operación: {listed}")
    return PROCESSED
