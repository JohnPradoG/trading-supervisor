"""Trading DNA (fase 8): indicadores contra valores calculados a mano, detección de
estructura sobre secuencias de velas preparadas y cálculo puro de las variables."""

from datetime import UTC, datetime, timedelta

import pytest

from supervisor.analytics import dna_features as df
from supervisor.analytics import structure as st
from supervisor.analytics.binning import bin_index, bin_label, quantile_edges
from supervisor.analytics.indicators import Bar, adx_series, roc, rsi_series

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def bars_from(rows: list[tuple[float, float, float, float]], minutes: int = 5) -> list[Bar]:
    return [
        Bar(T0 + timedelta(minutes=minutes * i), o, h, low, c, minutes)
        for i, (o, h, low, c) in enumerate(rows)
    ]


# Indicadores ---------------------------------------------------------------------------------


def test_rsi_wilder_by_hand() -> None:
    # Cambios 1, 1, -1, 2. Periodo 2: medias iniciales 1 y 0 -> 100; luego 0.5/0.5 -> 50;
    # luego 1.25/0.25 -> RS 5 -> 100 - 100/6.
    series = rsi_series([1, 2, 3, 2, 4], 2)
    assert series[:2] == [None, None]
    assert series[2] == 100.0
    assert series[3] == pytest.approx(50.0)
    assert series[4] == pytest.approx(100 - 100 / 6)
    assert rsi_series([5, 5, 5], 2)[-1] == 50.0  # sin subidas ni bajadas
    assert rsi_series([1, 2], 2) == [None, None]


def test_roc_by_hand() -> None:
    assert roc([100, 101, 102, 110], 3) == pytest.approx(10.0)
    assert roc([100, 101], 3) is None
    assert roc([0, 1, 2, 3], 3) is None


def test_adx_wilder_by_hand() -> None:
    bars = bars_from(
        [
            (9, 10, 8, 9),
            (10, 11, 9, 10.5),
            (11, 12, 10, 11.5),
            (11, 11.5, 9.5, 10),
            (11, 13, 10.5, 12.5),
        ]
    )
    # TR 2, 2, 2, 3; +DM 1, 1, 0, 1.5; -DM 0, 0, 0.5, 0. Periodo 2:
    # i=2: +DI 50, -DI 0, DX 100. i=3: TRs 4, +DMs 1, -DMs 0.5 -> +DI 25, -DI 12.5,
    # DX 33.33 -> ADX (100 + 33.33) / 2. i=4: TRs 5, +DMs 2, -DMs 0.25 -> +DI 40, -DI 5,
    # DX 77.78 -> ADX (66.67 + 77.78) / 2.
    series = adx_series(bars, 2)
    assert series[1].adx is None and series[2].adx is None
    assert series[2].plus_di == pytest.approx(50) and series[2].minus_di == pytest.approx(0)
    assert series[3].plus_di == pytest.approx(25) and series[3].minus_di == pytest.approx(12.5)
    assert series[3].adx == pytest.approx((100 + 100 / 3) / 2)
    assert series[4].plus_di == pytest.approx(40) and series[4].minus_di == pytest.approx(5)
    assert series[4].adx == pytest.approx(((100 + 100 / 3) / 2 + 700 / 9) / 2)


def test_trend_rule() -> None:
    up = [100 + i for i in range(60)]
    assert df.trend_label(up, 5, 10, 5) == "ALCISTA"
    assert df.trend_label(list(reversed(up)), 5, 10, 5) == "BAJISTA"
    assert df.trend_label([100.0] * 60, 5, 10, 5) == "LATERAL"
    with pytest.raises(ValueError):
        df.trend_label(up[:10], 5, 10, 5)


# Estructura ----------------------------------------------------------------------------------

