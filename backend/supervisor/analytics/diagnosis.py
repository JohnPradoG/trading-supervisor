"""Diagnóstico de un bot (funciones puras, sin base de datos): qué está fallando, qué cambiar y
qué funciona mejor, sin vender como resultado lo que solo mejora dentro de muestra.

Entradas: las operaciones REALES cerradas con riesgo conocido de una versión (R = neto /
riesgo inicial), cada una con su tramo del split congelado de la fase 9 (ENTRENAMIENTO,
VALIDACIÓN, FUERA DE MUESTRA, FORWARD) y, cuando hay velas M1 suficientes, la réplica de sus
salidas (supervisor.analytics.exit_replay).

Rigor (sección 11), igual para cambios de salida y para filtros:

1. **Rejilla fija y pequeña.** Las variantes de salida (exit_replay.exit_grid, 20) y los
   filtros (filter_grid, ~26) son fijos; el único valor que sale de los datos es el umbral de
   "spread alto" (percentil 80 del ENTRENAMIENTO). Se registra cuántas se probaron.
2. **Elección solo con entrenamiento.** En cada familia se elige la variante con la mayor
   mejora de expectativa (R por operación) en entrenamiento; la función que elige solo recibe
   las operaciones de entrenamiento: validación y fuera de muestra no pueden influir.
3. **Una sola evaluación en validación y fuera de muestra.** "validada" solo si en los dos
   tramos la mejora de la expectativa en R por operación tiene un IC95 que no cruza 0 (por
   encima) con la muestra mínima (la de las trampas: 15 por defecto). Si falla: "candidata, no
   validada"; si falta muestra o split: "sin datos suficientes". El tramo fuera de muestra es
   el de la fase 9 (fijo para un split): repetir el diagnóstico no cambia el veredicto; lo que
   cierra después es FORWARD y solo se muestra.
4. **Mejora de una salida:** diferencias emparejadas por operación (variante - réplica de las
   reglas actuales) con intervalo t. **Mejora de un filtro:** media de las mantenidas menos
   media de todas = fracción evitada x (mantenidas - evitadas); su IC sale del test de Welch
   (mantenidas - evitadas), y además las evitadas deben tener expectativa negativa. Un filtro
   es un contrafactual: lleva siempre el aviso de la fase 10.

Aciertos (win rate, con IC de Wilson) junto a la expectativa y el profit factor en todo. Se
prefieren los cambios que suben el % de aciertos sin empeorar el resultado; un cambio que gana
más operaciones pero pierde dinero se marca "no recomendado" y nunca se recomienda.

Los modos de fallo ("qué está fallando") son DESCRIPTIVOS (todas las operaciones), ordenados
por R perdido (= -suma de R del grupo si es negativa). Una operación puede estar en varios.
"""

import bisect
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from supervisor.analytics import exit_replay as er
from supervisor.analytics.lab import WARNING_COUNTERFACTUAL
from supervisor.analytics.patterns import welch_test
from supervisor.analytics.sessions import SESSION_BLOCKS, WEEKDAYS, session_label
from supervisor.analytics.statistics import mean, mean_t_interval, wilson_interval

TRAIN, VALIDATION, OOS, FORWARD = "TRAIN", "VALIDATION", "OOS", "FORWARD"
SEGMENT_NAMES = {
    TRAIN: "entrenamiento",
    VALIDATION: "validacion",
    OOS: "fuera_de_muestra",
    FORWARD: "forward",
}
VALIDATED, CANDIDATE, NO_DATA = "validada", "candidata, no validada", "sin datos suficientes"
STATUS_RANK = {VALIDATED: 0, CANDIDATE: 1, NO_DATA: 2}
WIN_RATE_TRAP = "Gana más operaciones pero pierde dinero: no recomendado"
PREFERRED = "Sube el % de aciertos sin empeorar el resultado"
DESCRIPTIVE = "descriptivo, no validado"
WARNING_REPLAY = (
    "Réplica con velas M1 sobre las entradas reales: si en una vela se tocan el stop y el "
    "objetivo se supone primero el stop; sin deslizamiento; ventas con el spread al entrar. "
    "Confírmalo en demo o forward con una versión nueva del EA."
)
SESSION_TEXT = {
    "ASIA": "la sesión de Asia (00:00-07:00 UTC)",
    "LONDRES": "la sesión de Londres (07:00-12:00 UTC)",
    "LONDRES_NY": "el solape Londres-Nueva York (12:00-16:00 UTC)",
    "NUEVA_YORK": "la sesión de Nueva York (16:00-21:00 UTC)",
    "FUERA_SESION": "fuera de sesión (21:00-24:00 UTC)",
}
FILTER_FAMILY_TEXT = {
    "sesion": "Sesión",
    "dia": "Día de la semana",
    "franja": "Franja horaria",
    "tras_perdida": "Después de una pérdida",
    "racha": "Rachas de pérdidas",
    "spread": "Spread al entrar",
    "reentrada": "Reentradas tras SL",
}


