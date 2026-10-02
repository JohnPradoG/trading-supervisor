"""Alertas (sección 15). Requiere token de administración. Las alertas las crea el worker;
aquí solo se consultan y se reconocen (acknowledged_at, una vez)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from supervisor.api.deps import SessionDep, admin_auth
from supervisor.models.enums import AlertSeverity
from supervisor.services import alerts as alerts_service

router = APIRouter(prefix="/v1", tags=["alertas"], dependencies=[Depends(admin_auth)])


@router.get("/alerts")
def list_alerts(
    session: SessionDep,
    kind: Annotated[str | None, Query(max_length=64)] = None,
    severity: AlertSeverity | None = None,
    unacknowledged: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    """Últimas alertas (las más recientes primero). `kind`: TRAMPA_ACTIVA, PATRON_VALIDADO,
    PATRON_CADUCADO o EA_SIN_LATIDO. `unacknowledged`: solo disparos sin reconocer."""
    return alerts_service.list_alerts(
        session, rule=kind, severity=severity, unacknowledged=unacknowledged, limit=limit
    )


@router.post("/alerts/{alert_id}/ack")
def acknowledge(alert_id: uuid.UUID, session: SessionDep):
    """Marca la alerta como reconocida (idempotente: la primera fecha se conserva)."""
    return alerts_service.acknowledge(session, alert_id)
