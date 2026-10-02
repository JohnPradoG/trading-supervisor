"""Fase 10 (laboratorio) en PostgreSQL: experimentos con revisiones de solo inserción,
filtro contrafactual sobre operaciones reales con el split congelado de la fase 9, backtests
del Strategy Tester que nunca se mezclan con lo real, comparación de brazos y de versiones,
API, CLI y dashboard. Los datos son sintéticos (pattern_helpers: una trampa plantada) y los
archivos del probador están construidos a mano para las pruebas, no exportados de MT5."""

import io
import json
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from supervisor import cli
from supervisor.analytics import pattern_search as ps
from supervisor.analytics import patterns as pt
from supervisor.config import Settings
from supervisor.models import (
    BacktestRun,
    BotVersion,
    Experiment,
    ExperimentResult,
    Hypothesis,
    Trade,
)
from supervisor.models.enums import ExperimentStatus
from supervisor.services import lab as lab_service
from tests.conftest import ADMIN_TOKEN
from tests.integration.pattern_helpers import START, Scope, insert_trades, make_scope
from tests.integration.test_dashboard import login
from tests.integration.test_dashboard import make_dash as make_dash  # fixture
from tests.integration.trading_helpers import admin

PLANTED = pt.Condition((pt.Clause("tendencia_h1_rel", "eq", value="EN_CONTRA"),)).key()
TREND_FILTER = {
    "modo": "excluir",
    "condicion": {
        "tipo": "busqueda",
        "clausulas": [{"variable": "tendencia_h1_rel", "op": "eq", "valor": "EN_CONTRA"}],
    },
}


def backtest_csv(n: int = 40, sep: str = ";", decimal: str = ",") -> str:
    """CSV de transacciones CONSTRUIDO para las pruebas con las columnas de la tabla de deals
    del Strategy Tester: depósito, y n operaciones (entrada + salida) que ganan 15 o pierden
    10 (comisión 0.35 por lado)."""

    def num(value: float) -> str:
        return f"{value:.2f}".replace(".", decimal)

    rows = [
        sep.join(
            [
                "Time",
                "Deal",
                "Symbol",
                "Type",
                "Direction",
                "Volume",
                "Price",
                "Order",
                "Commission",
                "Swap",
                "Profit",
                "Balance",
                "Comment",
            ]
        ),
        sep.join(["2026.01.02 00:00:00", "1", "", "balance", "", "", "", "", "0", "0"])
        + sep
        + num(10000)
        + sep
        + num(10000)
        + sep,
    ]
    balance = 10000.0
    for i in range(n):
        day = 3 + i // 8
        hour = 1 + (i % 8) * 2
        profit = 15.0 if i % 5 in (0, 2) else -10.0
        balance += profit - 0.7
        opened = f"2026.01.{day:02d} {hour:02d}:00:00"
        closed = f"2026.01.{day:02d} {hour:02d}:45:00"
        rows.append(
            sep.join(
                [opened, str(10 + 2 * i), "USTEC_x100", "buy", "in", num(0.1), num(20000)]
                + [str(10 + 2 * i), num(-0.35), num(0), num(0), num(balance - profit + 0.35), ""]
            )
        )
        rows.append(
            sep.join(
                [closed, str(11 + 2 * i), "USTEC_x100", "sell", "out", num(0.1), num(20010)]
                + [str(11 + 2 * i), num(-0.35), num(0), num(profit), num(balance), "tp"]
            )
        )
    return "\n".join(rows) + "\n"


@pytest.fixture
def settings(worker_settings: Settings) -> Settings:
    return worker_settings.model_copy(update={"lab_bootstrap_samples": 200})


def _trap(session: Session, scope: Scope):
    return next(
        h
        for h in session.scalars(
            select(Hypothesis).where(Hypothesis.bot_version_id == scope.version_id)
        )
        if h.condition_key == PLANTED
    )


def _planted(session: Session, settings: Settings) -> Scope:
    scope = make_scope(session)
    insert_trades(session, scope, 360, seed=3, planted=True, facts=True)
    ps.run_patterns(session, settings, scope.version_id)
    return scope


