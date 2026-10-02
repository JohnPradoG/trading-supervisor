"""Grupo B: bots, versiones inmutables y despliegues.

- Bot: el concepto (nombre, estrategia, estado). Nunca se borra, para evitar sesgo de
  supervivencia al comparar.
- BotVersion: una versión concreta. Inmutable: cambiar un parámetro relevante = versión nueva.
- Deployment: una versión corriendo en una cuenta y símbolo con un magic number, entre dos
  fechas. Es lo que permite asignar cada operación de MT5 a su bot.
"""

import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from supervisor.models.base import Base, CreatedAt, Json, UTCDateTime, UUIDPk, str_enum
from supervisor.models.enums import BotStatus

SEMVER_REGEX = r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$"


class Bot(Base):
    __tablename__ = "bots"

    bot_id: Mapped[UUIDPk]
    name: Mapped[str] = mapped_column(String(128), unique=True)
    strategy: Mapped[str] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[BotStatus] = mapped_column(
        str_enum(BotStatus), server_default=BotStatus.ACTIVE.value
    )
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[CreatedAt]

    versions: Mapped[list["BotVersion"]] = relationship(back_populates="bot")


class BotVersion(Base):
    __tablename__ = "bot_versions"
    __table_args__ = (
        UniqueConstraint("bot_id", "version"),
        CheckConstraint(f"version ~ '{SEMVER_REGEX}'", name="semver"),
    )

    id: Mapped[UUIDPk]
    bot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bots.bot_id"), index=True)
    version: Mapped[str] = mapped_column(String(32))
    main_timeframe: Mapped[str] = mapped_column(String(8))
    params: Mapped[Json]
    # sha256 de los parámetros normalizados y del .ex5/.mq5, si se conocen.
    params_hash: Mapped[str | None] = mapped_column(String(64))
    source_hash: Mapped[str | None] = mapped_column(String(64))
    changelog: Mapped[str | None] = mapped_column(Text)
    released_at: Mapped[UTCDateTime]
    created_at: Mapped[CreatedAt]

    bot: Mapped[Bot] = relationship(back_populates="versions")


class Deployment(Base):
    __tablename__ = "deployments"
    __table_args__ = (
        CheckConstraint("ended_at IS NULL OR ended_at > started_at", name="dates"),
        # Un magic solo puede tener un despliegue activo por cuenta y símbolo; si no, la
        # asignación de operaciones a bots sería ambigua.
        Index(
            "uq_deployments_active_magic",
            "account_id",
            "magic_number",
            "symbol",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
    )

    id: Mapped[UUIDPk]
    bot_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bot_versions.id"), index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(64))
    magic_number: Mapped[int] = mapped_column(BigInteger)
    started_at: Mapped[UTCDateTime]
    ended_at: Mapped[UTCDateTime | None]
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[CreatedAt]

    bot_version: Mapped[BotVersion] = relationship()
