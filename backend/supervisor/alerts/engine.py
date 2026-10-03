"""Motor de alertas del worker (sección 15). Periódico, acotado e idempotente.

Tipos (por prioridad):
- TRAMPA_ACTIVA (CRITICAL): una operación recién abierta (alerts_trap_window_minutes, sin las
  importadas del historial) cuyo Trading DNA cumple una trampa VALIDADA fuera de muestra de su
  versión de bot (alcance de toda la versión o de su símbolo). Una alerta por operación y
  trampa. Las candidatas no validadas nunca disparan esta alerta. Las condiciones sobre
  hechos de la fase 6 previos a la entrada (precio frente a EMA200/EMA50 de H1 y M15,
  volatilidad, spread) se evalúan con los que el DNA guarda al abrir
  (supervisor.analytics.pre_entry); las que dependen de lo que pasó después, no.
- PATRON_VALIDADO (WARNING) / PATRON_CADUCADO (INFO): una trampa pasa a VALIDATED o a
  DECAYED (cambiadas en los últimos alerts_pattern_lookback_days días). Una vez por hipótesis.
- EA_SIN_LATIDO (WARNING): un terminal sin latido desde hace alerts_heartbeat_minutes, solo
  con el mercado abierto (sin fines de semana, configurable). Se resuelve (fila RESUELTA)
  cuando vuelve el latido.

Deduplicación: cada condición tiene una dedup_key (p. ej. EA_SIN_LATIDO:<terminal>). Mientras
la última fila de esa clave es un DISPARO no se repite. Las de sistema se cierran con una fila
RESUELTA cuando la condición desaparece, y no vuelven a dispararse hasta pasados
alerts_cooldown_minutes desde el último disparo (evita el parpadeo). Las de operación y patrón
son únicas: se disparan una sola vez. Todo es de solo inserción (trigger en la base).

Entrega: si Telegram está configurado la fila nace PENDING y `deliver_pending` la envía
(acotado, ~1 mensaje/s, reintentos con espera exponencial, FAILED al agotarlos); si no, nace
SKIPPED y solo se ve en el dashboard.
"""

import logging
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from supervisor.alerts import telegram as tg
from supervisor.analytics import pattern_search as ps
from supervisor.analytics import patterns as pt
from supervisor.analytics.dna_features import FEATURES
from supervisor.analytics.pre_entry import fact_values
from supervisor.config import Settings
from supervisor.models import (
    Account,
    Alert,
    Bot,
    BotVersion,
    Heartbeat,
    Hypothesis,
    PatternTest,
    Terminal,
    Trade,
)
from supervisor.models.enums import AlertSeverity, HypothesisStatus, SplitSegment

log = logging.getLogger("supervisor.alerts")

FIRE, RESOLVE = "DISPARO", "RESUELTA"
PENDING, SENT, FAILED, SKIPPED = "PENDING", "SENT", "FAILED", "SKIPPED"
TRAP_ACTIVE = "TRAMPA_ACTIVA"
PATTERN_VALIDATED = "PATRON_VALIDADO"
PATTERN_DECAYED = "PATRON_CADUCADO"
NO_HEARTBEAT = "EA_SIN_LATIDO"
RULES = {
    TRAP_ACTIVE: "Trampa activa",
    PATTERN_VALIDATED: "Trampa validada",
    PATTERN_DECAYED: "Trampa caducada",
    NO_HEARTBEAT: "EA sin latido",
}
MAX_TERMINALS = 200
MAX_TRADES = 200
MAX_PATTERNS = 100


@dataclass
class Candidate:
    """Una condición que está ocurriendo ahora."""

    rule: str
    severity: AlertSeverity
    key: str
    title: str
    lines: list[str]
    link: str | None = None
    once: bool = False  # operación o patrón: una sola vez, sin resolución
    bot_version_id: uuid.UUID | None = None
    trade_id: uuid.UUID | None = None
    hypothesis_id: uuid.UUID | None = None
    data: dict[str, Any] = field(default_factory=dict)


def dashboard_link(settings: Settings, path: str) -> str | None:
    base = (settings.public_url or "").strip().rstrip("/")
    return f"{base}{path}" if base else None


def market_closed(now: datetime, settings: Settings) -> bool:
    """Fin de semana del mercado en UTC: viernes desde weekend_start hasta domingo a
    weekend_end."""
    if not settings.alerts_skip_weekends:
        return False
    day, hour = now.astimezone(UTC).weekday(), now.astimezone(UTC).hour
    if day == 5:
        return True
    if day == 4:
        return hour >= settings.alerts_weekend_start_hour_utc
    if day == 6:
        return hour < settings.alerts_weekend_end_hour_utc
    return False


