"""Ayudas para las pruebas del worker: eventos con la forma exacta que envía el EA, envío por
la API real de ingesta y lectura de las operaciones resultantes."""

import math
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from supervisor.models import RawEvent, Terminal, Trade, TradeEvent
from tests.conftest import ADMIN_TOKEN, _headers

# Hora base en el pasado reciente (la API rechaza horas futuras), en minuto exacto.
BASE = (datetime.now(UTC) - timedelta(hours=3)).replace(second=0, microsecond=0)
SYMBOL = "USTEC_x100"
MAGIC = 20260903
# USTEC en Exness: 2 decimales, 1 tick = 0.01 vale 0.01 USD por lote.
SPEC = {"digits": 2, "point": 0.01, "tick_size": 0.01, "tick_value": 0.01, "contract_size": 1}


def admin() -> dict:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def at(minutes: float) -> datetime:
    return BASE + timedelta(minutes=minutes)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def deal(
    login: int,
    ticket: int,
    position: int,
    entry: str,
    side: str,
    volume: float,
    price: float,
    when: datetime,
    *,
    reason: str = "EXPERT",
    sl: float | None = None,
    tp: float | None = None,
    profit: float = 0,
    commission: float = 0,
    swap: float = 0,
    fee: float = 0,
    magic: int = MAGIC,
    symbol: str = SYMBOL,
    spec: dict | None = SPEC,
    balance_after: float | None = None,
) -> dict:
    return {
        "type": "DEAL",
        "account_login": login,
        "symbol": symbol,
        "magic": magic,
        "position_id": position,
        "time_utc": _iso(when),
        "time_server_msc": int(when.timestamp() * 1000) + 3 * 3600 * 1000,
        "deal_ticket": ticket,
        "order_ticket": ticket + 100_000,
        "deal_type": side,
        "entry": entry,
        "reason": reason,
        "volume": volume,
        "price": price,
        "profit": profit,
        "commission": commission,
        "swap": swap,
        "fee": fee,
        "comment": "FVG retest" if entry == "IN" else "",
        "sl": sl,
        "tp": tp,
        "balance_after": balance_after,
        "symbol_spec": spec,
    }


def modify(
    login: int,
    position: int,
    when: datetime,
    sl: float | None,
    tp: float | None,
    *,
    magic: int = MAGIC,
    symbol: str = SYMBOL,
) -> dict:
    return {
        "type": "POSITION_MODIFY",
        "account_login": login,
        "symbol": symbol,
        "magic": magic,
        "position_id": position,
        "time_utc": _iso(when),
        "time_server_msc": int(when.timestamp() * 1000) + 3 * 3600 * 1000,
        "sl": sl if sl is not None else 0,
        "tp": tp if tp is not None else 0,
    }


def snapshot(login: int, when: datetime, positions: list[dict]) -> dict:
    return {
        "type": "POSITIONS_SNAPSHOT",
        "account_login": login,
        "time_utc": _iso(when),
        "positions": positions,
    }


def position(
    position_id: int,
    side: str,
    volume: float,
    price_open: float,
    opened: datetime,
    sl: float | None,
    tp: float | None,
    *,
    magic: int = MAGIC,
    symbol: str = SYMBOL,
) -> dict:
    return {
        "position_id": position_id,
        "symbol": symbol,
        "magic": magic,
        "direction": side,
        "volume": volume,
        "price_open": price_open,
        "sl": sl if sl is not None else 0,
        "tp": tp if tp is not None else 0,
        "time_open_utc": _iso(opened),
        "profit": 0,
    }


def post_events(client: TestClient, terminal: dict, *events: dict) -> dict:
    body = {
        "schema_version": 1,
        "sent_at": _iso(datetime.now(UTC)),
        "ea_version": "1.0.0",
        "events": list(events),
    }
    response = client.post("/v1/ingest/events", json=body, headers=_headers(terminal))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["rejected"] == 0, result
    return result


def post_balance(client: TestClient, terminal: dict, when: datetime, balance: float) -> None:
    body = {
        "account_login": terminal["login"],
        "time_utc": _iso(when),
        "balance": balance,
        "equity": balance,
        "open_positions": 0,
    }
    response = client.post("/v1/ingest/account-snapshot", json=body, headers=_headers(terminal))
    assert response.status_code == 200, response.text


def bar(when: datetime, open_: float, high: float, low: float, close: float) -> dict:
    return {
        "time_utc": _iso(when),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "tick_volume": 50,
        "spread": 120,
    }


def post_bars(client: TestClient, terminal: dict, bars: list[dict], symbol: str = SYMBOL) -> None:
    body = {"symbol": symbol, "timeframe": "M1", "bars": bars}
    response = client.post("/v1/ingest/bars", json=body, headers=_headers(terminal))
    assert response.status_code == 200, response.text


