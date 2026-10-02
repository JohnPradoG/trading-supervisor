"""importación del historial de MT5

- raw_events.origin: EA (en vivo, el valor de todas las filas existentes) o HISTORY_IMPORT
  (script SupervisorImportHistory). La marca la decide el lote (`origin` en el cuerpo) y queda
  protegida por el trigger de raw_events como el resto del contenido recibido.
- ix_raw_events_pending_live: pendientes del EA en vivo por hora del hecho. El worker los
  atiende antes que una importación grande, que avanza en lotes acotados.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORIGIN = sa.Enum(
    "EA",
    "HISTORY_IMPORT",
    name="raweventorigin",
    native_enum=False,
    create_constraint=True,
    length=32,
)

_GUARD = """
CREATE OR REPLACE FUNCTION ts_raw_events_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.terminal_id IS DISTINCT FROM OLD.terminal_id
       OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
       OR NEW.event_type IS DISTINCT FROM OLD.event_type
       OR NEW.schema_version IS DISTINCT FROM OLD.schema_version
       OR NEW.payload IS DISTINCT FROM OLD.payload
       OR NEW.sent_at IS DISTINCT FROM OLD.sent_at
       OR NEW.received_at IS DISTINCT FROM OLD.received_at
       OR NEW.event_time IS DISTINCT FROM OLD.event_time{extra} THEN
        RAISE EXCEPTION 'raw_events: solo se pueden cambiar status, attempts, processed_at, error y next_attempt_at'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END $$;
"""


def upgrade() -> None:
    op.add_column(
        "raw_events",
        sa.Column("origin", _ORIGIN, server_default="EA", nullable=False),
    )
    op.execute(_GUARD.format(extra="\n       OR NEW.origin IS DISTINCT FROM OLD.origin"))
    op.create_index(
        "ix_raw_events_pending_live",
        "raw_events",
        ["event_time", "id"],
        unique=False,
        postgresql_where=sa.text("status = 'PENDING' AND origin = 'EA'"),
    )


def downgrade() -> None:
    op.drop_index("ix_raw_events_pending_live", table_name="raw_events")
    op.execute(_GUARD.format(extra=""))
    op.drop_column("raw_events", "origin")
