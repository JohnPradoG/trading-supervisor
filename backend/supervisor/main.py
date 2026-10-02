"""Aplicación FastAPI del Trading Supervisor."""

import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from supervisor import __version__, dashboard
from supervisor.api import admin, analysis, ingest, trades
from supervisor.config import Settings, get_settings
from supervisor.log_config import configure_logging, request_id_var
from supervisor.services.errors import ServiceError

log = logging.getLogger("supervisor.api")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    engine = create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        pool_pre_ping=True,
        echo=settings.db_echo,
        connect_args={"options": "-c timezone=UTC"},
    )
    app = FastAPI(
        title="Trading Supervisor",
        version=__version__,
        description="Supervisor de solo lectura para bots de MT5.",
        # La documentación interactiva no se expone en producción.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("error no controlado", extra={"path": request.url.path})
            response = JSONResponse({"detail": "error interno"}, status_code=500)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = rid
        log.info(
            "petición",
            extra={
                "request_id": rid,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "ms": round((time.perf_counter() - start) * 1000, 1),
                "client": request.client.host if request.client else None,
            },
        )
        return response

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse({"detail": exc.message}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()[:20]]
        log.warning("petición inválida", extra={"path": request.url.path, "errors": errors})
        return JSONResponse({"detail": errors}, status_code=422)

    @app.get("/health", tags=["salud"])
    def health() -> JSONResponse:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            log.exception("base de datos no disponible")
            return JSONResponse({"status": "degraded", "database": "down"}, status_code=503)
        return JSONResponse({"status": "ok", "database": "up", "version": __version__})

    app.include_router(ingest.router)
    app.include_router(admin.router)
    app.include_router(trades.router)
    app.include_router(analysis.router)
    dashboard.install(app)
    return app
