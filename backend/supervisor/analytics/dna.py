"""Trading DNA (fase 8): reúne las velas cerradas antes de la entrada, calcula las variables
(supervisor.analytics.dna_features) y guarda una versión nueva de trade_dna solo si cambió
algo.

Versionado e idempotencia (igual que el análisis de la fase 6):
- input_hash = sha256 de la versión del conjunto de variables, sus parámetros, la operación
  (dirección, hora y precio de entrada, point, timeframe principal), TODAS las velas usadas
  y las noticias. Si coincide con el de la versión vigente no se crea nada.
- Una vela que llega después de la entrada no cambia nada: las consultas se cortan en la
  hora de entrada. Una vela ANTERIOR que llega tarde (backfill) sí cambia el hash y crea una
  versión nueva con más datos; la anterior queda como historia.

Cuándo se calcula:
- El worker, al crear la operación (la entrada ya se conoce) y si la hora o el precio de
  entrada cambian al recomponer deals recibidos fuera de orden.
- El repaso periódico: operaciones con entrada en los últimos worker_recheck_days días sin
  DNA, con otra versión del conjunto de variables, otros parámetros, otra operación (p. ej.
  se asignó después a un bot con otro timeframe) o con variables NULL por falta de velas si
  han llegado velas nuevas (huella barata: número de velas M1 en la ventana). Como mucho
  dna_max_per_pass por pasada.
- `supervisor-cli dna --trade <id>` o `--all`.

Límites para el VPS (1 vCPU, 3.8 GB): cada timeframe consulta solo su ventana de M1,
agregada en PostgreSQL (M1, M5, M15 y el principal si es otro: las velas que piden sus
indicadores, unos 9 días para M15). H1 consulta dna_history_days días (35 por defecto) y de
ahí salen H4 y D1 en Python, así que nunca se recorren más de ~35 días de M1 de un símbolo
(~50 000 filas por la clave primaria) y a Python llegan unas 2 000 velas agregadas. Con 35
días caben la tendencia H4 (100 velas) y D1 (20 velas), pero no la EMA200 de H4 o D1. El EA
envía 24 h de historia al arrancar: conviene BarsBackfillHours=720 (30 días).
"""

import hashlib
import json
import logging
import math
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError
from sqlalchemy.orm import Session

from supervisor.analytics import dna_features as df
from supervisor.analytics.indicators import Bar
from supervisor.analytics.market import aggregated_bars, floor_time
from supervisor.config import Settings
from supervisor.models import (
    Account,
    BotVersion,
    FeatureDefinition,
    NewsEvent,
    PriceBar,
    Trade,
    TradeDna,
)
from supervisor.models.enums import DataOrigin

log = logging.getLogger("supervisor.dna")
TRANSIENT_ERRORS = (OperationalError, InterfaceError)
QUERIED = ("M1", "M5", "M15")


# Catálogo ------------------------------------------------------------------------------------


def feature_rows() -> list[dict[str, Any]]:
    return [
        {
            "name": f.name,
            "version": f.version,
            "group": f.group,
            "timeframe": f.timeframe,
            "value_type": f.value_type,
            "label": f.label,
            "unit": f.unit,
            "searchable": f.searchable,
            "formula_doc": f.formula,
            "null_policy": f.null_policy,
            "available": f.available,
        }
        for f in df.FEATURES
    ]


def ensure_feature_definitions(session: Session) -> int:
    """Registra en feature_definitions las variables del conjunto vigente que falten (la
    tabla es de solo inserción: una definición cambiada lleva versión nueva)."""
    pairs = {(f.name, f.version) for f in df.FEATURES}
    present = session.scalar(
        select(func.count()).where(
            func.concat(FeatureDefinition.name, ":", FeatureDefinition.version).in_(
                [f"{n}:{v}" for n, v in pairs]
            )
        )
    )
    if present == len(pairs):
        return 0
    result = session.execute(
        insert(FeatureDefinition).values(feature_rows()).on_conflict_do_nothing()
    )
    return result.rowcount


# Entradas ------------------------------------------------------------------------------------


def canonical_hash(data: Any) -> str:
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def dna_parameters(settings: Settings) -> dict[str, Any]:
    return {
        "conjunto": df.FEATURE_SET_VERSION,
        "definiciones": df.PARAMS,
        "cobertura_minima": settings.analysis_min_bar_coverage,
        "warmup": settings.analysis_ema_warmup_factor,
        "historia_dias": settings.dna_history_days,
        "volatilidad_dias": settings.analysis_volatility_lookback_days,
        "volatilidad_muestras_min": settings.analysis_volatility_min_samples,
    }


