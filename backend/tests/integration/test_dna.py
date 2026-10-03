"""Trading DNA de punta a punta: eventos por la API de ingesta, worker, velas M1 reales en
PostgreSQL, versiones de solo inserción, API, CLI, dashboard y group_by=dna."""

import uuid
from contextlib import contextmanager
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from supervisor import cli
from supervisor.analytics import dna as dna_module
from supervisor.analytics import dna_features as df
from supervisor.analytics.indicators import ema_series
from supervisor.analytics.market import floor_time
from supervisor.config import Settings
from supervisor.main import create_app
from supervisor.models import FeatureDefinition, TradeDna
from supervisor.worker.maintenance import maintenance_pass
from supervisor.worker.processor import process_pending
from tests.conftest import ADMIN_TOKEN
from tests.integration.test_dashboard import login
from tests.integration.trading_helpers import (
    BASE,
    admin,
    at,
    bar,
    deal,
    deploy,
    history_price,
    insert_m1_history,
    load_trade,
    post_bars,
    post_events,
)

HISTORY_DAYS = 21


def _history(engine: Engine, terminal: dict, end_minutes: int = -1) -> None:
    start = BASE - timedelta(days=HISTORY_DAYS)
    insert_m1_history(
        engine,
        terminal["broker_id"],
        "USTEC_x100",
        start,
        BASE + timedelta(minutes=end_minutes),
    )


def _versions(engine: Engine, trade_id) -> list[TradeDna]:
    with Session(engine) as session:
        return list(
            session.scalars(
                select(TradeDna).where(TradeDna.trade_id == trade_id).order_by(TradeDna.dna_version)
            )
        )


@contextmanager
def _scope(engine: Engine):
    with Session(engine, expire_on_commit=False) as session:
        yield session
        session.commit()


def _open_buy(client: TestClient, terminal: dict, pos: int, minute: float = 0) -> None:
    post_events(
        client,
        terminal,
        deal(terminal["login"], pos * 10 + 1, pos, "IN", "BUY", 1.0, 20300, at(minute), sl=20270),
    )


def _recompute(engine: Engine, trade_id, settings: Settings) -> str:
    with _scope(engine) as session:
        return dna_module.dna_by_id(session, trade_id, settings).status