# Servicio ----------------------------------------------------------------------------------


def test_trap_filter_counterfactual_out_of_sample(session: Session, settings: Settings) -> None:
    scope = _planted(session, settings)
    trap = _trap(session, scope)
    exp = lab_service.create_from_trap(session, settings, trap.id)
    assert exp.code == f"#{exp.number:03d}" and exp.status == ExperimentStatus.RUNNING
    assert exp.filter_spec["modo"] == "excluir"
    revision = session.scalars(
        select(ExperimentResult).where(ExperimentResult.experiment_id == exp.id)
    ).one()
    res = revision.results
    assert revision.revision == 1 and revision.kind == "FILTRO"
    assert res["titular"] == "fuera_de_muestra" and res["split"]["origen"] == "hipotesis"
    assert res["split"]["id"] == str(trap.split_id)
    oos, ins, total = (res["tramos"][k] for k in ("fuera_de_muestra", "dentro_de_muestra", "todo"))
    # Particiones exactas: dentro + fuera = todo, filtrado + evitadas = original.
    assert oos["original"]["n_trades"] + ins["original"]["n_trades"] == 360
    for seg in (oos, ins, total):
        assert (
            seg["filtrado"]["n_trades"] + seg["evitadas"]["n_trades"]
            == (seg["original"]["n_trades"])
        )
        assert seg["sin_dato"] == 0
    # Fuera de muestra, saltarse las entradas contra tendencia mejora la expectativa.
    assert oos["filtrado"]["expectancy_r"] > oos["original"]["expectancy_r"]
    assert oos["evitadas"]["expectancy_r"] < 0
    assert oos["evitadas_vs_mantenidas"]["ic95"][1] < 0
    assert oos["filtrado"]["max_drawdown"] <= oos["original"]["max_drawdown"]
    low, high = oos["filtrado"]["max_drawdown_ic95"]
    assert low <= high and oos["original"]["profit_factor_ic95"] is not None
    assert res["avisos"][0].startswith("Dentro de muestra es optimista")
    assert any("contrafactual" in w and "en vivo es desconocido" in w for w in res["avisos"])
    assert res["filtro"]["texto"] == (
        "Saltarse las entradas que cumplan: «Entrar contra la tendencia H1»"
    )

    # Determinista: repetirlo da los mismos números en otra revisión.
    again = lab_service.run_filter(session, settings, exp)
    assert again.revision == 2
    assert again.results["tramos"] == res["tramos"]


def test_manual_filter_without_split_and_validation(session: Session, settings: Settings) -> None:
    scope = make_scope(session)
    insert_trades(session, scope, 40, seed=4, planted=True)
    exp = lab_service.create_experiment(
        session,
        title="Filtro H1",
        base_version_id=scope.version_id,
        change_description="Agregar filtro de tendencia H1",
        filter_spec=TREND_FILTER,
    )
    res = lab_service.run_filter(session, settings, exp).results
    assert res["titular"] == "todo" and res["split"] is None
    assert set(res["tramos"]) == {"todo"}
    assert res["avisos"][0].startswith("Todavía no hay split congelado")
    assert any("no viene de una hipótesis validada" in w for w in res["avisos"])
    assert any("muestra pequeña" in w for w in res["avisos"])

    only = lab_service.create_experiment(
        session,
        title="Solo a favor",
        base_version_id=scope.version_id,
        change_description="Solo entradas con RSI bajo",
        filter_spec={
            "modo": "solo",
            "condicion": {"clausulas": [{"variable": "rsi14", "op": "range", "hasta": 40}]},
        },
    )
    res = lab_service.run_filter(session, settings, only).results["tramos"]["todo"]
    assert res["filtrado"]["n_trades"] < 40
    assert only.filter_spec["condicion"]["clausulas"][0]["tramo"] == "[-∞, 40)"

    bad = [
        {"modo": "otro", "condicion": TREND_FILTER["condicion"]},
        {"modo": "excluir", "condicion": {"clausulas": [{"variable": "inventada", "op": "eq"}]}},
        {"condicion": {"clausulas": [{"variable": "rsi14", "op": "eq", "valor": "x"}]}},
        {
            "condicion": {
                "clausulas": [{"variable": "rsi14", "op": "range", "desde": 5, "hasta": 1}]
            }
        },
        {"condicion": {"clausulas": [{"variable": "fvg_cercano", "op": "eq", "valor": "sí"}]}},
        {"condicion": {"clausulas": []}},
    ]
    for spec in bad:
        with pytest.raises(lab_service.ServiceError):
            lab_service.create_experiment(
                session,
                title="x",
                base_version_id=scope.version_id,
                change_description="x",
                filter_spec=spec,
            )
    no_filter = lab_service.create_experiment(
        session, title="Solo texto", base_version_id=scope.version_id, change_description="x"
    )
    with pytest.raises(lab_service.ServiceError, match="no tiene filtro"):
        lab_service.run_filter(session, settings, no_filter)
    with pytest.raises(lab_service.ServiceError, match="trampa validada"):
        lab_service.create_from_trap(session, settings, _any_rejected(session, scope))