# Estado por clave ----------------------------------------------------------------------------


def _latest(session: Session, keys: list[str], event: str | None = None) -> dict[str, Alert]:
    if not keys:
        return {}
    stmt = select(Alert).where(Alert.dedup_key.in_(keys))
    if event:
        stmt = stmt.where(Alert.event == event)
    stmt = stmt.order_by(Alert.dedup_key, Alert.created_at.desc(), Alert.id).distinct(
        Alert.dedup_key
    )
    return {a.dedup_key: a for a in session.scalars(stmt)}


def _active_keys(session: Session, rule: str) -> dict[str, Alert]:
    """Claves de una regla cuya última fila es un DISPARO (condición activa)."""
    stmt = (
        select(Alert)
        .where(Alert.rule == rule)
        .order_by(Alert.dedup_key, Alert.created_at.desc(), Alert.id)
        .distinct(Alert.dedup_key)
    )
    return {a.dedup_key: a for a in session.scalars(stmt) if a.event == FIRE}


def _insert(session: Session, settings: Settings, c: Candidate, event: str, now: datetime) -> Alert:
    data = {**c.data, "titulo": c.title, "lineas": c.lines, "enlace": c.link}
    alert = Alert(
        id=uuid.uuid4(),
        rule=c.rule,
        severity=AlertSeverity.INFO if event == RESOLVE else c.severity,
        dedup_key=c.key,
        event=event,
        bot_version_id=c.bot_version_id,
        trade_id=c.trade_id,
        hypothesis_id=c.hypothesis_id,
        message=" · ".join([c.title, *c.lines]),
        data=data,
        created_at=now,
        delivery_status=PENDING if settings.telegram_enabled else SKIPPED,
        delivery_attempts=0,
    )
    session.add(alert)
    return alert


def apply(
    session: Session,
    settings: Settings,
    candidates: list[Candidate],
    now: datetime,
    *,
    resolvable: dict[str, Alert] | None = None,
    suppress_new: bool = False,
) -> Counter:
    """Inserta los disparos nuevos (deduplicados, con enfriamiento) y, para las claves de
    `resolvable` (activas) que ya no están en `candidates`, una fila RESUELTA."""
    stats: Counter = Counter()
    keys = [c.key for c in candidates]
    latest = _latest(session, keys)
    last_fire = _latest(session, keys, FIRE)
    cooldown = timedelta(minutes=settings.alerts_cooldown_minutes)
    for c in candidates:
        last = latest.get(c.key)
        if last is not None and (c.once or last.event == FIRE):
            stats["deduplicadas"] += 1
            continue
        fired = last_fire.get(c.key)
        if fired is not None and now - fired.created_at < cooldown:
            stats["en_enfriamiento"] += 1
            continue
        if suppress_new:
            stats["suprimidas"] += 1
            continue
        _insert(session, settings, c, FIRE, now)
        stats[c.rule] += 1
    current = set(keys)
    for key, active in (resolvable or {}).items():
        if key in current:
            continue
        data = active.data or {}
        resolved = Candidate(
            rule=active.rule,
            severity=AlertSeverity.INFO,
            key=key,
            title=f"Resuelta: {data.get('titulo') or RULES.get(active.rule, active.rule)}",
            lines=[data.get("resuelta") or "La condición ya no se cumple."],
            link=data.get("enlace"),
            bot_version_id=active.bot_version_id,
            data={k: v for k, v in data.items() if k not in ("titulo", "lineas", "enlace")},
        )
        _insert(session, settings, resolved, RESOLVE, now)
        stats[f"{active.rule}_resuelta"] += 1
    session.flush()
    return stats


# TRAMPA_ACTIVA -------------------------------------------------------------------------------


def _fmt(value: Any, digits: int = 2, signed: bool = False) -> str:
    if value is None:
        return "—"
    return f"{float(value):+.{digits}f}" if signed else f"{float(value):.{digits}f}"


def oos_stats(test: PatternTest | None) -> dict[str, Any] | None:
    if test is None:
        return None
    details = test.details or {}
    ci = details.get("expectancy_r_ic95") or [
        float(test.ci_low) if test.ci_low is not None else None,
        float(test.ci_high) if test.ci_high is not None else None,
    ]
    return {
        "n": test.n,
        "win_rate": details.get("win_rate", float(test.win_rate) if test.win_rate else None),
        "expectancy_r": details.get(
            "expectancy_r", float(test.expectancy) if test.expectancy is not None else None
        ),
        "expectancy_r_ic95": ci,
    }


