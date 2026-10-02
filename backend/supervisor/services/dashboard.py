"""Consultas del dashboard (solo lectura).

Todas están acotadas (LIMIT o ventana de fechas) porque el VPS tiene 1 vCPU y 3.8 GB de RAM
compartidos con PostgreSQL, la API y el worker. Las series largas (equity, velas) se reducen
en el servidor a un máximo de puntos con date_bin: se toma el último valor de cada intervalo.
"""

import math
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, case, func, select, text
from sqlalchemy.orm import Session

from supervisor.models import (
    Account,
    AccountSnapshot,
    Bot,
    BotVersion,
    Broker,
    Deployment,
    Heartbeat,
    RawEvent,
    Terminal,
    Trade,
)
from supervisor.models.enums import Direction, FindingKind, RawEventStatus, TradeStatus
from supervisor.services import trades as trades_service

ONLINE_SECONDS = 120
MAX_SERIES_POINTS = 500
EQUITY_RANGES = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
UNASSIGNED = "none"  # valor del filtro de bot para "sin asignar"


def utc_now() -> datetime:
    return datetime.now(UTC)


# Salud del sistema -------------------------------------------------------------------------


@dataclass(frozen=True)
class TerminalHealth:
    id: uuid.UUID
    name: str
    account_login: int
    account_server: str
    last_seen_at: datetime | None
    heartbeat_at: datetime | None
    ea_version: str | None
    terminal_connected: bool | None
    trade_allowed: bool | None
    online: bool


def terminals_health(session: Session, now: datetime, limit: int = 100) -> list[TerminalHealth]:
    """Cada terminal con su último latido (subconsulta lateral sobre la PK de heartbeats)."""
    last_hb = (
        select(
            Heartbeat.ts,
            Heartbeat.ea_version,
            Heartbeat.terminal_connected,
            Heartbeat.trade_allowed,
        )
        .where(Heartbeat.terminal_id == Terminal.id)
        .order_by(Heartbeat.ts.desc())
        .limit(1)
        .lateral("hb")
    )
    rows = session.execute(
        select(Terminal, Account.login, Account.server, last_hb)
        .join(Account, Terminal.account_id == Account.id)
        .outerjoin(last_hb, text("true"))
        .order_by(Terminal.name)
        .limit(limit)
    ).all()
    limit_online = now - timedelta(seconds=ONLINE_SECONDS)
    return [
        TerminalHealth(
            id=r.Terminal.id,
            name=r.Terminal.name,
            account_login=r.login,
            account_server=r.server,
            last_seen_at=r.Terminal.last_seen_at,
            heartbeat_at=r.ts,
            ea_version=r.ea_version,
            terminal_connected=r.terminal_connected,
            trade_allowed=r.trade_allowed,
            online=r.ts is not None and r.ts >= limit_online,
        )
        for r in rows
    ]


@dataclass(frozen=True)
class WorkerBacklog:
    pending: int
    failed: int
    last_processed_id: int | None
    last_processed_type: str | None
    last_processed_at: datetime | None
    last_processed_status: str | None


def worker_backlog(session: Session) -> WorkerBacklog:
    counts = dict(
        session.execute(
            select(RawEvent.status, func.count())
            .where(RawEvent.status.in_([RawEventStatus.PENDING, RawEventStatus.FAILED]))
            .group_by(RawEvent.status)
        )
        .tuples()
        .all()
    )
    # Por id descendente (usa la PK) en lugar de ordenar por processed_at, que no tiene índice:
    # el evento procesado de id más alto es, en la práctica, el último procesado.
    last = session.execute(
        select(RawEvent.id, RawEvent.event_type, RawEvent.processed_at, RawEvent.status)
        .where(RawEvent.processed_at.is_not(None))
        .order_by(RawEvent.id.desc())
        .limit(1)
    ).first()
    return WorkerBacklog(
        pending=counts.get(RawEventStatus.PENDING, 0),
        failed=counts.get(RawEventStatus.FAILED, 0),
        last_processed_id=last.id if last else None,
        last_processed_type=last.event_type if last else None,
        last_processed_at=last.processed_at if last else None,
        last_processed_status=last.status.value if last else None,
    )