def main_timeframe(session: Session, trade: Trade) -> tuple[str, str | None]:
    """Timeframe principal de la versión del bot, o el de reserva con el motivo."""
    if trade.bot_version_id is None:
        return df.DEFAULT_MAIN_TF, f"operación sin bot: indicadores en {df.DEFAULT_MAIN_TF}"
    tf = session.scalar(
        select(BotVersion.main_timeframe).where(BotVersion.id == trade.bot_version_id)
    )
    tf = (tf or "").upper()
    if tf not in df.TF_MINUTES:
        return df.DEFAULT_MAIN_TF, (
            f"timeframe principal '{tf}' no soportado: indicadores en {df.DEFAULT_MAIN_TF}"
        )
    return tf, None


def _span(bars: int, minutes: int) -> timedelta:
    return timedelta(minutes=math.ceil(bars * minutes * 7 / 5)) + timedelta(days=3)


def _group(bars: list[Bar], minutes: int, cutoff: datetime) -> list[Bar]:
    """Agrupa velas H1 en velas de `minutes` (H4, D1), solo las cerradas antes del corte."""
    groups: dict[datetime, list[Bar]] = {}
    for b in bars:
        start = floor_time(b.time, minutes)
        if start < cutoff:
            groups.setdefault(start, []).append(b)
    return [
        Bar(
            time=start,
            open=members[0].open,
            high=max(m.high for m in members),
            low=min(m.low for m in members),
            close=members[-1].close,
            m1_count=sum(m.m1_count for m in members),
        )
        for start, members in sorted(groups.items())
    ]


def history_start(entry: datetime, settings: Settings) -> datetime:
    """Primera hora de M1 que puede consultar el DNA (la ventana de H1)."""
    return floor_time(entry, 60) - timedelta(days=settings.dna_history_days)


@dataclass
class Collected:
    inputs: df.DnaInputs
    fingerprint: dict[str, Any]
    last_bar_time: datetime | None
    m1_count: int


def collect(session: Session, trade: Trade, broker_id: uuid.UUID, settings: Settings) -> Collected:
    entry = trade.entry_time.astimezone(UTC)
    main_tf, note = main_timeframe(session, trade)
    floor_start = history_start(entry, settings)
    cutoffs = {tf: floor_time(entry, minutes) for tf, minutes in df.TF_MINUTES.items()}
    bars: dict[str, list[Bar]] = {}
    for tf in (*QUERIED, *((main_tf,) if main_tf == "M30" else ())):
        minutes = df.TF_MINUTES[tf]
        needed = df.needed_bars(tf, main_tf, settings.analysis_ema_warmup_factor)
        start = max(cutoffs[tf] - _span(needed, minutes), floor_start)
        bars[tf] = aggregated_bars(session, broker_id, trade.symbol, minutes, start, cutoffs[tf])
    bars["H1"] = aggregated_bars(session, broker_id, trade.symbol, 60, floor_start, cutoffs["H1"])
    bars["H4"] = _group(bars["H1"], 240, cutoffs["H4"])
    bars["D1"] = _group(bars["H1"], 1440, cutoffs["D1"])

    last_m1 = bars["M1"][-1].time if bars["M1"] else None
    spread = None
    spread_row = session.execute(
        select(PriceBar.bar_time, PriceBar.spread)
        .where(
            PriceBar.broker_id == broker_id,
            PriceBar.symbol == trade.symbol,
            PriceBar.timeframe == "M1",
            PriceBar.bar_time < cutoffs["M1"],
            PriceBar.bar_time >= cutoffs["M1"] - timedelta(minutes=df.PARAMS["spread_max_minutos"]),
        )
        .order_by(PriceBar.bar_time.desc())
        .limit(1)
    ).first()
    if spread_row is not None:
        spread = {"puntos": spread_row.spread, "vela": spread_row.bar_time.isoformat()}

    source_days = timedelta(days=df.PARAMS["noticias_fuente_dias"])
    has_source = session.scalar(
        select(NewsEvent.id)
        .where(NewsEvent.time_utc >= entry - source_days, NewsEvent.time_utc <= entry + source_days)
        .limit(1)
    )
    news = None
    if has_source is not None:
        window = timedelta(minutes=df.PARAMS["noticias_ventana_min"])
        rows = session.scalars(
            select(NewsEvent)
            .where(NewsEvent.time_utc >= entry - window, NewsEvent.time_utc <= entry + window)
            .order_by(NewsEvent.time_utc)
            .limit(50)
        ).all()
        news = [
            {
                "minutos": round((n.time_utc - entry).total_seconds() / 60, 1),
                "impacto": n.impact,
                "titulo": n.title,
                "moneda": n.currency,
                "fuente": n.source,
            }
            for n in rows
        ]

    point = float(trade.symbol_point) if trade.symbol_point else None
    inputs = df.DnaInputs(
        direction=trade.direction.value,
        entry_time=entry,
        entry_price=float(trade.entry_price),
        point=point,
        main_tf=main_tf,
        main_tf_note=note,
        bars=bars,
        cutoffs=cutoffs,
        spread=spread,
        news=news,
        min_coverage=settings.analysis_min_bar_coverage,
        warmup=settings.analysis_ema_warmup_factor,
        volatility_days=settings.analysis_volatility_lookback_days,
        volatility_min_samples=settings.analysis_volatility_min_samples,
    )
    m1_total = _m1_count(session, broker_id, trade, settings)
    fingerprint = {
        "operacion": trade_snapshot(trade, main_tf),
        "parametros": dna_parameters(settings),
        "velas": {
            tf: [[b.time.isoformat(), b.open, b.high, b.low, b.close, b.m1_count] for b in series]
            for tf, series in sorted(bars.items())
        },
        "spread": spread,
        "noticias": news,
    }
    return Collected(inputs, fingerprint, last_m1, m1_total)


