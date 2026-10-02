"""Esquemas de la detección de patrones y trampas (fase 9)."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from supervisor.models.enums import HypothesisStatus


class SegmentOut(BaseModel):
    """Métricas de la condición en un tramo (R = beneficio neto / riesgo inicial)."""

    segmento: str
    n: int
    win_rate: float | None = None
    win_rate_ic95: list[float] | None = None
    expectancy_r: float | None = None
    expectancy_r_ic95: list[float] | None = None
    profit_factor_r: float | None = None
    expectancy_r_complemento: float | None = None
    diferencia_r: float | None = None
    p_valor: float | None = None
    p_ajustado: float | None = None
    pruebas_en_familia: int | None = None
    mae_r: float | None = None
    mae_r_complemento: float | None = None
    paso: bool | None = None
    motivo: str | None = None
    metodo: str | None = None
    run_at: datetime | None = None


class PatternOut(BaseModel):
    id: uuid.UUID | None
    statement: str
    kind: Literal["TRAP", "EDGE"] | None
    kind_text: str
    origin: str
    status: HypothesisStatus | None
    # "trampa validada" / "ventaja validada" solo si pasó fuera de muestra; si no,
    # "candidata, no validada", "rechazada" o "caducada".
    label: str
    validated: bool
    status_note: str | None
    condition_spec: dict[str, Any]
    bot_version_id: uuid.UUID | None = None
    symbol: str | None = None
    split_id: uuid.UUID | None = None
    forward_from: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    entrenamiento: SegmentOut | None
    validacion: SegmentOut | None
    fuera_de_muestra: SegmentOut | None
    forward: SegmentOut | None


class SplitOut(BaseModel):
    id: uuid.UUID
    train_end: datetime
    validation_end: datetime
    oos_end: datetime
    frozen_at: datetime
    notes: str | None


class PatternRunOut(BaseModel):
    id: uuid.UUID
    status: str
    symbol: str | None
    n_trades: int
    n_train: int
    n_validation: int
    n_oos: int
    n_forward: int
    candidates_generated: int
    candidates_tested: int
    candidates_low_sample: int
    passed_train: int
    passed_validation: int
    validated: int
    rejected: int
    finished_at: datetime
    split_id: uuid.UUID | None


class PatternDetailOut(PatternOut):
    tests: list[SegmentOut]
    split: SplitOut | None
    run: PatternRunOut | None


class OverviewOut(BaseModel):
    trampas_validadas: int
    ventajas_validadas: int
    trampas_candidatas: int
    ventajas_candidatas: int
    rechazadas: int
    caducadas: int
    candidatas_probadas_ultima: int
    candidatas_probadas_total: int
    ultima_ejecucion: PatternRunOut | None
    ultima_completa: PatternRunOut | None
    operaciones_cerradas: int
    aviso: str | None


class GroupOut(BaseModel):
    n: int
    r_medio: float | None
    mae_r_medio: float | None


class DifferenceOut(BaseModel):
    condicion: str
    n: int
    en_ganadoras: float
    en_perdedoras: float
    diferencia: float


class WinnersLosersOut(BaseModel):
    ganadoras: GroupOut
    perdedoras: GroupOut
    diferencias: list[DifferenceOut]
    nota: str


class DrawdownOut(BaseModel):
    condicion: str
    mae_r_extra: float
    label: str
    entrenamiento: dict[str, Any]
    fuera_de_muestra: dict[str, Any] | None


class VersionReportOut(BaseModel):
    bot_id: uuid.UUID
    bot_name: str
    version_id: uuid.UUID
    version: str
    symbol: str | None
    resumen: OverviewOut
    split: SplitOut | None
    ganadoras_vs_perdedoras: WinnersLosersOut
    peores_condiciones: list[PatternOut]
    mejores_condiciones: list[PatternOut]
    mejor_combinacion: PatternOut | None
    aumentan_drawdown: list[DrawdownOut]
    reglas: list[PatternOut]
    nota: str