def _any_rejected(session: Session, scope: Scope) -> uuid.UUID:
    row = Hypothesis(
        statement="x",
        condition_spec=pt.Condition((pt.Clause("sesion", "eq", value="ASIA"),)).spec(),
        scope={},
        kind=pt.TRAP,
        bot_version_id=scope.version_id,
    )
    session.add(row)
    session.flush()
    return row.id


def test_definition_immutable_and_results_append_only(session: Session, settings: Settings) -> None:
    scope = make_scope(session)
    insert_trades(session, scope, 20, seed=5, planted=False)
    exp = lab_service.create_experiment(
        session,
        title="Inmutable",
        base_version_id=scope.version_id,
        change_description="original",
        filter_spec=TREND_FILTER,
    )
    lab_service.run_filter(session, settings, exp)
    note = lab_service.add_manual_result(
        session,
        exp,
        notes="cierro",
        status=ExperimentStatus.COMPLETED,
        conclusion="no concluyente",
    )
    assert note.revision == 2 and exp.status == ExperimentStatus.COMPLETED
    session.flush()
    for sql in (
        "UPDATE experiments SET change_description = 'otro' WHERE id = :id",
        "UPDATE experiments SET filter_spec = NULL WHERE id = :id",
        "DELETE FROM experiments WHERE id = :id",
        "UPDATE experiment_results SET notes = 'x' WHERE experiment_id = :id",
        "DELETE FROM experiment_results WHERE experiment_id = :id",
    ):
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(text(sql), {"id": exp.id})
    with session.begin_nested():  # el estado y la conclusión sí cambian
        session.execute(
            text("UPDATE experiments SET status = 'RUNNING' WHERE id = :id"), {"id": exp.id}
        )
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(ExperimentResult(experiment_id=exp.id, revision=1, kind="MANUAL", results={}))
        session.flush()

    run, _ = lab_service.import_backtest(
        session, settings, exp, backtest_csv(12).encode(), filename="bt.csv"
    )
    for sql in (
        "UPDATE backtest_runs SET label = 'x' WHERE id = :id",
        "DELETE FROM backtest_runs WHERE id = :id",
    ):
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(text(sql), {"id": run.id})
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(
            text(
                "INSERT INTO backtest_runs (id, bot_version_id, source, symbol, period_start, "
                "period_end, model) VALUES (gen_random_uuid(), :v, 'LIVE', 'X', '2026-01-01', "
                "'2026-01-02', 'm')"
            ),
            {"v": scope.version_id},
        )


