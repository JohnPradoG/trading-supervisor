"""Métricas puras con valores calculados a mano (las cuentas están en los comentarios)."""

import math
from datetime import UTC, datetime, timedelta

import pytest

from supervisor.analytics.statistics import (
    StatsParams,
    TradeResult,
    compute_metrics,
    histogram,
    max_drawdown,
    max_streaks,
    mean_t_interval,
    net_edges,
    r_edges,
    sharpe_ratio,
    sortino_ratio,
    t_quantile_975,
    wilson_interval,
)
from supervisor.models.enums import Outcome

T0 = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
W, L, B = Outcome.WIN, Outcome.LOSS, Outcome.BREAKEVEN


def _series(nets, risk=100.0, balance=10000.0, account="a1", **kw) -> list[TradeResult]:
    return [
        TradeResult(
            trade_id=f"t{i:03d}",
            close_time=T0 + timedelta(hours=i),
            net=net,
            risk=risk,
            commission=0.0,
            balance=balance,
            duration_seconds=600 * (i + 1),
            account_id=account,
            **kw,
        )
        for i, net in enumerate(nets)
    ]


# Serie de referencia: riesgo 100 en todas, balance inicial 10 000.
#   netos   +200  -100  +150  -100  -100  +3   +300
#   R       2     -1    1.5   -1    -1    0.03 3
#   result  W     L     W     L     L     B    W     (+3 <= 0.05 R = 5: breakeven)
NETS = [200.0, -100.0, 150.0, -100.0, -100.0, 3.0, 300.0]


def test_reference_series_all_metrics() -> None:
    m = compute_metrics(_series(NETS), StatsParams(min_sample=30))
    assert (m["n_trades"], m["wins"], m["losses"], m["breakevens"]) == (7, 3, 3, 1)
    assert m["win_rate"] == 0.4286 and m["loss_rate"] == 0.4286  # 3/7
    assert m["breakeven_rate"] == 0.1429
    # Profit factor = (200 + 150 + 300) / (100 + 100 + 100) = 650 / 300
    assert (m["gross_profit"], m["gross_loss"]) == (650.0, -300.0)
    assert m["profit_factor"] == 2.1667 and m["profit_factor_note"] is None
    # Expectancy = 353 / 7; en R = 3.53 / 7
    assert m["expectancy"] == 50.4286
    assert m["expectancy_r"] == 0.5043 and m["n_with_risk"] == 7
    assert (m["average_win"], m["average_loss"]) == (216.6667, -100.0)
    assert (m["largest_win"], m["largest_loss"]) == (300.0, -100.0)
    # Duraciones 600, 1200, ..., 4200 s: media 2400
    assert m["average_duration_seconds"] == 2400.0
    # Acumulado: 200 100 250 150 50 53 353; pico 250 -> valle 50 = 200.
    # En %: 200 / (10 000 + 250) = 1.9512 %
    assert m["max_drawdown"] == 200.0 and m["max_drawdown_pct"] == 1.9512
    assert m["max_drawdown_note"] is None
    # W L W L L B W -> 1 ganadora seguida como máximo, 2 perdedoras
    assert (m["max_consecutive_wins"], m["max_consecutive_losses"]) == (1, 2)
    assert m["cumulative_return"] == 353.0 and m["cumulative_return_pct"] == 3.53
    assert m["cumulative_r"] == 3.53
    assert m["first_close"] == T0 and m["last_close"] == T0 + timedelta(hours=6)
    # n = 7 < 30: Sharpe y Sortino no se calculan y se avisa de la muestra.
    assert m["sharpe"] is None and m["sortino"] is None and m["ratio_basis"] == "R"
    assert "n=7 < 30" in m["sharpe_note"]
    assert "muestra pequeña: 7 operaciones" in m["sample_warning"]
    # Wilson 3/7: centro (3/7 + z²/14) / (1 + z²/7) ± ... = [0.1582, 0.7495]
    assert m["win_rate_ci95"] == {"low": 0.1582, "high": 0.7495}
    # t: media 50.4286 ± 2.446912 x 165.6843 / raíz(7) = ± 153.2325
    assert m["expectancy_ci95"] == {"low": -102.8039, "high": 203.661}
    assert m["expectancy_r_ci95"] == {"low": -1.028, "high": 2.0366}


