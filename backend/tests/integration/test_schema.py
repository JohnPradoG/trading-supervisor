"""Las migraciones crean exactamente el esquema de los modelos, y se pueden revertir."""

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect

from supervisor.models import Base
from tests.conftest import alembic_config


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
