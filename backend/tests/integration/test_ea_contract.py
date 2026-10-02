"""Contrato EA → API: estos cuerpos reproducen byte a byte lo que construye
mt5/Experts/SupervisorMonitor.mq5 (mismos campos, nulls, milisegundos y formato numérico).
Si alguien cambia el EA o los esquemas, esta prueba avisa antes de llegar a producción."""

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import _headers

EA_SOURCE = Path(__file__).resolve().parents[3] / "mt5" / "Experts" / "SupervisorMonitor.mq5"


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


def test_ea_never_trades() -> None:
    """El EA no debe contener ninguna función capaz de enviar órdenes."""
    source = EA_SOURCE.read_text(encoding="utf-8")
    code = re.sub(r"//.*", "", source)  # ignorar comentarios
    for forbidden in ("OrderSend", "OrderSendAsync", "CTrade", "PositionClose", "Trade.mqh"):
        assert forbidden not in code, f"El EA Monitor no puede usar {forbidden}"