def test_sharpe_and_sortino_when_sample_is_enough() -> None:
    m = compute_metrics(_series(NETS), StatsParams(min_sample=5))
    # media R 0.504286; s = raíz(16.470771 / 6) = 1.656843 -> Sharpe 0.3044
    assert m["sharpe"] == 0.3044 and m["sharpe_note"] is None
    # Desviación a la baja: raíz((1 + 1 + 1) / 7) = 0.654654 -> Sortino 0.7703
    assert m["sortino"] == 0.7703 and m["sortino_note"] is None
    assert m["sample_warning"] is None


def test_sharpe_gating_reasons() -> None:
    assert sharpe_ratio([1.0] * 40, 30) == (
        None,
        "desviación típica 0: todos los resultados son iguales",
    )
    value, note = sortino_ratio([1.0, 2.0] * 20, 30)
    assert value is None and "sin resultados negativos" in note
    value, note = sharpe_ratio([1.0, -1.0], 30)
    assert value is None and note == "muestra insuficiente: n=2 < 30"


def test_no_losses_profit_factor_is_null_infinite() -> None:
    m = compute_metrics(_series([50.0, 80.0]), StatsParams())
    assert m["profit_factor"] is None
    assert m["profit_factor_note"] == "sin pérdidas: profit factor infinito"
    assert m["average_loss"] is None and m["largest_loss"] is None
    assert m["max_drawdown"] == 0.0 and m["max_consecutive_wins"] == 2


def test_all_breakeven() -> None:
    m = compute_metrics(_series([1.0, -2.0, 0.0]), StatsParams())  # todas dentro de ±5
    assert (m["wins"], m["losses"], m["breakevens"]) == (0, 0, 3)
    assert m["win_rate"] == 0.0 and m["breakeven_rate"] == 1.0
    assert m["profit_factor"] is None and "no definido" in m["profit_factor_note"]
    assert (m["max_consecutive_wins"], m["max_consecutive_losses"]) == (0, 0)
    assert m["average_win"] is None and m["largest_win"] is None
    # Wilson con 0 de 3 no da [0, 0]: [0, 0.5615]
    assert m["win_rate_ci95"] == {"low": 0.0, "high": 0.5615}


def test_single_trade() -> None:
    m = compute_metrics(_series([-100.0]), StatsParams())
    assert m["n_trades"] == 1 and m["losses"] == 1
    assert m["expectancy"] == -100.0 and m["expectancy_r"] == -1.0
    assert m["expectancy_ci95"] is None and m["expectancy_r_ci95"] is None
    assert m["max_drawdown"] == 100.0 and m["max_drawdown_pct"] == 1.0  # 100 / 10 000
    assert m["sharpe"] is None and m["histogram_r"] == [{"from": -1.0, "to": -0.5, "count": 1}]


def test_empty_series() -> None:
    m = compute_metrics([], StatsParams())
    assert m["n_trades"] == 0 and m["win_rate"] is None and m["profit_factor"] is None
    assert m["histogram_net"] == [] and m["sample_warning"].startswith("muestra pequeña")


def test_order_is_by_close_time_not_input_order() -> None:
    trades = list(reversed(_series(NETS)))
    m = compute_metrics(trades, StatsParams())
    assert m["max_drawdown"] == 200.0 and m["max_consecutive_losses"] == 2


