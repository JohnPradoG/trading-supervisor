"""API de consulta de operaciones: autenticación, filtros y detalle con su historia."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from supervisor.config import Settings
from supervisor.worker.processor import process_pending
from tests.conftest import _headers
from tests.integration.trading_helpers import admin, at, deal, deploy, load_trade, post_events


@pytest.fixture
def trades(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> dict:
    """Tres operaciones en un símbolo propio de la prueba: dos del bot (una cerrada) y una sin
    asignar."""
    symbol = f"TEST{uuid.uuid4().hex[:8]}"
    ids = deploy(client, terminal, symbol=symbol)
    login = terminal["login"]
    post_events(
        client,
        terminal,
        deal(login, 1, 10, "IN", "BUY", 0.1, 100.0, at(0), symbol=symbol),
        deal(login, 2, 10, "OUT", "SELL", 0.1, 101.0, at(5), symbol=symbol, profit=1.0),
        deal(login, 3, 11, "IN", "SELL", 0.1, 100.0, at(30), symbol=symbol),
        deal(login, 4, 12, "IN", "BUY", 0.1, 100.0, at(60), symbol=symbol, magic=4242),
    )
    process_pending(session_factory, worker_settings)
    return {
        **ids,
        "symbol": symbol,
        "closed": str(load_trade(engine, terminal, 10).trade_id),
        "open": str(load_trade(engine, terminal, 11).trade_id),
        "unassigned": str(load_trade(engine, terminal, 12).trade_id),
    }


def _ids(response) -> list[str]:
    assert response.status_code == 200, response.text
    return [t["trade_id"] for t in response.json()]


def test_trades_require_admin_token(client: TestClient, terminal: dict) -> None:
    assert client.get("/v1/trades").status_code == 401
    bad = {"Authorization": f"Bearer {terminal['key']}"}
    assert client.get("/v1/trades", headers=bad).status_code == 401
    assert client.get("/v1/trades", headers=_headers(terminal)).status_code == 401
    assert client.get(f"/v1/trades/{uuid.uuid4()}").status_code == 401


def test_trades_filters(client: TestClient, trades: dict) -> None:
    base = f"/v1/trades?symbol={trades['symbol']}"
    # Más recientes primero.
    assert _ids(client.get(base, headers=admin())) == [
        trades["unassigned"],
        trades["open"],
        trades["closed"],
    ]
    assert _ids(client.get(f"{base}&status=CLOSED", headers=admin())) == [trades["closed"]]
    assert _ids(client.get(f"{base}&status=OPEN", headers=admin())) == [
        trades["unassigned"],
        trades["open"],
    ]
    by_bot = client.get(f"/v1/trades?bot_id={trades['bot_id']}", headers=admin())
    assert _ids(by_bot) == [trades["open"], trades["closed"]]
    by_version = client.get(f"/v1/trades?version_id={trades['version_id']}", headers=admin())
    assert _ids(by_version) == [trades["open"], trades["closed"]]
    # Con `params`, httpx sustituye la query de la URL: el símbolo va también en params.
    window = client.get(
        "/v1/trades",
        params={"symbol": trades["symbol"], "from": at(10).isoformat(), "to": at(60).isoformat()},
        headers=admin(),
    )
    assert _ids(window) == [trades["open"]]
    naive = client.get(
        "/v1/trades",
        params={"symbol": trades["symbol"], "from": at(45).strftime("%Y-%m-%dT%H:%M:%S")},
        headers=admin(),
    )
    assert _ids(naive) == [trades["unassigned"]]
    page = client.get(f"{base}&limit=1&offset=1", headers=admin())
    assert _ids(page) == [trades["open"]]
    assert client.get(f"{base}&status=PAUSED", headers=admin()).status_code == 422
    assert client.get(f"{base}&limit=0", headers=admin()).status_code == 422


def test_trade_detail_with_events(client: TestClient, trades: dict) -> None:
    response = client.get(f"/v1/trades/{trades['closed']}", headers=admin())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "CLOSED" and body["exit_reason"] == "EXPERT"
    assert body["bot_version_id"] == trades["version_id"]
    assert body["net_profit"] == "1.0000"
    assert [e["event_type"] for e in body["events"]] == ["TRADE_OPENED", "TRADE_CLOSED"]
    assert [e["deal_ticket"] for e in body["events"]] == [1, 2]
    unassigned = client.get(f"/v1/trades/{trades['unassigned']}", headers=admin()).json()
    assert unassigned["data_quality"]["asignacion"]["estado"] == "sin_despliegue"
    assert client.get(f"/v1/trades/{uuid.uuid4()}", headers=admin()).status_code == 404
