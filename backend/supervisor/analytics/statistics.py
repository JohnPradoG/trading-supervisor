"""Métricas de una serie de operaciones cerradas (funciones puras, sin base de datos).

Convenciones (las mismas que muestra la API en `parameters`):

- Orden: por hora de cierre (y trade_id para desempatar). La curva de equity, el drawdown y
  las rachas siguen ese orden.
- Resultado por operación: WIN / LOSS / BREAKEVEN con la regla de `outcome.classify_outcome`.
  win_rate y loss_rate se calculan sobre todas las operaciones (los breakeven cuentan en n).
- Profit factor = suma de netos de las WIN / |suma de netos de las LOSS|. Sin pérdidas es
  infinito: se devuelve null con `profit_factor_note` (JSON no admite infinito). Sin
  ganancias ni pérdidas (todo breakeven) también es null.
- Expectancy = neto medio por operación (dinero) y R medio (neto / riesgo inicial) sobre las
  operaciones con riesgo conocido; `n_with_risk` dice cuántas son.
- Max drawdown en dinero: mayor caída desde un máximo de la curva de neto acumulado (que
  empieza en 0). En %: sobre un capital de referencia = balance al entrar de la primera
  operación de la serie más el neto acumulado; solo si todas son de la misma cuenta y hay
  balance (si no, null con el motivo). Es aproximado: el balance real de la cuenta incluye
  operaciones de otros bots.
- Rachas: máximas operaciones WIN (o LOSS) seguidas; un BREAKEVEN corta ambas rachas.
- Sharpe y Sortino por operación, sin anualizar (anualizar exigiría suponer una frecuencia de
  operaciones constante). Base: R por operación si todas tienen riesgo; si no, retorno % sobre
  el balance al entrar si todas lo tienen; si no, null. Solo con n >= min_sample (30 por
  defecto) y desviación > 0. Sharpe = media / desviación típica muestral (n - 1). Sortino =
  media / desviación a la baja con objetivo 0: raíz(suma(min(x, 0)^2) / n); null si no hay
  ningún resultado negativo.
- Intervalos al 95 %: win rate con Wilson (no se sale de [0, 1] y se comporta bien con n
  pequeño); expectancy con t de Student (media ± t(0.975, n-1) x s / raíz(n)), que supone
  aproximadamente normal la media muestral: con muestras pequeñas y colas gordas es optimista,
  de ahí el aviso de muestra pequeña. Se eligió t y no bootstrap porque es determinista y
  barato en un VPS de 1 vCPU.
- Distribución: histograma de R (ancho 0.5 R, ampliado en múltiplos de 0.5 si salen más de
  40 tramos) y de neto (min(20, raíz(n)) tramos de igual ancho). Cada tramo es [desde, hasta)
  salvo el último, que incluye su límite superior.
"""

import bisect
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from supervisor.analytics.outcome import DEFAULT_BREAKEVEN_R_FRACTION, classify_outcome
from supervisor.models.enums import Outcome

Z_95 = 1.959963984540054
MAX_R_BINS = 40
MAX_NET_BINS = 20

# t de Student, cuantil 0.975, para 1..30 grados de libertad.
_T_975 = (
    12.706205,
    4.302653,
    3.182446,
    2.776445,
    2.570582,
    2.446912,
    2.364624,
    2.306004,
    2.262157,
    2.228139,
    2.200985,
    2.178813,
    2.160369,
    2.144787,
    2.131450,
    2.119905,
    2.109816,
    2.100922,
    2.093024,
    2.085963,
    2.079614,
    2.073873,
    2.068658,
    2.063899,
    2.059539,
    2.055529,
    2.051831,
    2.048407,
    2.045230,
    2.042272,
)


@dataclass(frozen=True)
class TradeResult:
    trade_id: str
    close_time: datetime
    net: float
    risk: float | None = None
    commission: float | None = None
    balance: float | None = None
    duration_seconds: int | None = None
    account_id: str | None = None


@dataclass(frozen=True)
class StatsParams:
    min_sample: int = 30
    breakeven_r_fraction: float = DEFAULT_BREAKEVEN_R_FRACTION


# Utilidades ---------------------------------------------------------------------------------


def _r(value: float | None, places: int = 4) -> float | None:
    if value is None:
        return None
    return round(value, places)


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def sample_std(values: Sequence[float]) -> float:
    """Desviación típica muestral (n - 1). Requiere n >= 2."""
    m = mean(values)
    return math.sqrt(sum((x - m) ** 2 for x in values) / (len(values) - 1))


def t_quantile_975(df: int) -> float:
    """Cuantil 0.975 de la t de Student: tabla exacta hasta 30 grados de libertad y expansión
    de Cornish-Fisher (error < 0.001) por encima."""
    if df < 1:
        raise ValueError("df debe ser >= 1")
    if df <= len(_T_975):
        return _T_975[df - 1]
    z = Z_95
    g1 = (z**3 + z) / 4
    g2 = (5 * z**5 + 16 * z**3 + 3 * z) / 96
    g3 = (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) / 384
    return z + g1 / df + g2 / df**2 + g3 / df**3