@dataclass(frozen=True)
class AccountBalance:
    account_id: uuid.UUID
    login: int
    server: str
    currency: str
    broker: str
    ts: datetime | None
    balance: Decimal | None
    equity: Decimal | None
    margin: Decimal | None
    open_positions: int | None


def account_balances(session: Session, limit: int = 50) -> list[AccountBalance]:
    """Última foto de balance/equity de cada cuenta activa."""
    snap = (
        select(
            AccountSnapshot.ts,
            AccountSnapshot.balance,
            AccountSnapshot.equity,
            AccountSnapshot.margin,
            AccountSnapshot.open_positions,
        )
        .where(AccountSnapshot.account_id == Account.id)
        .order_by(AccountSnapshot.ts.desc())
        .limit(1)
        .lateral("snap")
    )
    rows = session.execute(
        select(Account, Broker.name, snap)
        .join(Broker, Account.broker_id == Broker.id)
        .outerjoin(snap, text("true"))
        .where(Account.active.is_(True))
        .order_by(snap.c.ts.desc().nulls_last(), Account.login)
        .limit(limit)
    ).all()
    return [
        AccountBalance(
            account_id=r.Account.id,
            login=r.Account.login,
            server=r.Account.server,
            currency=r.Account.currency,
            broker=r.name,
            ts=r.ts,
            balance=r.balance,
            equity=r.equity,
            margin=r.margin,
            open_positions=r.open_positions,
        )
        for r in rows
    ]


# Operaciones ------------------------------------------------------------------------------


@dataclass(frozen=True)
class TradeRow:
    trade: Trade
    bot_name: str | None
    version: str | None
    account_login: int

    @property
    def r_multiple(self) -> Decimal | None:
        """Resultado neto en múltiplos del riesgo inicial (R). None si no hay riesgo."""
        t = self.trade
        if t.net_profit is None or not t.risk_amount or t.risk_amount <= 0:
            return None
        return (t.net_profit / t.risk_amount).quantize(Decimal("0.01"))


def _trade_rows_stmt():
    return (
        select(Trade, Bot.name, BotVersion.version, Account.login)
        .join(Account, Trade.account_id == Account.id)
        .outerjoin(BotVersion, Trade.bot_version_id == BotVersion.id)
        .outerjoin(Bot, BotVersion.bot_id == Bot.bot_id)
    )


def _to_rows(result) -> list[TradeRow]:
    return [
        TradeRow(trade=r.Trade, bot_name=r.name, version=r.version, account_login=r.login)
        for r in result
    ]


def open_trades(session: Session, limit: int = 100) -> list[TradeRow]:
    stmt = (
        _trade_rows_stmt()
        .where(Trade.status == TradeStatus.OPEN)
        .order_by(Trade.entry_time.desc())
        .limit(limit)
    )
    return _to_rows(session.execute(stmt).all())


@dataclass(frozen=True)
class ClosedSummary:
    count: int
    wins: int
    net: Decimal

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.count * 100 if self.count else None


def closed_summary(session: Session, since: datetime, until: datetime) -> ClosedSummary:
    """Operaciones cerradas en [since, until): número, neto y ganadoras (neto > 0)."""
    row = session.execute(
        select(
            func.count(),
            func.count().filter(Trade.net_profit > 0),
            func.coalesce(func.sum(Trade.net_profit), 0),
        ).where(
            Trade.status == TradeStatus.CLOSED,
            Trade.close_time >= since,
            Trade.close_time < until,
        )
    ).one()
    return ClosedSummary(count=row[0], wins=row[1], net=Decimal(row[2]))


@dataclass(frozen=True)
class DashboardTradeFilters:
    bot: str | None = None  # uuid del bot o UNASSIGNED
    version_id: uuid.UUID | None = None
    symbol: str | None = None
    status: TradeStatus | None = None
    direction: Direction | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None