def trade_snapshot(trade: Trade, main_tf: str) -> dict[str, Any]:
    return {
        "direccion": trade.direction.value,
        "simbolo": trade.symbol,
        "entrada": trade.entry_time.astimezone(UTC).isoformat(),
        "precio_entrada": str(trade.entry_price),
        "punto": str(trade.symbol_point) if trade.symbol_point is not None else None,
        "tf_principal": main_tf,
    }


# Cálculo y guardado ------------------------------------------------------------------------


def latest_dna(session: Session, trade_id: uuid.UUID) -> TradeDna | None:
    return session.scalar(
        select(TradeDna)
        .where(TradeDna.trade_id == trade_id)
        .order_by(TradeDna.dna_version.desc())
        .limit(1)
    )


@dataclass(frozen=True)
class DnaOutcome:
    status: str  # "creado" o "sin_cambios"
    dna: TradeDna


def compute_dna(
    session: Session, trade: Trade, settings: Settings, broker_id: uuid.UUID | None = None
) -> DnaOutcome:
    """Calcula el DNA de la operación y guarda una versión nueva solo si cambió el hash. El
    llamador debe tener la fila de la operación bloqueada para numerar versiones sin carreras
    (como en el análisis)."""
    if broker_id is None:
        broker_id = session.scalar(select(Account.broker_id).where(Account.id == trade.account_id))
    ensure_feature_definitions(session)
    collected = collect(session, trade, broker_id, settings)
    digest = canonical_hash(collected.fingerprint)
    latest = latest_dna(session, trade.trade_id)
    if latest is not None and latest.input_hash == digest:
        return DnaOutcome("sin_cambios", latest)
    result = df.build_dna(collected.inputs)
    missing_data = sorted(
        name for name, reason in result.null_reasons.items() if not _not_data(reason)
    )
    dna = TradeDna(
        trade_id=trade.trade_id,
        dna_version=(latest.dna_version + 1) if latest else 1,
        feature_set_version=df.FEATURE_SET_VERSION,
        input_hash=digest,
        source=DataOrigin.SERVER,
        data_cutoff=trade.entry_time,
        last_bar_time=collected.last_bar_time,
        features=result.features,
        null_reasons=result.null_reasons,
        data_quality=result.quality,
        inputs={
            "control": {
                "huella_operacion": canonical_hash(collected.fingerprint["operacion"]),
                "huella_parametros": canonical_hash(collected.fingerprint["parametros"]),
                "velas_m1": collected.m1_count,
                "desde": history_start(trade.entry_time, settings).isoformat(),
                "incompleto": bool(missing_data),
                "sin_datos": missing_data,
            },
            "spread": collected.fingerprint["spread"],
            "noticias": collected.fingerprint["noticias"],
        },
    )
    session.add(dna)
    session.flush()
    return DnaOutcome("creado", dna)