def test_backtest_import_never_mixes_with_live(session: Session, settings: Settings) -> None:
    scope = make_scope(session)
    insert_trades(session, scope, 30, seed=6, planted=False)
    trades_before = session.scalar(select(func.count()).select_from(Trade))
    exp = lab_service.create_experiment(
        session, title="Backtest", base_version_id=scope.version_id, change_description="bt"
    )
    run, result = lab_service.import_backtest(
        session, settings, exp, backtest_csv(40).encode("utf-16"), filename="C:\\tester\\d.csv"
    )
    assert run.source.value == "BACKTEST" and run.report_format == "CSV"
    assert run.report_file == "d.csv" and run.label == "base"
    assert run.initial_deposit == 10000
    m = result.results["metricas"]
    assert m["n_trades"] == 40 and m["wins"] == 16 and m["losses"] == 24
    assert m["cumulative_return"] == pytest.approx(16 * 14.3 - 24 * 10.7)
    assert m["expectancy_r"] is None  # sin SL en los deals: se compara en dinero
    assert m["max_drawdown_pct"] is not None and m["max_drawdown_ic95"] is not None
    assert result.results["avisos"][0].startswith("Backtest del Strategy Tester")
    assert session.scalar(select(func.count()).select_from(Trade)) == trades_before
    with pytest.raises(lab_service.Conflict):
        lab_service.import_backtest(session, settings, exp, backtest_csv(40).encode("utf-16"))
    with pytest.raises(lab_service.ServiceError, match="no se pudo leer"):
        lab_service.import_backtest(session, settings, exp, b"hola;que;tal\n1;2;3\n")

    comparison = lab_service.experiment_comparison(session, settings, exp)
    sources = [a["fuente"] for a in comparison["brazos"]]
    assert sources == ["REAL", "BACKTEST"]
    real, backtest = comparison["brazos"]
    assert real["metricas"]["todo"]["n_trades"] == 30
    assert backtest["metricas"]["todo"]["n_trades"] == 40 and backtest["trampas"] is None
    assert len(backtest["curva"]) == 40


def test_comparison_arms_and_version_compare(session: Session, settings: Settings) -> None:
    scope = _planted(session, settings)
    candidate = BotVersion(
        bot_id=scope.bot_id, version="1.1.0", main_timeframe="M15", params={}, released_at=START
    )
    session.add(candidate)
    session.flush()
    insert_trades(
        session, replace(scope, version_id=candidate.id), 25, seed=8, planted=False, start_index=400
    )
    trap = _trap(session, scope)
    exp = lab_service.create_experiment(
        session,
        title="v1.1 con filtro H1",
        base_version_id=scope.version_id,
        candidate_version_id=candidate.id,
        hypothesis_id=trap.id,
        change_description="Agregar filtro de tendencia H1",
    )
    comparison = lab_service.experiment_comparison(session, settings, exp)
    keys = [a["clave"] for a in comparison["brazos"]]
    assert keys == ["original", "filtrado", "candidata"]
    original, filtered, cand = comparison["brazos"]
    assert comparison["titular"] == "fuera_de_muestra"
    assert set(original["metricas"]) == {"todo", "dentro_de_muestra", "fuera_de_muestra"}
    assert filtered["metricas"]["todo"]["n_trades"] < original["metricas"]["todo"]["n_trades"]
    assert filtered["avisos"][0].startswith("Es un contrafactual")
    assert any(
        t["statement"] == "Entrar contra la tendencia H1" for t in original["trampas"]["validadas"]
    )
    assert cand["metricas"]["todo"]["n_trades"] == 25
    assert any("muestra pequeña" in w for w in cand["avisos"])
    assert cand["trampas"] == {"validadas": [], "candidatas": 0}

    pair = lab_service.compare_two_versions(session, settings, scope.version_id, candidate.id)
    assert pair["a"]["metricas"]["todo"]["n_trades"] == 360
    assert pair["b"]["metricas"]["todo"]["n_trades"] == 25
    assert pair["diferencia_expectativa"]["metodo"].startswith("Welch")
    assert pair["avisos"] and "Muestra pequeña" in pair["avisos"][0]
    assert len(pair["a"]["curva"]) <= settings.lab_curve_points
    with pytest.raises(lab_service.ServiceError):
        lab_service.compare_two_versions(session, settings, candidate.id, candidate.id)


# API, CLI y dashboard (datos confirmados) ------------------------------------------------------


