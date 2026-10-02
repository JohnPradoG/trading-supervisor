"""Grupo A: quién opera. Brokers, cuentas, terminales, credenciales de API y usuarios.

Nunca se guardan contraseñas de MT5 ni del broker. Las API keys y contraseñas del dashboard
se guardan solo como hash.
"""

import uuid

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from supervisor.models.base import Base, CreatedAt, UTCDateTime, UUIDPk, str_enum
from supervisor.models.enums import AccountType, MarginMode


class Broker(Base):
    __tablename__ = "brokers"

    id: Mapped[UUIDPk]
    name: Mapped[str] = mapped_column(String(128), unique=True)
    # Cómo se obtiene el desfase UTC del servidor: "EA_REPORTED" (TimeGMT del EA) o fijo.
    server_utc_offset_policy: Mapped[str] = mapped_column(String(32), server_default="EA_REPORTED")
    notes: Mapped[str | None] = mapped_column(String(2000))
    created_at: Mapped[CreatedAt]

    accounts: Mapped[list["Account"]] = relationship(back_populates="broker")


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("server", "login"),)

    id: Mapped[UUIDPk]
    broker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("brokers.id"))
    login: Mapped[int] = mapped_column(BigInteger)
    server: Mapped[str] = mapped_column(String(128))
    currency: Mapped[str] = mapped_column(String(8))
    account_type: Mapped[AccountType] = mapped_column(str_enum(AccountType))
    margin_mode: Mapped[MarginMode] = mapped_column(str_enum(MarginMode))
    leverage: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    notes: Mapped[str | None] = mapped_column(String(2000))
    created_at: Mapped[CreatedAt]

    broker: Mapped[Broker] = relationship(back_populates="accounts")


class Terminal(Base):
    """Una instalación de MT5 con el EA Monitor. Es la unidad que se autentica contra la API."""

    __tablename__ = "terminals"

    id: Mapped[UUIDPk]
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    host_description: Mapped[str | None] = mapped_column(String(256))
    last_seen_at: Mapped[UTCDateTime | None]
    created_at: Mapped[CreatedAt]


class ApiClient(Base):
    """Credencial de un terminal. La key completa se muestra una sola vez al crearla."""

    __tablename__ = "api_clients"

    id: Mapped[UUIDPk]
    terminal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("terminals.id"), index=True)
    # Primeros caracteres de la key, para identificarla en logs sin revelarla.
    key_prefix: Mapped[str] = mapped_column(String(16), unique=True)
    key_hash: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[CreatedAt]
    revoked_at: Mapped[UTCDateTime | None]


class User(Base):
    """Usuario del dashboard. password_hash es argon2."""

    __tablename__ = "users"

    id: Mapped[UUIDPk]
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[CreatedAt]