# Swing alto en 2 (13) y swing bajo en 5 (8); la 8 cierra sobre 13 = BOS alcista (primera
# ruptura). Swing alto en 9 (15) y bajo en 10 (11.5); la 13 cierra en 11 < 11.5 = CHOCH.
STRUCTURE = [
    (9.5, 10, 9, 9.8),
    (10.2, 11, 10, 10.8),
    (11.2, 13, 11, 12.5),
    (11.8, 12, 10.5, 11),
    (11, 11, 9.5, 10),
    (9.8, 10, 8, 8.5),
    (9.2, 11, 9, 10.5),
    (11.5, 12, 10, 10.2),
    (12.2, 14, 12, 13.8),
    (13.8, 15, 13, 14.5),
    (14, 14, 11.5, 12),
    (12.2, 13.5, 12, 13),
    (12.6, 13.8, 12.5, 13.5),
    (12.8, 13, 10.5, 11),
]


def test_swings_need_n_closed_bars_each_side() -> None:
    bars = bars_from(STRUCTURE)
    swings = st.find_swings(bars, 2)
    assert [(s.index, s.kind, s.price, s.confirmed_at) for s in swings] == [
        (2, "HIGH", 13, 4),
        (5, "LOW", 8, 7),
        (9, "HIGH", 15, 11),
        (10, "LOW", 11.5, 12),
    ]
    # Sin las 2 velas posteriores, el swing de la 10 no existe todavía.
    assert all(s.index != 10 for s in st.find_swings(bars[:12], 2))


def test_bos_then_choch_and_structure() -> None:
    scan = st.scan_structure(bars_from(STRUCTURE), 2)
    assert [(b.index, b.direction, b.kind, b.level) for b in scan.breaks] == [
        (8, st.UP, "BOS", 13),
        (13, st.DOWN, "CHOCH", 11.5),
    ]
    assert st.market_structure(scan.swings) == "HH_HL"
    assert scan.sweeps == []


def test_second_break_same_direction_is_bos() -> None:
    rows = STRUCTURE[:12] + [(13, 15.5, 12.8, 15.4)]  # cierra sobre el swing alto 15
    scan = st.scan_structure(bars_from(rows), 2)
    assert [(b.index, b.kind, b.direction) for b in scan.breaks] == [
        (8, "BOS", st.UP),
        (12, "BOS", st.UP),
    ]


def test_liquidity_sweep() -> None:
    rows = STRUCTURE[:8] + [(10.4, 13.5, 10.2, 12.5)]  # supera 13 y cierra por debajo
    scan = st.scan_structure(bars_from(rows), 2)
    assert [(s.index, s.direction, s.level) for s in scan.sweeps] == [(8, st.UP, 13)]
    assert scan.breaks == []


def test_order_blocks() -> None:
    bars = bars_from(STRUCTURE)
    scan = st.scan_structure(bars, 2)
    blocks = st.order_blocks(bars, scan.breaks, 50, 10)
    # BOS alcista en 8: última vela bajista antes = 7 [10, 12]. CHOCH bajista en 13: última
    # vela alcista antes = 12 [12.5, 13.8].
    assert [(z.index, z.direction, z.low, z.high) for z in blocks] == [
        (7, st.UP, 10, 12),
        (12, st.DOWN, 12.5, 13.8),
    ]
    # Un cierre posterior por debajo de 10 invalida el order block alcista.
    more = bars_from([*STRUCTURE, (11, 11, 9, 9.5)])
    scan = st.scan_structure(more, 2)
    assert [z.index for z in st.order_blocks(more, scan.breaks, 50, 10)] == [12]
    assert st.nearest_zone(blocks, 13.0).index == 12
    assert st.nearest_zone(blocks, 11.0).distance(11.0) == 0
    assert st.nearest_zone([], 1.0) is None


def test_fvg_open_and_filled() -> None:
    rows = [
        (10, 11, 9.5, 10.8),
        (11, 13, 10.9, 12.8),
        (12.9, 14, 12, 13.5),  # bajo 12 > alto 11 de dos velas antes: FVG alcista [11, 12]
        (13.5, 14.5, 11.5, 14),  # entra en el hueco pero no llega a 11
    ]
    zones = st.open_fvgs(bars_from(rows), 50, 0.5)
    assert [(z.index, z.direction, z.low, z.high) for z in zones] == [(2, st.UP, 11, 12)]
    assert zones[0].distance(11.5) == 0 and zones[0].distance(13) == 1
    assert st.open_fvgs(bars_from(rows), 50, 1.5) == []  # más pequeño que el mínimo
    filled = [*rows, (14, 14, 10.9, 11.2)]
    assert st.open_fvgs(bars_from(filled), 50, 0.5) == []
    bearish = [(14, 14.5, 13, 13.2), (13, 13.1, 11, 11.5), (11.5, 12, 10, 10.5)]
    zones = st.open_fvgs(bars_from(bearish), 50, 0.5)
    assert [(z.direction, z.low, z.high) for z in zones] == [(st.DOWN, 12, 13)]


