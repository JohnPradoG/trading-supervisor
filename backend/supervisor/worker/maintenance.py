"""Repaso periódico de operaciones recientes (últimos worker_recheck_days días):

- Excursiones incompletas: si han llegado más velas M1, se recalculan MFE/MAE.
- Operaciones sin despliegue: si después se registró el despliegue que cubría la entrada, se
  asignan su bot y versión.
- Riesgo sin balance: si llegó después la foto de cuenta, se completa risk_percent.
"""

import logging
from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from supervisor.config import Settings
from supervisor.models import Account, Trade, TradeEvent
from supervisor.models.enums import TradeEventType, TradeStatus
from supervisor.worker.excursions import bars_available, compute_excursion
from supervisor.worker.processor import TRANSIENT_ERRORS
from supervisor.worker.trades import compute_risk, resolve_deployment

log = logging.getLogger("supervisor.worker")


def _each(session: Session, trades: list[Trade], action, stats: Counter, key: str) -> None:
    for trade in trades:
        try:
            with session.begin_nested():
                if action(trade):
                    stats[key] += 1
        except TRANSIENT_ERRORS:
            raise
        except Exception:
            log.exception("repaso fallido", extra={"trade_id": str(trade.trade_id), "paso": key})


def run_maintenance(session: Session, settings: Settings, now: datetime | None = None) -> Counter:
    now = now or datetime.now(UTC)
    since = now - timedelta(days=settings.worker_recheck_days)
    stats: Counter = Counter()

    complete = Trade.data_quality["excursion"]["completa"].astext
    incomplete = session.execute(
        select(Trade, Account.broker_id)
        .join(Account, Account.id == Trade.account_id)
        .where(
            Trade.status == TradeStatus.CLOSED,
            Trade.close_time >= since,
            func.coalesce(complete, "false") != "true",
        )
        .with_for_update(of=Trade)
    ).all()
    brokers = {trade.trade_id: broker_id for trade, broker_id in incomplete}

    def excursion(trade: Trade) -> bool:
        before = (trade.data_quality or {}).get("excursion", {}).get("velas_encontradas")
        broker_id = brokers[trade.trade_id]
        if before is not None and bars_available(session, trade, broker_id) == before:
            return False  # no han llegado velas nuevas
        compute_excursion(session, trade, broker_id)
        return True

    _each(session, [t for t, _ in incomplete], excursion, stats, "excursiones")

    unassigned = session.scalars(
        select(Trade)
        .where(Trade.deployment_id.is_(None), Trade.entry_time >= since)
        .with_for_update()
    ).all()

    def assign(trade: Trade) -> bool:
        resolve_deployment(session, trade)
        return trade.deployment_id is not None

    _each(session, list(unassigned), assign, stats, "asignadas")

    no_balance = session.scalars(
        select(Trade)
        .where(
            or_(Trade.balance_at_entry.is_(None), Trade.risk_percent.is_(None)),
            Trade.initial_sl.is_not(None),
            Trade.entry_time >= since,
        )
        .with_for_update()
    ).all()

    def risk(trade: Trade) -> bool:
        first = session.scalar(
            select(TradeEvent).where(
                TradeEvent.trade_id == trade.trade_id,
                TradeEvent.event_type == TradeEventType.TRADE_OPENED,
            )
        )
        compute_risk(session, trade, first)
        return trade.risk_percent is not None

    _each(session, list(no_balance), risk, stats, "riesgos")
    session.flush()
    if any(stats.values()):
        log.info("repaso de operaciones", extra=dict(stats))
    return stats


def maintenance_pass(session_factory: sessionmaker[Session], settings: Settings) -> Counter:
    with session_factory() as session, session.begin():
        return run_maintenance(session, settings)