def _not_data(reason: str) -> bool:
    """Motivos de NULL que no se arreglan con más velas."""
    return reason.startswith("no aplica") or reason == df.NO_NEWS_SOURCE or "point" in reason


def compute_dna_safely(
    session: Session, trade: Trade, settings: Settings, broker_id: uuid.UUID | None = None
) -> str:
    """Como compute_dna, en un savepoint: un fallo se registra y no afecta al registro de la
    operación (el repaso lo reintenta). Los errores de conexión sí se propagan."""
    try:
        with session.begin_nested():
            return compute_dna(session, trade, settings, broker_id).status
    except TRANSIENT_ERRORS:
        raise
    except IntegrityError:
        log.warning("DNA concurrente descartado", extra={"trade_id": str(trade.trade_id)})
        return "error"
    except Exception:
        log.exception("DNA fallido", extra={"trade_id": str(trade.trade_id)})
        return "error"


def _m1_count(session: Session, broker_id: uuid.UUID, trade: Trade, settings: Settings) -> int:
    start = history_start(trade.entry_time, settings)
    return session.scalar(
        select(func.count()).where(
            PriceBar.broker_id == broker_id,
            PriceBar.symbol == trade.symbol,
            PriceBar.timeframe == "M1",
            PriceBar.bar_time >= start,
            PriceBar.bar_time < floor_time(trade.entry_time, 1),
        )
    )


def needs_dna(
    session: Session,
    trade: Trade,
    broker_id: uuid.UUID,
    latest: TradeDna | None,
    settings: Settings,
) -> bool:
    """Decide con consultas baratas (sin agregar velas) si hay que recalcular."""
    if latest is None or latest.feature_set_version != df.FEATURE_SET_VERSION:
        return True
    control = (latest.inputs or {}).get("control", {})
    if control.get("huella_parametros") != canonical_hash(dna_parameters(settings)):
        return True
    main_tf, _ = main_timeframe(session, trade)
    if control.get("huella_operacion") != canonical_hash(trade_snapshot(trade, main_tf)):
        return True
    if control.get("incompleto"):
        return _m1_count(session, broker_id, trade, settings) != control.get("velas_m1")
    return False


def recompute_recent(session: Session, settings: Settings, now: datetime | None = None) -> Counter:
    """Paso del repaso del worker: operaciones con entrada en los últimos worker_recheck_days
    días (las más recientes primero), como mucho dna_max_per_pass cálculos por pasada."""
    now = now or datetime.now(UTC)
    since = now - timedelta(days=settings.worker_recheck_days)
    stats: Counter = Counter()
    rows = session.execute(
        select(Trade, Account.broker_id)
        .join(Account, Account.id == Trade.account_id)
        .where(Trade.entry_time >= since)
        .order_by(Trade.entry_time.desc())
    ).all()
    for trade, broker_id in rows:
        if stats["calculados"] >= settings.dna_max_per_pass:
            stats["pendientes"] += 1
            continue
        try:
            with session.begin_nested():
                latest = latest_dna(session, trade.trade_id)
                if not needs_dna(session, trade, broker_id, latest, settings):
                    continue
                locked = session.scalar(
                    select(Trade).where(Trade.trade_id == trade.trade_id).with_for_update()
                )
                stats["calculados"] += 1
                stats[compute_dna(session, locked, settings, broker_id).status] += 1
        except TRANSIENT_ERRORS:
            raise
        except Exception:
            stats["error"] += 1
            log.exception("DNA fallido en el repaso", extra={"trade_id": str(trade.trade_id)})
    return stats


def dna_by_id(session: Session, trade_id: uuid.UUID, settings: Settings) -> DnaOutcome:
    trade = session.scalar(select(Trade).where(Trade.trade_id == trade_id).with_for_update())
    if trade is None:
        raise LookupError(f"no existe la operación {trade_id}")
    return compute_dna(session, trade, settings)


def trade_ids_page(
    session: Session, after: tuple[datetime, uuid.UUID] | None, limit: int
) -> list[tuple[datetime, uuid.UUID]]:
    """Página de operaciones por (entry_time, trade_id) para recorrerlas todas."""
    stmt = select(Trade.entry_time, Trade.trade_id)
    if after is not None:
        stmt = stmt.where(
            (Trade.entry_time > after[0])
            | ((Trade.entry_time == after[0]) & (Trade.trade_id > after[1]))
        )
    stmt = stmt.order_by(Trade.entry_time, Trade.trade_id).limit(limit)
    return [tuple(row) for row in session.execute(stmt).all()]
