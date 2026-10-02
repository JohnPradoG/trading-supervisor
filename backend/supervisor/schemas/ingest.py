"""Contrato de datos entre el EA Monitor y la API (schema_version 1).

El EA envía hechos tal como MT5 los reporta. Las reglas que aplica la API:
- Cada evento se valida por separado: uno inválido no tumba el lote.
- La clave de idempotencia la calcula el servidor a partir del contenido, nunca la decide el
  cliente, así el mismo hecho produce siempre la misma clave.
- En MT5, SL/TP = 0 significa "sin SL/TP"; aquí se normaliza a None.
"""

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = 1


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("la hora debe incluir zona horaria (UTC)")
    value = value.astimezone(UTC)
    if value.year < 2000:
        raise ValueError("hora anterior al año 2000")
    return value


UtcDatetime = Annotated[datetime, AfterValidator(_aware_utc)]
Symbol = Annotated[str, Field(min_length=1, max_length=64)]
Login = Annotated[int, Field(gt=0)]
TicketId = Annotated[int, Field(gt=0)]
PositivePrice = Annotated[Decimal, Field(gt=0, max_digits=24, decimal_places=10)]
Amount = Annotated[Decimal, Field(max_digits=20, decimal_places=4)]


def _zero_is_none(value: Decimal | None) -> Decimal | None:
    return None if value is None or value == 0 else value


OptionalLevel = Annotated[
    Decimal | None,
    Field(ge=0, max_digits=24, decimal_places=10),
    AfterValidator(_zero_is_none),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)


class DealType(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class DealEntry(StrEnum):
    IN = "IN"
    OUT = "OUT"
    INOUT = "INOUT"
    OUT_BY = "OUT_BY"


class DealReason(StrEnum):
    CLIENT = "CLIENT"
    MOBILE = "MOBILE"
    WEB = "WEB"
    EXPERT = "EXPERT"
    SL = "SL"
    TP = "TP"
    SO = "SO"
    ROLLOVER = "ROLLOVER"
    VMARGIN = "VMARGIN"
    SPLIT = "SPLIT"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    # Motivo que el EA no reconoce (builds futuros de MT5): se guarda sin inventar uno.
    OTHER = "OTHER"


class SymbolSpec(StrictModel):
    digits: int = Field(ge=0, le=10)
    point: PositivePrice
    tick_size: PositivePrice
    tick_value: Amount
    contract_size: Annotated[Decimal, Field(gt=0, max_digits=20, decimal_places=4)]


class _TradeEventBase(StrictModel):
    account_login: Login
    symbol: Symbol
    magic: int = Field(ge=0)
    position_id: TicketId
    time_utc: UtcDatetime
    # Hora del servidor del broker en milisegundos (TimeCurrent / DEAL_TIME_MSC).
    time_server_msc: int = Field(gt=0)


class DealEvent(_TradeEventBase):
    """Un deal de MT5 (HistoryDealGet*). Cubre aperturas, aumentos, parciales y cierres."""

    type: Literal["DEAL"]
    deal_ticket: TicketId
    order_ticket: int = Field(ge=0)
    deal_type: DealType
    entry: DealEntry
    reason: DealReason
    volume: Annotated[Decimal, Field(gt=0, max_digits=16, decimal_places=4)]
    price: PositivePrice
    profit: Amount = Decimal(0)
    commission: Amount = Decimal(0)
    swap: Amount = Decimal(0)
    fee: Amount = Decimal(0)
    comment: str = Field(default="", max_length=64)
    # SL/TP de la posición en el momento del deal (0 = sin nivel).
    sl: OptionalLevel = None
    tp: OptionalLevel = None
    balance_after: Amount | None = None
    symbol_spec: SymbolSpec | None = None

    def idempotency_key(self) -> str:
        return f"deal:{self.account_login}:{self.deal_ticket}"


class PositionModifyEvent(_TradeEventBase):
    """Cambio de SL y/o TP de una posición abierta."""

    type: Literal["POSITION_MODIFY"]
    sl: OptionalLevel = None
    tp: OptionalLevel = None

    def idempotency_key(self) -> str:
        return (
            f"mod:{self.account_login}:{self.position_id}:{self.time_server_msc}:"
            f"{_fmt(self.sl)}:{_fmt(self.tp)}"
        )


class OpenPosition(StrictModel):
    position_id: TicketId
    symbol: Symbol
    magic: int = Field(ge=0)
    direction: DealType
    volume: Annotated[Decimal, Field(gt=0, max_digits=16, decimal_places=4)]
    price_open: PositivePrice
    sl: OptionalLevel = None
    tp: OptionalLevel = None
    time_open_utc: UtcDatetime
    profit: Amount = Decimal(0)


class PositionsSnapshotEvent(StrictModel):
    """Foto de las posiciones abiertas para reconciliar eventos perdidos."""

    type: Literal["POSITIONS_SNAPSHOT"]
    account_login: Login
    time_utc: UtcDatetime
    positions: list[OpenPosition] = Field(max_length=1000)

    def idempotency_key(self) -> str:
        return f"possnap:{self.account_login}:{self.time_utc:%Y%m%dT%H%M}"


def _fmt(value: Decimal | None) -> str:
    return "0" if value is None else format(value.normalize(), "f")


TradingEvent = Annotated[
    DealEvent | PositionModifyEvent | PositionsSnapshotEvent, Field(discriminator="type")
]
trading_event_adapter: TypeAdapter[TradingEvent] = TypeAdapter(TradingEvent)


class EventBatch(BaseModel):
    """Lote enviado por el EA. Los eventos se validan uno por uno en el servicio.

    `origin` es opcional para que el EA siga siendo compatible: sin él, el lote es del EA en
    vivo; el script de importación de historial envía "history_import" y las operaciones que
    nacen de esos eventos quedan marcadas con data_quality.importado."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    sent_at: UtcDatetime
    ea_version: str = Field(min_length=1, max_length=32)
    origin: Literal["ea", "history_import"] = "ea"
    events: list[dict] = Field(min_length=1)


class EventStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


class EventResult(BaseModel):
    index: int
    status: EventStatus
    idempotency_key: str | None = None
    error: str | None = None


class BatchResult(BaseModel):
    accepted: int
    duplicate: int
    rejected: int
    results: list[EventResult]


class HeartbeatIn(StrictModel):
    sent_at: UtcDatetime
    ea_version: str = Field(min_length=1, max_length=32)
    terminal_connected: bool
    trade_allowed: bool
    info: dict = Field(default_factory=dict)

    @field_validator("info")
    @classmethod
    def _small(cls, value: dict) -> dict:
        if len(str(value)) > 4000:
            raise ValueError("info demasiado grande")
        return value


class AccountSnapshotIn(StrictModel):
    account_login: Login
    time_utc: UtcDatetime
    balance: Amount
    equity: Amount
    margin: Amount | None = None
    free_margin: Amount | None = None
    open_positions: int = Field(ge=0)


class BarIn(StrictModel):
    time_utc: UtcDatetime
    open: PositivePrice
    high: PositivePrice
    low: PositivePrice
    close: PositivePrice
    tick_volume: int = Field(ge=0)
    spread: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _ohlc_consistent(self) -> "BarIn":
        if not (self.low <= min(self.open, self.close) and self.high >= max(self.open, self.close)):
            raise ValueError("vela inconsistente: low/high no contienen open/close")
        return self


class BarsIn(StrictModel):
    symbol: Symbol
    timeframe: Literal["M1"]
    bars: list[BarIn] = Field(min_length=1)


class SimpleIngestResult(BaseModel):
    received: int
    inserted: int
