"""Contrato EA → API: estos cuerpos reproducen byte a byte lo que construye
mt5/Experts/SupervisorMonitor.mq5 (mismos campos, nulls, milisegundos y formato numérico).
Si alguien cambia el EA o los esquemas, esta prueba avisa antes de llegar a producción."""

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from supervisor.config import Settings
from supervisor.models import RawEvent
from supervisor.models.enums import RawEventOrigin
from supervisor.schemas.ingest import DealEvent
from supervisor.worker.processor import process_pending
from tests.conftest import _headers
from tests.integration.trading_helpers import load_trade

MT5_DIR = Path(__file__).resolve().parents[3] / "mt5"
EA_SOURCE = MT5_DIR / "Experts" / "SupervisorMonitor.mq5"
COMMON_SOURCE = MT5_DIR / "Include" / "Supervisor" / "SupervisorComun.mqh"
IMPORT_SOURCE = MT5_DIR / "Scripts" / "SupervisorImportHistory.mq5"
CADDY_MAX_BODY = 2 * 1024 * 1024  # request_body max_size 2MB (deploy/Caddyfile)


def _ea_batch(login: int) -> str:
    deal_open = (
        '{"type":"DEAL","account_login":LOGIN,"symbol":"USTEC_x100","magic":20260903,'
        '"position_id":350001,"time_utc":"2026-10-02T11:59:58.250Z","time_server_msc":1790942398250,'
        '"deal_ticket":410001,"order_ticket":420001,"deal_type":"BUY","entry":"IN","reason":"EXPERT",'
        '"volume":0.1,"price":20000.5,"profit":0,"commission":-0.35,"swap":0,"fee":0,'
        '"comment":"FVG \\"retest\\" v1","sl":19950,"tp":null,'
        '"symbol_spec":{"digits":2,"point":0.01,"tick_size":0.01,"tick_value":0.01,"contract_size":1}}'
    ).replace("LOGIN", str(login))
    deal_unknown_reason = (
        '{"type":"DEAL","account_login":LOGIN,"symbol":"XAUUSDm","magic":0,'
        '"position_id":350002,"time_utc":"2026-10-02T11:59:59Z","time_server_msc":1790942399000,'
        '"deal_ticket":410002,"order_ticket":420002,"deal_type":"SELL","entry":"OUT","reason":"OTHER",'
        '"volume":0.01,"price":2650.12,"profit":-1.5,"commission":0,"swap":-0.2,"fee":0,'
        '"comment":"","sl":null,"tp":null,"symbol_spec":null}'
    ).replace("LOGIN", str(login))
    modify = (
        '{"type":"POSITION_MODIFY","account_login":LOGIN,"symbol":"USTEC_x100","magic":20260903,'
        '"position_id":350001,"time_utc":"2026-10-02T12:00:00Z","time_server_msc":1790942400123,'
        '"sl":19975.5,"tp":20100}'
    ).replace("LOGIN", str(login))
    snapshot = (
        '{"type":"POSITIONS_SNAPSHOT","account_login":LOGIN,"time_utc":"2026-10-02T12:00:00Z",'
        '"positions":[{"position_id":350001,"symbol":"USTEC_x100","magic":20260903,"direction":"BUY",'
        '"volume":0.1,"price_open":20000.5,"sl":19975.5,"tp":20100,'
        '"time_open_utc":"2026-10-02T11:59:58Z","profit":3.2}]}'
    ).replace("LOGIN", str(login))
    events = ",".join([deal_open, deal_unknown_reason, modify, snapshot])
    return (
        '{"schema_version":1,"sent_at":"2026-10-02T12:00:01Z","ea_version":"1.0.0",'
        f'"events":[{events}]}}'
    )


