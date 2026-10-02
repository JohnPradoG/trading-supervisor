"""Estadísticas de operaciones cerradas: carga en SQL y métricas puras
(supervisor.analytics.statistics).

- Solo cuentan operaciones CLOSED con neto. Los filtros `from`/`to` son por hora de CIERRE
  (el resultado se realiza al cerrar), en [from, to).
- Las operaciones sin despliegue (sin bot asignado) nunca se mezclan con un bot: van aparte
  en `unassigned`. Si se filtra por bot o versión, no aplica.
- Agrupación por hasta MAX_GROUP_DIMENSIONS dimensiones de GROUP_DIMENSIONS. Día, hora y
  sesión salen de la hora de ENTRADA en UTC (la decisión del bot); timeframe es el principal
  de la versión del bot.
- Memoria: se cargan solo columnas (no objetos ORM), ordenadas por cierre y con un tope de
  stats_max_trades filas (~1 KB por fila en Python); si el filtro abarca más, se rechaza la
  consulta pidiendo acotarla en lugar de truncar en silencio.
"""

import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from supervisor.analytics.sessions import SESSION_HOURS, WEEKDAYS, session_label
from supervisor.analytics.statistics import StatsParams, TradeResult, compute_metrics
from supervisor.config import Settings
from supervisor.models import Bot, BotVersion, Trade
from supervisor.models.enums import TradeSource, TradeStatus
from supervisor.services.errors import NotFound, ServiceError

MAX_GROUP_DIMENSIONS = 2


@dataclass(frozen=True)
class StatsFilters:
    bot_id: uuid.UUID | None = None
    version_id: uuid.UUID | None = None
    symbol: str | None = None
    account_id: uuid.UUID | None = None
    source: TradeSource | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "version_id": self.version_id,
            "symbol": self.symbol,
            "account_id": self.account_id,
            "source": self.source,
            "from": self.date_from,
            "to": self.date_to,
        }


@dataclass(frozen=True)
class Row:
    result: TradeResult
    entry_time: datetime
    symbol: str
    direction: str
    source: str
    deployment_id: uuid.UUID | None
    version_id: uuid.UUID | None
    version: str | None
    timeframe: str | None
    bot_id: uuid.UUID | None
    bot_name: str | None


def _weekday(row: Row) -> tuple[Any, str]:
    day = row.entry_time.astimezone(UTC).weekday()
    return day, WEEKDAYS[day]


def _hour(row: Row) -> tuple[Any, str]:
    hour = row.entry_time.astimezone(UTC).hour
    return hour, f"{hour:02d}h UTC"


def _session(row: Row) -> tuple[Any, str]:
    label = session_label(row.entry_time)
    return label, label


# dimensión -> función (fila -> (clave ordenable, etiqueta)). Fase 8: añadir aquí las
# condiciones del Trading DNA (uniendo trade_dna en `load_rows`).
GROUP_DIMENSIONS: dict[str, Callable[[Row], tuple[Any, str]]] = {
    "bot": lambda r: (r.bot_name or "", r.bot_name or "sin bot"),
    "version": lambda r: ((r.bot_name or "", r.version or ""), f"{r.bot_name} {r.version}"),
    "symbol": lambda r: (r.symbol, r.symbol),
    "direction": lambda r: (r.direction, r.direction),
    "weekday": _weekday,
    "hour": _hour,
    "session": _session,
    "timeframe": lambda r: (r.timeframe or "", r.timeframe or "?"),
    "account": lambda r: (str(r.result.account_id), str(r.result.account_id)),
    "source": lambda r: (r.source, r.source),
}
PENDING_DIMENSIONS = {"dna": "condiciones del Trading DNA: disponible en la fase 8"}


