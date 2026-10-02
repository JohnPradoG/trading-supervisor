"""Configuración leída exclusivamente desde variables de entorno (o un archivo .env local)."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SUPERVISOR_", extra="ignore")

    # postgresql+psycopg://usuario:clave@host:5432/base
    database_url: str = Field(..., description="URL SQLAlchemy de PostgreSQL")
    db_pool_size: int = 5
    db_echo: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
