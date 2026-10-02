"""Esquemas de análisis post-operación y estadísticas."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from supervisor.models.enums import FindingKind, Outcome, TradeStatus


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# Análisis ----------------------------------------------------------------------------------


class FindingOut(_Out):
    position: int
    kind: FindingKind
    code: str
    text: str
    evidence: dict[str, Any]
    # Solo hipótesis: "no_validada" hasta que la fase 9 la contraste.
    confidence: str | None
    rule_version: int | None
    supported_by: list[str]
    # Solo hipótesis: estado de la regla en la fase 9 para el bot de la operación (consulta;
    # el hallazgo no se modifica). etiqueta = "trampa validada", "candidata, no validada"...
    validation: dict[str, Any] | None = None


class DataQualityNote(BaseModel):
    code: str
    text: str


class AnalysisVersionOut(_Out):
    analysis_version: int
    outcome: Outcome
    analyzer_version: str
    ruleset_version: int
    input_hash: str
    created_at: datetime


class AnalysisOut(AnalysisVersionOut):
    id: uuid.UUID
    data_quality: list[DataQualityNote]
    facts: list[FindingOut]
    hypotheses: list[FindingOut]
    inputs: dict[str, Any] | None = None


class TradeAnalysisOut(BaseModel):
    trade_id: uuid.UUID
    trade_status: TradeStatus
    # Vigente (o la versión pedida). None si la operación sigue abierta o aún no se analizó.
    analysis: AnalysisOut | None
    history: list[AnalysisVersionOut]
    note: str | None = None


# Estadísticas ------------------------------------------------------------------------------


class Interval(BaseModel):
    low: float | None
    high: float | None


class HistogramBin(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: float = Field(alias="from")
    to: float
    count: int


class Metrics(BaseModel):
    n_trades: int
    wins: int
    losses: int
    breakevens: int
    sample_warning: str | None
    win_rate: float | None
    loss_rate: float | None
    breakeven_rate: float | None
    win_rate_ci95: Interval | None
    gross_profit: float | None
    gross_loss: float | None
    profit_factor: float | None
    profit_factor_note: str | None
    expectancy: float | None
    expectancy_ci95: Interval | None
    n_with_risk: int
    expectancy_r: float | None
    expectancy_r_ci95: Interval | None
    average_win: float | None
    average_loss: float | None
    largest_win: float | None
    largest_loss: float | None
    average_duration_seconds: float | None
    max_drawdown: float | None
    max_drawdown_pct: float | None
    max_drawdown_note: str | None
    max_consecutive_wins: int | None
    max_consecutive_losses: int | None
    sharpe: float | None
    sortino: float | None
    ratio_basis: str | None
    sharpe_note: str | None
    sortino_note: str | None
    cumulative_return: float | None
    cumulative_return_pct: float | None
    cumulative_r: float | None
    first_close: datetime | None
    last_close: datetime | None
    histogram_r: list[HistogramBin]
    histogram_net: list[HistogramBin]


class GroupOut(BaseModel):
    key: dict[str, Any]
    label: str
    metrics: Metrics


class StatsOut(BaseModel):
    filters: dict[str, Any]
    group_by: list[str]
    parameters: dict[str, Any]
    # Operaciones asignadas a un bot (con despliegue) que cumplen los filtros.
    total: Metrics
    groups: list[GroupOut]
    # Operaciones sin despliegue, siempre aparte. None si se filtró por bot o versión.
    unassigned: Metrics | None


class VersionStats(BaseModel):
    version_id: uuid.UUID
    version: str
    main_timeframe: str
    released_at: datetime
    metrics: Metrics


class CompareVersionsOut(BaseModel):
    bot_id: uuid.UUID
    bot_name: str
    filters: dict[str, Any]
    parameters: dict[str, Any]
    versions: list[VersionStats]
    comparison_note: str


# Trading DNA (fase 8) ----------------------------------------------------------------------


class DnaFeatureOut(BaseModel):
    name: str
    label: str
    value_type: str
    timeframe: str | None
    unit: str | None
    value: Any
    # Motivo cuando el valor es NULL (nunca se inventa un dato).
    null_reason: str | None


class DnaSectionOut(BaseModel):
    group: str
    title: str
    features: list[DnaFeatureOut]


class DnaVersionOut(_Out):
    dna_version: int
    feature_set_version: int
    input_hash: str
    computed_at: datetime


class DnaOut(DnaVersionOut):
    data_cutoff: datetime
    last_bar_time: datetime | None
    data_quality: dict[str, Any]
    sections: list[DnaSectionOut]
    features: dict[str, Any]
    null_reasons: dict[str, str]


class TradeDnaOut(BaseModel):
    trade_id: uuid.UUID
    dna: DnaOut | None
    history: list[DnaVersionOut]
    note: str | None = None


class FeatureDefinitionOut(BaseModel):
    name: str
    version: int
    group: str
    label: str
    value_type: str
    timeframe: str | None
    unit: str | None
    searchable: bool
    available: bool
    categories: list[str] | None
    bins: list[float] | None
    formula: str
    null_policy: str
