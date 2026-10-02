"""Piezas puras del análisis: resultado, sesiones, indicadores, hechos y reglas."""

import copy
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from supervisor.analytics.facts import build_analysis
from supervisor.analytics.indicators import Bar, atr_series, ema_series, percentile_rank
from supervisor.analytics.market import floor_time
from supervisor.analytics.outcome import classify_outcome
from supervisor.analytics.rules import (
    CONFIDENCE_UNVALIDATED,
    RULES,
    RULESET_VERSION,
    Condition,
    evaluate,
    ruleset_spec,
)
from supervisor.analytics.sessions import session_flags, session_label, time_info
from supervisor.models.enums import FindingKind, Outcome

UTC_DAY = datetime(2026, 9, 29, tzinfo=UTC)  # martes


# Resultado ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("net", "risk", "commission", "expected", "basis"),
    [
        (4, 100, -1, Outcome.BREAKEVEN, "riesgo"),  # |4| <= 0.05 x 100
        (-5, 100, -1, Outcome.BREAKEVEN, "riesgo"),  # límite incluido
        (6, 100, -1, Outcome.WIN, "riesgo"),
        (-5.01, 100, 0, Outcome.LOSS, "riesgo"),
        (-3.5, None, -3.5, Outcome.BREAKEVEN, "comision"),  # solo el coste
        (-3.6, None, -3.5, Outcome.LOSS, "comision"),
        (0, None, 0, Outcome.BREAKEVEN, "comision"),
        (0.01, None, None, Outcome.WIN, "comision"),
        (1, 0, -2, Outcome.BREAKEVEN, "comision"),  # riesgo 0 = sin riesgo
    ],
)
def test_classify_outcome(net, risk, commission, expected, basis) -> None:
    result = classify_outcome(net, risk, commission)
    assert result.outcome == expected and result.basis == basis


def test_breakeven_fraction_is_configurable() -> None:
    assert classify_outcome(8, 100, 0, 0.1).outcome == Outcome.BREAKEVEN
    assert classify_outcome(8, 100, 0, 0.05).outcome == Outcome.WIN


# Sesiones ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "label"),
    [
        (3, "ASIA"),
        (8, "LONDRES"),
        (13, "LONDRES_NY"),
        (17, "NUEVA_YORK"),
        (22, "FUERA_SESION"),
    ],
)
def test_session_labels(hour: int, label: str) -> None:
    assert session_label(UTC_DAY.replace(hour=hour, minute=30)) == label


def test_session_flags_and_time_info_use_utc() -> None:
    assert session_flags(UTC_DAY.replace(hour=8)) == {
        "asia": True,
        "londres": True,
        "nueva_york": False,
    }
    # Una hora con otra zona se convierte a UTC: 13:30 en Moscú (UTC+3) son las 10:30 UTC.
    moscow = datetime(2026, 9, 29, 13, 30, tzinfo=ZoneInfo("Europe/Moscow"))
    info = time_info(moscow)
    assert info["hora_utc"] == 10 and info["dia_nombre"] == "martes"
    with pytest.raises(ValueError):
        session_label(datetime(2026, 9, 29, 10))


# Indicadores -------------------------------------------------------------------------------


def test_ema_seeded_with_sma() -> None:
    # Semilla SMA(1, 2, 3) = 2; k = 2 / 4 = 0.5: 4 x 0.5 + 2 x 0.5 = 3; 5 x 0.5 + 3 x 0.5 = 4
    assert ema_series([1, 2, 3, 4, 5], 3) == [None, None, 2, 3, 4]
    assert ema_series([1, 2], 3) == [None, None]
    with pytest.raises(ValueError):
        ema_series([1.0], 0)


def _bar(i: int, h: float, low: float, c: float) -> Bar:
    return Bar(UTC_DAY + timedelta(hours=i), c, h, low, c)