def wilson_interval(successes: int, n: int, z: float = Z_95) -> tuple[float, float] | None:
    """Intervalo de Wilson para una proporción. None si n = 0."""
    if n <= 0:
        return None
    p = successes / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def mean_t_interval(values: Sequence[float]) -> tuple[float, float] | None:
    """Intervalo t al 95 % de la media. None con menos de 2 valores."""
    n = len(values)
    if n < 2:
        return None
    m = mean(values)
    half = t_quantile_975(n - 1) * sample_std(values) / math.sqrt(n)
    return m - half, m + half


def max_drawdown(
    nets: Sequence[float], initial_capital: float | None = None
) -> tuple[float, float | None]:
    """Mayor caída de la curva de neto acumulado (en dinero, >= 0) y, con capital, la mayor
    caída en % del máximo de equity (capital + acumulado)."""
    cumulative = peak = 0.0
    worst = 0.0
    worst_pct = 0.0
    for net in nets:
        cumulative += net
        peak = max(peak, cumulative)
        worst = max(worst, peak - cumulative)
        if initial_capital is not None:
            peak_equity = initial_capital + peak
            if peak_equity > 0:
                worst_pct = max(worst_pct, (peak - cumulative) / peak_equity * 100)
    return worst, (worst_pct if initial_capital is not None else None)


def max_streaks(outcomes: Sequence[Outcome]) -> tuple[int, int]:
    """Máximo de WIN seguidas y de LOSS seguidas. BREAKEVEN corta las dos."""
    best_w = best_l = run_w = run_l = 0
    for outcome in outcomes:
        if outcome == Outcome.WIN:
            run_w, run_l = run_w + 1, 0
        elif outcome == Outcome.LOSS:
            run_w, run_l = 0, run_l + 1
        else:
            run_w = run_l = 0
        best_w, best_l = max(best_w, run_w), max(best_l, run_l)
    return best_w, best_l


def sharpe_ratio(values: Sequence[float], min_sample: int) -> tuple[float | None, str | None]:
    n = len(values)
    if n < min_sample:
        return None, f"muestra insuficiente: n={n} < {min_sample}"
    std = sample_std(values)
    if std == 0:
        return None, "desviación típica 0: todos los resultados son iguales"
    return mean(values) / std, None


def sortino_ratio(values: Sequence[float], min_sample: int) -> tuple[float | None, str | None]:
    n = len(values)
    if n < min_sample:
        return None, f"muestra insuficiente: n={n} < {min_sample}"
    downside = math.sqrt(sum(min(x, 0.0) ** 2 for x in values) / n)
    if downside == 0:
        return None, "sin resultados negativos: la desviación a la baja es 0"
    return mean(values) / downside, None


def histogram(values: Sequence[float], edges: Sequence[float]) -> list[dict[str, Any]]:
    """Recuento por tramos [edges[i], edges[i+1]); el último tramo incluye su límite."""
    counts = [0] * (len(edges) - 1)
    for value in values:
        if value < edges[0] or value > edges[-1]:
            continue
        index = min(bisect.bisect_right(edges, value) - 1, len(counts) - 1)
        counts[index] += 1
    return [
        {"from": _r(edges[i]), "to": _r(edges[i + 1]), "count": counts[i]}
        for i in range(len(counts))
    ]


def r_edges(values: Sequence[float], width: float = 0.5) -> list[float]:
    low = math.floor(min(values) / width) * width
    high = math.ceil(max(values) / width) * width
    if high == low:
        high = low + width
    bins = round((high - low) / width)
    if bins > MAX_R_BINS:
        width = width * math.ceil(bins / MAX_R_BINS)
        low = math.floor(min(values) / width) * width
        high = math.ceil(max(values) / width) * width
        if high == low:
            high = low + width
        bins = round((high - low) / width)
    return [low + i * width for i in range(bins + 1)]


def net_edges(values: Sequence[float]) -> list[float]:
    low, high = min(values), max(values)
    if high == low:
        return [low, high]
    bins = max(1, min(MAX_NET_BINS, math.ceil(math.sqrt(len(values)))))
    width = (high - low) / bins
    return [low + i * width for i in range(bins)] + [high]


# Métricas ------------------------------------------------------------------------------------


def _interval(pair: tuple[float, float] | None, places: int = 4) -> dict | None:
    if pair is None:
        return None
    return {"low": _r(pair[0], places), "high": _r(pair[1], places)}