def test_ratio_basis_and_drawdown_pct_fallbacks() -> None:
    # Sin riesgo pero con balance: Sharpe sobre retorno % (net / balance x 100).
    no_risk = _series([10.0, -5.0, 20.0, -5.0], risk=None)
    m = compute_metrics(no_risk, StatsParams(min_sample=2))
    assert m["ratio_basis"] == "retorno_pct" and m["n_with_risk"] == 0
    assert m["expectancy_r"] is None and m["cumulative_r"] is None
    # retornos 0.1 -0.05 0.2 -0.05: media 0.05, s = raíz(0.045 / 3) = 0.122474
    assert m["sharpe"] == round(0.05 / math.sqrt(0.015), 4)
    # Sin riesgo: breakeven solo si |neto| <= |comisión| (0): -5 es pérdida.
    assert m["losses"] == 2
    # Ni riesgo ni balance: sin Sharpe, y sin % de drawdown.
    bare = _series([10.0, -5.0], risk=None, balance=None)
    m = compute_metrics(bare, StatsParams(min_sample=2))
    assert m["sharpe"] is None and "ni balance" in m["sharpe_note"]
    assert m["max_drawdown_pct"] is None and "sin balance" in m["max_drawdown_note"]
    # Varias cuentas: no hay capital de referencia común.
    mixed = _series([10.0], account="a") + _series([-5.0], account="b")
    m = compute_metrics(mixed, StatsParams())
    assert m["max_drawdown_pct"] is None and "varias cuentas" in m["max_drawdown_note"]


def test_drawdown_and_streak_helpers() -> None:
    assert max_drawdown([]) == (0.0, None)
    # Empieza perdiendo: el pico es el capital inicial (acumulado 0).
    assert max_drawdown([-10.0, -5.0, 20.0, -30.0], 100.0) == (30.0, 30.0 / 105 * 100)
    assert max_streaks([W, W, B, W, L, L, L, W]) == (2, 3)
    assert max_streaks([]) == (0, 0)


def test_confidence_intervals() -> None:
    assert wilson_interval(0, 0) is None
    low, high = wilson_interval(10, 10)
    assert round(low, 4) == 0.7225 and high == pytest.approx(1.0)
    assert mean_t_interval([5.0]) is None
    # [1, 2, 3]: media 2, s 1, t(2) = 4.302653 -> 2 ± 2.484138
    low, high = mean_t_interval([1.0, 2.0, 3.0])
    assert (round(low, 6), round(high, 6)) == (-0.484138, 4.484138)
    assert t_quantile_975(1) == 12.706205 and t_quantile_975(30) == 2.042272
    # Por encima de la tabla, Cornish-Fisher frente a valores de referencia.
    assert t_quantile_975(40) == pytest.approx(2.021075, abs=1e-5)
    assert t_quantile_975(100) == pytest.approx(1.983972, abs=1e-5)
    assert t_quantile_975(1000) == pytest.approx(1.962339, abs=1e-5)


def test_histograms() -> None:
    edges = r_edges([-1.0, 0.2, 2.1])
    assert edges == [-1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0, 2.5]
    bins = histogram([-1.0, 0.2, 2.1, 2.5], edges)
    assert [b["count"] for b in bins] == [1, 0, 1, 0, 0, 0, 2]  # 2.5 entra en el último
    # Más de 40 tramos de 0.5: el ancho crece en múltiplos de 0.5.
    wide = r_edges([-1.0, 30.0])
    assert wide[1] - wide[0] == 1.0 and len(wide) - 1 <= 40
    # Neto: raíz(4) = 2 tramos iguales entre mínimo y máximo.
    assert net_edges([-10.0, 0.0, 5.0, 10.0]) == [-10.0, 0.0, 10.0]
    assert net_edges([3.0, 3.0]) == [3.0, 3.0]
    assert histogram([3.0, 3.0], [3.0, 3.0]) == [{"from": 3.0, "to": 3.0, "count": 2}]
    m = compute_metrics(_series(NETS), StatsParams())
    assert sum(b["count"] for b in m["histogram_r"]) == 7
    assert sum(b["count"] for b in m["histogram_net"]) == 7
    assert m["histogram_r"][0] == {"from": -1.0, "to": -0.5, "count": 3}
