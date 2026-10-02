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

    # Worker de operaciones (supervisor-worker).
    # Segundos de espera entre consultas cuando no hay eventos pendientes.
    worker_poll_interval_seconds: float = Field(default=2.0, gt=0)
    # Eventos por lote; cada uno se procesa en su propio savepoint.
    worker_batch_size: int = Field(default=50, ge=1, le=1000)
    # Primer reintento de un evento diferido; se duplica en cada intento hasta el máximo.
    worker_retry_base_seconds: int = Field(default=30, ge=1)
    worker_retry_max_seconds: int = Field(default=900, ge=1)
    # Cuánto se espera (desde que llegó) a la apertura de un cierre o modificación huérfanos
    # antes de marcarlo FAILED/IGNORED. Si la apertura llega después, se reactiva solo.
    worker_defer_max_seconds: int = Field(default=21600, ge=0)
    # Cada cuánto se repasan excursiones incompletas, despliegues sin asignar y balances.
    worker_maintenance_interval_seconds: int = Field(default=300, ge=10)
    # Ventana del repaso: operaciones cerradas en los últimos N días.
    worker_recheck_days: int = Field(default=7, ge=1)
    # Archivo que el worker toca en cada vuelta; lo usa el healthcheck de Docker.
    worker_health_file: str = "/tmp/supervisor-worker.alive"
    # Reentrada: apertura en la misma cuenta, símbolo y magic tras un cierre por SL.
    reentry_window_minutes: int = Field(default=30, ge=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