def test_dna_computed_when_trade_is_created(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    deploy(client, terminal)
    _history(engine, terminal)
    _open_buy(client, terminal, 8001)
    process_pending(session_factory, worker_settings)
    trade = load_trade(engine, terminal, 8001)
    assert trade.status.value == "OPEN"  # la entrada basta: no espera al cierre

    (dna,) = _versions(engine, trade.trade_id)
    assert dna.dna_version == 1 and dna.feature_set_version == df.FEATURE_SET_VERSION
    assert dna.data_cutoff == trade.entry_time
    assert dna.last_bar_time == BASE - timedelta(minutes=1)
    f = dna.features
    assert set(f) == set(df.FEATURES_BY_NAME)
    # Historia alcista (+0.6 por hora): todas las tendencias alcistas y a favor de una compra.
    for tf in ("m5", "m15", "h1", "h4", "d1"):
        assert f[f"tendencia_{tf}"] == "ALCISTA", tf
        assert f[f"tendencia_{tf}_rel"] == "A_FAVOR", tf
    assert f["tf_indicadores"] == "M5"  # timeframe principal de la versión del bot

    # EMA50 M5 recalculada desde la fórmula de la historia: cierre de cada vela M5 = precio
    # de su último minuto; solo velas cerradas antes de la entrada.
    start = BASE - timedelta(days=HISTORY_DAYS)
    cutoff = floor_time(BASE, 5)
    span_start = max(
        cutoff - dna_module._span(df.needed_bars("M5", "M5", 2.0), 5),
        dna_module.history_start(BASE, worker_settings),
    )
    t, closes = floor_time(max(span_start, start), 5), []
    while t < cutoff:
        closes.append(history_price(t + timedelta(minutes=4), start))
        t += timedelta(minutes=5)
    assert f["ema50"] == pytest.approx(ema_series(closes, 50)[-1], abs=1e-4)
    assert f["ema200"] == pytest.approx(ema_series(closes, 200)[-1], abs=1e-4)
    assert f["spread_puntos"] == 100  # la vela M1 anterior a la entrada
    assert f["volatilidad_relativa"] is not None and f["adx14"] is not None
    assert f["sesion"] and f["dia_semana"]
    # Sin calendario de noticias: NULL con el motivo, nunca "no había noticia".
    assert f["noticia_cercana"] is None
    assert dna.null_reasons["noticia_cercana"] == df.NO_NEWS_SOURCE
    assert all(dna.null_reasons.get(n) for n, v in f.items() if v is None)
    cov = dna.data_quality["timeframes"]
    assert cov["M5"]["cobertura"] == 1.0 and cov["D1"]["velas"] >= 20

    # Hechos previos a la entrada, calculados al abrir (para TRAMPA_ACTIVA con trampas de EMA):
    # historia alcista y compra = a favor de las EMAs; sin lookahead (solo velas cerradas).
    previous = dna.inputs["hechos_previos"]
    assert previous["version"] == 1 and "PRECIO_VS_EMA200_H1" in previous["hechos"]
    assert previous["hechos"]["PRECIO_VS_EMA50_H1"]["a_favor"] is True
    assert previous["hechos"]["SPREAD_ENTRADA"]["spread_puntos"] == 100
    assert previous["hechos"]["VOLATILIDAD_ATR_H1"]["percentil"] is not None

    # El catálogo queda registrado con su tipo para la fase 9.
    with Session(engine) as session:
        rows = {r.name: r for r in session.scalars(select(FeatureDefinition))}
    assert set(df.FEATURES_BY_NAME) <= set(rows)
    assert rows["tendencia_h1_rel"].value_type == "categorical"
    assert rows["bos_reciente"].value_type == "boolean"
    assert rows["rsi14"].value_type == "numeric" and rows["rsi14"].searchable
    assert rows["ema200"].searchable is False
    assert rows["noticia_cercana"].available is False


def test_no_lookahead_bars_at_or_after_entry_change_nothing(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    deploy(client, terminal)
    _history(engine, terminal)
    _open_buy(client, terminal, 8101)
    process_pending(session_factory, worker_settings)
    trade = load_trade(engine, terminal, 8101)
    (v1,) = _versions(engine, trade.trade_id)

    # Velas desde el minuto de la entrada en adelante, con precios absurdos: no se usan.
    crash = [bar(at(m), 100, 30000, 50, 100) for m in range(0, 90)]
    post_bars(client, terminal, crash)
    assert _recompute(engine, trade.trade_id, worker_settings) == "sin_cambios"
    maintenance_pass(session_factory, worker_settings)
    (same,) = _versions(engine, trade.trade_id)
    assert same.features == v1.features and same.input_hash == v1.input_hash

    # La base de datos impide guardar un DNA cuya última vela termina después de entrar.
    with Session(engine) as session, pytest.raises(IntegrityError, match="no_lookahead"):
        session.add(
            TradeDna(
                trade_id=trade.trade_id,
                dna_version=99,
                feature_set_version=1,
                input_hash="x" * 64,
                source="SERVER",
                data_cutoff=trade.entry_time,
                last_bar_time=trade.entry_time,
            )
        )
        session.flush()


def test_dna_versions_backfill_and_immutability(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deploy(client, terminal)
    _open_buy(client, terminal, 8201)
    process_pending(session_factory, worker_settings)
    trade = load_trade(engine, terminal, 8201)

    # Sin velas: el DNA existe, todo lo de mercado es NULL con su motivo.
    (v1,) = _versions(engine, trade.trade_id)
    assert v1.features["tendencia_h1"] is None
    assert v1.null_reasons["tendencia_h1"].startswith("0 velas H1")
    assert v1.features["sesion"] is not None  # el tiempo no depende de velas
    assert v1.last_bar_time is None and v1.inputs["control"]["incompleto"] is True

    # Repasar sin velas nuevas no crea versión.
    maintenance_pass(session_factory, worker_settings)
    assert len(_versions(engine, trade.trade_id)) == 1

    # Llega la historia (backfill del EA): el repaso crea la versión 2; la 1 no cambia.
    _history(engine, terminal)
    maintenance_pass(session_factory, worker_settings)
    v1_again, v2 = _versions(engine, trade.trade_id)
    assert (v1_again.id, v1_again.input_hash) == (v1.id, v1.input_hash)
    assert v2.dna_version == 2 and v2.input_hash != v1.input_hash
    assert v2.features["tendencia_h1"] == "ALCISTA"

    # Idempotencia: repaso y CLI no crean más versiones si nada cambió.
    maintenance_pass(session_factory, worker_settings)
    assert _recompute(engine, trade.trade_id, worker_settings) == "sin_cambios"
    assert len(_versions(engine, trade.trade_id)) == 2

    # Otro conjunto de variables: versión nueva (aquí simulada subiendo la versión).
    monkeypatch.setattr(df, "FEATURE_SET_VERSION", 2)
    maintenance_pass(session_factory, worker_settings)
    v3 = _versions(engine, trade.trade_id)[-1]
    assert v3.dna_version == 3 and v3.feature_set_version == 2

    # Historia inmutable: ni UPDATE ni DELETE.
    with Session(engine) as session, pytest.raises(DBAPIError, match="solo inserción"):
        session.execute(text("UPDATE trade_dna SET features = '{}' WHERE id = :id"), {"id": v1.id})
    with Session(engine) as session, pytest.raises(DBAPIError, match="solo inserción"):
        session.execute(text("DELETE FROM trade_dna WHERE id = :id"), {"id": v1.id})
    with Session(engine) as session, pytest.raises(DBAPIError, match="solo inserción"):
        session.execute(text("UPDATE feature_definitions SET label = 'x'"))


def test_dna_api_and_cli(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    deploy(client, terminal)
    _history(engine, terminal)
    _open_buy(client, terminal, 8301)
    process_pending(session_factory, worker_settings)
    trade = load_trade(engine, terminal, 8301)

    assert client.get(f"/v1/trades/{trade.trade_id}/dna").status_code == 401
    r = client.get(f"/v1/trades/{trade.trade_id}/dna", headers=admin())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dna"]["dna_version"] == 1 and len(body["dna"]["input_hash"]) == 64
    assert [h["dna_version"] for h in body["history"]] == [1]
    sections = {s["group"]: s for s in body["dna"]["sections"]}
    assert list(sections) == [g for g, _ in df.GROUPS]
    news = {f["name"]: f for f in sections["noticias"]["features"]}
    assert news["noticia_cercana"]["value"] is None
    assert news["noticia_cercana"]["null_reason"] == df.NO_NEWS_SOURCE
    trend = {f["name"]: f for f in sections["tendencia"]["features"]}
    assert (
        trend["tendencia_h1"]["value"] == "ALCISTA" and trend["tendencia_h1"]["timeframe"] == "H1"
    )
    assert body["dna"]["features"]["tendencia_h1_rel"] == "A_FAVOR"
    assert (
        client.get(f"/v1/trades/{trade.trade_id}/dna", params={"version": 7}, headers=admin())
    ).status_code == 404
    assert client.get(f"/v1/trades/{uuid.uuid4()}/dna", headers=admin()).status_code == 404

    catalog = client.get("/v1/dna/features", headers=admin()).json()
    by_name = {c["name"]: c for c in catalog}
    assert by_name["rsi14"]["bins"] == [30, 50, 70]
    assert by_name["tendencia_h1_rel"]["categories"] == ["A_FAVOR", "EN_CONTRA", "LATERAL"]

    monkeypatch.setattr(cli, "session_scope", lambda: _scope(engine))
    monkeypatch.setattr(cli, "get_settings", lambda: worker_settings)
    cli.main(["dna", "--trade", str(trade.trade_id)])
    out = capsys.readouterr().out
    assert "Versión 1 del DNA sin cambios" in out
    assert "[Tendencia]" in out and "Tendencia H1: ALCISTA" in out
    assert f"sin dato ({df.NO_NEWS_SOURCE})" in out
    cli.main(["dna", "--all"])  # pone al día las operaciones de otras pruebas
    capsys.readouterr()
    cli.main(["dna", "--all"])
    out = capsys.readouterr().out
    assert "Versiones nuevas del DNA: 0" in out and "errores: 0" in out
    assert len(_versions(engine, trade.trade_id)) == 1
    with pytest.raises(SystemExit, match="no existe la operación"):
        cli.main(["dna", "--trade", str(uuid.uuid4())])


def test_dashboard_shows_dna(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    database_url: str,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    deploy(client, terminal)
    _open_buy(client, terminal, 8401)
    process_pending(session_factory, worker_settings)
    trade = load_trade(engine, terminal, 8401)
    app = create_app(
        Settings(
            database_url=database_url,
            admin_token=ADMIN_TOKEN,
            log_level="WARNING",
            dashboard_cookie_secure=False,
        )
    )
    try:
        dash = TestClient(app, follow_redirects=False)
        assert login(dash).status_code == 303
        page = dash.get(f"/dashboard/operaciones/{trade.trade_id}")
        assert page.status_code == 200
        html = page.text
        assert 'id="dna"' in html and "Trading DNA" in html
        assert "Tendencia H1" in html and "Order blocks" in html
        assert "sin dato" in html and df.NO_NEWS_SOURCE in html
        assert "0 velas H1" in html  # el motivo de cada NULL
        assert " style=" not in html
    finally:
        app.state.engine.dispose()


def test_stats_group_by_dna(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    """Dos compras a favor de la tendencia H1 alcista y una venta en contra, cerradas."""
    ids = deploy(client, terminal)
    _history(engine, terminal, end_minutes=200)
    login_ = terminal["login"]
    events = []
    for n, (side, minute, profit) in enumerate(
        [("BUY", 0, 20.0), ("BUY", 60, -10.0), ("SELL", 120, -10.0)]
    ):
        pos = 8500 + n
        out = "SELL" if side == "BUY" else "BUY"
        sl = 20290 if side == "BUY" else 20310
        events += [
            deal(login_, pos * 10, pos, "IN", side, 1.0, 20300, at(minute), sl=sl),
            deal(login_, pos * 10 + 1, pos, "OUT", out, 1.0, 20300, at(minute + 10), profit=profit),
        ]
    post_events(client, terminal, *events)
    process_pending(session_factory, worker_settings)

    def groups(group_by: str) -> dict:
        r = client.get(
            "/v1/stats",
            params={"version_id": ids["version_id"], "group_by": group_by},
            headers=admin(),
        )
        assert r.status_code == 200, r.text
        return {g["label"]: g for g in r.json()["groups"]}

    rel = groups("dna:tendencia_h1_rel")
    assert set(rel) == {"tendencia_h1_rel=A_FAVOR", "tendencia_h1_rel=EN_CONTRA"}
    assert rel["tendencia_h1_rel=A_FAVOR"]["metrics"]["n_trades"] == 2
    assert rel["tendencia_h1_rel=EN_CONTRA"]["key"] == {"dna:tendencia_h1_rel": "EN_CONTRA"}
    hour = groups("dna:hora_utc,direction")
    assert sum(g["metrics"]["n_trades"] for g in hour.values()) == 3
    rsi = groups("dna:rsi14")
    assert all(label.startswith("rsi14 ") for label in rsi)
    news = groups("dna:noticia_cercana")
    assert list(news) == ["noticia_cercana: sin dato"]
    for bad in ("dna", "dna:no_existe"):
        r = client.get("/v1/stats", params={"group_by": bad}, headers=admin())
        assert r.status_code == 400 and "dna" in r.json()["detail"]