@dataclass(frozen=True)
class Params:
    min_n_train: int = 30
    min_n_validation: int = 15
    min_n_oos: int = 15
    breakeven_r: float = 0.05
    tp_reach_fraction: float = 0.7
    after_loss_minutes: int = 60
    loss_streak: int = 2
    min_group: int = 10
    min_sample: int = 30
    win_rate_trap_points: float = 0.05
    curve_points: int = 400

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class DiagTrade:
    """Una operación real cerrada con riesgo conocido, vista por el diagnóstico."""

    trade_id: str
    entry_time: datetime
    close_time: datetime
    r: float
    exit_reason: str | None = None
    mfe_r: float | None = None
    tp_r: float | None = None  # distancia al TP inicial en R (None sin TP)
    spread_points: float | None = None  # spread al entrar (DNA)
    reentry: bool = False
    segment: str | None = None  # TRAIN, VALIDATION, OOS, FORWARD o None sin split
    minutes_since_loss: float | None = None
    loss_streak: int = 0
    sl_then_tp: bool | None = None  # cerró por SL y después tocó el TP (None: no evaluable)


# Métricas en R ---------------------------------------------------------------------------------


def _round(value: float | None, places: int = 4) -> float | None:
    return None if value is None else round(value, places)


def is_win(r: float, params: Params) -> bool:
    return r > params.breakeven_r


def r_stats(rs: Sequence[float], params: Params) -> dict[str, Any]:
    """n, aciertos (IC Wilson), expectativa en R (IC t), profit factor en R, ganancia y
    pérdida media en R y R neto."""
    n = len(rs)
    wins = [r for r in rs if r > params.breakeven_r]
    losses = [r for r in rs if r < -params.breakeven_r]
    out: dict[str, Any] = {
        "n": n,
        "ganadoras": len(wins),
        "perdedoras": len(losses),
        "win_rate": _round(len(wins) / n) if n else None,
        "win_rate_ic95": None,
        "expectancy_r": _round(mean(rs)) if n else None,
        "expectancy_r_ic95": None,
        "profit_factor_r": None,
        "ganancia_media_r": _round(mean(wins)) if wins else None,
        "perdida_media_r": _round(mean(losses)) if losses else None,
        "r_neto": _round(sum(rs)),
        "muestra_pequena": n < params.min_sample,
    }
    if n:
        lo, hi = wilson_interval(len(wins), n)
        out["win_rate_ic95"] = [_round(lo), _round(hi)]
        ci = mean_t_interval(rs)
        if ci is not None:
            out["expectancy_r_ic95"] = [_round(ci[0]), _round(ci[1])]
        gains, loss = sum(r for r in rs if r > 0), -sum(r for r in rs if r < 0)
        out["profit_factor_r"] = _round(gains / loss) if loss > 0 else None
    return out


def breakeven_win_rate(avg_win: float | None, avg_loss: float | None) -> float | None:
    """% de aciertos necesario para no perder con esa ganancia y pérdida medias."""
    if not avg_win or avg_loss is None or avg_loss >= 0:
        return None
    return abs(avg_loss) / (avg_win + abs(avg_loss))


def win_rate_note(stats: dict[str, Any]) -> str | None:
    """Explicación llana con los números del propio bot: acertar mucho no basta."""
    wr, aw, al = stats.get("win_rate"), stats.get("ganancia_media_r"), stats.get("perdida_media_r")
    need = breakeven_win_rate(aw, al)
    if wr is None or need is None:
        return None
    state = (
        "ahora está por encima, por eso gana dinero"
        if wr > need
        else "ahora está por debajo, por eso pierde dinero aunque acierte a menudo"
        if wr >= 0.5
        else "ahora está por debajo"
    )
    return (
        f"El bot acierta el {_pct(wr)} de sus operaciones. Un % de aciertos alto por sí solo no "
        f"garantiza ganar dinero: su ganancia media es {aw:+.2f}R y su pérdida media {al:+.2f}R, "
        f"así que necesita acertar al menos el {_pct(need)} para no perder ({state})."
    )


# Contexto secuencial (después de pérdidas) ------------------------------------------------------


def with_sequence(trades: Sequence[DiagTrade], params: Params) -> list[DiagTrade]:
    """Añade a cada operación los minutos desde la última pérdida CERRADA antes de su entrada
    y cuántas pérdidas seguidas llevaba el bot en ese momento (solo información pasada)."""
    closed = sorted(trades, key=lambda t: (t.close_time, t.trade_id))
    times = [t.close_time for t in closed]
    streaks: list[int] = []
    last_loss: list[datetime | None] = []
    run, current = 0, None
    for t in closed:
        if t.r < -params.breakeven_r:
            run, current = run + 1, t.close_time
        else:
            run = 0
        streaks.append(run)
        last_loss.append(current)
    out = []
    for t in trades:
        i = bisect.bisect_left(times, t.entry_time) - 1
        minutes = None
        streak = 0
        if i >= 0:
            streak = streaks[i]
            if last_loss[i] is not None:
                minutes = (t.entry_time - last_loss[i]).total_seconds() / 60
        out.append(replace(t, minutes_since_loss=minutes, loss_streak=streak))
    return out


# Evaluación de cambios ----------------------------------------------------------------------------


