"""Réplica de salidas sobre velas M1, hechos previos a la entrada y métricas del diagnóstico
(funciones puras). Unidades: entrada 100, SL a 1 (1 R = 1 de precio)."""

from datetime import UTC, datetime, timedelta

import pytest

from supervisor.analytics import diagnosis as dg
from supervisor.analytics import exit_replay as er
from supervisor.analytics import market
from supervisor.analytics.pre_entry import build_pre_entry_facts, fact_values

T0 = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)


def trade(
    bars, *, buy=True, tp=2.0, real_exit="SL", spread=0.0, cost=0.0, real_close=None, rp=None
):
    bars = tuple(
        er.M1Bar(T0 + timedelta(minutes=1 + i), h, lo, c) for i, (h, lo, c) in enumerate(bars)
    )
    return er.ReplayTrade(
        trade_id="t",
        buy=buy,
        entry_time=T0 + timedelta(seconds=30),
        entry_price=100.0,
        risk_dist=1.0,
        tp_dist=tp,
        spread_price=spread,
        cost_r=cost,
        real_exit=real_exit,
        real_close_time=real_close or T0 + timedelta(minutes=30),
        real_close_price=rp if rp is not None else 99.0,
        bars=bars,
    )


def test_sl_first_when_a_bar_touches_stop_and_target() -> None:
    res = er.replay(trade([(102.5, 98.5, 100.0)]), er.BASELINE)
    assert (res.reason, res.r) == (er.SL, -1.0)


def test_target_and_stop_levels_and_costs() -> None:
    assert er.replay(trade([(100.5, 99.5, 100), (102.1, 100, 102)]), er.BASELINE).r == 2.0
    res = er.replay(trade([(100.5, 99.5, 100), (101, 98.9, 99)], cost=-0.1), er.BASELINE)
    assert (res.reason, res.r, res.r_price) == (er.SL, -1.1, -1.0)
    wide = er.replay(
        trade([(100.5, 99.0, 99.2), (101, 98.4, 99)]), er.ExitRule("x", "sl", sl_k=1.5)
    )
    assert (wide.reason, wide.r) == (er.SL, -1.5)
    short_tp = er.replay(trade([(101.2, 99.5, 101)]), er.ExitRule("x", "tp", tp_m=1.0))
    assert (short_tp.reason, short_tp.r) == (er.TP, 1.0)


def test_breakeven_applies_from_next_bar() -> None:
    rule = er.ExitRule("be", "breakeven", be_r=1.0)
    # Toca +1R y -1R en la MISMA vela: el breakeven aún no vale, sale por el SL.
    same = er.replay(trade([(101.1, 98.9, 100.2), (100.2, 99.5, 99.8)]), rule)
    assert same.reason == er.SL and same.r == -1.0
    # Con una vela de por medio, el breakeven ya vale y sale a 0.
    later = er.replay(trade([(101.1, 100.2, 101.0), (101.0, 99.9, 100.1)]), rule)
    assert later.reason == er.BE and later.r == 0.0


def test_trailing_time_stop_and_horizon() -> None:
    trailing = er.ExitRule("tr", "trailing", trail_r=1.0)
    res = er.replay(trade([(101.5, 100.1, 101.4), (101.6, 100.4, 100.5)], tp=None), trailing)
    assert res.reason == er.TRAIL and res.r == pytest.approx(0.5)
    timed = er.replay(trade([(100.3, 99.8, 100.2)] * 5), er.ExitRule("t", "tiempo", time_min=3))
    assert timed.reason == er.TIME and timed.bars_used == 3
    horizon = er.replay(trade([(100.3, 99.8, 100.2)] * 4), er.BASELINE.__class__("h", "sl"))
    assert horizon.reason == er.HORIZON and horizon.r == pytest.approx(0.2)
    assert er.replay(trade([]), er.BASELINE) is None


def test_sell_uses_ask_with_spread_and_ea_exit_is_respected() -> None:
    # Venta: la vela Bid baja a 98 -> Ask 98.5; el SL (101 Ask) se toca por el spread.
    sell = trade([(100.6, 99.8, 100.2)], buy=False, spread=0.5)
    assert er.replay(sell, er.BASELINE).reason == er.SL
    ea = trade(
        [(100.2, 99.8, 100.0)] * 5,
        real_exit="EXPERT",
        real_close=T0 + timedelta(minutes=3, seconds=40),
        rp=100.4,
    )
    res = er.replay(ea, er.BASELINE)
    assert res.reason == er.EA and res.r == pytest.approx(0.4)


def test_grid_is_small_fixed_and_registered() -> None:
    grid = er.exit_grid()
    assert len(grid) == 20 and len({r.key for r in grid}) == 20
    assert {r.family for r in grid} == {"sl", "tp", "breakeven", "tiempo", "trailing", "sl_tp"}


def test_pre_entry_facts_use_same_shape_as_phase6() -> None:
    ctx = {
        "emas": {
            "ema200_h1": {"ok": True, "valor": 101.0, "ultima_vela": "x"},
            "ema50_h1": {"ok": True, "valor": 99.0, "ultima_vela": "x"},
            "ema200_m15": {"ok": False, "motivo": "pocas velas"},
        },
        "volatilidad": {"ok": True, "atr": 1.0, "percentil": 80, "muestras": 50},
    }
    facts = build_pre_entry_facts(
        buy=True,
        entry_price=100.0,
        point=0.01,
        initial_sl=99.0,
        context=ctx,
        spread_points=10,
        reentry=True,
        news=None,
    )
    h = facts["hechos"]
    assert h["PRECIO_VS_EMA200_H1"]["a_favor"] is False  # compra por debajo de la EMA
    assert h["PRECIO_VS_EMA50_H1"]["a_favor"] is True
    assert h["SPREAD_ENTRADA"]["porcentaje_sl"] == 10.0 and h["REENTRADA_TRAS_SL"]
    assert "PRECIO_VS_EMA200_M15" in facts["sin_datos"] and facts["incompleto"]
    values = fact_values(h)
    assert values["hecho_ema200_h1_a_favor"] is False and values["hecho_ema50_h1_a_favor"] is True
    assert market.market_context  # misma función que usa la fase 6


def test_win_rate_up_but_expectancy_down_is_flagged() -> None:
    p = dg.Params()
    head = {
        "original": {"win_rate": 0.4},
        "sugerida": {"win_rate": 0.7},
        "mejora_r": -0.2,
    }
    preferred, rejected, note = dg._win_rate_flags(head, dg.VALIDATED)
    assert (preferred, rejected) == (False, True) and note
    better = {**head, "mejora_r": 0.3}
    assert dg._win_rate_flags(better, dg.VALIDATED)[:2] == (True, False)
    assert dg._win_rate_flags(better, dg.CANDIDATE)[:2] == (False, False)
    assert p.min_n_oos >= 15


def test_r_stats_reports_wilson_expectancy_and_profit_factor_together() -> None:
    s = dg.r_stats([2.0] * 3 + [-1.0] * 7, dg.Params())
    assert s["win_rate"] == 0.3 and s["win_rate_ic95"][0] < 0.3 < s["win_rate_ic95"][1]
    assert s["expectancy_r"] == pytest.approx(-0.1) and s["profit_factor_r"] == pytest.approx(
        6 / 7, abs=1e-3
    )
    assert s["muestra_pequena"]
    note = dg.win_rate_note(s)
    assert "33 %" in note and "por debajo" in note
