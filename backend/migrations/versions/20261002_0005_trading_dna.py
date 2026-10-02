"""Trading DNA versionado (fase 8)

- trade_dna se rehace: la tabla de 0001 (una columna por variable y clave trade_id +
  feature_set_version) nunca se llegó a escribir. La nueva guarda una fila por versión del
  DNA de cada operación (dna_version, de solo inserción), con input_hash para no duplicar
  versiones, las variables en `features` (JSONB, nombres de feature_definitions), sus
  motivos de NULL, la cobertura de velas y la última vela M1 usada. Un CHECK impide que esa
  vela termine después de la entrada (sin lookahead). Si la tabla antigua tuviera filas, la
  migración se detiene en lugar de perderlas.
- feature_definitions: tipo de valor (numeric, boolean, categorical), etiqueta, unidad y si
  la fase 9 puede buscar patrones con la variable. Pasa a ser de solo inserción.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DATA_ORIGIN = sa.Enum(
    "EA", "SERVER", name="dataorigin", native_enum=False, create_constraint=True, length=32
)


def _jsonb(name: str) -> sa.Column:
    return sa.Column(
        name, postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
    )


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM trade_dna) THEN
                RAISE EXCEPTION 'trade_dna tiene filas: migrarlas a mano antes de 0005';
            END IF;
        END $$;
        """
    )
    op.drop_table("trade_dna")
    op.create_table(
        "trade_dna",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("trade_id", sa.UUID(), nullable=False),
        sa.Column("dna_version", sa.SmallInteger(), nullable=False),
        sa.Column("feature_set_version", sa.SmallInteger(), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("source", _DATA_ORIGIN, nullable=False),
        sa.Column("data_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_bar_time", sa.DateTime(timezone=True), nullable=True),
        _jsonb("features"),
        _jsonb("null_reasons"),
        _jsonb("data_quality"),
        _jsonb("inputs"),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "last_bar_time IS NULL OR last_bar_time + interval '1 minute' <= data_cutoff",
            name=op.f("ck_trade_dna_no_lookahead"),
        ),
        sa.ForeignKeyConstraint(
            ["trade_id"], ["trades.trade_id"], name=op.f("fk_trade_dna_trade_id_trades")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trade_dna")),
        sa.UniqueConstraint(
            "trade_id", "dna_version", name=op.f("uq_trade_dna_trade_id_dna_version")
        ),
    )
    op.create_index(op.f("ix_trade_dna_trade_id"), "trade_dna", ["trade_id"], unique=False)

    columns = (
        ("value_type", sa.String(length=16), "numeric"),
        ("label", sa.String(length=128), ""),
        ("searchable", sa.Boolean(), "false"),
    )
    for name, type_, default in columns:
        op.add_column(
            "feature_definitions",
            sa.Column(name, type_, server_default=default, nullable=False),
        )
        op.alter_column("feature_definitions", name, server_default=None)
    op.add_column("feature_definitions", sa.Column("unit", sa.String(length=16), nullable=True))
    op.create_check_constraint(
        op.f("ck_feature_definitions_value_type"),
        "feature_definitions",
        "value_type IN ('numeric', 'boolean', 'categorical')",
    )
    for table in ("trade_dna", "feature_definitions"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION ts_forbid_change()"
        )


def downgrade() -> None:
    for table in ("trade_dna", "feature_definitions"):
        op.execute(f"DROP TRIGGER {table}_append_only ON {table}")
    # Las definiciones las registra el código de 0005 con sus columnas nuevas: al bajar se
    # borran y se vuelven a registrar completas al subir.
    op.execute("DELETE FROM feature_definitions")
    op.drop_constraint(
        op.f("ck_feature_definitions_value_type"), "feature_definitions", type_="check"
    )
    for column in ("unit", "searchable", "label", "value_type"):
        op.drop_column("feature_definitions", column)
    op.drop_index(op.f("ix_trade_dna_trade_id"), table_name="trade_dna")
    op.drop_table("trade_dna")
    # Tabla original de 0001.
    op.create_table(
        "trade_dna",
        sa.Column("trade_id", sa.UUID(), nullable=False),
        sa.Column("feature_set_version", sa.SmallInteger(), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "source",
            sa.Enum(
                "EA",
                "SERVER",
                name="dataorigin",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("data_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trend_m1", sa.SmallInteger(), nullable=True),
        sa.Column("trend_m5", sa.SmallInteger(), nullable=True),
        sa.Column("trend_m15", sa.SmallInteger(), nullable=True),
        sa.Column("trend_h1", sa.SmallInteger(), nullable=True),
        sa.Column("trend_h4", sa.SmallInteger(), nullable=True),
        sa.Column("trend_d1", sa.SmallInteger(), nullable=True),
        sa.Column("ema20", sa.Numeric(precision=24, scale=10), nullable=True),
        sa.Column("ema50", sa.Numeric(precision=24, scale=10), nullable=True),
        sa.Column("ema200", sa.Numeric(precision=24, scale=10), nullable=True),
        sa.Column("rsi", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column("atr", sa.Numeric(precision=24, scale=10), nullable=True),
        sa.Column("roc", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column("adx", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column("market_structure", sa.String(length=16), nullable=True),
        sa.Column("last_swing_high", sa.Numeric(precision=24, scale=10), nullable=True),
        sa.Column("last_swing_low", sa.Numeric(precision=24, scale=10), nullable=True),
        sa.Column("bos_recent", sa.Boolean(), nullable=True),
        sa.Column("choch_recent", sa.Boolean(), nullable=True),
        sa.Column("is_range", sa.Boolean(), nullable=True),
        sa.Column("liquidity_sweep", sa.Boolean(), nullable=True),
        sa.Column("equal_highs", sa.Boolean(), nullable=True),
        sa.Column("equal_lows", sa.Boolean(), nullable=True),
        sa.Column("dist_prev_high_points", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("dist_prev_low_points", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("fvg_present", sa.Boolean(), nullable=True),
        sa.Column("fvg_direction", sa.SmallInteger(), nullable=True),
        sa.Column("fvg_size_points", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("fvg_distance_points", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("ob_present", sa.Boolean(), nullable=True),
        sa.Column("ob_direction", sa.SmallInteger(), nullable=True),
        sa.Column("ob_distance_points", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("spread_points", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("recent_range_points", sa.Numeric(precision=20, scale=4), nullable=True),
        sa.Column("relative_volatility", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column("weekday", sa.SmallInteger(), nullable=True),
        sa.Column("hour_utc", sa.SmallInteger(), nullable=True),
        sa.Column("session_asia", sa.Boolean(), nullable=True),
        sa.Column("session_london", sa.Boolean(), nullable=True),
        sa.Column("session_newyork", sa.Boolean(), nullable=True),
        sa.Column("news_nearby", sa.Boolean(), nullable=True),
        sa.Column("minutes_to_news", sa.Integer(), nullable=True),
        sa.Column("news_impact", sa.String(length=16), nullable=True),
        sa.Column("news_type", sa.String(length=64), nullable=True),
        sa.Column(
            "extra", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "null_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["trade_id"], ["trades.trade_id"], name=op.f("fk_trade_dna_trade_id_trades")
        ),
        sa.PrimaryKeyConstraint("trade_id", "feature_set_version", name=op.f("pk_trade_dna")),
    )
