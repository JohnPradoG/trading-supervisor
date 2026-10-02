"""Alertas (sección 15): TRAMPA_ACTIVA, PATRON_VALIDADO/CADUCADO, EA_SIN_LATIDO, entrega por
Telegram (con un opener falso: nunca red real), dashboard, API y CLI."""

import io
import json
import logging
import re
import urllib.error
import urllib.parse
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from supervisor import cli
from supervisor.alerts import engine as al
from supervisor.alerts import telegram as tg
from supervisor.analytics import pattern_search as ps
from supervisor.config import Settings
from supervisor.log_config import JsonFormatter
from supervisor.models import Alert, Heartbeat, Hypothesis, Trade, TradeDna
from supervisor.models.enums import (
    AlertSeverity,
    DataOrigin,
    Direction,
    HypothesisStatus,
    TradeSource,
    TradeStatus,
)
from tests.conftest import ADMIN_TOKEN
from tests.integration.pattern_helpers import Scope, insert_trades, make_scope
from tests.integration.test_dashboard import _session_csrf, login, make_dash  # noqa: F401
from tests.integration.test_patterns import PLANTED, _by_key

TOKEN = "123456789:AAH-secreto_de_prueba_XYZ"
CHAT = "987654321"
# Miércoles, mercado abierto.
WEDNESDAY = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


@pytest.fixture
def settings(worker_settings: Settings) -> Settings:
    return worker_settings


def tg_settings(base: Settings, **overrides) -> Settings:
    return base.model_copy(
        update={
            "telegram_bot_token": SecretStr(TOKEN),
            "telegram_chat_id": CHAT,
            "public_url": "https://supervisor.ejemplo.com",
            **overrides,
        }
    )


def _alerts(session: Session, key_prefix: str) -> list[Alert]:
    return list(
        session.scalars(
            select(Alert)
            .where(Alert.dedup_key.like(f"{key_prefix}%"))
            .order_by(Alert.created_at, Alert.event)
        )
    )


# TRAMPA_ACTIVA y patrones -------------------------------------------------------------------


def _open_trade(
    session: Session, scope: Scope, trend: str, entry: datetime, imported: bool = False
) -> Trade:
    trade = Trade(
        account_id=scope.account_id,
        bot_version_id=scope.version_id,
        position_id=uuid.uuid4().int % 2**62,
        magic_number=909,
        symbol=scope.symbol,
        direction=Direction.SELL,
        order_type="MARKET",
        source=TradeSource.DEMO,
        status=TradeStatus.OPEN,
        symbol_point=Decimal("0.01"),
        symbol_digits=2,
        entry_time=entry,
        entry_price=Decimal("20000.50"),
        initial_sl=Decimal("20100"),
        initial_volume=Decimal("0.10"),
        max_volume=Decimal("0.10"),
        data_quality={"importado": True} if imported else {},
    )
    session.add(trade)
    session.flush()
    session.add(
        TradeDna(
            trade_id=trade.trade_id,
            dna_version=1,
            feature_set_version=1,
            input_hash=uuid.uuid4().hex * 2,
            source=DataOrigin.SERVER,
            data_cutoff=entry,
            features={"tendencia_h1_rel": trend, "sesion": "LONDRES"},
            null_reasons={},
            data_quality={},
            inputs={},
        )
    )
    session.flush()
    return trade


@pytest.fixture
def planted(session: Session, settings: Settings) -> tuple[Scope, Hypothesis]:
    scope = make_scope(session)
    insert_trades(session, scope, 360, seed=3, planted=True, facts=True)
    ps.run_patterns(session, settings, scope.version_id)
    trap = _by_key(session, scope, PLANTED)
    assert trap.status == HypothesisStatus.VALIDATED
    return scope, trap


