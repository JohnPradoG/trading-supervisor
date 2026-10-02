"""Laboratorio (fase 10): experimentos, filtro contrafactual, backtests del Strategy Tester y
comparación de versiones. Requiere token de administración. Escribe solo en las tablas del
laboratorio: nunca en las operaciones ni hacia MT5."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status

from supervisor.api.deps import SessionDep, SettingsDep, admin_auth
from supervisor.schemas.lab import ExperimentCreate, FilterRunIn, ResultsRevisionIn
from supervisor.services import lab as lab_service
from supervisor.services.errors import ServiceError

router = APIRouter(prefix="/v1", tags=["laboratorio"], dependencies=[Depends(admin_auth)])


class PayloadTooLarge(ServiceError):
    status_code = 413


@router.post("/experiments", status_code=status.HTTP_201_CREATED)
def create_experiment(data: ExperimentCreate, session: SessionDep):
    exp = lab_service.create_experiment(
        session,
        title=data.title,
        base_version_id=data.base_version_id,
        change_description=data.change_description,
        candidate_version_id=data.candidate_version_id,
        hypothesis_id=data.hypothesis_id,
        symbol=data.symbol,
        filter_spec=data.filter,
        filter_from_hypothesis_=data.filter_from_hypothesis,
    )
    return lab_service.experiment_detail(session, exp)


@router.get("/experiments")
def list_experiments(session: SessionDep, limit: Annotated[int, Query(ge=1, le=500)] = 200):
    return lab_service.list_experiments(session, limit)


@router.get("/experiments/{ref}")
def get_experiment(ref: str, session: SessionDep):
    """`ref`: número (7 o 007) o id."""
    return lab_service.experiment_detail(session, lab_service.get_experiment(session, ref))


@router.post("/experiments/{ref}/results", status_code=status.HTTP_201_CREATED)
def add_results_revision(ref: str, data: ResultsRevisionIn, session: SessionDep):
    exp = lab_service.get_experiment(session, ref)
    row = lab_service.add_manual_result(
        session,
        exp,
        notes=data.notes,
        results=data.results,
        status=data.status,
        conclusion=data.conclusion,
    )
    return lab_service.result_view(row)


@router.post("/experiments/{ref}/filter-run", status_code=status.HTTP_201_CREATED)
def run_filter(
    ref: str, session: SessionDep, settings: SettingsDep, data: FilterRunIn | None = None
):
    """Calcula el filtro contrafactual con los datos de ahora y lo guarda como revisión."""
    exp = lab_service.get_experiment(session, ref)
    row = lab_service.run_filter(session, settings, exp, data.notes if data else None)
    return lab_service.result_view(row)


def _read_limited(file: UploadFile, limit: int) -> bytes:
    """Lee el archivo subido (Starlette ya lo tiene en un temporal) sin pasar de `limit`."""
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise PayloadTooLarge(f"archivo demasiado grande (máximo {limit // 1024} KB)")
    return data


@router.post("/experiments/{ref}/backtests", status_code=status.HTTP_201_CREATED)
def upload_backtest(
    ref: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    file: Annotated[UploadFile, File()],
    bot_version_id: Annotated[uuid.UUID | None, Form()] = None,
    label: Annotated[str | None, Form(max_length=64)] = None,
    symbol: Annotated[str | None, Form(max_length=64)] = None,
):
    """Sube el resultado del Strategy Tester (multipart, campo `file`): CSV de transacciones
    (recomendado), informe HTML o XML. Se guarda como backtest (origen BACKTEST) y como
    revisión del experimento; nunca crea operaciones."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > settings.lab_upload_max_bytes + 65536:
        raise PayloadTooLarge(
            f"archivo demasiado grande (máximo {settings.lab_upload_max_bytes // 1024} KB)"
        )
    data = _read_limited(file, settings.lab_upload_max_bytes)
    exp = lab_service.get_experiment(session, ref)
    run, row = lab_service.import_backtest(
        session,
        settings,
        exp,
        data,
        filename=file.filename,
        version_id=bot_version_id,
        label=label or None,
        symbol=symbol or None,
    )
    return {"backtest": lab_service.backtest_view(session, run), **lab_service.result_view(row)}


@router.get("/experiments/{ref}/comparison")
def experiment_comparison(ref: str, session: SessionDep, settings: SettingsDep):
    """Brazos del experimento lado a lado (original, filtrado, candidata, backtests) con
    avisos de muestra y las trampas de cada versión. Se calcula con los datos de ahora."""
    exp = lab_service.get_experiment(session, ref)
    return lab_service.experiment_comparison(session, settings, exp)


@router.get("/versions/compare")
def compare_versions(
    a: uuid.UUID,
    b: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    symbol: Annotated[str | None, Query(max_length=64)] = None,
):
    """Dos versiones cualesquiera lado a lado (sección 13)."""
    return lab_service.compare_two_versions(session, settings, a, b, symbol)