def test_equal_highs_and_lows() -> None:
    rows = [
        (10, 11, 9, 10),
        (10, 12, 9.5, 11),
        (11, 13.0, 10, 12),
        (12, 12, 10.5, 11),
        (11, 11.5, 10, 10.5),
        (10.5, 12, 10.2, 11.5),
        (11.5, 13.05, 11, 12),
        (12, 12.5, 10.8, 11),
        (11, 12, 10.5, 11.5),
    ]
    bars = bars_from(rows)
    swings = st.find_swings(bars, 2)
    highs = [s.price for s in swings if s.kind == "HIGH"]
    assert highs == [13.0, 13.05]
    assert st.equal_levels(bars, swings, "HIGH", 0.1, 50)
    assert not st.equal_levels(bars, swings, "HIGH", 0.01, 50)
    swept = bars_from([*rows, (11.5, 13.2, 11, 12)])
    assert not st.equal_levels(swept, st.find_swings(swept, 2), "HIGH", 0.1, 50)
    assert not st.equal_levels(bars, swings, "LOW", 0.1, 50)


# Tramos ----------------------------------------------------------------------------------


def test_quantile_bins() -> None:
    values = list(range(1, 9))  # 1..8
    edges = quantile_edges(values, 4)
    assert edges == [3, 5, 7]
    assert [bin_index(edges, v) for v in (1, 3, 4.9, 7, 100)] == [0, 1, 1, 3, 3]
    assert bin_label(edges, 0) == "< 3" and bin_label(edges, 1) == "[3, 5)"
    assert bin_label(edges, 3) == ">= 7"
    assert quantile_edges([2, 2, 2, 2], 4) == []  # sin tramos vacíos
    assert quantile_edges([], 4) == []


# Cálculo completo ------------------------------------------------------------------------


def _series(tf: str, count: int, end: datetime, slope: float, start: float = 100.0) -> list[Bar]:
    minutes = df.TF_MINUTES[tf]
    out = []
    for i in range(count):
        t = end - timedelta(minutes=minutes * (count - i))
        # Zigzag sobre la tendencia para que haya swings.
        p = start + slope * i + (0.3 if i % 4 < 2 else -0.3)
        out.append(Bar(t, p - 0.05, p + 0.4, p - 0.4, p, minutes))
    return out


def _inputs(direction: str = "BUY", slope: float = 0.1, **overrides) -> df.DnaInputs:
    entry = datetime(2026, 9, 15, 10, 30, tzinfo=UTC)
    from supervisor.analytics.market import floor_time

    cutoffs = {tf: floor_time(entry, m) for tf, m in df.TF_MINUTES.items()}
    counts = {"M1": 450, "M5": 450, "M15": 450, "H1": 500, "H4": 130, "D1": 25}
    bars = {tf: _series(tf, n, cutoffs[tf], slope) for tf, n in counts.items()}
    last = bars["M5"][-1].close
    values = dict(
        direction=direction,
        entry_time=entry,
        entry_price=last,
        point=0.01,
        main_tf="M5",
        main_tf_note=None,
        bars=bars,
        cutoffs=cutoffs,
        spread={"puntos": 20, "vela": "x"},
        news=None,
    )
    values.update(overrides)
    return df.DnaInputs(**values)