@pytest.fixture(scope="module")
def lab_data(engine: Engine) -> dict:
    settings = Settings(database_url="postgresql://x/y", admin_token=ADMIN_TOKEN)
    with Session(engine, expire_on_commit=False) as session:
        scope = make_scope(session)
        insert_trades(session, scope, 360, seed=3, planted=True, facts=True)
        session.commit()
    with Session(engine, expire_on_commit=False) as session:
        ps.run_patterns(session, settings, scope.version_id)
        session.commit()
        trap = _trap(session, scope)
        other = BotVersion(
            bot_id=scope.bot_id, version="2.0.0", main_timeframe="M5", params={}, released_at=START
        )
        session.add(other)
        session.commit()
    return {"scope": scope, "trap_id": trap.id, "other_version": other.id}


def test_lab_api(client: TestClient, lab_data: dict, engine: Engine) -> None:
    scope: Scope = lab_data["scope"]
    body = {
        "title": "Filtro de tendencia H1",
        "base_version_id": str(scope.version_id),
        "change_description": "Agregar filtro de tendencia H1",
        "filter": TREND_FILTER,
    }
    assert client.post("/v1/experiments", json=body).status_code == 401
    created = client.post("/v1/experiments", json=body, headers=admin())
    assert created.status_code == 201, created.text
    exp = created.json()
    number = exp["number"]
    assert exp["code"] == f"#{number:03d}" and exp["status"] == "DRAFT"
    assert exp["filter_text"].startswith("Saltarse las entradas")
    bad = client.post(
        "/v1/experiments",
        json={
            **body,
            "filter": {"modo": "excluir", "condicion": {"clausulas": [{"variable": "x"}]}},
        },
        headers=admin(),
    )
    assert bad.status_code == 400 and "variable desconocida" in bad.json()["detail"]
    missing = {**body, "base_version_id": str(uuid.uuid4())}
    assert client.post("/v1/experiments", json=missing, headers=admin()).status_code == 404
    assert (
        client.post("/v1/experiments", json={**body, "extra": 1}, headers=admin()).status_code
        == 422
    )

    run = client.post(f"/v1/experiments/{number}/filter-run", headers=admin())
    assert run.status_code == 201, run.text
    assert run.json()["revision"] == 1 and run.json()["results"]["titular"] == "fuera_de_muestra"

    csv_bytes = backtest_csv(30).encode()
    up = client.post(
        f"/v1/experiments/{number}/backtests",
        files={"file": ("deals.csv", csv_bytes, "text/csv")},
        data={"label": "original"},
        headers=admin(),
    )
    assert up.status_code == 201, up.text
    assert up.json()["revision"] == 2 and up.json()["backtest"]["source"] == "BACKTEST"
    dup = client.post(
        f"/v1/experiments/{number}/backtests",
        files={"file": ("deals.csv", csv_bytes, "text/csv")},
        headers=admin(),
    )
    assert dup.status_code == 409
    junk = client.post(
        f"/v1/experiments/{number}/backtests",
        files={"file": ("x.xlsx", b"PK\x03\x04resto", "application/octet-stream")},
        headers=admin(),
    )
    assert junk.status_code == 400 and ".xlsx" in junk.json()["detail"]

    note = client.post(
        f"/v1/experiments/{exp['id']}/results",
        json={
            "notes": "Mejora fuera de muestra",
            "status": "COMPLETED",
            "conclusion": "prometedor",
        },
        headers=admin(),
    )
    assert note.status_code == 201 and note.json()["revision"] == 3

    detail = client.get(f"/v1/experiments/%23{number:03d}", headers=admin()).json()
    assert [r["revision"] for r in detail["results"]] == [1, 2, 3]
    assert detail["status"] == "COMPLETED" and detail["conclusion"] == "prometedor"
    assert detail["backtests"][0]["label"] == "original"
    listing = client.get("/v1/experiments", headers=admin()).json()
    assert any(e["number"] == number and e["headline"] for e in listing)
    assert client.get("/v1/experiments/999999", headers=admin()).status_code == 404
    assert client.get("/v1/experiments/nada", headers=admin()).status_code == 404

    comparison = client.get(f"/v1/experiments/{number}/comparison", headers=admin()).json()
    assert [a["fuente"] for a in comparison["brazos"]] == ["REAL", "CONTRAFACTUAL", "BACKTEST"]
    pair = client.get(
        "/v1/versions/compare",
        params={"a": str(scope.version_id), "b": str(lab_data["other_version"])},
        headers=admin(),
    )
    assert pair.status_code == 200 and pair.json()["b"]["metricas"]["todo"]["n_trades"] == 0

    # Los backtests no crean operaciones: la versión sigue con sus 360 reales.
    with Session(engine) as session:
        assert (
            session.scalar(select(func.count()).where(Trade.bot_version_id == scope.version_id))
            == 360
        )


