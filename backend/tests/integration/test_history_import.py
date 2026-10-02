"""Importación del historial de punta a punta: velas y deals con la forma exacta del script
SupervisorImportHistory por la API real, worker, despliegues con fechas pasadas, backfill
(CLI y endpoint) y búsqueda de patrones; reimportación idempotente y prioridad del EA en vivo
frente a una importación grande."""

import math
import random
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from supervisor import cli
from supervisor.analytics import pattern_search
from supervisor.config import Settings
from supervisor.models import RawEvent, Terminal, Trade, TradeAnalysis, TradeDna
from supervisor.models.enums import RawEventStatus
from supervisor.worker.processor import process_batch, process_pending
from tests.conftest import _headers
from tests.integration.trading_helpers import admin, deal, load_trade, post_events

NOW = datetime.now(UTC).replace(second=0, microsecond=0)
N_BARS = 50_000
BARS_END = NOW - timedelta(hours=1)
BARS_START = BARS_END - timedelta(minutes=N_BARS - 1)  # ~34.7 días de M1
N_TRADES = 150  # 300 deals
FIRST_ENTRY = BARS_START + timedelta(days=12)
LAST_ENTRY = BARS_END - timedelta(days=3)
SPEC = {"digits": 2, "point": 0.01, "tick_size": 0.01, "tick_value": 0.01, "contract_size": 1}


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _price(t: datetime) -> float:
    m = (t - BARS_START).total_seconds() / 60
    return round(20000 + 0.01 * m + 30 * math.sin(m / 600), 2)


def _bars() -> list[dict]:
    out = []
    for i in range(N_BARS):
        t = BARS_START + timedelta(minutes=i)
        close = _price(t)
        open_ = _price(t - timedelta(minutes=1))
        out.append(
            {
                "time_utc": _iso(t),
                "open": open_,
                "high": round(max(open_, close) + 2, 2),
                "low": round(min(open_, close) - 2, 2),
                "close": close,
                "tick_volume": 40,
                "spread": 120,
            }
        )
    return out


def _deals(login: int, symbol: str, magic: int) -> list[dict]:
    """150 operaciones (IN + OUT) con SL a 10 y TP a 15 puntos de precio (1 lote: 1 punto =
    1 USD); ganan 1.5 R o pierden 1 R de forma pseudoaleatoria. balance_after como lo
    reconstruye el script."""
    rng = random.Random(7)
    step = (LAST_ENTRY - FIRST_ENTRY) / (N_TRADES - 1)
    balance = 10_000.0
    out = []
    for i in range(N_TRADES):
        opened = (FIRST_ENTRY + step * i).replace(second=0, microsecond=0)
        closed = opened + timedelta(minutes=45)
        side = "BUY" if i % 2 == 0 else "SELL"
        exit_side = "SELL" if side == "BUY" else "BUY"
        sign = 1 if side == "BUY" else -1
        entry = _price(opened)
        win = rng.random() < 0.45
        move = 15 if win else -10
        exit_price = round(entry + sign * move, 2)
        common = {"symbol": symbol, "magic": magic, "spec": SPEC}
        out.append(
            deal(
                login,
                900_000 + 2 * i,
                800_000 + i,
                "IN",
                side,
                1.0,
                entry,
                opened,
                sl=entry - sign * 10,
                tp=entry + sign * 15,
                balance_after=balance,
                **common,
            )
        )
        balance += move
        out.append(
            deal(
                login,
                900_001 + 2 * i,
                800_000 + i,
                "OUT",
                exit_side,
                1.0,
                exit_price,
                closed,
                reason="TP" if win else "SL",
                profit=move,
                balance_after=balance,
                **common,
            )
        )
    return out


def _import(client: TestClient, terminal: dict, events: list[dict], size: int = 100) -> dict:
    totals = {"accepted": 0, "duplicate": 0, "rejected": 0}
    for start in range(0, len(events), size):
        body = {
            "schema_version": 1,
            "sent_at": _iso(datetime.now(UTC)),
            "ea_version": "import-1.0.0",
            "origin": "history_import",
            "events": events[start : start + size],
        }
        r = client.post("/v1/ingest/events", json=body, headers=_headers(terminal))
        assert r.status_code == 200, r.text
        for key in totals:
            totals[key] += r.json()[key]
    return totals