def test_wilder_atr_hand_computed() -> None:
    bars = [_bar(0, 10, 8, 9), _bar(1, 11, 9, 10), _bar(2, 13, 10, 12), _bar(3, 12, 7, 8)]
    # TR: 2 (11-9), 3 (13-10), 5 (|7-12|). ATR(2) = (2 + 3) / 2 = 2.5; (2.5 + 5) / 2 = 3.75
    assert atr_series(bars, 2) == [None, None, 2.5, 3.75]
    assert atr_series(bars[:2], 2) == [None, None]


def test_percentile_rank() -> None:
    assert percentile_rank([1, 2, 3, 4], 3) == 75.0
    assert percentile_rank([1, 2, 3, 4], 0) == 0.0
    with pytest.raises(ValueError):
        percentile_rank([], 1)


def test_floor_time_aligns_to_midnight() -> None:
    t = datetime(2026, 9, 29, 13, 47, 31, tzinfo=UTC)
    assert floor_time(t, 60) == datetime(2026, 9, 29, 13, 0, tzinfo=UTC)
    assert floor_time(t, 15) == datetime(2026, 9, 29, 13, 45, tzinfo=UTC)
    assert floor_time(t, 1) == datetime(2026, 9, 29, 13, 47, tzinfo=UTC)


# Hechos e hipótesis --------------------------------------------------------------------------

ENTRY = datetime(2026, 9, 29, 14, 0, tzinfo=UTC)


def _ema(value: float) -> dict:
    return {
        "ok": True,
        "valor": value,
        "velas": 450,
        "cobertura": 1.0,
        "ultima_vela": "2026-09-29T13:00:00+00:00",
        "corte": "2026-09-29T14:00:00+00:00",
    }


def losing_sell() -> dict:
    """SELL de 1 lote en 20300 con SL 20330 (3000 puntos) y TP 20240 (6000 puntos), cerrada
    por SL: neto -31 con 30 de riesgo. Fue 4500 puntos a favor y 3100 en contra."""
    return {
        "operacion": {
            "trade_id": "t-1",
            "direccion": "SELL",
            "simbolo": "USTEC",
            "entrada": ENTRY.isoformat(),
            "precio_entrada": "20300",
            "sl_inicial": "20330",
            "tp_inicial": "20240",
            "volumen_inicial": "1",
            "cierre": (ENTRY + timedelta(minutes=20)).isoformat(),
            "precio_cierre": "20330",
            "bruto": "-30",
            "comision": "-1",
            "swap": "0",
            "neto": "-31",
            "puntos": "-3000",
            "duracion_s": 1200,
            "motivo_salida": "SL",
            "mfe_puntos": "4500",
            "mae_puntos": "-3100",
            "mfe_dinero": "45",
            "mae_dinero": "-31",
            "punto": "0.01",
            "riesgo": "30",
            "riesgo_pct": "0.3",
            "balance_entrada": "10000",
            "reentrada_de": None,
            "despliegue": "d-1",
            "version_bot": "v-1",
            "excursion": {"cobertura": 1.0, "completa": True, "velas_encontradas": 21},
            "precio_medio_entrada": None,
        },
        "eventos": [
            {
                "tipo": "TRADE_OPENED",
                "hora": ENTRY.isoformat(),
                "precio": "20300",
                "volumen": "1",
                "sl": "20330",
                "tp": "20240",
                "inferido": False,
                "extra": {},
            },
        ],
        "cuenta": {"moneda": "USD"},
        "mercado": {
            "emas": {
                "ema200_h1": _ema(20240.0),
                "ema50_h1": _ema(20280.0),
                "ema200_m15": _ema(20290.0),
                "ema50_m15": _ema(20296.0),
            },
            "volatilidad": {
                "periodo": 14,
                "dias": 20,
                "ok": True,
                "atr": 23.5,
                "percentil": 100.0,
                "muestras": 470,
                "mediana": 10.2,
            },
            "vela_entrada": {"hora": ENTRY.isoformat(), "spread": 120},
            "toque_tp": None,
        },
        "noticias": [],
        "parametros": {"breakeven_r_fraction": 0.05, "ventana_noticias_min": 60},
        "control": {},
    }