def test_trap_active_fires_once_for_validated_trap(
    session: Session, settings: Settings, planted
) -> None:
    scope, trap = planted
    now = datetime.now(UTC)
    hit = _open_trade(session, scope, "EN_CONTRA", now - timedelta(minutes=3))
    miss = _open_trade(session, scope, "A_FAVOR", now - timedelta(minutes=2))
    imported = _open_trade(session, scope, "EN_CONTRA", now - timedelta(minutes=1), True)
    old = _open_trade(session, scope, "EN_CONTRA", now - timedelta(hours=5))
    s = tg_settings(settings)

    stats = al.evaluate(session, s, now)
    assert stats[al.TRAP_ACTIVE] >= 1
    alerts = _alerts(session, f"{al.TRAP_ACTIVE}:{hit.trade_id}")
    planted_alert = next(a for a in alerts if a.hypothesis_id == trap.id)
    assert planted_alert.severity == AlertSeverity.CRITICAL and planted_alert.event == al.FIRE
    assert planted_alert.delivery_status == al.PENDING and planted_alert.trade_id == hit.trade_id
    for other in (miss, imported, old):
        assert not _alerts(session, f"{al.TRAP_ACTIVE}:{other.trade_id}")
    # Contenido: bot/versión, símbolo, dirección, entrada, trampa y estadísticas OOS.
    data = planted_alert.data
    assert data["simbolo"] == scope.symbol and data["direccion"] == "SELL"
    assert data["trampa"] == "Entrar contra la tendencia H1"
    oos = data["fuera_de_muestra"]
    assert oos["n"] >= settings.patterns_min_n_oos and oos["expectancy_r_ic95"][1] < 0
    assert "Fuera de muestra: n=" in planted_alert.message and "IC95" in planted_alert.message
    assert "v1.0.0" in data["titulo"] and "20000.50" in planted_alert.message
    assert data["enlace"] == f"https://supervisor.ejemplo.com/dashboard/operaciones/{hit.trade_id}"
    message = al.telegram_text(planted_alert)
    assert message.startswith("🚨 <b>TRAMPA ACTIVA") and "Abrir en el dashboard" in message

    # Idempotente: la misma operación y trampa no vuelve a avisar.
    count = len(_alerts(session, al.TRAP_ACTIVE))
    al.evaluate(session, s, now + timedelta(minutes=1))
    assert len(_alerts(session, al.TRAP_ACTIVE)) == count


def test_unvalidated_candidate_does_not_fire(session: Session, settings: Settings, planted) -> None:
    scope, trap = planted
    session.execute(
        update(Hypothesis)
        .where(Hypothesis.bot_version_id == scope.version_id)
        .values(status=HypothesisStatus.TESTING)
    )
    session.expire_all()
    now = datetime.now(UTC)
    hit = _open_trade(session, scope, "EN_CONTRA", now - timedelta(minutes=3))
    al.evaluate(session, settings, now)
    assert not _alerts(session, f"{al.TRAP_ACTIVE}:{hit.trade_id}")


def test_pattern_validated_then_decayed(session: Session, settings: Settings, planted) -> None:
    _, trap = planted
    now = datetime.now(UTC)
    al.evaluate(session, settings, now)
    fired = _alerts(session, f"{al.PATTERN_VALIDATED}:{trap.id}")
    assert len(fired) == 1 and fired[0].severity == AlertSeverity.WARNING
    assert fired[0].delivery_status == al.SKIPPED  # sin Telegram configurado
    assert "Entrar contra la tendencia H1" in fired[0].message
    al.evaluate(session, settings, now + timedelta(minutes=1))
    assert len(_alerts(session, f"{al.PATTERN_VALIDATED}:{trap.id}")) == 1

    trap.status = HypothesisStatus.DECAYED
    trap.status_note = "forward: la expectativa pasó a ser positiva"
    session.flush()
    al.evaluate(session, settings, now + timedelta(minutes=2))
    decayed = _alerts(session, f"{al.PATTERN_DECAYED}:{trap.id}")
    assert len(decayed) == 1 and "forward" in decayed[0].message


# EA_SIN_LATIDO -------------------------------------------------------------------------------


