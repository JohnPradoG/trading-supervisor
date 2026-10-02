"""Grupo D: Trading DNA, análisis post-operación y noticias.

Reglas:
- Una variable que no se puede obtener queda NULL y su motivo va en trade_dna.null_reasons.
- El DNA se calcula solo con datos anteriores a la entrada (velas cerradas): sin lookahead.
- DNA y análisis son de solo inserción: recalcular crea filas nuevas (versión + 1).
- FACT es un dato medido; HYPOTHESIS es una explicación posible que se valida aparte.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from supervisor.models.base import (
    Base,
    CreatedAt,
    Json,
    UTCDateTime,
    UUIDPk,
    str_enum,
)
from supervisor.models.enums import DataOrigin, FindingKind, Outcome

Ratio = Numeric(14, 6)


class FeatureDefinition(Base):
    """Catálogo versionado de variables del Trading DNA (fase 8): qué mide cada una, su tipo
    (numeric, boolean o categorical, que la fase 9 usa para generar condiciones) y cuándo
    queda NULL. De solo inserción: cambiar una definición = una fila con versión nueva."""

    __tablename__ = "feature_definitions"
    __table_args__ = (
        CheckConstraint("value_type IN ('numeric', 'boolean', 'categorical')", name="value_type"),
    )

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    group: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str | None] = mapped_column(String(8))
    value_type: Mapped[str] = mapped_column(String(16))
    label: Mapped[str] = mapped_column(String(128))
    unit: Mapped[str | None] = mapped_column(String(16))
    # La fase 9 solo busca patrones en variables comparables entre operaciones (no en
    # precios absolutos como el nivel de una EMA).
    searchable: Mapped[bool] = mapped_column(Boolean)
    formula_doc: Mapped[str] = mapped_column(Text)
    null_policy: Mapped[str] = mapped_column(Text)
    # False si todavía no hay fuente de datos (p. ej. noticias): siempre NULL.
    available: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[CreatedAt]


class TradeDna(Base):
    """Condiciones de mercado en el momento de la entrada (fase 8).

    De solo inserción, como el análisis: recalcular con otra versión del conjunto de
    variables o con más velas crea una fila con dna_version + 1, solo si cambia input_hash
    (sha256 de la operación, las velas usadas, las noticias y los parámetros). La vigente es
    la de mayor dna_version.

    Sin lookahead: solo se usan velas M1 con bar_time + 1 min <= data_cutoff (la hora de
    entrada) y velas agregadas ya cerradas; un CHECK lo garantiza para la última vela M1.
    `features` es {nombre: valor} con los nombres de feature_definitions; una variable que
    no se pudo calcular vale null y su motivo va en `null_reasons`."""

    __tablename__ = "trade_dna"
    __table_args__ = (
        UniqueConstraint("trade_id", "dna_version"),
        CheckConstraint(
            "last_bar_time IS NULL OR last_bar_time + interval '1 minute' <= data_cutoff",
            name="no_lookahead",
        ),
    )

    id: Mapped[UUIDPk]
    trade_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trades.trade_id"), index=True)
    dna_version: Mapped[int] = mapped_column(SmallInteger)
    feature_set_version: Mapped[int] = mapped_column(SmallInteger)
    input_hash: Mapped[str] = mapped_column(String(64))
    source: Mapped[DataOrigin] = mapped_column(str_enum(DataOrigin))
    # Hora de entrada: todo dato usado es estrictamente anterior.
    data_cutoff: Mapped[UTCDateTime]
    # Inicio de la última vela M1 usada (NULL si no había ninguna).
    last_bar_time: Mapped[UTCDateTime | None]
    features: Mapped[Json]
    null_reasons: Mapped[Json]
    # Cobertura de velas por timeframe y avisos.
    data_quality: Mapped[Json]
    # Resumen de las entradas (huellas para decidir si hay que recalcular).
    inputs: Mapped[Json]
    computed_at: Mapped[CreatedAt]


class TradeAnalysis(Base):
    """Análisis post-operación (fase 6). De solo inserción: re-analizar crea una versión nueva
    (analysis_version + 1) y la vigente es siempre la de mayor versión.

    input_hash resume todo lo que se usó (operación, eventos, velas, configuración y versión
    del analizador): si no cambia, no se crea versión nueva."""

    __tablename__ = "trade_analyses"
    __table_args__ = (UniqueConstraint("trade_id", "analysis_version"),)

    id: Mapped[UUIDPk]
    trade_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trades.trade_id"), index=True)
    analysis_version: Mapped[int] = mapped_column(SmallInteger)
    outcome: Mapped[Outcome] = mapped_column(str_enum(Outcome))
    analyzer_version: Mapped[str] = mapped_column(String(16))
    ruleset_version: Mapped[int] = mapped_column(SmallInteger)
    input_hash: Mapped[str] = mapped_column(String(64))
    # Datos de entrada resumidos (para auditar y para decidir si hay que re-analizar).
    inputs: Mapped[Json]
    # Lo que no se pudo analizar y por qué: [{"code": ..., "text": ...}].
    data_quality: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    created_at: Mapped[CreatedAt]

    findings: Mapped[list["AnalysisFinding"]] = relationship(
        back_populates="analysis", order_by="AnalysisFinding.position"
    )


class AnalysisFinding(Base):
    """Un hallazgo del análisis.

    - FACT: dato medido, con sus valores en `evidence`. Sin confianza ni soportes.
    - HYPOTHESIS: explicación posible generada por una regla versionada (`code` +
      `rule_version`), apoyada en los FACT de `supported_by` y con `confidence` "no_validada"
      hasta que la fase 9 la valide estadísticamente.
    La base de datos impide mezclar ambas cosas (CHECK fact_vs_hypothesis)."""

    __tablename__ = "analysis_findings"
    __table_args__ = (
        UniqueConstraint("analysis_id", "code"),
        CheckConstraint(
            "(kind = 'FACT' AND confidence IS NULL AND rule_version IS NULL"
            " AND supported_by = '[]'::jsonb)"
            " OR (kind = 'HYPOTHESIS' AND confidence IS NOT NULL AND rule_version IS NOT NULL"
            " AND jsonb_typeof(supported_by) = 'array' AND jsonb_array_length(supported_by) > 0)",
            name="fact_vs_hypothesis",
        ),
    )

    id: Mapped[UUIDPk]
    analysis_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trade_analyses.id"), index=True)
    position: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    kind: Mapped[FindingKind] = mapped_column(str_enum(FindingKind))
    code: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    evidence: Mapped[Json]
    confidence: Mapped[str | None] = mapped_column(String(16))
    rule_version: Mapped[int | None] = mapped_column(SmallInteger)
    supported_by: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    hypothesis_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("hypotheses.id"))
    created_at: Mapped[CreatedAt]

    analysis: Mapped[TradeAnalysis] = relationship(back_populates="findings")


class NewsEvent(Base):
    __tablename__ = "news_events"
    __table_args__ = (
        UniqueConstraint("source", "time_utc", "currency", "title"),
        Index("ix_news_events_time", "time_utc"),
    )

    id: Mapped[UUIDPk]
    time_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    currency: Mapped[str] = mapped_column(String(8))
    impact: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(256))
    source: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[CreatedAt]