def deploy(
    client: TestClient,
    terminal: dict,
    *,
    magic: int = MAGIC,
    symbol: str = SYMBOL,
    started: datetime | None = None,
    bot_id: str | None = None,
    version: str = "1.0.0",
) -> dict:
    """Registra bot (si no se da), versión y despliegue por la API de administración."""
    if bot_id is None:
        bot = client.post(
            "/v1/bots",
            json={"name": f"EA_Test_{uuid.uuid4().hex[:8]}", "strategy": "FVG retest"},
            headers=admin(),
        )
        assert bot.status_code == 201, bot.text
        bot_id = bot.json()["bot_id"]
    v = client.post(
        f"/v1/bots/{bot_id}/versions",
        json={"version": version, "main_timeframe": "M5", "released_at": _iso(at(-24 * 60))},
        headers=admin(),
    )
    assert v.status_code == 201, v.text
    dep = client.post(
        "/v1/deployments",
        json={
            "bot_version_id": v.json()["id"],
            "account_login": terminal["login"],
            "account_server": "Exness-MT5Trial",
            "symbol": symbol,
            "magic_number": magic,
            "started_at": _iso(started or at(-24 * 60)),
        },
        headers=admin(),
    )
    assert dep.status_code == 201, dep.text
    return {"bot_id": bot_id, "version_id": v.json()["id"], "deployment_id": dep.json()["id"]}


def account_id(engine: Engine, terminal: dict) -> uuid.UUID:
    with Session(engine) as session:
        return session.get(Terminal, terminal["terminal_id"]).account_id


def load_trade(engine: Engine, terminal: dict, position_id: int) -> Trade | None:
    with Session(engine, expire_on_commit=False) as session:
        acc = session.get(Terminal, terminal["terminal_id"]).account_id
        return session.scalar(
            select(Trade).where(Trade.account_id == acc, Trade.position_id == position_id)
        )


def load_events(engine: Engine, trade: Trade) -> list[TradeEvent]:
    with Session(engine) as session:
        return list(
            session.scalars(
                select(TradeEvent)
                .where(TradeEvent.trade_id == trade.trade_id)
                .order_by(TradeEvent.event_time_utc, TradeEvent.created_at, TradeEvent.event_type)
            )
        )


def raw_events(engine: Engine, terminal: dict) -> list[RawEvent]:
    with Session(engine) as session:
        return list(
            session.scalars(
                select(RawEvent)
                .where(RawEvent.terminal_id == terminal["terminal_id"])
                .order_by(RawEvent.id)
            )
        )


def D(value: str) -> Decimal:
    return Decimal(value)


# Historia de velas M1 para el análisis (EMAs de M15/H1 y ATR H1) -------------------------------

HISTORY_SLOPE = 0.01  # tendencia alcista: +0.01 por minuto (+0.6 por hora)


def history_price(t: datetime, start: datetime, base: float = 20000.0) -> float:
    """Precio de cierre de la vela M1 de `t` en la historia sintética: tendencia lineal más
    una onda de 5 puntos con periodo de unas 6 horas."""
    minutes = (t - start).total_seconds() / 60
    return base + HISTORY_SLOPE * minutes + 5 * math.sin(t.timestamp() / 3600.0)


def insert_m1_history(
    engine: Engine,
    broker_id: uuid.UUID,
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    base: float = 20000.0,
    spike_from: datetime | None = None,
) -> int:
    """Inserta en SQL (rápido) una vela M1 por minuto entre start y end, ambos incluidos.
    Rango normal ±3; desde `spike_from`, ±40 (volatilidad alta) sin cambiar los cierres."""
    sql = text(
        """
        INSERT INTO price_bars (broker_id, symbol, timeframe, bar_time, open, high, low, close,
                                tick_volume, spread)
        SELECT :broker_id, :symbol, 'M1', t, p, p + r, p - r, p, 10, 100
        FROM (
            SELECT t,
                   (CAST(:base AS float8)
                    + CAST(:slope AS float8) * extract(epoch FROM t - CAST(:start AS timestamptz))::float8 / 60
                    + 5 * sin(extract(epoch FROM t)::float8 / 3600.0))::numeric(24, 10) AS p,
                   CASE WHEN t >= CAST(:spike AS timestamptz) THEN 40 ELSE 3 END AS r
            FROM generate_series(CAST(:start AS timestamptz), CAST(:end AS timestamptz),
                                 interval '1 minute') AS t
        ) AS x
        """
    )
    with engine.begin() as conn:
        result = conn.execute(
            sql,
            {
                "broker_id": broker_id,
                "symbol": symbol,
                "start": start,
                "end": end,
                "base": base,
                "slope": HISTORY_SLOPE,
                "spike": spike_from,
            },
        )
    return result.rowcount