def list_trades(
    session: Session, filters: DashboardTradeFilters, limit: int = 50, offset: int = 0
) -> tuple[list[TradeRow], bool]:
    """Página de operaciones (entrada descendente) y si hay más páginas."""
    stmt = _trade_rows_stmt()
    if filters.bot == UNASSIGNED:
        stmt = stmt.where(Trade.bot_version_id.is_(None))
    elif filters.bot:
        stmt = stmt.where(BotVersion.bot_id == uuid.UUID(filters.bot))
    if filters.version_id is not None:
        stmt = stmt.where(Trade.bot_version_id == filters.version_id)
    if filters.symbol:
        stmt = stmt.where(Trade.symbol == filters.symbol)
    if filters.status is not None:
        stmt = stmt.where(Trade.status == filters.status)
    if filters.direction is not None:
        stmt = stmt.where(Trade.direction == filters.direction)
    if filters.date_from is not None:
        stmt = stmt.where(Trade.entry_time >= filters.date_from)
    if filters.date_to is not None:
        stmt = stmt.where(Trade.entry_time < filters.date_to)
    stmt = stmt.order_by(Trade.entry_time.desc(), Trade.trade_id).limit(limit + 1).offset(offset)
    rows = _to_rows(session.execute(stmt).all())
    return rows[:limit], len(rows) > limit


@dataclass(frozen=True)
class FilterOptions:
    bots: list[tuple[uuid.UUID, str]]
    versions: list[tuple[uuid.UUID, str]]
    symbols: list[str]


def filter_options(session: Session) -> FilterOptions:
    bots = session.execute(select(Bot.bot_id, Bot.name).order_by(Bot.name).limit(200)).all()
    versions = session.execute(
        select(BotVersion.id, Bot.name, BotVersion.version)
        .join(Bot, BotVersion.bot_id == Bot.bot_id)
        .order_by(Bot.name, BotVersion.released_at.desc())
        .limit(500)
    ).all()
    symbols = session.scalars(
        select(Trade.symbol).distinct().order_by(Trade.symbol).limit(200)
    ).all()
    return FilterOptions(
        bots=[(b.bot_id, b.name) for b in bots],
        versions=[(v.id, f"{v.name} v{v.version}") for v in versions],
        symbols=list(symbols),
    )


@dataclass
class TradeDetail:
    row: TradeRow
    events: list
    account: Account
    broker_name: str
    deployment: Deployment | None
    reentry_of: Trade | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def trade_detail(session: Session, trade_id: uuid.UUID) -> TradeDetail:
    trade, events = trades_service.get_trade(session, trade_id)  # NotFound si no existe
    row = _to_rows(session.execute(_trade_rows_stmt().where(Trade.trade_id == trade_id)).all())[0]
    account = session.get(Account, trade.account_id)
    broker = session.get(Broker, account.broker_id)
    deployment = session.get(Deployment, trade.deployment_id) if trade.deployment_id else None
    reentry_of = None
    if trade.reentry_of_trade_id:
        reentry_of = session.get(Trade, trade.reentry_of_trade_id)
    return TradeDetail(
        row=row,
        events=events,
        account=account,
        broker_name=broker.name,
        deployment=deployment,
        reentry_of=reentry_of,
    )


def trade_analysis(session: Session, trade_id: uuid.UUID) -> dict[str, Any] | None:
    """Análisis vigente de la Fase 6 en la forma que usa templates/partials/analisis.html.
    None si la operación sigue abierta o el worker aún no la analizó."""
    from supervisor.services.analysis import get_trade_analysis

    analysis = get_trade_analysis(session, trade_id).analysis
    if analysis is None:
        return None
    findings = sorted(analysis.findings, key=lambda f: f.position)

    def _finding(f: Any) -> dict[str, Any]:
        confidence = f.confidence.replace("_", " ") if f.confidence else None
        return {"text": f.text, "confidence": confidence}

    return {
        "outcome": analysis.outcome,
        "analyzer_version": analysis.analyzer_version,
        "created_at": analysis.created_at,
        "facts": [_finding(f) for f in findings if f.kind == FindingKind.FACT],
        "hypotheses": [_finding(f) for f in findings if f.kind == FindingKind.HYPOTHESIS],
        "data_quality": list(analysis.data_quality or []),
    }


