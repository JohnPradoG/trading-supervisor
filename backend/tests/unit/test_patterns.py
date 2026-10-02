"""Fase 9: estadística pura de la búsqueda de patrones (t de Student, Welch, FDR, tramos solo
con entrenamiento, criterios de validación y descripción en castellano)."""

import random
from datetime import UTC, datetime, timedelta

import pytest

from supervisor.analytics import patterns as pt
from supervisor.analytics.binning import quantile_edges

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def test_betainc_and_t_distribution_by_hand() -> None:
    assert pt.betainc(1, 1, 0.3) == pytest.approx(0.3)
    # I_0.4(2, 3) = sum_{j=2..4} C(4, j) 0.4^j 0.6^(4-j) = 0.3456 + 0.1536 + 0.0256
    assert pt.betainc(2, 3, 0.4) == pytest.approx(0.5248)
    assert pt.t_two_sided_p(0.0, 7) == pytest.approx(1.0)
    assert pt.t_two_sided_p(1.0, 1) == pytest.approx(0.5)  # Cauchy
    assert pt.t_two_sided_p(2.228139, 10) == pytest.approx(0.05, abs=1e-5)
    assert pt.t_two_sided_p(-2.570582, 5) == pytest.approx(0.05, abs=1e-5)
    assert pt.t_two_sided_p(1.959964, 1e7) == pytest.approx(0.05, abs=1e-5)


def test_welch_by_hand() -> None:
    a, b = [1, 2, 3, 4], [2, 4, 6, 8, 10]
    # medias 2.5 y 6, varianzas 5/3 y 10: se² = 5/12 + 2, df = se²² / ((5/12)²/3 + 2²/4)
    w = pt.welch_test(a, b)
    se2 = 5 / 12 + 2
    assert w.diff == pytest.approx(-3.5)
    assert w.t == pytest.approx(-3.5 / se2**0.5)
    assert w.df == pytest.approx(se2**2 / ((5 / 12) ** 2 / 3 + 1))
    assert w.p_value == pytest.approx(pt.t_two_sided_p(w.t, w.df))
    assert 0.05 < w.p_value < 0.1
    assert w.ci_low < -3.5 < w.ci_high < 0.5
    assert pt.welch_test([1], [1, 2]) is None
    assert pt.welch_test([1, 1], [1, 1]) is None


def test_benjamini_hochberg_by_hand() -> None:
    # Ordenados 0.005, 0.01, 0.03, 0.04 (m = 4): 0.04, min(0.04, 0.04), 0.02, min(0.02, 0.02)
    assert pt.benjamini_hochberg([0.01, 0.04, 0.03, 0.005]) == pytest.approx(
        [0.02, 0.04, 0.04, 0.02]
    )
    assert pt.benjamini_hochberg([0.9, 0.95]) == pytest.approx([0.95, 0.95])
    assert pt.benjamini_hochberg([]) == []


def _obs(i: int, r: float, **values) -> pt.Obs:
    return pt.Obs(f"t{i:04d}", T0 + timedelta(hours=i), r, r > 0, values)


def _dataset(n: int, seed: int, planted: bool) -> list[pt.Obs]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        values = {
            "tendencia_h1_rel": rng.choice(["A_FAVOR", "EN_CONTRA", "LATERAL"]),
            "sesion": rng.choice(["ASIA", "LONDRES", "LONDRES_NY", "NUEVA_YORK", "FUERA_SESION"]),
            "rsi14": rng.uniform(20, 80),
            "bos_reciente": rng.random() < 0.5,
            "adx14": rng.uniform(10, 50),
            "fvg_cercano": rng.random() < 0.4,
        }
        if planted and values["tendencia_h1_rel"] == "EN_CONTRA":
            r = 1.0 if rng.random() < 0.1 else -1.0
        else:
            r = 1.5 if rng.random() < 0.5 else -1.0
        out.append(_obs(i, r, **values))
    return out


VARS = [
    pt.Variable("tendencia_h1_rel", "categorical", "Tendencia H1 respecto a la operación"),
    pt.Variable("sesion", "categorical", "Sesión"),
    pt.Variable("rsi14", "numeric", "RSI(14)"),
    pt.Variable("bos_reciente", "boolean", "BOS en las últimas 20 velas"),
    pt.Variable("adx14", "numeric", "ADX(14)"),
    pt.Variable("fvg_cercano", "boolean", "FVG sin rellenar cerca"),
]


def test_search_finds_planted_trap() -> None:
    result = pt.search(_dataset(240, 7, planted=True), VARS, pt.SearchParams())
    traps = [c for c in result.candidates if c.kind == pt.TRAP]
    keys = {c.condition.key() for c in traps}
    single = pt.Condition((pt.Clause("tendencia_h1_rel", "eq", value="EN_CONTRA"),))
    assert single.key() in keys
    found = next(c for c in traps if c.condition.key() == single.key())
    assert found.stats["expectancy_r"] < -0.5 and found.p_adjusted < 0.001
    assert result.tested == len(result.candidates) and result.generated >= result.tested
    # La trampa más significativa es la plantada; en entrenamiento pueden colarse condiciones
    # correlacionadas por azar (para eso están validación y fuera de muestra).
    assert min(traps, key=lambda c: c.p_adjusted).condition.key() == single.key()
    assert sum("EN_CONTRA" in c.condition.key() for c in traps) > len(traps) / 2