def test_build_dna_relative_features_flip_with_direction() -> None:
    buy = df.build_dna(_inputs("BUY"))
    sell = df.build_dna(_inputs("SELL"))
    assert set(buy.features) == set(df.FEATURES_BY_NAME)
    for tf in ("m1", "m5", "m15", "h1", "h4", "d1"):
        assert buy.features[f"tendencia_{tf}"] == "ALCISTA"
        assert buy.features[f"tendencia_{tf}_rel"] == "A_FAVOR"
        assert sell.features[f"tendencia_{tf}_rel"] == "EN_CONTRA"
    assert buy.features["ema50_dist_atr"] > 0
    assert buy.features["ema50_dist_atr_rel"] == buy.features["ema50_dist_atr"]
    assert sell.features["ema50_dist_atr_rel"] == pytest.approx(-buy.features["ema50_dist_atr"])
    assert sell.features["rsi14_rel"] == pytest.approx(100 - buy.features["rsi14"])
    assert buy.features["tf_indicadores"] == "M5"
    assert buy.features["spread_puntos"] == 20
    assert buy.features["spread_atr"] == pytest.approx(20 * 0.01 / buy.features["atr14"], rel=1e-4)
    assert buy.features["sesion"] == "LONDRES" and buy.features["hora_utc"] == 10
    assert buy.features["dia_semana"] == "martes"
    assert buy.features["volatilidad_relativa"] is not None
    assert buy.features["dist_max_dia_anterior_atr"] is not None
    assert buy.features["sesion_anterior"] == "ASIA"
    assert buy.quality["timeframes"]["M5"]["velas"] == 450


def test_build_dna_nulls_have_reasons() -> None:
    inputs = _inputs()
    inputs.bars["D1"] = inputs.bars["D1"][:5]
    inputs.bars["M5"] = inputs.bars["M5"][-60:]  # sin EMA200 ni estructura completas
    inputs.spread = None
    result = df.build_dna(inputs)
    assert result.features["tendencia_d1"] is None
    assert "hacen falta 20" in result.null_reasons["tendencia_d1"]
    assert (
        result.features["ema200"] is None and "hacen falta 400" in (result.null_reasons["ema200"])
    )
    assert result.features["ema20"] is not None  # 40 velas bastan
    assert result.features["estructura"] is None
    assert "hacen falta 100" in result.null_reasons["estructura"]
    assert result.features["spread_puntos"] is None
    assert "5 minutos" in result.null_reasons["spread_puntos"]
    for name in ("noticia_cercana", "minutos_a_noticia", "impacto_noticia", "tipo_noticia"):
        assert result.features[name] is None
        assert result.null_reasons[name] == df.NO_NEWS_SOURCE
    assert all(result.null_reasons[n] for n, v in result.features.items() if v is None)
    assert not any(n in result.null_reasons for n, v in result.features.items() if v is not None)


def test_build_dna_low_coverage_and_stale_bars() -> None:
    inputs = _inputs()
    inputs.bars["H1"] = [Bar(b.time, b.open, b.high, b.low, b.close, 30) for b in inputs.bars["H1"]]
    result = df.build_dna(inputs)
    assert result.features["tendencia_h1"] is None
    assert "cobertura" in result.null_reasons["tendencia_h1"]
    stale = _inputs()
    stale.bars["M15"] = [
        Bar(b.time - timedelta(days=10), b.open, b.high, b.low, b.close, 15)
        for b in stale.bars["M15"]
    ]
    assert "demasiado antes" in df.build_dna(stale).null_reasons["tendencia_m15"]


def test_build_dna_with_news_source() -> None:
    news = [
        {"minutos": -90.0, "impacto": "HIGH", "titulo": "CPI"},
        {"minutos": 25.0, "impacto": "MEDIUM", "titulo": "PMI"},
    ]
    result = df.build_dna(_inputs(news=news))
    assert result.features["noticia_cercana"] is True
    assert result.features["minutos_a_noticia"] == 25.0
    assert result.features["impacto_noticia"] == "MEDIUM"
    assert result.features["tipo_noticia"] == "PMI"
    empty = df.build_dna(_inputs(news=[]))
    assert empty.features["noticia_cercana"] is False
    assert empty.null_reasons["impacto_noticia"].startswith("no aplica")


def test_feature_catalog_is_consistent() -> None:
    names = [f.name for f in df.FEATURES]
    assert len(names) == len(set(names))
    groups = {g for g, _ in df.GROUPS}
    for f in df.FEATURES:
        assert f.value_type in ("numeric", "boolean", "categorical")
        assert f.group in groups
        assert len(f.name) <= 64 and len(f.label) <= 128
        assert f.formula and f.null_policy
