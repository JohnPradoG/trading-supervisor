"""Laboratorio (fase 10): filtro contrafactual sobre operaciones reales (funciones puras).

Pregunta: "¿qué habría pasado si la versión base no hubiera entrado cuando se cumple esta
condición?". Se responde con las operaciones REALES cerradas de esa versión, sin inventar
ninguna:

- **Población:** las mismas que la búsqueda de patrones (fase 9): cerradas y con riesgo
  conocido (R = neto / riesgo), con las variables conocidas AL ENTRAR (Trading DNA vigente,
  dirección, reentrada y hechos previos del análisis).
- **Filtro:** una condición de la fase 9 (cláusulas `eq`/`range` o una regla de la fase 6) y
  un modo: `excluir` (saltarse las entradas que la cumplen; lo natural para una trampa) o
  `solo` (operar solo las que la cumplen). Una operación sin dato para alguna variable del
  filtro no se puede evaluar: se mantiene y se cuenta aparte (`sin_dato`).
- **Tramos:** con el split congelado de la fase 9 (el de la hipótesis si el experimento sale
  de una, si no el último del alcance): DENTRO DE MUESTRA = cerradas hasta validation_end
  (entrenamiento + validación: con ellas se eligió la condición) y FUERA DE MUESTRA = cerradas
  después (fuera de muestra + forward). El titular es fuera de muestra. Sin split no hay fuera
  de muestra y el resultado es solo orientativo.
- **Métricas** (supervisor.analytics.statistics.compute_metrics) del original, del filtrado y
  de las evitadas: n, win rate (IC de Wilson), profit factor, expectativa en dinero y en R
  (IC t), neto y drawdown máximo. Profit factor y drawdown máximo llevan un intervalo
  bootstrap al 95 % (remuestreo con reemplazo, semilla fija derivada de las operaciones:
  determinista). Las evitadas frente a las mantenidas se comparan con el test de Welch en R.

Avisos fijos: dentro de muestra es optimista (la condición se eligió mirando esos datos); es
un contrafactual: el bot filtrado nunca operó y su comportamiento en vivo es desconocido
(otras entradas, gestión, lotes, ejecución).
"""

import hashlib
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from supervisor.analytics import patterns as pt
from supervisor.analytics.outcome import classify_outcome
from supervisor.analytics.statistics import StatsParams, TradeResult, compute_metrics, max_drawdown
from supervisor.models.enums import Outcome

EXCLUDE, ONLY = "excluir", "solo"
MODES = (EXCLUDE, ONLY)
IN_SAMPLE, OUT_OF_SAMPLE, ALL = "dentro_de_muestra", "fuera_de_muestra", "todo"
MAX_CLAUSES = 3

WARNING_IN_SAMPLE = (
    "Dentro de muestra es optimista: la condición se eligió mirando esas mismas operaciones. "
    "Juzga el filtro por el tramo fuera de muestra."
)
WARNING_COUNTERFACTUAL = (
    "Es un contrafactual sobre operaciones reales: el bot filtrado nunca operó. Su "
    "comportamiento en vivo es desconocido (podría tomar otras entradas, con otra gestión, "
    "lotes o ejecución). Confírmalo con una versión nueva en demo o forward antes de usarlo."
)
WARNING_NO_SPLIT = (
    "Todavía no hay split congelado para esta versión (se crea con la búsqueda de patrones a "
    "partir de {min_trades} operaciones): no existe tramo fuera de muestra y el resultado es "
    "solo orientativo."
)
WARNING_MANUAL = (
    "El filtro no viene de una hipótesis validada: si se ideó mirando todas las operaciones, "
    "también el tramo fuera de muestra está contaminado."
)


class FilterError(ValueError):
    """Filtro mal formado (mensaje apto para el usuario)."""


# Filtro --------------------------------------------------------------------------------------


