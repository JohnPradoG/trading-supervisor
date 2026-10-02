"""Dashboard web (/dashboard): sesión, CSRF, límite de intentos, páginas con datos reales del
pipeline (API de ingesta -> worker) y reducción de la serie de equity."""

import re
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, insert, update
from sqlalchemy.orm import Session, sessionmaker

from supervisor.config import Settings
from supervisor.dashboard import auth, routes
from supervisor.main import create_app
from supervisor.models import AccountSnapshot, RawEvent
from supervisor.models.enums import RawEventStatus
from supervisor.services import dashboard as svc
from supervisor.worker.processor import process_pending
from tests.conftest import ADMIN_TOKEN, _headers
from tests.integration.trading_helpers import (
    _iso,
    account_id,
    at,
    bar,
    deal,
    deploy,
    load_trade,
    modify,
    post_balance,
    post_bars,
    post_events,
    raw_events,
)

PAGES = ["/dashboard", "/dashboard/operaciones", "/dashboard/bots", "/dashboard/sistema"]


# Fixtures ---------------------------------------------------------------------------------


@pytest.fixture
def make_dash(database_url: str, engine: Engine) -> Iterator[Callable[..., TestClient]]:
    """Clientes del dashboard con su propia app (y su propio límite de intentos)."""
    apps = []

    def factory(**overrides) -> TestClient:
        params = {
            "database_url": database_url,
            "admin_token": ADMIN_TOKEN,
            "log_level": "WARNING",
            "dashboard_cookie_secure": False,
            **overrides,
        }
        app = create_app(Settings(**params))
        apps.append(app)
        return TestClient(app, follow_redirects=False)

    yield factory
    for app in apps:
        app.state.engine.dispose()


def _login_csrf(client: TestClient) -> str:
    page = client.get("/dashboard/login")
    assert page.status_code == 200
    token = client.cookies.get(auth.LOGIN_CSRF_COOKIE)
    assert token and f'value="{token}"' in page.text
    return token


def login(client: TestClient, token: str = ADMIN_TOKEN, next_url: str = "/dashboard"):
    csrf = _login_csrf(client)
    return client.post(
        "/dashboard/login", data={"token": token, "csrf_token": csrf, "next": next_url}
    )


@pytest.fixture
def dash(make_dash) -> TestClient:
    client = make_dash()
    assert login(client).status_code == 303
    return client


def _session_csrf(client: TestClient) -> str:
    page = client.get("/dashboard")
    match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text)
    assert match, page.text[:500]
    return match.group(1)