def _beat(session: Session, terminal_id: uuid.UUID, ts: datetime) -> None:
    session.add(
        Heartbeat(
            terminal_id=terminal_id,
            ts=ts,
            ea_version="1.0.0",
            terminal_connected=True,
            trade_allowed=True,
            info={},
        )
    )
    session.flush()


def test_missing_heartbeat_fires_dedups_resolves_and_cools_down(
    session: Session, settings: Settings, world: dict
) -> None:
    terminal = world["terminal"]
    key = f"{al.NO_HEARTBEAT}:{terminal.id}"
    now = WEDNESDAY
    _beat(session, terminal.id, now - timedelta(minutes=3))
    al.evaluate(session, settings, now)
    assert not _alerts(session, key)  # 3 min < 5 min

    now += timedelta(minutes=4)  # último latido hace 7 min
    al.evaluate(session, settings, now)
    rows = _alerts(session, key)
    assert [r.event for r in rows] == [al.FIRE] and rows[0].severity == AlertSeverity.WARNING
    assert terminal.name in rows[0].message and "hace 7 min" in rows[0].message

    al.evaluate(session, settings, now + timedelta(minutes=10))
    assert len(_alerts(session, key)) == 1  # sigue activa: no se repite

    now += timedelta(minutes=15)
    _beat(session, terminal.id, now)
    al.evaluate(session, settings, now)
    rows = _alerts(session, key)
    assert [r.event for r in rows] == [al.FIRE, al.RESOLVE]
    assert rows[1].severity == AlertSeverity.INFO and "vuelve a enviar" in rows[1].message
    assert al.telegram_text(rows[1]).startswith("✅")

    # Vuelve a caer enseguida: enfriamiento (60 min desde el último disparo).
    now += timedelta(minutes=10)
    al.evaluate(session, settings, now)
    assert len(_alerts(session, key)) == 2
    now += timedelta(minutes=50)
    al.evaluate(session, settings, now)
    assert [r.event for r in _alerts(session, key)] == [al.FIRE, al.RESOLVE, al.FIRE]


def test_missing_heartbeat_suppressed_on_weekend(
    session: Session, settings: Settings, world: dict
) -> None:
    terminal = world["terminal"]
    key = f"{al.NO_HEARTBEAT}:{terminal.id}"
    friday_close = datetime(2026, 10, 2, 20, 30, tzinfo=UTC)  # viernes
    _beat(session, terminal.id, friday_close)
    for when in (
        datetime(2026, 10, 2, 22, 0, tzinfo=UTC),  # viernes tras el cierre
        datetime(2026, 10, 3, 12, 0, tzinfo=UTC),  # sábado
        datetime(2026, 10, 4, 20, 0, tzinfo=UTC),  # domingo antes de abrir
    ):
        assert al.market_closed(when, settings)
        al.evaluate(session, settings, when)
    assert not _alerts(session, key)
    sunday_open = datetime(2026, 10, 4, 21, 30, tzinfo=UTC)
    assert not al.market_closed(sunday_open, settings)
    al.evaluate(session, settings, sunday_open)
    assert len(_alerts(session, key)) == 1
    weekends_on = settings.model_copy(update={"alerts_skip_weekends": False})
    assert not al.market_closed(datetime(2026, 10, 3, 12, 0, tzinfo=UTC), weekends_on)


def test_alerts_are_append_only(session: Session, settings: Settings, world: dict) -> None:
    _beat(session, world["terminal"].id, WEDNESDAY - timedelta(hours=1))
    al.evaluate(session, settings, WEDNESDAY)
    alert = _alerts(session, f"{al.NO_HEARTBEAT}:{world['terminal'].id}")[0]
    session.execute(
        text("UPDATE alerts SET acknowledged_at = now(), delivery_attempts = 1 WHERE id = :i"),
        {"i": alert.id},
    )
    for sql in (
        "UPDATE alerts SET message = 'otra' WHERE id = :i",
        "UPDATE alerts SET acknowledged_at = NULL WHERE id = :i",
        "DELETE FROM alerts WHERE id = :i",
    ):
        with pytest.raises(DBAPIError, match="alerts|solo inserción"), session.begin_nested():
            session.execute(text(sql), {"i": alert.id})


