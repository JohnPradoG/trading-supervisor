"""Análisis post-operación de punta a punta: eventos por la API de ingesta, worker, velas M1
reales en PostgreSQL y consulta por la API de administración."""

import uuid
from contextlib import contextmanager
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from supervisor import cli
from supervisor.analytics import analyzer
from supervisor.analytics.indicators import ema_series
from supervisor.analytics.market import floor_time
from supervisor.config import Settings
from supervisor.models import AnalysisFinding, Trade, TradeAnalysis
from supervisor.models.enums import Direction, FindingKind, Outcome, TradeSource
from supervisor.worker.maintenance import maintenance_pass
from supervisor.worker.processor import process_pending
from tests.conftest import _headers
from tests.integration.trading_helpers import (
    BASE,
    admin,
    at,
    bar,
    deal,
    deploy,
    history_price,
    insert_m1_history,
    load_trade,
    modify,
    post_balance,
    post_bars,
    post_events,
)

HISTORY_DAYS = 21


def _history(engine: Engine, terminal: dict, **kw) -> None:
    start = BASE - timedelta(days=HISTORY_DAYS)
    insert_m1_history(
        engine, terminal["broker_id"], "USTEC_x100", start, BASE - timedelta(minutes=1), **kw
    )


def _analysis(client: TestClient, trade_id, **params) -> dict:
    response = client.get(f"/v1/trades/{trade_id}/analysis", headers=admin(), params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _versions(engine: Engine, trade_id) -> list[TradeAnalysis]:
    with Session(engine) as session:
        return list(
            session.scalars(
                select(TradeAnalysis)
                .where(TradeAnalysis.trade_id == trade_id)
                .order_by(TradeAnalysis.analysis_version)
            )
        )


@contextmanager
def _scope(engine: Engine):
    with Session(engine, expire_on_commit=False) as session:
        yield session
        session.commit()


def _winner_bars() -> list[dict]:
    """BUY desde 20300: baja a 20290 en el minuto 3, sube 2 por minuto y toca 20361 en el 25."""
    bars = [bar(at(0), 20300, 20302, 20295, 20300)]
    for i in range(1, 25):
        p = 20300 + 2 * i
        low = 20290 if i == 3 else p - 3
        bars.append(bar(at(i), p, p + 3, low, p))
    bars.append(bar(at(25), 20350, 20361, 20349, 20360))
    return bars


def _close_winner(client: TestClient, terminal: dict, pos: int) -> None:
    login = terminal["login"]
    post_bars(client, terminal, _winner_bars())
    post_events(
        client,
        terminal,
        deal(
            login,
            pos * 10 + 1,
            pos,
            "IN",
            "BUY",
            1.0,
            20300,
            at(0),
            sl=20270,
            tp=20360,
            commission=-1.0,
        ),
        modify(login, pos, at(10), 20300, 20360),
        deal(
            login,
            pos * 10 + 2,
            pos,
            "OUT",
            "SELL",
            0.4,
            20330,
            at(15),
            sl=20300,
            tp=20360,
            profit=12.0,
        ),
        deal(
            login,
            pos * 10 + 3,
            pos,
            "OUT",
            "SELL",
            0.6,
            20360,
            at(25),
            reason="TP",
            sl=20300,
            tp=20360,
            profit=36.0,
        ),
    )


def test_winning_trade_analysis_from_real_bars(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    deploy(client, terminal)
    post_balance(client, terminal, at(-1), 10000)
    _history(engine, terminal)
    _close_winner(client, terminal, 9001)
    process_pending(session_factory, worker_settings)
    trade = load_trade(engine, terminal, 9001)
    assert trade.net_profit == 47 and trade.risk_amount == 30  # 48 bruto - 1; 30 x 1 lote

    body = _analysis(client, trade.trade_id)
    a = body["analysis"]
    assert a["analysis_version"] == 1 and a["outcome"] == "WIN"
    assert a["analyzer_version"] == analyzer.ANALYZER_VERSION and len(a["input_hash"]) == 64
    assert [h["analysis_version"] for h in body["history"]] == [1]
    facts = {f["code"]: f for f in a["facts"]}
    assert all(f["kind"] == "FACT" and f["confidence"] is None for f in a["facts"])
    assert set(facts) == {
        "RESULTADO",
        "MULTIPLO_R",
        "DURACION",
        "MOTIVO_SALIDA",
        "TIEMPO_ENTRADA",
        "EXCURSIONES",
        "MFE_VS_TP",
        "MAE_VS_SL",
        "CAPTURA_MFE",
        "TP_ALCANZADO",
        "MODIFICACIONES_SL_TP",
        "SL_A_BREAKEVEN",
        "CIERRES_PARCIALES",
        "SPREAD_ENTRADA",
        "PRECIO_VS_EMA200_H1",
        "PRECIO_VS_EMA50_H1",
        "PRECIO_VS_EMA200_M15",
        "PRECIO_VS_EMA50_M15",
        "VOLATILIDAD_ATR_H1",
    }
    ev = {code: f["evidence"] for code, f in facts.items()}
    assert ev["MULTIPLO_R"]["r"] == 1.5667  # 47 / 30
    assert ev["MOTIVO_SALIDA"]["motivo"] == "MIXED"
    # MFE 20361 - 20300 = 6100 puntos (TP a 6000); MAE 20290 - 20300 = -1000 (SL a 3000).
    assert (ev["EXCURSIONES"]["mfe_puntos"], ev["EXCURSIONES"]["mae_puntos"]) == (6100, -1000)
    assert ev["MFE_VS_TP"]["supero"] is True and ev["MAE_VS_SL"]["fraccion"] == 0.3333
    # Cierre medio (0.4 x 20330 + 0.6 x 20360) = 20348: 4800 de 6100 puntos.
    assert ev["CAPTURA_MFE"]["captura"] == 0.7869
    # No cerró todo por TP (MIXED): el toque del TP sale de las velas M1, minuto 25.
    assert ev["TP_ALCANZADO"] == {"minutos": 25.0, "fuente": "velas M1", "vela": at(25).isoformat()}
    assert ev["SL_A_BREAKEVEN"]["minutos"] == 10.0 and ev["SL_A_BREAKEVEN"]["sl"] == 20300
    assert ev["CIERRES_PARCIALES"]["cantidad"] == 1
    assert ev["SPREAD_ENTRADA"] == {
        "spread_puntos": 120,
        "vela": at(0).isoformat(),
        "fraccion_sl": 0.04,
        "porcentaje_sl": 4.0,
    }
    # EMA200 H1 recalculada aquí desde la fórmula de la historia: cierre de cada hora = precio
    # de su último minuto; solo horas cerradas antes de la entrada (sin lookahead).
    start = BASE - timedelta(days=HISTORY_DAYS)
    cutoff = floor_time(BASE, 60)
    hour = floor_time(start, 60)
    closes = []
    while hour < cutoff:
        closes.append(history_price(hour + timedelta(minutes=59), start))
        hour += timedelta(hours=1)
    expected = ema_series(closes, 200)[-1]
    ema = ev["PRECIO_VS_EMA200_H1"]
    assert ema["ema"] == pytest.approx(expected, abs=1e-4)
    assert (
        ema["velas"] == len(closes)
        and ema["ultima_vela"] == (cutoff - timedelta(hours=1)).isoformat()
    )
    assert ema["posicion"] == "ENCIMA" and ema["a_favor"] is True
    assert ev["VOLATILIDAD_ATR_H1"]["muestras"] >= 100
    assert [q["code"] for q in a["data_quality"]] == ["SIN_DATOS_NOTICIAS"]

    (hyp,) = a["hypotheses"]
    assert hyp["code"] == "H_A_FAVOR_TENDENCIA_H1" and hyp["kind"] == "HYPOTHESIS"
    assert hyp["confidence"] == "no_validada" and hyp["rule_version"] == 1
    assert hyp["supported_by"] == ["PRECIO_VS_EMA200_H1"]
    assert hyp["text"].startswith("Operar a favor") and "pudo haber" in hyp["text"]


def test_losing_trade_hypotheses_linked_to_facts(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    """SELL contra una tendencia alcista, con volatilidad disparada las 3 horas previas: fue
    4500 puntos a favor (75 % del TP) y terminó en el SL."""
    deploy(client, terminal)
    _history(engine, terminal, spike_from=BASE - timedelta(hours=3))
    bars = [bar(at(0), 20300, 20303, 20297, 20300)]
    for i in range(1, 9):
        p = 20300 - 5 * i
        bars.append(bar(at(i), p + 5, p + 5, p - 3, p))
    bars.append(bar(at(9), 20258, 20262, 20255, 20258))
    for i in range(10, 20):
        p = 20255 + 7 * (i - 9)
        bars.append(bar(at(i), p - 7, p + 3, p - 7, p))
    bars.append(bar(at(20), 20326, 20331, 20324, 20330))
    post_bars(client, terminal, bars)
    login = terminal["login"]
    post_events(
        client,
        terminal,
        deal(
            login, 9101, 9100, "IN", "SELL", 1.0, 20300, at(0), sl=20330, tp=20240, commission=-1.0
        ),
        deal(
            login,
            9102,
            9100,
            "OUT",
            "BUY",
            1.0,
            20330,
            at(20),
            reason="SL",
            sl=20330,
            tp=20240,
            profit=-30.0,
        ),
    )
    process_pending(session_factory, worker_settings)
    trade = load_trade(engine, terminal, 9100)
    assert trade.direction == Direction.SELL and trade.net_profit == -31

    a = _analysis(client, trade.trade_id)["analysis"]
    assert a["outcome"] == "LOSS"
    facts = {f["code"]: f["evidence"] for f in a["facts"]}
    assert facts["MULTIPLO_R"]["r"] == -1.0333
    assert facts["MFE_VS_TP"]["fraccion"] == 0.75 and facts["MAE_VS_SL"]["fraccion"] == 1.0333
    assert facts["PRECIO_VS_EMA200_H1"]["a_favor"] is False
    assert facts["VOLATILIDAD_ATR_H1"]["percentil"] == 100.0
    assert "TP_ALCANZADO" not in facts  # el mínimo fue 20255, el TP 20240
    hyps = {h["code"]: h for h in a["hypotheses"]}
    assert set(hyps) == {
        "H_CONTRA_TENDENCIA_H1",
        "H_CONTRA_TENDENCIA_M15",
        "H_VOLATILIDAD_ALTA",
        "H_SL_TP_MAL_UBICADO",
        "H_PERDEDORA_CON_MFE",
    }
    for h in hyps.values():
        assert h["confidence"] == "no_validada"
        assert h["supported_by"] and set(h["supported_by"]) <= set(facts)
    assert hyps["H_SL_TP_MAL_UBICADO"]["supported_by"] == ["MAE_VS_SL", "MFE_VS_TP"]
    assert hyps["H_VOLATILIDAD_ALTA"]["supported_by"] == ["VOLATILIDAD_ATR_H1"]

    # En la base de datos los hechos y las hipótesis están separados por tipo.
    with Session(engine) as session:
        rows = session.scalars(
            select(AnalysisFinding)
            .join(TradeAnalysis)
            .where(TradeAnalysis.trade_id == trade.trade_id)
            .order_by(AnalysisFinding.position)
        ).all()
    kinds = [r.kind for r in rows]
    assert kinds == sorted(kinds, key=lambda k: k != FindingKind.FACT)  # hechos primero
    assert all((r.kind == FindingKind.FACT) == (r.confidence is None) for r in rows)


def test_reanalysis_creates_new_versions_only_when_inputs_change(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    deploy(client, terminal)
    _close_winner(client, terminal, 9200)
    process_pending(session_factory, worker_settings)
    trade = load_trade(engine, terminal, 9200)

    (v1,) = _versions(engine, trade.trade_id)
    notes = {q["code"] for q in v1.data_quality}
    # Sin historia antes de la entrada: ni EMAs ni volatilidad, y se dice por qué.
    assert {"PRECIO_VS_EMA200_H1_SIN_DATOS", "VOLATILIDAD_SIN_DATOS"} <= notes
    assert v1.inputs["control"]["incompleto"] is True
    assert v1.inputs["mercado"]["emas"]["ema200_h1"]["motivo"].startswith("0 velas cerradas")

    # Repasar sin cambios no crea versión.
    maintenance_pass(session_factory, worker_settings)
    assert len(_versions(engine, trade.trade_id)) == 1

    # Llegan 21 días de historia: el repaso detecta velas nuevas y crea la versión 2.
    _history(engine, terminal)
    maintenance_pass(session_factory, worker_settings)
    v1_again, v2 = _versions(engine, trade.trade_id)
    assert (v1_again.id, v1_again.input_hash) == (v1.id, v1.input_hash)  # la v1 no cambia
    assert v2.analysis_version == 2 and v2.input_hash != v1.input_hash
    assert "PRECIO_VS_EMA200_H1_SIN_DATOS" not in {q["code"] for q in v2.data_quality}
    body = _analysis(client, trade.trade_id)
    assert body["analysis"]["analysis_version"] == 2
    assert [h["analysis_version"] for h in body["history"]] == [2, 1]
    old = _analysis(client, trade.trade_id, version=1)["analysis"]
    assert "PRECIO_VS_EMA200_H1" not in {f["code"] for f in old["facts"]}
    assert "H_A_FAVOR_TENDENCIA_H1" in {h["code"] for h in body["analysis"]["hypotheses"]}

    # Idempotencia: repaso y CLI no crean más versiones si nada cambió.
    maintenance_pass(session_factory, worker_settings)
    monkeypatch.setattr(cli, "session_scope", lambda: _scope(engine))
    monkeypatch.setattr(cli, "get_settings", lambda: worker_settings)
    cli.main(["analyze", "--trade", str(trade.trade_id)])
    assert "v2 sin cambios" in capsys.readouterr().out
    cli.main(["analyze", "--all-closed"])
    out = capsys.readouterr().out
    assert "Versiones nuevas: 0" in out and "errores: 0" in out
    assert len(_versions(engine, trade.trade_id)) == 2

    # Nueva versión del analizador: se re-analiza (aquí con la CLI) y queda registrada.
    monkeypatch.setattr(analyzer, "ANALYZER_VERSION", "9.9.9")
    cli.main(["analyze", "--trade", str(trade.trade_id)])
    assert "v3 creada: WIN" in capsys.readouterr().out
    v3 = _versions(engine, trade.trade_id)[-1]
    assert v3.analyzer_version == "9.9.9" and v3.input_hash != v2.input_hash

    # Historia inmutable: ni UPDATE ni DELETE.
    with Session(engine) as session, pytest.raises(DBAPIError, match="solo inserción"):
        session.execute(
            text("UPDATE trade_analyses SET outcome = 'LOSS' WHERE id = :id"), {"id": v1.id}
        )
    with Session(engine) as session, pytest.raises(DBAPIError, match="solo inserción"):
        session.execute(
            text("DELETE FROM analysis_findings WHERE analysis_id = :id"), {"id": v1.id}
        )

    with pytest.raises(SystemExit, match="no existe la operación"):
        cli.main(["analyze", "--trade", str(uuid.uuid4())])


def test_parameter_change_triggers_reanalysis(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    """Cambiar la tolerancia de breakeven cambia el resultado: versión nueva con BREAKEVEN."""
    login = terminal["login"]
    deploy(client, terminal)
    post_events(
        client,
        terminal,
        deal(login, 9301, 9300, "IN", "BUY", 1.0, 20300, at(0), sl=20270),
        deal(login, 9302, 9300, "OUT", "SELL", 1.0, 20302, at(5), profit=2.0),
    )
    process_pending(session_factory, worker_settings)
    trade = load_trade(engine, terminal, 9300)
    (v1,) = _versions(engine, trade.trade_id)
    assert v1.outcome == Outcome.WIN  # 2 > 0.05 x 30 = 1.5
    wider = worker_settings.model_copy(update={"breakeven_r_fraction": 0.1})
    maintenance_pass(session_factory, wider)
    v1_again, v2 = _versions(engine, trade.trade_id)
    assert v1_again.outcome == Outcome.WIN and v2.outcome == Outcome.BREAKEVEN  # 2 <= 3


def test_analysis_api_auth_open_trade_and_errors(
    client: TestClient,
    terminal: dict,
    engine: Engine,
    session_factory: sessionmaker[Session],
    worker_settings: Settings,
) -> None:
    login = terminal["login"]
    post_events(client, terminal, deal(login, 9401, 9400, "IN", "BUY", 0.1, 20000, at(0)))
    process_pending(session_factory, worker_settings)
    trade = load_trade(engine, terminal, 9400)
    url = f"/v1/trades/{trade.trade_id}/analysis"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=_headers(terminal)).status_code == 401
    body = _analysis(client, trade.trade_id)
    assert body["analysis"] is None and body["history"] == []
    assert body["trade_status"] == "OPEN" and "abierta" in body["note"]
    assert client.get(f"/v1/trades/{uuid.uuid4()}/analysis", headers=admin()).status_code == 404

    post_events(
        client, terminal, deal(login, 9402, 9400, "OUT", "SELL", 0.1, 20001, at(3), profit=0.1)
    )
    process_pending(session_factory, worker_settings)
    body = _analysis(client, trade.trade_id, include_inputs="true")
    a = body["analysis"]
    # Sin SL: sin riesgo; sin velas: sin excursiones; todo explicado en data_quality.
    assert {"SIN_RIESGO", "SIN_EXCURSION", "SIN_SL", "SIN_TP", "SIN_DESPLIEGUE"} <= {
        q["code"] for q in a["data_quality"]
    }
    assert a["inputs"]["operacion"]["trade_id"] == str(trade.trade_id)
    assert _analysis(client, trade.trade_id)["analysis"]["inputs"] is None
    response = client.get(url, headers=admin(), params={"version": 7})
    assert response.status_code == 404


def test_findings_check_constraint_separates_fact_and_hypothesis(session: Session, world) -> None:
    trade = Trade(
        account_id=world["account"].id,
        position_id=1,
        magic_number=1,
        symbol="X",
        direction=Direction.BUY,
        order_type="UNKNOWN",
        source=TradeSource.DEMO,
        entry_time=BASE,
        entry_price=1,
        initial_volume=1,
        max_volume=1,
    )
    session.add(trade)
    session.flush()
    analysis = TradeAnalysis(
        trade_id=trade.trade_id,
        analysis_version=1,
        outcome=Outcome.WIN,
        analyzer_version="1.0.0",
        ruleset_version=1,
        input_hash="x" * 64,
        inputs={},
        data_quality=[],
    )
    session.add(analysis)
    session.flush()

    def add(**kw) -> None:
        values = {"analysis_id": analysis.id, "text": "t", "evidence": {}, **kw}
        with session.begin_nested():
            session.add(AnalysisFinding(**values))
            session.flush()

    add(kind=FindingKind.FACT, code="A")
    add(
        kind=FindingKind.HYPOTHESIS,
        code="H",
        confidence="no_validada",
        rule_version=1,
        supported_by=["A"],
    )
    bad = [
        {"kind": FindingKind.FACT, "code": "B", "confidence": "alta"},
        {"kind": FindingKind.FACT, "code": "C", "supported_by": ["A"]},
        {
            "kind": FindingKind.HYPOTHESIS,
            "code": "H2",
            "confidence": "no_validada",
            "rule_version": 1,
            "supported_by": [],
        },
        {"kind": FindingKind.HYPOTHESIS, "code": "H3", "rule_version": 1, "supported_by": ["A"]},
        {"kind": FindingKind.FACT, "code": "A"},  # código repetido en el mismo análisis
    ]
    for values in bad:
        with pytest.raises(IntegrityError):
            add(**values)