# Series para gráficos ----------------------------------------------------------------------


def _bucket_seconds(span: timedelta, max_points: int, minimum: int = 1) -> int:
    # Con un ancho de ceil(span / (max-1)) hay como mucho max intervalos en [inicio, fin].
    return max(minimum, math.ceil(span.total_seconds() / max(max_points - 1, 1)))


def equity_series(
    session: Session,
    account_id: uuid.UUID,
    start: datetime,
    end: datetime,
    max_points: int = MAX_SERIES_POINTS,
) -> list[tuple[datetime, Decimal, Decimal]]:
    """(ts, balance, equity) entre start y end, como mucho max_points: la última foto de cada
    intervalo de date_bin."""
    width = _bucket_seconds(end - start, max_points)
    rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (bucket)
                   date_bin(make_interval(secs => :width), ts, :start) AS bucket,
                   ts, balance, equity
            FROM account_snapshots
            WHERE account_id = :account_id AND ts >= :start AND ts <= :end
            ORDER BY bucket, ts DESC
            """
        ),
        {"width": width, "start": start, "end": end, "account_id": account_id},
    ).all()
    return [(r.ts, r.balance, r.equity) for r in rows]


BARS_MARGIN_MINUTES = 30
MAX_BAR_POINTS = 1000


def trade_bars(
    session: Session,
    trade: Trade,
    broker_id: uuid.UUID,
    now: datetime,
    margin_minutes: int = BARS_MARGIN_MINUTES,
    max_points: int = MAX_BAR_POINTS,
) -> list[tuple[datetime, Decimal]]:
    """Cierres M1 desde entrada − N hasta cierre (o ahora) + N minutos. Si el tramo es largo se
    reduce al último cierre de cada intervalo."""
    start = trade.entry_time - timedelta(minutes=margin_minutes)
    end = (trade.close_time or now) + timedelta(minutes=margin_minutes)
    width = _bucket_seconds(end - start, max_points, minimum=60)
    rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (bucket)
                   date_bin(make_interval(secs => :width), bar_time, :start) AS bucket,
                   bar_time, close
            FROM price_bars
            WHERE broker_id = :broker_id AND symbol = :symbol AND timeframe = 'M1'
              AND bar_time >= :start AND bar_time <= :end
            ORDER BY bucket, bar_time DESC
            """
        ),
        {
            "width": width,
            "start": start,
            "end": end,
            "broker_id": broker_id,
            "symbol": trade.symbol,
        },
    ).all()
    return [(r.bar_time, r.close) for r in rows]


# Bots ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class VersionStats:
    closed: int
    open: int
    wins: int
    net: Decimal
    gross_win: Decimal
    gross_loss: Decimal

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.closed * 100 if self.closed else None

    @property
    def profit_factor(self) -> Decimal | None:
        if not self.gross_loss:
            return None
        return (self.gross_win / self.gross_loss).quantize(Decimal("0.01"))


EMPTY_STATS = VersionStats(0, 0, 0, Decimal(0), Decimal(0), Decimal(0))


@dataclass
class VersionOverview:
    version: BotVersion
    stats: VersionStats
    deployments: list[tuple[Deployment, int, str]]  # (despliegue, login, servidor)


@dataclass
class BotOverview:
    bot: Bot
    versions: list[VersionOverview]


