"""Patrones, trampas e informe por versión (fase 9). Requiere token de administración. Solo
lectura: la búsqueda la ejecutan el worker y supervisor-cli patterns."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from supervisor.api.deps import SessionDep, SettingsDep, admin_auth
from supervisor.models.enums import HypothesisStatus
from supervisor.schemas.patterns import PatternDetailOut, PatternOut, VersionReportOut
from supervisor.services import patterns as patterns_service

router = APIRouter(prefix="/v1", tags=["patrones"], dependencies=[Depends(admin_auth)])

KINDS = {"trap": "TRAP", "edge": "EDGE"}


@router.get("/patterns", response_model=list[PatternOut])
def list_patterns(
    session: SessionDep,
    bot_id: uuid.UUID | None = None,
    version_id: uuid.UUID | None = None,
    symbol: Annotated[str | None, Query(max_length=64)] = None,
    status: HypothesisStatus | None = None,
    kind: Literal["trap", "edge"] | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
):
    """Trampas y ventajas: validadas primero. Solo `label` = "trampa validada" (o "ventaja
    validada") ha pasado fuera de muestra; el resto es "candidata, no validada", "rechazada" o
    "caducada". Cada una con sus métricas en entrenamiento, validación, fuera de muestra y
    forward."""
    return patterns_service.list_patterns(
        session,
        bot_id=bot_id,
        version_id=version_id,
        symbol=symbol,
        status=status,
        kind=KINDS.get(kind) if kind else None,
        limit=limit,
    )


@router.get("/patterns/{pattern_id}", response_model=PatternDetailOut)
def pattern_detail(pattern_id: uuid.UUID, session: SessionDep):
    """Un patrón con todas sus pruebas (de solo inserción), su split y la ejecución que lo
    propuso (cuántas candidatas se probaron)."""
    return patterns_service.pattern_detail(session, pattern_id)


@router.get("/bots/{bot_id}/versions/{version_id}/report", response_model=VersionReportOut)
def version_report(
    bot_id: uuid.UUID,
    version_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    symbol: Annotated[str | None, Query(max_length=64)] = None,
):
    """Informe de la versión: ganadoras frente a perdedoras, peores y mejores condiciones,
    mejor combinación y condiciones que aumentan el drawdown (MAE en R), con muestra, win
    rate, expectativa, profit factor e intervalos, dentro y fuera de muestra."""
    return patterns_service.version_report(session, settings, bot_id, version_id, symbol)
