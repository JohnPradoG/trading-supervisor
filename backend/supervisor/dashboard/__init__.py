"""Dashboard web de solo lectura (Fase 7): Jinja2 + HTMX + Chart.js servidos por la propia API.

htmx y Chart.js van incluidos en static/vendor (versiones fijas): no se carga nada de CDNs.
"""

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from supervisor.dashboard import auth
from supervisor.dashboard.routes import BASE_DIR, LoginRequired, login_required_response, router

# Sin scripts ni estilos en línea: todo el JS está en /dashboard/static y los datos de los
# gráficos llegan por atributos data-* y endpoints JSON del propio dashboard.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
    "base-uri 'none'"
)


def install(app: FastAPI) -> None:
    """Monta el dashboard en /dashboard. Las rutas /v1 no cambian."""
    settings = app.state.settings
    app.state.login_limiter = auth.LoginRateLimiter(
        settings.dashboard_login_max_failures, settings.dashboard_login_window_seconds
    )

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _: LoginRequired):
        return login_required_response(request)

    @app.middleware("http")
    async def _dashboard_headers(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/dashboard" or path.startswith("/dashboard/"):
            response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["X-Content-Type-Options"] = "nosniff"
            # same-origin (no no-referrer): con no-referrer los navegadores envían
            # "Origin: null" en los POST del propio sitio.
            response.headers["Referrer-Policy"] = "same-origin"
            if path.startswith("/dashboard/static/"):
                response.headers["Cache-Control"] = "public, max-age=3600"
            else:
                response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(router)
    app.mount(
        "/dashboard/static",
        StaticFiles(directory=str(BASE_DIR / "static")),
        name="dashboard-static",
    )

    @app.get("/", include_in_schema=False)
    def _root() -> RedirectResponse:
        return RedirectResponse("/dashboard", status_code=307)