def test_ea_batch_is_accepted(client: TestClient, terminal: dict) -> None:
    body = _ea_batch(terminal["login"])
    json.loads(body)  # el EA debe producir JSON válido
    response = client.post(
        "/v1/ingest/events",
        content=body,
        headers={**_headers(terminal), "Content-Type": "application/json"},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["accepted"] == 4, result
    # El EA busca este patrón exacto para saber qué sacar de su cola.
    for i in range(4):
        assert f'"index":{i},"status":"accepted"' in response.text


def test_ea_resend_is_duplicate(client: TestClient, terminal: dict) -> None:
    body = _ea_batch(terminal["login"])
    headers = {**_headers(terminal), "Content-Type": "application/json"}
    client.post("/v1/ingest/events", content=body, headers=headers)
    again = client.post("/v1/ingest/events", content=body, headers=headers)
    assert again.json()["duplicate"] == 4


def test_ea_heartbeat_snapshot_and_bars(client: TestClient, terminal: dict) -> None:
    headers = {**_headers(terminal), "Content-Type": "application/json"}
    heartbeat = (
        '{"sent_at":"2026-10-02T12:00:00Z","ea_version":"1.0.0","terminal_connected":true,'
        '"trade_allowed":true,"info":{"build":4755,"tz_offset_seconds":0,"open_positions":1}}'
    )
    snapshot = (
        '{"account_login":LOGIN,"time_utc":"2026-10-02T12:00:00Z","balance":1000,'
        '"equity":1003.2,"margin":20.05,"free_margin":983.15,"open_positions":1}'
    ).replace("LOGIN", str(terminal["login"]))
    bars = (
        '{"symbol":"USTEC_x100","timeframe":"M1","bars":['
        '{"time_utc":"2026-10-02T11:58:00Z","open":20001,"high":20003.5,"low":19998.25,'
        '"close":20000.5,"tick_volume":87,"spread":120}]}'
    )
    assert (
        client.post("/v1/ingest/heartbeat", content=heartbeat, headers=headers).status_code == 200
    )
    assert (
        client.post("/v1/ingest/account-snapshot", content=snapshot, headers=headers).status_code
        == 200
    )
    assert client.post("/v1/ingest/bars", content=bars, headers=headers).status_code == 200


def _code(path: Path) -> str:
    return re.sub(r"//.*", "", path.read_text(encoding="utf-8"))  # sin comentarios


def test_ea_never_trades() -> None:
    """Ningún archivo MQL5 del proyecto (EA, script de importación, include) puede contener
    una función capaz de enviar órdenes."""
    sources = sorted(MT5_DIR.rglob("*.mq5")) + sorted(MT5_DIR.rglob("*.mqh"))
    assert {EA_SOURCE, COMMON_SOURCE, IMPORT_SOURCE} <= set(sources)
    for path in sources:
        code = _code(path)
        for forbidden in (
            "OrderSend",
            "OrderSendAsync",
            "CTrade",
            "PositionClose",
            "PositionModify",
            "OrderModify",
            "OrderDelete",
            "Trade.mqh",
        ):
            assert forbidden not in code, f"{path.name} no puede usar {forbidden}"
        includes = re.findall(r"#include\s*[<\"]([^>\"]+)[>\"]", code)
        assert all(inc == "Supervisor\\SupervisorComun.mqh" for inc in includes), includes


# Script de importación del historial (mt5/Scripts/SupervisorImportHistory.mq5) --------------
#
# Los cuerpos reproducen byte a byte lo que construyen el script y SupervisorComun.mqh: el
# mismo JSON de deal que el EA más balance_after (reconstruido desde el balance actual), y el
# lote marcado con "origin":"history_import".


def _import_deal(
    login: int,
    ticket: int,
    position: int,
    entry: str,
    side: str,
    time_utc: str,
    msc: int,
    price: str,
    profit: str,
    balance: str,
    *,
    sl: str = "19950",
    tp: str = "20100",
    reason: str = "EXPERT",
    comment: str = "",
) -> str:
    return (
        f'{{"type":"DEAL","account_login":{login},"symbol":"USTEC_x100","magic":20260903,'
        f'"position_id":{position},"time_utc":"{time_utc}","time_server_msc":{msc},'
        f'"deal_ticket":{ticket},"order_ticket":{ticket + 10000},"deal_type":"{side}",'
        f'"entry":"{entry}","reason":"{reason}","volume":0.1,"price":{price},'
        f'"profit":{profit},"commission":-0.35,"swap":0,"fee":0,"comment":"{comment}",'
        f'"sl":{sl},"tp":{tp},"balance_after":{balance},'
        '"symbol_spec":{"digits":2,"point":0.01,"tick_size":0.01,"tick_value":0.01,'
        '"contract_size":1}}'
    )


def _import_batch(*events: str) -> str:
    return (
        '{"schema_version":1,"sent_at":"2026-10-02T12:00:01Z","ea_version":"import-1.0.0",'
        f'"origin":"history_import","events":[{",".join(events)}]}}'
    )


def _post(client: TestClient, terminal: dict, path: str, body: str):
    headers = {**_headers(terminal), "Content-Type": "application/json"}
    return client.post(path, content=body, headers=headers)


def _define(source: str, name: str) -> int:
    match = re.search(rf"#define\s+{name}\s+(\d+)", source)
    assert match, name
    return int(match.group(1))


def test_import_script_batch_accepted_and_marked(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    login = terminal["login"]
    body = _import_batch(
        _import_deal(
            login,
            510001,
            360001,
            "IN",
            "BUY",
            "2026-03-02T14:30:00.125Z",
            1772461800125,
            "20000.5",
            "0",
            "1000.65",
            comment="FVG retest",
        ),
        _import_deal(
            login,
            510002,
            360001,
            "OUT",
            "SELL",
            "2026-03-02T15:10:00Z",
            1772464200000,
            "20100",
            "9.95",
            "1010.25",
            reason="TP",
        ),
    )
    json.loads(body)
    response = _post(client, terminal, "/v1/ingest/events", body)
    assert response.status_code == 200, response.text
    assert response.json()["accepted"] == 2
    for i in range(2):
        assert f'"index":{i},"status":"accepted"' in response.text

    with Session(engine) as session:
        origins = session.scalars(
            select(RawEvent.origin).where(RawEvent.terminal_id == terminal["terminal_id"])
        ).all()
    assert origins == [RawEventOrigin.HISTORY_IMPORT] * 2

    process_pending(session_factory, worker_settings)
    trade = load_trade(engine, terminal, 360001)
    assert trade.status.value == "CLOSED"
    assert trade.data_quality["importado"] is True
    # Sin fotos de cuenta: el balance sale de balance_after del deal de entrada.
    assert trade.balance_at_entry == Decimal("1000.65")
    # 50.5 de distancia / 0.01 x 0.01 USD x 0.1 lotes = 5.05 USD = 0.5047 % de 1000.65
    assert trade.risk_amount == Decimal("5.05")
    assert trade.risk_percent == Decimal("0.5047")
    assert "balance_after" in trade.data_quality["riesgo"]["balance"]


def test_import_resend_and_overlap_with_ea_are_duplicates(
    client: TestClient, terminal: dict, engine: Engine
) -> None:
    login = terminal["login"]
    # El EA ya envió el deal de apertura en vivo (sin balance_after ni origin).
    ea = _post(client, terminal, "/v1/ingest/events", _ea_batch(login))
    assert ea.json()["accepted"] == 4
    same_deal = _import_deal(
        login,
        410001,
        350001,
        "IN",
        "BUY",
        "2026-10-02T11:59:58.250Z",
        1790942398250,
        "20000.5",
        "0",
        "1000",
        comment='FVG \\"retest\\" v1',
        tp="null",
    )
    new_deal = _import_deal(
        login,
        410009,
        350009,
        "IN",
        "SELL",
        "2026-09-01T09:00:00Z",
        1788253200000,
        "19000",
        "0",
        "990",
    )
    first = _post(client, terminal, "/v1/ingest/events", _import_batch(same_deal, new_deal))
    assert (first.json()["accepted"], first.json()["duplicate"]) == (1, 1)
    again = _post(client, terminal, "/v1/ingest/events", _import_batch(same_deal, new_deal))
    assert again.json()["duplicate"] == 2
    with Session(engine) as session:
        rows = dict(
            session.execute(
                select(RawEvent.idempotency_key, RawEvent.origin).where(
                    RawEvent.terminal_id == terminal["terminal_id"],
                    RawEvent.event_type == "DEAL",
                )
            ).all()
        )
    # El duplicado conserva el origen del primero que llegó.
    assert rows[f"deal:{login}:410001"] == RawEventOrigin.EA
    assert rows[f"deal:{login}:410009"] == RawEventOrigin.HISTORY_IMPORT


def test_import_rejected_deal_carries_error_for_the_script(
    client: TestClient, terminal: dict
) -> None:
    login = terminal["login"]
    good = _import_deal(
        login,
        520001,
        370001,
        "IN",
        "BUY",
        "2026-03-02T14:30:00Z",
        1772461800000,
        "20000.5",
        "0",
        "1000",
    )
    bad = good.replace('"deal_ticket":520001', '"deal_ticket":520002').replace(
        '"volume":0.1', '"volume":0'
    )
    response = _post(client, terminal, "/v1/ingest/events", _import_batch(good, bad))
    assert response.status_code == 200
    assert response.json()["rejected"] == 1
    # El script busca este texto exacto (ErrorForIndex) para escribir el motivo en el diario.
    text = response.text
    start = text.index('"index":1,"status":"rejected"')
    error_at = text.index('"error":"', start) + len('"error":"')
    assert "volume" in text[error_at : text.index('"}', error_at)]


def test_import_origin_is_validated(client: TestClient, terminal: dict) -> None:
    body = _import_batch(
        _import_deal(
            terminal["login"],
            530001,
            380001,
            "IN",
            "BUY",
            "2026-03-02T14:30:00Z",
            1772461800000,
            "20000.5",
            "0",
            "1000",
        )
    ).replace("history_import", "otro")
    assert _post(client, terminal, "/v1/ingest/events", body).status_code == 422


def test_import_chunks_fit_server_and_caddy_limits(client: TestClient, terminal: dict) -> None:
    """Los tamaños del script caben en los límites del servidor (5000 velas y 500 eventos por
    envío) y en los 2 MB de Caddy, incluso con números y comentarios en el peor caso."""
    script = _code(IMPORT_SOURCE)
    settings = Settings(database_url="postgresql+psycopg://x@y/z", admin_token="t" * 48)
    assert _define(script, "SERVER_MAX_BARS") == settings.ingest_max_bars == 5000
    assert _define(script, "SERVER_MAX_DEALS") == settings.ingest_max_batch == 500
    assert _define(script, "MAX_BODY_CHARS") < CADDY_MAX_BODY
    assert _define(script, "BARS_WINDOW_DAYS") * 1440 <= settings.ingest_max_bars

    start = datetime(2026, 3, 2, tzinfo=UTC)
    items = []
    for i in range(5000):
        t = (start + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        items.append(
            f'{{"time_utc":"{t}","open":123456.78901,"high":123459.98765,'
            f'"low":123450.12345,"close":123457.55555,"tick_volume":9999999,"spread":99999}}'
        )
    bars = '{"symbol":"USTEC_x100","timeframe":"M1","bars":[' + ",".join(items) + "]}"
    assert len(bars.encode()) < CADDY_MAX_BODY, len(bars)
    response = _post(client, terminal, "/v1/ingest/bars", bars)
    assert response.status_code == 200, response.text[:300]
    assert response.json() == {"received": 5000, "inserted": 5000}
    again = _post(client, terminal, "/v1/ingest/bars", bars)
    assert again.json() == {"received": 5000, "inserted": 0}

    comment = '\\"' * 32  # 64 caracteres que se escapan: el comentario más largo posible
    deals = [
        _import_deal(
            terminal["login"],
            600000 + i,
            700000 + i,
            "IN",
            "BUY",
            "2026-03-02T14:30:00.999Z",
            1772461800999,
            "123456.78901",
            "-123456.7891",
            "-12345678.12",
            comment=comment,
        )
        for i in range(500)
    ]
    body = _import_batch(*deals)
    assert len(body.encode()) < CADDY_MAX_BODY, len(body)


def test_deal_json_is_shared_and_matches_the_schema() -> None:
    """El EA y el script construyen el deal con la misma función, y sus campos son los del
    esquema DealEvent (los obligatorios están todos y no hay ninguno desconocido)."""
    common = _code(COMMON_SOURCE)
    body = common[common.index("string SupervisorDealJson(") :]
    body = body[: body.index("\n  }\n")]
    keys = re.findall(r'\\"(\w+)\\":', body)
    assert keys == [
        "type",
        "account_login",
        "symbol",
        "magic",
        "position_id",
        "time_utc",
        "time_server_msc",
        "deal_ticket",
        "order_ticket",
        "deal_type",
        "entry",
        "reason",
        "volume",
        "price",
        "profit",
        "commission",
        "swap",
        "fee",
        "comment",
        "sl",
        "tp",
        "balance_after",
        "symbol_spec",
    ]
    fields = DealEvent.model_fields
    assert set(keys) <= set(fields)
    assert {name for name, f in fields.items() if f.is_required()} <= set(keys)
    assert "HistoryDealSelect(" not in common  # vaciaría la lista de HistorySelect

    ea = _code(EA_SOURCE)
    script = _code(IMPORT_SOURCE)
    for source in (ea, script):
        assert "SupervisorDealJson(" in source
        assert "HistoryDealSelect(" not in source
    assert 'SupervisorDealJson(ticket, g_login, g_tzOffset, "", symbol)' in ea
    assert '\\"origin\\":\\"history_import\\"' in script
    # Primero las velas y después los deals, para que el DNA encuentre el contexto.
    on_start = script[script.index("void OnStart()") :]
    assert on_start.index("SendAllBars(") < on_start.index("SendAllDeals(")
    assert "AccountSnapshot" not in script and "account-snapshot" not in script
