"""Grupo C: ingesta cruda, operaciones, eventos de operación y datos de mercado/cuenta.

Regla central: todo lo que llega del EA se guarda primero en raw_events, tal cual y una sola
vez (idempotencia). trades y trade_events se derivan de ahí y se pueden reconstruir.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from supervisor.models.base import (
    Base,
    CreatedAt,
    Json,
    Money,
    Points,
    Price,
    ShortStr,
    Ticket,
    UTCDateTime,
    UUIDPk,
    Volume,
    str_enum,
)
from supervisor.models.enums import (
    Direction,
    ExcursionSource,
    ExitReason,
    RawEventStatus,
    TradeEventType,
    TradeSource,
    TradeStatus,
)


class RawEvent(Base):
    """Registro inmutable de cada mensaje aceptado del EA. Solo cambian status/processed_at/
    error/attempts/next_attempt_at (lo garantiza un trigger en la base de datos)."""

    __tablename__ = "raw_events"
    __table_args__ = (
        UniqueConstraint("terminal_id", "idempotency_key"),
        Index(
            "ix_raw_events_pending",
            "event_time",
            "id",
            postgresql_where=text("status = 'PENDING'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    terminal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("terminals.id"))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    event_type: Mapped[str] = mapped_column(String(48))
    schema_version: Mapped[int] = mapped_column(SmallInteger)
    payload: Mapped[Json]
    sent_at: Mapped[UTCDateTime]
    # Hora del hecho en MT5 (time_utc del evento): el worker procesa en este orden.
    event_time: Mapped[UTCDateTime | None]
    received_at: Mapped[CreatedAt]
    status: Mapped[RawEventStatus] = mapped_column(
        str_enum(RawEventStatus), server_default=RawEventStatus.PENDING.value
    )
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    processed_at: Mapped[UTCDateTime | None]
    error: Mapped[str | None] = mapped_column(Text)
    # Evento diferido (p. ej. cierre recibido antes que la apertura): no se reintenta antes.
    next_attempt_at: Mapped[UTCDateTime | None]


class Trade(Base):
    """Una operación = una posición de MT5 (position_id), desde la apertura hasta volumen 0."""

    __tablename__ = "trades"
    __table_args__ = (
        UniqueConstraint("account_id", "position_id"),
        CheckConstraint("status = 'OPEN' OR close_time IS NOT NULL", name="closed_has_close_time"),
        CheckConstraint("close_time IS NULL OR close_time >= entry_time", name="time_order"),
        CheckConstraint("initial_volume > 0", name="positive_volume"),
        Index("ix_trades_version_entry", "bot_version_id", "entry_time"),
        Index("ix_trades_symbol_entry", "symbol", "entry_time"),
        Index("ix_trades_open", "account_id", postgresql_where=text("status = 'OPEN'")),
        # Estadísticas y repaso del análisis: operaciones cerradas por hora de cierre.
        Index("ix_trades_closed", "close_time", postgresql_where=text("status = 'CLOSED'")),
    )

    trade_id: Mapped[UUIDPk]
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"))
    # NULL mientras el magic no está asignado a ningún despliegue (se alerta, no se pierde).
    deployment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("deployments.id"))
    bot_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bot_versions.id"))
    position_id: Mapped[Ticket]
    magic_number: Mapped[int] = mapped_column(BigInteger)
    symbol: Mapped[str] = mapped_column(String(64))
    direction: Mapped[Direction] = mapped_column(str_enum(Direction))
    order_type: Mapped[str] = mapped_column(String(32))
    source: Mapped[TradeSource] = mapped_column(str_enum(TradeSource))
    status: Mapped[TradeStatus] = mapped_column(
        str_enum(TradeStatus), server_default=TradeStatus.OPEN.value
    )
    comment: Mapped[str | None] = mapped_column(String(64))

    # Especificación del símbolo en el momento de la operación (difiere entre brokers).
    symbol_digits: Mapped[int | None] = mapped_column(SmallInteger)
    symbol_point: Mapped[Price | None]
    tick_size: Mapped[Price | None]
    tick_value: Mapped[Money | None]
    contract_size: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))

    # Entrada
    entry_time: Mapped[UTCDateTime]
    entry_price: Mapped[Price]
    initial_sl: Mapped[Price | None]
    initial_tp: Mapped[Price | None]
    initial_volume: Mapped[Volume]
    max_volume: Mapped[Volume]
    # Riesgo calculado desde SL inicial, volumen y balance. NULL si abrió sin SL.
    risk_amount: Mapped[Money | None]
    risk_percent: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    balance_at_entry: Mapped[Money | None]

    # Estado vivo de la posición: volumen abierto y último SL/TP conocido.
    current_volume: Mapped[Volume | None]
    current_sl: Mapped[Price | None]
    current_tp: Mapped[Price | None]

    # Cierre
    close_time: Mapped[UTCDateTime | None]
    close_price: Mapped[Price | None]  # media ponderada por volumen de los deals de salida
    gross_profit: Mapped[Money | None]
    commission: Mapped[Money | None]
    swap: Mapped[Money | None]
    net_profit: Mapped[Money | None]
    points: Mapped[Points | None]
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    exit_reason: Mapped[ExitReason | None] = mapped_column(str_enum(ExitReason))

    # Excursiones
    mfe_points: Mapped[Points | None]
    mae_points: Mapped[Points | None]
    max_floating_profit: Mapped[Money | None]
    max_floating_drawdown: Mapped[Money | None]
    excursion_source: Mapped[ExcursionSource | None] = mapped_column(str_enum(ExcursionSource))

    # Reentrada: inferida por ventana de tiempo, por eso se marca aparte.
    reentry_of_trade_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("trades.trade_id"))
    data_quality: Mapped[Json]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )

    events: Mapped[list["TradeEvent"]] = relationship(
        back_populates="trade", order_by="TradeEvent.event_time_utc"
    )


class TradeEvent(Base):
    """Cada hecho en la vida de una operación. Inmutable (trigger)."""

    __tablename__ = "trade_events"
    __table_args__ = (
        # Un deal de MT5 solo puede aparecer una vez en una operación.
        Index(
            "uq_trade_events_trade_id_deal_ticket",
            "trade_id",
            "deal_ticket",
            unique=True,
            postgresql_where=text("deal_ticket IS NOT NULL"),
        ),
        Index("ix_trade_events_trade_time", "trade_id", "event_time_utc"),
    )

    event_id: Mapped[UUIDPk]
    trade_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trades.trade_id"))
    raw_event_id: Mapped[int | None] = mapped_column(ForeignKey("raw_events.id"))
    event_type: Mapped[TradeEventType] = mapped_column(str_enum(TradeEventType))
    event_time_utc: Mapped[UTCDateTime]
    # Hora del servidor del broker tal como la reporta MT5 (sin zona), para auditoría.
    event_time_server: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    price: Mapped[Price | None]
    volume: Mapped[Volume | None]
    sl: Mapped[Price | None]
    tp: Mapped[Price | None]
    deal_ticket: Mapped[Ticket | None]
    order_ticket: Mapped[Ticket | None]
    profit: Mapped[Money | None]
    commission: Mapped[Money | None]
    swap: Mapped[Money | None]
    is_inferred: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    extra: Mapped[Json]
    created_at: Mapped[CreatedAt]

    trade: Mapped[Trade] = relationship(back_populates="events")


class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"

    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    balance: Mapped[Money]
    equity: Mapped[Money]
    margin: Mapped[Money | None]
    free_margin: Mapped[Money | None]
    open_positions: Mapped[int] = mapped_column(Integer)


class Heartbeat(Base):
    __tablename__ = "heartbeats"

    terminal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("terminals.id"), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    ea_version: Mapped[ShortStr]
    terminal_connected: Mapped[bool] = mapped_column(Boolean)
    trade_allowed: Mapped[bool] = mapped_column(Boolean)
    info: Mapped[Json]


class PriceBar(Base):
    """Velas cerradas. M1 llega del EA; los demás timeframes se agregan desde M1."""

    __tablename__ = "price_bars"
    __table_args__ = (
        CheckConstraint("high >= low", name="high_low"),
        CheckConstraint("high >= open AND high >= close", name="high_bounds"),
        CheckConstraint("low <= open AND low <= close", name="low_bounds"),
    )

    broker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("brokers.id"), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(8), primary_key=True)
    bar_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Price]
    high: Mapped[Price]
    low: Mapped[Price]
    close: Mapped[Price]
    tick_volume: Mapped[int] = mapped_column(BigInteger)
    spread: Mapped[int | None] = mapped_column(Integer)