def _verdict(
    validation: dict[str, Any] | None, oos: dict[str, Any] | None, params: Params
) -> tuple[str, str]:
    if validation is None or validation["n"] < params.min_n_validation:
        n = validation["n"] if validation else 0
        return NO_DATA, f"validación: n={n} (mínimo {params.min_n_validation})"
    if not validation["paso"]:
        return CANDIDATE, f"no se confirma en validación: {validation['motivo']}"
    if oos is None or oos["n"] < params.min_n_oos:
        n = oos["n"] if oos else 0
        return NO_DATA, (
            f"pasa validación; falta muestra fuera de muestra: n={n} (mínimo {params.min_n_oos})"
        )
    if not oos["paso"]:
        return CANDIDATE, f"no se confirma fuera de muestra: {oos['motivo']}"
    return VALIDATED, f"validada fuera de muestra: {oos['motivo']}"


def _improvement_check(
    n: int, low: float | None, high: float | None, min_n: int, extra: str | None = None
) -> tuple[bool, str]:
    if n < min_n:
        return False, f"muestra insuficiente: n={n} < {min_n}"
    if low is None:
        return False, "sin intervalo de la mejora"
    if extra:
        return False, extra
    if low <= 0:
        return False, f"el IC95 de la mejora cruza 0 ({low:+.2f}, {high:+.2f} R)"
    return True, f"mejora con IC95 mínimo {low:+.2f} R > 0"


@dataclass(frozen=True)
class ExitRow:
    """Resultados de la réplica de una operación: R por clave de regla (incluida "actual")."""

    trade_id: str
    close_time: datetime
    segment: str | None
    real_r: float
    results: dict[str, float]


def exit_eval(rows: Sequence[ExitRow], key: str, params: Params, min_n: int) -> dict[str, Any]:
    """Réplica de las reglas actuales frente a la variante, con la mejora emparejada."""
    pairs = [(row.results[er.BASELINE.key], row.results[key]) for row in rows if key in row.results]
    base = [b for b, _ in pairs]
    variant = [v for _, v in pairs]
    diffs = [v - b for b, v in pairs]
    ci = mean_t_interval(diffs)
    out = {
        "n": len(pairs),
        "original": r_stats(base, params),
        "sugerida": r_stats(variant, params),
        "mejora_r": _round(mean(diffs)) if diffs else None,
        "mejora_r_ic95": [_round(ci[0]), _round(ci[1])] if ci else None,
        "real": r_stats([row.real_r for row in rows if key in row.results], params),
    }
    ok, reason = _improvement_check(len(pairs), ci[0] if ci else None, ci[1] if ci else None, min_n)
    out["paso"], out["motivo"] = ok, reason
    return out


@dataclass(frozen=True)
class FilterRule:
    key: str
    family: str
    action: str
    test: Callable[[DiagTrade], bool | None]  # True = se evita; None = sin dato (se mantiene)
    spec: dict[str, Any] | None = None  # filtro de la fase 10 (None si no se puede expresar)

    def describe(self) -> dict[str, Any]:
        return {"clave": self.key, "familia": self.family, "filtro_fase10": self.spec}


def _eq(variable: str, value: Any) -> dict[str, Any]:
    return {
        "modo": "excluir",
        "condicion": {
            "tipo": "busqueda",
            "clausulas": [{"variable": variable, "op": "eq", "valor": value}],
        },
    }


def _range(variable: str, low: float | None, high: float | None) -> dict[str, Any]:
    return {
        "modo": "excluir",
        "condicion": {
            "tipo": "busqueda",
            "clausulas": [{"variable": variable, "op": "range", "desde": low, "hasta": high}],
        },
    }


def spread_threshold(train: Sequence[DiagTrade], params: Params) -> float | None:
    """Percentil 80 del spread al entrar, SOLO con entrenamiento."""
    values = sorted(t.spread_points for t in train if t.spread_points is not None)
    if len(values) < params.min_n_train:
        return None
    return float(values[min(len(values) - 1, math.floor(0.8 * len(values)))])