def test_backtest_upload_size_limit(make_dash, lab_data: dict) -> None:  # noqa: F811
    c = make_dash(lab_upload_max_bytes=2048)
    scope: Scope = lab_data["scope"]
    exp = c.post(
        "/v1/experiments",
        json={
            "title": "Límite",
            "base_version_id": str(scope.version_id),
            "change_description": "x",
        },
        headers=admin(),
    ).json()
    big = c.post(
        f"/v1/experiments/{exp['number']}/backtests",
        files={"file": ("big.csv", backtest_csv(200).encode(), "text/csv")},
        headers=admin(),
    )
    assert big.status_code == 413 and "demasiado grande" in big.json()["detail"]


def test_cli_experiment(
    engine: Engine,
    lab_data: dict,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    tmp_path,
) -> None:
    @contextmanager
    def scope_() -> Iterator[Session]:
        with Session(engine, expire_on_commit=False) as session:
            yield session
            session.commit()

    monkeypatch.setattr(cli, "session_scope", scope_)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    scope: Scope = lab_data["scope"]
    cli.main(
        [
            "experiment",
            "create",
            "--base",
            str(scope.version_id),
            "--title",
            "CLI H1",
            "--change",
            "Agregar filtro de tendencia H1",
            "--hypothesis",
            str(lab_data["trap_id"]),
        ]
    )
    out = capsys.readouterr().out
    number = re.search(r"Experimento #(\d+) creado", out).group(1)
    assert "Saltarse las entradas que cumplan" in out
    cli.main(["experiment", "run-filter", "--id", number])
    out = capsys.readouterr().out
    assert "revisión 1 (filtro contrafactual)" in out and "Titular: fuera de muestra" in out
    assert "filtrado" in out and "AVISO: Dentro de muestra es optimista" in out

    path = tmp_path / "informe.csv"
    path.write_text(backtest_csv(20), encoding="utf-8")
    cli.main(["experiment", "import-backtest", "--id", number, "--file", str(path)])
    assert "backtest 'base' importado (CSV)" in capsys.readouterr().out
    monkeypatch.setattr(
        "sys.stdin", io.TextIOWrapper(io.BytesIO(backtest_csv(21, sep=",", decimal=".").encode()))
    )
    cli.main(
        ["experiment", "import-backtest", "--id", number, "--file", "-", "--label", "candidata"]
    )
    assert "backtest 'candidata' importado" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["experiment", "import-backtest", "--id", number, "--file", str(path)])
    capsys.readouterr()

    cli.main(["experiment", "show", "--id", number])
    out = capsys.readouterr().out
    assert f"Experimento #{int(number):03d}: CLI H1" in out
    assert "Hipótesis: [trampa validada] Entrar contra la tendencia H1" in out
    assert "Backtest base" in out and "Backtest candidata" in out and "fuera de muestra" in out
    cli.main(["experiment", "list"])
    assert f"#{int(number):03d} [en curso] CLI H1" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["experiment", "show", "--id", "999999"])


def _csrf(client: TestClient, path: str = "/dashboard") -> str:
    page = client.get(path)
    return re.search(r'name="csrf_token" value="([0-9a-f]+)"', page.text).group(1)


