"""Las pruebas de integración usan un PostgreSQL real: la idempotencia y la inmutabilidad
dependen de restricciones y triggers que SQLite no tiene.

SUPERVISOR_TEST_DATABASE_URL apunta a un servidor donde el usuario puede crear bases; cada
ejecución crea una base temporal, aplica las migraciones y la elimina al terminar.
"""

import os
import random
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from supervisor.config import Settings
from supervisor.main import create_app
from supervisor.models import Account, ApiClient, Bot, BotVersion, Broker, Terminal
from supervisor.models.enums import AccountType, MarginMode
from supervisor.security.api_keys import generate_api_key

BACKEND_DIR = Path(__file__).resolve().parent.parent


def alembic_config(url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    cfg.attributes["database_url"] = url
    return cfg


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    admin_url = os.environ.get("SUPERVISOR_TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("SUPERVISOR_TEST_DATABASE_URL no está definida")
    name = f"ts_test_{uuid.uuid4().hex[:10]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(admin_url).set(database=name).render_as_string(hide_password=False)
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    command.upgrade(alembic_config(database_url), "head")
    eng = create_engine(database_url, connect_args={"options": "-c timezone=UTC"})
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Cada prueba corre dentro de una transacción que se revierte al final."""
    conn = engine.connect()
    trans = conn.begin()
    sess = Session(bind=conn, join_transaction_mode="create_savepoint")
    try:
        yield sess
    finally:
        sess.close()
        trans.rollback()
        conn.close()


NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def world(session: Session) -> dict:
    """Broker, cuenta, terminal, bot y versión mínimos para las pruebas."""
    broker = Broker(name=f"Broker {uuid.uuid4().hex[:6]}")
    account = Account(
        broker=broker,
        login=int(uuid.uuid4().int % 10**9),
        server="Demo-Server",
        currency="USD",
        account_type=AccountType.DEMO,
        margin_mode=MarginMode.HEDGING,
    )
    session.add_all([broker, account])
    session.flush()
    terminal = Terminal(account_id=account.id, name=f"term-{uuid.uuid4().hex[:6]}")
    bot = Bot(name=f"EA_Nasdaq_FVG_Retest_{uuid.uuid4().hex[:4]}", strategy="FVG retest")
    session.add_all([terminal, bot])
    session.flush()
    version = BotVersion(
        bot_id=bot.bot_id, version="1.0.0", main_timeframe="M5", params={}, released_at=NOW
    )
    session.add(version)
    session.flush()
    return {
        "broker": broker,
        "account": account,
        "terminal": terminal,
        "bot": bot,
        "version": version,
    }


# Fixtures de la API -----------------------------------------------------------------------

ADMIN_TOKEN = "t" * 48


@pytest.fixture(scope="module")
def client(engine: Engine, database_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=database_url, admin_token=ADMIN_TOKEN, log_level="WARNING")
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client
    app.state.engine.dispose()


@pytest.fixture
def terminal(engine: Engine) -> dict:
    """Cuenta + terminal + API key, confirmados en la base (la API usa su propia conexión)."""
    login = random.randint(10_000_000, 99_999_999)
    key = generate_api_key()
    with Session(engine) as session:
        broker = Broker(name=f"Exness-{uuid.uuid4().hex[:8]}")
        account = Account(
            broker=broker,
            login=login,
            server="Exness-MT5Trial",
            currency="USD",
            account_type=AccountType.DEMO,
            margin_mode=MarginMode.HEDGING,
        )
        session.add_all([broker, account])
        session.flush()
        term = Terminal(account_id=account.id, name=f"win-{uuid.uuid4().hex[:8]}")
        session.add(term)
        session.flush()
        api_client = ApiClient(terminal_id=term.id, key_prefix=key.prefix, key_hash=key.key_hash)
        session.add(api_client)
        session.commit()
        return {
            "key": key.plaintext,
            "login": login,
            "terminal_id": term.id,
            "broker_id": broker.id,
            "api_client_id": api_client.id,
        }


def _headers(terminal: dict) -> dict:
    return {"X-API-Key": terminal["key"]}
