"""Consulta y reconocimiento de alertas (dashboard, API y CLI).

Reconocer una alerta solo pone acknowledged_at (una vez; el trigger de la base impide
cambiarlo después). No borra ni modifica nada más.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from supervisor.alerts import engine
from supervisor.models import Alert
from supervisor.models.enums import AlertSeverity
from supervisor.services.errors import NotFound

KINDS = tuple(engine.RULES)
SEVERITY_TEXT = {"CRITICAL": "crítica", "WARNING": "aviso", "INFO": "info"}
DELIVERY_TEXT = {
    engine.PENDING: "pendiente",
    engine.SENT: "enviada",
    engine.FAILED: "fallida",
    engine.SKIPPED: "sin Telegram",
}


def alert_view(a: Alert) -> dict[str, Any]:
    data = a.data or {}
    return {
        "id": a.id,
        "rule": a.rule,
        "rule_text": engine.RULES.get(a.rule, a.rule),
        "severity": a.severity.value,
        "severity_text": SEVERITY_TEXT.get(a.severity.value, a.severity.value),
        "event": a.event,
        "title": data.get("titulo") or engine.RULES.get(a.rule, a.rule),
        "lines": list(data.get("lineas") or [a.message]),
        "message": a.message,
        "data": data,
        "trade_id": a.trade_id,
        "hypothesis_id": a.hypothesis_id,
        "bot_version_id": a.bot_version_id,
        "created_at": a.created_at,
        "acknowledged_at": a.acknowledged_at,
        "delivery_status": a.delivery_status,
        "delivery_text": DELIVERY_TEXT.get(a.delivery_status, a.delivery_status),
        "delivery_attempts": a.delivery_attempts,
        "delivery_error": a.delivery_error,
        "sent_at": a.sent_at,
    }


def list_alerts(
    session: Session,
    *,
    rule: str | None = None,
    severity: AlertSeverity | None = None,
    unacknowledged: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    stmt = select(Alert)
    if rule:
        stmt = stmt.where(Alert.rule == rule)
    if severity is not None:
        stmt = stmt.where(Alert.severity == severity)
    if unacknowledged:
        stmt = stmt.where(Alert.acknowledged_at.is_(None), Alert.event == engine.FIRE)
    stmt = stmt.order_by(Alert.created_at.desc(), Alert.id).limit(limit)
    return [alert_view(a) for a in session.scalars(stmt)]


def unacknowledged_count(session: Session) -> int:
    return (
        session.scalar(
            select(func.count()).where(Alert.acknowledged_at.is_(None), Alert.event == engine.FIRE)
        )
        or 0
    )


def recent_traps(session: Session, limit: int = 5) -> list[dict[str, Any]]:
    return list_alerts(session, rule=engine.TRAP_ACTIVE, limit=limit)


def acknowledge(session: Session, alert_id: uuid.UUID) -> dict[str, Any]:
    alert = session.get(Alert, alert_id)
    if alert is None:
        raise NotFound("alerta no encontrada")
    session.execute(
        update(Alert)
        .where(Alert.id == alert_id, Alert.acknowledged_at.is_(None))
        .values(acknowledged_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    session.flush()
    session.refresh(alert)
    return alert_view(alert)