# Telegram ------------------------------------------------------------------------------------


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class FakeOpener:
    """Sustituye a urllib.request.urlopen: registra las peticiones y responde lo indicado."""

    def __init__(self, responses: list) -> None:
        self.responses = responses
        self.requests: list = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        result = self.responses.pop(0) if self.responses else {"ok": True}
        if isinstance(result, Exception):
            raise result
        return FakeResponse(json.dumps(result).encode())


def http_error(code: int, body: dict) -> urllib.error.HTTPError:
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    return urllib.error.HTTPError(url, code, "error", {}, io.BytesIO(json.dumps(body).encode()))


def _pending(session: Session, settings: Settings, n: int = 1) -> list[Alert]:
    out = []
    for i in range(n):
        c = al.Candidate(
            rule=al.NO_HEARTBEAT,
            severity=AlertSeverity.WARNING,
            key=f"PRUEBA:{uuid.uuid4()}",
            title=f"EA sin latido · <term&{i}>",
            lines=["Último latido hace 9 min"],
            link="https://supervisor.ejemplo.com/dashboard/sistema",
        )
        out.append(
            al._insert(
                session,
                settings,
                c,
                al.FIRE,
                WEDNESDAY - timedelta(days=400) + timedelta(seconds=i),
            )
        )
    session.flush()
    return out


def test_telegram_sends_escaped_html_with_rate_limit(session: Session, settings: Settings) -> None:
    s = tg_settings(settings, telegram_batch_size=3)
    alerts = _pending(session, s, 2)
    opener = FakeOpener([{"ok": True}, {"ok": True}])
    sleeps: list[float] = []
    client = tg.TelegramClient.from_settings(s, opener)
    stats = al.deliver_pending(session, s, client, WEDNESDAY, sleep=sleeps.append)
    assert stats["enviadas"] >= 2
    assert all(a.delivery_status == al.SENT and a.sent_at for a in alerts)
    assert sleeps and all(x == s.telegram_min_interval_seconds for x in sleeps)
    request = opener.requests[0]
    assert request.full_url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    body = urllib.parse.parse_qs(request.data.decode())
    assert body["chat_id"] == [CHAT] and body["parse_mode"] == ["HTML"]
    assert re.search(r"<b>EA sin latido · &lt;term&amp;\d&gt;</b>", body["text"][0])
    assert body["text"][0].startswith("⚠️")
    assert '<a href="https://supervisor.ejemplo.com/dashboard/sistema">' in body["text"][0]


def test_telegram_retries_then_fails_without_leaking_token(
    session: Session, settings: Settings
) -> None:
    s = tg_settings(settings, telegram_max_attempts=2, telegram_batch_size=50)
    [alert] = _pending(session, s)
    session.execute(
        update(Alert)
        .where(Alert.delivery_status == al.PENDING, Alert.id != alert.id)
        .values(delivery_status=al.SKIPPED)
    )
    error = urllib.error.URLError(f"fallo conectando a https://api.telegram.org/bot{TOKEN}/x")
    opener = FakeOpener([error, http_error(502, {"ok": False, "description": "Bad Gateway"})])
    client = tg.TelegramClient.from_settings(s, opener)
    # Handler propio (configure_logging reemplaza los del root) con el formato JSON real.
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("supervisor.alerts")
    logger.addHandler(handler)
    # fileConfig de alembic (en las migraciones de las pruebas) desactiva los loggers previos.
    disabled, logger.disabled = logger.disabled, False

    now = WEDNESDAY
    al.deliver_pending(session, s, client, now, sleep=lambda _: None)
    assert alert.delivery_status == al.PENDING and alert.delivery_attempts == 1
    assert alert.next_delivery_at == now + timedelta(seconds=s.telegram_retry_base_seconds)
    assert TOKEN not in alert.delivery_error and tg.REDACTED in alert.delivery_error
    # Antes de la hora del reintento no se vuelve a intentar.
    al.deliver_pending(session, s, client, now + timedelta(seconds=5), sleep=lambda _: None)
    assert alert.delivery_attempts == 1
    al.deliver_pending(session, s, client, now + timedelta(minutes=5), sleep=lambda _: None)
    assert alert.delivery_status == al.FAILED and alert.delivery_attempts == 2
    assert "HTTP 502" in alert.delivery_error
    logger.removeHandler(handler)
    logger.disabled = disabled
    logged = stream.getvalue()
    assert "alerta no enviada" in logged and tg.REDACTED in logged and TOKEN not in logged


