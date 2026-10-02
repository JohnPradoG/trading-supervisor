"""Endpoints que usa el EA Monitor. Solo reciben datos: no existe ninguna ruta que envíe
órdenes a MT5."""

from fastapi import APIRouter

from supervisor.api.deps import SessionDep, SettingsDep, TerminalDep
from supervisor.schemas.ingest import (
    AccountSnapshotIn,
    BarsIn,
    BatchResult,
    EventBatch,
    HeartbeatIn,
    SimpleIngestResult,
)
from supervisor.services import ingest

router = APIRouter(prefix="/v1/ingest", tags=["ingesta (EA)"])


@router.post("/events", response_model=BatchResult)
def post_events(
    batch: EventBatch, ctx: TerminalDep, session: SessionDep, settings: SettingsDep
) -> BatchResult:
    """Lote de eventos de trading. Responde el estado de cada uno: `accepted` y `duplicate`
    significan que el EA puede sacarlo de su cola; `rejected` trae el motivo."""
    result = ingest.ingest_events(session, ctx, batch, settings)
    session.commit()  # confirmado en la base antes de responder al EA
    return result


@router.post("/heartbeat", response_model=SimpleIngestResult)
def post_heartbeat(
    hb: HeartbeatIn, ctx: TerminalDep, session: SessionDep, settings: SettingsDep
) -> SimpleIngestResult:
    result = ingest.ingest_heartbeat(session, ctx, hb, settings)
    session.commit()  # confirmado en la base antes de responder al EA
    return result


@router.post("/account-snapshot", response_model=SimpleIngestResult)
def post_account_snapshot(
    snap: AccountSnapshotIn, ctx: TerminalDep, session: SessionDep, settings: SettingsDep
) -> SimpleIngestResult:
    result = ingest.ingest_account_snapshot(session, ctx, snap, settings)
    session.commit()  # confirmado en la base antes de responder al EA
    return result


@router.post("/bars", response_model=SimpleIngestResult)
def post_bars(
    payload: BarsIn, ctx: TerminalDep, session: SessionDep, settings: SettingsDep
) -> SimpleIngestResult:
    result = ingest.ingest_bars(session, ctx, payload, settings)
    session.commit()  # confirmado en la base antes de responder al EA
    return result
