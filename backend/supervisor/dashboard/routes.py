"""Páginas del dashboard (/dashboard): HTML renderizado en el servidor con Jinja2 + HTMX.

Solo lectura: ninguna ruta modifica datos de trading ni envía nada a MT5. Los únicos POST son
login y logout. Las horas se muestran siempre en UTC.
"""

import logging
import secrets
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from supervisor import __version__
from supervisor.api.deps import SessionDep, SettingsDep
from supervisor.dashboard import auth
from supervisor.dashboard.auth import DashboardSession
from supervisor.models.enums import Direction, TradeStatus
from supervisor.services import dashboard as svc
from supervisor.services.errors import NotFound

log = logging.getLogger("supervisor.dashboard")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
PAGE_SIZE = 50
MAX_PAGE = 1000


# Filtros de plantilla ---------------------------------------------------------------------


def _utc(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    if value is None:
        return "—"
    return value.astimezone(UTC).strftime(fmt)


def _num(value, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{Decimal(value):,.{digits}f}"


def _price(value, digits: int | None = None) -> str:
    if value is None:
        return "—"
    return f"{Decimal(value):,.{5 if digits is None else digits}f}"


def _dec(value) -> str:
    """Decimal sin ceros sobrantes (tick_size, tick_value...)."""
    if value is None:
        return "—"
    return format(Decimal(value).normalize(), "f")


def _pct(value) -> str:
    return "—" if value is None else f"{value:.1f} %"


def _duration(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {minutes:02d} min"
    if minutes:
        return f"{minutes} min {secs:02d} s"
    return f"{secs} s"


def _sign(value) -> str:
    if value is None:
        return ""
    return "pos" if value > 0 else "neg" if value < 0 else ""


templates.env.filters.update(
    utc=_utc, num=_num, price=_price, dec=_dec, pct=_pct, duration=_duration, sign=_sign
)
templates.env.globals["version"] = __version__


# Sesión obligatoria -----------------------------------------------------------------------


class LoginRequired(Exception):
    """Petición al dashboard sin sesión válida."""


def require_session(request: Request) -> DashboardSession:
    session = auth.current_session(request)
    if session is None:
        raise LoginRequired()
    return session


DashSession = Annotated[DashboardSession, Depends(require_session)]


def login_required_response(request: Request) -> Response:
    path = request.url.path
    if path.startswith("/dashboard/api/"):
        return JSONResponse({"detail": "sesión requerida"}, status_code=401)
    target = path + (f"?{request.url.query}" if request.url.query else "")
    login_url = f"/dashboard/login?next={quote(target, safe='')}"
    if request.headers.get("hx-request"):
        # HTMX no sigue redirecciones de página: le indicamos que recargue en el login.
        return Response(status_code=401, headers={"HX-Redirect": login_url})
    return RedirectResponse(login_url, status_code=303)


def _safe_next(value: str | None) -> str:
    """Solo rutas internas del dashboard (evita redirecciones abiertas)."""
    if (
        value
        and value.startswith("/dashboard")
        and not value.startswith("//")
        and "\\" not in value
        and not value.startswith("/dashboard/login")
    ):
        return value
    return "/dashboard"


def _render(
    request: Request,
    name: str,
    context: dict,
    session: DashboardSession | None,
    status_code: int = 200,
) -> HTMLResponse:
    context = {
        "csrf_token": session.csrf_token if session else None,
        "session_expires": datetime.fromtimestamp(session.expires_at, UTC) if session else None,
        **context,
    }
    return templates.TemplateResponse(request, name, context, status_code=status_code)


router = APIRouter(prefix="/dashboard", include_in_schema=False)


# Login / logout ---------------------------------------------------------------------------


def _login_page(
    request: Request, next_url: str, error: str | None = None, status_code: int = 200
) -> HTMLResponse:
    settings = request.app.state.settings
    token = secrets.token_urlsafe(24)
    response = _render(
        request,
        "login.html",
        {"next": next_url, "error": error, "login_csrf": token},
        None,
        status_code,
    )
    response.set_cookie(
        auth.LOGIN_CSRF_COOKIE, token, max_age=3600, **auth.cookie_options(settings)
    )
    return response


@router.get("/login")
def login_form(request: Request, next: str | None = None) -> Response:
    if auth.current_session(request) is not None:
        return RedirectResponse(_safe_next(next), status_code=303)
    return _login_page(request, _safe_next(next))


@router.post("/login")
def login(
    request: Request,
    settings: SettingsDep,
    token: Annotated[str, Form(max_length=512)] = "",
    csrf_token: Annotated[str, Form(max_length=128)] = "",
    next: Annotated[str, Form(max_length=2048)] = "/dashboard",
) -> Response:
    next_url = _safe_next(next)
    if not auth.same_origin(request) or not auth.tokens_match(
        request.cookies.get(auth.LOGIN_CSRF_COOKIE), csrf_token
    ):
        log.warning("login rechazado por CSRF", extra={"client": auth.client_ip(request)})
        return _login_page(
            request, next_url, "La sesión del formulario caducó. Inténtalo de nuevo.", 403
        )
    limiter: auth.LoginRateLimiter = request.app.state.login_limiter
    ip = auth.client_ip(request)
    if limiter.blocked(ip):
        log.warning("login bloqueado por intentos", extra={"client": ip})
        return _login_page(
            request, next_url, "Demasiados intentos fallidos. Espera unos minutos.", 429
        )
    if not token or not auth.admin_token_ok(settings, token):
        limiter.record_failure(ip)
        log.warning("login fallido", extra={"client": ip})
        return _login_page(request, next_url, "Token incorrecto.", 401)
    limiter.reset(ip)
    value, max_age = auth.create_session_cookie(settings)
    response = RedirectResponse(next_url, status_code=303)
    options = auth.cookie_options(settings)
    response.set_cookie(auth.SESSION_COOKIE, value, max_age=max_age, **options)
    response.delete_cookie(auth.LOGIN_CSRF_COOKIE, **auth.cookie_options(settings))
    log.info("login correcto", extra={"client": ip})
    return response


@router.post("/logout")
def logout(
    request: Request,
    settings: SettingsDep,
    csrf_token: Annotated[str, Form(max_length=128)] = "",
) -> Response:
    session = auth.current_session(request)
    if session is None:
        return RedirectResponse("/dashboard/login", status_code=303)
    if not auth.same_origin(request) or not auth.tokens_match(session.csrf_token, csrf_token):
        return JSONResponse({"detail": "token CSRF inválido"}, status_code=403)
    response = RedirectResponse("/dashboard/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE, **auth.cookie_options(settings))
    return response


# Resumen ----------------------------------------------------------------------------------


@router.get("")
@router.get("/")
def resumen(request: Request, dash: DashSession, session: SessionDep) -> HTMLResponse:
    now = svc.utc_now()
    today = datetime.combine(now.date(), time.min, tzinfo=UTC)
    balances = svc.account_balances(session)
    context = {
        "nav": "resumen",
        "now": now,
        "terminals": svc.terminals_health(session, now),
        "backlog": svc.worker_backlog(session),
        "balances": balances,
        "open_trades": svc.open_trades(session),
        "summary_today": svc.closed_summary(session, today, now + timedelta(days=1)),
        "summary_7d": svc.closed_summary(session, now - timedelta(days=7), now + timedelta(days=1)),
        "chart_accounts": [b for b in balances if b.ts is not None],
        "ranges": list(svc.EQUITY_RANGES),
    }
    return _render(request, "resumen.html", context, dash)


@router.get("/api/equity")
def equity_data(
    dash: DashSession,
    session: SessionDep,
    account_id: uuid.UUID,
    range: str = "24h",  # noqa: A002 - nombre del parámetro en la URL
) -> JSONResponse:
    span = svc.EQUITY_RANGES.get(range, svc.EQUITY_RANGES["24h"])
    end = svc.utc_now()
    points = svc.equity_series(session, account_id, end - span, end)
    return JSONResponse(
        {
            "range": range if range in svc.EQUITY_RANGES else "24h",
            "points": [
                {"t": int(ts.timestamp() * 1000), "balance": float(b), "equity": float(e)}
                for ts, b, e in points
            ],
        },
        headers={"Cache-Control": "no-store"},
    )


# Operaciones ------------------------------------------------------------------------------


def _parse_uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _parse_enum(enum_cls, value: str | None):
    try:
        return enum_cls(value) if value else None
    except ValueError:
        return None


@router.get("/operaciones")
def operaciones(
    request: Request,
    dash: DashSession,
    session: SessionDep,
    bot: str | None = None,
    version: str | None = None,
    symbol: str | None = None,
    status: str | None = None,
    direction: str | None = None,
    desde: str | None = None,
    hasta: str | None = None,
    pagina: int = 1,
) -> HTMLResponse:
    bot_value = svc.UNASSIGNED if bot == svc.UNASSIGNED else _parse_uuid(bot)
    d_from, d_to = _parse_date(desde), _parse_date(hasta)
    filters = svc.DashboardTradeFilters(
        bot=str(bot_value) if bot_value else None,
        version_id=_parse_uuid(version),
        symbol=(symbol or "").strip()[:64] or None,
        status=_parse_enum(TradeStatus, status),
        direction=_parse_enum(Direction, direction),
        date_from=datetime.combine(d_from, time.min, tzinfo=UTC) if d_from else None,
        # "hasta" incluye el día completo.
        date_to=datetime.combine(d_to + timedelta(days=1), time.min, tzinfo=UTC) if d_to else None,
    )
    page = min(max(pagina, 1), MAX_PAGE)
    rows, has_more = svc.list_trades(session, filters, PAGE_SIZE, (page - 1) * PAGE_SIZE)
    selected = {
        "bot": filters.bot or "",
        "version": str(filters.version_id or ""),
        "symbol": filters.symbol or "",
        "status": filters.status.value if filters.status else "",
        "direction": filters.direction.value if filters.direction else "",
        "desde": d_from.isoformat() if d_from else "",
        "hasta": d_to.isoformat() if d_to else "",
    }
    query = {k: v for k, v in selected.items() if v}
    context = {
        "nav": "operaciones",
        "rows": rows,
        "page": page,
        "has_more": has_more,
        "selected": selected,
        "prev_url": f"/dashboard/operaciones?{urlencode({**query, 'pagina': page - 1})}"
        if page > 1
        else None,
        "next_url": f"/dashboard/operaciones?{urlencode({**query, 'pagina': page + 1})}"
        if has_more
        else None,
        "unassigned_value": svc.UNASSIGNED,
    }
    partial = request.headers.get("hx-request") and not request.headers.get(
        "hx-history-restore-request"
    )
    if partial:
        response = _render(request, "partials/tabla_operaciones.html", context, dash)
    else:
        context["options"] = svc.filter_options(session)
        response = _render(request, "operaciones.html", context, dash)
    response.headers["Vary"] = "HX-Request"
    return response


@router.get("/operaciones/{trade_id}")
def operacion(
    request: Request, trade_id: uuid.UUID, dash: DashSession, session: SessionDep
) -> HTMLResponse:
    try:
        detail = svc.trade_detail(session, trade_id)
    except NotFound:
        return _render(request, "no_encontrada.html", {"nav": "operaciones"}, dash, status_code=404)
    context = {
        "nav": "operaciones",
        "d": detail,
        "t": detail.row.trade,
        "analysis": svc.trade_analysis(session, trade_id),
        "bars_margin": svc.BARS_MARGIN_MINUTES,
    }
    return _render(request, "operacion.html", context, dash)


@router.get("/api/operaciones/{trade_id}/velas")
def trade_bars_data(trade_id: uuid.UUID, dash: DashSession, session: SessionDep) -> JSONResponse:
    try:
        detail = svc.trade_detail(session, trade_id)
    except NotFound:
        return JSONResponse({"detail": "operación no encontrada"}, status_code=404)
    t = detail.row.trade
    bars = svc.trade_bars(session, t, detail.account.broker_id, svc.utc_now())

    def f(value) -> float | None:
        return None if value is None else float(value)

    def ms(value: datetime | None) -> int | None:
        return None if value is None else int(value.timestamp() * 1000)

    return JSONResponse(
        {
            "symbol": t.symbol,
            "digits": t.symbol_digits,
            "direction": t.direction.value,
            "entry": {"t": ms(t.entry_time), "price": f(t.entry_price)},
            "close": {"t": ms(t.close_time), "price": f(t.close_price)},
            "levels": {
                "initial_sl": f(t.initial_sl),
                "initial_tp": f(t.initial_tp),
                "current_sl": f(t.current_sl),
                "current_tp": f(t.current_tp),
            },
            "bars": [{"t": ms(ts), "close": float(c)} for ts, c in bars],
        },
        headers={"Cache-Control": "no-store"},
    )


# Bots y sistema ---------------------------------------------------------------------------


@router.get("/bots")
def bots(request: Request, dash: DashSession, session: SessionDep) -> HTMLResponse:
    return _render(request, "bots.html", {"nav": "bots", "bots": svc.bots_overview(session)}, dash)


@router.get("/sistema")
def sistema(request: Request, dash: DashSession, session: SessionDep) -> HTMLResponse:
    now = svc.utc_now()
    context = {
        "nav": "sistema",
        "now": now,
        "terminals": svc.terminals_health(session, now),
        "backlog": svc.worker_backlog(session),
        "failed": svc.failed_events(session),
        "unassigned": svc.unassigned_trades(session),
    }
    return _render(request, "sistema.html", context, dash)
