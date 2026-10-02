"""Las migraciones crean exactamente el esquema de los modelos, y se pueden revertir."""

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect, select
from sqlalchemy.orm import Session

from supervisor.models import Base, RawEvent
from tests.conftest import NOW, alembic_config


def test_all_tables_created(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert tables == set(Base.metadata.tables)
    assert len(tables) == 25


def test_migrations_match_models(engine: Engine) -> None:
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == [], f"Los modelos y las migraciones difieren: {diff}"


def test_downgrade_and_upgrade_roundtrip(database_url: str, engine: Engine) -> None:
    cfg = alembic_config(database_url)
    engine.dispose()
    command.downgrade(cfg, "base")
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    command.upgrade(cfg, "head")
    assert len(set(inspect(engine).get_table_names()) - {"alembic_version"}) == 25


def _columns(engine: Engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def test_migration_0003_roundtrip_backfills_event_time(
    database_url: str, engine: Engine, terminal: dict
) -> None:
    """0003 se puede revertir y volver a aplicar; al subir rellena event_time desde el
    payload de los eventos que ya existían."""
    with Session(engine) as session:
        session.add(
            RawEvent(
                terminal_id=terminal["terminal_id"],
                idempotency_key="mig:1",
                event_type="DEAL",
                schema_version=1,
                payload={"type": "DEAL", "time_utc": "2026-10-02T11:59:58.250000Z"},
                sent_at=NOW,
                event_time=NOW,
            )
        )
        session.commit()
    cfg = alembic_config(database_url)
    engine.dispose()
    command.downgrade(cfg, "0002")
    assert "event_time" not in _columns(engine, "raw_events")
    assert "current_volume" not in _columns(engine, "trades")
    engine.dispose()
    command.upgrade(cfg, "head")
    assert {"event_time", "next_attempt_at"} <= _columns(engine, "raw_events")
    assert {"current_volume", "current_sl", "current_tp"} <= _columns(engine, "trades")
    with Session(engine) as session:
        restored = session.scalar(
            select(RawEvent.event_time).where(RawEvent.idempotency_key == "mig:1")
        )
    assert restored.isoformat() == "2026-10-02T11:59:58.250000+00:00"