def _post_bars(client: TestClient, terminal: dict, symbol: str, bars: list[dict]) -> int:
    inserted = 0
    for start in range(0, len(bars), 5000):
        body = {"symbol": symbol, "timeframe": "M1", "bars": bars[start : start + 5000]}
        r = client.post("/v1/ingest/bars", json=body, headers=_headers(terminal))
        assert r.status_code == 200, r.text
        inserted += r.json()["inserted"]
    return inserted


def _trades(engine: Engine, terminal: dict, symbol: str) -> list[Trade]:
    with Session(engine) as session:
        account = session.get(Terminal, terminal["terminal_id"]).account_id
        return list(
            session.scalars(
                select(Trade)
                .where(Trade.account_id == account, Trade.symbol == symbol)
                .order_by(Trade.entry_time)
            )
        )


def _count(engine: Engine, model, trade_ids) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).where(model.trade_id.in_(trade_ids)))


@contextmanager
def _scope(engine: Engine):
    with Session(engine, expire_on_commit=False) as session:
        yield session
        session.commit()


def _deployment(client: TestClient, terminal: dict, version_id: str, symbol: str, **kw):
    body = {
        "bot_version_id": version_id,
        "account_login": terminal["login"],
        "account_server": "Exness-MT5Trial",
        "symbol": symbol,
        "magic_number": 4242,
        **{k: _iso(v) if isinstance(v, datetime) else v for k, v in kw.items()},
    }
    return client.post("/v1/deployments", json=body, headers=admin())


