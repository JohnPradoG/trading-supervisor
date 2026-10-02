"""Configuración leída exclusivamente desde variables de entorno (o un archivo .env local)."""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SUPERVISOR_", extra="ignore")

    # postgresql+psycopg://usuario:clave@host:5432/base
    database_url: str = Field(..., description="URL SQLAlchemy de PostgreSQL")
    db_pool_size: int = 5
    db_echo: bool = False

    # Token para los endpoints de administración (registro de bots, versiones, despliegues).
    # Mínimo 32 caracteres; se genera con `openssl rand -hex 32`.
    admin_token: SecretStr = Field(..., min_length=32)

    log_level: str = "INFO"
    # Máximo de eventos por lote y de velas por envío desde el EA.
    ingest_max_batch: int = 500
    ingest_max_bars: int = 5000
    # Tolerancia para relojes adelantados: un evento con hora UTC más allá de esto se rechaza.
    max_future_skew_seconds: int = 300


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