def test_dashboard_lab(make_dash, lab_data: dict, engine: Engine) -> None:  # noqa: F811
    dash = make_dash()
    assert dash.get("/dashboard/laboratorio").status_code == 303
    assert login(dash).status_code == 303
    trap_id = str(lab_data["trap_id"])

    traps = dash.get("/dashboard/trampas").text
    assert 'action="/dashboard/laboratorio/crear"' in traps
    assert f'name="hypothesis_id" value="{trap_id}"' in traps

    csrf = _csrf(dash, "/dashboard/trampas")
    assert (
        dash.post(
            "/dashboard/laboratorio/crear",
            data={"hypothesis_id": trap_id, "csrf_token": "x" * 32},
        ).status_code
        == 403
    )
    cross = dash.post(
        "/dashboard/laboratorio/crear",
        data={"hypothesis_id": trap_id, "csrf_token": csrf},
        headers={"Origin": "https://evil.example"},
    )
    assert cross.status_code == 403
    created = dash.post(
        "/dashboard/laboratorio/crear", data={"hypothesis_id": trap_id, "csrf_token": csrf}
    )
    assert created.status_code == 303
    location = created.headers["location"]
    number = int(location.rsplit("/", 1)[-1])
    with Session(engine) as session:
        exp = session.scalar(select(Experiment).where(Experiment.number == number))
        assert exp.hypothesis_id == lab_data["trap_id"]
        assert (
            session.scalar(select(func.count()).where(ExperimentResult.experiment_id == exp.id))
            == 1
        )

    detail = dash.get(location)
    assert detail.status_code == 200
    html = detail.text
    assert 'aria-current="page"' in html and "Laboratorio" in html
    assert "Fuera de muestra" in html and "Filtrado (contrafactual)" in html
    assert "Dentro de muestra es optimista" in html and "en vivo es desconocido" in html
    assert f'data-url="/dashboard/api/laboratorio/{number}/curvas"' in html
    assert "style=" not in html and "<script>" not in html

    curves = dash.get(f"/dashboard/api/laboratorio/{number}/curvas").json()
    assert [s["fuente"] for s in curves["series"]] == ["REAL", "CONTRAFACTUAL"]
    assert len(curves["series"]) == 2 and all(s["points"] for s in curves["series"])

    listing = dash.get("/dashboard/laboratorio").text
    assert f"#{number:03d}" in listing and 'name="a"' in listing
    compare = dash.get(
        "/dashboard/laboratorio/comparar",
        params={"a": str(lab_data["scope"].version_id), "b": str(lab_data["other_version"])},
    )
    assert compare.status_code == 200 and "Muestra pequeña" in compare.text
    assert 'data-url="/dashboard/api/laboratorio/comparar/curvas?' in compare.text
    pair_curves = dash.get(
        "/dashboard/api/laboratorio/comparar/curvas",
        params={"a": str(lab_data["scope"].version_id), "b": str(lab_data["other_version"])},
    ).json()
    assert len(pair_curves["series"]) == 2
    assert dash.get("/dashboard/laboratorio/999999").status_code == 404
    bad = dash.get("/dashboard/laboratorio/comparar", params={"a": "x", "b": "y"})
    assert bad.status_code == 200 and "Elige dos versiones" in bad.text


def test_filter_json_roundtrip_is_canonical() -> None:
    spec = lab_service.lab.normalize_filter(
        TREND_FILTER, ps.search_variables(), lab_service._rule_spec
    )
    assert json.dumps(spec, sort_keys=True) == json.dumps(
        {"modo": "excluir", "condicion": pt.Condition.from_spec(spec["condicion"]).spec()},
        sort_keys=True,
    )
    rule = lab_service.lab.normalize_filter(
        {"condicion": {"tipo": "regla", "regla": "H_CONTRA_TENDENCIA_H1"}},
        ps.search_variables(),
        lab_service._rule_spec,
    )
    assert rule["condicion"]["tipo"] == "regla" and rule["condicion"]["condiciones"]
    assert isinstance(ps.condition_for(rule["condicion"]), ps.RuleCondition)
    assert BacktestRun.__table__.c.source.server_default.arg == "BACKTEST"
