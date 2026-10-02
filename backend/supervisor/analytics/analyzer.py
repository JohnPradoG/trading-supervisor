"""Análisis post-operación: reúne las entradas, calcula hechos e hipótesis y lo guarda como
una versión nueva de trade_analyses (de solo inserción).

Versionado e idempotencia:
- `collect_inputs` reúne todo lo que usa el análisis (operación, eventos, contexto de velas,
  noticias, parámetros) en un dict JSON. `input_hash` = sha256 de ese dict canónico + versión
  del analizador + versión de las reglas.
- `analyze_trade` solo inserta una versión (analysis_version = última + 1) si ese hash difiere
  del de la última versión. Repetirlo sin cambios no crea nada.
- La vigente es la de mayor analysis_version; las anteriores quedan como historia.

Cuándo se analiza:
- El worker, al recomponer una operación cerrada (justo tras calcular sus excursiones).
- El repaso periódico, para operaciones cerradas recientes sin análisis, con otra versión del
  analizador o de las reglas, con otros parámetros, cuya operación o eventos cambiaron (huella
  barata, sin consultar velas), o con datos de velas incompletos si llegaron velas nuevas.
- `supervisor-cli analyze`.
"""

import hashlib
import json
import logging
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError
from sqlalchemy.orm import Session

from supervisor.analytics import market
from supervisor.analytics.facts import build_analysis
from supervisor.analytics.rules import RULESET_VERSION
from supervisor.config import Settings
from supervisor.models import Account, AnalysisFinding, Trade, TradeAnalysis, TradeEvent
from supervisor.models.enums import TradeStatus

log = logging.getLogger("supervisor.analysis")

# Versión del código que calcula hechos y contexto. Subirla re-analiza las operaciones
# recientes en el repaso (y todas con `supervisor-cli analyze --all-closed`).
ANALYZER_VERSION = "1.0.0"
TRANSIENT_ERRORS = (OperationalError, InterfaceError)

_TRADE_FIELDS = {
    "direccion": "direction",
    "simbolo": "symbol",
    "entrada": "entry_time",
    "precio_entrada": "entry_price",
    "sl_inicial": "initial_sl",
    "tp_inicial": "initial_tp",
    "volumen_inicial": "initial_volume",
    "cierre": "close_time",
    "precio_cierre": "close_price",
    "bruto": "gross_profit",
    "comision": "commission",
    "swap": "swap",
    "neto": "net_profit",
    "puntos": "points",
    "duracion_s": "duration_seconds",
    "motivo_salida": "exit_reason",
    "mfe_puntos": "mfe_points",
    "mae_puntos": "mae_points",
    "mfe_dinero": "max_floating_profit",
    "mae_dinero": "max_floating_drawdown",
    "punto": "symbol_point",
    "riesgo": "risk_amount",
    "riesgo_pct": "risk_percent",
    "balance_entrada": "balance_at_entry",
    "reentrada_de": "reentry_of_trade_id",
    "despliegue": "deployment_id",
    "version_bot": "bot_version_id",
}


def _plain(value: Any) -> Any:
    """Valor JSON estable: Decimal y UUID como texto, fechas en ISO UTC, enums por valor."""
    if value is None or isinstance(value, bool | int | str):
        return value.value if hasattr(value, "value") else value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if hasattr(value, "value"):
        return value.value
    return str(value)


def canonical_hash(data: Any) -> str:
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def analysis_parameters(settings: Settings) -> dict[str, Any]:
    return {
        "breakeven_r_fraction": settings.breakeven_r_fraction,
        "ema_warmup_factor": settings.analysis_ema_warmup_factor,
        "cobertura_minima": settings.analysis_min_bar_coverage,
        "volatilidad_dias": settings.analysis_volatility_lookback_days,
        "volatilidad_muestras_min": settings.analysis_volatility_min_samples,
        "ventana_noticias_min": settings.analysis_news_window_minutes,
    }


def _events(session: Session, trade: Trade) -> list[TradeEvent]:
    return list(
        session.scalars(
            select(TradeEvent)
            .where(TradeEvent.trade_id == trade.trade_id)
            .order_by(TradeEvent.event_time_utc, TradeEvent.created_at, TradeEvent.deal_ticket)
        )
    )