def test_telegram_permanent_error_and_retry_after(session: Session, settings: Settings) -> None:
    s = tg_settings(settings, telegram_batch_size=50)
    session.execute(
        update(Alert).where(Alert.delivery_status == al.PENDING).values(delivery_status=al.SKIPPED)
    )
    first, second = _pending(session, s, 2)
    opener = FakeOpener(
        [
            http_error(401, {"ok": False, "description": "Unauthorized"}),
            http_error(429, {"ok": False, "parameters": {"retry_after": 7}}),
        ]
    )
    client = tg.TelegramClient.from_settings(s, opener)
    al.deliver_pending(session, s, client, WEDNESDAY, sleep=lambda _: None)
    assert first.delivery_status == al.FAILED and first.delivery_attempts == 1
    assert second.delivery_status == al.PENDING
    assert second.next_delivery_at == WEDNESDAY + timedelta(seconds=7)


def test_telegram_disabled_when_unconfigured(session: Session, settings: Settings) -> None:
    assert not settings.telegram_enabled
    assert not tg_settings(settings, telegram_chat_id=" ").telegram_enabled
    [alert] = _pending(session, settings)
    assert alert.delivery_status == al.SKIPPED
    assert al.deliver_pending(session, settings) == {}
    with pytest.raises(tg.TelegramError, match="no está configurado"):
        tg.TelegramClient.from_settings(settings)
    assert TOKEN not in repr(tg_settings(settings))


def test_redact_and_format() -> None:
    assert tg.redact(f"https://api.telegram.org/bot{TOKEN}/sendMessage", TOKEN) == (
        f"https://api.telegram.org/bot{tg.REDACTED}/sendMessage"
    )
    text_ = tg.format_alert(
        severity="CRITICAL", event="DISPARO", title="A<b>", lines=["x & y"], link=None
    )
    assert text_ == "🚨 <b>A&lt;b&gt;</b>\nx &amp; y"


# Dashboard, API y CLI (datos confirmados) ---------------------------------------------------


@pytest.fixture
def committed_alert(engine: Engine) -> dict:
    with Session(engine, expire_on_commit=False) as session:
        c = al.Candidate(
            rule=al.TRAP_ACTIVE,
            severity=AlertSeverity.CRITICAL,
            key=f"{al.TRAP_ACTIVE}:prueba:{uuid.uuid4()}",
            title="TRAMPA ACTIVA · EA_Prueba v1.0.0",
            lines=["USTEC SELL a 20000.00", "Trampa validada: Entrar contra la tendencia H1"],
        )
        alert = al._insert(
            session,
            Settings(database_url="x", admin_token=ADMIN_TOKEN),
            c,
            al.FIRE,
            datetime.now(UTC),
        )
        session.commit()
        return {"id": alert.id}