def filter_grid(train: Sequence[DiagTrade], params: Params) -> list[FilterRule]:
    """Rejilla fija de filtros (lo único que se calcula, con entrenamiento, es el umbral del
    spread)."""
    rules: list[FilterRule] = []
    for _, _, label in SESSION_BLOCKS:
        rules.append(
            FilterRule(
                f"sesion_{label.lower()}",
                "sesion",
                f"No operar durante {SESSION_TEXT[label]}",
                lambda t, label=label: session_label(t.entry_time) == label,
                _eq("sesion", label),
            )
        )
    for index, name in enumerate(WEEKDAYS):
        rules.append(
            FilterRule(
                f"dia_{index}",
                "dia",
                f"No operar los {name}",
                lambda t, index=index: t.entry_time.weekday() == index,
                _eq("dia_semana", name),
            )
        )
    for start in range(0, 24, 3):
        rules.append(
            FilterRule(
                f"franja_{start:02d}_{start + 3:02d}",
                "franja",
                f"No operar entre las {start:02d}:00 y las {start + 3:02d}:00 UTC",
                lambda t, start=start: start <= t.entry_time.hour < start + 3,
                _range("hora_utc", start, start + 3),
            )
        )
    for minutes in (30, 60):
        rules.append(
            FilterRule(
                f"tras_perdida_{minutes}",
                "tras_perdida",
                f"No abrir operaciones en los {minutes} minutos siguientes a una pérdida",
                lambda t, minutes=minutes: (
                    t.minutes_since_loss is not None and t.minutes_since_loss <= minutes
                ),
            )
        )
    for k in (2, 3):
        rules.append(
            FilterRule(
                f"racha_{k}",
                "racha",
                f"Pausar las entradas tras {k} pérdidas seguidas (no abrir mientras las últimas "
                f"{k} operaciones cerradas sean pérdidas)",
                lambda t, k=k: t.loss_streak >= k,
            )
        )
    threshold = spread_threshold(train, params)
    if threshold is not None:
        rules.append(
            FilterRule(
                "spread_alto",
                "spread",
                f"No entrar con un spread de {threshold:g} puntos o más (percentil 80 del "
                "entrenamiento)",
                lambda t, thr=threshold: (
                    None if t.spread_points is None else t.spread_points >= thr
                ),
                _range("spread_puntos", threshold, None),
            )
        )
    rules.append(
        FilterRule(
            "reentrada",
            "reentrada",
            "No reentrar tras un SL (desactivar la reentrada en los minutos siguientes a un SL)",
            lambda t: t.reentry,
            _eq("reentrada", True),
        )
    )
    return rules


def _apply(
    rows: Sequence[DiagTrade], rule: FilterRule
) -> tuple[list[DiagTrade], list[DiagTrade], int]:
    kept, removed, unknown = [], [], 0
    for t in rows:
        hit = rule.test(t)
        if hit is None:
            unknown += 1
            kept.append(t)
        elif hit:
            removed.append(t)
        else:
            kept.append(t)
    return kept, removed, unknown


def filter_eval(
    rows: Sequence[DiagTrade], rule: FilterRule, params: Params, min_n: int
) -> dict[str, Any]:
    kept, removed, unknown = _apply(rows, rule)
    all_rs = [t.r for t in rows]
    kept_rs, removed_rs = [t.r for t in kept], [t.r for t in removed]
    out: dict[str, Any] = {
        "n": len(removed),
        "n_total": len(rows),
        "original": r_stats(all_rs, params),
        "sugerida": r_stats(kept_rs, params),
        "evitadas": r_stats(removed_rs, params),
        "sin_dato": unknown,
        "mejora_r": None,
        "mejora_r_ic95": None,
    }
    if all_rs and kept_rs:
        out["mejora_r"] = _round(mean(kept_rs) - mean(all_rs))
    welch = welch_test(kept_rs, removed_rs)
    low = high = None
    if welch is not None and rows:
        share = len(removed) / len(rows)
        low, high = welch.ci_low * share, welch.ci_high * share
        out["mejora_r_ic95"] = [_round(low), _round(high)]
    extra = None
    if removed_rs and mean(removed_rs) >= 0:
        extra = f"las operaciones evitadas no pierden (expectativa {mean(removed_rs):+.2f} R)"
    out["paso"], out["motivo"] = _improvement_check(len(removed), low, high, min_n, extra)
    return out


# Elección en entrenamiento --------------------------------------------------------------------


def choose_exits(
    train: Sequence[ExitRow], rules: Sequence[er.ExitRule], params: Params
) -> dict[str, Any]:
    """Elige por familia la variante de mayor mejora de expectativa en ENTRENAMIENTO (solo
    recibe entrenamiento). Devuelve {"elegidas": {familia: (regla, evaluación)}, "probadas",
    "trampas_win_rate": [(regla, evaluación)], "sin_mejora": [familias]}."""
    by_family: dict[str, list[tuple[er.ExitRule, dict[str, Any]]]] = {}
    traps = []
    for rule in rules:
        ev = exit_eval(train, rule.key, params, params.min_n_train)
        by_family.setdefault(rule.family, []).append((rule, ev))
        wr_o, wr_s = ev["original"]["win_rate"], ev["sugerida"]["win_rate"]
        if (
            ev["n"] >= params.min_n_train
            and wr_o is not None
            and wr_s - wr_o >= params.win_rate_trap_points
            and ev["mejora_r"] is not None
            and ev["mejora_r"] < 0
        ):
            traps.append((rule, ev))
    chosen, flat = {}, []
    for family, items in by_family.items():
        eligible = [(r, ev) for r, ev in items if ev["n"] >= params.min_n_train]
        if not eligible:
            continue
        rule, ev = max(
            eligible,
            key=lambda x: (x[1]["mejora_r"], x[1]["sugerida"]["win_rate"] or 0, x[0].key),
        )
        if ev["mejora_r"] is None or ev["mejora_r"] <= 0:
            flat.append(family)
            continue
        chosen[family] = (rule, ev)
    return {
        "elegidas": chosen,
        "probadas": len(rules),
        "trampas_win_rate": traps,
        "sin_mejora": flat,
    }