def test_search_on_noise_finds_nothing_after_fdr() -> None:
    noise = _dataset(400, 11, planted=False)
    result = pt.search(noise, VARS, pt.SearchParams())
    assert result.tested > 100  # muchas candidatas: el FDR tiene trabajo
    assert [c for c in result.candidates if c.kind is not None] == []
    # Sin corrección alguna habría "hallazgos": p < 0.05 sin ajustar.
    assert any(c.welch.p_value < 0.05 for c in result.candidates)


def test_minimum_sample_gating() -> None:
    obs = _dataset(100, 3, planted=True)
    rare = [
        pt.Obs(o.trade_id, o.close_time, o.r, o.win, {**o.values, "raro": i < 10})
        for i, o in enumerate(obs)
    ]
    variables = [pt.Variable("raro", "boolean", "Raro")]
    result = pt.search(rare, variables, pt.SearchParams(min_n_train=30))
    # raro = sí tiene 10 (< 30): no se prueba; raro = no tiene 90 pero su complemento 10.
    assert result.tested == 0 and result.low_sample == 2


def test_numeric_bins_come_from_training_only() -> None:
    train = _dataset(200, 5, planted=False)
    clauses = pt.single_clauses(train, [pt.Variable("rsi14", "numeric", "RSI")], 4)
    edges = quantile_edges([o.values["rsi14"] for o in train], 4)
    assert [(c.low, c.high) for c in clauses] == [
        (None, edges[0]),
        (edges[0], edges[1]),
        (edges[1], edges[2]),
        (edges[2], None),
    ]
    # Un valor futuro fuera del rango de entrenamiento cae en el último tramo, sin moverlo.
    future = _obs(999, 1.0, rsi14=99.0)
    assert clauses[-1].matches(future) and not clauses[0].matches(future)
    assert not clauses[0].known(_obs(1000, 1.0))  # sin dato: ni cumple ni complemento


def _stats(n: int, exp: float, ci: tuple[float, float], diff: float) -> dict:
    return {
        "n": n,
        "n_complemento": 50,
        "expectancy_r": exp,
        "expectancy_r_ic95": list(ci),
        "diferencia_r": diff,
    }


def test_segment_checks() -> None:
    assert pt.segment_check(pt.TRAP, _stats(20, -0.8, (-1.1, -0.5), -1.0), 15)[0]
    ok, reason = pt.segment_check(pt.TRAP, _stats(10, -0.8, (-1.1, -0.5), -1.0), 15)
    assert not ok and "muestra insuficiente" in reason
    ok, reason = pt.segment_check(pt.TRAP, _stats(20, -0.2, (-0.6, 0.2), -0.5), 15)
    assert not ok and "cruza 0" in reason
    ok, reason = pt.segment_check(pt.TRAP, _stats(20, -0.2, (-0.4, -0.1), 0.3), 15)
    assert not ok and "cambia de sentido" in reason
    assert pt.segment_check(pt.EDGE, _stats(20, 0.6, (0.2, 1.0), 0.5), 15)[0]
    assert not pt.segment_check(pt.EDGE, _stats(20, 0.6, (-0.1, 1.0), 0.5), 15)[0]


def test_forward_decay_rule() -> None:
    assert pt.forward_decayed(pt.TRAP, _stats(25, 0.6, (0.2, 1.0), 0.5), 20)[0]
    decayed, reason = pt.forward_decayed(pt.TRAP, _stats(25, 0.2, (-0.2, 0.6), 0.3), 20)
    assert not decayed and "sentido contrario" in reason
    assert not pt.forward_decayed(pt.TRAP, _stats(10, 0.9, (0.5, 1.2), 1.0), 20)[0]
    assert pt.forward_decayed(pt.EDGE, _stats(25, -0.6, (-1.0, -0.2), -0.5), 20)[0]


def test_train_kind() -> None:
    assert pt.train_kind(_stats(40, -0.5, (-1, 0), -0.8), 0.01, 0.05) == pt.TRAP
    assert pt.train_kind(_stats(40, 0.5, (0, 1), 0.8), 0.01, 0.05) == pt.EDGE
    assert pt.train_kind(_stats(40, -0.5, (-1, 0), -0.8), 0.2, 0.05) is None
    # Peor que el resto pero con expectativa positiva: no es trampa.
    assert pt.train_kind(_stats(40, 0.1, (-1, 1), -0.8), 0.01, 0.05) is None


def test_describe_in_plain_spanish() -> None:
    labels = {"tendencia_h1_rel": "Tendencia H1 respecto a la operación", "rsi14": "RSI(14)"}
    spec = pt.Condition(
        (
            pt.Clause("tendencia_h1_rel", "eq", value="EN_CONTRA"),
            pt.Clause("sesion", "eq", value="ASIA"),
        )
    ).spec()
    assert pt.describe(spec, labels) == "Entrar contra la tendencia H1 y durante la sesión de Asia"
    rng = pt.Condition((pt.Clause("rsi14", "range", low=30, high=50, bin="[30, 50)"),)).spec()
    assert pt.describe(rng, labels) == "Entrar con RSI(14) [30, 50)"
    assert pt.Condition.from_spec(spec).key() == pt.condition_key(spec)
    rule = {"tipo": "regla", "regla": "H_X", "texto": "Entrar contra la tendencia H1"}
    assert pt.describe(rule, labels) == "Entrar contra la tendencia H1"