def _clause(raw: Any, variables: dict[str, pt.Variable]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise FilterError("cada cláusula debe ser un objeto con variable y op")
    name = raw.get("variable")
    var = variables.get(name) if isinstance(name, str) else None
    if var is None:
        raise FilterError(f"variable desconocida en el filtro: {name!r}")
    op = raw.get("op")
    if var.value_type == "numeric":
        if op != "range":
            raise FilterError(f"{name} es numérica: usa op 'range' con desde y/o hasta")
        low, high = raw.get("desde"), raw.get("hasta")
        for value in (low, high):
            numeric = isinstance(value, int | float) and not isinstance(value, bool)
            if value is not None and not numeric:
                raise FilterError(f"{name}: desde y hasta deben ser números")
        if low is None and high is None:
            raise FilterError(f"{name}: indica desde, hasta o ambos")
        if low is not None and high is not None and not low < high:
            raise FilterError(f"{name}: desde debe ser menor que hasta")
        label = raw.get("tramo") or (
            f"[{'-∞' if low is None else f'{low:g}'}, {'∞' if high is None else f'{high:g}'})"
        )
        return pt.Clause(
            name,
            "range",
            low=None if low is None else float(low),
            high=None if high is None else float(high),
            bin=str(label)[:40],
        ).spec()
    if op != "eq":
        raise FilterError(f"{name} no es numérica: usa op 'eq' con valor")
    value = raw.get("valor")
    if var.value_type == "boolean":
        if not isinstance(value, bool):
            raise FilterError(f"{name}: el valor debe ser true o false")
    elif not isinstance(value, str) or not 0 < len(value) <= 64:
        raise FilterError(f"{name}: el valor debe ser un texto (p. ej. EN_CONTRA)")
    return pt.Clause(name, "eq", value=value).spec()


def normalize_filter(
    spec: Any,
    variables: Sequence[pt.Variable],
    rule_spec: Callable[[str], dict[str, Any] | None],
) -> dict[str, Any]:
    """Valida y normaliza {"modo", "condicion"}. `condicion` es una condition_spec de la fase
    9: {"tipo": "busqueda", "clausulas": [...]} (1 a 3 cláusulas) o {"tipo": "regla",
    "regla": código}. `rule_spec(código)` devuelve la spec completa de una regla contrastable."""
    if not isinstance(spec, dict):
        raise FilterError("el filtro debe ser un objeto {modo, condicion}")
    mode = spec.get("modo", EXCLUDE)
    if mode not in MODES:
        raise FilterError("modo debe ser 'excluir' o 'solo'")
    cond = spec.get("condicion")
    if not isinstance(cond, dict):
        raise FilterError("falta la condicion del filtro")
    kind = cond.get("tipo", "busqueda")
    if kind == "regla":
        full = rule_spec(str(cond.get("regla")))
        if full is None:
            raise FilterError(f"regla desconocida o no contrastable: {cond.get('regla')!r}")
        return {"modo": mode, "condicion": full}
    if kind != "busqueda":
        raise FilterError("condicion.tipo debe ser 'busqueda' o 'regla'")
    clauses = cond.get("clausulas")
    if not isinstance(clauses, list) or not 1 <= len(clauses) <= MAX_CLAUSES:
        raise FilterError(f"la condición necesita entre 1 y {MAX_CLAUSES} cláusulas")
    by_name = {v.name: v for v in variables}
    normalized = [_clause(c, by_name) for c in clauses]
    return {"modo": mode, "condicion": {"tipo": "busqueda", "clausulas": normalized}}


def filter_text(spec: dict[str, Any], labels: dict[str, str]) -> str:
    condition = pt.describe(spec["condicion"], labels)
    if spec["modo"] == ONLY:
        return f"Operar solo las entradas que cumplan: «{condition}»"
    return f"Saltarse las entradas que cumplan: «{condition}»"


# Contrafactual -------------------------------------------------------------------------------


@dataclass(frozen=True)
class LabTrade:
    """Una operación real cerrada: sus variables al entrar (obs) y su resultado (dinero)."""

    obs: pt.Obs
    result: TradeResult


@dataclass(frozen=True)
class Split:
    """Corte del split congelado de la fase 9 que separa dentro y fuera de muestra."""

    id: str
    validation_end: datetime
    oos_end: datetime
    train_end: datetime
    origin: str  # "hipotesis" o "ultimo_split"


def apply_filter(
    trades: Sequence[LabTrade], condition: Any, mode: str
) -> tuple[list[LabTrade], list[LabTrade], int]:
    """(mantenidas, evitadas, sin dato). Las que no se pueden evaluar se mantienen."""
    kept: list[LabTrade] = []
    removed: list[LabTrade] = []
    unknown = 0
    for t in trades:
        if not condition.known(t.obs):
            unknown += 1
            kept.append(t)
            continue
        hit = condition.matches(t.obs)
        skip = hit if mode == EXCLUDE else not hit
        (removed if skip else kept).append(t)
    return kept, removed, unknown


def _seed(trades: Sequence[LabTrade | TradeResult], salt: str) -> int:
    digest = hashlib.sha256(salt.encode())
    for t in trades:
        result = t.result if isinstance(t, LabTrade) else t
        digest.update(result.trade_id.encode())
    return int.from_bytes(digest.digest()[:8], "big")


def _percentile(sorted_values: list[float], q: float) -> float:
    index = q * (len(sorted_values) - 1)
    low = int(index)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (index - low)


# Trabajo máximo del bootstrap por serie (remuestreos x operaciones): con muchas operaciones
# se usan menos remuestreos (nunca menos de 100) para que una página no tarde segundos en el
# VPS de 1 vCPU. El número usado se devuelve en "remuestreos".
BOOTSTRAP_BUDGET = 1_500_000
MIN_BOOTSTRAP = 100


def bootstrap_intervals(
    results: Sequence[TradeResult], params: StatsParams, samples: int, seed: int
) -> dict[str, Any]:
    """IC95 percentil del profit factor y del drawdown máximo (dinero) por remuestreo con
    reemplazo. Con < 10 operaciones o samples = 0 no se calculan. El profit factor solo si es
    finito en al menos el 90 % de los remuestreos."""
    out: dict[str, Any] = {"profit_factor_ic95": None, "max_drawdown_ic95": None, "remuestreos": 0}
    n = len(results)
    if samples <= 0 or n < 10:
        return out
    samples = min(samples, max(MIN_BOOTSTRAP, BOOTSTRAP_BUDGET // n))
    out["remuestreos"] = samples
    outcomes = [
        classify_outcome(r.net, r.risk, r.commission, params.breakeven_r_fraction).outcome
        for r in results
    ]
    nets = [r.net for r in results]
    gains_of = [net if o == Outcome.WIN else 0.0 for net, o in zip(nets, outcomes, strict=True)]
    losses_of = [-net if o == Outcome.LOSS else 0.0 for net, o in zip(nets, outcomes, strict=True)]
    rng = random.Random(seed)
    population = range(n)
    pfs: list[float] = []
    dds: list[float] = []
    for _ in range(samples):
        idx = rng.choices(population, k=n)
        losses = sum(losses_of[i] for i in idx)
        if losses > 0:
            pfs.append(sum(gains_of[i] for i in idx) / losses)
        dds.append(max_drawdown([nets[i] for i in idx])[0])
    dds.sort()
    out["max_drawdown_ic95"] = [
        round(_percentile(dds, 0.025), 2),
        round(_percentile(dds, 0.975), 2),
    ]
    if len(pfs) >= 0.9 * samples:
        pfs.sort()
        out["profit_factor_ic95"] = [
            round(_percentile(pfs, 0.025), 4),
            round(_percentile(pfs, 0.975), 4),
        ]
    return out


METRIC_KEYS = (
    "n_trades",
    "wins",
    "losses",
    "breakevens",
    "sample_warning",
    "win_rate",
    "win_rate_ci95",
    "profit_factor",
    "profit_factor_note",
    "expectancy",
    "expectancy_ci95",
    "n_with_risk",
    "expectancy_r",
    "expectancy_r_ci95",
    "cumulative_return",
    "cumulative_r",
    "max_drawdown",
    "max_drawdown_pct",
    "max_drawdown_note",
    "first_close",
    "last_close",
)


def summary_metrics(
    results: Sequence[TradeResult], params: StatsParams, samples: int, salt: str = ""
) -> dict[str, Any]:
    """Las métricas que compara el laboratorio, con los intervalos bootstrap."""
    full = compute_metrics(results, params)
    out = {k: full.get(k) for k in METRIC_KEYS}
    ordered = sorted(results, key=lambda r: (r.close_time, r.trade_id))
    out.update(bootstrap_intervals(ordered, params, samples, _seed(ordered, salt)))
    return out


def _welch_view(kept: Sequence[LabTrade], removed: Sequence[LabTrade]) -> dict[str, Any] | None:
    welch = pt.welch_test([t.obs.r for t in removed], [t.obs.r for t in kept])
    if welch is None:
        return None
    return {
        "diferencia_r": round(welch.diff, 4),
        "ic95": [round(welch.ci_low, 4), round(welch.ci_high, 4)],
        "p_valor": round(welch.p_value, 6),
        "metodo": "Welch (evitadas - mantenidas, en R)",
    }


def compare_segment(
    trades: Sequence[LabTrade], condition: Any, mode: str, params: StatsParams, samples: int
) -> dict[str, Any]:
    kept, removed, unknown = apply_filter(trades, condition, mode)
    return {
        "original": summary_metrics([t.result for t in trades], params, samples, "original"),
        "filtrado": summary_metrics([t.result for t in kept], params, samples, "filtrado"),
        "evitadas": summary_metrics([t.result for t in removed], params, samples, "evitadas"),
        "sin_dato": unknown,
        "evitadas_vs_mantenidas": _welch_view(kept, removed),
    }


def segment_trades(trades: Sequence[LabTrade], split: Split | None) -> dict[str, list[LabTrade]]:
    out: dict[str, list[LabTrade]] = {ALL: list(trades)}
    if split is not None:
        out[IN_SAMPLE] = [t for t in trades if t.result.close_time <= split.validation_end]
        out[OUT_OF_SAMPLE] = [t for t in trades if t.result.close_time > split.validation_end]
    return out


def counterfactual(
    trades: Sequence[LabTrade],
    filter_spec: dict[str, Any],
    condition: Any,
    split: Split | None,
    params: StatsParams,
    samples: int,
    *,
    from_hypothesis: bool,
    min_trades_for_split: int,
) -> dict[str, Any]:
    """Resultado completo del filtro contrafactual (ver cabecera del módulo)."""
    mode = filter_spec["modo"]
    parts = segment_trades(trades, split)
    tramos = {
        name: compare_segment(items, condition, mode, params, samples)
        for name, items in parts.items()
    }
    warnings = [WARNING_COUNTERFACTUAL]
    if split is None:
        warnings.insert(0, WARNING_NO_SPLIT.format(min_trades=min_trades_for_split))
        headline = ALL
    else:
        warnings.insert(0, WARNING_IN_SAMPLE)
        headline = OUT_OF_SAMPLE
        if not parts[OUT_OF_SAMPLE]:
            warnings.insert(
                0,
                "No hay operaciones cerradas después del split (fuera de muestra vacío): espera "
                "a que cierren más operaciones antes de sacar conclusiones.",
            )
    if not from_hypothesis:
        warnings.append(WARNING_MANUAL)
    head = tramos[headline]
    for arm in ("original", "filtrado"):
        if head[arm]["sample_warning"]:
            warnings.append(f"{arm} ({headline.replace('_', ' ')}): {head[arm]['sample_warning']}")
    return {
        "tipo": "FILTRO",
        "titular": headline,
        "split": None
        if split is None
        else {
            "id": split.id,
            "origen": split.origin,
            "train_end": split.train_end,
            "validation_end": split.validation_end,
            "oos_end": split.oos_end,
            "dentro_de_muestra": "cerradas hasta validation_end (entrenamiento + validación)",
            "fuera_de_muestra": "cerradas después de validation_end (fuera de muestra + forward)",
        },
        "tramos": tramos,
        "avisos": warnings,
    }


# Curvas de equity ------------------------------------------------------------------------------


def equity_curve(results: Sequence[TradeResult], max_points: int) -> list[list[float]]:
    """[[hora en ms, neto acumulado], ...] por hora de cierre, reducida a max_points como mucho
    (se conserva siempre el último punto)."""
    ordered = sorted(results, key=lambda r: (r.close_time, r.trade_id))
    points: list[list[float]] = []
    total = 0.0
    for r in ordered:
        total += r.net
        points.append([int(r.close_time.timestamp() * 1000), round(total, 2)])
    if len(points) <= max_points:
        return points
    step = len(points) / (max_points - 1)
    reduced = [points[int(i * step)] for i in range(max_points - 1)]
    reduced.append(points[-1])
    return reduced
