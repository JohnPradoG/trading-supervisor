"""Fase 9 de punta a punta en PostgreSQL: split cronológico congelado, búsqueda solo con
entrenamiento, FDR, validación, fuera de muestra una sola vez, forward (DECAYED), reglas de la
fase 6, API, CLI, worker y dashboard. Datos sintéticos: una trampa plantada (entrar contra la
tendencia H1 pierde -0.8 R de media) o solo ruido."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from supervisor import cli
from supervisor.analytics import pattern_search as ps
from supervisor.analytics import patterns as pt
from supervisor.analytics.binning import quantile_edges
from supervisor.config import Settings
from supervisor.models import DataSplit, Hypothesis, PatternRun, PatternTest
from supervisor.models.enums import HypothesisStatus, SplitSegment
from supervisor.services import patterns as patterns_service
from tests.conftest import ADMIN_TOKEN
from tests.integration.pattern_helpers import Scope, insert_trades, make_scope
from tests.integration.test_dashboard import login
from tests.integration.test_dashboard import make_dash as make_dash  # fixture
from tests.integration.trading_helpers import admin

PLANTED = pt.Condition((pt.Clause("tendencia_h1_rel", "eq", value="EN_CONTRA"),)).key()
RULE_KEY = ps.rule_key("H_CONTRA_TENDENCIA_H1", 1)


@pytest.fixture
def settings(worker_settings: Settings) -> Settings:
    return worker_settings


def _hypotheses(session: Session, scope: Scope) -> list[Hypothesis]:
    return list(
        session.scalars(select(Hypothesis).where(Hypothesis.bot_version_id == scope.version_id))
    )


def _by_key(session: Session, scope: Scope, key: str) -> Hypothesis:
    return next(h for h in _hypotheses(session, scope) if h.condition_key == key)


def _tests(session: Session, h: Hypothesis) -> list[PatternTest]:
    return list(
        session.scalars(
            select(PatternTest)
            .where(PatternTest.hypothesis_id == h.id)
            .order_by(PatternTest.run_at)
        )
    )


def _planted_run(session: Session, settings: Settings) -> tuple[Scope, ps.RunReport]:
    scope = make_scope(session)
    insert_trades(session, scope, 360, seed=3, planted=True, facts=True)
    return scope, ps.run_patterns(session, settings, scope.version_id)


# Pipeline -----------------------------------------------------------------------------------


def test_planted_trap_is_found_and_validated_out_of_sample(
    session: Session, settings: Settings
) -> None:
    scope, report = _planted_run(session, settings)
    assert report.status == ps.COMPLETED
    run = report.run
    # Transparencia: cuántas candidatas se generaron y probaron (familia del FDR).
    assert run.candidates_tested > 50 and run.candidates_generated >= run.candidates_tested
    assert (run.n_train, run.n_validation, run.n_oos, run.n_forward) == (216, 72, 72, 0)
    split = session.get(DataSplit, run.split_id)
    assert split.train_end < split.validation_end < split.oos_end

    trap = _by_key(session, scope, PLANTED)
    assert trap.status == HypothesisStatus.VALIDATED and trap.kind == pt.TRAP
    assert trap.statement == "Entrar contra la tendencia H1"
    tests = {t.segment: t for t in _tests(session, trap)}
    assert set(tests) == {SplitSegment.TRAIN, SplitSegment.VALIDATION, SplitSegment.OOS}
    assert tests[SplitSegment.TRAIN].tests_in_family == run.candidates_tested
    assert float(tests[SplitSegment.TRAIN].p_adjusted) < 0.05
    assert tests[SplitSegment.OOS].n >= settings.patterns_min_n_oos
    assert float(tests[SplitSegment.OOS].ci_high) < 0 and tests[SplitSegment.OOS].passed
    assert trap.forward_from == split.oos_end

    # Todo lo validado describe la trampa plantada (o su hecho equivalente de la fase 6).
    validated = [h for h in _hypotheses(session, scope) if h.status == HypothesisStatus.VALIDATED]
    assert validated and all(h.kind == pt.TRAP for h in validated)
    assert all(
        "EN_CONTRA" in h.condition_key or "ema200_h1" in h.condition_key or h.origin == "REGLA"
        for h in validated
    )


def test_phase6_rule_is_tested_and_exposed_by_lookup(session: Session, settings: Settings) -> None:
    scope, _ = _planted_run(session, settings)
    rule = _by_key(session, scope, RULE_KEY)
    assert rule.origin == "REGLA" and rule.kind == pt.TRAP
    assert rule.status == HypothesisStatus.VALIDATED
    assert rule.condition_spec["regla"] == "H_CONTRA_TENDENCIA_H1"
    lookup = patterns_service.rule_validation(session, scope.version_id, scope.symbol)
    assert lookup[("H_CONTRA_TENDENCIA_H1", 1)]["etiqueta"] == "trampa validada"
    # Reglas sobre lo que pasó durante la operación (MAE, MFE...) no se contrastan.
    codes = {h.condition_spec["regla"] for h in _hypotheses(session, scope) if h.origin == "REGLA"}
    assert "H_SL_TP_MAL_UBICADO" not in codes and "H_PERDEDORA_CON_MFE" not in codes
    # Sin muestra (ninguna operación con noticias): queda PROPOSED, sin pruebas.
    news = _by_key(session, scope, ps.rule_key("H_NOTICIA_CERCANA", 1))
    assert news.status == HypothesisStatus.PROPOSED and not _tests(session, news)
    assert patterns_service.rule_validation(session, uuid.uuid4(), None) == {}


def test_noise_yields_no_validated_pattern(session: Session, settings: Settings) -> None:
    for seed in (11, 12):
        scope = make_scope(session)
        insert_trades(session, scope, 360, seed=seed, planted=False, facts=True)
        report = ps.run_patterns(session, settings, scope.version_id)
        assert report.status == ps.COMPLETED and report.run.candidates_tested > 50
        assert report.counts["validadas"] == 0
        statuses = {h.status for h in _hypotheses(session, scope)}
        assert HypothesisStatus.VALIDATED not in statuses
        page = patterns_service.overview(session, settings, scope.version_id)
        assert page["trampas_validadas"] == 0 and page["aviso"] is None


def test_small_data_warning_and_minimum_sample(session: Session, settings: Settings) -> None:
    scope = make_scope(session)
    insert_trades(session, scope, 60, seed=3, planted=True)
    report = ps.run_patterns(session, settings, scope.version_id)
    assert report.status == ps.INSUFFICIENT
    assert report.message.startswith(
        "Con 60 operaciones cerradas todavía no se puede validar ninguna trampa; se necesitan "
        "al menos 100"
    )
    ps.run_patterns(session, settings, scope.version_id)  # mismo n: no se registra otra vez
    runs = session.scalar(select(func.count()).where(PatternRun.bot_version_id == scope.version_id))
    assert runs == 1 and not _hypotheses(session, scope)
    assert (
        session.scalar(
            select(func.count())
            .select_from(DataSplit)
            .where(DataSplit.scope["bot_version_id"].astext == str(scope.version_id))
        )
        == 0
    )
    assert patterns_service.overview(session, settings, scope.version_id)["aviso"] == (
        report.message
    )

    # Con una muestra mínima por tramo inalcanzable nada pasa de entrenamiento a validada.
    strict = settings.model_copy(update={"patterns_min_n_validation": 1000})
    other = make_scope(session)
    insert_trades(session, other, 360, seed=3, planted=True)
    report = ps.run_patterns(session, strict, other.version_id)
    assert report.counts["pasan_entrenamiento"] > 0 and report.counts["validadas"] == 0
    trap = _by_key(session, other, PLANTED)
    assert trap.status == HypothesisStatus.REJECTED and "muestra insuficiente" in trap.status_note


def test_out_of_sample_never_leaks_into_search(session: Session, settings: Settings) -> None:
    """Mismos datos de entrenamiento y validación, fuera de muestra distinto: la búsqueda (sus
    candidatas, tramos y p-valores) es idéntica."""
    results = []
    for oos_seed in (101, 202):
        scope = make_scope(session)
        insert_trades(session, scope, 288, seed=3, planted=True)
        insert_trades(session, scope, 72, seed=oos_seed, planted=False, start_index=288)
        report = ps.run_patterns(session, settings, scope.version_id)
        train = {
            h.condition_key: float(t.p_adjusted)
            for h in _hypotheses(session, scope)
            for t in _tests(session, h)
            if t.segment == SplitSegment.TRAIN and h.origin == "BUSQUEDA"
        }
        results.append((report.run.candidates_tested, report.run.candidates_generated, train))
        obs = ps.load_observations(session, scope.version_id, None, settings)
        split = session.get(DataSplit, report.run.split_id)
        train_obs = ps.segments(obs, split)[SplitSegment.TRAIN]
        edges = quantile_edges([o.values["rsi14"] for o in train_obs], 4)
        for h in _hypotheses(session, scope):
            for clause in (h.condition_spec or {}).get("clausulas", []):
                if clause["variable"] == "rsi14":
                    assert {clause.get("desde"), clause.get("hasta")} <= {None, *edges}
    assert results[0] == results[1]
    assert results[0][2]  # hubo candidatas que pasaron entrenamiento


def test_rerun_without_new_data_does_not_retest_oos(session: Session, settings: Settings) -> None:
    scope, _ = _planted_run(session, settings)
    oos_before = session.scalar(
        select(func.count())
        .select_from(PatternTest)
        .join(Hypothesis, Hypothesis.id == PatternTest.hypothesis_id)
        .where(Hypothesis.bot_version_id == scope.version_id, PatternTest.segment == "OOS")
    )
    again = ps.run_patterns(session, settings, scope.version_id)
    assert again.status == "SIN_DATOS_NUEVOS"
    oos_after = session.scalar(
        select(func.count())
        .select_from(PatternTest)
        .join(Hypothesis, Hypothesis.id == PatternTest.hypothesis_id)
        .where(Hypothesis.bot_version_id == scope.version_id, PatternTest.segment == "OOS")
    )
    assert oos_after == oos_before
    splits = session.scalar(
        select(func.count())
        .select_from(DataSplit)
        .where(DataSplit.scope["bot_version_id"].astext == str(scope.version_id))
    )
    assert splits == 1

    # La base de datos impide una segunda prueba OOS y modificar pruebas o ejecuciones.
    trap = _by_key(session, scope, PLANTED)
    first = next(t for t in _tests(session, trap) if t.segment == SplitSegment.OOS)
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            PatternTest(
                hypothesis_id=trap.id,
                split_id=first.split_id,
                segment=SplitSegment.OOS,
                n=1,
                tests_in_family=1,
                method="x",
                details={},
            )
        )
        session.flush()
    for stmt in (
        "UPDATE pattern_tests SET n = n + 1 WHERE id = :id",
        "UPDATE pattern_runs SET n_trades = 0 WHERE bot_version_id = :v",
        "UPDATE data_splits SET notes = 'x' WHERE id = :s",
    ):
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(
                text(stmt), {"id": first.id, "v": scope.version_id, "s": first.split_id}
            )


def test_new_trades_create_new_split_without_duplicates(
    session: Session, settings: Settings
) -> None:
    scope, first = _planted_run(session, settings)
    insert_trades(session, scope, 19, seed=8, planted=True, facts=True, start_index=360)
    assert ps.run_patterns(session, settings, scope.version_id).status == "SIN_DATOS_NUEVOS"
    insert_trades(session, scope, 1, seed=9, planted=True, facts=True, start_index=379)
    second = ps.run_patterns(session, settings, scope.version_id)
    assert second.status == ps.COMPLETED and second.split.id != first.split.id
    assert second.split.oos_end > first.split.oos_end and second.run.n_trades == 380
    assert second.counts["ya_existentes"] > 0  # la trampa viva no se duplica
    live = [h for h in _hypotheses(session, scope) if h.status in ps.LIVE]
    keys = [h.condition_key for h in live]
    assert len(keys) == len(set(keys)) and PLANTED in keys


def test_validated_trap_decays_with_forward_data(session: Session, settings: Settings) -> None:
    scope, _ = _planted_run(session, settings)
    no_resplit = settings.model_copy(update={"patterns_new_trades": 1000})
    # El mercado cambia: entrar contra la tendencia H1 ahora gana.
    insert_trades(
        session, scope, 90, seed=21, planted=True, facts=True, flipped=True, start_index=360
    )
    report = ps.run_patterns(session, no_resplit, scope.version_id)
    assert report.status == "SIN_DATOS_NUEVOS" and report.counts["caducadas"] >= 1
    trap = _by_key(session, scope, PLANTED)
    assert trap.status == HypothesisStatus.DECAYED and trap.status_note.startswith("forward")
    forward = [t for t in _tests(session, trap) if t.segment == SplitSegment.FORWARD]
    assert len(forward) == 1 and forward[0].passed is False and float(forward[0].ci_low) > 0
    assert (
        patterns_service.list_patterns(
            session, version_id=scope.version_id, status=(HypothesisStatus.DECAYED)
        )[0]["label"]
        == "caducada"
    )


def test_report_answers_section_10_questions(session: Session, settings: Settings) -> None:
    scope, _ = _planted_run(session, settings)
    from supervisor.models import BotVersion

    version = session.get(BotVersion, scope.version_id)
    report = patterns_service.version_report(session, settings, version.bot_id, version.id)
    worst = report["peores_condiciones"]
    assert worst[0]["label"] == "trampa validada"
    assert worst[0]["entrenamiento"]["n"] > 0 and worst[0]["fuera_de_muestra"]["n"] > 0
    assert all(i["label"] in ("trampa validada", patterns_service.CANDIDATE) for i in worst)
    wl = report["ganadoras_vs_perdedoras"]
    assert wl["ganadoras"]["n"] + wl["perdedoras"]["n"] == 360
    assert wl["diferencias"]
    # Las perdedoras llegan a 1 R de MAE y la trampa pierde casi siempre: más drawdown.
    assert any("contra la tendencia H1" in d["condicion"] for d in report["aumentan_drawdown"])
    assert report["resumen"]["candidatas_probadas_ultima"] > 50
    with pytest.raises(patterns_service.NotFound):
        patterns_service.version_report(session, settings, uuid.uuid4(), version.id)


def test_cli_patterns(
    session: Session,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    scope = make_scope(session)
    insert_trades(session, scope, 360, seed=3, planted=True, facts=True)

    @contextmanager
    def scope_() -> Iterator[Session]:
        yield session

    monkeypatch.setattr(cli, "session_scope", scope_)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    cli.main(["patterns", "--version", str(scope.version_id)])
    out = capsys.readouterr().out
    assert "Búsqueda: COMPLETADA" in out and "candidatas probadas" in out
    assert "[TRAMPA VALIDADA] Entrar contra la tendencia H1" in out
    assert "fuera de muestra: n=" in out and "Ganadoras frente a perdedoras" in out
    cli.main(["patterns", "--version", str(scope.version_id), "--report-only"])
    assert "Búsqueda:" not in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["patterns", "--version", str(uuid.uuid4())])


# API, worker y dashboard (datos confirmados: la API usa su propia conexión) -----------------


@pytest.fixture(scope="module")
def committed(engine: Engine) -> dict:
    settings = Settings(database_url="postgresql://x/y", admin_token=ADMIN_TOKEN)
    with Session(engine, expire_on_commit=False) as session:
        scope = make_scope(session)
        insert_trades(session, scope, 360, seed=3, planted=True, facts=True)
        small = make_scope(session)
        insert_trades(session, small, 40, seed=4, planted=True)
        session.commit()
    with Session(engine, expire_on_commit=False) as session:
        ps.run_patterns(session, settings, scope.version_id)
        session.commit()
        trap = _by_key(session, scope, PLANTED)
    return {"scope": scope, "small": small, "trap_id": trap.id}


def test_patterns_api(client: TestClient, committed: dict) -> None:
    scope: Scope = committed["scope"]
    assert client.get("/v1/patterns").status_code == 401
    items = client.get(
        "/v1/patterns",
        params={"version_id": str(scope.version_id), "kind": "trap"},
        headers=admin(),
    ).json()
    assert items[0]["label"] == "trampa validada" and items[0]["validated"] is True
    labels = [i["label"] for i in items]
    # Validadas primero; ninguna candidata se presenta como trampa.
    assert labels == sorted(labels, key=lambda x: x != "trampa validada")
    assert set(labels) <= {"trampa validada", "candidata, no validada", "rechazada", "caducada"}
    bot_items = client.get(
        "/v1/patterns", params={"bot_id": str(scope.bot_id), "status": "VALIDATED"}, headers=admin()
    ).json()
    assert {i["id"] for i in bot_items} >= {str(committed["trap_id"])}
    assert client.get("/v1/patterns", params={"kind": "x"}, headers=admin()).status_code == 422

    detail = client.get(f"/v1/patterns/{committed['trap_id']}", headers=admin()).json()
    assert [t["segmento"] for t in detail["tests"]] == ["TRAIN", "VALIDATION", "OOS"]
    assert detail["run"]["candidates_tested"] == detail["entrenamiento"]["pruebas_en_familia"]
    assert detail["split"]["train_end"] < detail["split"]["oos_end"]
    assert client.get(f"/v1/patterns/{uuid.uuid4()}", headers=admin()).status_code == 404

    url = f"/v1/bots/{scope.bot_id}/versions/{scope.version_id}/report"
    report = client.get(url, headers=admin()).json()
    assert report["resumen"]["trampas_validadas"] >= 1
    assert report["peores_condiciones"][0]["fuera_de_muestra"]["expectancy_r"] < 0
    assert report["ganadoras_vs_perdedoras"]["ganadoras"]["n"] > 0
    assert (
        client.get(
            f"/v1/bots/{uuid.uuid4()}/versions/{scope.version_id}/report", headers=admin()
        ).status_code
        == 404
    )

    small: Scope = committed["small"]
    report = client.get(
        f"/v1/bots/{small.bot_id}/versions/{small.version_id}/report", headers=admin()
    ).json()
    assert report["resumen"]["aviso"].startswith("Con 40 operaciones cerradas todavía no")
    assert report["peores_condiciones"] == []


def test_worker_patterns_pass(
    committed: dict, session_factory: sessionmaker[Session], worker_settings: Settings
) -> None:
    wide = worker_settings.model_copy(update={"patterns_max_scopes_per_pass": 1000})
    stats = ps.patterns_pass(session_factory, wide)
    assert stats["error"] == 0
    assert stats["SIN_DATOS_NUEVOS"] >= 1 and stats[ps.INSUFFICIENT] >= 1
    with session_factory() as session:
        trap = session.get(Hypothesis, committed["trap_id"])
        assert trap.status == HypothesisStatus.VALIDATED


def test_dashboard_traps_page(make_dash, committed: dict) -> None:  # noqa: F811 (fixture importada)
    dash = make_dash()
    assert dash.get("/dashboard/trampas").status_code == 303
    assert login(dash).status_code == 303
    page = dash.get("/dashboard/trampas")
    assert page.status_code == 200
    html = page.text
    assert 'href="/dashboard/trampas" aria-current="page"' in html
    assert "trampa validada</span> Entrar contra la tendencia H1" in html
    assert "candidatas probadas en la última búsqueda" in html
    assert "Con 40 operaciones cerradas todavía no se puede validar ninguna trampa" in html
    assert "style=" not in html and "<script>" not in html
    home = dash.get("/dashboard").text
    assert "Trampas activas" in home and 'href="/dashboard/trampas"' in home
