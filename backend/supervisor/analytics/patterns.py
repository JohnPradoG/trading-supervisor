"""Detección de patrones (fase 9): estadística pura, sin base de datos ni NumPy.

Todo lo que decide si una condición es una TRAMPA (expectativa negativa) o una VENTAJA
(expectativa positiva) está aquí, en funciones puras y deterministas:

- Unidad: R por operación (neto / riesgo inicial). Solo entran operaciones con riesgo
  conocido (abrieron con SL).
- Condición: una o dos (AND) comparaciones sobre variables conocidas AL ENTRAR (Trading DNA,
  hechos previos a la entrada del análisis y la dirección). Categóricas y booleanas: igual a
  un valor visto en ENTRENAMIENTO. Numéricas: un tramo de cuantiles calculados SOLO con
  ENTRENAMIENTO (ver supervisor.analytics.binning); los cortes quedan fijos en la condición
  y se aplican tal cual a validación, fuera de muestra y forward.
- Complemento: operaciones con la variable conocida que NO cumplen la condición (una
  operación sin dato no cuenta en ningún lado).
- Por condición y tramo: n, win rate con intervalo de Wilson, expectancy en R con intervalo
  t de Student al 95 %, profit factor en R, la diferencia frente al complemento y su p-valor
  con el test t de Welch (bilateral; distribución t con los grados de libertad de
  Welch-Satterthwaite, calculada con la beta incompleta regularizada). Welch es determinista
  y barato; con colas gordas es optimista, por eso nada se da por bueno sin validación y
  fuera de muestra.
- Comparaciones múltiples: Benjamini-Hochberg (FDR) sobre TODAS las candidatas probadas en
  la ejecución (las que cumplen la muestra mínima); se guarda cuántas fueron.
- Tipos: TRAMPA si en entrenamiento la expectativa es < 0, peor que el complemento y el
  p-valor ajustado <= q. VENTAJA al revés. En validación y fuera de muestra se exige muestra
  mínima, el mismo sentido de la diferencia y un intervalo de la expectativa que no cruce 0
  (máximo < 0 para una trampa, mínimo > 0 para una ventaja).
"""

import json
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

from supervisor.analytics.binning import bin_bounds, bin_label, quantile_edges
from supervisor.analytics.statistics import mean, mean_t_interval, t_quantile_975, wilson_interval

TRAP, EDGE = "TRAP", "EDGE"
KIND_TEXT = {TRAP: "trampa", EDGE: "ventaja"}


# Distribución t ------------------------------------------------------------------------------


