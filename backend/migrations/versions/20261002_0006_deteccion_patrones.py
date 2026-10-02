"""detección de patrones y trampas (fase 9)

- pattern_runs (nueva, solo inserción): cada ejecución de la búsqueda en un alcance (versión
  de bot y, opcional, símbolo), con su split, cuántas candidatas generó y probó, cuántas
  pasaron cada tramo y un resumen.
- hypotheses: tipo (TRAP/EDGE), origen (BUSQUEDA/REGLA/MANUAL), versión de bot y símbolo,
  condición canónica, split y ejecución que la propusieron, nota del estado, inicio del
  forward y updated_at. Solo cambia su estado; la historia está en pattern_tests.
- pattern_tests: ejecución, si pasó el criterio y los detalles (todas las métricas). Índice
  único (hipótesis, tramo) para TRAIN, VALIDATION y OOS: el fuera de muestra se evalúa una
  sola vez.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _jsonb(name: str) -> sa.Column:
    return sa.Column(
        name, postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
    )


def _int(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), server_default="0", nullable=False)


def upgrade() -> None:
    op.create_table(
        "pattern_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("bot_version_id", sa.UUID(), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=True),
        sa.Column("split_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("n_trades", sa.Integer(), nullable=False),
        _int("n_train"),
        _int("n_validation"),
        _int("n_oos"),
        _int("n_forward"),
        _int("candidates_generated"),
        _int("candidates_tested"),
        _int("candidates_low_sample"),
        _int("passed_train"),
        _int("passed_validation"),
        _int("validated"),
        _int("rejected"),
        sa.Column("last_close_time", sa.DateTime(timezone=True), nullable=True),
        _jsonb("params"),
        _jsonb("summary"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["bot_version_id"],
            ["bot_versions.id"],
            name=op.f("fk_pattern_runs_bot_version_id_bot_versions"),
        ),
        sa.ForeignKeyConstraint(
            ["split_id"], ["data_splits.id"], name=op.f("fk_pattern_runs_split_id_data_splits")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pattern_runs")),
    )
    op.create_index(
        "ix_pattern_runs_version", "pattern_runs", ["bot_version_id", "started_at"], unique=False
    )

    op.add_column("hypotheses", sa.Column("kind", sa.String(length=8), nullable=True))
    op.add_column(
        "hypotheses",
        sa.Column("origin", sa.String(length=16), server_default="MANUAL", nullable=False),
    )
    op.add_column("hypotheses", sa.Column("bot_version_id", sa.UUID(), nullable=True))
    op.add_column("hypotheses", sa.Column("symbol", sa.String(length=64), nullable=True))
    op.add_column("hypotheses", sa.Column("condition_key", sa.Text(), nullable=True))
    op.add_column("hypotheses", sa.Column("split_id", sa.UUID(), nullable=True))
    op.add_column("hypotheses", sa.Column("run_id", sa.UUID(), nullable=True))
    op.add_column("hypotheses", sa.Column("status_note", sa.Text(), nullable=True))
    op.add_column(
        "hypotheses", sa.Column("forward_from", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "hypotheses",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_hypotheses_kind"), "hypotheses", "kind IS NULL OR kind IN ('TRAP', 'EDGE')"
    )
    op.create_foreign_key(
        op.f("fk_hypotheses_bot_version_id_bot_versions"),
        "hypotheses",
        "bot_versions",
        ["bot_version_id"],
        ["id"],
    )
    op.create_foreign_key(
        op.f("fk_hypotheses_split_id_data_splits"),
        "hypotheses",
        "data_splits",
        ["split_id"],
        ["id"],
    )
    op.create_foreign_key(
        op.f("fk_hypotheses_run_id_pattern_runs"), "hypotheses", "pattern_runs", ["run_id"], ["id"]
    )
    op.create_index(
        "ix_hypotheses_version_status", "hypotheses", ["bot_version_id", "status"], unique=False
    )

    op.add_column("pattern_tests", sa.Column("run_id", sa.UUID(), nullable=True))
    op.add_column("pattern_tests", sa.Column("passed", sa.Boolean(), nullable=True))
    op.add_column("pattern_tests", _jsonb("details"))
    op.create_foreign_key(
        op.f("fk_pattern_tests_run_id_pattern_runs"),
        "pattern_tests",
        "pattern_runs",
        ["run_id"],
        ["id"],
    )
    op.create_index(
        "uq_pattern_tests_once",
        "pattern_tests",
        ["hypothesis_id", "segment"],
        unique=True,
        postgresql_where=sa.text("segment IN ('TRAIN', 'VALIDATION', 'OOS')"),
    )
    op.execute(
        "CREATE TRIGGER pattern_runs_append_only BEFORE UPDATE OR DELETE ON pattern_runs "
        "FOR EACH ROW EXECUTE FUNCTION ts_forbid_change()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER pattern_runs_append_only ON pattern_runs")
    op.drop_index(
        "uq_pattern_tests_once",
        table_name="pattern_tests",
        postgresql_where=sa.text("segment IN ('TRAIN', 'VALIDATION', 'OOS')"),
    )
    op.drop_constraint(
        op.f("fk_pattern_tests_run_id_pattern_runs"), "pattern_tests", type_="foreignkey"
    )
    for column in ("details", "passed", "run_id"):
        op.drop_column("pattern_tests", column)
    op.drop_index("ix_hypotheses_version_status", table_name="hypotheses")
    for name in (
        "fk_hypotheses_run_id_pattern_runs",
        "fk_hypotheses_split_id_data_splits",
        "fk_hypotheses_bot_version_id_bot_versions",
    ):
        op.drop_constraint(op.f(name), "hypotheses", type_="foreignkey")
    op.drop_constraint(op.f("ck_hypotheses_kind"), "hypotheses", type_="check")
    for column in (
        "updated_at",
        "forward_from",
        "status_note",
        "run_id",
        "split_id",
        "condition_key",
        "symbol",
        "bot_version_id",
        "origin",
        "kind",
    ):
        op.drop_column("hypotheses", column)
    op.drop_index("ix_pattern_runs_version", table_name="pattern_runs")
    op.drop_table("pattern_runs")
