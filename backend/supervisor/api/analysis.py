"""Análisis post-operación y estadísticas (requiere token de administración). Solo lectura."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from supervisor.api.deps import SessionDep, SettingsDep, admin_auth
from supervisor.api.trades import _utc
from supervisor.models.enums import FindingKind, TradeSource, TradeStatus
from supervisor.schemas.analysis import (
    AnalysisOut,
    AnalysisVersionOut,
    CompareVersionsOut,
    FindingOut,
    StatsOut,
    TradeAnalysisOut,
)
from supervisor.services import stats as stats_service
from supervisor.services.analysis import get_trade_analysis
from supervisor.services.stats import StatsFilters

router = APIRouter(prefix="/v1", tags=["análisis"], dependencies=[Depends(admin_auth)])


@router.get("/stats", response_model=StatsOut)
def get_stats(
    session: SessionDep,
    settings: SettingsDep,
    bot_id: uuid.UUID | None = None,
    version_id: uuid.UUID | None = None,
    symbol: Annotated[str | None, Query(max_length=64)] = None,
    account_id: uuid.UUID | None = None,
    source: TradeSource | None = None,
    date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None,
    group_by: Annotated[str | None, Query(max_length=64)] = None,
):
    """Métricas de operaciones cerradas. `from`/`to` filtran por hora de cierre; `group_by`
    admite hasta dos dimensiones separadas por coma (bot, version, symbol, direction, weekday,
    hour, session, timeframe, account, source). Las operaciones sin bot van en `unassigned`."""
    filters = StatsFilters(
        bot_id=bot_id,
        version_id=version_id,
        symbol=symbol,
        account_id=account_id,
        source=source,
        date_from=_utc(date_from),
        date_to=_utc(date_to),
    )
    dims = stats_service.parse_group_by(group_by)
    return stats_service.stats(session, settings, filters, dims)


@router.get("/bots/{bot_id}/compare-versions", response_model=CompareVersionsOut)
def compare_versions(
    bot_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    symbol: Annotated[str | None, Query(max_length=64)] = None,
    account_id: uuid.UUID | None = None,
    source: TradeSource | None = None,
    date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None,
):
    filters = StatsFilters(
        symbol=symbol,
        account_id=account_id,
        source=source,
        date_from=_utc(date_from),
        date_to=_utc(date_to),
    )
    return stats_service.compare_versions(session, settings, bot_id, filters)


@router.get("/trades/{trade_id}/analysis", response_model=TradeAnalysisOut)
def trade_analysis(
    trade_id: uuid.UUID,
    session: SessionDep,
    version: Annotated[int | None, Query(ge=1)] = None,
    include_inputs: bool = False,
):
    """Análisis vigente de la operación (o la `version` pedida) separado en hechos e
    hipótesis, con la lista de versiones anteriores."""
    view = get_trade_analysis(session, trade_id, version)
    analysis = None
    note = None
    if view.analysis is not None:
        a = view.analysis
        findings = [FindingOut.model_validate(f) for f in a.findings]
        analysis = AnalysisOut(
            **AnalysisVersionOut.model_validate(a).model_dump(),
            id=a.id,
            data_quality=a.data_quality,
            facts=[f for f in findings if f.kind == FindingKind.FACT],
            hypotheses=[f for f in findings if f.kind == FindingKind.HYPOTHESIS],
            inputs=a.inputs if include_inputs else None,
        )
    elif view.trade.status == TradeStatus.OPEN:
        note = "la operación sigue abierta: se analiza al cerrarse"
    else:
        note = "aún sin análisis: el worker lo genera en su próximo repaso"
    return TradeAnalysisOut(
        trade_id=trade_id,
        trade_status=view.trade.status,
        analysis=analysis,
        history=[AnalysisVersionOut.model_validate(h) for h in view.history],
        note=note,
    )
