"""Esquemas de registro y consulta de bots, versiones y despliegues."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from supervisor.models.bots import SEMVER_REGEX
from supervisor.models.enums import BotStatus
from supervisor.schemas.ingest import UtcDatetime

TIMEFRAMES = r"^(M1|M2|M3|M4|M5|M6|M10|M12|M15|M20|M30|H1|H2|H3|H4|H6|H8|H12|D1|W1|MN1)$"


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class BotCreate(_In):
    name: str = Field(min_length=1, max_length=128)
    strategy: str = Field(min_length=1, max_length=256)
    description: str | None = None
    notes: str | None = None


class BotUpdate(_In):
    """Lo único mutable de un bot: estado, descripción y notas. El nombre identifica al bot."""

    status: BotStatus | None = None
    description: str | None = None
    notes: str | None = None


class BotOut(_Out):
    bot_id: uuid.UUID
    name: str
    strategy: str
    description: str | None
    status: BotStatus
    notes: str | None
    created_at: datetime


class VersionCreate(_In):
    version: str = Field(pattern=SEMVER_REGEX, max_length=32)
    main_timeframe: str = Field(pattern=TIMEFRAMES)
    params: dict[str, Any] = Field(default_factory=dict)
    source_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    changelog: str | None = None
    released_at: UtcDatetime


class VersionOut(_Out):
    id: uuid.UUID
    bot_id: uuid.UUID
    version: str
    main_timeframe: str
    params: dict[str, Any]
    params_hash: str | None
    source_hash: str | None
    changelog: str | None
    released_at: datetime
    created_at: datetime


class DeploymentCreate(_In):
    bot_version_id: uuid.UUID
    account_login: int = Field(gt=0)
    account_server: str = Field(min_length=1, max_length=128)
    symbol: str = Field(min_length=1, max_length=64)
    magic_number: int = Field(ge=1)
    started_at: UtcDatetime
    notes: str | None = None


class DeploymentEnd(_In):
    ended_at: UtcDatetime


class DeploymentOut(_Out):
    id: uuid.UUID
    bot_version_id: uuid.UUID
    account_id: uuid.UUID
    symbol: str
    magic_number: int
    started_at: datetime
    ended_at: datetime | None
    notes: str | None
    created_at: datetime


class BotDetail(BotOut):
    versions: list[VersionOut]
    deployments: list[DeploymentOut]


class RawEventOut(_Out):
    id: int
    terminal_id: uuid.UUID
    idempotency_key: str
    event_type: str
    status: str
    received_at: datetime
    processed_at: datetime | None
    error: str | None