def oos_text(stats: dict[str, Any] | None) -> str:
    if not stats:
        return "Fuera de muestra: sin datos"
    wr = stats.get("win_rate")
    ci = stats.get("expectancy_r_ic95") or [None, None]
    return (
        f"Fuera de muestra: n={stats['n']} · win rate "
        f"{'—' if wr is None else f'{wr * 100:.0f} %'} · expectativa "
        f"{_fmt(stats.get('expectancy_r'), signed=True)} R "
        f"[IC95 {_fmt(ci[0], signed=True)}, {_fmt(ci[1], signed=True)}]"
    )


def _oos_tests(session: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, PatternTest]:
    if not ids:
        return {}
    rows = session.scalars(
        select(PatternTest).where(
            PatternTest.hypothesis_id.in_(ids), PatternTest.segment == SplitSegment.OOS
        )
    )
    return {t.hypothesis_id: t for t in rows}


def _dna(
    session: Session, ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[dict[str, Any], dict[str, Any] | None]]:
    """Variables del DNA vigente y sus hechos previos a la entrada (si los tiene)."""
    if not ids:
        return {}
    rows = session.execute(
        text(
            "SELECT DISTINCT ON (trade_id) trade_id, features,"
            " inputs->'hechos_previos'->'hechos' FROM trade_dna"
            " WHERE trade_id = ANY(:ids) ORDER BY trade_id, dna_version DESC"
        ),
        {"ids": ids},
    )
    return {trade_id: (features, previous) for trade_id, features, previous in rows}


def entry_observation(
    trade: Trade, features: dict[str, Any], pre_entry: dict[str, Any] | None = None
) -> pt.Obs:
    """Las variables de la búsqueda conocidas AL ENTRAR: DNA, dirección, reentrada y los
    hechos previos a la entrada que el DNA guarda al abrir (EMAs M15/H1, volatilidad, spread;
    ver supervisor.analytics.pre_entry). Sin ellos (DNA antiguo) quedan desconocidos."""
    values: dict[str, Any] = {
        f.name: features.get(f.name) for f in FEATURES if f.searchable and f.available
    }
    values["direccion"] = trade.direction.value
    values["reentrada"] = trade.reentry_of_trade_id is not None
    values.update(fact_values(pre_entry))
    return pt.Obs(
        trade_id=str(trade.trade_id),
        close_time=trade.entry_time,
        r=0.0,
        win=False,
        values=values,
        facts=pre_entry,
    )


def trap_candidates(session: Session, settings: Settings, now: datetime) -> list[Candidate]:
    since = now - timedelta(minutes=settings.alerts_trap_window_minutes)
    traps = list(
        session.scalars(
            select(Hypothesis).where(
                Hypothesis.status == HypothesisStatus.VALIDATED,
                Hypothesis.kind == pt.TRAP,
                Hypothesis.bot_version_id.is_not(None),
            )
        )
    )
    if not traps:
        return []
    by_version: dict[uuid.UUID, list[Hypothesis]] = {}
    for h in traps:
        by_version.setdefault(h.bot_version_id, []).append(h)
    trades = list(
        session.scalars(
            select(Trade)
            .where(
                Trade.bot_version_id.in_(list(by_version)),
                Trade.entry_time >= since,
                Trade.entry_time <= now,
                func.coalesce(Trade.data_quality["importado"].astext, "false") != "true",
            )
            .order_by(Trade.entry_time.desc())
            .limit(MAX_TRADES)
        )
    )
    if not trades:
        return []
    dna = _dna(session, [t.trade_id for t in trades])
    tests = _oos_tests(session, [h.id for h in traps])
    names = dict(
        session.execute(
            select(BotVersion.id, Bot.name + " v" + BotVersion.version)
            .join(Bot, Bot.bot_id == BotVersion.bot_id)
            .where(BotVersion.id.in_(list(by_version)))
        ).all()
    )
    out = []
    for trade in trades:
        found = dna.get(trade.trade_id)
        if found is None:
            continue  # sin DNA todavía: se mira en la próxima pasada
        obs = entry_observation(trade, *found)
        for h in by_version[trade.bot_version_id]:
            if h.symbol and h.symbol != trade.symbol:
                continue
            try:
                condition = ps.condition_for(h.condition_spec)
            except LookupError:
                continue
            if not condition.matches(obs):
                continue
            stats = oos_stats(tests.get(h.id))
            bot = names.get(trade.bot_version_id, "bot")
            digits = trade.symbol_digits if trade.symbol_digits is not None else 5
            out.append(
                Candidate(
                    rule=TRAP_ACTIVE,
                    severity=AlertSeverity.CRITICAL,
                    key=f"{TRAP_ACTIVE}:{trade.trade_id}:{h.id}",
                    title=f"TRAMPA ACTIVA · {bot}",
                    lines=[
                        f"{trade.symbol} {trade.direction.value} a "
                        f"{float(trade.entry_price):.{digits}f} "
                        f"({trade.entry_time.astimezone(UTC):%Y-%m-%d %H:%M} UTC)",
                        f"Trampa validada: {h.statement}",
                        oos_text(stats),
                    ],
                    link=dashboard_link(settings, f"/dashboard/operaciones/{trade.trade_id}"),
                    once=True,
                    bot_version_id=trade.bot_version_id,
                    trade_id=trade.trade_id,
                    hypothesis_id=h.id,
                    data={
                        "bot": bot,
                        "simbolo": trade.symbol,
                        "direccion": trade.direction.value,
                        "precio_entrada": str(trade.entry_price),
                        "entrada": trade.entry_time.isoformat(),
                        "trampa": h.statement,
                        "fuera_de_muestra": stats,
                    },
                )
            )
    return out


# PATRON_VALIDADO / PATRON_CADUCADO -----------------------------------------------------------


def pattern_candidates(session: Session, settings: Settings, now: datetime) -> list[Candidate]:
    since = now - timedelta(days=settings.alerts_pattern_lookback_days)
    rows = session.execute(
        select(Hypothesis, Bot.name, BotVersion.version)
        .join(BotVersion, BotVersion.id == Hypothesis.bot_version_id)
        .join(Bot, Bot.bot_id == BotVersion.bot_id)
        .where(
            Hypothesis.kind == pt.TRAP,
            Hypothesis.status.in_((HypothesisStatus.VALIDATED, HypothesisStatus.DECAYED)),
            Hypothesis.updated_at >= since,
        )
        .order_by(Hypothesis.updated_at.desc())
        .limit(MAX_PATTERNS)
    ).all()
    tests = _oos_tests(session, [h.id for h, _, _ in rows])
    out = []
    for h, bot_name, version in rows:
        validated = h.status == HypothesisStatus.VALIDATED
        rule = PATTERN_VALIDATED if validated else PATTERN_DECAYED
        scope = f"{bot_name} v{version}" + (f" · {h.symbol}" if h.symbol else "")
        stats = oos_stats(tests.get(h.id))
        lines = [scope, h.statement, oos_text(stats)]
        if not validated and h.status_note:
            lines.append(h.status_note)
        out.append(
            Candidate(
                rule=rule,
                severity=AlertSeverity.WARNING if validated else AlertSeverity.INFO,
                key=f"{rule}:{h.id}",
                title="Nueva trampa validada" if validated else "Trampa caducada (forward)",
                lines=lines,
                link=dashboard_link(settings, f"/dashboard/trampas#p-{h.id}"),
                once=True,
                bot_version_id=h.bot_version_id,
                hypothesis_id=h.id,
                data={"trampa": h.statement, "alcance": scope, "fuera_de_muestra": stats},
            )
        )
    return out


# EA_SIN_LATIDO -------------------------------------------------------------------------------


