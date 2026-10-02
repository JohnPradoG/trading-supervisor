"""Las migraciones crean exactamente el esquema de los modelos, y se pueden revertir."""

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.orm import Session

from supervisor.models import Base, RawEvent
from tests.conftest import NOW, alembic_config


def test_all_tables_created(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert tables == set(Base.metadata.tables)
    assert len(tables) == 26


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
    assert len(set(inspect(engine).get_table_names()) - {"alembic_version"}) == 26


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


def test_migration_0004_roundtrip_keeps_existing_analyses(
    database_url: str, engine: Engine, terminal: dict
) -> None:
    """0004 se puede revertir y volver a aplicar con análisis ya guardados (incluida una
    hipótesis anterior a la regla FACT/HYPOTHESIS, que se conserva sin validar)."""
    with engine.begin() as conn:
        trade_id = conn.execute(
            text(
                "INSERT INTO trades (trade_id, account_id, position_id, magic_number, symbol,"
                " direction, order_type, source, entry_time, entry_price, initial_volume,"
                " max_volume) SELECT gen_random_uuid(), accounts.id, 424242, 1, 'X', 'BUY', 'UNKNOWN',"
                " 'DEMO', now(), 1, 1, 1 FROM terminals t JOIN accounts ON accounts.id = t.account_id"
                " WHERE t.id = :term RETURNING trade_id"
            ),
            {"term": terminal["terminal_id"]},
        ).scalar()
    cfg = alembic_config(database_url)
    engine.dispose()
    command.downgrade(cfg, "0003")
    assert "input_hash" not in _columns(engine, "trade_analyses")
    assert "supported_by" not in _columns(engine, "analysis_findings")
    with engine.begin() as conn:
        analysis_id = conn.execute(
            text(
                "INSERT INTO trade_analyses (id, trade_id, analysis_version, outcome)"
                " VALUES (gen_random_uuid(), :t, 1, 'LOSS') RETURNING id"
            ),
            {"t": trade_id},
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO analysis_findings (id, analysis_id, kind, code, text)"
                " VALUES (gen_random_uuid(), :a, 'HYPOTHESIS', 'H_OLD', 'antigua')"
            ),
            {"a": analysis_id},
        )
    engine.dispose()
    command.upgrade(cfg, "head")
    assert {"analyzer_version", "ruleset_version", "input_hash", "inputs", "data_quality"} <= (
        _columns(engine, "trade_analyses")
    )
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT analyzer_version, input_hash, data_quality FROM trade_analyses"
                " WHERE id = :a"
            ),
            {"a": analysis_id},
        ).one()
        assert tuple(row) == ("0.0.0", "", [])
        validated = conn.execute(
            text(
                "SELECT convalidated FROM pg_constraint"
                " WHERE conname = 'ck_analysis_findings_fact_vs_hypothesis'"
            )
        ).scalar()
        assert validated is False  # hay una hipótesis antigua: solo se exige a filas nuevas
    with engine.begin() as conn, pytest.raises(Exception, match="fact_vs_hypothesis"):
        conn.execute(
            text(
                "INSERT INTO analysis_findings (id, analysis_id, kind, code, text, confidence)"
                " VALUES (gen_random_uuid(), :a, 'FACT', 'F', 'x', 'alta')"
            ),
            {"a": analysis_id},
        )


def test_migration_0005_roundtrip_and_refuses_to_drop_old_dna(
    database_url: str, engine: Engine, terminal: dict
) -> None:
    """0005 rehace trade_dna (la de 0001 nunca se escribió); si tuviera filas se detiene en
    lugar de perderlas."""
    cfg = alembic_config(database_url)
    engine.dispose()
    command.downgrade(cfg, "0004")
    assert "trend_h1" in _columns(engine, "trade_dna")
    assert "value_type" not in _columns(engine, "feature_definitions")
    with engine.begin() as conn:
        trade_id = conn.execute(
            text(
                "INSERT INTO trades (trade_id, account_id, position_id, magic_number, symbol,"
                " direction, order_type, source, entry_time, entry_price, initial_volume,"
                " max_volume) SELECT gen_random_uuid(), accounts.id, 515151, 1, 'X', 'BUY', 'UNKNOWN',"
                " 'DEMO', now(), 1, 1, 1 FROM terminals t JOIN accounts ON accounts.id = t.account_id"
                " WHERE t.id = :term RETURNING trade_id"
            ),
            {"term": terminal["terminal_id"]},
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO trade_dna (trade_id, feature_set_version, source, data_cutoff)"
                " VALUES (:t, 1, 'SERVER', now())"
            ),
            {"t": trade_id},
        )
    engine.dispose()
    with pytest.raises(Exception, match="trade_dna tiene filas"):
        command.upgrade(cfg, "head")
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM trade_dna"))
    engine.dispose()
    command.upgrade(cfg, "head")
    assert {"dna_version", "input_hash", "features", "null_reasons"} <= _columns(
        engine, "trade_dna"
    )
    assert {"value_type", "label", "searchable", "unit"} <= _columns(engine, "feature_definitions")
