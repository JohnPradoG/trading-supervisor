"""Base declarativa, convenciones de nombres y tipos compartidos por todos los modelos."""

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from sqlalchemy import BigInteger, DateTime, Enum, MetaData, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, mapped_column

# Nombres deterministas para índices y restricciones: Alembic los necesita para migrar sin
# ambigüedad, y los errores de unicidad se pueden reconocer por nombre en el código.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


# Tipos anotados reutilizables ------------------------------------------------------------
UUIDPk = Annotated[
    uuid.UUID,
    mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
]
UTCDateTime = Annotated[datetime, mapped_column(DateTime(timezone=True))]
CreatedAt = Annotated[
    datetime,
    mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False),
]
Ticket = Annotated[int, mapped_column(BigInteger)]
# Precios: MT5 trabaja con double; 10 decimales cubren cualquier símbolo sin redondear.
Price = Annotated[Decimal, mapped_column(Numeric(24, 10))]
Volume = Annotated[Decimal, mapped_column(Numeric(16, 4))]
Money = Annotated[Decimal, mapped_column(Numeric(20, 4))]
Points = Annotated[Decimal, mapped_column(Numeric(20, 4))]
ShortStr = Annotated[str, mapped_column(String(64))]
Json = Annotated[dict[str, Any], mapped_column(JSONB, nullable=False, server_default="{}")]


def str_enum(enum_cls: type[enum.Enum]) -> Enum:
    """Enum guardado como VARCHAR + CHECK (no ENUM nativo), para que añadir valores sea una
    migración simple y no un ALTER TYPE."""
    return Enum(
        enum_cls,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )
