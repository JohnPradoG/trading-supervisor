"""Grupos E y F: hipótesis, validación fuera de muestra, laboratorio y alertas.

Diseñado contra el overfitting:
- Una hipótesis se define (condition_spec) antes de mirar validación/OOS.
- Los cortes temporales (data_splits) se congelan; el tramo OOS no se usa para elegir.
- Cada prueba queda registrada, también las que fallan, con el número de pruebas de su
  familia para poder corregir por comparaciones múltiples.
"""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
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
    __tablename__ = "hypotheses"

    id: Mapped[UUIDPk]
    statement: Mapped[str] = mapped_column(Text)
    condition_spec: Mapped[Json]
    # Alcance: {"bot_id": ..., "bot_version_id": ..., "symbol": ...}
    scope: Mapped[Json]
    status: Mapped[HypothesisStatus] = mapped_column(
        str_enum(HypothesisStatus), server_default=HypothesisStatus.PROPOSED.value
    )
    created_at: Mapped[CreatedAt]


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
    __tablename__ = "pattern_tests"
    __table_args__ = (CheckConstraint("n >= 0", name="n_non_negative"),)

    id: Mapped[UUIDPk]
    hypothesis_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("hypotheses.id"), index=True)
    split_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("data_splits.id"))
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
