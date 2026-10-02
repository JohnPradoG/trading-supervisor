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
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
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
    TradeSource,
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
    """Experimento del laboratorio (fase 10): "#001 · bot base v1.0 · cambio: filtro de
    tendencia H1". Lo que define el experimento (versión base, cambio, filtro, hipótesis) no
    cambia una vez creado (trigger); solo cambian el estado y la última conclusión. Los
    resultados van en experiment_results, de solo inserción, una revisión cada vez."""

    __tablename__ = "experiments"

    id: Mapped[UUIDPk]
    # Número correlativo (#001, #002...) y su código para mostrar.
    number: Mapped[int] = mapped_column(Integer, unique=True)
    code: Mapped[str] = mapped_column(String(16), unique=True)
    title: Mapped[str] = mapped_column(String(200))
    base_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bot_versions.id"))
    candidate_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bot_versions.id"))
    hypothesis_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("hypotheses.id"))
    # Alcance opcional: solo las operaciones de este símbolo.
    symbol: Mapped[str | None] = mapped_column(String(64))
    change_description: Mapped[str] = mapped_column(Text)
    # Filtro estructurado opcional: {"modo": "excluir" | "solo", "condicion": <condition_spec
    # de la fase 9>} (ver supervisor.analytics.lab).
    filter_spec: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[ExperimentStatus] = mapped_column(
        str_enum(ExperimentStatus), server_default=ExperimentStatus.DRAFT.value
    )
    conclusion: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), onupdate=text("now()")
    )


class BacktestRun(Base):
    """Resultado importado del Strategy Tester de MT5. De solo inserción. Nunca se mezcla con
    las operaciones reales: no crea filas en trades y su origen es siempre BACKTEST."""

    __tablename__ = "backtest_runs"
    __table_args__ = (
        CheckConstraint("period_end >= period_start", name="period"),
        CheckConstraint("source = 'BACKTEST'", name="solo_backtest"),
    )

    id: Mapped[UUIDPk]
    experiment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("experiments.id"), index=True
    )
    bot_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bot_versions.id"))
    source: Mapped[TradeSource] = mapped_column(
        str_enum(TradeSource), server_default=TradeSource.BACKTEST.value
    )
    label: Mapped[str | None] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(64))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    model: Mapped[str] = mapped_column(String(64))  # p. ej. "Every tick based on real ticks"
    initial_deposit: Mapped[Money | None]
    report_file: Mapped[str | None] = mapped_column(String(512))
    # CSV (export de transacciones), HTML o XML (informe del probador).
    report_format: Mapped[str | None] = mapped_column(String(8))
    file_sha256: Mapped[str | None] = mapped_column(String(64))
    metrics: Mapped[Json]
    # {"operaciones": [...], "informe": {...}, "avisos": [...]}: lo leído del archivo.
    trades: Mapped[Json]
    imported_at: Mapped[CreatedAt]


class ExperimentResult(Base):
    """Una revisión de los resultados de un experimento. De solo inserción: un resultado
    nuevo (otro filtro con más datos, un backtest, una nota) es otra revisión; las anteriores
    no se tocan."""

    __tablename__ = "experiment_results"
    __table_args__ = (
        UniqueConstraint("experiment_id", "revision"),
        CheckConstraint("kind IN ('FILTRO', 'BACKTEST', 'MANUAL')", name="kind"),
        CheckConstraint("revision >= 1", name="revision"),
    )

    id: Mapped[UUIDPk]
    experiment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("experiments.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    notes: Mapped[str | None] = mapped_column(Text)
    results: Mapped[Json]
    backtest_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("backtest_runs.id"))
    created_at: Mapped[CreatedAt]


class Alert(Base):
    """Alerta (sección 15). De solo inserción: cada disparo y cada resolución es una fila
    nueva con la misma dedup_key. Solo cambian (trigger) las columnas de entrega por Telegram
    y acknowledged_at, este último una sola vez. Ver supervisor.alerts.engine."""

    __tablename__ = "alerts"
    __table_args__ = (
        CheckConstraint("event IN ('DISPARO', 'RESUELTA')", name="event"),
        CheckConstraint(
            "delivery_status IN ('PENDING', 'SENT', 'FAILED', 'SKIPPED')", name="delivery_status"
        ),
        Index("ix_alerts_dedup_key_created", "dedup_key", "created_at"),
        Index("ix_alerts_created_at", "created_at"),
        Index(
            "ix_alerts_delivery_pending",
            "created_at",
            postgresql_where=text("delivery_status = 'PENDING'"),
        ),
        Index(
            "ix_alerts_unacknowledged",
            "created_at",
            postgresql_where=text("acknowledged_at IS NULL AND event = 'DISPARO'"),
        ),
    )

    id: Mapped[UUIDPk]
    rule: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[AlertSeverity] = mapped_column(str_enum(AlertSeverity))
    # Misma condición = misma clave: no se repite mientras sigue activa.
    dedup_key: Mapped[str] = mapped_column(String(200), server_default="")
    # DISPARO (la condición aparece) o RESUELTA (la condición desaparece).
    event: Mapped[str] = mapped_column(String(16), server_default="DISPARO")
    bot_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bot_versions.id"))
    deployment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("deployments.id"))
    trade_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("trades.trade_id"))
    hypothesis_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("hypotheses.id"))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[Json]
    created_at: Mapped[CreatedAt]
    acknowledged_at: Mapped[UTCDateTime | None]
    # Entrega por Telegram: PENDING, SENT, FAILED o SKIPPED (sin canal configurado).
    delivery_status: Mapped[str] = mapped_column(String(16), server_default="SKIPPED")
    delivery_attempts: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    delivery_error: Mapped[str | None] = mapped_column(Text)
    next_delivery_at: Mapped[UTCDateTime | None]
    sent_at: Mapped[UTCDateTime | None]