@pytest.fixture
def seeded(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> dict:
    """Operaciones reales por el pipeline: una cerrada con SL/TP modificado y velas, una
    abierta del bot y una abierta sin asignar (magic sin despliegue)."""
    symbol = f"DASH{uuid.uuid4().hex[:6].upper()}"
    ids = deploy(client, terminal, symbol=symbol)
    login_ = terminal["login"]
    post_balance(client, terminal, at(-5), 10_000)
    post_bars(
        client,
        terminal,
        [
            bar(at(m), 100 + m * 0.1, 100.5 + m * 0.1, 99.5 + m * 0.1, 100 + m * 0.1)
            for m in range(-2, 8)
        ],
        symbol=symbol,
    )
    post_events(
        client,
        terminal,
        deal(login_, 1, 10, "IN", "BUY", 0.5, 100.0, at(0), symbol=symbol, sl=99.0, tp=103.0),
        modify(login_, 10, at(2), 99.5, 103.0, symbol=symbol),
        deal(
            login_, 2, 10, "OUT", "SELL", 0.5, 101.0, at(5), symbol=symbol, profit=50.0, reason="TP"
        ),
        deal(login_, 3, 11, "IN", "SELL", 0.2, 100.0, at(30), symbol=symbol, sl=101.0),
        deal(login_, 4, 12, "IN", "BUY", 0.1, 100.0, at(60), symbol=symbol, magic=777001),
    )
    process_pending(session_factory, worker_settings)
    hb = {
        "sent_at": _iso(datetime.now(UTC)),
        "ea_version": "1.2.3",
        "terminal_connected": True,
        "trade_allowed": True,
    }
    assert (
        client.post("/v1/ingest/heartbeat", json=hb, headers=_headers(terminal)).status_code == 200
    )
    # Un evento FAILED (el worker solo los produce tras horas de espera): se marca a mano.
    failed_id = raw_events(engine, terminal)[0].id
    with Session(engine) as session:
        session.execute(
            update(RawEvent)
            .where(RawEvent.id == failed_id)
            .values(status=RawEventStatus.FAILED, error="error de prueba del dashboard")
        )
        session.commit()
    return {
        **ids,
        "symbol": symbol,
        "terminal": terminal,
        "closed": str(load_trade(engine, terminal, 10).trade_id),
        "open": str(load_trade(engine, terminal, 11).trade_id),
        "unassigned": str(load_trade(engine, terminal, 12).trade_id),
        "account_id": account_id(engine, terminal),
    }


# Autenticación ----------------------------------------------------------------------------


def test_no_page_reachable_without_session(make_dash, seeded: dict) -> None:
    c = make_dash()
    for path in [*PAGES, f"/dashboard/operaciones/{seeded['closed']}", "/dashboard/?x=1"]:
        r = c.get(path)
        assert r.status_code == 303, path
        assert r.headers["location"].startswith("/dashboard/login?next=%2Fdashboard"), path
    for path in [
        f"/dashboard/api/equity?account_id={seeded['account_id']}",
        f"/dashboard/api/operaciones/{seeded['closed']}/velas",
    ]:
        r = c.get(path)
        assert r.status_code == 401, path
        assert "balance" not in r.text and "bars" not in r.text
    htmx = c.get("/dashboard/operaciones?status=OPEN", headers={"HX-Request": "true"})
    assert htmx.status_code == 401
    assert htmx.headers["HX-Redirect"].startswith("/dashboard/login?next=")
    # Los estáticos son públicos (no tienen datos) y las rutas /v1 no cambian.
    assert c.get("/dashboard/static/app.js").status_code == 200
    assert c.get("/dashboard/static/vendor/htmx-2.0.4.min.js").status_code == 200
    assert c.get("/v1/trades").status_code == 401


def test_login_success_sets_hardened_cookie_and_logout(make_dash) -> None:
    c = make_dash()
    page = c.get("/dashboard/login")
    csp = page.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert "frame-ancestors 'none'" in csp
    assert "<script>" not in page.text and " style=" not in page.text

    r = login(c, next_url="/dashboard/bots")
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard/bots"
    cookie = next(h for h in r.headers.get_list("set-cookie") if h.startswith("ts_session="))
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/dashboard" in cookie
    assert "Max-Age=43200" in cookie
    assert c.get("/dashboard").status_code == 200
    # Ya con sesión, /login lleva al dashboard.
    assert c.get("/dashboard/login").headers["location"] == "/dashboard"

    # Logout exige el token CSRF de la sesión.
    assert c.post("/dashboard/logout", data={"csrf_token": "x" * 32}).status_code == 403
    assert c.post("/dashboard/logout", data={}).status_code == 403
    assert c.get("/dashboard").status_code == 200
    r = c.post("/dashboard/logout", data={"csrf_token": _session_csrf(c)})
    assert r.status_code == 303 and r.headers["location"] == "/dashboard/login"
    assert c.get("/dashboard").status_code == 303


def test_cookie_is_secure_by_default(make_dash) -> None:
    c = make_dash(dashboard_cookie_secure=True)
    page = c.get("/dashboard/login")
    assert "Secure" in page.headers["set-cookie"]
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    # El cliente de pruebas va por http y no reenvía cookies Secure: se pone a mano.
    c.cookies.set(auth.LOGIN_CSRF_COOKIE, csrf, path="/dashboard")
    r = c.post("/dashboard/login", data={"token": ADMIN_TOKEN, "csrf_token": csrf})
    assert r.status_code == 303
    cookie = next(h for h in r.headers.get_list("set-cookie") if h.startswith("ts_session="))
    assert "Secure" in cookie


def test_login_failure_and_open_redirect(make_dash) -> None:
    c = make_dash()
    r = login(c, token="incorrecto" * 5)
    assert r.status_code == 401
    assert "Token incorrecto" in r.text
    assert auth.SESSION_COOKIE not in c.cookies
    assert c.get("/dashboard").status_code == 303
    # El destino tras el login solo puede ser una ruta del dashboard.
    for evil in ["https://evil.example/", "//evil.example/dashboard", "/v1/trades"]:
        c2 = make_dash()
        assert login(c2, next_url=evil).headers["location"] == "/dashboard"


def test_login_rate_limit(make_dash) -> None:
    c = make_dash(dashboard_login_max_failures=3)
    for _ in range(3):
        assert login(c, token="malo" * 10).status_code == 401
    # Bloqueado incluso con el token correcto.
    r = login(c)
    assert r.status_code == 429
    assert "Demasiados intentos" in r.text
    assert auth.SESSION_COOKIE not in c.cookies
    # Otra app (otro proceso) tiene su propio contador.
    assert login(make_dash(dashboard_login_max_failures=3)).status_code == 303


def test_rate_limiter_window() -> None:
    limiter = auth.LoginRateLimiter(max_failures=2, window_seconds=60)
    limiter.record_failure("1.2.3.4", now=0)
    limiter.record_failure("1.2.3.4", now=10)
    assert limiter.blocked("1.2.3.4", now=20)
    assert not limiter.blocked("5.6.7.8", now=20)
    assert not limiter.blocked("1.2.3.4", now=61)  # la primera salió de la ventana
    limiter.record_failure("1.2.3.4", now=62)
    assert limiter.blocked("1.2.3.4", now=63)
    limiter.reset("1.2.3.4")
    assert not limiter.blocked("1.2.3.4", now=63)


def test_login_csrf_and_origin(make_dash) -> None:
    c = make_dash()
    csrf = _login_csrf(c)
    # Sin token de formulario, con uno distinto o sin la cookie de login.
    assert c.post("/dashboard/login", data={"token": ADMIN_TOKEN}).status_code == 403
    bad = c.post("/dashboard/login", data={"token": ADMIN_TOKEN, "csrf_token": "x" + csrf[1:]})
    assert bad.status_code == 403
    fresh = make_dash()
    no_cookie = fresh.post("/dashboard/login", data={"token": ADMIN_TOKEN, "csrf_token": csrf})
    assert no_cookie.status_code == 403
    # Origen de otro sitio.
    data = {"token": ADMIN_TOKEN, "csrf_token": csrf}
    assert (
        c.post("/dashboard/login", data=data, headers={"Origin": "https://evil.example"})
    ).status_code == 403
    assert (
        c.post("/dashboard/login", data=data, headers={"Sec-Fetch-Site": "cross-site"})
    ).status_code == 403
    assert auth.SESSION_COOKIE not in c.cookies
    # Mismo origen: correcto (cada respuesta de error renueva el token del formulario).
    ok = c.post(
        "/dashboard/login",
        data={"token": ADMIN_TOKEN, "csrf_token": _login_csrf(c)},
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    )
    assert ok.status_code == 303


def test_logout_rejects_cross_origin(dash: TestClient) -> None:
    csrf = _session_csrf(dash)
    r = dash.post(
        "/dashboard/logout", data={"csrf_token": csrf}, headers={"Origin": "https://evil.example"}
    )
    assert r.status_code == 403
    assert dash.get("/dashboard").status_code == 200


def test_tampered_and_expired_cookies_rejected(make_dash) -> None:
    settings = Settings(database_url="postgresql+psycopg://x@localhost/x", admin_token=ADMIN_TOKEN)
    valid, _ = auth.create_session_cookie(settings)
    assert auth.read_session_cookie(settings, valid) is not None

    body, sig = valid.split(".")
    payload = auth._b64d(body).replace(b'"exp":', b'"exp":9')  # alarga la caducidad
    tampered_body = f"{auth._b64e(payload)}.{sig}"
    other = Settings(database_url="postgresql+psycopg://x@localhost/x", admin_token="o" * 48)
    forged, _ = auth.create_session_cookie(other)
    expired, _ = auth.create_session_cookie(settings, now=time.time() - 13 * 3600)
    flipped_sig = body + "." + ("A" if sig[0] != "A" else "B") + sig[1:]
    for bad in [tampered_body, forged, expired, flipped_sig, "", "abc", "a.b.c", valid + "x"]:
        assert auth.read_session_cookie(settings, bad) is None, bad

    c = make_dash()
    for bad in [tampered_body, forged, expired, flipped_sig]:
        c.cookies.set(auth.SESSION_COOKIE, bad, path="/dashboard")
        assert c.get("/dashboard").status_code == 303
        c.cookies.clear()
    c.cookies.set(auth.SESSION_COOKIE, valid, path="/dashboard")
    assert c.get("/dashboard").status_code == 200

    # Duración configurable.
    short = make_dash(dashboard_session_hours=1)
    r = login(short)
    assert "Max-Age=3600" in r.headers["set-cookie"]


# Páginas con datos ------------------------------------------------------------------------


def test_pages_render_with_data(dash: TestClient, seeded: dict) -> None:
    for path in PAGES:
        r = dash.get(path)
        assert r.status_code == 200, path
        assert "UTC" in r.text
        assert "Content-Security-Policy" in r.headers
        assert r.headers["Cache-Control"] == "no-store"
        assert "<script>" not in r.text and " style=" not in r.text and " onclick" not in r.text

    resumen = dash.get("/dashboard").text
    assert seeded["symbol"] in resumen  # operaciones abiertas
    assert "online" in resumen and "1.2.3" in resumen  # latido reciente del terminal
    assert 'data-chart="equity"' in resumen and str(seeded["account_id"]) in resumen

    bots = dash.get("/dashboard/bots").text
    assert seeded["version_id"] in bots and seeded["symbol"] in bots
    assert "1 cerradas" in bots and "1 abiertas" in bots and "win rate 100.0 %" in bots

    sistema = dash.get("/dashboard/sistema").text
    assert "error de prueba del dashboard" in sistema

    detail = dash.get(f"/dashboard/operaciones/{seeded['closed']}")
    assert detail.status_code == 200
    text = detail.text
    for expected in [
        "TRADE_OPENED",
        "SL_MODIFIED",
        "TRADE_CLOSED",
        "HECHO",
        'id="analisis"',
        "MFE",
        "/velas",
        "Calidad de datos",
    ]:
        assert expected in text, expected
    assert dash.get(f"/dashboard/operaciones/{uuid.uuid4()}").status_code == 404
    assert dash.get("/dashboard/operaciones/no-es-uuid").status_code == 422

    velas = dash.get(f"/dashboard/api/operaciones/{seeded['closed']}/velas").json()
    assert velas["entry"]["price"] == 100.0 and velas["close"]["price"] == 101.0
    assert velas["levels"]["initial_sl"] == 99.0 and velas["levels"]["current_sl"] == 99.5
    assert velas["levels"]["initial_tp"] == 103.0
    assert len(velas["bars"]) == 10  # todas las velas del tramo entrada-30 min .. cierre+30 min
    assert [b["t"] for b in velas["bars"]] == sorted(b["t"] for b in velas["bars"])


def test_analysis_slot_renders_provider_output(
    dash: TestClient, seeded: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La Fase 6 solo tiene que devolver el dict desde services.dashboard.trade_analysis."""
    analysis = {
        "outcome": "WIN",
        "analyzer_version": "1.0.0",
        "created_at": datetime.now(UTC),
        "facts": [{"text": "El precio estaba por encima de EMA200.", "confidence": None}],
        "hypotheses": [{"text": "Entrada a favor de tendencia H1.", "confidence": "baja"}],
        "data_quality": [{"code": "sin_dna", "text": "DNA no disponible"}],
    }
    monkeypatch.setattr(svc, "trade_analysis", lambda session, trade_id: analysis)
    text = dash.get(f"/dashboard/operaciones/{seeded['closed']}").text
    assert "Análisis pendiente" not in text
    assert "El precio estaba por encima de EMA200." in text and "HIPÓTESIS" in text


def test_operaciones_htmx_partial_filters(
    dash: TestClient, seeded: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    def ids(params: dict, partial: bool = True) -> list[str]:
        headers = {"HX-Request": "true"} if partial else {}
        r = dash.get("/dashboard/operaciones", params=params, headers=headers)
        assert r.status_code == 200, r.text
        assert ("<html" in r.text) is not partial
        assert r.headers["Vary"] == "HX-Request"
        return re.findall(r'href="/dashboard/operaciones/([0-9a-f-]{36})"', r.text)

    sym = {"symbol": seeded["symbol"]}
    assert ids(sym) == [seeded["unassigned"], seeded["open"], seeded["closed"]]
    assert ids(sym, partial=False) == [seeded["unassigned"], seeded["open"], seeded["closed"]]
    assert ids({**sym, "status": "CLOSED"}) == [seeded["closed"]]
    assert ids({**sym, "direction": "SELL"}) == [seeded["open"]]
    assert ids({**sym, "bot": seeded["bot_id"]}) == [seeded["open"], seeded["closed"]]
    assert ids({**sym, "version": seeded["version_id"], "status": "OPEN"}) == [seeded["open"]]
    assert ids({**sym, "bot": "none"}) == [seeded["unassigned"]]
    day = at(0).date()
    assert len(ids({**sym, "desde": day.isoformat(), "hasta": day.isoformat()})) >= 1
    assert ids({**sym, "desde": (day + timedelta(days=2)).isoformat()}) == []
    # Parámetros vacíos o inválidos de los formularios se ignoran en vez de dar 422.
    assert len(ids({**sym, "bot": "", "version": "x", "status": "", "desde": "nope"})) == 3
    # El historial de HTMX pide la página completa.
    full = dash.get(
        "/dashboard/operaciones",
        params=sym,
        headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
    )
    assert "<html" in full.text

    monkeypatch.setattr(routes, "PAGE_SIZE", 2)
    first = dash.get("/dashboard/operaciones", params=sym, headers={"HX-Request": "true"}).text
    assert "pagina=2" in first and "Siguiente" in first
    second = dash.get(
        "/dashboard/operaciones", params={**sym, "pagina": 2}, headers={"HX-Request": "true"}
    ).text
    assert seeded["closed"] in second and "Anterior" in second and "Siguiente" not in second


def test_unassigned_trades_listing(dash: TestClient, seeded: dict, engine: Engine) -> None:
    with Session(engine) as session:
        groups = svc.unassigned_trades(session)
    mine = [g for g in groups if g.symbol == seeded["symbol"]]
    assert len(mine) == 1
    g = mine[0]
    assert (g.magic_number, g.trades, g.open_trades) == (777001, 1, 1)
    assert g.account_login == seeded["terminal"]["login"]
    sistema = dash.get("/dashboard/sistema").text
    assert "777001" in sistema and seeded["symbol"] in sistema


# Serie de equity --------------------------------------------------------------------------


def test_equity_downsampling(dash: TestClient, seeded: dict, engine: Engine) -> None:
    acc = seeded["account_id"]
    now = datetime.now(UTC).replace(microsecond=0)
    # 2000 fotos en las últimas ~23 h (cada 40 s) y una vieja fuera de los 30 días.
    rows = [
        {
            "account_id": acc,
            "ts": now - timedelta(seconds=40 * i),
            "balance": Decimal(10_000) + i,
            "equity": Decimal(10_000) - i,
            "open_positions": 0,
        }
        for i in range(1, 2001)
    ]
    rows.append({**rows[0], "ts": now - timedelta(days=40), "balance": Decimal(1)})
    with Session(engine) as session:
        session.execute(insert(AccountSnapshot), rows)
        session.commit()

    for rng in ["24h", "7d", "30d"]:
        data = dash.get(f"/dashboard/api/equity?account_id={acc}&range={rng}").json()
        pts = data["points"]
        assert data["range"] == rng
        assert 1 < len(pts) <= 500, (rng, len(pts))
        ts = [p["t"] for p in pts]
        assert ts == sorted(ts) and len(set(ts)) == len(ts)
        # El último punto es la foto más reciente; la vieja no entra.
        assert pts[-1]["t"] == int(rows[0]["ts"].timestamp() * 1000)
        assert pts[-1]["balance"] == 10_001.0 and pts[-1]["equity"] == 9_999.0
        assert all(p["balance"] != 1 for p in pts)
    # 24 h: intervalos de ~174 s con fotos cada 40 s -> unos 470 puntos.
    assert len(dash.get(f"/dashboard/api/equity?account_id={acc}&range=24h").json()["points"]) > 400
    # Rango desconocido -> 24h; cuenta sin fotos -> vacío.
    assert dash.get(f"/dashboard/api/equity?account_id={acc}&range=1y").json()["range"] == "24h"
    empty = dash.get(f"/dashboard/api/equity?account_id={uuid.uuid4()}").json()
    assert empty["points"] == []

    with Session(engine) as session:
        few = svc.equity_series(session, acc, now - timedelta(hours=24), now, max_points=10)
    assert len(few) <= 10 and few[-1][0] == rows[0]["ts"]