def compute_metrics(trades: Sequence[TradeResult], params: StatsParams) -> dict[str, Any]:
    """Todas las métricas de la sección 9 para una serie de operaciones cerradas."""
    ordered = sorted(trades, key=lambda t: (t.close_time, t.trade_id))
    n = len(ordered)
    outcomes = [
        classify_outcome(t.net, t.risk, t.commission, params.breakeven_r_fraction).outcome
        for t in ordered
    ]
    nets = [t.net for t in ordered]
    wins = [t.net for t, o in zip(ordered, outcomes, strict=True) if o == Outcome.WIN]
    losses = [t.net for t, o in zip(ordered, outcomes, strict=True) if o == Outcome.LOSS]
    n_be = n - len(wins) - len(losses)
    r_values = [t.net / t.risk for t in ordered if t.risk is not None and t.risk > 0]

    result: dict[str, Any] = {
        "n_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "breakevens": n_be,
        "sample_warning": None,
    }
    if n < params.min_sample:
        result["sample_warning"] = (
            f"muestra pequeña: {n} operaciones (< {params.min_sample}); las métricas son "
            "orientativas y no demuestran una ventaja"
        )
    if n == 0:
        result.update(
            {
                key: None
                for key in (
                    "win_rate",
                    "loss_rate",
                    "breakeven_rate",
                    "win_rate_ci95",
                    "gross_profit",
                    "gross_loss",
                    "profit_factor",
                    "profit_factor_note",
                    "expectancy",
                    "expectancy_ci95",
                    "expectancy_r",
                    "expectancy_r_ci95",
                    "average_win",
                    "average_loss",
                    "largest_win",
                    "largest_loss",
                    "average_duration_seconds",
                    "max_drawdown",
                    "max_drawdown_pct",
                    "max_drawdown_note",
                    "max_consecutive_wins",
                    "max_consecutive_losses",
                    "sharpe",
                    "sortino",
                    "ratio_basis",
                    "sharpe_note",
                    "sortino_note",
                    "cumulative_return",
                    "cumulative_return_pct",
                    "cumulative_r",
                    "first_close",
                    "last_close",
                )
            }
        )
        result.update({"n_with_risk": 0, "histogram_r": [], "histogram_net": []})
        return result

    gross_profit = sum(wins)
    gross_loss = sum(losses)
    if losses and gross_loss < 0:
        profit_factor, pf_note = gross_profit / abs(gross_loss), None
    elif wins:
        profit_factor, pf_note = None, "sin pérdidas: profit factor infinito"
    else:
        profit_factor, pf_note = None, "sin ganancias ni pérdidas: no definido"

    accounts = {t.account_id for t in ordered}
    capital = ordered[0].balance
    if len(accounts) > 1:
        capital, dd_note = None, "varias cuentas: no hay un capital de referencia común"
    elif capital is None or capital <= 0:
        capital, dd_note = None, "sin balance al entrar en la primera operación"
    else:
        dd_note = None
    dd_money, dd_pct = max_drawdown(nets, capital)
    best_w, best_l = max_streaks(outcomes)

    if r_values and len(r_values) == n:
        basis, ratio_values = "R", r_values
    elif all(t.balance is not None and t.balance > 0 for t in ordered):
        basis = "retorno_pct"
        ratio_values = [t.net / t.balance * 100 for t in ordered]  # type: ignore[operator]
    else:
        basis, ratio_values = None, []
    if basis is None:
        sharpe = sortino = None
        sharpe_note = sortino_note = "no todas las operaciones tienen riesgo ni balance al entrar"
    else:
        sharpe, sharpe_note = sharpe_ratio(ratio_values, params.min_sample)
        sortino, sortino_note = sortino_ratio(ratio_values, params.min_sample)

    durations = [t.duration_seconds for t in ordered if t.duration_seconds is not None]
    total = sum(nets)
    result.update(
        {
            "win_rate": _r(len(wins) / n),
            "loss_rate": _r(len(losses) / n),
            "breakeven_rate": _r(n_be / n),
            "win_rate_ci95": _interval(wilson_interval(len(wins), n)),
            "gross_profit": _r(gross_profit),
            "gross_loss": _r(gross_loss),
            "profit_factor": _r(profit_factor),
            "profit_factor_note": pf_note,
            "expectancy": _r(mean(nets)),
            "expectancy_ci95": _interval(mean_t_interval(nets)),
            "n_with_risk": len(r_values),
            "expectancy_r": _r(mean(r_values)) if r_values else None,
            "expectancy_r_ci95": _interval(mean_t_interval(r_values)),
            "average_win": _r(mean(wins)) if wins else None,
            "average_loss": _r(mean(losses)) if losses else None,
            "largest_win": _r(max(wins)) if wins else None,
            "largest_loss": _r(min(losses)) if losses else None,
            "average_duration_seconds": _r(mean(durations), 1) if durations else None,
            "max_drawdown": _r(dd_money),
            "max_drawdown_pct": _r(dd_pct),
            "max_drawdown_note": dd_note,
            "max_consecutive_wins": best_w,
            "max_consecutive_losses": best_l,
            "sharpe": _r(sharpe),
            "sortino": _r(sortino),
            "ratio_basis": basis,
            "sharpe_note": sharpe_note,
            "sortino_note": sortino_note,
            "cumulative_return": _r(total),
            "cumulative_return_pct": _r(total / capital * 100) if capital else None,
            "cumulative_r": _r(sum(r_values)) if len(r_values) == n else None,
            "first_close": ordered[0].close_time,
            "last_close": ordered[-1].close_time,
            "histogram_r": histogram(r_values, r_edges(r_values)) if r_values else [],
            "histogram_net": histogram(nets, net_edges(nets)),
        }
    )
    return result