def parse_group_by(value: str | None) -> list[str]:
    if not value:
        return []
    dims = [d.strip().lower() for d in value.split(",") if d.strip()]
    for dim in dims:
        if dim in PENDING_DIMENSIONS:
            raise ServiceError(f"group_by '{dim}': {PENDING_DIMENSIONS[dim]}")
        if dim not in GROUP_DIMENSIONS:
            valid = ", ".join(GROUP_DIMENSIONS)
            raise ServiceError(f"group_by '{dim}' no válido; opciones: {valid}")
    if len(set(dims)) != len(dims):
        raise ServiceError("group_by repite una dimensión")
    if len(dims) > MAX_GROUP_DIMENSIONS:
        raise ServiceError(f"group_by admite como mucho {MAX_GROUP_DIMENSIONS} dimensiones")
    return dims


def params_from(settings: Settings) -> StatsParams:
    return StatsParams(
        min_sample=settings.stats_min_sample,
        breakeven_r_fraction=settings.breakeven_r_fraction,
    )


def parameters(settings: Settings) -> dict[str, Any]:
    """Convenciones de las métricas, para que quien lea los números sepa cómo se calcularon."""
    return {
        "min_sample": settings.stats_min_sample,
        "breakeven_r_fraction": settings.breakeven_r_fraction,
        "confidence_level": 0.95,
        "win_rate_interval": "Wilson",
        "expectancy_interval": "t de Student",
        "ratio_basis": "por operación, sin anualizar (R; si falta, % del balance al entrar)",
        "date_field": "close_time",
        "time_field": "entry_time (UTC)",
        "sessions_utc": {k: f"{a:02d}-{b:02d}" for k, (a, b) in SESSION_HOURS.items()},
        "max_trades": settings.stats_max_trades,
    }


def load_rows(session: Session, filters: StatsFilters, max_rows: int) -> list[Row]:
    stmt = (
        select(
            Trade.trade_id,
            Trade.close_time,
            Trade.entry_time,
            Trade.net_profit,
            Trade.risk_amount,
            Trade.commission,
            Trade.balance_at_entry,
            Trade.duration_seconds,
            Trade.account_id,
            Trade.symbol,
            Trade.direction,
            Trade.source,
            Trade.deployment_id,
            Trade.bot_version_id,
            BotVersion.version,
            BotVersion.main_timeframe,
            Bot.bot_id,
            Bot.name,
        )
        .outerjoin(BotVersion, BotVersion.id == Trade.bot_version_id)
        .outerjoin(Bot, Bot.bot_id == BotVersion.bot_id)
        .where(Trade.status == TradeStatus.CLOSED, Trade.net_profit.is_not(None))
    )
    if filters.bot_id is not None:
        stmt = stmt.where(BotVersion.bot_id == filters.bot_id)
    if filters.version_id is not None:
        stmt = stmt.where(Trade.bot_version_id == filters.version_id)
    if filters.symbol:
        stmt = stmt.where(Trade.symbol == filters.symbol)
    if filters.account_id is not None:
        stmt = stmt.where(Trade.account_id == filters.account_id)
    if filters.source is not None:
        stmt = stmt.where(Trade.source == filters.source)
    if filters.date_from is not None:
        stmt = stmt.where(Trade.close_time >= filters.date_from)
    if filters.date_to is not None:
        stmt = stmt.where(Trade.close_time < filters.date_to)
    stmt = stmt.order_by(Trade.close_time, Trade.trade_id).limit(max_rows + 1)
    rows = session.execute(stmt).all()
    if len(rows) > max_rows:
        raise ServiceError(
            f"el filtro abarca más de {max_rows} operaciones cerradas; acótalo por bot, "
            "versión, símbolo o fechas (límite SUPERVISOR_STATS_MAX_TRADES)"
        )

    def _f(value) -> float | None:
        return None if value is None else float(value)

    return [
        Row(
            result=TradeResult(
                trade_id=str(r.trade_id),
                close_time=r.close_time,
                net=float(r.net_profit),
                risk=_f(r.risk_amount),
                commission=_f(r.commission),
                balance=_f(r.balance_at_entry),
                duration_seconds=r.duration_seconds,
                account_id=str(r.account_id),
            ),
            entry_time=r.entry_time,
            symbol=r.symbol,
            direction=r.direction.value,
            source=r.source.value,
            deployment_id=r.deployment_id,
            version_id=r.bot_version_id,
            version=r.version,
            timeframe=r.main_timeframe,
            bot_id=r.bot_id,
            bot_name=r.name,
        )
        for r in rows
    ]


