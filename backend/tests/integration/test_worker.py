"""Worker de operaciones contra PostgreSQL real. Los eventos entran por la API de ingesta con
la forma que envía el EA y después se ejecuta el procesamiento del worker de forma síncrona."""

import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text, update
from sqlalchemy.orm import Session, sessionmaker

from supervisor import cli
from supervisor.config import Settings
from supervisor.models import RawEvent, Terminal
from supervisor.models.enums import (
    Direction,
    ExcursionSource,
    ExitReason,
    RawEventStatus,
    TradeEventType,
    TradeSource,
    TradeStatus,
)
from supervisor.worker.maintenance import maintenance_pass
from supervisor.worker.processor import process_pending
from tests.conftest import BACKEND_DIR
from tests.integration.trading_helpers import (
    MAGIC,
    D,
    at,
    bar,
    deal,
    deploy,
    load_events,
    load_trade,
    modify,
    position,
    post_balance,
    post_bars,
    post_events,
    raw_events,
    snapshot,
)

E = TradeEventType


def _types(events) -> list[str]:
    return [e.event_type.value for e in events]


# Ciclo de vida completo ------------------------------------------------------------------------


def test_full_lifecycle_with_exact_numbers(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    login, pos = terminal["login"], 350001
    ids = deploy(client, terminal)
    post_balance(client, terminal, at(-1), 10000)

    # Apertura BUY 0.5 @ 20000 con SL 19950 y TP 20100; el EA mueve el SL a 19980.
    post_events(
        client,
        terminal,
        deal(
            login,
            410001,
            pos,
            "IN",
            "BUY",
            0.5,
            20000.0,
            at(0),
            sl=19950,
            tp=20100,
            commission=-1.75,
        ),
        modify(login, pos, at(5), 19980, 20100),
    )
    stats = process_pending(session_factory, worker_settings)
    assert stats["PROCESSED"] >= 2

    trade = load_trade(engine, terminal, pos)
    assert trade.status == TradeStatus.OPEN
    assert trade.direction == Direction.BUY
    assert trade.source == TradeSource.DEMO
    assert str(trade.deployment_id) == ids["deployment_id"]
    assert str(trade.bot_version_id) == ids["version_id"]
    assert trade.entry_price == D("20000") and trade.entry_time == at(0)
    assert (trade.initial_sl, trade.initial_tp) == (D("19950"), D("20100"))
    assert (trade.current_sl, trade.current_tp) == (D("19980"), D("20100"))
    assert trade.initial_volume == trade.max_volume == trade.current_volume == D("0.5")
    assert trade.symbol_point == D("0.01") and trade.tick_value == D("0.01")
    assert trade.comment == "FVG retest" and trade.magic_number == MAGIC
    assert trade.order_type == "UNKNOWN"
    # 50 puntos de precio / 0.01 * 0.01 * 0.5 lotes = 25 USD = 0.25 % de 10 000.
    assert trade.balance_at_entry == D("10000")
    assert trade.risk_amount == D("25.0000")
    assert trade.risk_percent == D("0.2500")
    assert "asignacion" not in trade.data_quality and "riesgo" not in trade.data_quality

    # Parcial de 0.2 a 20050 por el EA.
    post_events(
        client,
        terminal,
        deal(
            login,
            410002,
            pos,
            "OUT",
            "SELL",
            0.2,
            20050.0,
            at(10),
            sl=19980,
            tp=20100,
            profit=10.0,
            commission=-0.70,
        ),
    )
    process_pending(session_factory, worker_settings)
    trade = load_trade(engine, terminal, pos)
    assert trade.status == TradeStatus.OPEN
    assert trade.current_volume == D("0.3")
    assert trade.max_volume == D("0.5")

    # SL a break-even y TP más lejos; el resto cierra por SL. Llegan también las velas M1.
    bars = []
    for i in range(21):
        if i == 3:
            bars.append(bar(at(i), 20000, 20005, 19970, 20000))
        elif i == 8:
            bars.append(bar(at(i), 20050, 20080, 20040, 20060))
        else:
            bars.append(bar(at(i), 20020, 20030, 20010, 20020))
    post_bars(client, terminal, bars)
    post_events(
        client,
        terminal,
        modify(login, pos, at(12), 20000, 20150),
        deal(
            login,
            410003,
            pos,
            "OUT",
            "SELL",
            0.3,
            20000.0,
            at(20),
            reason="SL",
            sl=20000,
            tp=20150,
            profit=0,
            commission=-1.05,
            swap=-0.40,
        ),
    )
    process_pending(session_factory, worker_settings)

    trade = load_trade(engine, terminal, pos)
    assert trade.status == TradeStatus.CLOSED
    assert trade.close_time == at(20)
    # (0.2 * 20050 + 0.3 * 20000) / 0.5
    assert trade.close_price == D("20020")
    assert trade.gross_profit == D("10.0000")
    assert trade.commission == D("-3.5000")  # entrada incluida
    assert trade.swap == D("-0.4000")
    assert trade.net_profit == D("6.1000")
    assert trade.points == D("2000.0000")
    assert trade.duration_seconds == 1200
    assert trade.exit_reason == ExitReason.MIXED  # EXPERT + SL
    assert trade.current_volume == D("0")
    assert (trade.current_sl, trade.current_tp) == (D("20000"), D("20150"))
    # Excursiones: máximo 20080 (+80) y mínimo 19970 (-30) sobre 21 velas completas.
    assert trade.excursion_source == ExcursionSource.M1_BARS
    assert trade.mfe_points == D("8000.0000")
    assert trade.mae_points == D("-3000.0000")
    assert trade.max_floating_profit == D("40.0000")
    assert trade.max_floating_drawdown == D("-15.0000")
    excursion = trade.data_quality["excursion"]
    assert excursion["completa"] is True and excursion["velas_encontradas"] == 21

    events = load_events(engine, trade)
    assert _types(events) == [
        "TRADE_OPENED",
        "SL_MODIFIED",
        "PARTIAL_CLOSE",
        "SL_MODIFIED",
        "TP_MODIFIED",
        "TRADE_CLOSED",
    ]
    assert not any(e.is_inferred for e in events)
    assert [e.deal_ticket for e in events if e.deal_ticket] == [410001, 410002, 410003]
    sl_change = events[1]
    assert (sl_change.sl, sl_change.extra["sl_anterior"]) == (D("19980"), "19950.0000000000")
    assert events[0].event_time_server is not None
    assert all(r.status == RawEventStatus.PROCESSED for r in raw_events(engine, terminal))


def test_sell_closed_by_tp_points_and_reason(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    login = terminal["login"]
    xau = {"digits": 2, "point": 0.01, "tick_size": 0.01, "tick_value": 1.0, "contract_size": 100}
    post_events(
        client,
        terminal,
        deal(
            login,
            1,
            77,
            "IN",
            "SELL",
            1.0,
            2650.0,
            at(0),
            sl=2655,
            tp=2640,
            symbol="XAUUSDm",
            spec=xau,
            magic=5,
        ),
        deal(
            login,
            2,
            77,
            "OUT",
            "BUY",
            1.0,
            2640.0,
            at(30),
            reason="TP",
            profit=1000.0,
            symbol="XAUUSDm",
            spec=xau,
            magic=5,
        ),
    )
    process_pending(session_factory, worker_settings)
    trade = load_trade(engine, terminal, 77)
    assert trade.direction == Direction.SELL
    assert trade.exit_reason == ExitReason.TP
    assert trade.points == D("1000.0000")  # SELL: beneficio positivo
    assert trade.net_profit == D("1000.0000")
    # Sin foto de cuenta: riesgo en dinero sí (5 * 100 * 1 lote), porcentaje no.
    assert trade.risk_amount == D("500.0000") and trade.risk_percent is None
    assert "sin foto de cuenta" in trade.data_quality["riesgo"]["balance"]
    assert trade.data_quality["riesgo"]["estado"] == "sin_balance"
    # Sin velas: excursiones vacías y anotado.
    assert trade.mfe_points is None and trade.excursion_source is None
    assert trade.data_quality["excursion"]["velas_encontradas"] == 0


@pytest.mark.parametrize(
    ("reason", "expected"),
    [("SO", ExitReason.STOP_OUT), ("CLIENT", ExitReason.MANUAL), ("OTHER", ExitReason.UNKNOWN)],
)
def test_exit_reason_from_deal(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
    reason: str,
    expected: ExitReason,
) -> None:
    login = terminal["login"]
    post_events(
        client,
        terminal,
        deal(login, 11, 90, "IN", "BUY", 0.1, 20000.0, at(0)),
        deal(login, 12, 90, "OUT", "SELL", 0.1, 19990.0, at(1), reason=reason, profit=-1.0),
    )
    process_pending(session_factory, worker_settings)
    assert load_trade(engine, terminal, 90).exit_reason == expected


# Idempotencia y orden de llegada ---------------------------------------------------------------


def test_duplicates_and_reprocessing_are_idempotent(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    login = terminal["login"]
    events = [
        deal(login, 21, 100, "IN", "BUY", 0.2, 20000.0, at(0), sl=19900, commission=-0.5),
        modify(login, 100, at(1), 19950, None),
        deal(login, 22, 100, "OUT", "SELL", 0.2, 20010.0, at(2), profit=2.0, commission=-0.5),
    ]
    post_events(client, terminal, *events)
    process_pending(session_factory, worker_settings)
    before = load_trade(engine, terminal, 100)
    n_events = len(load_events(engine, before))

    # El EA reenvía: la API los marca duplicados y no crea raw_events nuevos.
    assert post_events(client, terminal, *events)["duplicate"] == 3
    # Forzar el reprocesado de todo (como haría un reprocess manual).
    with Session(engine) as session:
        session.execute(
            update(RawEvent)
            .where(RawEvent.terminal_id == terminal["terminal_id"])
            .values(status=RawEventStatus.PENDING)
        )
        session.commit()
    process_pending(session_factory, worker_settings)

    after = load_trade(engine, terminal, 100)
    assert len(load_events(engine, after)) == n_events == 3
    for field in ("net_profit", "commission", "close_price", "points", "current_sl", "status"):
        assert getattr(after, field) == getattr(before, field), field
    statuses = {r.idempotency_key.split(":")[0]: r for r in raw_events(engine, terminal)}
    assert statuses["deal"].status == RawEventStatus.PROCESSED
    assert statuses["deal"].error == "deal ya registrado"


def test_out_before_in_is_deferred_then_applied(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    login = terminal["login"]
    # Llega primero el cierre (p. ej. por backfill): no hay operación, se difiere.
    post_events(
        client, terminal, deal(login, 32, 200, "OUT", "SELL", 0.1, 20030.0, at(5), profit=3.0)
    )
    process_pending(session_factory, worker_settings)
    assert load_trade(engine, terminal, 200) is None
    (out,) = raw_events(engine, terminal)
    assert out.status == RawEventStatus.PENDING
    assert out.attempts == 1
    assert out.next_attempt_at is not None
    assert out.error.startswith("en espera: ")

    # Llega la apertura: se crea la operación y el cierre en espera se reactiva al momento.
    post_events(client, terminal, deal(login, 31, 200, "IN", "BUY", 0.1, 20000.0, at(0), sl=19950))
    process_pending(session_factory, worker_settings)
    trade = load_trade(engine, terminal, 200)
    assert trade.status == TradeStatus.CLOSED
    assert trade.close_price == D("20030") and trade.points == D("3000.0000")
    assert _types(load_events(engine, trade)) == ["TRADE_OPENED", "TRADE_CLOSED"]
    assert all(r.status == RawEventStatus.PROCESSED for r in raw_events(engine, terminal))


def test_orphan_out_fails_and_revives_when_in_arrives(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    login = terminal["login"]
    impatient = worker_settings.model_copy(update={"worker_defer_max_seconds": 0})
    post_events(client, terminal, deal(login, 42, 210, "OUT", "SELL", 0.1, 20010.0, at(5)))
    process_pending(session_factory, impatient, now=datetime.now(UTC) + timedelta(seconds=5))
    (out,) = raw_events(engine, terminal)
    assert out.status == RawEventStatus.FAILED
    assert out.error.startswith("huérfano: ")

    post_events(client, terminal, deal(login, 41, 210, "IN", "BUY", 0.1, 20000.0, at(0)))
    process_pending(session_factory, impatient)
    assert load_trade(engine, terminal, 210).status == TradeStatus.CLOSED
    assert all(r.status == RawEventStatus.PROCESSED for r in raw_events(engine, terminal))


def test_partial_arriving_after_final_close_is_recomposed(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    login = terminal["login"]
    post_events(
        client,
        terminal,
        deal(login, 171, 1500, "IN", "BUY", 0.2, 20000.0, at(0)),
        deal(login, 173, 1500, "OUT", "SELL", 0.1, 20020.0, at(10), reason="TP", profit=2.0),
    )
    process_pending(session_factory, worker_settings)
    trade = load_trade(engine, terminal, 1500)
    assert trade.status == TradeStatus.OPEN and trade.current_volume == D("0.1")

    # El parcial anterior (minuto 5) llega tarde: la operación queda cerrada a la hora del
    # último deal y, como ningún deal quedó como TRADE_CLOSED, se añade uno inferido.
    post_events(
        client,
        terminal,
        deal(login, 172, 1500, "OUT", "SELL", 0.1, 20010.0, at(5), reason="SL", profit=1.0),
    )
    process_pending(session_factory, worker_settings)
    trade = load_trade(engine, terminal, 1500)
    assert trade.status == TradeStatus.CLOSED
    assert trade.close_time == at(10) and trade.close_price == D("20015")
    assert trade.exit_reason == ExitReason.MIXED
    events = load_events(engine, trade)
    assert _types(events) == ["TRADE_OPENED", "PARTIAL_CLOSE", "PARTIAL_CLOSE", "TRADE_CLOSED"]
    assert events[-1].is_inferred and events[-1].deal_ticket is None
    assert any("después de deals posteriores" in a for a in trade.data_quality["avisos"])


def test_scale_in_keeps_first_entry_and_uses_average_for_points(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    login = terminal["login"]
    post_events(
        client,
        terminal,
        deal(login, 51, 300, "IN", "BUY", 0.1, 20000.0, at(0), sl=19900),
        deal(login, 52, 300, "IN", "BUY", 0.3, 20040.0, at(3), sl=19900),
        deal(login, 53, 300, "OUT", "SELL", 0.4, 20100.0, at(9), reason="TP", profit=26.0),
    )
    process_pending(session_factory, worker_settings)
    trade = load_trade(engine, terminal, 300)
    assert _types(load_events(engine, trade)) == [
        "TRADE_OPENED",
        "POSITION_INCREASED",
        "TRADE_CLOSED",
    ]
    assert trade.entry_price == D("20000") and trade.initial_volume == D("0.1")
    assert trade.max_volume == D("0.4")
    # Media de entradas: (0.1 * 20000 + 0.3 * 20040) / 0.4 = 20030 -> 70 de precio = 7000 pts.
    assert trade.data_quality["precio_medio_entrada"] == "20030.0000000000"
    assert trade.points == D("7000.0000")
    assert trade.risk_amount == D("10.0000")  # solo el volumen inicial: 100 de precio * 0.1


def test_inout_reversal_closes_trade_and_ignores_rest(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    login = terminal["login"]
    post_events(
        client,
        terminal,
        deal(login, 61, 400, "IN", "BUY", 0.1, 20000.0, at(0)),
        deal(login, 62, 400, "INOUT", "SELL", 0.3, 20020.0, at(4), profit=2.0),
        deal(login, 63, 400, "OUT", "BUY", 0.2, 20000.0, at(8), profit=4.0),
    )
    process_pending(session_factory, worker_settings)
    trade = load_trade(engine, terminal, 400)
    assert trade.status == TradeStatus.CLOSED
    assert trade.close_time == at(4) and trade.close_price == D("20020")
    assert trade.net_profit == D("2.0000")
    reversal = trade.data_quality["inversion_inout"]
    assert reversal["volumen_cerrado"] == "0.1000"
    assert reversal["volumen_reabierto"] == "0.2000"
    last = raw_events(engine, terminal)[-1]
    assert last.status == RawEventStatus.IGNORED and "INOUT" in last.error


# Asignación a bots -------------------------------------------------------------------------------


def test_unassigned_magic_is_flagged_then_assigned_by_maintenance(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    login = terminal["login"]
    post_events(
        client,
        terminal,
        deal(login, 71, 500, "IN", "BUY", 0.1, 20000.0, at(0), magic=777),
        deal(login, 72, 501, "IN", "BUY", 0.1, 20000.0, at(0), magic=0),
    )
    process_pending(session_factory, worker_settings)
    trade = load_trade(engine, terminal, 500)
    assert trade.deployment_id is None and trade.bot_version_id is None
    assert trade.data_quality["asignacion"]["estado"] == "sin_despliegue"
    assert "magic 777" in trade.data_quality["asignacion"]["detalle"]
    assert "manual" in load_trade(engine, terminal, 501).data_quality["asignacion"]["detalle"]

    # Se registra después el despliegue que cubría la entrada: el repaso lo asigna.
    ids = deploy(client, terminal, magic=777)
    stats = maintenance_pass(session_factory, worker_settings)
    assert stats["asignadas"] >= 1
    trade = load_trade(engine, terminal, 500)
    assert str(trade.bot_version_id) == ids["version_id"]
    assert "asignacion" not in trade.data_quality


def test_deployment_resolved_by_entry_time(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    login = terminal["login"]
    v1 = deploy(client, terminal, magic=888, started=at(-600))
    ended = client.post(
        f"/v1/deployments/{v1['deployment_id']}/end",
        json={"ended_at": at(10).isoformat()},
        headers={"Authorization": f"Bearer {worker_settings.admin_token.get_secret_value()}"},
    )
    assert ended.status_code == 200
    v2 = deploy(client, terminal, magic=888, started=at(10), bot_id=v1["bot_id"], version="1.1.0")
    post_events(
        client,
        terminal,
        deal(login, 81, 600, "IN", "BUY", 0.1, 20000.0, at(5), magic=888),
        deal(login, 82, 601, "IN", "BUY", 0.1, 20000.0, at(15), magic=888),
    )
    process_pending(session_factory, worker_settings)
    assert str(load_trade(engine, terminal, 600).bot_version_id) == v1["version_id"]
    assert str(load_trade(engine, terminal, 601).bot_version_id) == v2["version_id"]


def test_risk_without_sl_is_null_and_flagged(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    login = terminal["login"]
    post_events(
        client,
        terminal,
        deal(login, 91, 700, "IN", "BUY", 0.1, 20000.0, at(0)),
        deal(login, 92, 701, "IN", "BUY", 0.1, 20000.0, at(0), sl=19990, balance_after=5000.0),
    )
    process_pending(session_factory, worker_settings)
    no_sl = load_trade(engine, terminal, 700)
    assert no_sl.risk_amount is None and no_sl.risk_percent is None
    assert no_sl.data_quality["riesgo"]["estado"] == "sin_sl"
    # Sin foto de cuenta se usa balance_after del deal de entrada: 10 * 0.1 = 1 USD de 5000.
    fallback = load_trade(engine, terminal, 701)
    assert fallback.risk_amount == D("1.0000") and fallback.risk_percent == D("0.0200")
    assert "balance_after" in fallback.data_quality["riesgo"]["balance"]


# Excursiones ------------------------------------------------------------------------------------


def test_mfe_mae_with_gaps_recomputed_when_bars_arrive(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    login = terminal["login"]
    # SELL de 0.2 entre los minutos 0 y 10 (11 velas). Solo llegan las de los minutos 0-5.
    post_bars(client, terminal, [bar(at(i), 20000, 20010, 19980, 20000) for i in range(6)])
    post_events(
        client,
        terminal,
        deal(login, 101, 800, "IN", "SELL", 0.2, 20000.0, at(0), sl=20050),
        deal(login, 102, 800, "OUT", "BUY", 0.2, 19990.0, at(10), profit=2.0),
    )
    process_pending(session_factory, worker_settings)
    trade = load_trade(engine, terminal, 800)
    info = trade.data_quality["excursion"]
    assert (info["velas_esperadas"], info["velas_encontradas"]) == (11, 6)
    assert info["completa"] is False and info["cobertura"] == 0.5455
    assert info["huecos"] == [[at(6).isoformat(), at(10).isoformat()]]
    assert trade.mfe_points == D("2000.0000")  # SELL: 20000 - 19980
    assert trade.mae_points == D("-1000.0000")  # 20000 - 20010
    assert trade.max_floating_profit == D("4.0000")
    assert trade.max_floating_drawdown == D("-2.0000")

    # Llegan las velas que faltaban, con un extremo nuevo: el repaso recalcula.
    post_bars(
        client,
        terminal,
        [
            bar(at(i), 20000, 20030 if i == 7 else 20005, 19970 if i == 9 else 19995, 20000)
            for i in range(6, 11)
        ],
    )
    stats = maintenance_pass(session_factory, worker_settings)
    assert stats["excursiones"] >= 1
    trade = load_trade(engine, terminal, 800)
    assert trade.data_quality["excursion"]["completa"] is True
    assert trade.mfe_points == D("3000.0000")
    assert trade.mae_points == D("-3000.0000")


def test_excursion_ignores_other_symbols_and_brokers(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    login = terminal["login"]
    post_bars(client, terminal, [bar(at(i), 2650, 2700, 2600, 2650) for i in range(3)], "XAUUSDm")
    post_events(
        client,
        terminal,
        deal(login, 111, 900, "IN", "BUY", 0.1, 20000.0, at(0)),
        deal(login, 112, 900, "OUT", "SELL", 0.1, 20000.0, at(2)),
    )
    process_pending(session_factory, worker_settings)
    trade = load_trade(engine, terminal, 900)
    assert trade.mfe_points is None
    assert trade.data_quality["excursion"]["velas_encontradas"] == 0


# Reentradas ---------------------------------------------------------------------------------------


def test_reentry_after_sl_detected_within_window(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    login = terminal["login"]
    post_events(
        client,
        terminal,
        # A: cierra por SL en el minuto 10.
        deal(login, 121, 1000, "IN", "BUY", 0.1, 20000.0, at(0), sl=19950),
        deal(login, 122, 1000, "OUT", "SELL", 0.1, 19950.0, at(10), reason="SL", profit=-5.0),
        # B: abre 15 minutos después -> reentrada de A. Cierra por TP.
        deal(login, 123, 1001, "IN", "BUY", 0.1, 19960.0, at(25), sl=19910),
        deal(login, 124, 1001, "OUT", "SELL", 0.1, 20000.0, at(30), reason="TP", profit=4.0),
        # C: abre 5 min después del TP de B (y 25 después del SL de A) -> no es reentrada.
        deal(login, 125, 1002, "IN", "BUY", 0.1, 20000.0, at(35)),
        # D: otro magic en la ventana de A -> no es reentrada.
        deal(login, 126, 1003, "IN", "BUY", 0.1, 20000.0, at(12), magic=999),
    )
    process_pending(session_factory, worker_settings)
    a = load_trade(engine, terminal, 1000)
    b = load_trade(engine, terminal, 1001)
    assert b.reentry_of_trade_id == a.trade_id
    reentry = [e for e in load_events(engine, b) if e.event_type == E.REENTRY]
    assert len(reentry) == 1 and reentry[0].is_inferred
    assert reentry[0].extra["minutos_desde_cierre_por_sl"] == 15.0
    assert load_trade(engine, terminal, 1002).reentry_of_trade_id is None
    assert load_trade(engine, terminal, 1003).reentry_of_trade_id is None


def test_reentry_linked_when_sl_close_arrives_late(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    login = terminal["login"]
    post_events(
        client,
        terminal,
        deal(login, 131, 1100, "IN", "SELL", 0.1, 20000.0, at(0), sl=20050),
        deal(login, 133, 1101, "IN", "SELL", 0.1, 20040.0, at(20)),
    )
    process_pending(session_factory, worker_settings)
    assert load_trade(engine, terminal, 1101).reentry_of_trade_id is None
    # El cierre por SL de la primera (minuto 10) llega después de procesar la segunda.
    post_events(
        client,
        terminal,
        deal(login, 132, 1100, "OUT", "BUY", 0.1, 20050.0, at(10), reason="SL", profit=-5.0),
    )
    process_pending(session_factory, worker_settings)
    first = load_trade(engine, terminal, 1100)
    assert load_trade(engine, terminal, 1101).reentry_of_trade_id == first.trade_id


# Modificaciones y fotos de posiciones ------------------------------------------------------------


def test_snapshot_infers_missed_sl_change_and_flags_gaps(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    login = terminal["login"]
    post_events(
        client,
        terminal,
        deal(login, 141, 1200, "IN", "BUY", 0.1, 20000.0, at(0), sl=19950, tp=20100),
        deal(login, 142, 1201, "IN", "BUY", 0.1, 20000.0, at(0), sl=19950, tp=20100),
    )
    process_pending(session_factory, worker_settings)
    # El EA estuvo apagado mientras se movió el SL de 1200; la foto lo refleja. 4242 no
    # tiene operación y 1201 no aparece.
    post_events(
        client,
        terminal,
        snapshot(
            login,
            at(5),
            [
                position(1200, "BUY", 0.1, 20000.0, at(0), 19990, 20100),
                position(4242, "BUY", 0.1, 20000.0, at(-60), None, None),
            ],
        ),
    )
    process_pending(session_factory, worker_settings)
    trade = load_trade(engine, terminal, 1200)
    events = load_events(engine, trade)
    assert _types(events) == ["TRADE_OPENED", "SL_MODIFIED"]
    assert events[1].is_inferred and events[1].sl == D("19990")
    assert trade.current_sl == D("19990")
    snap_raw = raw_events(engine, terminal)[-1]
    assert snap_raw.status == RawEventStatus.PROCESSED
    assert snap_raw.error == "aviso: posiciones sin operación: 4242"
    assert load_trade(engine, terminal, 1201).data_quality["ausente_en_foto"] == at(5).isoformat()

    # Una foto posterior igual no genera nada nuevo.
    post_events(
        client,
        terminal,
        snapshot(login, at(10), [position(1200, "BUY", 0.1, 20000.0, at(0), 19990, 20100)]),
    )
    process_pending(session_factory, worker_settings)
    assert len(load_events(engine, trade)) == 2


def test_modify_unknown_closed_and_unchanged(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    login = terminal["login"]
    post_events(
        client,
        terminal,
        deal(login, 151, 1300, "IN", "BUY", 0.1, 20000.0, at(0), sl=19950),
        modify(login, 1300, at(2), 19950, None),  # sin cambios
        deal(login, 152, 1300, "OUT", "SELL", 0.1, 20010.0, at(5)),
        modify(login, 1300, at(6), 19990, None),  # después del cierre
        modify(login, 1399, at(1), 19990, None),  # posición desconocida y reciente
    )
    process_pending(session_factory, worker_settings)
    by_key = {r.idempotency_key: r for r in raw_events(engine, terminal)}
    status = {k.split(":")[2] + "@" + k.split(":")[3]: r for k, r in by_key.items() if "mod" in k}
    unchanged, after_close, unknown = (
        status[f"1300@{int(at(2).timestamp() * 1000) + 3 * 3600 * 1000}"],
        status[f"1300@{int(at(6).timestamp() * 1000) + 3 * 3600 * 1000}"],
        status[f"1399@{int(at(1).timestamp() * 1000) + 3 * 3600 * 1000}"],
    )
    assert (unchanged.status, unchanged.error) == (RawEventStatus.IGNORED, "SL/TP sin cambios")
    assert after_close.status == RawEventStatus.IGNORED
    assert unknown.status == RawEventStatus.PENDING and unknown.error.startswith("en espera")

    # Pasado el plazo de espera queda IGNORED como huérfano.
    impatient = worker_settings.model_copy(update={"worker_defer_max_seconds": 0})
    process_pending(session_factory, impatient, now=datetime.now(UTC) + timedelta(hours=1))
    unknown = next(r for r in raw_events(engine, terminal) if r.id == unknown.id)
    assert unknown.status == RawEventStatus.IGNORED
    assert unknown.error.startswith("huérfano: ")


# Errores, CLI y proceso --------------------------------------------------------------------------


@contextmanager
def _scope(engine: Engine):
    session = Session(engine)
    try:
        yield session
        session.commit()
    finally:
        session.close()


def test_failed_event_and_reprocess_cli(
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    # Un evento corrupto (no puede llegar por la API, se inserta a mano) falla de forma
    # determinista y no bloquea la cola.
    with Session(engine) as session:
        session.add(
            RawEvent(
                terminal_id=terminal["terminal_id"],
                idempotency_key="corrupto:1",
                event_type="DEAL",
                schema_version=1,
                payload={"type": "DEAL", "account_login": terminal["login"]},
                sent_at=at(0),
                event_time=at(0),
            )
        )
        name = session.get(Terminal, terminal["terminal_id"]).name
        session.commit()
    process_pending(session_factory, worker_settings)
    (raw,) = raw_events(engine, terminal)
    assert raw.status == RawEventStatus.FAILED
    assert raw.attempts == 1 and raw.error.startswith("ValidationError")
    assert raw.processed_at is not None

    monkeypatch.setattr(cli, "session_scope", lambda: _scope(engine))
    cli.main(["reprocess", "--terminal", name])
    assert "1 eventos FAILED" in capsys.readouterr().out
    (raw,) = raw_events(engine, terminal)
    assert raw.status == RawEventStatus.PENDING

    process_pending(session_factory, worker_settings)
    (raw,) = raw_events(engine, terminal)
    assert raw.status == RawEventStatus.FAILED and raw.attempts == 2

    cli.main(["trade-summary"])
    out = capsys.readouterr().out
    assert "Operaciones:" in out and "FAILED" in out and "Último evento procesado: #" in out

    with pytest.raises(SystemExit):
        cli.main(["reprocess", "--terminal", "no-existe"])


def test_event_time_is_protected(terminal: dict, engine: Engine) -> None:
    with Session(engine) as session:
        session.add(
            RawEvent(
                terminal_id=terminal["terminal_id"],
                idempotency_key="guard:1",
                event_type="DEAL",
                schema_version=1,
                payload={},
                sent_at=at(0),
                event_time=at(0),
            )
        )
        session.commit()
    with Session(engine) as session, pytest.raises(Exception, match="solo se pueden cambiar"):
        session.execute(
            text("UPDATE raw_events SET event_time = now() WHERE idempotency_key = 'guard:1'")
        )
    with Session(engine) as session:
        session.execute(
            text("UPDATE raw_events SET next_attempt_at = now() WHERE idempotency_key = 'guard:1'")
        )
        session.commit()


def test_ingest_stores_event_time(client: TestClient, terminal: dict, engine: Engine) -> None:
    post_events(client, terminal, deal(terminal["login"], 161, 1400, "IN", "BUY", 0.1, 1.5, at(7)))
    (raw,) = raw_events(engine, terminal)
    assert raw.event_time == at(7)


def test_worker_process_stops_cleanly_on_sigterm(database_url: str, tmp_path) -> None:
    env = dict(
        os.environ,
        SUPERVISOR_DATABASE_URL=database_url,
        SUPERVISOR_ADMIN_TOKEN="w" * 64,
        SUPERVISOR_WORKER_POLL_INTERVAL_SECONDS="0.2",
        SUPERVISOR_WORKER_HEALTH_FILE=str(tmp_path / "alive"),
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "supervisor.worker.main"],
        cwd=BACKEND_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not (tmp_path / "alive").exists():
            assert proc.poll() is None, proc.stdout.read()
            assert time.monotonic() < deadline, "el worker no completó ninguna vuelta"
            time.sleep(0.1)
        proc.send_signal(signal.SIGTERM)
        output, _ = proc.communicate(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, output
    lines = [line for line in output.splitlines() if line.startswith("{")]
    assert '"msg": "worker iniciado"' in lines[0]
    assert '"msg": "worker detenido"' in lines[-1]
