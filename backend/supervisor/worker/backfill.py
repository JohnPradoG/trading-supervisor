"""Backfill: completa operaciones antiguas que el repaso periódico no alcanza.

El repaso del worker (supervisor.worker.maintenance) solo mira los últimos
worker_recheck_days días. Tras importar el historial de MT5 (script
SupervisorImportHistory) o registrar despliegues con fechas pasadas, las operaciones
antiguas necesitan lo mismo y aquí se hace, en páginas acotadas:

1. **Despliegue:** si no tiene, se busca el despliegue que cubría la entrada (los
   despliegues históricos se registran después de importar).
2. **Excursiones (MFE/MAE):** si estaban incompletas y han llegado más velas M1.
3. **Riesgo:** si faltaba el balance o el % (fotos de cuenta o balance_after posteriores).
4. **Trading DNA:** si falta, si cambió la operación (p. ej. ahora tiene bot con otro
   timeframe principal), el conjunto de variables o los parámetros, o si le faltaban velas
   y han llegado (supervisor.analytics.dna.needs_dna).
5. **Análisis:** lo mismo para las cerradas (supervisor.analytics.analyzer.needs_analysis).

Idempotente: las comprobaciones son consultas baratas y el DNA y el análisis solo crean
versión nueva si cambia su input_hash, así que repetirlo no duplica nada. Reanudable: recorre
las operaciones por (entry_time, trade_id) y cada página devuelve el cursor de la siguiente;
si se corta, se puede seguir desde el último cursor impreso o simplemente repetir (lo ya hecho
se salta). Cada operación va en su propio savepoint: una con datos raros no para el resto.
"""

import logging
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from supervisor.analytics.analyzer import analyze_trade, latest_analysis, needs_analysis
from supervisor.analytics.dna import compute_dna, latest_dna, needs_dna
from supervisor.config import Settings
from supervisor.models import Account, Trade, TradeEvent
from supervisor.models.enums import TradeEventType, TradeStatus
from supervisor.worker.excursions import bars_available, compute_excursion
from supervisor.worker.processor import TRANSIENT_ERRORS
from supervisor.worker.trades import compute_risk, resolve_deployment

log = logging.getLogger("supervisor.backfill")

Cursor = tuple[datetime, uuid.UUID]
MAX_PAGE = 500


@dataclass
class BackfillPage:
    stats: Counter
    # Cursor para pedir la página siguiente; None si esta fue la última.
    next_cursor: Cursor | None


def _excursion(session: Session, trade: Trade, broker_id: uuid.UUID) -> bool:
    info = (trade.data_quality or {}).get("excursion") or {}
    if info.get("completa"):
        return False
    before = info.get("velas_encontradas")
    if before is not None and bars_available(session, trade, broker_id) == before:
        return False
    compute_excursion(session, trade, broker_id)
    return True


def _risk(session: Session, trade: Trade) -> bool:
    if trade.initial_sl is None or (
        trade.balance_at_entry is not None and trade.risk_percent is not None
    ):
        return False
    first = session.scalar(
        select(TradeEvent).where(
            TradeEvent.trade_id == trade.trade_id,
            TradeEvent.event_type == TradeEventType.TRADE_OPENED,
        )
    )
    compute_risk(session, trade, first)
    return trade.risk_percent is not None


def backfill_trade(
    session: Session, trade_id: uuid.UUID, settings: Settings, stats: Counter
) -> None:
    """Completa una operación (bloqueada durante el paso). Suma lo hecho en `stats`."""
    row = session.execute(
        select(Trade, Account)
        .join(Account, Account.id == Trade.account_id)
        .where(Trade.trade_id == trade_id)
        .with_for_update(of=Trade)
    ).first()
    if row is None:
        return
    trade, account = row
    if trade.deployment_id is None:
        resolve_deployment(session, trade)
        if trade.deployment_id is not None:
            stats["asignadas"] += 1
    closed = trade.status == TradeStatus.CLOSED
    if closed and _excursion(session, trade, account.broker_id):
        stats["excursiones"] += 1
    if _risk(session, trade):
        stats["riesgos"] += 1
    session.flush()

    if needs_dna(session, trade, account.broker_id, latest_dna(session, trade.trade_id), settings):
        status = compute_dna(session, trade, settings, account.broker_id).status
        if status == "creado":
            stats["dna"] += 1
    if closed and needs_analysis(
        session, trade, account, latest_analysis(session, trade.trade_id), settings
    ):
        status = analyze_trade(session, trade, settings, account).status
        if status == "creado":
            stats["analisis"] += 1


def backfill_page(
    session: Session,
    settings: Settings,
    after: Cursor | None = None,
    limit: int = 200,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> BackfillPage:
    """Una página de operaciones (por hora de entrada) a partir de `after`. El llamador
    confirma la transacción al terminar la página."""
    limit = max(1, min(limit, MAX_PAGE))
    stmt = select(Trade.entry_time, Trade.trade_id)
    if after is not None:
        stmt = stmt.where(tuple_(Trade.entry_time, Trade.trade_id) > tuple_(*after))
    if date_from is not None:
        stmt = stmt.where(Trade.entry_time >= date_from)
    if date_to is not None:
        stmt = stmt.where(Trade.entry_time < date_to)
    page = session.execute(stmt.order_by(Trade.entry_time, Trade.trade_id).limit(limit)).all()
    stats: Counter = Counter()
    for _, trade_id in page:
        stats["revisadas"] += 1
        try:
            with session.begin_nested():
                backfill_trade(session, trade_id, settings, stats)
        except TRANSIENT_ERRORS:
            raise
        except Exception:
            stats["errores"] += 1
            log.exception("backfill fallido", extra={"trade_id": str(trade_id)})
    next_cursor = (page[-1][0], page[-1][1]) if len(page) == limit else None
    if any(v for k, v in stats.items() if k != "revisadas"):
        log.info("backfill de operaciones", extra=dict(stats))
    return BackfillPage(stats, next_cursor)