def _key_value(dim: str, row: Row) -> Any:
    """Valor de la clave de grupo que devuelve la API (id cuando lo hay)."""
    if dim == "bot":
        return str(row.bot_id) if row.bot_id else None
    if dim == "version":
        return str(row.version_id) if row.version_id else None
    return GROUP_DIMENSIONS[dim](row)[0]


def group_metrics(rows: list[Row], dims: list[str], params: StatsParams) -> list[dict[str, Any]]:
    buckets: dict[tuple, list[Row]] = defaultdict(list)
    for row in rows:
        buckets[tuple(GROUP_DIMENSIONS[d](row)[0] for d in dims)].append(row)
    groups = []

    def order(key: tuple) -> tuple:
        return tuple((0, x, "") if isinstance(x, int) else (1, 0, str(x)) for x in key)

    for sort_key in sorted(buckets, key=order):
        members = buckets[sort_key]
        first = members[0]
        groups.append(
            {
                "key": {d: _key_value(d, first) for d in dims},
                "label": " · ".join(GROUP_DIMENSIONS[d](first)[1] for d in dims),
                "metrics": compute_metrics([m.result for m in members], params),
            }
        )
    return groups


def stats(
    session: Session, settings: Settings, filters: StatsFilters, group_by: list[str]
) -> dict[str, Any]:
    params = params_from(settings)
    rows = load_rows(session, filters, settings.stats_max_trades)
    assigned = [r for r in rows if r.deployment_id is not None]
    unassigned = [r for r in rows if r.deployment_id is None]
    by_bot = filters.bot_id is not None or filters.version_id is not None
    return {
        "filters": filters.as_dict(),
        "group_by": group_by,
        "parameters": parameters(settings),
        "total": compute_metrics([r.result for r in assigned], params),
        "groups": group_metrics(assigned, group_by, params) if group_by else [],
        "unassigned": None if by_bot else compute_metrics([r.result for r in unassigned], params),
    }


def compare_versions(
    session: Session, settings: Settings, bot_id: uuid.UUID, filters: StatsFilters
) -> dict[str, Any]:
    """Métricas lado a lado de todas las versiones del bot (también las que no tienen
    operaciones), por fecha de publicación."""
    bot = session.get(Bot, bot_id)
    if bot is None:
        raise NotFound("bot no encontrado")
    params = params_from(settings)
    versions = session.scalars(
        select(BotVersion)
        .where(BotVersion.bot_id == bot_id)
        .order_by(BotVersion.released_at, BotVersion.created_at)
    ).all()
    scoped = StatsFilters(
        bot_id=bot_id,
        symbol=filters.symbol,
        account_id=filters.account_id,
        source=filters.source,
        date_from=filters.date_from,
        date_to=filters.date_to,
    )
    rows = load_rows(session, scoped, settings.stats_max_trades)
    by_version: dict[uuid.UUID, list[TradeResult]] = defaultdict(list)
    for row in rows:
        if row.version_id is not None:
            by_version[row.version_id].append(row.result)
    items = []
    for version in versions:
        metrics = compute_metrics(by_version.get(version.id, []), params)
        items.append(
            {
                "version_id": version.id,
                "version": version.version,
                "main_timeframe": version.main_timeframe,
                "released_at": version.released_at,
                "metrics": metrics,
            }
        )
    small = [i["version"] for i in items if i["metrics"]["n_trades"] < params.min_sample]
    return {
        "bot_id": bot.bot_id,
        "bot_name": bot.name,
        "filters": scoped.as_dict(),
        "parameters": parameters(settings),
        "versions": items,
        "comparison_note": (
            "Una diferencia entre versiones solo es orientativa si sus intervalos al 95 % se "
            "solapan o alguna versión tiene muestra pequeña"
            + (f" ({', '.join(small)})" if small else "")
            + ". Una mejora histórica no es una ventaja demostrada: hay que confirmarla fuera "
            "de muestra (fase 9)."
        ),
    }
