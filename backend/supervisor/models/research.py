"""Grupos E y F: hipótesis, validación fuera de muestra, laboratorio y alertas.

Diseñado contra el overfitting:
- Una hipótesis se define (condition_spec) antes de mirar validación/OOS.
- Los cortes temporales (data_splits) se congelan; el tramo OOS no se usa para elegir.
- Cada prueba queda registrada, también las que fallan, con el número de pruebas de su
  familia para poder corregir por comparaciones múltiples.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from supervisor.models.base import (
    Base,
    CreatedAt,
    Json,
    Money,
    UTCDateTime,
    UUIDPk,
    str_enum,
)
from supervisor.models.enums import (
    AlertSeverity,
    ExperimentStatus,
    HypothesisStatus,
    SplitSegment,
)

Stat = Numeric(14, 6)


class Hypothesis(Base):
    """Una condición a contrastar (fase 9): de la búsqueda de patrones o de una regla de la
    fase 6. Su estado (PROPOSED -> TESTING -> VALIDATED / REJECTED, y DECAYED si el forward la
    contradice) es lo único que cambia; cada prueba queda en pattern_tests (solo inserción)."""

    __tablename__ = "hypotheses"
    __table_args__ = (
        CheckConstraint("kind IS NULL OR kind IN ('TRAP', 'EDGE')", name="kind"),
        Index("ix_hypotheses_version_status", "bot_version_id", "status"),
    )

    id: Mapped[UUIDPk]
    statement: Mapped[str] = mapped_column(Text)
    condition_spec: Mapped[Json]
    # Alcance: {"bot_id": ..., "bot_version_id": ..., "symbol": ...}
    scope: Mapped[Json]
    status: Mapped[HypothesisStatus] = mapped_column(
        str_enum(HypothesisStatus), server_default=HypothesisStatus.PROPOSED.value
    )
    # TRAP (expectativa negativa) o EDGE (positiva).
    kind: Mapped[str | None] = mapped_column(String(8))
    # BUSQUEDA (fase 9), REGLA (reglas de la fase 6) o MANUAL.
    origin: Mapped[str] = mapped_column(String(16), server_default="MANUAL")
    bot_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bot_versions.id"))
    symbol: Mapped[str | None] = mapped_column(String(64))
    # Condición canónica (JSON ordenado) para no duplicar hipótesis vivas del mismo alcance.
    condition_key: Mapped[str | None] = mapped_column(Text)
    split_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("data_splits.id"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("pattern_runs.id"))
    status_note: Mapped[str | None] = mapped_column(Text)
    # Las operaciones cerradas después de esta hora son forward para esta hipótesis.
    forward_from: Mapped[UTCDateTime | None]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )


class PatternRun(Base):
    """Una ejecución de la búsqueda de patrones en un alcance (versión de bot y, opcional,
    símbolo). De solo inserción. Guarda cuántas candidatas se generaron y probaron
    (transparencia frente al data snooping) y el resumen de resultados."""

    __tablename__ = "pattern_runs"
    __table_args__ = (Index("ix_pattern_runs_version", "bot_version_id", "started_at"),)

    id: Mapped[UUIDPk]
    bot_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bot_versions.id"))
    symbol: Mapped[str | None] = mapped_column(String(64))
    split_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("data_splits.id"))
    # COMPLETADA o MUESTRA_INSUFICIENTE.
    status: Mapped[str] = mapped_column(String(24))
    n_trades: Mapped[int] = mapped_column(Integer)
    n_train: Mapped[int] = mapped_column(Integer, server_default="0")
    n_validation: Mapped[int] = mapped_column(Integer, server_default="0")
    n_oos: Mapped[int] = mapped_column(Integer, server_default="0")
    n_forward: Mapped[int] = mapped_column(Integer, server_default="0")
    candidates_generated: Mapped[int] = mapped_column(Integer, server_default="0")
    candidates_tested: Mapped[int] = mapped_column(Integer, server_default="0")
    candidates_low_sample: Mapped[int] = mapped_column(Integer, server_default="0")
    passed_train: Mapped[int] = mapped_column(Integer, server_default="0")
    passed_validation: Mapped[int] = mapped_column(Integer, server_default="0")
    validated: Mapped[int] = mapped_column(Integer, server_default="0")
    rejected: Mapped[int] = mapped_column(Integer, server_default="0")
    # Hora de cierre de la última operación usada: si no hay nuevas, no se repite.
    last_close_time: Mapped[UTCDateTime | None]
    params: Mapped[Json]
    summary: Mapped[Json]
    started_at: Mapped[UTCDateTime]
    finished_at: Mapped[CreatedAt]


class DataSplit(Base):
    __tablename__ = "data_splits"
    __table_args__ = (
        CheckConstraint("train_end < validation_end AND validation_end < oos_end", name="ordered"),
    )

    id: Mapped[UUIDPk]
    scope: Mapped[Json]
    train_end: Mapped[UTCDateTime]
    validation_end: Mapped[UTCDateTime]
    oos_end: Mapped[UTCDateTime]
    frozen_at: Mapped[CreatedAt]
    notes: Mapped[str | None] = mapped_column(Text)


class PatternTest(Base):
    """Prueba de una hipótesis en un tramo de su split. De solo inserción. TRAIN, VALIDATION
    y OOS solo pueden existir una vez por hipótesis (índice único): el tramo fuera de muestra
    se evalúa UNA vez. FORWARD se repite a medida que llegan operaciones."""

    __tablename__ = "pattern_tests"
    __table_args__ = (
        CheckConstraint("n >= 0", name="n_non_negative"),
        Index(
            "uq_pattern_tests_once",
            "hypothesis_id",
            "segment",
            unique=True,
            postgresql_where=text("segment IN ('TRAIN', 'VALIDATION', 'OOS')"),
        ),
    )

    id: Mapped[UUIDPk]
    hypothesis_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("hypotheses.id"), index=True)
    split_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("data_splits.id"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("pattern_runs.id"))
    segment: Mapped[SplitSegment] = mapped_column(str_enum(SplitSegment))
    n: Mapped[int] = mapped_column(Integer)
    win_rate: Mapped[Decimal | None] = mapped_column(Stat)
    expectancy: Mapped[Decimal | None] = mapped_column(Stat)
    profit_factor: Mapped[Decimal | None] = mapped_column(Stat)
    ci_low: Mapped[Decimal | None] = mapped_column(Stat)
    ci_high: Mapped[Decimal | None] = mapped_column(Stat)
    p_value: Mapped[Decimal | None] = mapped_column(Stat)
    p_adjusted: Mapped[Decimal | None] = mapped_column(Stat)
    tests_in_family: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(64))
    # ¿Pasó el criterio del tramo? y por qué (texto), más todas las métricas.
    passed: Mapped[bool | None] = mapped_column(Boolean)
    details: Mapped[Json]
    run_at: Mapped[CreatedAt]


class Experiment(Base):
    __tablename__ = "experiments"

    id: Mapped[UUIDPk]
    code: Mapped[str] = mapped_column(String(16), unique=True)  # EXP-001
    base_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bot_versions.id"))
    candidate_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bot_versions.id"))
    hypothesis_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("hypotheses.id"))
    change_description: Mapped[str] = mapped_column(Text)
    status: Mapped[ExperimentStatus] = mapped_column(
        str_enum(ExperimentStatus), server_default=ExperimentStatus.DRAFT.value
    )
    conclusion: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[CreatedAt]


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    __table_args__ = (CheckConstraint("period_end > period_start", name="period"),)

    id: Mapped[UUIDPk]
    experiment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("experiments.id"), index=True
    )
    bot_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bot_versions.id"))
    symbol: Mapped[str] = mapped_column(String(64))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    model: Mapped[str] = mapped_column(String(64))  # p. ej. "Every tick based on real ticks"
    initial_deposit: Mapped[Money | None]
    report_file: Mapped[str | None] = mapped_column(String(512))
    metrics: Mapped[Json]
    imported_at: Mapped[CreatedAt]


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[UUIDPk]
    rule: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[AlertSeverity] = mapped_column(str_enum(AlertSeverity))
    bot_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bot_versions.id"))
    deployment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("deployments.id"))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[Json]
    created_at: Mapped[CreatedAt]
    acknowledged_at: Mapped[UTCDateTime | None]