def choose_filters(
    train: Sequence[DiagTrade], rules: Sequence[FilterRule], params: Params
) -> dict[str, Any]:
    """Por familia, el filtro que más sube la expectativa por operación en ENTRENAMIENTO, con
    muestra mínima en evitadas y mantenidas y evitadas con expectativa negativa."""
    chosen: dict[str, tuple[FilterRule, dict[str, Any]]] = {}
    for rule in rules:
        ev = filter_eval(train, rule, params, params.min_n_train)
        kept_n = ev["sugerida"]["n"]
        removed_exp = ev["evitadas"]["expectancy_r"]
        if (
            ev["n"] < params.min_n_train
            or kept_n < params.min_n_train
            or removed_exp is None
            or removed_exp >= 0
            or ev["mejora_r"] is None
            or ev["mejora_r"] <= 0
        ):
            continue
        current = chosen.get(rule.family)
        if current is None or (ev["mejora_r"], rule.key) > (current[1]["mejora_r"], current[0].key):
            chosen[rule.family] = (rule, ev)
    return {"elegidas": chosen, "probadas": len(rules)}


# Sugerencias -------------------------------------------------------------------------------------


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f} %"


def _headline(action: str, oos: dict[str, Any] | None, status: str) -> str:
    if oos is None or not oos["n"] or oos["original"]["expectancy_r"] is None:
        return f"{action}. Todavía sin resultado fuera de muestra ({status})."
    o, s = oos["original"], oos["sugerida"]
    ci = oos.get("mejora_r_ic95")
    ci_text = f"IC95 de la mejora [{ci[0]:+.2f}, {ci[1]:+.2f}] R, " if ci else ""
    exp_s = s["expectancy_r"]
    return (
        f"{action}: en datos fuera de muestra la expectativa pasa de {o['expectancy_r']:+.2f}R "
        f"a {exp_s:+.2f}R ({ci_text}n={oos['n']}); aciertos {_pct(o['win_rate'])} → "
        f"{_pct(s['win_rate'])}, profit factor "
        f"{o['profit_factor_r'] if o['profit_factor_r'] is not None else '—'} → "
        f"{s['profit_factor_r'] if s['profit_factor_r'] is not None else '—'}."
        if exp_s is not None
        else f"{action}: fuera de muestra no queda ninguna operación (n={oos['n']})."
    )


def _win_rate_flags(head: dict[str, Any] | None, status: str) -> tuple[bool, bool, str | None]:
    """(preferida, no_recomendada, aviso) con el tramo titular."""
    if head is None or head["sugerida"]["win_rate"] is None or head["original"]["win_rate"] is None:
        return False, False, None
    up = head["sugerida"]["win_rate"] > head["original"]["win_rate"]
    worse = head["mejora_r"] is not None and head["mejora_r"] < 0
    if up and worse:
        return False, True, WIN_RATE_TRAP
    if up and status == VALIDATED:
        return True, False, PREFERRED
    return False, False, None


def _curve(rows: Sequence[tuple[datetime, float, float]], max_points: int) -> dict[str, list]:
    """Curvas de R acumulado (original y sugerida) por hora de cierre."""
    ordered = sorted(rows, key=lambda x: x[0])
    a = b = 0.0
    orig, sug = [], []
    for when, r0, r1 in ordered:
        a += r0
        b += r1
        ms = int(when.timestamp() * 1000)
        orig.append([ms, round(a, 4)])
        sug.append([ms, round(b, 4)])
    if len(orig) > max_points:
        step = len(orig) / (max_points - 1)
        idx = [int(i * step) for i in range(max_points - 1)] + [len(orig) - 1]
        orig, sug = [orig[i] for i in idx], [sug[i] for i in idx]
    return {"original": orig, "sugerida": sug}


def _segments_of(rows: Sequence[Any]) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {s: [] for s in SEGMENT_NAMES}
    for row in rows:
        if row.segment in out:
            out[row.segment].append(row)
    return out


