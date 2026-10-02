"""Registro y consulta de bots, versiones y despliegues (requiere token de administración)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from supervisor.api.deps import SessionDep, SettingsDep, admin_auth
from supervisor.models.enums import RawEventStatus
from supervisor.schemas.registry import (
    BackfillCursor,
    BackfillOut,
    BackfillRequest,
    BotCreate,
    BotDetail,
    BotOut,
    BotUpdate,
    DeploymentCreate,
    DeploymentEnd,
    DeploymentOut,
    RawEventOut,
    VersionCreate,
    VersionOut,
)
from supervisor.services import registry
from supervisor.worker.backfill import backfill_page

router = APIRouter(prefix="/v1", tags=["registro"], dependencies=[Depends(admin_auth)])


@router.get("/bots", response_model=list[BotOut])
def list_bots(session: SessionDep):
    return registry.list_bots(session)


@router.post("/bots", response_model=BotOut, status_code=status.HTTP_201_CREATED)
def create_bot(data: BotCreate, session: SessionDep):
    result = registry.create_bot(session, data)
    session.commit()
    return result


@router.get("/bots/{bot_id}", response_model=BotDetail)
def get_bot(bot_id: uuid.UUID, session: SessionDep):
    bot = registry.get_bot(session, bot_id)
    return BotDetail(
        **BotOut.model_validate(bot).model_dump(),
        versions=[VersionOut.model_validate(v) for v in registry.list_versions(session, bot_id)],
        deployments=[
            DeploymentOut.model_validate(d) for d in registry.list_deployments(session, bot_id)
        ],
    )


@router.patch("/bots/{bot_id}", response_model=BotOut)
def update_bot(bot_id: uuid.UUID, data: BotUpdate, session: SessionDep):
    result = registry.update_bot(session, bot_id, data)
    session.commit()
    return result


@router.post(
    "/bots/{bot_id}/versions", response_model=VersionOut, status_code=status.HTTP_201_CREATED
)
def create_version(bot_id: uuid.UUID, data: VersionCreate, session: SessionDep):
    result = registry.create_version(session, bot_id, data)
    session.commit()
    return result


@router.post("/deployments", response_model=DeploymentOut, status_code=status.HTTP_201_CREATED)
def create_deployment(data: DeploymentCreate, session: SessionDep):
    result = registry.create_deployment(session, data)
    session.commit()
    return result


@router.post("/deployments/{deployment_id}/end", response_model=DeploymentOut)
def end_deployment(deployment_id: uuid.UUID, data: DeploymentEnd, session: SessionDep):
    result = registry.end_deployment(session, deployment_id, data)
    session.commit()
    return result


@router.get("/raw-events", response_model=list[RawEventOut])
def list_raw_events(
    session: SessionDep,
    status_filter: Annotated[RawEventStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    return registry.list_raw_events(session, status_filter, limit)


@router.post("/admin/backfill", response_model=BackfillOut)
def backfill(data: BackfillRequest, session: SessionDep, settings: SettingsDep):
    """Una página acotada del backfill (despliegue, MFE/MAE, riesgo, DNA y análisis de
    operaciones antiguas). Trabajo limitado por petición para no bloquear la API: para
    recorrerlo todo, repetir con `next` hasta que sea null, o usar `supervisor-cli backfill`."""
    after = (data.after_time, data.after_id) if data.after_time is not None else None
    page = backfill_page(session, settings, after, data.limit, data.entry_from, data.entry_to)
    session.commit()
    cursor = page.next_cursor
    return BackfillOut(
        stats=dict(page.stats),
        next=BackfillCursor(after_time=cursor[0], after_id=cursor[1]) if cursor else None,
    )
