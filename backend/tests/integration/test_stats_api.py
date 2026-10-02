"""Estadísticas y comparación de versiones contra operaciones creadas por el pipeline real
(API de ingesta + worker), en un símbolo propio de cada prueba."""

import uuid
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from supervisor import cli
from supervisor.analytics.sessions import session_label
from supervisor.config import Settings
from supervisor.services import stats as stats_service
from supervisor.services.errors import ServiceError
from supervisor.services.stats import StatsFilters
from supervisor.worker.processor import process_pending
from tests.conftest import _headers
from tests.integration.trading_helpers import admin, at, deal, deploy, post_events


@pytest.fixture
def book(
    client: TestClient,
    terminal: dict,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> dict:
    """Bot con v1.0.0 (hasta el minuto 100) y v1.1.0 (desde el 100), 1 lote y SL a 10 de
    distancia = 10 USD de riesgo, más una operación de otro magic sin desplegar.

        T1 v1 BUY   +20  (2 R)     cierra en el minuto 10
        T2 v1 SELL  -10  (-1 R)    cierra en el 30
        T3 v1 BUY   +0.3 (0.03 R)  cierra en el 50  -> breakeven (|0.3| <= 0.5)
        T4 v2 BUY   +15  (1.5 R)   cierra en el 120
        T5 sin bot  -50  (sin SL)  cierra en el 140
        T6 v2 BUY   abierta: no cuenta
    """
    symbol = f"ST{uuid.uuid4().hex[:8]}"
    login = terminal["login"]
    v1 = deploy(client, terminal, symbol=symbol, started=at(-600))
    ended = client.post(
        f"/v1/deployments/{v1['deployment_id']}/end",
        json={"ended_at": at(100).isoformat()},
        headers=admin(),
    )
    assert ended.status_code == 200, ended.text
    v2 = deploy(
        client, terminal, symbol=symbol, started=at(100), bot_id=v1["bot_id"], version="1.1.0"
    )

    def trade(n: int, side: str, opened: float, closed: float, profit: float, **kw) -> list:
        sl = 19990 if side == "BUY" else 20010
        out = "SELL" if side == "BUY" else "BUY"
        return [
            deal(login, n * 10, n, "IN", side, 1.0, 20000, at(opened), sl=sl, symbol=symbol, **kw),
            deal(
                login,
                n * 10 + 1,
                n,
                "OUT",
                out,
                1.0,
                20000,
                at(closed),
                profit=profit,
                symbol=symbol,
                **kw,
            ),
        ]

    post_events(
        client,
        terminal,
        *trade(1, "BUY", 0, 10, 20.0),
        *trade(2, "SELL", 20, 30, -10.0),
        *trade(3, "BUY", 40, 50, 0.3),
        *trade(4, "BUY", 110, 120, 15.0),
        deal(login, 50, 5, "IN", "BUY", 1.0, 20000, at(130), symbol=symbol, magic=4343),
        deal(
            login,
            51,
            5,
            "OUT",
            "SELL",
            1.0,
            19950,
            at(140),
            profit=-50.0,
            symbol=symbol,
            magic=4343,
        ),
        deal(login, 60, 6, "IN", "BUY", 1.0, 20000, at(150), sl=19990, symbol=symbol),
    )
    process_pending(session_factory, worker_settings)
    return {
        "symbol": symbol,
        "bot_id": v1["bot_id"],
        "v1": v1["version_id"],
        "v2": v2["version_id"],
    }


def _stats(client: TestClient, **params) -> dict:
    response = client.get("/v1/stats", headers=admin(), params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_stats_requires_admin(client: TestClient, terminal: dict) -> None:
    assert client.get("/v1/stats").status_code == 401
    assert client.get("/v1/stats", headers=_headers(terminal)).status_code == 401
    url = f"/v1/bots/{uuid.uuid4()}/compare-versions"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=admin()).status_code == 404


def test_stats_totals_and_unassigned_apart(client: TestClient, book: dict) -> None:
    body = _stats(client, symbol=book["symbol"])
    total = body["total"]
    assert (total["n_trades"], total["wins"], total["losses"], total["breakevens"]) == (4, 2, 1, 1)
    assert total["win_rate"] == 0.5 and total["loss_rate"] == 0.25
    assert total["profit_factor"] == 3.5  # (20 + 15) / 10
    assert total["expectancy"] == 6.325  # (20 - 10 + 0.3 + 15) / 4
    assert total["expectancy_r"] == 0.6325  # (2 - 1 + 0.03 + 1.5) / 4
    assert total["cumulative_r"] == 2.53 and total["cumulative_return"] == 25.3
    assert total["max_drawdown"] == 10.0  # 20 -> 10
    assert total["max_drawdown_pct"] is None and "sin balance" in total["max_drawdown_note"]
    assert total["average_duration_seconds"] == 600.0
    assert (total["max_consecutive_wins"], total["max_consecutive_losses"]) == (1, 1)
    assert total["sharpe"] is None and "n=4 < 30" in total["sharpe_note"]
    assert "muestra pequeña" in total["sample_warning"]
    assert total["win_rate_ci95"]["low"] < 0.5 < total["win_rate_ci95"]["high"]
    assert sum(b["count"] for b in total["histogram_r"]) == 4
    # La operación sin despliegue va aparte, nunca dentro de un bot.
    assert body["unassigned"]["n_trades"] == 1 and body["unassigned"]["expectancy"] == -50.0
    assert body["unassigned"]["expectancy_r"] is None
    params = body["parameters"]
    assert params["min_sample"] == 30 and params["date_field"] == "close_time"
    assert params["win_rate_interval"] == "Wilson"

    # Filtrar por bot excluye por definición las no asignadas.
    by_bot = _stats(client, symbol=book["symbol"], bot_id=book["bot_id"])
    assert by_bot["total"]["n_trades"] == 4 and by_bot["unassigned"] is None


def test_stats_filters(client: TestClient, book: dict) -> None:
    sym = book["symbol"]
    assert _stats(client, symbol=sym, version_id=book["v2"])["total"]["n_trades"] == 1
    # from/to por hora de cierre: [minuto 100, ...) deja T4 (y T5 aparte).
    late = _stats(client, symbol=sym, **{"from": at(100).isoformat()})
    assert late["total"]["n_trades"] == 1 and late["unassigned"]["n_trades"] == 1
    early = _stats(client, symbol=sym, to=at(30).isoformat())
    assert early["total"]["n_trades"] == 1  # el cierre del minuto 30 queda fuera: [from, to)
    assert _stats(client, symbol=sym, source="LIVE")["total"]["n_trades"] == 0
    assert _stats(client, symbol=sym, source="DEMO")["total"]["n_trades"] == 4
    other = _stats(client, symbol=sym, account_id=str(uuid.uuid4()))
    assert other["total"]["n_trades"] == 0 and other["total"]["win_rate"] is None


def test_stats_group_by(client: TestClient, book: dict) -> None:
    sym = book["symbol"]
    body = _stats(client, symbol=sym, group_by="version")
    groups = {g["key"]["version"]: g for g in body["groups"]}
    assert groups[book["v1"]]["metrics"]["n_trades"] == 3
    assert groups[book["v2"]]["metrics"]["n_trades"] == 1
    assert groups[book["v1"]]["label"].endswith(" 1.0.0")

    body = _stats(client, symbol=sym, group_by="direction")
    assert {g["key"]["direction"]: g["metrics"]["n_trades"] for g in body["groups"]} == {
        "BUY": 3,
        "SELL": 1,
    }
    body = _stats(client, symbol=sym, group_by="session,timeframe")
    assert sum(g["metrics"]["n_trades"] for g in body["groups"]) == 4
    assert all(g["key"]["timeframe"] == "M5" for g in body["groups"])
    assert {g["key"]["session"] for g in body["groups"]} == {
        session_label(at(m)) for m in (0, 20, 40, 110)
    }
    body = _stats(client, symbol=sym, group_by="hour")
    hours = [g["key"]["hour"] for g in body["groups"]]
    assert hours == sorted(hours) and all(isinstance(h, int) for h in hours)
    body = _stats(client, symbol=sym, group_by="bot,weekday")
    assert {g["key"]["bot"] for g in body["groups"]} == {book["bot_id"]}

    for bad, message in (
        ("dna", "dna:<variable>"),
        ("dna:no_existe", "desconocida"),
        ("color", "no válido"),
        ("bot,symbol,hour", "como mucho 2"),
        ("hour,hour", "repite"),
    ):
        response = client.get("/v1/stats", headers=admin(), params={"group_by": bad})
        assert response.status_code == 400 and message in response.json()["detail"]


def test_compare_versions_side_by_side(client: TestClient, book: dict) -> None:
    response = client.get(
        f"/v1/bots/{book['bot_id']}/compare-versions",
        headers=admin(),
        params={"symbol": book["symbol"]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [v["version"] for v in body["versions"]] == ["1.0.0", "1.1.0"]
    v1, v2 = (v["metrics"] for v in body["versions"])
    assert (v1["n_trades"], v1["profit_factor"], v1["expectancy_r"]) == (3, 2.0, 0.3433)
    assert v2["n_trades"] == 1 and v2["profit_factor"] is None
    assert v2["profit_factor_note"] == "sin pérdidas: profit factor infinito"
    assert v1["sample_warning"] and v2["sample_warning"]
    assert "1.0.0, 1.1.0" in body["comparison_note"]
    assert "fuera de muestra" in body["comparison_note"]


def test_stats_row_limit_is_enforced(engine: Engine, book: dict) -> None:
    with Session(engine) as session:
        rows = stats_service.load_rows(session, StatsFilters(symbol=book["symbol"]), 5)
        assert len(rows) == 5  # 4 asignadas + 1 sin asignar
        with pytest.raises(ServiceError, match="más de 4 operaciones"):
            stats_service.load_rows(session, StatsFilters(symbol=book["symbol"]), 4)


@contextmanager
def _scope(engine: Engine):
    with Session(engine, expire_on_commit=False) as session:
        yield session
        session.commit()


def test_stats_cli_table(
    engine: Engine,
    book: dict,
    worker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setattr(cli, "session_scope", lambda: _scope(engine))
    monkeypatch.setattr(cli, "get_settings", lambda: worker_settings)
    cli.main(
        ["stats", "--bot", book["bot_id"], "--symbol", book["symbol"], "--group-by", "version"]
    )
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].split()[:3] == ["Grupo", "n", "Win%"]
    total = next(line for line in lines if line.startswith("TOTAL"))
    assert " 4 " in total and "50.0%" in total and "3.50" in total and "muestra pequeña" in total
    assert any(" 1.0.0 " in line and " 3 " in line for line in lines)
    assert "SIN ASIGNAR" not in out  # filtrado por bot
    cli.main(["stats", "--symbol", book["symbol"]])
    assert "SIN ASIGNAR (aparte)" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="dna:<variable>"):
        cli.main(["stats", "--group-by", "dna"])
    cli.main(["stats", "--symbol", book["symbol"], "--group-by", "dna:sesion"])
    assert "sesion=" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="No existe el bot"):
        cli.main(["stats", "--bot", "no-existe"])