def test_dashboard_alerts_page_ack_and_badge(make_dash, committed_alert: dict) -> None:  # noqa: F811
    dash = make_dash()
    assert dash.get("/dashboard/alertas").status_code == 303
    assert login(dash).status_code == 303
    page = dash.get("/dashboard/alertas?tipo=TRAMPA_ACTIVA&gravedad=CRITICAL")
    assert page.status_code == 200
    html = page.text
    assert 'href="/dashboard/alertas" aria-current="page"' in html
    assert re.search(r'Alertas <span class="badge bad nav-badge"[^>]*>\d+</span>', html)
    assert f'id="a-{committed_alert["id"]}"' in html and "Entrar contra la tendencia H1" in html
    assert "Telegram no está configurado" in html and "style=" not in html
    home = dash.get("/dashboard").text
    assert "Trampas activas recientes" in home and "EA_Prueba v1.0.0" in home

    url = f"/dashboard/alertas/{committed_alert['id']}/reconocer"
    assert dash.post(url, data={"csrf_token": "malo"}).status_code == 403
    csrf = _session_csrf(dash)
    done = dash.post(url, data={"csrf_token": csrf, "next": "/dashboard/alertas"})
    assert done.status_code == 303 and done.headers["location"] == "/dashboard/alertas"
    after = dash.get("/dashboard/alertas?pendientes=1").text
    assert f'id="a-{committed_alert["id"]}"' not in after
    missing = dash.post(f"/dashboard/alertas/{uuid.uuid4()}/reconocer", data={"csrf_token": csrf})
    assert missing.status_code == 404


def test_alerts_api(client: TestClient, committed_alert: dict) -> None:
    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    assert client.get("/v1/alerts").status_code == 401
    items = client.get("/v1/alerts?kind=TRAMPA_ACTIVA&unacknowledged=true", headers=admin).json()
    assert any(i["id"] == str(committed_alert["id"]) for i in items)
    assert all(i["rule"] == "TRAMPA_ACTIVA" and i["acknowledged_at"] is None for i in items)
    assert client.get("/v1/alerts?severity=NADA", headers=admin).status_code == 422
    url = f"/v1/alerts/{committed_alert['id']}/ack"
    assert client.post(url).status_code == 401
    first = client.post(url, headers=admin).json()
    assert first["acknowledged_at"] is not None
    again = client.post(url, headers=admin).json()
    assert again["acknowledged_at"] == first["acknowledged_at"]
    assert client.post(f"/v1/alerts/{uuid.uuid4()}/ack", headers=admin).status_code == 404


def test_cli_alerts_and_setup_help(
    engine: Engine,
    committed_alert: dict,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    @contextmanager
    def scope_() -> Iterator[Session]:
        with Session(engine, expire_on_commit=False) as session:
            yield session
            session.commit()

    monkeypatch.setattr(cli, "session_scope", scope_)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    cli.main(["telegram", "setup-help"])
    out = capsys.readouterr().out
    assert "@BotFather" in out and "/newbot" in out and "getUpdates" in out
    assert "SUPERVISOR_TELEGRAM_CHAT_ID" in out

    cli.main(["alerts", "list", "--kind", "TRAMPA_ACTIVA"])
    assert "EA_Prueba v1.0.0" in capsys.readouterr().out

    with pytest.raises(SystemExit, match="no está configurado"):
        cli.main(["alerts", "test"])

    configured = tg_settings(settings)
    monkeypatch.setattr(cli, "get_settings", lambda: configured)
    opener = FakeOpener([{"ok": True}, http_error(400, {"description": "chat not found"})])
    monkeypatch.setattr(tg.urllib.request, "urlopen", opener)
    cli.main(["alerts", "test"])
    assert "Mensaje de prueba enviado" in capsys.readouterr().out
    assert "Prueba del Trading Supervisor" in urllib.parse.unquote_plus(
        opener.requests[0].data.decode()
    )
    with pytest.raises(SystemExit, match="chat not found") as exc:
        cli.main(["alerts", "test"])
    assert TOKEN not in str(exc.value)


def test_worker_alerts_pass(session_factory, settings: Settings) -> None:
    off = settings.model_copy(update={"alerts_enabled": False})
    assert al.alerts_pass(session_factory, off) == {}
    stats = al.alerts_pass(session_factory, settings)
    assert "error_entrega" not in stats
