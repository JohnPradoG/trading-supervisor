"""alertas (sección 15): deduplicación, resolución y entrega por Telegram

- alerts: clave de deduplicación (dedup_key), evento (DISPARO o RESUELTA), operación e
  hipótesis relacionadas y el estado de entrega (PENDING, SENT, FAILED o SKIPPED si no hay
  canal configurado) con sus intentos, último error y próximo reintento.
- Las alertas son de solo inserción: un trigger solo deja cambiar las columnas de entrega y
  acknowledged_at, y este último solo de NULL a una fecha (el reconocimiento no se deshace).
  No se borran.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUARD = """
CREATE FUNCTION ts_alerts_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.rule IS DISTINCT FROM OLD.rule
       OR NEW.severity IS DISTINCT FROM OLD.severity
       OR NEW.bot_version_id IS DISTINCT FROM OLD.bot_version_id
       OR NEW.deployment_id IS DISTINCT FROM OLD.deployment_id
       OR NEW.message IS DISTINCT FROM OLD.message
       OR NEW.data IS DISTINCT FROM OLD.data
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.dedup_key IS DISTINCT FROM OLD.dedup_key
       OR NEW.event IS DISTINCT FROM OLD.event
       OR NEW.trade_id IS DISTINCT FROM OLD.trade_id
       OR NEW.hypothesis_id IS DISTINCT FROM OLD.hypothesis_id THEN
        RAISE EXCEPTION 'alerts: solo se pueden cambiar la entrega y acknowledged_at'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF OLD.acknowledged_at IS NOT NULL
       AND NEW.acknowledged_at IS DISTINCT FROM OLD.acknowledged_at THEN
        RAISE EXCEPTION 'alerts: una alerta reconocida no cambia de reconocimiento'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END $$;
"""


def upgrade() -> None:
    op.add_column(
        "alerts", sa.Column("dedup_key", sa.String(length=200), server_default="", nullable=False)
    )
    op.add_column(
        "alerts", sa.Column("event", sa.String(length=16), server_default="DISPARO", nullable=False)
    )
    op.add_column("alerts", sa.Column("trade_id", sa.UUID(), nullable=True))
    op.add_column("alerts", sa.Column("hypothesis_id", sa.UUID(), nullable=True))
    op.add_column(
        "alerts",
        sa.Column(
            "delivery_status", sa.String(length=16), server_default="SKIPPED", nullable=False
        ),
    )
    op.add_column(
        "alerts",
        sa.Column("delivery_attempts", sa.SmallInteger(), server_default="0", nullable=False),
    )
    op.add_column("alerts", sa.Column("delivery_error", sa.Text(), nullable=True))
    op.add_column(
        "alerts", sa.Column("next_delivery_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("alerts", sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        op.f("fk_alerts_trade_id_trades"), "alerts", "trades", ["trade_id"], ["trade_id"]
    )
    op.create_foreign_key(
        op.f("fk_alerts_hypothesis_id_hypotheses"),
        "alerts",
        "hypotheses",
        ["hypothesis_id"],
        ["id"],
    )
    op.create_check_constraint(
        op.f("ck_alerts_event"), "alerts", "event IN ('DISPARO', 'RESUELTA')"
    )
    op.create_check_constraint(
        op.f("ck_alerts_delivery_status"),
        "alerts",
        "delivery_status IN ('PENDING', 'SENT', 'FAILED', 'SKIPPED')",
    )
    op.create_index("ix_alerts_dedup_key_created", "alerts", ["dedup_key", "created_at"])
    op.create_index("ix_alerts_created_at", "alerts", ["created_at"])
    op.create_index(
        "ix_alerts_delivery_pending",
        "alerts",
        ["created_at"],
        postgresql_where=sa.text("delivery_status = 'PENDING'"),
    )
    op.create_index(
        "ix_alerts_unacknowledged",
        "alerts",
        ["created_at"],
        postgresql_where=sa.text("acknowledged_at IS NULL AND event = 'DISPARO'"),
    )
    op.execute(_GUARD)
    op.execute(
        "CREATE TRIGGER alerts_guard BEFORE UPDATE ON alerts "
        "FOR EACH ROW EXECUTE FUNCTION ts_alerts_guard()"
    )
    op.execute(
        "CREATE TRIGGER alerts_no_delete BEFORE DELETE ON alerts "
        "FOR EACH ROW EXECUTE FUNCTION ts_forbid_change()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER alerts_no_delete ON alerts")
    op.execute("DROP TRIGGER alerts_guard ON alerts")
    op.execute("DROP FUNCTION ts_alerts_guard()")
    op.drop_index("ix_alerts_unacknowledged", table_name="alerts")
    op.drop_index("ix_alerts_delivery_pending", table_name="alerts")
    op.drop_index("ix_alerts_created_at", table_name="alerts")
    op.drop_index("ix_alerts_dedup_key_created", table_name="alerts")
    op.drop_constraint(op.f("ck_alerts_delivery_status"), "alerts", type_="check")
    op.drop_constraint(op.f("ck_alerts_event"), "alerts", type_="check")
    op.drop_constraint(op.f("fk_alerts_hypothesis_id_hypotheses"), "alerts", type_="foreignkey")
    op.drop_constraint(op.f("fk_alerts_trade_id_trades"), "alerts", type_="foreignkey")
    for column in (
        "sent_at",
        "next_delivery_at",
        "delivery_error",
        "delivery_attempts",
        "delivery_status",
        "hypothesis_id",
        "trade_id",
        "event",
        "dedup_key",
    ):
        op.drop_column("alerts", column)
