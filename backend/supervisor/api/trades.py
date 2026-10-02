"""Consulta de operaciones y su historia (requiere token de administración). Solo lectura."""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from supervisor.api.deps import SessionDep, admin_auth
from supervisor.models.enums import TradeStatus
from supervisor.schemas.trades import TradeDetail, TradeEventOut, TradeOut
from supervisor.services import trades
from supervisor.services.trades import TradeFilters

router = APIRouter(prefix="/v1", tags=["operaciones"], dependencies=[Depends(admin_auth)])


def _utc(value: datetime | None) -> datetime | None:
    """Una fecha sin zona horaria se interpreta como UTC."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


@router.get("/trades", response_model=list[TradeOut])
def list_trades(
    session: SessionDep,
    bot_id: uuid.UUID | None = None,
    version_id: uuid.UUID | None = None,
    symbol: Annotated[str | None, Query(max_length=64)] = None,
    status_filter: Annotated[TradeStatus | None, Query(alias="status")] = None,
    date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Operaciones por hora de entrada descendente; `from`/`to` filtran por hora de entrada."""
    filters = TradeFilters(
        bot_id=bot_id,
        version_id=version_id,
        symbol=symbol,
        status=status_filter,
        date_from=_utc(date_from),
        date_to=_utc(date_to),
    )
    return trades.list_trades(session, filters, limit, offset)


@router.get("/trades/{trade_id}", response_model=TradeDetail)
def get_trade(trade_id: uuid.UUID, session: SessionDep):
    trade, events = trades.get_trade(session, trade_id)
    return TradeDetail(
        **TradeOut.model_validate(trade).model_dump(),
        events=[TradeEventOut.model_validate(e) for e in events],
    )