def trade_snapshot(trade: Trade, events: list[TradeEvent], currency: str) -> dict[str, Any]:
    """Parte de las entradas que sale de la operación y sus eventos (sin velas)."""
    op = {key: _plain(getattr(trade, attr)) for key, attr in _TRADE_FIELDS.items()}
    op["trade_id"] = str(trade.trade_id)
    quality = trade.data_quality or {}
    excursion = quality.get("excursion")
    op["excursion"] = (
        {k: excursion.get(k) for k in ("cobertura", "completa", "velas_encontradas")}
        if excursion
        else None
    )
    op["precio_medio_entrada"] = quality.get("precio_medio_entrada")
    return {
        "operacion": op,
        "eventos": [
            {
                "tipo": e.event_type.value,
                "hora": _plain(e.event_time_utc),
                "precio": _plain(e.price),
                "volumen": _plain(e.volume),
                "sl": _plain(e.sl),
                "tp": _plain(e.tp),
                "inferido": e.is_inferred,
                "extra": (
                    {"minutos_desde_cierre_por_sl": e.extra.get("minutos_desde_cierre_por_sl")}
                    if e.event_type.value == "REENTRY"
                    else {}
                ),
            }
            for e in events
        ],
        "cuenta": {"moneda": currency},
    }


def collect_inputs(
    session: Session,
    trade: Trade,
    settings: Settings,
    account: Account | None = None,
    events: list[TradeEvent] | None = None,
) -> dict[str, Any]:
    account = account or session.get(Account, trade.account_id)
    events = _events(session, trade) if events is None else events
    base = trade_snapshot(trade, events, account.currency)
    broker, symbol = account.broker_id, trade.symbol
    context = market.market_context(session, broker, symbol, trade.entry_time, settings)
    touch = None
    if trade.initial_tp is not None and trade.exit_reason is not None:
        touch = market.first_touch(
            session,
            broker,
            symbol,
            trade.entry_time,
            trade.close_time,
            float(trade.initial_tp),
            trade.direction.value == "BUY",
        )
    since = market.window_start(trade.entry_time, settings)
    bars_total = market.m1_count(session, broker, symbol, since, trade.close_time)
    excursion = base["operacion"]["excursion"]
    bar = market.entry_bar(session, broker, symbol, trade.entry_time)
    incomplete = (
        not (excursion and excursion.get("completa"))
        or bar is None
        or not context["volatilidad"]["ok"]
        or not all(e["ok"] for e in context["emas"].values())
    )
    params = analysis_parameters(settings)
    return {
        **base,
        "mercado": {
            **context,
            "vela_entrada": bar,
            "toque_tp": touch,
        },
        "noticias": market.news_near(session, trade.entry_time, settings),
        "parametros": params,
        "control": {
            "huella_operacion": canonical_hash(base),
            "huella_parametros": canonical_hash(params),
            "velas_m1": bars_total,
            "ventana_desde": since.isoformat(),
            "incompleto": incomplete,
        },
    }


def input_hash(inputs: dict[str, Any]) -> str:
    return canonical_hash(
        {"analizador": ANALYZER_VERSION, "reglas": RULESET_VERSION, "entradas": inputs}
    )


def latest_analysis(session: Session, trade_id: uuid.UUID) -> TradeAnalysis | None:
    return session.scalar(
        select(TradeAnalysis)
        .where(TradeAnalysis.trade_id == trade_id)
        .order_by(TradeAnalysis.analysis_version.desc())
        .limit(1)
    )


@dataclass(frozen=True)
class AnalyzeResult:
    status: str  # "creado", "sin_cambios", "no_cerrada"
    analysis: TradeAnalysis | None = None


def analyze_trade(
    session: Session, trade: Trade, settings: Settings, account: Account | None = None
) -> AnalyzeResult:
    """Analiza una operación cerrada y guarda una versión nueva solo si cambió algo. El
    llamador debe tener la fila de la operación bloqueada (FOR UPDATE) para que dos procesos
    no numeren a la vez la misma versión."""
    if trade.status != TradeStatus.CLOSED or trade.net_profit is None:
        return AnalyzeResult("no_cerrada")
    inputs = collect_inputs(session, trade, settings, account)
    digest = input_hash(inputs)
    latest = latest_analysis(session, trade.trade_id)
    if latest is not None and latest.input_hash == digest:
        return AnalyzeResult("sin_cambios", latest)
    result = build_analysis(inputs)
    analysis = TradeAnalysis(
        trade_id=trade.trade_id,
        analysis_version=(latest.analysis_version + 1) if latest else 1,
        outcome=result.outcome,
        analyzer_version=ANALYZER_VERSION,
        ruleset_version=RULESET_VERSION,
        input_hash=digest,
        inputs=inputs,
        data_quality=result.data_quality,
    )
    session.add(analysis)
    session.flush()
    session.add_all(
        AnalysisFinding(
            analysis_id=analysis.id,
            position=position,
            kind=finding.kind,
            code=finding.code,
            text=finding.text,
            evidence=finding.evidence,
            confidence=finding.confidence,
            rule_version=finding.rule_version,
            supported_by=list(finding.supported_by),
        )
        for position, finding in enumerate(result.findings)
    )
    session.flush()
    return AnalyzeResult("creado", analysis)


