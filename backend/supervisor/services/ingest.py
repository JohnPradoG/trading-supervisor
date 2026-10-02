"""Ingesta desde el EA Monitor: valida, deduplica y guarda en crudo.

Este módulo no interpreta operaciones (eso lo hace supervisor.worker). Su único trabajo es
que cada hecho enviado por MT5 quede guardado exactamente una vez, y responder al EA qué
pasó con cada evento para que lo saque de su cola local.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from supervisor.config import Settings
from supervisor.models import AccountSnapshot, Heartbeat, PriceBar, RawEvent, Terminal
from supervisor.schemas.ingest import (
    SCHEMA_VERSION,
    AccountSnapshotIn,
    BarsIn,
    BatchResult,
    EventBatch,
    EventResult,
    EventStatus,
    HeartbeatIn,
    SimpleIngestResult,
    trading_event_adapter,
)
from supervisor.services.errors import ServiceError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TerminalContext:
    """Identidad resuelta a partir de la API key: a qué terminal y cuenta pertenece."""

    api_client_id: uuid.UUID
    terminal_id: uuid.UUID
    terminal_name: str
    account_id: uuid.UUID
    account_login: int
    broker_id: uuid.UUID


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(p) for p in err["loc"])
        parts.append(f"{loc}: {err['msg']}" if loc else err["msg"])
    return "; ".join(parts)[:500]


def _check_time(value: datetime, settings: Settings, now: datetime) -> None:
    if value > now + timedelta(seconds=settings.max_future_skew_seconds):
        raise ServiceError("hora en el futuro: revisa el reloj del servidor o TimeGMT()")


def ingest_events(
    session: Session, ctx: TerminalContext, batch: EventBatch, settings: Settings
) -> BatchResult:
    if len(batch.events) > settings.ingest_max_batch:
        raise ServiceError(f"lote demasiado grande: máximo {settings.ingest_max_batch} eventos")

    now = datetime.now(UTC)
    results: list[EventResult] = []
    for index, raw in enumerate(batch.events):
        try:
            event = trading_event_adapter.validate_python(raw)
            if event.account_login != ctx.account_login:
                raise ServiceError(
                    f"account_login {event.account_login} no corresponde a este terminal"
                )
            _check_time(event.time_utc, settings, now)
        except ValidationError as exc:
            results.append(
                EventResult(
                    index=index, status=EventStatus.REJECTED, error=_validation_message(exc)
                )
            )
            continue
        except ServiceError as exc:
            results.append(EventResult(index=index, status=EventStatus.REJECTED, error=exc.message))
            continue

        key = event.idempotency_key()
        stmt = (
            insert(RawEvent)
            .values(
                terminal_id=ctx.terminal_id,
                idempotency_key=key,
                event_type=event.type,
                schema_version=SCHEMA_VERSION,
                payload=event.model_dump(mode="json"),
                sent_at=batch.sent_at,
                event_time=event.time_utc,
            )
            .on_conflict_do_nothing(index_elements=["terminal_id", "idempotency_key"])
            .returning(RawEvent.id)
        )
        inserted = session.execute(stmt).scalar() is not None
        results.append(
            EventResult(
                index=index,
                status=EventStatus.ACCEPTED if inserted else EventStatus.DUPLICATE,
                idempotency_key=key,
            )
        )

    counts = {s: sum(1 for r in results if r.status == s) for s in EventStatus}
    rejected = [r for r in results if r.status == EventStatus.REJECTED]
    log.info(
        "lote de eventos",
        extra={
            "terminal": ctx.terminal_name,
            "accepted": counts[EventStatus.ACCEPTED],
            "duplicate": counts[EventStatus.DUPLICATE],
            "rejected": counts[EventStatus.REJECTED],
        },
    )
    for r in rejected:
        log.warning(
            "evento rechazado",
            extra={"terminal": ctx.terminal_name, "index": r.index, "error": r.error},
        )
    return BatchResult(
        accepted=counts[EventStatus.ACCEPTED],
        duplicate=counts[EventStatus.DUPLICATE],
        rejected=counts[EventStatus.REJECTED],
        results=results,
    )


def ingest_heartbeat(
    session: Session, ctx: TerminalContext, hb: HeartbeatIn, settings: Settings
) -> SimpleIngestResult:
    _check_time(hb.sent_at, settings, datetime.now(UTC))
    stmt = (
        insert(Heartbeat)
        .values(
            terminal_id=ctx.terminal_id,
            ts=hb.sent_at,
            ea_version=hb.ea_version,
            terminal_connected=hb.terminal_connected,
            trade_allowed=hb.trade_allowed,
            info=hb.info,
        )
        .on_conflict_do_nothing()
        .returning(Heartbeat.ts)
    )
    inserted = session.execute(stmt).scalar() is not None
    return SimpleIngestResult(received=1, inserted=int(inserted))


def ingest_account_snapshot(
    session: Session, ctx: TerminalContext, snap: AccountSnapshotIn, settings: Settings
) -> SimpleIngestResult:
    if snap.account_login != ctx.account_login:
        raise ServiceError(f"account_login {snap.account_login} no corresponde a este terminal")
    _check_time(snap.time_utc, settings, datetime.now(UTC))
    stmt = (
        insert(AccountSnapshot)
        .values(
            account_id=ctx.account_id,
            ts=snap.time_utc,
            balance=snap.balance,
            equity=snap.equity,
            margin=snap.margin,
            free_margin=snap.free_margin,
            open_positions=snap.open_positions,
        )
        .on_conflict_do_nothing()
        .returning(AccountSnapshot.ts)
    )
    inserted = session.execute(stmt).scalar() is not None
    return SimpleIngestResult(received=1, inserted=int(inserted))


def ingest_bars(
    session: Session, ctx: TerminalContext, payload: BarsIn, settings: Settings
) -> SimpleIngestResult:
    if len(payload.bars) > settings.ingest_max_bars:
        raise ServiceError(f"demasiadas velas: máximo {settings.ingest_max_bars} por envío")
    now = datetime.now(UTC)
    for bar in payload.bars:
        _check_time(bar.time_utc, settings, now)
    rows = [
        {
            "broker_id": ctx.broker_id,
            "symbol": payload.symbol,
            "timeframe": payload.timeframe,
            "bar_time": bar.time_utc,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "tick_volume": bar.tick_volume,
            "spread": bar.spread,
        }
        for bar in payload.bars
    ]
    # Las velas cerradas no cambian: si ya existe, se conserva la primera versión recibida.
    stmt = insert(PriceBar).values(rows).on_conflict_do_nothing().returning(PriceBar.bar_time)
    inserted = len(session.execute(stmt).all())
    return SimpleIngestResult(received=len(rows), inserted=inserted)


def touch_terminal(session: Session, terminal_id: uuid.UUID) -> None:
    session.execute(
        update(Terminal).where(Terminal.id == terminal_id).values(last_seen_at=datetime.now(UTC))
    )