def _codes(findings, kind: FindingKind) -> set[str]:
    return {f.code for f in findings if f.kind == kind}


def test_losing_trade_facts_and_hypotheses() -> None:
    result = build_analysis(losing_sell())
    assert result.outcome == Outcome.LOSS
    facts = {f.code: f for f in result.facts}
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
        "SPREAD_ENTRADA",
        "PRECIO_VS_EMA200_H1",
        "PRECIO_VS_EMA50_H1",
        "PRECIO_VS_EMA200_M15",
        "PRECIO_VS_EMA50_M15",
        "VOLATILIDAD_ATR_H1",
    }
    assert facts["MULTIPLO_R"].evidence["r"] == -1.0333  # -31 / 30
    assert facts["EXCURSIONES"].evidence["mfe_r"] == 1.5  # 4500 / 3000
    assert facts["EXCURSIONES"].evidence["mae_r"] == -1.0333
    assert facts["MFE_VS_TP"].evidence == {
        "mfe_puntos": 4500.0,
        "distancia_tp_puntos": 6000.0,
        "fraccion": 0.75,
        "porcentaje": 75.0,
        "supero": False,
    }
    assert facts["MAE_VS_SL"].evidence["fraccion"] == 1.0333
    assert facts["SPREAD_ENTRADA"].evidence["fraccion_sl"] == 0.04  # 120 / 3000
    ema = facts["PRECIO_VS_EMA200_H1"].evidence
    assert ema["posicion"] == "ENCIMA" and ema["a_favor"] is False  # SELL sobre la EMA
    assert ema["distancia_puntos"] == 6000.0
    assert facts["TIEMPO_ENTRADA"].evidence["sesion"] == "LONDRES_NY"
    assert "estaba por encima de la EMA200 H1" in facts["PRECIO_VS_EMA200_H1"].text
    assert [q["code"] for q in result.data_quality] == ["SIN_DATOS_NOTICIAS"]
    assert "no se puede afirmar ni descartar" in result.data_quality[0]["text"]

    hyps = {h.code: h for h in result.hypotheses}
    assert set(hyps) == {
        "H_CONTRA_TENDENCIA_H1",
        "H_CONTRA_TENDENCIA_M15",
        "H_VOLATILIDAD_ALTA",
        "H_SL_TP_MAL_UBICADO",
        "H_PERDEDORA_CON_MFE",
    }
    for h in hyps.values():
        assert h.confidence == CONFIDENCE_UNVALIDATED == "no_validada"
        assert h.rule_version == 1 and h.supported_by
        assert set(h.supported_by) <= set(facts)  # siempre apoyada en hechos presentes
        assert "pudo" in h.text.lower() or "pudieron" in h.text.lower()
    assert hyps["H_SL_TP_MAL_UBICADO"].supported_by == ("MAE_VS_SL", "MFE_VS_TP")
    assert "75 % del camino al TP" in hyps["H_SL_TP_MAL_UBICADO"].text
    assert "percentil 100" in hyps["H_VOLATILIDAD_ALTA"].text
    # Los hechos no llevan confianza ni soportes.
    assert all(f.confidence is None and not f.supported_by for f in result.facts)


