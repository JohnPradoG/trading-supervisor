"""Diagnóstico de una versión de bot: qué está fallando, qué cambiar y qué funciona mejor.
Requiere token de administración. Las sugerencias son texto para quien edita el EA: ninguna
ruta modifica ni controla los bots."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from supervisor.api.deps import SessionDep, SettingsDep, admin_auth
from supervisor.services import diagnosis as diagnosis_service
from supervisor.services.errors import NotFound

router = APIRouter(prefix="/v1", tags=["diagnóstico"], dependencies=[Depends(admin_auth)])


@router.get("/bots/{bot_id}/versions/{version_id}/diagnosis")
def get_diagnosis(
    bot_id: uuid.UUID,
    version_id: uuid.UUID,
    session: SessionDep,
    symbol: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = diagnosis_service.HISTORY_LIMIT,
):
    """El último diagnóstico (`latest`, con el informe completo) y la historia (`history`, de
    solo inserción, el más reciente primero). Cada sugerencia lleva `estado`: "validada" solo
    si la mejora se confirmó en validación y fuera de muestra; si no, "candidata, no validada"
    o "sin datos suficientes"."""
    return diagnosis_service.diagnosis_for(session, bot_id, version_id, symbol, limit)


@router.post("/bots/{bot_id}/versions/{version_id}/diagnosis")
def run_diagnosis(
    bot_id: uuid.UUID,
    version_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    symbol: Annotated[str | None, Query(max_length=64)] = None,
):
    """Calcula el diagnóstico ahora. Si las entradas no cambiaron desde el último no se
    recalcula (`status` = "sin_cambios")."""
    if diagnosis_service.get_version(session, version_id).bot_id != bot_id:
        raise NotFound("versión de bot no encontrada")
    status, run = diagnosis_service.compute_diagnosis(session, settings, version_id, symbol)
    return {"status": status, "run": diagnosis_service.run_view(run)}
