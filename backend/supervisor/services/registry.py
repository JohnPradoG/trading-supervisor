"""Registro de bots, versiones (inmutables) y despliegues."""

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from supervisor.models import Account, Bot, BotVersion, Deployment, RawEvent
from supervisor.schemas.registry import (
    BotCreate,
    BotUpdate,
    DeploymentCreate,
    DeploymentEnd,
    VersionCreate,
)
from supervisor.services.errors import Conflict, NotFound, ServiceError


def params_hash(params: dict[str, Any]) -> str:
    """Hash estable de los parámetros: mismo contenido, mismo hash, sin importar el orden."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _flush_or_conflict(session: Session, message: str) -> None:
    """Hace flush dentro de un savepoint para que un choque de unicidad no anule la petición."""
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError as exc:
        raise Conflict(message) from exc


def create_bot(session: Session, data: BotCreate) -> Bot:
    bot = Bot(**data.model_dump())
    session.add(bot)
    _flush_or_conflict(session, f"ya existe un bot llamado '{data.name}'")
    return bot


def get_bot(session: Session, bot_id: uuid.UUID) -> Bot:
    bot = session.get(Bot, bot_id)
    if bot is None:
        raise NotFound("bot no encontrado")
    return bot


def list_bots(session: Session) -> list[Bot]:
    return list(session.scalars(select(Bot).order_by(Bot.name)))


def update_bot(session: Session, bot_id: uuid.UUID, data: BotUpdate) -> Bot:
    bot = get_bot(session, bot_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        if field == "status" and value is None:
            continue
        setattr(bot, field, value)
    session.flush()
    return bot


def create_version(session: Session, bot_id: uuid.UUID, data: VersionCreate) -> BotVersion:
    get_bot(session, bot_id)
    version = BotVersion(
        bot_id=bot_id,
        version=data.version,
        main_timeframe=data.main_timeframe,
        params=data.params,
        params_hash=params_hash(data.params),
        source_hash=data.source_hash,
        changelog=data.changelog,
        released_at=data.released_at,
    )
    session.add(version)
    _flush_or_conflict(
        session,
        f"la versión {data.version} ya existe y es inmutable; registra una versión nueva",
    )
    return version


def list_versions(session: Session, bot_id: uuid.UUID) -> list[BotVersion]:
    return list(
        session.scalars(
            select(BotVersion)
            .where(BotVersion.bot_id == bot_id)
            .order_by(BotVersion.released_at, BotVersion.created_at)
        )
    )


def list_deployments(session: Session, bot_id: uuid.UUID) -> list[Deployment]:
    return list(
        session.scalars(
            select(Deployment)
            .join(BotVersion, Deployment.bot_version_id == BotVersion.id)
            .where(BotVersion.bot_id == bot_id)
            .order_by(Deployment.started_at)
        )
    )


def create_deployment(session: Session, data: DeploymentCreate) -> Deployment:
    if session.get(BotVersion, data.bot_version_id) is None:
        raise NotFound("versión de bot no encontrada")
    account = session.scalar(
        select(Account).where(
            Account.server == data.account_server, Account.login == data.account_login
        )
    )
    if account is None:
        raise NotFound(
            f"cuenta {data.account_login} en {data.account_server} no registrada; "
            "créala con la CLI (create-account)"
        )
    deployment = Deployment(
        bot_version_id=data.bot_version_id,
        account_id=account.id,
        symbol=data.symbol,
        magic_number=data.magic_number,
        started_at=data.started_at,
        notes=data.notes,
    )
    session.add(deployment)
    _flush_or_conflict(
        session,
        f"el magic {data.magic_number} ya tiene un despliegue activo en {data.symbol} para esa "
        "cuenta; termina el anterior antes de crear uno nuevo",
    )
    return deployment


def end_deployment(session: Session, deployment_id: uuid.UUID, data: DeploymentEnd) -> Deployment:
    deployment = session.get(Deployment, deployment_id)
    if deployment is None:
        raise NotFound("despliegue no encontrado")
    if deployment.ended_at is not None:
        raise Conflict("el despliegue ya estaba terminado")
    if data.ended_at <= deployment.started_at:
        raise ServiceError("ended_at debe ser posterior a started_at")
    deployment.ended_at = data.ended_at
    session.flush()
    return deployment


def list_raw_events(session: Session, status: str | None, limit: int) -> list[RawEvent]:
    stmt = select(RawEvent).order_by(RawEvent.id.desc()).limit(limit)
    if status:
        stmt = stmt.where(RawEvent.status == status)
    return list(session.scalars(stmt))