def _betacf(a: float, b: float, x: float) -> float:
    """Fracción continua de la beta incompleta (Numerical Recipes, método de Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-14:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Beta incompleta regularizada I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    ln_front = (
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    front = math.exp(ln_front)
    if x < (a + 1) / (a + b + 2):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_two_sided_p(t: float, df: float) -> float:
    """P(|T| >= |t|) para una t de Student con df grados de libertad (df puede ser real)."""
    if df <= 0:
        raise ValueError("df debe ser > 0")
    if math.isinf(t):
        return 0.0
    return min(1.0, betainc(df / 2.0, 0.5, df / (df + t * t)))


@dataclass(frozen=True)
class WelchResult:
    t: float
    df: float
    p_value: float
    diff: float
    ci_low: float
    ci_high: float


def welch_test(a: Sequence[float], b: Sequence[float]) -> WelchResult | None:
    """Test t de Welch de mean(a) - mean(b). None si algún grupo tiene < 2 valores o ambas
    varianzas son 0."""
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return None
    ma, mb = mean(a), mean(b)
    va = sum((x - ma) ** 2 for x in a) / (na - 1)
    vb = sum((x - mb) ** 2 for x in b) / (nb - 1)
    se2 = va / na + vb / nb
    diff = ma - mb
    if se2 == 0:
        return None
    se = math.sqrt(se2)
    df = se2**2 / ((va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1))
    t = diff / se
    half = t_quantile_975(max(1, math.floor(df))) * se
    return WelchResult(t, df, t_two_sided_p(t, df), diff, diff - half, diff + half)


def benjamini_hochberg(p_values: Sequence[float]) -> list[float]:
    """p-valores ajustados de Benjamini-Hochberg (FDR), en el orden de entrada: p_(i) x m / i
    con el mínimo acumulado desde el final, sin pasar de 1."""
    m = len(p_values)
    order = sorted(range(m), key=lambda i: p_values[i])
    adjusted = [0.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, p_values[i] * m / rank)
        adjusted[i] = min(1.0, running)
    return adjusted


# Observaciones y condiciones -------------------------------------------------------------------


@dataclass(frozen=True)
class Obs:
    """Una operación cerrada vista por la búsqueda: R, si ganó, y sus variables al entrar."""

    trade_id: str
    close_time: Any  # datetime; solo se usa para ordenar y asignar tramos
    r: float
    win: bool
    values: dict[str, Any]
    mae_r: float | None = None
    # Hechos del análisis vigente {código: evidence} (None si no tiene análisis): para
    # contrastar las reglas de la fase 6.
    facts: dict[str, dict[str, Any]] | None = None


@dataclass(frozen=True)
class Variable:
    name: str
    value_type: str  # numeric, boolean, categorical
    label: str
    group: str = ""


@dataclass(frozen=True)
class Clause:
    var: str
    op: str  # "eq" o "range"
    value: Any = None
    low: float | None = None
    high: float | None = None
    bin: str | None = None  # etiqueta del tramo, p. ej. "[30, 50)"

    def known(self, obs: Obs) -> bool:
        return obs.values.get(self.var) is not None

    def matches(self, obs: Obs) -> bool:
        value = obs.values.get(self.var)
        if value is None:
            return False
        if self.op == "eq":
            return value == self.value
        number = float(value)
        return (self.low is None or number >= self.low) and (
            self.high is None or number < self.high
        )

    def spec(self) -> dict[str, Any]:
        out: dict[str, Any] = {"variable": self.var, "op": self.op}
        if self.op == "eq":
            out["valor"] = self.value
        else:
            out.update({"desde": self.low, "hasta": self.high, "tramo": self.bin})
        return out

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> "Clause":
        if spec["op"] == "eq":
            return cls(spec["variable"], "eq", value=spec["valor"])
        return cls(
            spec["variable"], "range", low=spec["desde"], high=spec["hasta"], bin=spec.get("tramo")
        )


@dataclass(frozen=True)
class Condition:
    clauses: tuple[Clause, ...]

    def known(self, obs: Obs) -> bool:
        return all(c.known(obs) for c in self.clauses)

    def matches(self, obs: Obs) -> bool:
        return all(c.matches(obs) for c in self.clauses)

    def spec(self) -> dict[str, Any]:
        return {"tipo": "busqueda", "clausulas": [c.spec() for c in self.clauses]}

    def key(self) -> str:
        return condition_key(self.spec())

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> "Condition":
        return cls(tuple(Clause.from_spec(c) for c in spec["clausulas"]))


def condition_key(spec: dict[str, Any]) -> str:
    return json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def split_groups(
    obs: Sequence[Obs], matches: Callable[[Obs], bool], known: Callable[[Obs], bool]
) -> tuple[list[Obs], list[Obs]]:
    """(cumplen, complemento con dato) de una condición."""
    hit, rest = [], []
    for o in obs:
        if not known(o):
            continue
        (hit if matches(o) else rest).append(o)
    return hit, rest


# Candidatas ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchParams:
    min_n_train: int = 30
    min_n_validation: int = 15
    min_n_oos: int = 15
    min_n_forward: int = 20
    fdr_q: float = 0.05
    quantile_bins: int = 4
    max_pairs: int = 3000

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _base(name: str) -> str:
    """Variables que miden lo mismo (bruta y relativa) no se combinan entre sí."""
    return name.removesuffix("_rel")


def single_clauses(train: Sequence[Obs], variables: Sequence[Variable], bins: int) -> list[Clause]:
    """Condiciones simples generadas SOLO con los valores de entrenamiento."""
    out: list[Clause] = []
    for var in variables:
        values = [o.values.get(var.name) for o in train]
        present = [v for v in values if v is not None]
        if not present:
            continue
        if var.value_type == "numeric":
            numbers = [float(v) for v in present]
            edges = quantile_edges(numbers, bins)
            if not edges:
                continue
            for index in range(len(edges) + 1):
                low, high = bin_bounds(edges, index)
                out.append(
                    Clause(var.name, "range", low=low, high=high, bin=bin_label(edges, index))
                )
        else:
            for value in sorted(set(present), key=lambda v: (str(type(v)), str(v))):
                out.append(Clause(var.name, "eq", value=value))
    return out


@dataclass
class CandidateStats:
    condition: Condition
    n: int
    n_complement: int
    stats: dict[str, Any]
    welch: WelchResult | None
    p_adjusted: float | None = None
    kind: str | None = None


def segment_stats(hit: Sequence[Obs], rest: Sequence[Obs]) -> dict[str, Any]:
    """Métricas de una condición en un tramo de datos frente a su complemento."""
    rs = [o.r for o in hit]
    rest_rs = [o.r for o in rest]
    n = len(rs)
    wins = sum(1 for o in hit if o.win)
    out: dict[str, Any] = {
        "n": n,
        "n_complemento": len(rest_rs),
        "ganadoras": wins,
        "win_rate": round(wins / n, 4) if n else None,
        "win_rate_ic95": None,
        "expectancy_r": round(mean(rs), 4) if n else None,
        "expectancy_r_ic95": None,
        "profit_factor_r": None,
        "expectancy_r_complemento": round(mean(rest_rs), 4) if rest_rs else None,
        "diferencia_r": None,
        "diferencia_r_ic95": None,
        "p_valor": None,
        "mae_r": None,
        "mae_r_complemento": None,
    }
    if n:
        low, high = wilson_interval(wins, n)
        out["win_rate_ic95"] = [round(low, 4), round(high, 4)]
        ci = mean_t_interval(rs)
        if ci is not None:
            out["expectancy_r_ic95"] = [round(ci[0], 4), round(ci[1], 4)]
        gains = sum(r for r in rs if r > 0)
        losses = -sum(r for r in rs if r < 0)
        out["profit_factor_r"] = round(gains / losses, 4) if losses > 0 else None
        maes = [o.mae_r for o in hit if o.mae_r is not None]
        out["mae_r"] = round(mean(maes), 4) if maes else None
    rest_maes = [o.mae_r for o in rest if o.mae_r is not None]
    out["mae_r_complemento"] = round(mean(rest_maes), 4) if rest_maes else None
    if n and rest_rs:
        out["diferencia_r"] = round(mean(rs) - mean(rest_rs), 4)
    welch = welch_test(rs, rest_rs)
    if welch is not None:
        out["diferencia_r_ic95"] = [round(welch.ci_low, 4), round(welch.ci_high, 4)]
        out["p_valor"] = welch.p_value
    return out


def evaluate(condition: Any, obs: Sequence[Obs]) -> tuple[dict[str, Any], WelchResult | None]:
    """Métricas de una condición (cualquier objeto con matches/known) en unas operaciones."""
    hit, rest = split_groups(obs, condition.matches, condition.known)
    welch = welch_test([o.r for o in hit], [o.r for o in rest])
    return segment_stats(hit, rest), welch


@dataclass
class SearchResult:
    generated: int
    tested: int
    low_sample: int
    candidates: list[CandidateStats] = field(default_factory=list)  # las probadas
    pairs_truncated: bool = False


def search(
    train: Sequence[Obs], variables: Sequence[Variable], params: SearchParams
) -> SearchResult:
    """Genera condiciones simples y pares (AND) en entrenamiento, las prueba (muestra mínima en
    la condición y en el complemento), ajusta por FDR y clasifica trampas y ventajas."""
    singles = single_clauses(train, variables, params.quantile_bins)
    index = {o.trade_id: i for i, o in enumerate(train)}
    by_clause: dict[Clause, tuple[frozenset[int], frozenset[int]]] = {}
    for clause in singles:
        hit, rest = split_groups(train, clause.matches, clause.known)
        by_clause[clause] = (
            frozenset(index[o.trade_id] for o in hit),
            frozenset(index[o.trade_id] for o in hit + rest),
        )
    generated = len(singles)
    tested: list[CandidateStats] = []
    low_sample = 0

    def consider(condition: Condition, hit_ids: frozenset[int], known_ids: frozenset[int]) -> None:
        nonlocal low_sample
        n, n_rest = len(hit_ids), len(known_ids) - len(hit_ids)
        if n < params.min_n_train or n_rest < params.min_n_train:
            low_sample += 1
            return
        hit = [train[i] for i in sorted(hit_ids)]
        rest = [train[i] for i in sorted(known_ids - hit_ids)]
        welch = welch_test([o.r for o in hit], [o.r for o in rest])
        if welch is None:
            low_sample += 1
            return
        tested.append(CandidateStats(condition, n, n_rest, segment_stats(hit, rest), welch))

    for clause in singles:
        hit_ids, known_ids = by_clause[clause]
        consider(Condition((clause,)), hit_ids, known_ids)

    eligible = [c for c in singles if len(by_clause[c][0]) >= params.min_n_train]
    pairs = 0
    truncated = False
    for a, b in combinations(eligible, 2):
        if _base(a.var) == _base(b.var):
            continue
        if pairs >= params.max_pairs:
            truncated = True
            break
        hit_ids = by_clause[a][0] & by_clause[b][0]
        if len(hit_ids) < params.min_n_train:
            continue  # no se genera: el par no tiene muestra
        if hit_ids in (by_clause[a][0], by_clause[b][0]):
            continue  # no se genera: el par selecciona lo mismo que una de sus partes
        pairs += 1
        generated += 1
        consider(Condition((a, b)), hit_ids, by_clause[a][1] & by_clause[b][1])

    adjusted = benjamini_hochberg([c.welch.p_value for c in tested]) if tested else []
    for cand, p_adj in zip(tested, adjusted, strict=True):
        cand.p_adjusted = p_adj
        cand.kind = train_kind(cand.stats, p_adj, params.fdr_q)
    return SearchResult(generated, len(tested), low_sample, tested, truncated)


def train_kind(stats: dict[str, Any], p_adjusted: float, q: float) -> str | None:
    exp, diff = stats["expectancy_r"], stats["diferencia_r"]
    if exp is None or diff is None or p_adjusted > q:
        return None
    if exp < 0 and diff < 0:
        return TRAP
    if exp > 0 and diff > 0:
        return EDGE
    return None


def segment_check(kind: str, stats: dict[str, Any], min_n: int) -> tuple[bool, str]:
    """¿Se sostiene la trampa (o ventaja) en un tramo nuevo (validación, fuera de muestra)?"""
    n = stats["n"]
    if n < min_n:
        return False, f"muestra insuficiente: n={n} < {min_n}"
    if stats["n_complemento"] < 2 or stats["diferencia_r"] is None:
        return False, "sin complemento con el que comparar"
    ci = stats["expectancy_r_ic95"]
    if ci is None:
        return False, "sin intervalo de la expectativa"
    diff = stats["diferencia_r"]
    if kind == TRAP:
        if diff >= 0:
            return False, f"el efecto cambia de sentido (diferencia {diff:+.2f} R)"
        if ci[1] >= 0:
            return False, f"el intervalo de la expectativa cruza 0 ({ci[0]:+.2f}, {ci[1]:+.2f} R)"
        return True, f"expectativa {stats['expectancy_r']:+.2f} R, IC95 máximo {ci[1]:+.2f} R < 0"
    if diff <= 0:
        return False, f"el efecto cambia de sentido (diferencia {diff:+.2f} R)"
    if ci[0] <= 0:
        return False, f"el intervalo de la expectativa cruza 0 ({ci[0]:+.2f}, {ci[1]:+.2f} R)"
    return True, f"expectativa {stats['expectancy_r']:+.2f} R, IC95 mínimo {ci[0]:+.2f} R > 0"


def forward_decayed(kind: str, stats: dict[str, Any], min_n: int) -> tuple[bool, str]:
    """Forward: la trampa caduca si su expectativa pasa a ser significativamente positiva
    (IC95 mínimo > 0); la ventaja, si pasa a ser significativamente negativa."""
    n = stats["n"]
    ci = stats["expectancy_r_ic95"]
    if n < min_n or ci is None:
        return False, f"forward en curso: n={n} (se evalúa desde {min_n})"
    if kind == TRAP and ci[0] > 0:
        return True, f"forward con expectativa {stats['expectancy_r']:+.2f} R, IC95 mínimo > 0"
    if kind == EDGE and ci[1] < 0:
        return True, f"forward con expectativa {stats['expectancy_r']:+.2f} R, IC95 máximo < 0"
    exp = stats["expectancy_r"]
    opposite = (kind == TRAP and exp > 0) or (kind == EDGE and exp < 0)
    note = " (en sentido contrario, aún no significativo)" if opposite else ""
    return False, f"forward n={n}, expectativa {exp:+.2f} R{note}"


# Descripción en castellano llano -------------------------------------------------------------

_SESSIONS = {
    "ASIA": "durante la sesión de Asia",
    "LONDRES": "durante la sesión de Londres",
    "LONDRES_NY": "durante el solape Londres-Nueva York",
    "NUEVA_YORK": "durante la sesión de Nueva York",
    "FUERA_SESION": "fuera de sesión",
}


def _value_text(value: Any) -> str:
    if isinstance(value, bool):
        return "sí" if value else "no"
    return str(value).replace("_", " ").lower()


def _lower(label: str) -> str:
    """Minúscula inicial salvo en siglas ("BOS", "FVG", "RSI(14)", "ADX")."""
    if len(label) > 1 and label[1].isupper():
        return label
    return label[:1].lower() + label[1:]


def clause_text(clause: Clause, labels: dict[str, str]) -> str:
    var, value = clause.var, clause.value
    label = _lower(labels.get(var, var))
    if clause.op == "range":
        return f"con {label} {clause.bin}"
    if var.startswith("tendencia_") and var.endswith("_rel"):
        tf = var.split("_")[1].upper()
        return {
            "A_FAVOR": f"a favor de la tendencia {tf}",
            "EN_CONTRA": f"contra la tendencia {tf}",
            "LATERAL": f"con {tf} sin tendencia",
        }.get(value, f"{label} {_value_text(value)}")
    if var.startswith("tendencia_"):
        return f"con tendencia {var.split('_')[1].upper()} {_value_text(value)}"
    if var == "sesion":
        return _SESSIONS.get(value, f"en la sesión {value}")
    if var == "dia_semana":
        return f"los {value}"
    if var == "direccion":
        return "en compra" if value == "BUY" else "en venta"
    if isinstance(value, bool):
        return f"{'con' if value else 'sin'} {label}"
    return f"con {label} {_value_text(value)}"


def describe(spec: dict[str, Any], labels: dict[str, str]) -> str:
    """Frase de la condición: "Entrar contra la tendencia H1 durante la sesión de Asia"."""
    if spec.get("tipo") == "regla":
        return spec.get("texto") or spec["regla"]
    condition = Condition.from_spec(spec)
    return "Entrar " + " y ".join(clause_text(c, labels) for c in condition.clauses)


def top(items: Iterable[CandidateStats], kind: str, count: int) -> list[CandidateStats]:
    """Las mejores candidatas de un tipo por diferencia frente al complemento (para el
    resumen; no implica que sean significativas)."""
    sign = -1 if kind == TRAP else 1
    pool = [c for c in items if c.stats["diferencia_r"] is not None]
    pool.sort(key=lambda c: (-sign * c.stats["diferencia_r"], c.welch.p_value, c.condition.key()))
    return pool[:count]
