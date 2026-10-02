"""análisis post-operación versionado (fase 6)

- trade_analyses: analyzer_version, ruleset_version, input_hash (sha256 de todo lo que usó el
  análisis), inputs (resumen de esas entradas) y data_quality (lo que no se pudo analizar).
  Sigue siendo de solo inserción: re-analizar crea una fila con analysis_version + 1.
- analysis_findings: position (orden), confidence y rule_version (solo hipótesis),
  supported_by (códigos de los FACT que apoyan una hipótesis). Un CHECK impide que un FACT
  lleve confianza o soportes y que una HYPOTHESIS no los lleve (para las filas nuevas; las
  hipótesis anteriores a 0004, si las hubiera, se conservan sin validar). Un código por
  análisis.
- trades: índice parcial de operaciones cerradas por close_time (estadísticas y repaso).

Las columnas obligatorias se añaden con un valor por defecto temporal para las filas que ya
existieran (ADD COLUMN no dispara los triggers de solo inserción) y después se quita.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FACT_VS_HYPOTHESIS = (
    "(kind = 'FACT' AND confidence IS NULL AND rule_version IS NULL"
    " AND supported_by = '[]'::jsonb)"
    " OR (kind = 'HYPOTHESIS' AND confidence IS NOT NULL AND rule_version IS NOT NULL"
    " AND jsonb_typeof(supported_by) = 'array' AND jsonb_array_length(supported_by) > 0)"
)


def upgrade() -> None:
    op.add_column(
        "trade_analyses",
        sa.Column("analyzer_version", sa.String(length=16), server_default="0.0.0", nullable=False),
    )
    op.add_column(
        "trade_analyses",
        sa.Column("ruleset_version", sa.SmallInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "trade_analyses",
        sa.Column("input_hash", sa.String(length=64), server_default="", nullable=False),
    )
    for column in ("analyzer_version", "ruleset_version", "input_hash"):
        op.alter_column("trade_analyses", column, server_default=None)
    op.add_column(
        "trade_analyses",
        sa.Column(
            "inputs", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
    )
    op.add_column(
        "trade_analyses",
        sa.Column(
            "data_quality",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
    )

    op.add_column(
        "analysis_findings",
        sa.Column("position", sa.SmallInteger(), server_default="0", nullable=False),
    )
    op.add_column("analysis_findings", sa.Column("confidence", sa.String(length=16), nullable=True))
    op.add_column("analysis_findings", sa.Column("rule_version", sa.SmallInteger(), nullable=True))
    op.add_column(
        "analysis_findings",
        sa.Column(
            "supported_by",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        op.f("uq_analysis_findings_analysis_id_code"), "analysis_findings", ["analysis_id", "code"]
    )
    # NOT VALID: se aplica a toda fila nueva. Se valida sobre las existentes salvo que haya
    # hipótesis anteriores a esta migración (no tenían confianza ni soportes y la tabla no
    # admite UPDATE): esas quedan como historia tal cual.
    op.execute(
        "ALTER TABLE analysis_findings ADD CONSTRAINT ck_analysis_findings_fact_vs_hypothesis "
        f"CHECK ({_FACT_VS_HYPOTHESIS}) NOT VALID"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM analysis_findings WHERE kind = 'HYPOTHESIS') THEN
                ALTER TABLE analysis_findings
                    VALIDATE CONSTRAINT ck_analysis_findings_fact_vs_hypothesis;
            END IF;
        END $$;
        """
    )

    op.create_index(
        "ix_trades_closed",
        "trades",
        ["close_time"],
        unique=False,
        postgresql_where=sa.text("status = 'CLOSED'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_trades_closed", table_name="trades", postgresql_where=sa.text("status = 'CLOSED'")
    )
    op.drop_constraint(
        op.f("ck_analysis_findings_fact_vs_hypothesis"), "analysis_findings", type_="check"
    )
    op.drop_constraint(
        op.f("uq_analysis_findings_analysis_id_code"), "analysis_findings", type_="unique"
    )
    for column in ("supported_by", "rule_version", "confidence", "position"):
        op.drop_column("analysis_findings", column)
    for column in ("data_quality", "inputs", "input_hash", "ruleset_version", "analyzer_version"):
        op.drop_column("trade_analyses", column)
