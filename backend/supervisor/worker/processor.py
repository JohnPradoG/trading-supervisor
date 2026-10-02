"""Bucle de procesamiento de raw_events: selección, despacho y estado de cada evento."""

import logging
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from supervisor.config import Settings
from supervisor.models import Account, RawEvent, Terminal
from supervisor.models.enums import RawEventStatus
from supervisor.schemas.ingest import (
    SCHEMA_VERSION,
    DealEvent,
    PositionModifyEvent,
    PositionsSnapshotEvent,
    trading_event_adapter,
)
from supervisor.worker.outcomes import (
    ORPHAN_PREFIX,
    WAITING_PREFIX,
    Deferred,
    Outcome,
    ProcessingError,
)
from supervisor.worker.trades import AccountInfo, handle_deal, handle_modify, handle_snapshot

log = logging.getLogger("supervisor.worker")

DEFERRED = "DEFERRED"
# Errores de conexión: no son culpa del evento. Se aborta el lote y se reintenta entero.
TRANSIENT_ERRORS = (OperationalError, InterfaceError)


def _account(session: Session, terminal_id: uuid.UUID, cache: dict) -> AccountInfo:
    if terminal_id not in cache:
        account = session.scalar(
            select(Account)
            .join(Terminal, Terminal.account_id == Account.id)
            .where(Terminal.id == terminal_id)
        )
        if account is None:
            raise ProcessingError(f"terminal {terminal_id} sin cuenta")
        cache[terminal_id] = AccountInfo(
            account_id=account.id,
            broker_id=account.broker_id,
            login=account.login,
            account_type=account.account_type,
        )
    return cache[terminal_id]


def _dispatch(session: Session, raw: RawEvent, settings: Settings, cache: dict) -> Outcome:
    if raw.schema_version != SCHEMA_VERSION:
        raise ProcessingError(f"schema_version {raw.schema_version} no soportada")
    event = trading_event_adapter.validate_python(raw.payload)
    acc = _account(session, raw.terminal_id, cache)
    if event.account_login != acc.login:
        raise ProcessingError(f"account_login {event.account_login} no es el del terminal")
    if isinstance(event, DealEvent):
        return handle_deal(session, acc, raw, event, settings)
    if isinstance(event, PositionModifyEvent):
        return handle_modify(session, acc, raw, event, settings)
    if isinstance(event, PositionsSnapshotEvent):
        return handle_snapshot(session, acc, raw, event)
    raise ProcessingError(f"tipo de evento sin procesador: {raw.event_type}")


def _defer(raw: RawEvent, deferred: Deferred, settings: Settings, now: datetime) -> str:
    raw.attempts += 1
    age = (now - raw.received_at).total_seconds()
    if age > settings.worker_defer_max_seconds:
        raw.status = deferred.expire_status
        raw.error = ORPHAN_PREFIX + deferred.reason
        raw.processed_at = now
        raw.next_attempt_at = None
        return deferred.expire_status.value
    delay = min(
        settings.worker_retry_base_seconds * 2 ** min(raw.attempts - 1, 20),
        settings.worker_retry_max_seconds,
    )
    raw.error = WAITING_PREFIX + deferred.reason
    raw.next_attempt_at = now + timedelta(seconds=delay)
    return DEFERRED


def process_event(
    session: Session, raw: RawEvent, settings: Settings, now: datetime, cache: dict
) -> str:
    """Procesa un evento en su propio savepoint y deja su estado actualizado."""
    try:
        with session.begin_nested():
            outcome = _dispatch(session, raw, settings, cache)
    except Deferred as deferred:
        result = _defer(raw, deferred, settings, now)
    except TRANSIENT_ERRORS:
        raise
    except Exception as exc:  # determinista: datos que no cuadran o un fallo del código
        log.warning(
            "evento fallido",
            extra={"raw_event_id": raw.id, "event_type": raw.event_type, "error": str(exc)[:500]},
            exc_info=not isinstance(exc, ProcessingError),
        )
        raw.status = RawEventStatus.FAILED
        raw.attempts += 1
        raw.error = f"{type(exc).__name__}: {exc}"[:2000]
        raw.processed_at = now
        raw.next_attempt_at = None
        result = RawEventStatus.FAILED.value
    else:
        raw.status = outcome.status
        raw.error = outcome.note
        raw.processed_at = now
        raw.next_attempt_at = None
        result = outcome.status.value
    session.flush()
    return result


def process_batch(
    session_factory: sessionmaker[Session], settings: Settings, now: datetime | None = None
) -> Counter:
    """Toma hasta worker_batch_size eventos PENDING listos, por hora del hecho, y los procesa
    en una transacción (un savepoint por evento). Devuelve el recuento por resultado; la clave
    "selected" es el total tomado."""
    now = now or datetime.now(UTC)
    stats: Counter = Counter()
    with session_factory() as session, session.begin():
        rows = session.scalars(
            select(RawEvent)
            .where(
                RawEvent.status == RawEventStatus.PENDING,
                or_(RawEvent.next_attempt_at.is_(None), RawEvent.next_attempt_at <= now),
            )
            .order_by(RawEvent.event_time, RawEvent.id)
            .limit(settings.worker_batch_size)
            .with_for_update(skip_locked=True)
        ).all()
        stats["selected"] = len(rows)
        cache: dict = {}
        for raw in rows:
            stats[process_event(session, raw, settings, now, cache)] += 1
    return stats


def process_pending(
    session_factory: sessionmaker[Session],
    settings: Settings,
    now: datetime | None = None,
    max_batches: int = 1000,
) -> Counter:
    """Procesa lotes hasta que no quede nada listo. Útil para pruebas y tareas puntuales."""
    total: Counter = Counter()
    for _ in range(max_batches):
        stats = process_batch(session_factory, settings, now)
        if not stats["selected"]:
            break
        total.update(stats)
    return total