def analyze_safely(
    session: Session, trade: Trade, settings: Settings, account: Account | None = None
) -> str:
    """Como analyze_trade, en un savepoint: un fallo del análisis se registra y no afecta al
    procesamiento de la operación (el repaso lo reintentará). Los errores de conexión sí se
    propagan."""
    try:
        with session.begin_nested():
            return analyze_trade(session, trade, settings, account).status
    except TRANSIENT_ERRORS:
        raise
    except IntegrityError:
        log.warning("análisis concurrente descartado", extra={"trade_id": str(trade.trade_id)})
        return "error"
    except Exception:
        log.exception("análisis fallido", extra={"trade_id": str(trade.trade_id)})
        return "error"


# Repaso y re-análisis ---------------------------------------------------------------------------


def needs_analysis(
    session: Session,
    trade: Trade,
    account: Account,
    latest: TradeAnalysis | None,
    settings: Settings,
) -> bool:
    """Decide con consultas baratas (sin agregar velas) si vale la pena volver a analizar."""
    if latest is None:
        return True
    if latest.analyzer_version != ANALYZER_VERSION or latest.ruleset_version != RULESET_VERSION:
        return True
    control = (latest.inputs or {}).get("control", {})
    if control.get("huella_parametros") != canonical_hash(analysis_parameters(settings)):
        return True
    base = trade_snapshot(trade, _events(session, trade), account.currency)
    if control.get("huella_operacion") != canonical_hash(base):
        return True
    if control.get("incompleto"):
        since = market.window_start(trade.entry_time, settings)
        count = market.m1_count(session, account.broker_id, trade.symbol, since, trade.close_time)
        return count != control.get("velas_m1")
    return False


def reanalyze_recent(session: Session, settings: Settings, now: datetime | None = None) -> Counter:
    """Paso del repaso del worker: operaciones cerradas en los últimos worker_recheck_days
    días, como mucho analysis_max_per_pass análisis por pasada (las más recientes primero)."""
    now = now or datetime.now(UTC)
    since = now - timedelta(days=settings.worker_recheck_days)
    stats: Counter = Counter()
    rows = session.execute(
        select(Trade, Account)
        .join(Account, Account.id == Trade.account_id)
        .where(Trade.status == TradeStatus.CLOSED, Trade.close_time >= since)
        .order_by(Trade.close_time.desc())
    ).all()
    for trade, account in rows:
        if stats["analizadas"] >= settings.analysis_max_per_pass:
            stats["pendientes"] += 1
            continue
        try:
            with session.begin_nested():
                latest = latest_analysis(session, trade.trade_id)
                if not needs_analysis(session, trade, account, latest, settings):
                    continue
                locked = session.scalar(
                    select(Trade).where(Trade.trade_id == trade.trade_id).with_for_update()
                )
                stats["analizadas"] += 1
                result = analyze_trade(session, locked, settings, account)
                stats[result.status] += 1
        except TRANSIENT_ERRORS:
            raise
        except Exception:
            stats["error"] += 1
            log.exception("re-análisis fallido", extra={"trade_id": str(trade.trade_id)})
    return stats


def analyze_by_id(session: Session, trade_id: uuid.UUID, settings: Settings) -> AnalyzeResult:
    trade = session.scalar(select(Trade).where(Trade.trade_id == trade_id).with_for_update())
    if trade is None:
        raise LookupError(f"no existe la operación {trade_id}")
    return analyze_trade(session, trade, settings)


def closed_trade_ids(
    session: Session, after: tuple[datetime, uuid.UUID] | None, limit: int
) -> list[tuple[datetime, uuid.UUID]]:
    """Página de operaciones cerradas por (close_time, trade_id), para recorrerlas todas sin
    cargarlas a la vez."""
    stmt = select(Trade.close_time, Trade.trade_id).where(Trade.status == TradeStatus.CLOSED)
    if after is not None:
        stmt = stmt.where(tuple_(Trade.close_time, Trade.trade_id) > tuple_(*after))
    stmt = stmt.order_by(Trade.close_time, Trade.trade_id).limit(limit)
    return [tuple(row) for row in session.execute(stmt).all()]
