"""Proceso del worker: `supervisor-worker` (en Docker, el servicio `worker`).

Consulta raw_events pendientes cada worker_poll_interval_seconds, los convierte en
operaciones y cada worker_maintenance_interval_seconds hace el repaso de operaciones
recientes. Cada patterns_interval_seconds (un día) repasa la búsqueda de patrones de las
versiones con operaciones (solo repite la búsqueda con patterns_new_trades cerradas nuevas).
Con SIGTERM o SIGINT termina el lote en curso y sale limpio.
"""

import logging
import signal
import threading
import time
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from supervisor import __version__
from supervisor.analytics.pattern_search import patterns_pass
from supervisor.config import Settings, get_settings
from supervisor.log_config import configure_logging
from supervisor.worker.maintenance import maintenance_pass
from supervisor.worker.processor import TRANSIENT_ERRORS, process_batch

log = logging.getLogger("supervisor.worker")


def _touch(path: str) -> None:
    try:
        Path(path).touch()
    except OSError:
        log.warning("no se pudo escribir el archivo de salud", extra={"path": path})


def run_forever(
    session_factory: sessionmaker[Session], settings: Settings, stop: threading.Event
) -> None:
    last_maintenance = float("-inf")
    last_patterns = float("-inf")
    failures = 0
    while not stop.is_set():
        try:
            stats = process_batch(session_factory, settings)
            processed = {k: v for k, v in stats.items() if k != "selected" and v}
            if processed:
                log.info("lote procesado", extra=processed)
            if time.monotonic() - last_maintenance >= settings.worker_maintenance_interval_seconds:
                maintenance_pass(session_factory, settings)
                last_maintenance = time.monotonic()
            if time.monotonic() - last_patterns >= settings.patterns_interval_seconds:
                patterns_pass(session_factory, settings)
                last_patterns = time.monotonic()
            failures = 0
        except TRANSIENT_ERRORS:
            failures += 1
            wait = min(60.0, settings.worker_poll_interval_seconds * 2**failures)
            log.exception("base de datos no disponible", extra={"reintento_en_s": wait})
            stop.wait(wait)
            continue
        except Exception:
            log.exception("error inesperado en el worker")
            stop.wait(settings.worker_poll_interval_seconds)
            continue
        _touch(settings.worker_health_file)
        if stats["selected"] < settings.worker_batch_size:
            stop.wait(settings.worker_poll_interval_seconds)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_engine(
        settings.database_url,
        pool_size=2,
        pool_pre_ping=True,
        echo=settings.db_echo,
        connect_args={"options": "-c timezone=UTC"},
    )
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    stop = threading.Event()

    def _on_signal(signum: int, _frame) -> None:
        log.info("señal recibida: se termina tras el lote en curso", extra={"signal": signum})
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    log.info(
        "worker iniciado",
        extra={
            "version": __version__,
            "poll_s": settings.worker_poll_interval_seconds,
            "batch": settings.worker_batch_size,
        },
    )
    try:
        run_forever(session_factory, settings, stop)
    finally:
        engine.dispose()
        log.info("worker detenido")


if __name__ == "__main__":
    main()
