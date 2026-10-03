"""diagnóstico de bots: ejecuciones de solo inserción

- diagnosis_runs (nueva, solo inserción): cada diagnóstico de una versión de bot (y, opcional,
  un símbolo) con el hash de sus entradas, la versión del diagnóstico, el split de la fase 9
  usado, los parámetros y el informe completo en JSON (qué está fallando, qué cambiar, qué
  funciona mejor). Único (versión, hash): las mismas entradas no se recalculan. Nunca se
  modifica ni se borra: la historia queda.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _jsonb(name: str) -> sa.Column:
    return sa.Column(
        name, postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "diagnosis_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("bot_version_id", sa.UUID(), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=True),
        sa.Column("split_id", sa.UUID(), nullable=True),
        sa.Column("inputs_hash", sa.String(length=64), nullable=False),
        sa.Column("diagnosis_version", sa.String(length=16), nullable=False),
        sa.Column("n_trades", sa.Integer(), nullable=False),
        sa.Column("last_close_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        _jsonb("params"),
        _jsonb("report"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["bot_version_id"],
            ["bot_versions.id"],
            name=op.f("fk_diagnosis_runs_bot_version_id_bot_versions"),
        ),
        sa.ForeignKeyConstraint(
            ["split_id"], ["data_splits.id"], name=op.f("fk_diagnosis_runs_split_id_data_splits")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_diagnosis_runs")),
        sa.UniqueConstraint(
            "bot_version_id",
            "inputs_hash",
            name=op.f("uq_diagnosis_runs_bot_version_id_inputs_hash"),
        ),
    )
    op.create_index("ix_diagnosis_runs_version", "diagnosis_runs", ["bot_version_id", "created_at"])
    op.execute(
        "CREATE TRIGGER diagnosis_runs_append_only BEFORE UPDATE OR DELETE ON diagnosis_runs "
        "FOR EACH ROW EXECUTE FUNCTION ts_forbid_change()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER diagnosis_runs_append_only ON diagnosis_runs")
    op.drop_index("ix_diagnosis_runs_version", table_name="diagnosis_runs")
    op.drop_table("diagnosis_runs")