def heartbeat_candidates(session: Session, settings: Settings, now: datetime) -> list[Candidate]:
    limit = timedelta(minutes=settings.alerts_heartbeat_minutes)
    # Último latido de cada terminal por la PK (terminal_id, ts): subconsulta lateral.
    last = (
        select(Heartbeat.ts)
        .where(Heartbeat.terminal_id == Terminal.id)
        .order_by(Heartbeat.ts.desc())
        .limit(1)
        .lateral("hb")
    )
    rows = session.execute(
        select(Terminal.id, Terminal.name, Account.login, Account.server, last.c.ts)
        .join(Account, Account.id == Terminal.account_id)
        .join(last, text("true"))
        .where(Account.active.is_(True))
        .order_by(Terminal.name)
        .limit(MAX_TERMINALS)
    ).all()
    out = []
    for terminal_id, name, login, server, ts in rows:
        if now - ts < limit:
            continue
        minutes = int((now - ts).total_seconds() // 60)
        out.append(
            Candidate(
                rule=NO_HEARTBEAT,
                severity=AlertSeverity.WARNING,
                key=f"{NO_HEARTBEAT}:{terminal_id}",
                title=f"EA sin latido · {name}",
                lines=[
                    f"Cuenta {login} @ {server}",
                    f"Último latido hace {minutes} min ({ts.astimezone(UTC):%Y-%m-%d %H:%M} UTC)",
                    "Revisa que MT5 y el EA Monitor sigan abiertos y conectados.",
                ],
                link=dashboard_link(settings, "/dashboard/sistema"),
                data={
                    "terminal": name,
                    "ultimo_latido": ts.isoformat(),
                    "resuelta": f"El terminal {name} vuelve a enviar latidos.",
                },
            )
        )
    return out


# Pasada del worker ---------------------------------------------------------------------------


def evaluate(session: Session, settings: Settings, now: datetime | None = None) -> Counter:
    now = now or datetime.now(UTC)
    stats: Counter = Counter()
    stats.update(apply(session, settings, trap_candidates(session, settings, now), now))
    stats.update(apply(session, settings, pattern_candidates(session, settings, now), now))
    stats.update(
        apply(
            session,
            settings,
            heartbeat_candidates(session, settings, now),
            now,
            resolvable=_active_keys(session, NO_HEARTBEAT),
            suppress_new=market_closed(now, settings),
        )
    )
    return stats


def telegram_text(alert: Alert) -> str:
    data = alert.data or {}
    return tg.format_alert(
        severity=alert.severity.value,
        event=alert.event,
        title=data.get("titulo") or RULES.get(alert.rule, alert.rule),
        lines=list(data.get("lineas") or [alert.message]),
        link=data.get("enlace"),
    )


def deliver_pending(
    session: Session,
    settings: Settings,
    client: tg.TelegramClient | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Counter:
    """Envía como mucho telegram_batch_size alertas PENDING (las más antiguas primero),
    separadas telegram_min_interval_seconds. Cada fallo suma un intento y programa el
    siguiente con espera exponencial (o la que pida Telegram); al agotar los intentos, o si el
    error es permanente, queda FAILED."""
    stats: Counter = Counter()
    if not settings.telegram_enabled:
        return stats
    now = now or datetime.now(UTC)
    client = client or tg.TelegramClient.from_settings(settings)
    pending = list(
        session.scalars(
            select(Alert)
            .where(
                Alert.delivery_status == PENDING,
                (Alert.next_delivery_at.is_(None)) | (Alert.next_delivery_at <= now),
            )
            .order_by(Alert.created_at, Alert.id)
            .limit(settings.telegram_batch_size)
            .with_for_update(skip_locked=True)
        )
    )
    for i, alert in enumerate(pending):
        if i:
            sleep(settings.telegram_min_interval_seconds)
        alert.delivery_attempts += 1
        try:
            client.send_message(telegram_text(alert))
        except tg.TelegramError as exc:
            error = client.redact(exc.message)[:500]
            alert.delivery_error = error
            if exc.permanent or alert.delivery_attempts >= settings.telegram_max_attempts:
                alert.delivery_status = FAILED
                alert.next_delivery_at = None
                stats["fallidas"] += 1
            else:
                wait = exc.retry_after or settings.telegram_retry_base_seconds * 2 ** (
                    alert.delivery_attempts - 1
                )
                alert.next_delivery_at = now + timedelta(seconds=float(wait))
                stats["reintentos"] += 1
            log.warning(
                "alerta no enviada a Telegram",
                extra={
                    "alert_id": str(alert.id),
                    "intento": alert.delivery_attempts,
                    "error": error,
                },
            )
            if exc.retry_after:
                break  # Telegram pide esperar: el resto, en la próxima pasada
            continue
        alert.delivery_status = SENT
        alert.sent_at = datetime.now(UTC)
        alert.delivery_error = None
        alert.next_delivery_at = None
        stats["enviadas"] += 1
    session.flush()
    return stats


def alerts_pass(session_factory: sessionmaker[Session], settings: Settings) -> Counter:
    """Trabajo periódico del worker: evaluar (una transacción) y entregar (otra)."""
    stats: Counter = Counter()
    if not settings.alerts_enabled:
        return stats
    with session_factory() as session, session.begin():
        stats.update(evaluate(session, settings))
    if settings.telegram_enabled:
        try:
            with session_factory() as session, session.begin():
                stats.update(deliver_pending(session, settings))
        except Exception:
            log.exception("entrega de alertas fallida")
            stats["error_entrega"] += 1
    fired = {k: v for k, v in stats.items() if k not in ("deduplicadas",) and v}
    if fired:
        log.info("repaso de alertas", extra=fired)
    return stats
