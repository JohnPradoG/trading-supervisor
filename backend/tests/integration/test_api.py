"""Pruebas de la API contra PostgreSQL real: autenticación, idempotencia y registro."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from supervisor.models import ApiClient, Heartbeat, PriceBar, RawEvent, Terminal

NOW = datetime.now(UTC).replace(microsecond=0)


from tests.conftest import ADMIN_TOKEN, _headers  # noqa: E402


def _admin() -> dict:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _deal(login: int, ticket: int, **overrides) -> dict:
    event = {
        "type": "DEAL",
        "account_login": login,
        "symbol": "USTEC_x100",
        "magic": 20260903,
        "position_id": 9000 + ticket,
        "time_utc": NOW.isoformat(),
        "time_server_msc": 1_790_000_000_000 + ticket,
        "deal_ticket": ticket,
        "order_ticket": ticket + 1,
        "deal_type": "BUY",
        "entry": "IN",
        "reason": "EXPERT",
        "volume": "0.10",
        "price": "20000.25",
        "sl": "19950",
        "tp": 0,
    }
    event.update(overrides)
    return event


def _batch(*events: dict) -> dict:
    return {
        "schema_version": 1,
        "sent_at": NOW.isoformat(),
        "ea_version": "1.0.0",
        "events": list(events),
    }


# Salud y autenticación ----------------------------------------------------------------------


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["database"] == "up"


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-API-Key": "basura"}, {"X-API-Key": "tsk_000000000000_secreto-falso"}],
)
def test_ingest_requires_valid_key(client: TestClient, headers: dict) -> None:
    response = client.post("/v1/ingest/events", json=_batch(_deal(1, 1)), headers=headers)
    assert response.status_code == 401


def test_wrong_secret_with_valid_prefix_rejected(client: TestClient, terminal: dict) -> None:
    forged = terminal["key"][:17] + "x" * 43
    response = client.post(
        "/v1/ingest/events", json=_batch(_deal(terminal["login"], 1)), headers={"X-API-Key": forged}
    )
    assert response.status_code == 401


def test_revoked_key_rejected(client: TestClient, terminal: dict, engine: Engine) -> None:
    with Session(engine) as session:
        session.get(ApiClient, terminal["api_client_id"]).revoked_at = NOW
        session.commit()
    response = client.post(
        "/v1/ingest/events", json=_batch(_deal(terminal["login"], 1)), headers=_headers(terminal)
    )
    assert response.status_code == 401


def test_admin_endpoints_require_token(client: TestClient, terminal: dict) -> None:
    assert client.get("/v1/bots").status_code == 401
    # Una API key de terminal no sirve como token de administración.
    assert (
        client.get("/v1/bots", headers={"Authorization": f"Bearer {terminal['key']}"}).status_code
        == 401
    )
    assert client.get("/v1/bots", headers=_admin()).status_code == 200


def test_no_endpoint_sends_orders(client: TestClient) -> None:
    paths = [route.path for route in client.app.routes]
    assert not any(word in p for p in paths for word in ("order", "trade/open", "close", "execute"))


# Ingesta e idempotencia ---------------------------------------------------------------------


def test_events_accepted_then_duplicate(client: TestClient, terminal: dict, engine: Engine) -> None:
    batch = _batch(_deal(terminal["login"], 501), _deal(terminal["login"], 502))
    first = client.post("/v1/ingest/events", json=batch, headers=_headers(terminal))
    assert first.status_code == 200
    assert first.json()["accepted"] == 2

    again = client.post("/v1/ingest/events", json=batch, headers=_headers(terminal))
    body = again.json()
    assert body["accepted"] == 0 and body["duplicate"] == 2
    assert body["results"][0]["idempotency_key"] == f"deal:{terminal['login']}:501"

    with Session(engine) as session:
        stored = session.scalar(
            select(func.count())
            .select_from(RawEvent)
            .where(RawEvent.terminal_id == terminal["terminal_id"])
        )
        payload = session.scalar(
            select(RawEvent.payload).where(RawEvent.terminal_id == terminal["terminal_id"]).limit(1)
        )
    assert stored == 2
    assert payload["tp"] is None  # TP = 0 en MT5 significa "sin TP"


def test_same_event_twice_in_one_batch(client: TestClient, terminal: dict) -> None:
    deal = _deal(terminal["login"], 601)
    body = client.post(
        "/v1/ingest/events", json=_batch(deal, deal), headers=_headers(terminal)
    ).json()
    assert [r["status"] for r in body["results"]] == ["accepted", "duplicate"]


def test_bad_event_does_not_reject_batch(client: TestClient, terminal: dict) -> None:
    good = _deal(terminal["login"], 701)
    bad_volume = _deal(terminal["login"], 702, volume="-1")
    other_account = _deal(terminal["login"] + 1, 703)
    future = _deal(terminal["login"], 704, time_utc=(NOW + timedelta(hours=2)).isoformat())
    unknown_type = {"type": "ORDER_SEND", "account_login": terminal["login"]}
    body = client.post(
        "/v1/ingest/events",
        json=_batch(good, bad_volume, other_account, future, unknown_type),
        headers=_headers(terminal),
    ).json()
    assert [r["status"] for r in body["results"]] == [
        "accepted",
        "rejected",
        "rejected",
        "rejected",
        "rejected",
    ]
    assert "no corresponde" in body["results"][2]["error"]
    assert "futuro" in body["results"][3]["error"]


def test_naive_datetime_rejected(client: TestClient, terminal: dict) -> None:
    naive = _deal(terminal["login"], 801, time_utc="2026-10-02T10:00:00")
    body = client.post("/v1/ingest/events", json=_batch(naive), headers=_headers(terminal)).json()
    assert body["results"][0]["status"] == "rejected"
    assert "zona horaria" in body["results"][0]["error"]


def test_position_modify_key_is_deterministic(client: TestClient, terminal: dict) -> None:
    modify = {
        "type": "POSITION_MODIFY",
        "account_login": terminal["login"],
        "symbol": "USTEC_x100",
        "magic": 20260903,
        "position_id": 42,
        "time_utc": NOW.isoformat(),
        "time_server_msc": 1_790_000_123_456,
        "sl": "19975.50",
        "tp": "20100",
    }
    body = client.post("/v1/ingest/events", json=_batch(modify), headers=_headers(terminal)).json()
    key = body["results"][0]["idempotency_key"]
    assert key == f"mod:{terminal['login']}:42:1790000123456:19975.5:20100"
    # Mismo cambio enviado con otro formato numérico: misma clave, duplicado.
    modify2 = dict(modify, sl="19975.5000", tp="20100.00")
    body2 = client.post(
        "/v1/ingest/events", json=_batch(modify2), headers=_headers(terminal)
    ).json()
    assert body2["results"][0]["status"] == "duplicate"


def test_batch_too_large(client: TestClient, terminal: dict) -> None:
    events = [_deal(terminal["login"], 10_000 + i) for i in range(501)]
    response = client.post("/v1/ingest/events", json=_batch(*events), headers=_headers(terminal))
    assert response.status_code == 400


def test_heartbeat_and_snapshot(client: TestClient, terminal: dict, engine: Engine) -> None:
    hb = {
        "sent_at": NOW.isoformat(),
        "ea_version": "1.0.0",
        "terminal_connected": True,
        "trade_allowed": True,
    }
    assert (
        client.post("/v1/ingest/heartbeat", json=hb, headers=_headers(terminal)).json()["inserted"]
        == 1
    )
    assert (
        client.post("/v1/ingest/heartbeat", json=hb, headers=_headers(terminal)).json()["inserted"]
        == 0
    )
    snap = {
        "account_login": terminal["login"],
        "time_utc": NOW.isoformat(),
        "balance": "1000.00",
        "equity": "1012.50",
        "open_positions": 1,
    }
    assert (
        client.post("/v1/ingest/account-snapshot", json=snap, headers=_headers(terminal)).json()[
            "inserted"
        ]
        == 1
    )
    wrong = dict(snap, account_login=terminal["login"] + 1)
    assert (
        client.post(
            "/v1/ingest/account-snapshot", json=wrong, headers=_headers(terminal)
        ).status_code
        == 400
    )
    with Session(engine) as session:
        assert session.get(Terminal, terminal["terminal_id"]).last_seen_at is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(Heartbeat)
                .where(Heartbeat.terminal_id == terminal["terminal_id"])
            )
            == 1
        )


def test_bars_idempotent_and_validated(client: TestClient, terminal: dict, engine: Engine) -> None:
    bars = [
        {
            "time_utc": (NOW - timedelta(minutes=10 - i)).isoformat(),
            "open": "20000",
            "high": "20010",
            "low": "19990",
            "close": "20005",
            "tick_volume": 120,
            "spread": 18,
        }
        for i in range(3)
    ]
    payload = {"symbol": "USTEC_x100", "timeframe": "M1", "bars": bars}
    first = client.post("/v1/ingest/bars", json=payload, headers=_headers(terminal)).json()
    second = client.post("/v1/ingest/bars", json=payload, headers=_headers(terminal)).json()
    assert (first["inserted"], second["inserted"]) == (3, 0)
    broken = dict(payload, bars=[dict(bars[0], high="19000")])
    assert (
        client.post("/v1/ingest/bars", json=broken, headers=_headers(terminal)).status_code == 422
    )
    with Session(engine) as session:
        count = session.scalar(
            select(func.count())
            .select_from(PriceBar)
            .where(PriceBar.broker_id == terminal["broker_id"])
        )
    assert count == 3


# Registro de bots ---------------------------------------------------------------------------


def test_bot_version_deployment_flow(client: TestClient, terminal: dict) -> None:
    name = f"EA_Nasdaq_FVG_Retest_{uuid.uuid4().hex[:6]}"
    bot = client.post(
        "/v1/bots", json={"name": name, "strategy": "Retesteo de FVG"}, headers=_admin()
    )
    assert bot.status_code == 201
    bot_id = bot.json()["bot_id"]
    assert (
        client.post("/v1/bots", json={"name": name, "strategy": "x"}, headers=_admin()).status_code
        == 409
    )

    version_body = {
        "version": "1.0.0",
        "main_timeframe": "M5",
        "params": {"risk": 1.0, "rr": 2},
        "released_at": "2026-09-03T00:00:00Z",
    }
    v1 = client.post(f"/v1/bots/{bot_id}/versions", json=version_body, headers=_admin())
    assert v1.status_code == 201
    same_params_reordered = client.post(
        f"/v1/bots/{bot_id}/versions",
        json=dict(version_body, version="1.0.1", params={"rr": 2, "risk": 1.0}),
        headers=_admin(),
    )
    assert same_params_reordered.json()["params_hash"] == v1.json()["params_hash"]
    # Una versión existente no se puede volver a registrar ni sobrescribir.
    assert (
        client.post(f"/v1/bots/{bot_id}/versions", json=version_body, headers=_admin()).status_code
        == 409
    )
    bad = dict(version_body, version="v2", main_timeframe="M7")
    assert client.post(f"/v1/bots/{bot_id}/versions", json=bad, headers=_admin()).status_code == 422

    deployment = {
        "bot_version_id": v1.json()["id"],
        "account_login": terminal["login"],
        "account_server": "Exness-MT5Trial",
        "symbol": "USTEC_x100",
        "magic_number": 20260903,
        "started_at": "2026-09-03T00:00:00Z",
    }
    dep = client.post("/v1/deployments", json=deployment, headers=_admin())
    assert dep.status_code == 201
    assert client.post("/v1/deployments", json=deployment, headers=_admin()).status_code == 409
    unknown = dict(deployment, account_login=1)
    assert client.post("/v1/deployments", json=unknown, headers=_admin()).status_code == 404

    end = client.post(
        f"/v1/deployments/{dep.json()['id']}/end",
        json={"ended_at": "2026-09-20T00:00:00Z"},
        headers=_admin(),
    )
    assert end.status_code == 200
    # Terminado el anterior, el mismo magic puede pasar a la versión nueva.
    deployment_v2 = dict(
        deployment,
        bot_version_id=same_params_reordered.json()["id"],
        started_at="2026-09-20T00:00:00Z",
    )
    assert client.post("/v1/deployments", json=deployment_v2, headers=_admin()).status_code == 201

    detail = client.get(f"/v1/bots/{bot_id}", headers=_admin()).json()
    assert [v["version"] for v in detail["versions"]] == ["1.0.0", "1.0.1"]
    assert len(detail["deployments"]) == 2

    retired = client.patch(f"/v1/bots/{bot_id}", json={"status": "RETIRED"}, headers=_admin())
    assert retired.json()["status"] == "RETIRED"


def test_raw_events_listing(client: TestClient, terminal: dict) -> None:
    client.post(
        "/v1/ingest/events", json=_batch(_deal(terminal["login"], 901)), headers=_headers(terminal)
    )
    rows = client.get("/v1/raw-events?status=PENDING&limit=5", headers=_admin()).json()
    assert rows and all(r["status"] == "PENDING" for r in rows)