def test_missing_data_produces_notes_not_guesses() -> None:
    inputs = losing_sell()
    op = inputs["operacion"]
    op.update(sl_inicial=None, tp_inicial=None, riesgo=None, mfe_puntos=None, mae_puntos=None)
    op["despliegue"] = None
    inputs["mercado"]["emas"]["ema200_h1"] = {"ok": False, "motivo": "12 velas", "velas": 12}
    inputs["mercado"]["volatilidad"] = {"periodo": 14, "dias": 20, "ok": False, "motivo": "x"}
    inputs["mercado"]["vela_entrada"] = None
    result = build_analysis(inputs)
    codes = _codes(result.findings, FindingKind.FACT)
    for absent in ("MULTIPLO_R", "EXCURSIONES", "MAE_VS_SL", "MFE_VS_TP", "PRECIO_VS_EMA200_H1"):
        assert absent not in codes
    notes = {q["code"] for q in result.data_quality}
    assert {
        "SIN_RIESGO",
        "SIN_EXCURSION",
        "SIN_SL",
        "SIN_TP",
        "PRECIO_VS_EMA200_H1_SIN_DATOS",
        "VOLATILIDAD_SIN_DATOS",
        "SIN_VELA_ENTRADA",
        "SIN_DATOS_NOTICIAS",
        "SIN_DESPLIEGUE",
    } <= notes
    # Sin los hechos de apoyo, las hipótesis correspondientes no aparecen.
    hyps = _codes(result.findings, FindingKind.HYPOTHESIS)
    assert "H_CONTRA_TENDENCIA_H1" not in hyps and "H_VOLATILIDAD_ALTA" not in hyps
    assert hyps == {"H_CONTRA_TENDENCIA_M15"}
    # Sin riesgo, el breakeven usa la comisión: -31 no está dentro de |−1|.
    assert result.outcome == Outcome.LOSS


def test_winner_closed_by_tp_with_management() -> None:
    inputs = losing_sell()
    op = inputs["operacion"]
    op.update(
        direccion="BUY",
        sl_inicial="20270",
        tp_inicial="20360",
        precio_cierre="20360",
        bruto="60",
        neto="59",
        puntos="6000",
        motivo_salida="TP",
        duracion_s=1500,
        mfe_puntos="6100",
        mae_puntos="-1000",
    )
    sl_be = {
        "tipo": "SL_MODIFIED",
        "hora": (ENTRY + timedelta(minutes=10)).isoformat(),
        "precio": None,
        "volumen": None,
        "sl": "20300",
        "tp": "20360",
        "inferido": False,
        "extra": {},
    }
    partial = {**sl_be, "tipo": "PARTIAL_CLOSE", "precio": "20330", "volumen": "0.4"}
    inputs["eventos"] += [sl_be, partial]
    result = build_analysis(inputs)
    assert result.outcome == Outcome.WIN
    facts = {f.code: f for f in result.facts}
    assert facts["TP_ALCANZADO"].evidence == {"minutos": 25.0, "fuente": "cierre por TP"}
    assert facts["MFE_VS_TP"].evidence["supero"] is True
    assert facts["SL_A_BREAKEVEN"].evidence["minutos"] == 10.0
    assert facts["CIERRES_PARCIALES"].evidence["cantidad"] == 1
    assert facts["MODIFICACIONES_SL_TP"].evidence == {"sl": 1, "tp": 0, "inferidas": 0}
    assert facts["PRECIO_VS_EMA200_H1"].evidence["a_favor"] is True
    hyps = _codes(result.findings, FindingKind.HYPOTHESIS)
    assert hyps == {"H_A_FAVOR_TENDENCIA_H1"}  # capturó 6000/6100: no "devolvió el MFE"


def test_winner_that_gave_back_mfe() -> None:
    inputs = losing_sell()
    inputs["operacion"].update(
        direccion="BUY",
        sl_inicial="20270",
        tp_inicial="20400",
        neto="9",
        bruto="10",
        puntos="1000",
        mfe_puntos="6000",
        mae_puntos="-500",
        motivo_salida="EXPERT",
    )
    inputs["mercado"]["toque_tp"] = None
    result = build_analysis(inputs)
    facts = {f.code: f.evidence for f in result.facts}
    assert facts["CAPTURA_MFE"]["captura"] == 0.1667  # 1000 / 6000
    hyps = {h.code: h for h in result.hypotheses}
    assert "H_DEVOLVIO_MFE" in hyps
    assert hyps["H_DEVOLVIO_MFE"].supported_by == ("CAPTURA_MFE", "EXCURSIONES")