def exit_suggestions(rows: Sequence[ExitRow], params: Params, has_split: bool) -> dict[str, Any]:
    """Sugerencias de salida: elegidas en entrenamiento, evaluadas una vez en validación y
    fuera de muestra."""
    rules = er.exit_grid()
    families = sorted({r.family for r in rules})
    info: dict[str, Any] = {
        "variantes_probadas": len(rules),
        "familias": {f: sum(1 for r in rules if r.family == f) for f in families},
        "sugerencias": [],
        "trampas_win_rate": [],
        "sin_mejora_en_entrenamiento": [],
    }
    if not has_split:
        return info
    parts = _segments_of(rows)
    choice = choose_exits(parts[TRAIN], rules, params)
    info["sin_mejora_en_entrenamiento"] = sorted(choice["sin_mejora"])
    for family, (rule, train_ev) in sorted(choice["elegidas"].items()):
        validation = exit_eval(parts[VALIDATION], rule.key, params, params.min_n_validation)
        oos = exit_eval(parts[OOS], rule.key, params, params.min_n_oos)
        forward = exit_eval(parts[FORWARD], rule.key, params, params.min_n_oos)
        status, reason = _verdict(validation, oos, params)
        action = er.rule_text(rule)
        preferred, not_rec, note = _win_rate_flags(oos if oos["n"] else None, status)
        curve = _curve(
            [
                (row.close_time, row.results[er.BASELINE.key], row.results[rule.key])
                for row in parts[OOS]
                if rule.key in row.results
            ],
            params.curve_points,
        )
        info["sugerencias"].append(
            {
                "clave": f"salida:{rule.key}",
                "tipo": "SALIDA",
                "familia": family,
                "familia_texto": er.FAMILY_TEXT.get(family, family),
                "accion": action,
                "texto": _headline(action, oos if oos["n"] else None, status),
                "estado": status,
                "motivo_estado": reason,
                "preferida": preferred,
                "no_recomendada": not_rec,
                "aviso_win_rate": note,
                "variantes_probadas_familia": info["familias"][family],
                "regla": rule.spec(),
                "tramos": {
                    "entrenamiento": train_ev,
                    "validacion": validation,
                    "fuera_de_muestra": oos,
                    "forward": forward if forward["n"] else None,
                },
                "titular": "fuera_de_muestra",
                "curva_fuera_de_muestra": curve,
                "avisos": [WARNING_REPLAY],
                "experimento": {
                    "titulo": f"Diagnóstico: {action}"[:200],
                    "cambio": f"{action}. Propuesto por el diagnóstico ({status}). {reason}.",
                    "filtro": None,
                },
            }
        )
    for rule, ev in choice["trampas_win_rate"]:
        oos = exit_eval(parts[OOS], rule.key, params, params.min_n_oos)
        info["trampas_win_rate"].append(
            {
                "clave": f"salida:{rule.key}",
                "accion": er.rule_text(rule),
                "aviso": WIN_RATE_TRAP,
                "entrenamiento": ev,
                "fuera_de_muestra": oos if oos["n"] else None,
                "texto": (
                    f"{er.rule_text(rule)}: en entrenamiento los aciertos pasan de "
                    f"{_pct(ev['original']['win_rate'])} a {_pct(ev['sugerida']['win_rate'])}, "
                    f"pero la expectativa empeora {ev['mejora_r']:+.2f}R por operación. "
                    f"{WIN_RATE_TRAP}."
                ),
            }
        )
    return info


def filter_suggestions(
    trades: Sequence[DiagTrade], params: Params, has_split: bool
) -> dict[str, Any]:
    parts = _segments_of(trades)
    rules = filter_grid(parts[TRAIN], params) if has_split else filter_grid([], params)
    info: dict[str, Any] = {
        "filtros_probados": len(rules),
        "familias": {
            f: sum(1 for r in rules if r.family == f) for f in sorted({r.family for r in rules})
        },
        "sugerencias": [],
    }
    if not has_split:
        return info
    choice = choose_filters(parts[TRAIN], rules, params)
    for family, (rule, train_ev) in sorted(choice["elegidas"].items()):
        validation = filter_eval(parts[VALIDATION], rule, params, params.min_n_validation)
        oos = filter_eval(parts[OOS], rule, params, params.min_n_oos)
        forward = filter_eval(parts[FORWARD], rule, params, params.min_n_oos)
        status, reason = _verdict(validation, oos, params)
        preferred, not_rec, note = _win_rate_flags(oos if oos["n_total"] else None, status)
        kept_ids = {t.trade_id for t in _apply(parts[OOS], rule)[0]}
        curve = _curve(
            [(t.close_time, t.r, t.r if t.trade_id in kept_ids else 0.0) for t in parts[OOS]],
            params.curve_points,
        )
        avisos = [WARNING_COUNTERFACTUAL]
        if rule.spec is None:
            avisos.append(
                "Este filtro depende de la secuencia de resultados del bot: no se puede "
                "expresar como condición de entrada de la fase 9; el experimento se crea con "
                "la descripción del cambio."
            )
        info["sugerencias"].append(
            {
                "clave": f"filtro:{rule.key}",
                "tipo": "FILTRO",
                "familia": family,
                "familia_texto": FILTER_FAMILY_TEXT.get(family, family),
                "accion": rule.action,
                "texto": _headline(rule.action, oos if oos["n_total"] else None, status),
                "estado": status,
                "motivo_estado": reason,
                "preferida": preferred,
                "no_recomendada": not_rec,
                "aviso_win_rate": note,
                "variantes_probadas_familia": info["familias"][family],
                "regla": rule.describe(),
                "tramos": {
                    "entrenamiento": train_ev,
                    "validacion": validation,
                    "fuera_de_muestra": oos,
                    "forward": forward if forward["n_total"] else None,
                },
                "titular": "fuera_de_muestra",
                "curva_fuera_de_muestra": curve,
                "avisos": avisos,
                "experimento": {
                    "titulo": f"Diagnóstico: {rule.action}"[:200],
                    "cambio": f"{rule.action}. Propuesto por el diagnóstico ({status}). {reason}.",
                    "filtro": rule.spec,
                },
            }
        )
    return info


