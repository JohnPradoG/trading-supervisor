"""columnas para el worker de operaciones (fase 5)

- raw_events.event_time: hora del hecho en MT5 (time_utc del payload). El worker procesa por
  este orden, no por orden de llegada, para que un backfill no altere la secuencia. Se rellena
  para las filas existentes y pasa a estar protegida por el trigger de raw_events.
- raw_events.next_attempt_at: cuándo reintentar un evento diferido (p. ej. un cierre que llegó
  antes que su apertura). Es columna de procesamiento: el trigger permite cambiarla.
- trades.current_volume, current_sl, current_tp: estado vivo de la posición (volumen abierto
  y último SL/TP conocido).
- El índice parcial de pendientes pasa a ordenar por event_time.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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
       OR NEW.received_at IS DISTINCT FROM OLD.received_at{extra} THEN
        RAISE EXCEPTION 'raw_events: solo se pueden cambiar {allowed}'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END $$;
"""


def upgrade() -> None:
    op.add_column("raw_events", sa.Column("event_time", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "raw_events", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        "UPDATE raw_events SET event_time = (payload->>'time_utc')::timestamptz "
        "WHERE payload ? 'time_utc'"
    )
    op.execute(
        _GUARD.format(
            extra="\n       OR NEW.event_time IS DISTINCT FROM OLD.event_time",
            allowed="status, attempts, processed_at, error y next_attempt_at",
        )
    )
    op.drop_index("ix_raw_events_pending", table_name="raw_events")
    op.create_index(
        "ix_raw_events_pending",
        "raw_events",
        ["event_time", "id"],
        unique=False,
        postgresql_where=sa.text("status = 'PENDING'"),
    )

    op.add_column("trades", sa.Column("current_volume", sa.Numeric(16, 4), nullable=True))
    op.add_column("trades", sa.Column("current_sl", sa.Numeric(24, 10), nullable=True))
    op.add_column("trades", sa.Column("current_tp", sa.Numeric(24, 10), nullable=True))


def downgrade() -> None:
    op.drop_column("trades", "current_tp")
    op.drop_column("trades", "current_sl")
    op.drop_column("trades", "current_volume")

    op.drop_index("ix_raw_events_pending", table_name="raw_events")
    op.create_index(
        "ix_raw_events_pending",
        "raw_events",
        ["terminal_id", "sent_at"],
        unique=False,
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.execute(_GUARD.format(extra="", allowed="status, attempts, processed_at y error"))
    op.drop_column("raw_events", "next_attempt_at")
    op.drop_column("raw_events", "event_time")