def test_tp_touch_from_bars_and_news_fact() -> None:
    inputs = losing_sell()
    inputs["mercado"]["toque_tp"] = (ENTRY + timedelta(minutes=7)).isoformat()
    inputs["operacion"]["mfe_puntos"] = "6200"
    inputs["noticias"] = [{"hora": ENTRY.isoformat(), "titulo": "NFP", "impacto": "HIGH"}]
    result = build_analysis(inputs)
    facts = {f.code: f.evidence for f in result.facts}
    assert facts["TP_ALCANZADO"]["fuente"] == "velas M1" and facts["TP_ALCANZADO"]["minutos"] == 7
    assert "NOTICIA_CERCANA" in facts
    assert "SIN_DATOS_NOTICIAS" not in {q["code"] for q in result.data_quality}
    hyps = _codes(result.findings, FindingKind.HYPOTHESIS)
    assert {"H_TP_TOCADO_SIN_GANAR", "H_NOTICIA_CERCANA"} <= hyps


def test_build_analysis_is_deterministic() -> None:
    inputs = losing_sell()
    first, second = build_analysis(inputs), build_analysis(copy.deepcopy(inputs))
    assert first.findings == second.findings and first.data_quality == second.data_quality


# Reglas -------------------------------------------------------------------------------------


def test_rules_are_declarative_and_versioned() -> None:
    assert RULESET_VERSION >= 1
    codes = [r.code for r in RULES]
    assert len(codes) == len(set(codes)) and all(c.startswith("H_") for c in codes)
    spec = ruleset_spec()
    assert spec[0]["condiciones"][0] == {
        "hecho": "PRECIO_VS_EMA200_H1",
        "campo": "a_favor",
        "op": "==",
        "valor": False,
    }
    assert all(r.version >= 1 and r.conditions for r in RULES)


def test_rule_conditions_and_outcome_filter() -> None:
    facts = {"VOLATILIDAD_ATR_H1": {"percentil": 79.9}}
    assert evaluate(Outcome.LOSS, facts) == []
    facts["VOLATILIDAD_ATR_H1"]["percentil"] = 80.0
    (hyp,) = evaluate(Outcome.LOSS, facts)
    assert hyp.code == "H_VOLATILIDAD_ALTA" and hyp.kind == FindingKind.HYPOTHESIS
    assert hyp.evidence["valores"] == [
        {"hecho": "VOLATILIDAD_ATR_H1", "campo": "percentil", "valor": 80.0}
    ]
    assert evaluate(Outcome.WIN, facts) == []  # la regla es solo para pérdidas
    assert not Condition("X", "y", ">=", 1).matches({"X": {"y": None}})
    assert not Condition("X", "z", ">=", 1).matches({"X": {"y": 2}})


def test_reentry_after_sl_fact_and_hypothesis() -> None:
    inputs = losing_sell()
    inputs["operacion"]["reentrada_de"] = "t-0"
    inputs["eventos"].append(
        {
            "tipo": "REENTRY",
            "hora": ENTRY.isoformat(),
            "precio": "20300",
            "volumen": "1",
            "sl": None,
            "tp": None,
            "inferido": True,
            "extra": {"minutos_desde_cierre_por_sl": 4.5},
        }
    )
    result = build_analysis(inputs)
    facts = {f.code: f.evidence for f in result.facts}
    assert facts["REENTRADA_TRAS_SL"] == {"operacion_previa": "t-0", "minutos": 4.5}
    hyps = {h.code: h for h in result.hypotheses}
    assert hyps["H_REENTRADA_PRECIPITADA"].supported_by == ("REENTRADA_TRAS_SL",)
