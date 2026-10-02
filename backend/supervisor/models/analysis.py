"""Grupo D: Trading DNA, análisis post-operación y noticias.

Reglas:
- Una variable que no se puede obtener queda NULL y su motivo va en trade_dna.null_reasons.
- El DNA se calcula solo con datos anteriores a la entrada (velas cerradas): sin lookahead.
- Los análisis son de solo inserción: una versión nueva del analizador crea filas nuevas.
- FACT es un dato medido; HYPOTHESIS es una explicación posible que se valida aparte.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from supervisor.models.base import (
    Base,
    CreatedAt,
    Json,
    Points,
    Price,
    UTCDateTime,
    UUIDPk,
    str_enum,
)
from supervisor.models.enums import DataOrigin, FindingKind, Outcome

Ratio = Numeric(14, 6)


class FeatureDefinition(Base):
    """Catálogo versionado de variables del DNA: cómo se calcula cada una y cuándo es NULL."""

    __tablename__ = "feature_definitions"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    group: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str | None] = mapped_column(String(8))
    formula_doc: Mapped[str] = mapped_column(Text)
    null_policy: Mapped[str] = mapped_column(Text)
    available: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[CreatedAt]


class TradeDna(Base):
    """Condiciones de mercado en el momento de la entrada. Una fila por versión del conjunto de
    variables; recalcular con otra versión añade una fila, no sobrescribe."""

    __tablename__ = "trade_dna"

    trade_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trades.trade_id"), primary_key=True)
    feature_set_version: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    computed_at: Mapped[CreatedAt]
    source: Mapped[DataOrigin] = mapped_column(str_enum(DataOrigin))
    # Última vela cerrada usada en el cálculo; debe ser anterior a la entrada.
    data_cutoff: Mapped[UTCDateTime]

    # Tendencia por timeframe: -1 bajista, 0 rango, 1 alcista
    trend_m1: Mapped[int | None] = mapped_column(SmallInteger)
    trend_m5: Mapped[int | None] = mapped_column(SmallInteger)
    trend_m15: Mapped[int | None] = mapped_column(SmallInteger)
    trend_h1: Mapped[int | None] = mapped_column(SmallInteger)
    trend_h4: Mapped[int | None] = mapped_column(SmallInteger)
    trend_d1: Mapped[int | None] = mapped_column(SmallInteger)

    # Indicadores en el timeframe principal del bot
    ema20: Mapped[Price | None]
    ema50: Mapped[Price | None]
    ema200: Mapped[Price | None]
    rsi: Mapped[Decimal | None] = mapped_column(Ratio)
    atr: Mapped[Price | None]
    roc: Mapped[Decimal | None] = mapped_column(Ratio)
    adx: Mapped[Decimal | None] = mapped_column(Ratio)

    # Estructura
    market_structure: Mapped[str | None] = mapped_column(String(16))
    last_swing_high: Mapped[Price | None]
    last_swing_low: Mapped[Price | None]
    bos_recent: Mapped[bool | None] = mapped_column(Boolean)
    choch_recent: Mapped[bool | None] = mapped_column(Boolean)
    is_range: Mapped[bool | None] = mapped_column(Boolean)

    # Liquidez
    liquidity_sweep: Mapped[bool | None] = mapped_column(Boolean)
    equal_highs: Mapped[bool | None] = mapped_column(Boolean)
    equal_lows: Mapped[bool | None] = mapped_column(Boolean)
    dist_prev_high_points: Mapped[Points | None]
    dist_prev_low_points: Mapped[Points | None]

    # FVG y order blocks
    fvg_present: Mapped[bool | None] = mapped_column(Boolean)
    fvg_direction: Mapped[int | None] = mapped_column(SmallInteger)
    fvg_size_points: Mapped[Points | None]
    fvg_distance_points: Mapped[Points | None]
    ob_present: Mapped[bool | None] = mapped_column(Boolean)
    ob_direction: Mapped[int | None] = mapped_column(SmallInteger)
    ob_distance_points: Mapped[Points | None]

    # Volatilidad
    spread_points: Mapped[Points | None]
    recent_range_points: Mapped[Points | None]
    relative_volatility: Mapped[Decimal | None] = mapped_column(Ratio)

    # Tiempo (siempre calculado desde UTC)
    weekday: Mapped[int | None] = mapped_column(SmallInteger)
    hour_utc: Mapped[int | None] = mapped_column(SmallInteger)
    session_asia: Mapped[bool | None] = mapped_column(Boolean)
    session_london: Mapped[bool | None] = mapped_column(Boolean)
    session_newyork: Mapped[bool | None] = mapped_column(Boolean)

    # Noticias (NULL hasta que exista la fuente de calendario)
    news_nearby: Mapped[bool | None] = mapped_column(Boolean)
    minutes_to_news: Mapped[int | None] = mapped_column(Integer)
    news_impact: Mapped[str | None] = mapped_column(String(16))
    news_type: Mapped[str | None] = mapped_column(String(64))

    extra: Mapped[Json]
    null_reasons: Mapped[Json]


class TradeAnalysis(Base):
    __tablename__ = "trade_analyses"
    __table_args__ = (UniqueConstraint("trade_id", "analysis_version"),)

    id: Mapped[UUIDPk]
    trade_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trades.trade_id"), index=True)
    analysis_version: Mapped[int] = mapped_column(SmallInteger)
    outcome: Mapped[Outcome] = mapped_column(str_enum(Outcome))
    created_at: Mapped[CreatedAt]

    findings: Mapped[list["AnalysisFinding"]] = relationship(back_populates="analysis")


class AnalysisFinding(Base):
    __tablename__ = "analysis_findings"

    id: Mapped[UUIDPk]
    analysis_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trade_analyses.id"), index=True)
    kind: Mapped[FindingKind] = mapped_column(str_enum(FindingKind))
    code: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    evidence: Mapped[Json]
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
