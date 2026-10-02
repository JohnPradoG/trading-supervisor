"""Consulta de los análisis post-operación (solo lectura)."""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from supervisor.models import Trade, TradeAnalysis
from supervisor.services.errors import NotFound


@dataclass(frozen=True)
class TradeAnalysisView:
    trade: Trade
    analysis: TradeAnalysis | None
    history: list[TradeAnalysis]


def get_trade_analysis(
    session: Session, trade_id: uuid.UUID, version: int | None = None
) -> TradeAnalysisView:
    """Análisis vigente (la versión más alta) o el de `version`, con la lista de versiones."""
    trade = session.get(Trade, trade_id)
    if trade is None:
        raise NotFound("operación no encontrada")
    history = list(
        session.scalars(
            select(TradeAnalysis)
            .where(TradeAnalysis.trade_id == trade_id)
            .order_by(TradeAnalysis.analysis_version.desc())
        )
    )
    selected = None
    if history:
        wanted = history[0].analysis_version if version is None else version
        selected = session.scalar(
            select(TradeAnalysis)
            .where(
                TradeAnalysis.trade_id == trade_id,
                TradeAnalysis.analysis_version == wanted,
            )
            .options(selectinload(TradeAnalysis.findings))
        )
    if version is not None and selected is None:
        raise NotFound(f"la operación no tiene la versión {version} del análisis")
    return TradeAnalysisView(trade=trade, analysis=selected, history=history)
