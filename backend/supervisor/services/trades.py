"""Consulta de operaciones registradas por el worker (solo lectura)."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from supervisor.models import BotVersion, RawEvent, Trade, TradeEvent
from supervisor.models.enums import RawEventStatus, TradeStatus
from supervisor.services.errors import NotFound


@dataclass(frozen=True)
class TradeFilters:
    bot_id: uuid.UUID | None = None
    version_id: uuid.UUID | None = None
    symbol: str | None = None
    status: TradeStatus | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None


def list_trades(
    session: Session, filters: TradeFilters, limit: int = 100, offset: int = 0
) -> list[Trade]:
    """Operaciones por hora de entrada descendente. from/to filtran por entry_time [from, to)."""
    stmt = select(Trade)
    if filters.bot_id is not None:
        stmt = stmt.join(BotVersion, Trade.bot_version_id == BotVersion.id).where(
            BotVersion.bot_id == filters.bot_id
        )
    if filters.version_id is not None:
        stmt = stmt.where(Trade.bot_version_id == filters.version_id)
    if filters.symbol:
        stmt = stmt.where(Trade.symbol == filters.symbol)
    if filters.status is not None:
        stmt = stmt.where(Trade.status == filters.status)
    if filters.date_from is not None:
        stmt = stmt.where(Trade.entry_time >= filters.date_from)
    if filters.date_to is not None:
        stmt = stmt.where(Trade.entry_time < filters.date_to)
    stmt = stmt.order_by(Trade.entry_time.desc(), Trade.trade_id).limit(limit).offset(offset)
    return list(session.scalars(stmt))


def get_trade(session: Session, trade_id: uuid.UUID) -> tuple[Trade, list[TradeEvent]]:
    trade = session.get(Trade, trade_id)
    if trade is None:
        raise NotFound("operación no encontrada")
    events = session.scalars(
        select(TradeEvent)
        .where(TradeEvent.trade_id == trade_id)
        .order_by(TradeEvent.event_time_utc, TradeEvent.created_at, TradeEvent.deal_ticket)
    ).all()
    return trade, list(events)


@dataclass(frozen=True)
class Summary:
    trades_by_status: dict[str, int]
    unassigned: int
    raw_by_status: dict[str, int]
    last_processed: RawEvent | None


def summary(session: Session) -> Summary:
    by_status = dict(
        session.execute(select(Trade.status, func.count()).group_by(Trade.status)).tuples().all()
    )
    unassigned = session.scalar(
        select(func.count()).select_from(Trade).where(Trade.deployment_id.is_(None))
    )
    raw = dict(
        session.execute(select(RawEvent.status, func.count()).group_by(RawEvent.status))
        .tuples()
        .all()
    )
    last = session.scalar(
        select(RawEvent)
        .where(RawEvent.processed_at.is_not(None))
        .order_by(RawEvent.processed_at.desc(), RawEvent.id.desc())
        .limit(1)
    )
    return Summary(
        trades_by_status={s.value: by_status.get(s, 0) for s in TradeStatus},
        unassigned=unassigned or 0,
        raw_by_status={s.value: raw.get(s, 0) for s in RawEventStatus},
        last_processed=last,
    )
