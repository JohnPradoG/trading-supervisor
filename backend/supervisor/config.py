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
    # Eventos de una importación de historial (origin HISTORY_IMPORT) por lote, como mucho. Los
    # del EA en vivo se atienden siempre antes; la importación avanza con lo que sobra del lote.
    worker_import_batch_size: int = Field(default=20, ge=1, le=1000)
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

    # Análisis y estadísticas (fase 6).
    # Breakeven: |neto| <= esta fracción del riesgo inicial (sin riesgo: |neto| <= |comisión|).
    breakeven_r_fraction: float = Field(default=0.05, ge=0, le=1)
    # Muestra mínima para Sharpe/Sortino y para no avisar de muestra pequeña.
    stats_min_sample: int = Field(default=30, ge=2)
    # Máximo de operaciones cerradas que carga una consulta de estadísticas; si el filtro
    # abarca más se rechaza pidiendo acotarlo (protege la RAM del VPS).
    stats_max_trades: int = Field(default=50000, ge=100)
    # EMAs de M15/H1 agregadas desde M1: se exigen warmup_factor x periodo velas cerradas.
    analysis_ema_warmup_factor: float = Field(default=2.0, ge=1)
    # Cobertura mínima de velas M1 dentro de las velas agregadas usadas (0-1).
    analysis_min_bar_coverage: float = Field(default=0.8, gt=0, le=1)
    # Percentil de volatilidad: ATR(14) H1 al entrar frente a los N días anteriores.
    analysis_volatility_lookback_days: int = Field(default=20, ge=2)
    analysis_volatility_min_samples: int = Field(default=100, ge=10)
    # Noticias: ventana alrededor de la entrada (minutos) cuando haya fuente de calendario.
    analysis_news_window_minutes: int = Field(default=60, ge=1)
    # Operaciones re-analizadas como máximo en cada repaso del worker.
    analysis_max_per_pass: int = Field(default=200, ge=1)

    # Trading DNA (fase 8). Días de velas M1 que puede consultar el DNA de una operación
    # (ventana de H1, de la que salen H4 y D1); acota la consulta en el VPS.
    dna_history_days: int = Field(default=35, ge=7, le=120)
    # Operaciones cuyo DNA se recalcula como máximo en cada repaso del worker.
    dna_max_per_pass: int = Field(default=100, ge=1)

    # Detección de patrones y trampas (fase 9). Splits cronológicos por hora de cierre:
    # entrenamiento / validación / fuera de muestra (el resto). Nunca se barajan.
    patterns_train_fraction: float = Field(default=0.6, gt=0.2, lt=0.9)
    patterns_validation_fraction: float = Field(default=0.2, gt=0.05, lt=0.5)
    # Operaciones cerradas con riesgo conocido necesarias para crear el primer split.
    patterns_min_trades: int = Field(default=100, ge=20)
    # Muestra mínima de la condición (y del complemento en entrenamiento) en cada tramo.
    patterns_min_n_train: int = Field(default=30, ge=5)
    patterns_min_n_validation: int = Field(default=15, ge=3)
    patterns_min_n_oos: int = Field(default=15, ge=3)
    patterns_min_n_forward: int = Field(default=20, ge=3)
    # FDR de Benjamini-Hochberg sobre todas las candidatas probadas en una ejecución.
    patterns_fdr_q: float = Field(default=0.05, gt=0, lt=0.5)
    # Tramos de cuantiles (calculados solo con entrenamiento) para variables numéricas.
    patterns_quantile_bins: int = Field(default=4, ge=2, le=10)
    # Tope de pares de condiciones (AND) por ejecución (CPU del VPS).
    patterns_max_pairs: int = Field(default=3000, ge=0)
    # Operaciones cerradas nuevas (después del último split) para repetir la búsqueda.
    patterns_new_trades: int = Field(default=20, ge=1)
    # Cada cuánto el worker repasa los patrones (por defecto, una vez al día) y cuántas
    # versiones de bot como mucho en cada pasada.
    patterns_interval_seconds: int = Field(default=86400, ge=60)
    patterns_max_scopes_per_pass: int = Field(default=5, ge=1)

    # Laboratorio (fase 10). Tamaño máximo de un archivo del Strategy Tester (Caddy deja pasar
    # hasta 10 MB solo en la ruta de subida de backtests; el resto sigue en 2 MB).
    lab_upload_max_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=10 * 1024 * 1024)
    # Remuestreos (bootstrap con semilla fija) para los intervalos del profit factor y del
    # drawdown máximo. 0 los desactiva.
    lab_bootstrap_samples: int = Field(default=500, ge=0, le=5000)
    # Puntos por curva de equity en las comparaciones.
    lab_curve_points: int = Field(default=400, ge=10, le=5000)

    # Dashboard web (/dashboard). La sesión es una cookie firmada con HMAC-SHA256 con una clave
    # derivada de admin_token: rotar el token invalida todas las sesiones.
    dashboard_session_hours: float = Field(default=12, gt=0, le=24 * 30)
    # Cookie solo por HTTPS. Desactivar únicamente en pruebas o desarrollo local sin TLS.
    dashboard_cookie_secure: bool = True
    # Límite de intentos de login fallidos por IP y ventana (en memoria, por proceso).
    dashboard_login_max_failures: int = Field(default=5, ge=1)
    dashboard_login_window_seconds: int = Field(default=900, ge=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