def test_history_import_end_to_end(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    symbol = f"IMP{uuid.uuid4().hex[:5].upper()}"
    bars = _bars()
    events = _deals(terminal["login"], symbol, 4242)

    # Pendientes que hayan dejado otras pruebas: fuera, para contar solo esta importación.
    process_pending(session_factory, worker_settings)

    # 1. Primero las velas (10 envíos de 5000), después los deals en lotes marcados.
    started = time.monotonic()
    assert _post_bars(client, terminal, symbol, bars) == N_BARS
    bars_seconds = time.monotonic() - started
    assert bars_seconds < 60, bars_seconds  # 50 000 velas por la API real
    assert _import(client, terminal, events) == {"accepted": 300, "duplicate": 0, "rejected": 0}

    # 2. El worker crea las operaciones con DNA (las velas ya están) y su análisis.
    stats = process_pending(session_factory, worker_settings)
    assert stats["PROCESSED"] == 300 and stats["importados"] == 300
    trades = _trades(engine, terminal, symbol)
    ids = [t.trade_id for t in trades]
    assert len(trades) == N_TRADES
    assert all(t.status.value == "CLOSED" for t in trades)
    assert all(t.data_quality["importado"] is True for t in trades)
    assert all(t.deployment_id is None for t in trades)  # aún sin despliegue histórico
    assert all(t.risk_amount == 10 and t.balance_at_entry is not None for t in trades)
    assert all(t.data_quality["excursion"]["completa"] for t in trades)
    assert _count(engine, TradeDna, ids) == N_TRADES
    assert _count(engine, TradeAnalysis, ids) == N_TRADES
    with Session(engine) as session:
        dna = session.scalar(select(TradeDna).where(TradeDna.trade_id == ids[-1]))
    # 12 días de historia antes de la primera entrada: tendencia H1 y EMAs con dato.
    assert dna.features["tendencia_h1"] is not None
    assert dna.features["ema200"] is not None and dna.features["tf_indicadores"] == "M15"

    # 3. Reimportar es idempotente: todo duplicado y nada nuevo que procesar.
    assert _post_bars(client, terminal, symbol, bars[:5000]) == 0
    assert _import(client, terminal, events, size=500) == {
        "accepted": 0,
        "duplicate": 300,
        "rejected": 0,
    }
    assert process_pending(session_factory, worker_settings)["selected"] == 0

    # 4. Despliegues históricos: v1.0 corrió con el magic 4242 hasta hace media hora (fechas
    #    pasadas) y la v1.1 desde entonces. Los solapes se rechazan.
    bot = client.post(
        "/v1/bots",
        json={"name": f"EA_Import_{uuid.uuid4().hex[:6]}", "strategy": "historial"},
        headers=admin(),
    ).json()
    versions = [
        client.post(
            f"/v1/bots/{bot['bot_id']}/versions",
            json={"version": v, "main_timeframe": "M5", "released_at": _iso(BARS_START)},
            headers=admin(),
        ).json()["id"]
        for v in ("1.0.0", "1.1.0")
    ]
    switch = NOW - timedelta(minutes=30)
    old = _deployment(client, terminal, versions[0], symbol, started_at=BARS_START, ended_at=switch)
    assert old.status_code == 201, old.text
    assert old.json()["ended_at"] is not None
    current = _deployment(client, terminal, versions[1], symbol, started_at=switch)
    assert current.status_code == 201, current.text
    for clash in (
        {"started_at": BARS_START + timedelta(days=1)},  # activo dentro del histórico
        {"started_at": BARS_START - timedelta(days=5), "ended_at": BARS_START + timedelta(1)},
        {"started_at": switch - timedelta(days=1), "ended_at": switch + timedelta(days=1)},
    ):
        r = _deployment(client, terminal, versions[0], symbol, **clash)
        assert r.status_code == 409, (clash, r.text)
        assert "se cruza" in r.json()["detail"]
    before = _deployment(
        client,
        terminal,
        versions[0],
        symbol,
        started_at=BARS_START - timedelta(days=5),
        ended_at=BARS_START,  # se toca con el siguiente, no se cruza
    )
    assert before.status_code == 201, before.text
    bad = _deployment(client, terminal, versions[0], symbol, started_at=switch, ended_at=switch)
    assert bad.status_code == 422

    # 5. Backfill por la CLI (páginas de 40): asigna la v1.0 y rehace DNA (timeframe principal
    #    M5 en vez del de reserva) y análisis (cambió la versión del bot).
    monkeypatch.setattr(cli, "session_scope", lambda: _scope(engine))
    monkeypatch.setattr(cli, "get_settings", lambda: worker_settings)
    window = ["--from", _iso(FIRST_ENTRY), "--to", _iso(LAST_ENTRY + timedelta(minutes=1))]
    cli.main(["backfill", "--page-size", "40", *window])
    out = capsys.readouterr().out
    assert "Página 4:" in out and "Página 5:" not in out
    assert (
        f"Backfill: {N_TRADES} operaciones revisadas, {N_TRADES} asignadas a un despliegue, "
        f"0 MFE/MAE, 0 riesgos, {N_TRADES} DNA y {N_TRADES} análisis nuevos, 0 errores." in out
    )
    trades = _trades(engine, terminal, symbol)
    assert {str(t.bot_version_id) for t in trades} == {versions[0]}
    assert _count(engine, TradeDna, ids) == 2 * N_TRADES
    with Session(engine) as session:
        latest = session.scalar(
            select(TradeDna)
            .where(TradeDna.trade_id == ids[-1])
            .order_by(TradeDna.dna_version.desc())
            .limit(1)
        )
    assert latest.dna_version == 2 and latest.features["tf_indicadores"] == "M5"

    # Repetirlo no crea nada (idempotente); reanudar desde un cursor también funciona.
    cli.main(["backfill", *window])
    assert "0 asignadas a un despliegue, 0 MFE/MAE, 0 riesgos, 0 DNA y 0 análisis" in (
        capsys.readouterr().out
    )
    cli.main(
        [
            "backfill",
            "--after-time",
            _iso(trades[99].entry_time),
            "--after-id",
            str(trades[99].trade_id),
            *window,
        ]
    )
    assert "Backfill: 50 operaciones revisadas" in capsys.readouterr().out

    # 6. Con 150 operaciones cerradas la búsqueda de trampas ya puede correr.
    with _scope(engine) as session:
        report = pattern_search.run_patterns(session, worker_settings, uuid.UUID(versions[0]))
    assert report.status == "COMPLETADA", report.message
    assert report.n_trades == N_TRADES


def test_backfill_endpoint_pages(
    client: TestClient,
    terminal: dict,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    """POST /v1/admin/backfill hace una página acotada y devuelve el cursor de la siguiente."""
    login = terminal["login"]
    when = NOW - timedelta(days=200)
    post_events(
        client,
        terminal,
        *[
            deal(login, 950_000 + i, 960_000 + i, "IN", "BUY", 0.1, 20000, when + timedelta(i))
            for i in range(3)
        ],
    )
    body = {"entry_from": _iso(when), "entry_to": _iso(when + timedelta(days=3)), "limit": 2}
    assert client.post("/v1/admin/backfill", json=body).status_code == 401
    empty = client.post("/v1/admin/backfill", json=body, headers=admin())
    assert empty.status_code == 200, empty.text
    # El worker aún no procesó los deals: no hay operaciones en el rango.
    assert empty.json() == {"stats": {}, "next": None}

    process_pending(session_factory, worker_settings)
    first = client.post("/v1/admin/backfill", json=body, headers=admin()).json()
    assert first["stats"]["revisadas"] == 2 and first["next"] is not None
    second = client.post(
        "/v1/admin/backfill", json={**body, **first["next"]}, headers=admin()
    ).json()
    assert second["stats"]["revisadas"] == 1 and second["next"] is None
    bad = client.post("/v1/admin/backfill", json={"after_time": _iso(when)}, headers=admin())
    assert bad.status_code == 422
    too_big = client.post("/v1/admin/backfill", json={"limit": 1000}, headers=admin())
    assert too_big.status_code == 422


def test_live_events_first_during_a_big_import(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    """Con miles de deals importados pendientes (más antiguos, así que irían primero por hora
    del hecho), un deal del EA en vivo se procesa en el siguiente lote y la importación avanza
    en trozos acotados."""
    login = terminal["login"]
    symbol = f"LIV{uuid.uuid4().hex[:5].upper()}"
    old = NOW - timedelta(days=90)
    backlog = [
        deal(
            login,
            970_000 + i,
            980_000 + i,
            "IN",
            "BUY",
            0.1,
            20000,
            old + timedelta(hours=i),
            symbol=symbol,
        )
        for i in range(120)
    ]
    assert _import(client, terminal, backlog)["accepted"] == 120
    live_when = NOW - timedelta(minutes=2)
    post_events(
        client,
        terminal,
        deal(login, 990_001, 990_000, "IN", "SELL", 0.2, 20100, live_when, symbol=symbol),
    )

    settings = worker_settings.model_copy(
        update={"worker_batch_size": 10, "worker_import_batch_size": 4}
    )
    stats = process_batch(session_factory, settings)
    assert stats["selected"] == 5 and stats["importados"] == 4 and stats["more"] == 1
    live = load_trade(engine, terminal, 990_000)
    assert live is not None and "importado" not in live.data_quality
    imported = load_trade(engine, terminal, 980_000)
    assert imported is not None and imported.data_quality["importado"] is True
    with Session(engine) as session:
        pending = session.scalar(
            select(func.count()).where(
                RawEvent.terminal_id == terminal["terminal_id"],
                RawEvent.status == RawEventStatus.PENDING,
            )
        )
    assert pending == 116

    # Nuevo deal en vivo con la importación a medias: también pasa delante.
    post_events(
        client,
        terminal,
        deal(
            login,
            990_002,
            990_000,
            "OUT",
            "BUY",
            0.2,
            20050,
            NOW - timedelta(minutes=1),
            symbol=symbol,
        ),
    )
    stats = process_batch(session_factory, settings)
    assert stats["selected"] == 5 and stats["importados"] == 4
    assert load_trade(engine, terminal, 990_000).status.value == "CLOSED"

    # Sin EA en vivo, la importación sigue sola hasta terminar, en lotes de 4.
    total = process_pending(session_factory, settings)
    assert total["importados"] == 112 and total["PROCESSED"] == 112
