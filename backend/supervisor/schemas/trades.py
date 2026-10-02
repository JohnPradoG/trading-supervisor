"""Esquemas de consulta de operaciones (trades) y su historia (trade_events)."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from supervisor.models.enums import (
    Direction,
    ExcursionSource,
    ExitReason,
    TradeEventType,
    TradeSource,
    TradeStatus,
)


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TradeOut(_Out):
    trade_id: uuid.UUID
    account_id: uuid.UUID
    deployment_id: uuid.UUID | None
    bot_version_id: uuid.UUID | None
    position_id: int
    magic_number: int
    symbol: str
    direction: Direction
    order_type: str
    source: TradeSource
    status: TradeStatus
    comment: str | None
    symbol_digits: int | None
    symbol_point: Decimal | None
    tick_size: Decimal | None
    tick_value: Decimal | None
    contract_size: Decimal | None
    entry_time: datetime
    entry_price: Decimal
    initial_sl: Decimal | None
    initial_tp: Decimal | None
    initial_volume: Decimal
    max_volume: Decimal
    current_volume: Decimal | None
    current_sl: Decimal | None
    current_tp: Decimal | None
    risk_amount: Decimal | None
    risk_percent: Decimal | None
    balance_at_entry: Decimal | None
    close_time: datetime | None
    close_price: Decimal | None
    gross_profit: Decimal | None
    commission: Decimal | None
    swap: Decimal | None
    net_profit: Decimal | None
    points: Decimal | None
    duration_seconds: int | None
    exit_reason: ExitReason | None
    mfe_points: Decimal | None
    mae_points: Decimal | None
    max_floating_profit: Decimal | None
    max_floating_drawdown: Decimal | None
    excursion_source: ExcursionSource | None
    reentry_of_trade_id: uuid.UUID | None
    data_quality: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class TradeEventOut(_Out):
    event_id: uuid.UUID
    raw_event_id: int | None
    event_type: TradeEventType
    event_time_utc: datetime
    event_time_server: datetime | None
    price: Decimal | None
    volume: Decimal | None
    sl: Decimal | None
    tp: Decimal | None
    deal_ticket: int | None
    order_ticket: int | None
    profit: Decimal | None
    commission: Decimal | None
    swap: Decimal | None
    is_inferred: bool
    extra: dict[str, Any]
    created_at: datetime


class TradeDetail(TradeOut):
    events: list[TradeEventOut]
