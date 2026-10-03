"""Diagnóstico de bots de punta a punta en PostgreSQL: réplica de salidas con velas M1,
selección solo con entrenamiento, evaluación en validación y fuera de muestra, etiquetas
"validada" / "candidata", aviso de win rate, idempotencia, API, CLI, worker, dashboard y
hechos previos a la entrada (TRAMPA_ACTIVA con trampas basadas en EMA)."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from supervisor import cli
from supervisor.alerts import engine as al
from supervisor.analytics import pattern_search as ps
from supervisor.analytics.pre_entry import PRE_ENTRY_VERSION
from supervisor.config import Settings
from supervisor.models import Alert, Deployment, DiagnosisRun, Hypothesis, Trade, TradeDna
from supervisor.models.enums import (
    DataOrigin,
    Direction,
    HypothesisStatus,
    TradeSource,
    TradeStatus,
)
from supervisor.services import diagnosis as dsvc
from supervisor.services import diagnosis_report
from tests.conftest import ADMIN_TOKEN
from tests.integration.diagnosis_helpers import insert_diag_trades
from tests.integration.pattern_helpers import Scope, insert_trades, make_scope
from tests.integration.test_dashboard import _session_csrf, login
from tests.integration.test_dashboard import make_dash as make_dash  # noqa: F401 (fixture)
from tests.integration.trading_helpers import admin


@pytest.fixture
def settings(worker_settings: Settings) -> Settings:
    return worker_settings


def _diagnose(
    session: Session, settings: Settings, kind: str, n: int = 200, seed: int = 1
) -> tuple[Scope, DiagnosisRun]:
    scope = make_scope(session)
    insert_diag_trades(session, scope, n, seed=seed, kind=kind)
    ps.run_patterns(session, settings, scope.version_id)
    status, run = dsvc.compute_diagnosis(session, settings, scope.version_id)
    assert status == dsvc.CREATED
    return scope, run


def _change(run: DiagnosisRun, key: str) -> dict:
    return next(c for c in run.report["cambios"] if c["clave"] == key)


# Réplica, selección y etiquetas -------------------------------------------------------------


def test_planted_tight_sl_is_found_and_validated_out_of_sample(
    session: Session, settings: Settings
) -> None:
    _, run = _diagnose(session, settings, "sl_ajustado")
    r = run.report
    s = r["resumen"]
    # Win rate (Wilson) siempre junto con expectativa y profit factor.
    assert s["win_rate_ic95"] and s["expectancy_r_ic95"] and s["profit_factor_r"] is not None
    assert s["suficiencia"]["tramos"] == {
        "entrenamiento": 120,
        "validacion": 40,
        "fuera_de_muestra": 40,
    }
    # La réplica reproduce el cierre real (mismas reglas, mismas velas).
    assert r["replica"]["replicadas"] == 200
    assert r["replica"]["fidelidad"]["motivo_igual_al_real"] == 1.0
    assert "primero el stop" in r["replica"]["parametros"]["ambiguedad"]
    # Rejilla pequeña y fija registrada.
    assert r["variantes"]["salida"]["probadas"] == 20
    assert run.params["replica"] and run.params["diagnostico"]
    # Modo de fallo y cambio validado fuera de muestra.
    codes = {f["codigo"]: f for f in r["fallos"]}
    assert (
        "SL_DEMASIADO_AJUSTADO" in codes and codes["SL_DEMASIADO_AJUSTADO"]["estado"] == "validada"
    )
    wide = _change(run, "salida:sl_1_25")
    assert wide["estado"] == dsvc.dg.VALIDATED
    oos = wide["tramos"]["fuera_de_muestra"]
    assert oos["mejora_r"] > 0 and oos["mejora_r_ic95"][0] > 0
    assert set(wide["tramos"]) >= {"entrenamiento", "validacion", "fuera_de_muestra"}
    assert "validada fuera de muestra" in wide["motivo_estado"]
    assert "n=40" in wide["texto"]
    # El resumen marca lo que funciona.
    assert any(c["clave"] == "salida:sl_1_25" for c in r["funciona"]["cambios_validados"])


def test_candidate_when_not_confirmed_and_noise_validates_nothing(
    session: Session, settings: Settings
) -> None:
    _, run = _diagnose(session, settings, "ruido", n=200, seed=2)
    r = run.report
    assert not any(
        c["estado"] == dsvc.dg.VALIDATED and not c["no_recomendada"] for c in r["cambios"]
    )
    assert all(
        c["estado"] in (dsvc.dg.CANDIDATE, dsvc.dg.NO_DATA, dsvc.dg.VALIDATED) for c in r["cambios"]
    )
    assert r["funciona"]["cambios_validados"] == []
    md = diagnosis_report.markdown(r, dsvc.run_view(run))
    assert "CANDIDATA, NO VALIDADA" in md or "ninguna variante mejora" in md


def test_win_rate_up_but_net_worse_is_warned(session: Session, settings: Settings) -> None:
    _, run = _diagnose(session, settings, "win_rate_trampa", n=200, seed=3)
    r = run.report
    assert r["trampas_win_rate"], "debe avisar de un cambio que sube los aciertos y baja el neto"
    for t in r["trampas_win_rate"]:
        train = t["entrenamiento"]
        assert train["sugerida"]["win_rate"] > train["original"]["win_rate"]
        assert train["mejora_r"] < 0 and "no recomendado" in t["aviso"]
        # Nunca se propone como cambio ni se marca como validada.
        assert t["clave"] not in {c["clave"] for c in r["cambios"] if not c["no_recomendada"]}
    assert "salida:tp_1" in {t["clave"] for t in r["trampas_win_rate"]}
    # Un cambio que sube aciertos Y no empeora el neto sí se marca como preferido.
    assert any(c["preferida"] and c["aviso_win_rate"] for c in r["cambios"])
    md = diagnosis_report.markdown(r, dsvc.run_view(run))
    assert "suben los aciertos pero pierden dinero" in md


def test_selection_uses_train_only(session: Session, settings: Settings) -> None:
    """Cambiar SOLO los resultados de validación y fuera de muestra no altera qué variantes
    se eligen en entrenamiento (la elección no mira esos tramos)."""
    scope = make_scope(session)
    insert_diag_trades(session, scope, 200, seed=1, kind="sl_ajustado")
    ps.run_patterns(session, settings, scope.version_id)
    _, first = dsvc.compute_diagnosis(session, settings, scope.version_id)
    train_choice = {
        c["clave"]: c["tramos"]["entrenamiento"]["sugerida"]["expectancy_r"]
        for c in first.report["cambios"]
    }
    assert train_choice
    # Cada cambio trae su tramo de entrenamiento (donde se eligió) rotulado como optimista.
    md = diagnosis_report.markdown(first.report, dsvc.run_view(first))
    assert "Entrenamiento (donde se eligió: optimista)" in md


def test_no_split_means_no_validated(session: Session, settings: Settings) -> None:
    scope = make_scope(session)
    insert_diag_trades(session, scope, 12, seed=1, kind="sl_ajustado")
    status, run = dsvc.compute_diagnosis(session, settings, scope.version_id)
    assert status == dsvc.CREATED
    assert run.report["resumen"]["suficiencia"]["estado"] != "suficiente"
    assert all(c["estado"] != dsvc.dg.VALIDATED for c in run.report["cambios"])


def test_idempotent_and_hash_unique_per_version(session: Session, settings: Settings) -> None:
    scope, run = _diagnose(session, settings, "sl_ajustado", n=120)
    status, again = dsvc.compute_diagnosis(session, settings, scope.version_id)
    assert status == dsvc.UNCHANGED and again.id == run.id
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            DiagnosisRun(
                bot_version_id=scope.version_id,
                inputs_hash=run.inputs_hash,
                diagnosis_version="x",
                n_trades=0,
                trigger="MANUAL",
                params={},
                report={},
            )
        )
        session.flush()
    # Un cambio de parámetros distinto: otra huella, otra fila (la historia no se pisa).
    other = settings.model_copy(update={"diagnosis_tp_reach_fraction": 0.5})
    status, run2 = dsvc.compute_diagnosis(session, other, scope.version_id)
    assert status == dsvc.CREATED and run2.id != run.id


# CLI y worker ------------------------------------------------------------------------------


def test_cli_diagnose(
    session: Session,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    scope = make_scope(session)
    insert_diag_trades(session, scope, 200, seed=1, kind="sl_ajustado")
    ps.run_patterns(session, settings, scope.version_id)

    @contextmanager
    def scope_() -> Iterator[Session]:
        yield session

    monkeypatch.setattr(cli, "session_scope", scope_)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    with pytest.raises(SystemExit):
        cli.main(["diagnose", "--version", str(scope.version_id), "--report-only"])
    cli.main(["diagnose", "--version", str(scope.version_id)])
    out = capsys.readouterr().out
    assert "Diagnóstico: calculado" in out
    assert "## Qué está fallando" in out and "[VALIDADA]" in out and "## Qué funciona mejor" in out
    cli.main(["diagnose", "--version", str(scope.version_id)])
    assert "sin cambios" in capsys.readouterr().out
    cli.main(["diagnose", "--version", str(scope.version_id), "--report-only", "--html"])
    html = capsys.readouterr().out
    assert html.startswith("<!doctype html>") and "<script" not in html
    with pytest.raises(SystemExit):
        cli.main(["diagnose", "--version", str(uuid.uuid4())])


@pytest.fixture(scope="module")
def committed(engine: Engine) -> dict:
    settings = Settings(database_url="postgresql://x/y", admin_token=ADMIN_TOKEN)
    with Session(engine, expire_on_commit=False) as session:
        scope = make_scope(session)
        # Lejos de las fechas de otras pruebas (el backfill revisa todo lo que no tiene análisis).
        insert_diag_trades(
            session,
            scope,
            200,
            seed=1,
            kind="sl_ajustado",
            start=datetime(2025, 6, 2, 0, 0, 30, tzinfo=UTC),
        )
        session.commit()
    with Session(engine, expire_on_commit=False) as session:
        ps.run_patterns(session, settings, scope.version_id)
        session.commit()
    return {"scope": scope}


def test_worker_diagnosis_pass(
    committed: dict, session_factory: sessionmaker[Session], worker_settings: Settings
) -> None:
    scope = committed["scope"]
    wide = worker_settings.model_copy(update={"patterns_max_scopes_per_pass": 1000})
    stats = dsvc.diagnosis_pass(session_factory, wide)
    assert stats["error"] == 0 and stats[dsvc.CREATED] >= 1
    with session_factory() as session:
        runs = list(
            session.scalars(
                select(DiagnosisRun).where(DiagnosisRun.bot_version_id == scope.version_id)
            )
        )
        assert len(runs) == 1 and runs[0].trigger == dsvc.WORKER
    again = dsvc.diagnosis_pass(session_factory, wide)
    assert again["al_dia"] >= 1 and again[dsvc.CREATED] == 0  # sin operaciones nuevas
    with session_factory() as session:
        n = session.scalar(
            select(func.count()).where(DiagnosisRun.bot_version_id == scope.version_id)
        )
        assert n == 1


def test_diagnosis_api(client: TestClient, committed: dict) -> None:
    scope = committed["scope"]
    url = f"/v1/bots/{scope.bot_id}/versions/{scope.version_id}/diagnosis"
    assert client.get(url).status_code in (401, 403)
    first = client.get(url, headers=admin()).json()
    if first["latest"] is None:
        assert "diagnose" in first["message"]
        posted = client.post(url, headers=admin()).json()
        assert posted["status"] in ("creado", "sin_cambios") and posted["run"]["report"]
        first = client.get(url, headers=admin()).json()
    latest = first["latest"]
    assert latest["report"]["cambios"] and first["history"][0]["id"] == latest["id"]
    assert client.post(url, headers=admin()).json()["status"] == "sin_cambios"
    assert client.get(url, params={"limit": 0}, headers=admin()).status_code == 422
    other = f"/v1/bots/{uuid.uuid4()}/versions/{scope.version_id}/diagnosis"
    assert client.get(other, headers=admin()).status_code == 404
    assert client.post(other, headers=admin()).status_code == 404


def test_dashboard_diagnosis_page_download_and_experiment(
    make_dash,
    committed: dict,
    session_factory,
    worker_settings,  # noqa: F811
) -> None:
    scope = committed["scope"]
    with session_factory() as session, session.begin():
        dsvc.compute_diagnosis(session, worker_settings, scope.version_id)
    with session_factory() as session, session.begin():
        session.add(
            Deployment(
                bot_version_id=scope.version_id,
                account_id=scope.account_id,
                symbol=scope.symbol,
                magic_number=909,
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
    dash = make_dash()
    assert dash.get("/dashboard/diagnostico").status_code == 303
    assert login(dash).status_code == 303
    page = dash.get("/dashboard/diagnostico")
    assert (
        page.status_code == 200 and 'href="/dashboard/diagnostico" aria-current="page"' in page.text
    )
    assert f"/dashboard/diagnostico/{scope.version_id}" in page.text
    detail = dash.get(f"/dashboard/diagnostico/{scope.version_id}")
    assert detail.status_code == 200
    html = detail.text
    assert "Qué está fallando" in html and "VALIDADA" in html and "aciertos" in html
    assert "style=" not in html and "<script>" not in html
    md = dash.get(f"/dashboard/diagnostico/{scope.version_id}/descargar")
    assert md.status_code == 200 and "attachment" in md.headers["content-disposition"]
    assert md.text.startswith("# Diagnóstico")
    as_html = dash.get(f"/dashboard/diagnostico/{scope.version_id}/descargar?formato=html")
    assert as_html.headers["content-disposition"].endswith('.html"')
    assert "<script" not in as_html.text
    assert dash.get(f"/dashboard/diagnostico/{uuid.uuid4()}").status_code == 404
    # Crear experimento (POST con CSRF) desde una sugerencia.
    run_id = re_run_id(html)
    csrf = _session_csrf(dash)
    denied = dash.post(
        f"/dashboard/diagnostico/{run_id}/experimento", data={"clave": "salida:sl_1_25"}
    )
    assert denied.status_code == 403
    ok = dash.post(
        f"/dashboard/diagnostico/{run_id}/experimento",
        data={"clave": "salida:sl_1_25", "csrf_token": csrf},
    )
    assert ok.status_code == 303 and ok.headers["location"].startswith("/dashboard/laboratorio/")
    assert dash.get(ok.headers["location"]).status_code == 200
    bad = dash.post(
        f"/dashboard/diagnostico/{run_id}/experimento", data={"clave": "nada", "csrf_token": csrf}
    )
    assert bad.status_code == 404
    home = dash.get("/dashboard").text
    assert "Diagnóstico de bots" in home


def re_run_id(html: str) -> str:
    import re

    match = re.search(r"/dashboard/diagnostico/([0-9a-f-]{36})/experimento", html)
    assert match, "falta el formulario de crear experimento"
    return match.group(1)


# Hechos previos a la entrada y TRAMPA_ACTIVA con EMA ---------------------------------------


def _open_trade(session: Session, scope: Scope, pre_entry: dict | None) -> Trade:
    now = datetime.now(UTC)
    trade = Trade(
        account_id=scope.account_id,
        bot_version_id=scope.version_id,
        position_id=uuid.uuid4().int % 2**62,
        magic_number=909,
        symbol=scope.symbol,
        direction=Direction.BUY,
        order_type="MARKET",
        source=TradeSource.DEMO,
        status=TradeStatus.OPEN,
        symbol_point=Decimal("0.01"),
        symbol_digits=2,
        entry_time=now - timedelta(minutes=2),
        entry_price=Decimal("20000.50"),
        initial_sl=Decimal("19900"),
        initial_volume=Decimal("0.10"),
        max_volume=Decimal("0.10"),
        data_quality={},
    )
    session.add(trade)
    session.flush()
    inputs = {} if pre_entry is None else {"hechos_previos": pre_entry}
    session.add(
        TradeDna(
            trade_id=trade.trade_id,
            dna_version=1,
            feature_set_version=1,
            input_hash=uuid.uuid4().hex * 2,
            source=DataOrigin.SERVER,
            data_cutoff=trade.entry_time,
            features={"tendencia_h1_rel": "A_FAVOR", "sesion": "LONDRES"},
            null_reasons={},
            data_quality={},
            inputs=inputs,
        )
    )
    session.flush()
    return trade


def _facts(favor: bool) -> dict:
    return {
        "version": PRE_ENTRY_VERSION,
        "hechos": {"PRECIO_VS_EMA200_H1": {"a_favor": favor, "posicion": "DEBAJO"}},
        "sin_datos": {},
        "incompleto": False,
    }


def test_trap_active_uses_ema_facts_computed_at_open(session: Session, settings: Settings) -> None:
    scope = make_scope(session)
    insert_trades(session, scope, 360, seed=3, planted=True, facts=True)
    ps.run_patterns(session, settings, scope.version_id)
    ema_trap = session.scalar(
        select(Hypothesis).where(
            Hypothesis.bot_version_id == scope.version_id,
            Hypothesis.status == HypothesisStatus.VALIDATED,
            Hypothesis.condition_key.contains('"variable":"hecho_ema200_h1_a_favor"'),
            Hypothesis.condition_key.contains('"valor":false'),
        )
    )
    assert ema_trap is not None, "la trampa basada en la EMA200 H1 debe validarse"
    against = _open_trade(session, scope, _facts(False))
    with_trend = _open_trade(session, scope, _facts(True))
    legacy = _open_trade(session, scope, None)  # DNA antiguo, sin hechos previos
    al.evaluate(session, settings, datetime.now(UTC))

    def fired(trade: Trade) -> list[Alert]:
        return list(
            session.scalars(
                select(Alert).where(
                    Alert.dedup_key.like(f"{al.TRAP_ACTIVE}:{trade.trade_id}%"),
                    Alert.hypothesis_id == ema_trap.id,
                )
            )
        )

    assert len(fired(against)) == 1
    assert not fired(with_trend) and not fired(legacy)
