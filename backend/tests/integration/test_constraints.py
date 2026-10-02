"""Idempotencia, unicidad e inmutabilidad garantizadas por la base de datos."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from supervisor.models import (
    BotVersion,
    DataSplit,
    Deployment,
    PriceBar,
    RawEvent,
    Trade,
    TradeEvent,
)
from supervisor.models.enums import Direction, TradeEventType, TradeSource
from tests.conftest import NOW


def _raw_event_stmt(terminal_id, key: str):
    return (
        insert(RawEvent)
        .values(
            terminal_id=terminal_id,
            idempotency_key=key,
            event_type="DEAL",
            schema_version=1,
            payload={"deal": 123},
            sent_at=NOW,
        )
        .on_conflict_do_nothing(index_elements=["terminal_id", "idempotency_key"])
        .returning(RawEvent.id)
    )


def _trade(world, position_id: int = 5001) -> Trade:
    return Trade(
        account_id=world["account"].id,
        bot_version_id=world["version"].id,
        position_id=position_id,
        magic_number=20260903,
        symbol="USTEC_x100",
        direction=Direction.BUY,
        order_type="MARKET",
        source=TradeSource.DEMO,
        entry_time=NOW,
        entry_price=Decimal("20000.5"),
        initial_volume=Decimal("0.10"),
        max_volume=Decimal("0.10"),
    )


# Idempotencia -----------------------------------------------------------------------------


def test_same_event_twice_is_stored_once(session: Session, world) -> None:
    tid = world["terminal"].id
    first = session.execute(_raw_event_stmt(tid, "deal:1:900")).scalar()
    second = session.execute(_raw_event_stmt(tid, "deal:1:900")).scalar()
    assert first is not None
    assert second is None  # duplicado: no se insertó nada
    count = session.scalar(
        select(func.count()).select_from(RawEvent).where(RawEvent.idempotency_key == "deal:1:900")
    )
    assert count == 1


def test_same_position_cannot_create_two_trades(session: Session, world) -> None:
    session.add(_trade(world))
    session.flush()
    session.add(_trade(world))
    with pytest.raises(IntegrityError, match="uq_trades_account_id_position_id"):
        session.flush()


def test_same_deal_cannot_appear_twice_in_a_trade(session: Session, world) -> None:
    trade = _trade(world)
    session.add(trade)
    session.flush()

    def event() -> TradeEvent:
        return TradeEvent(
            trade_id=trade.trade_id,
            event_type=TradeEventType.TRADE_OPENED,
            event_time_utc=NOW,
            deal_ticket=777,
        )

    session.add(event())
    session.flush()
    session.add(event())
    with pytest.raises(IntegrityError, match="uq_trade_events_trade_id_deal_ticket"):
        session.flush()


def test_events_without_deal_are_not_blocked(session: Session, world) -> None:
    trade = _trade(world)
    session.add(trade)
    session.flush()
    for minutes in (1, 2):
        session.add(
            TradeEvent(
                trade_id=trade.trade_id,
                event_type=TradeEventType.SL_MODIFIED,
                event_time_utc=NOW + timedelta(minutes=minutes),
                sl=Decimal("19950"),
            )
        )
    session.flush()


# Despliegues ------------------------------------------------------------------------------


def _deployment(world, ended=None) -> Deployment:
    return Deployment(
        bot_version_id=world["version"].id,
        account_id=world["account"].id,
        symbol="USTEC_x100",
        magic_number=20260903,
        started_at=NOW,
        ended_at=ended,
    )


def test_one_active_deployment_per_magic(session: Session, world) -> None:
    session.add(_deployment(world))
    session.flush()
    session.add(_deployment(world))
    with pytest.raises(IntegrityError, match="uq_deployments_active_magic"):
        session.flush()


def test_ended_deployment_allows_new_one(session: Session, world) -> None:
    session.add(_deployment(world, ended=NOW + timedelta(days=1)))
    session.add(_deployment(world))
    session.flush()


# Validaciones -----------------------------------------------------------------------------


def test_version_must_be_semver(session: Session, world) -> None:
    session.add(
        BotVersion(
            bot_id=world["bot"].bot_id,
            version="v1",
            main_timeframe="M5",
            params={},
            released_at=NOW,
        )
    )
    with pytest.raises(IntegrityError, match="ck_bot_versions_semver"):
        session.flush()


def test_closed_trade_needs_close_time(session: Session, world) -> None:
    trade = _trade(world)
    trade.status = "CLOSED"
    session.add(trade)
    with pytest.raises(IntegrityError, match="ck_trades_closed_has_close_time"):
        session.flush()


def test_invalid_enum_value_rejected(session: Session, world) -> None:
    session.add(_trade(world))
    session.flush()
    with pytest.raises(IntegrityError, match="ck_trades_direction"):
        session.execute(text("UPDATE trades SET direction = 'LONG'"))


def test_bar_high_below_low_rejected(session: Session, world) -> None:
    session.add(
        PriceBar(
            broker_id=world["broker"].id,
            symbol="USTEC_x100",
            timeframe="M1",
            bar_time=NOW,
            open=Decimal("10"),
            high=Decimal("9"),
            low=Decimal("11"),
            close=Decimal("10"),
            tick_volume=1,
        )
    )
    with pytest.raises(IntegrityError, match="ck_price_bars"):
        session.flush()


def test_data_split_must_be_ordered(session: Session) -> None:
    session.add(
        DataSplit(
            scope={},
            train_end=NOW,
            validation_end=NOW - timedelta(days=1),
            oos_end=NOW + timedelta(days=1),
        )
    )
    with pytest.raises(IntegrityError, match="ck_data_splits_ordered"):
        session.flush()


# Inmutabilidad ----------------------------------------------------------------------------


def test_bot_version_cannot_be_modified(session: Session, world) -> None:
    with pytest.raises(DBAPIError, match="solo inserción"):
        session.execute(
            text("UPDATE bot_versions SET changelog = 'x' WHERE id = :id"),
            {"id": world["version"].id},
        )


def test_bot_version_cannot_be_deleted(session: Session, world) -> None:
    with pytest.raises(DBAPIError, match="solo inserción"):
        session.execute(
            text("DELETE FROM bot_versions WHERE id = :id"), {"id": world["version"].id}
        )


def test_trade_cannot_be_deleted(session: Session, world) -> None:
    trade = _trade(world)
    session.add(trade)
    session.flush()
    with pytest.raises(DBAPIError, match="solo inserción"):
        session.execute(text("DELETE FROM trades WHERE trade_id = :id"), {"id": trade.trade_id})


def test_trade_can_be_updated_while_open(session: Session, world) -> None:
    trade = _trade(world)
    session.add(trade)
    session.flush()
    trade.status = "CLOSED"
    trade.close_time = NOW + timedelta(minutes=30)
    trade.net_profit = Decimal("42.10")
    session.flush()


def test_raw_event_payload_is_immutable(session: Session, world) -> None:
    raw_id = session.execute(_raw_event_stmt(world["terminal"].id, "deal:1:901")).scalar()
    with pytest.raises(DBAPIError, match="solo se pueden cambiar"):
        session.execute(
            text("UPDATE raw_events SET payload = '{}'::jsonb WHERE id = :id"), {"id": raw_id}
        )


def test_raw_event_status_can_change(session: Session, world) -> None:
    raw_id = session.execute(_raw_event_stmt(world["terminal"].id, "deal:1:902")).scalar()
    session.execute(
        text(
            "UPDATE raw_events SET status = 'PROCESSED', processed_at = now(), attempts = 1 "
            "WHERE id = :id"
        ),
        {"id": raw_id},
    )
    status = session.scalar(select(RawEvent.status).where(RawEvent.id == raw_id))
    assert status == "PROCESSED"


def test_trade_event_is_append_only(session: Session, world) -> None:
    trade = _trade(world)
    session.add(trade)
    session.flush()
    session.add(
        TradeEvent(
            trade_id=trade.trade_id,
            event_type=TradeEventType.TRADE_OPENED,
            event_time_utc=NOW,
            deal_ticket=1,
        )
    )
    session.flush()
    with pytest.raises(DBAPIError, match="solo inserción"):
        session.execute(text("UPDATE trade_events SET price = 1"))