def rank_suggestions(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validadas primero (las que suben los aciertos sin empeorar, antes), después candidatas
    y sin datos; las "no recomendadas" al final. Dentro, por mejora fuera de muestra."""

    def key(s: dict[str, Any]) -> tuple:
        oos = s["tramos"].get("fuera_de_muestra") or {}
        gain = oos.get("mejora_r")
        return (
            1 if s["no_recomendada"] else 0,
            STATUS_RANK[s["estado"]],
            0 if s["preferida"] else 1,
            -(gain if gain is not None else -1e9),
            s["clave"],
        )

    return sorted(items, key=key)


# Qué está fallando (descriptivo) ------------------------------------------------------------


def _examples(group: Sequence[DiagTrade], count: int = 5) -> list[dict[str, Any]]:
    worst = sorted(group, key=lambda t: (t.r, t.trade_id))[:count]
    return [{"trade_id": t.trade_id, "r": round(t.r, 4), "entrada": t.entry_time} for t in worst]


def _failure(
    code: str,
    title: str,
    explanation: str,
    group: Sequence[DiagTrade],
    rest: Sequence[DiagTrade],
    total: int,
    params: Params,
    families: list[str],
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    net = sum(t.r for t in group)
    stats = r_stats([t.r for t in group], params)
    rest_stats = r_stats([t.r for t in rest], params)
    return {
        "codigo": code,
        "titulo": title,
        "explicacion": explanation,
        "n": len(group),
        "porcentaje": round(len(group) / total, 4) if total else None,
        "r_neto": round(net, 4),
        "r_perdido": round(max(0.0, -net), 4),
        "grupo": stats,
        "resto": rest_stats,
        "ejemplos": _examples(group),
        "familias": families,
        "detalle": detail or {},
        "etiqueta": DESCRIPTIVE,
    }


def _rest(trades: Sequence[DiagTrade], group: Sequence[DiagTrade]) -> list[DiagTrade]:
    ids = {t.trade_id for t in group}
    return [t for t in trades if t.trade_id not in ids]


def _hour_block(t: DiagTrade) -> int:
    return t.entry_time.hour // 3 * 3


def group_table(
    trades: Sequence[DiagTrade],
    key: Callable[[DiagTrade], Any],
    label: Callable[[Any], str],
    params: Params,
) -> list[dict[str, Any]]:
    groups: dict[Any, list[DiagTrade]] = {}
    for t in trades:
        groups.setdefault(key(t), []).append(t)
    return [
        {"clave": k, "etiqueta": label(k), **r_stats([t.r for t in groups[k]], params)}
        for k in sorted(groups)
    ]


def timing_tables(trades: Sequence[DiagTrade], params: Params) -> dict[str, list[dict[str, Any]]]:
    order = {label: i for i, (_, _, label) in enumerate(SESSION_BLOCKS)}
    return {
        "sesiones": group_table(
            trades,
            lambda t: order[session_label(t.entry_time)],
            lambda k: SESSION_BLOCKS[k][2],
            params,
        ),
        "franjas": group_table(
            trades, _hour_block, lambda k: f"{k:02d}:00-{k + 3:02d}:00 UTC", params
        ),
        "dias": group_table(
            trades, lambda t: t.entry_time.weekday(), lambda k: WEEKDAYS[k], params
        ),
    }


def failure_modes(
    trades: Sequence[DiagTrade],
    params: Params,
    traps: Sequence[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Modos de fallo con evidencia, ordenados por R perdido. `traps`: trampas validadas de la
    fase 9 con los trade_id que cumplen su condición ({"id", "statement", "trade_ids",
    "fuera_de_muestra"})."""
    total = len(trades)
    out: list[dict[str, Any]] = []
    be = params.breakeven_r

    evaluable = [t for t in trades if t.sl_then_tp is not None]
    group = [t for t in evaluable if t.sl_then_tp]
    if group:
        out.append(
            _failure(
                "SL_DEMASIADO_AJUSTADO",
                "SL demasiado ajustado",
                f"{len(group)} de {len(evaluable)} operaciones cerradas por SL (con TP y velas) "
                "llegaron después a su TP original dentro del horizonte de la réplica: el "
                "precio fue a favor, pero tras sacarlas.",
                group,
                _rest(trades, group),
                total,
                params,
                ["sl", "sl_tp"],
                {"evaluables": len(evaluable)},
            )
        )

    with_tp = [t for t in trades if t.tp_r and t.mfe_r is not None]
    group = [t for t in with_tp if t.mfe_r >= params.tp_reach_fraction * t.tp_r and t.r <= be]
    if group:
        out.append(
            _failure(
                "TP_DEMASIADO_LEJOS",
                "TP demasiado lejos",
                f"{len(group)} operaciones recorrieron al menos el "
                f"{_pct(params.tp_reach_fraction)} del camino al TP y aun así cerraron en pérdida "
                "o en breakeven.",
                group,
                _rest(with_tp, group),
                total,
                params,
                ["tp", "sl_tp"],
                {"evaluables": len(with_tp), "umbral_fraccion_tp": params.tp_reach_fraction},
            )
        )

    with_mfe = [t for t in trades if t.mfe_r is not None]
    group = [t for t in with_mfe if t.mfe_r >= 1.0 and t.r <= 0]
    if group:
        out.append(
            _failure(
                "DEVUELVE_BENEFICIO",
                "Devuelve el beneficio",
                f"{len(group)} operaciones llegaron a ir +1R o más a favor y terminaron con "
                "neto ≤ 0.",
                group,
                _rest(with_mfe, group),
                total,
                params,
                ["breakeven", "trailing", "tiempo"],
                {"evaluables": len(with_mfe)},
            )
        )

    tables = timing_tables(trades, params)
    bad_blocks = {
        row["clave"]
        for row in tables["franjas"]
        if row["n"] >= params.min_group and row["expectancy_r"] < 0
    }
    group = [t for t in trades if _hour_block(t) in bad_blocks]
    rest = [t for t in trades if _hour_block(t) not in bad_blocks]
    if group and rest and mean([t.r for t in group]) < mean([t.r for t in rest]):
        names = ", ".join(f"{b:02d}:00-{b + 3:02d}:00" for b in sorted(bad_blocks))
        out.append(
            _failure(
                "HORARIO",
                "Horario con expectativa negativa",
                f"Las entradas en las franjas {names} UTC tienen expectativa negativa "
                f"(al menos {params.min_group} operaciones por franja). Detalle por sesión, "
                "franja y día.",
                group,
                rest,
                total,
                params,
                ["sesion", "franja", "dia"],
                tables,
            )
        )

    group = [
        t
        for t in trades
        if t.minutes_since_loss is not None and t.minutes_since_loss <= params.after_loss_minutes
    ]
    rest = _rest(trades, group)
    streak = [t for t in trades if t.loss_streak >= params.loss_streak]
    if group and rest and mean([t.r for t in group]) < mean([t.r for t in rest]):
        out.append(
            _failure(
                "TRAS_PERDIDAS",
                "Opera peor después de perder",
                f"{len(group)} operaciones se abrieron en los {params.after_loss_minutes} minutos "
                "siguientes a una pérdida (posible revancha o sobreoperación) y rinden peor que "
                "el resto.",
                group,
                rest,
                total,
                params,
                ["tras_perdida", "racha"],
                {
                    "minutos": params.after_loss_minutes,
                    f"tras_{params.loss_streak}_perdidas_seguidas": r_stats(
                        [t.r for t in streak], params
                    ),
                    "resto_sin_racha": r_stats(
                        [t.r for t in trades if t.loss_streak < params.loss_streak], params
                    ),
                },
            )
        )

    spreads = [t for t in trades if t.spread_points is not None]
    if len(spreads) >= params.min_group * 2:
        values = sorted(t.spread_points for t in spreads)
        threshold = values[min(len(values) - 1, math.floor(0.8 * len(values)))]
        group = [t for t in spreads if t.spread_points >= threshold]
        rest = [t for t in spreads if t.spread_points < threshold]
        if group and rest and mean([t.r for t in group]) < mean([t.r for t in rest]):
            out.append(
                _failure(
                    "SPREAD_ALTO",
                    "Entradas con spread alto",
                    f"Con un spread de {threshold:g} puntos o más (el 20 % más alto) el bot rinde "
                    "peor que con un spread normal.",
                    group,
                    rest,
                    total,
                    params,
                    ["spread"],
                    {"umbral_puntos": threshold, "evaluables": len(spreads)},
                )
            )

    group = [t for t in trades if t.reentry]
    rest = [t for t in trades if not t.reentry]
    if group and rest and mean([t.r for t in group]) < mean([t.r for t in rest]):
        out.append(
            _failure(
                "REENTRADAS",
                "Reentradas tras SL",
                f"{len(group)} reentradas poco después de un SL rinden peor que las primeras "
                "entradas.",
                group,
                rest,
                total,
                params,
                ["reentrada"],
            )
        )

    by_id = {t.trade_id: t for t in trades}
    for trap in traps:
        ids = set(trap["trade_ids"])
        group = [by_id[i] for i in ids if i in by_id]
        rest = [t for t in trades if t.trade_id not in ids]
        item = _failure(
            f"TRAMPA:{trap['id']}",
            f"Trampa validada: {trap['statement']}",
            "Condición de entrada validada fuera de muestra por la búsqueda de patrones (fase 9).",
            group,
            rest,
            total,
            params,
            [],
            {"fuera_de_muestra": trap.get("fuera_de_muestra")},
        )
        item["etiqueta"] = "trampa validada"
        item["hypothesis_id"] = trap["id"]
        out.append(item)

    out = [f for f in out if f["r_perdido"] > 0]
    out.sort(key=lambda f: (-f["r_perdido"], f["codigo"]))
    return out


# Qué funciona mejor ----------------------------------------------------------------------------


def best_timing(trades: Sequence[DiagTrade], params: Params, count: int = 3) -> dict[str, Any]:
    tables = timing_tables(trades, params)
    out: dict[str, Any] = {}
    for name, rows in tables.items():
        good = [r for r in rows if r["n"] >= params.min_group and (r["expectancy_r"] or 0) > 0]
        good.sort(key=lambda r: (-r["expectancy_r"], str(r["clave"])))
        out[name] = [
            {**r, "ic_positivo": bool(r["expectancy_r_ic95"] and r["expectancy_r_ic95"][0] > 0)}
            for r in good[:count]
        ]
    out["etiqueta"] = DESCRIPTIVE
    return out
