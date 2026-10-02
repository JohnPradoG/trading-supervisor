"""reglas de inmutabilidad en la base de datos

La historia nunca se sobrescribe. Esta migración lo hace cumplir en PostgreSQL, no solo en el
código Python, para que ningún error, script o acceso manual pueda alterarla:

- Tablas de solo inserción (sin UPDATE ni DELETE): bot_versions, trade_events,
  trade_analyses, analysis_findings, pattern_tests, data_splits.
- raw_events: no se borra y solo cambian sus columnas de procesamiento.
- Tablas que se actualizan pero nunca se borran: bots, deployments, trades, accounts.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY = (
    "bot_versions",
    "trade_events",
    "trade_analyses",
    "analysis_findings",
    "pattern_tests",
    "data_splits",
)
NO_DELETE = ("bots", "deployments", "trades", "accounts", "raw_events")


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION ts_forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'La tabla % es de solo inserción: % no permitido', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'restrict_violation';
        END $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION ts_raw_events_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.terminal_id IS DISTINCT FROM OLD.terminal_id
               OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
               OR NEW.event_type IS DISTINCT FROM OLD.event_type
               OR NEW.schema_version IS DISTINCT FROM OLD.schema_version
               OR NEW.payload IS DISTINCT FROM OLD.payload
               OR NEW.sent_at IS DISTINCT FROM OLD.sent_at
               OR NEW.received_at IS DISTINCT FROM OLD.received_at THEN
                RAISE EXCEPTION 'raw_events: solo se pueden cambiar status, attempts, processed_at y error'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END $$;
        """
    )
    for table in APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION ts_forbid_change()"
        )
    for table in NO_DELETE:
        op.execute(
            f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION ts_forbid_change()"
        )
    op.execute(
        "CREATE TRIGGER raw_events_guard BEFORE UPDATE ON raw_events "
        "FOR EACH ROW EXECUTE FUNCTION ts_raw_events_guard()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER raw_events_guard ON raw_events")
    for table in NO_DELETE:
        op.execute(f"DROP TRIGGER {table}_no_delete ON {table}")
    for table in APPEND_ONLY:
        op.execute(f"DROP TRIGGER {table}_append_only ON {table}")
    op.execute("DROP FUNCTION ts_raw_events_guard()")
    op.execute("DROP FUNCTION ts_forbid_change()")