def bots_overview(session: Session, limit: int = 200) -> list[BotOverview]:
    bots = list(session.scalars(select(Bot).order_by(Bot.name).limit(limit)))
    if not bots:
        return []
    bot_ids = [b.bot_id for b in bots]
    versions = list(
        session.scalars(
            select(BotVersion)
            .where(BotVersion.bot_id.in_(bot_ids))
            .order_by(BotVersion.released_at.desc(), BotVersion.created_at.desc())
        )
    )
    version_ids = [v.id for v in versions]
    deployments: dict[uuid.UUID, list[tuple[Deployment, int, str]]] = {}
    stats: dict[uuid.UUID, VersionStats] = {}
    if version_ids:
        for dep, login, server in session.execute(
            select(Deployment, Account.login, Account.server)
            .join(Account, Deployment.account_id == Account.id)
            .where(Deployment.bot_version_id.in_(version_ids))
            .order_by(Deployment.ended_at.is_not(None), Deployment.started_at.desc())
        ).all():
            deployments.setdefault(dep.bot_version_id, []).append((dep, login, server))
        closed = Trade.status == TradeStatus.CLOSED
        for r in session.execute(
            select(
                Trade.bot_version_id,
                func.count().filter(closed),
                func.count().filter(Trade.status == TradeStatus.OPEN),
                func.count().filter(and_(closed, Trade.net_profit > 0)),
                func.coalesce(func.sum(Trade.net_profit).filter(closed), 0),
                func.coalesce(
                    func.sum(case((Trade.net_profit > 0, Trade.net_profit), else_=0)).filter(
                        closed
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(case((Trade.net_profit < 0, -Trade.net_profit), else_=0)).filter(
                        closed
                    ),
                    0,
                ),
            )
            .where(Trade.bot_version_id.in_(version_ids))
            .group_by(Trade.bot_version_id)
        ).all():
            stats[r[0]] = VersionStats(
                closed=r[1],
                open=r[2],
                wins=r[3],
                net=Decimal(r[4]),
                gross_win=Decimal(r[5]),
                gross_loss=Decimal(r[6]),
            )
    by_bot: dict[uuid.UUID, list[VersionOverview]] = {}
    for v in versions:
        by_bot.setdefault(v.bot_id, []).append(
            VersionOverview(
                version=v,
                stats=stats.get(v.id, EMPTY_STATS),
                deployments=deployments.get(v.id, []),
            )
        )
    return [BotOverview(bot=b, versions=by_bot.get(b.bot_id, [])) for b in bots]


# Sistema ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class FailedEvent:
    id: int
    terminal: str
    event_type: str
    event_time: datetime | None
    received_at: datetime
    attempts: int
    error: str | None


def failed_events(session: Session, limit: int = 50) -> list[FailedEvent]:
    rows = session.execute(
        select(
            RawEvent.id,
            Terminal.name,
            RawEvent.event_type,
            RawEvent.event_time,
            RawEvent.received_at,
            RawEvent.attempts,
            RawEvent.error,
        )
        .join(Terminal, RawEvent.terminal_id == Terminal.id)
        .where(RawEvent.status == RawEventStatus.FAILED)
        .order_by(RawEvent.id.desc())
        .limit(limit)
    ).all()
    return [
        FailedEvent(
            id=r.id,
            terminal=r.name,
            event_type=r.event_type,
            event_time=r.event_time,
            received_at=r.received_at,
            attempts=r.attempts,
            error=(r.error or "")[:500] or None,
        )
        for r in rows
    ]


@dataclass(frozen=True)
class UnassignedGroup:
    account_login: int
    account_server: str
    magic_number: int
    symbol: str
    trades: int
    open_trades: int
    first_entry: datetime
    last_entry: datetime


def unassigned_trades(session: Session, limit: int = 100) -> list[UnassignedGroup]:
    """Operaciones sin despliegue agrupadas por cuenta + magic + símbolo: lo que falta
    registrar para que el worker las asigne a un bot."""
    last_entry = func.max(Trade.entry_time)
    rows = session.execute(
        select(
            Account.login,
            Account.server,
            Trade.magic_number,
            Trade.symbol,
            func.count(),
            func.count().filter(Trade.status == TradeStatus.OPEN),
            func.min(Trade.entry_time),
            last_entry,
        )
        .join(Account, Trade.account_id == Account.id)
        .where(Trade.deployment_id.is_(None))
        .group_by(Account.login, Account.server, Trade.magic_number, Trade.symbol)
        .order_by(last_entry.desc())
        .limit(limit)
    ).all()
    return [UnassignedGroup(*r) for r in rows]
