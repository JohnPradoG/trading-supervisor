"""laboratorio de estrategias (fase 10)

- experiments: número correlativo (#001), título, símbolo opcional, filtro estructurado
  (condición de la fase 9) y updated_at. Un trigger impide cambiar lo que define el
  experimento (versión base, cambio, filtro, hipótesis...): solo cambian el estado y la última
  conclusión. No se borra.
- experiment_results (nueva, solo inserción): revisiones de resultados (FILTRO, BACKTEST o
  MANUAL), numeradas por experimento.
- backtest_runs: origen (siempre BACKTEST, con CHECK), etiqueta, formato y hash del archivo y
  las operaciones leídas. Solo inserción. El periodo puede ser de un solo día.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCE = sa.Enum(
    "LIVE",
    "DEMO",
    "BACKTEST",
    "FORWARD",
    "IMPORTED",
    name="tradesource",
    native_enum=False,
    create_constraint=True,
    length=32,
)

_GUARD = """
CREATE FUNCTION ts_experiments_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.number IS DISTINCT FROM OLD.number
       OR NEW.code IS DISTINCT FROM OLD.code
       OR NEW.title IS DISTINCT FROM OLD.title
       OR NEW.base_version_id IS DISTINCT FROM OLD.base_version_id
       OR NEW.candidate_version_id IS DISTINCT FROM OLD.candidate_version_id
       OR NEW.hypothesis_id IS DISTINCT FROM OLD.hypothesis_id
       OR NEW.symbol IS DISTINCT FROM OLD.symbol
       OR NEW.change_description IS DISTINCT FROM OLD.change_description
       OR NEW.filter_spec IS DISTINCT FROM OLD.filter_spec
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'experiments: solo se pueden cambiar status, conclusion y updated_at'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END $$;
"""


def _jsonb(name: str) -> sa.Column:
    return sa.Column(
        name, postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
    )


def upgrade() -> None:
    op.add_column("experiments", sa.Column("number", sa.Integer(), nullable=True))
    op.add_column(
        "experiments", sa.Column("title", sa.String(length=200), server_default="", nullable=False)
    )
    op.add_column("experiments", sa.Column("symbol", sa.String(length=64), nullable=True))
    op.add_column(
        "experiments",
        sa.Column("filter_spec", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "experiments",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    # Experimentos anteriores (si los hubiera): número por orden de creación y el código como
    # título.
    op.execute(
        "UPDATE experiments e SET number = o.n, title = e.code FROM "
        "(SELECT id, row_number() OVER (ORDER BY created_at, id) AS n FROM experiments) o "
        "WHERE o.id = e.id"
    )
    op.alter_column("experiments", "number", nullable=False)
    op.alter_column("experiments", "title", server_default=None)
    op.create_unique_constraint(op.f("uq_experiments_number"), "experiments", ["number"])
    op.execute(_GUARD)
    op.execute(
        "CREATE TRIGGER experiments_guard BEFORE UPDATE ON experiments "
        "FOR EACH ROW EXECUTE FUNCTION ts_experiments_guard()"
    )
    op.execute(
        "CREATE TRIGGER experiments_no_delete BEFORE DELETE ON experiments "
        "FOR EACH ROW EXECUTE FUNCTION ts_forbid_change()"
    )

    op.add_column(
        "backtest_runs", sa.Column("source", _SOURCE, server_default="BACKTEST", nullable=False)
    )
    op.add_column("backtest_runs", sa.Column("label", sa.String(length=64), nullable=True))
    op.add_column("backtest_runs", sa.Column("report_format", sa.String(length=8), nullable=True))
    op.add_column("backtest_runs", sa.Column("file_sha256", sa.String(length=64), nullable=True))
    op.add_column("backtest_runs", _jsonb("trades"))
    op.drop_constraint(op.f("ck_backtest_runs_period"), "backtest_runs", type_="check")
    op.create_check_constraint(
        op.f("ck_backtest_runs_period"), "backtest_runs", "period_end >= period_start"
    )
    op.create_check_constraint(
        op.f("ck_backtest_runs_solo_backtest"), "backtest_runs", "source = 'BACKTEST'"
    )
    op.execute(
        "CREATE TRIGGER backtest_runs_append_only BEFORE UPDATE OR DELETE ON backtest_runs "
        "FOR EACH ROW EXECUTE FUNCTION ts_forbid_change()"
    )

    op.create_table(
        "experiment_results",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("experiment_id", sa.UUID(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        _jsonb("results"),
        sa.Column("backtest_run_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('FILTRO', 'BACKTEST', 'MANUAL')", name=op.f("ck_experiment_results_kind")
        ),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_experiment_results_revision")),
        sa.ForeignKeyConstraint(
            ["backtest_run_id"],
            ["backtest_runs.id"],
            name=op.f("fk_experiment_results_backtest_run_id_backtest_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
            name=op.f("fk_experiment_results_experiment_id_experiments"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_experiment_results")),
        sa.UniqueConstraint(
            "experiment_id", "revision", name=op.f("uq_experiment_results_experiment_id_revision")
        ),
    )
    op.create_index(
        op.f("ix_experiment_results_experiment_id"),
        "experiment_results",
        ["experiment_id"],
        unique=False,
    )
    op.execute(
        "CREATE TRIGGER experiment_results_append_only BEFORE UPDATE OR DELETE ON "
        "experiment_results FOR EACH ROW EXECUTE FUNCTION ts_forbid_change()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER experiment_results_append_only ON experiment_results")
    op.drop_index(op.f("ix_experiment_results_experiment_id"), table_name="experiment_results")
    op.drop_table("experiment_results")

    op.execute("DROP TRIGGER backtest_runs_append_only ON backtest_runs")
    op.drop_constraint(op.f("ck_backtest_runs_solo_backtest"), "backtest_runs", type_="check")
    op.drop_constraint(op.f("ck_backtest_runs_period"), "backtest_runs", type_="check")
    op.create_check_constraint(
        op.f("ck_backtest_runs_period"), "backtest_runs", "period_end > period_start"
    )
    op.drop_column("backtest_runs", "trades")
    op.drop_column("backtest_runs", "file_sha256")
    op.drop_column("backtest_runs", "report_format")
    op.drop_column("backtest_runs", "label")
    op.drop_column("backtest_runs", "source")

    op.execute("DROP TRIGGER experiments_no_delete ON experiments")
    op.execute("DROP TRIGGER experiments_guard ON experiments")
    op.execute("DROP FUNCTION ts_experiments_guard()")
    op.drop_constraint(op.f("uq_experiments_number"), "experiments", type_="unique")
    op.drop_column("experiments", "updated_at")
    op.drop_column("experiments", "filter_spec")
    op.drop_column("experiments", "symbol")
    op.drop_column("experiments", "title")
    op.drop_column("experiments", "number")
