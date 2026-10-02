"""Consulta de patrones, trampas e informe de la versión (fase 9). Solo lectura.

Etiquetas que ve el usuario (nunca se llama trampa a algo que no pasó fuera de muestra):
- VALIDATED: "trampa validada" / "ventaja validada" (pasó entrenamiento con FDR, validación
  y fuera de muestra, y el forward no la contradice).
- PROPOSED / TESTING: "candidata, no validada".
- REJECTED: "rechazada". DECAYED: "caducada" (el forward la contradijo).
"""

import uuid
from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from supervisor.analytics import pattern_search as ps
from supervisor.analytics import patterns as pt
from supervisor.config import Settings
from supervisor.models import Bot, BotVersion, DataSplit, Hypothesis, PatternRun, PatternTest
from supervisor.models.enums import HypothesisStatus, SplitSegment
from supervisor.services.errors import NotFound

CANDIDATE = "candidata, no validada"
NOT_VALIDATED_NOTE = (
    "Candidata, no validada: pasó (o espera) alguna prueba pero no la de fuera de muestra. "
    "No es una trampa hasta que la pase."
)
STATUS_ORDER = {
    HypothesisStatus.VALIDATED: 0,
    HypothesisStatus.TESTING: 1,
    HypothesisStatus.PROPOSED: 2,
    HypothesisStatus.DECAYED: 3,
    HypothesisStatus.REJECTED: 4,
}
SEGMENT_FIELDS = {
    SplitSegment.TRAIN: "entrenamiento",
    SplitSegment.VALIDATION: "validacion",
    SplitSegment.OOS: "fuera_de_muestra",
    SplitSegment.FORWARD: "forward",
}
# Pruebas de una misma ejecución comparten run_at (la hora de la transacción).
SEGMENT_ORDER = case(
    {s.value: i for i, s in enumerate(SEGMENT_FIELDS)}, value=PatternTest.segment, else_=9
)
DRAWDOWN_TOP = 5
DIFFERENCES_TOP = 8


def label(status: HypothesisStatus, kind: str | None) -> str:
    if status == HypothesisStatus.VALIDATED:
        return f"{pt.KIND_TEXT.get(kind, 'patrón')} validada"
    if status in (HypothesisStatus.PROPOSED, HypothesisStatus.TESTING):
        return CANDIDATE
    if status == HypothesisStatus.DECAYED:
        return "caducada"
    return "rechazada"


def _test_view(t: PatternTest) -> dict[str, Any]:
    details = dict(t.details or {})
    return {
        "segmento": t.segment.value,
        "n": t.n,
        "win_rate": details.get("win_rate"),
        "win_rate_ic95": details.get("win_rate_ic95"),
        "expectancy_r": details.get("expectancy_r"),
        "expectancy_r_ic95": details.get("expectancy_r_ic95"),
        "profit_factor_r": details.get("profit_factor_r"),
        "expectancy_r_complemento": details.get("expectancy_r_complemento"),
        "diferencia_r": details.get("diferencia_r"),
        "p_valor": details.get("p_valor"),
        "p_ajustado": float(t.p_adjusted) if t.p_adjusted is not None else None,
        "pruebas_en_familia": t.tests_in_family,
        "mae_r": details.get("mae_r"),
        "mae_r_complemento": details.get("mae_r_complemento"),
        "paso": t.passed,
        "motivo": details.get("motivo"),
        "metodo": t.method,
        "run_at": t.run_at,
    }


def _tests_by_hypothesis(
    session: Session, ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[PatternTest]]:
    out: dict[uuid.UUID, list[PatternTest]] = defaultdict(list)
    for start in range(0, len(ids), 1000):
        stmt = (
            select(PatternTest)
            .where(PatternTest.hypothesis_id.in_(ids[start : start + 1000]))
            .order_by(PatternTest.run_at, SEGMENT_ORDER, PatternTest.n)
        )
        for t in session.scalars(stmt):
            out[t.hypothesis_id].append(t)
    return out


def hypothesis_view(h: Hypothesis, tests: list[PatternTest]) -> dict[str, Any]:
    segments: dict[str, Any] = {name: None for name in SEGMENT_FIELDS.values()}
    for t in tests:  # en orden: el último FORWARD queda
        segments[SEGMENT_FIELDS[t.segment]] = _test_view(t)
    return {
        "id": h.id,
        "statement": h.statement,
        "kind": h.kind,
        "kind_text": pt.KIND_TEXT.get(h.kind, "patrón"),
        "origin": h.origin,
        "status": h.status,
        "label": label(h.status, h.kind),
        "validated": h.status == HypothesisStatus.VALIDATED,
        "status_note": h.status_note,
        "bot_version_id": h.bot_version_id,
        "symbol": h.symbol,
        "condition_spec": h.condition_spec,
        "split_id": h.split_id,
        "forward_from": h.forward_from,
        "created_at": h.created_at,
        "updated_at": h.updated_at,
        **segments,
    }


def _sort_key(h: Hypothesis) -> tuple:
    return (STATUS_ORDER.get(h.status, 9), 0 if h.kind == pt.TRAP else 1, h.statement)


def list_patterns(
    session: Session,
    *,
    bot_id: uuid.UUID | None = None,
    version_id: uuid.UUID | None = None,
    symbol: str | None = None,
    status: HypothesisStatus | None = None,
    kind: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Hipótesis de la fase 9 (búsqueda y reglas): validadas primero, luego candidatas."""
    stmt = select(Hypothesis).where(Hypothesis.bot_version_id.is_not(None))
    if bot_id is not None:
        stmt = stmt.join(BotVersion, BotVersion.id == Hypothesis.bot_version_id).where(
            BotVersion.bot_id == bot_id
        )
    if version_id is not None:
        stmt = stmt.where(Hypothesis.bot_version_id == version_id)
    if symbol:
        stmt = stmt.where(Hypothesis.symbol == symbol)
    if status is not None:
        stmt = stmt.where(Hypothesis.status == status)
    if kind is not None:
        stmt = stmt.where(Hypothesis.kind == kind)
    order = case(
        {s.value: rank for s, rank in STATUS_ORDER.items()}, value=Hypothesis.status, else_=9
    )
    stmt = stmt.order_by(
        order, case((Hypothesis.kind == pt.TRAP, 0), else_=1), Hypothesis.created_at.desc()
    ).limit(limit)
    rows = list(session.scalars(stmt))
    tests = _tests_by_hypothesis(session, [h.id for h in rows])
    return [hypothesis_view(h, tests.get(h.id, [])) for h in sorted(rows, key=_sort_key)]


def pattern_detail(session: Session, hypothesis_id: uuid.UUID) -> dict[str, Any]:
    h = session.get(Hypothesis, hypothesis_id)
    if h is None:
        raise NotFound("patrón no encontrado")
    tests = _tests_by_hypothesis(session, [h.id]).get(h.id, [])
    split = session.get(DataSplit, h.split_id) if h.split_id else None
    run = session.get(PatternRun, h.run_id) if h.run_id else None
    return {
        **hypothesis_view(h, tests),
        "tests": [_test_view(t) for t in tests],
        "split": _split_view(split),
        "run": _run_view(run),
    }


def _split_view(split: DataSplit | None) -> dict[str, Any] | None:
    if split is None:
        return None
    return {
        "id": split.id,
        "train_end": split.train_end,
        "validation_end": split.validation_end,
        "oos_end": split.oos_end,
        "frozen_at": split.frozen_at,
        "notes": split.notes,
    }


def _run_view(run: PatternRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "id": run.id,
        "status": run.status,
        "symbol": run.symbol,
        "n_trades": run.n_trades,
        "n_train": run.n_train,
        "n_validation": run.n_validation,
        "n_oos": run.n_oos,
        "n_forward": run.n_forward,
        "candidates_generated": run.candidates_generated,
        "candidates_tested": run.candidates_tested,
        "candidates_low_sample": run.candidates_low_sample,
        "passed_train": run.passed_train,
        "passed_validation": run.passed_validation,
        "validated": run.validated,
        "rejected": run.rejected,
        "finished_at": run.finished_at,
        "split_id": run.split_id,
    }


# Reglas de la fase 6 --------------------------------------------------------------------


def rule_validation(
    session: Session, version_id: uuid.UUID | None, symbol: str | None
) -> dict[tuple[str, int], dict[str, Any]]:
    """Estado de validación de las reglas de la fase 6 en el alcance de una operación. Los
    hallazgos del análisis no se modifican: se consulta aquí. Gana la hipótesis del símbolo
    de la operación si existe; si no, la de toda la versión."""
    if version_id is None:
        return {}
    stmt = select(Hypothesis).where(
        Hypothesis.bot_version_id == version_id,
        Hypothesis.origin == "REGLA",
        (Hypothesis.symbol.is_(None)) | (Hypothesis.symbol == symbol),
    )
    out: dict[tuple[str, int], dict[str, Any]] = {}
    ranked = sorted(
        session.scalars(stmt), key=lambda h: (h.symbol is not None, h.created_at), reverse=False
    )
    for h in ranked:  # las de símbolo y las más recientes al final: sobrescriben
        spec = h.condition_spec or {}
        out[(spec.get("regla"), int(spec.get("version", 0)))] = {
            "hypothesis_id": str(h.id),
            "estado": h.status.value,
            "etiqueta": label(h.status, h.kind),
            "nota": h.status_note,
        }
    return out


def no_rule_validation() -> dict[str, Any]:
    return {
        "hypothesis_id": None,
        "estado": None,
        "etiqueta": "sin contrastar",
        "nota": "la búsqueda de patrones aún no ha contrastado esta regla en este bot",
    }


# Resumen y avisos -----------------------------------------------------------------------


def _latest_run(
    session: Session, version_id: uuid.UUID, symbol: str | None, status: str | None = None
) -> PatternRun | None:
    stmt = select(PatternRun).where(
        PatternRun.bot_version_id == version_id, ps.symbol_filter(PatternRun.symbol, symbol)
    )
    if status:
        stmt = stmt.where(PatternRun.status == status)
    return session.scalar(stmt.order_by(PatternRun.finished_at.desc()).limit(1))


def overview(
    session: Session, settings: Settings, version_id: uuid.UUID, symbol: str | None = None
) -> dict[str, Any]:
    """Recuentos y avisos de un alcance: validadas, candidatas y candidatas probadas."""
    counts: Counter = Counter()
    rows = session.execute(
        select(Hypothesis.status, Hypothesis.kind, func.count())
        .where(
            Hypothesis.bot_version_id == version_id,
            ps.symbol_filter(Hypothesis.symbol, symbol),
        )
        .group_by(Hypothesis.status, Hypothesis.kind)
    ).all()
    for status, kind, n in rows:
        counts[(status, kind)] += n
    completed = _latest_run(session, version_id, symbol, ps.COMPLETED)
    last = _latest_run(session, version_id, symbol)
    tested_total = session.scalar(
        select(func.coalesce(func.sum(PatternRun.candidates_tested), 0)).where(
            PatternRun.bot_version_id == version_id,
            ps.symbol_filter(PatternRun.symbol, symbol),
        )
    )
    n_closed = ps.count_observations(session, version_id, symbol)
    message = None
    if completed is None and n_closed < settings.patterns_min_trades:
        message = ps.insufficient_message(n_closed, settings)
    elif completed is None:
        message = (
            f"Hay {n_closed} operaciones cerradas pero la búsqueda aún no se ha ejecutado (el "
            "worker la lanza una vez al día; también con supervisor-cli patterns)."
        )

    def total(statuses: tuple[HypothesisStatus, ...], kind: str) -> int:
        return sum(counts[(s, kind)] for s in statuses)

    candidates = (HypothesisStatus.PROPOSED, HypothesisStatus.TESTING)
    return {
        "trampas_validadas": total((HypothesisStatus.VALIDATED,), pt.TRAP),
        "ventajas_validadas": total((HypothesisStatus.VALIDATED,), pt.EDGE),
        "trampas_candidatas": total(candidates, pt.TRAP),
        "ventajas_candidatas": total(candidates, pt.EDGE),
        "rechazadas": total((HypothesisStatus.REJECTED,), pt.TRAP)
        + total((HypothesisStatus.REJECTED,), pt.EDGE),
        "caducadas": total((HypothesisStatus.DECAYED,), pt.TRAP)
        + total((HypothesisStatus.DECAYED,), pt.EDGE),
        "candidatas_probadas_ultima": completed.candidates_tested if completed else 0,
        "candidatas_probadas_total": int(tested_total or 0),
        "ultima_ejecucion": _run_view(last),
        "ultima_completa": _run_view(completed),
        "operaciones_cerradas": n_closed,
        "aviso": message,
    }


def traps_page(session: Session, settings: Settings, limit: int = 30) -> list[dict[str, Any]]:
    """Datos de la página "Trampas": un bloque por alcance con búsqueda ejecutada y por versión
    con operaciones cerradas (aunque aún no haya datos para buscar: muestra el aviso)."""
    rows = session.execute(
        select(PatternRun.bot_version_id, PatternRun.symbol, func.max(PatternRun.finished_at))
        .group_by(PatternRun.bot_version_id, PatternRun.symbol)
        .order_by(func.max(PatternRun.finished_at).desc())
        .limit(limit)
    ).all()
    scopes = [(version_id, symbol) for version_id, symbol, _ in rows]
    for version_id in ps.versions_with_trades(session, limit):
        if (version_id, None) not in scopes and len(scopes) < limit:
            scopes.append((version_id, None))
    out = []
    for version_id, symbol in scopes:
        version = session.get(BotVersion, version_id)
        bot = session.get(Bot, version.bot_id)
        items = list_patterns(session, version_id=version_id, symbol=symbol)
        items = [i for i in items if i["symbol"] == symbol]
        out.append(
            {
                "bot": bot,
                "version": version,
                "symbol": symbol,
                "overview": overview(session, settings, version_id, symbol),
                "validated": [i for i in items if i["validated"] and i["kind"] == pt.TRAP],
                "validated_edges": [i for i in items if i["validated"] and i["kind"] == pt.EDGE],
                "candidates": [
                    i
                    for i in items
                    if i["status"] in (HypothesisStatus.PROPOSED, HypothesisStatus.TESTING)
                    and i["entrenamiento"] is not None
                ],
            }
        )
    out.sort(key=lambda b: (-b["overview"]["trampas_validadas"], b["bot"].name, b["symbol"] or ""))
    return out


def active_traps(session: Session) -> int:
    return (
        session.scalar(
            select(func.count()).where(
                Hypothesis.status == HypothesisStatus.VALIDATED, Hypothesis.kind == pt.TRAP
            )
        )
        or 0
    )


# Informe de la versión (sección 10) ------------------------------------------------------


def _candidate_from_summary(item: dict[str, Any], kind: str) -> dict[str, Any]:
    """Candidata del resumen de la última ejecución (solo entrenamiento, sin guardar)."""
    return {
        "id": None,
        "statement": item["condicion"],
        "kind": kind,
        "kind_text": pt.KIND_TEXT[kind],
        "origin": "BUSQUEDA",
        "status": None,
        "label": CANDIDATE,
        "validated": False,
        "status_note": "solo en entrenamiento: no superó el FDR o no se ha validado",
        "condition_spec": item["spec"],
        "entrenamiento": {
            "segmento": "TRAIN",
            "n": item["n"],
            "win_rate": item["win_rate"],
            "win_rate_ic95": item["win_rate_ic95"],
            "expectancy_r": item["expectancy_r"],
            "expectancy_r_ic95": item["expectancy_r_ic95"],
            "profit_factor_r": item["profit_factor_r"],
            "expectancy_r_complemento": item["expectancy_r_complemento"],
            "diferencia_r": item["diferencia_r"],
            "p_valor": item["p_valor"],
            "p_ajustado": item["p_ajustado"],
            "mae_r": item["mae_r"],
            "mae_r_complemento": item["mae_r_complemento"],
        },
        "validacion": None,
        "fuera_de_muestra": None,
        "forward": None,
    }


def _conditions(
    items: list[dict[str, Any]], run: PatternRun | None, kind: str, count: int = 10
) -> list[dict[str, Any]]:
    keep = (HypothesisStatus.VALIDATED, HypothesisStatus.TESTING, HypothesisStatus.PROPOSED)
    out = [
        i
        for i in items
        if i["kind"] == kind and i["status"] in keep and i["entrenamiento"] is not None
    ]
    seen = {pt.condition_key(i["condition_spec"]) for i in out}
    if run is not None:
        field = "trampas_candidatas" if kind == pt.TRAP else "ventajas_candidatas"
        for item in (run.summary or {}).get(field, []):
            if pt.condition_key(item["spec"]) not in seen:
                out.append(_candidate_from_summary(item, kind))
    return out[:count]


def _winners_vs_losers(obs: list[pt.Obs], min_n: int) -> dict[str, Any]:
    """Descriptivo: en qué se diferencian las ganadoras de las perdedoras (sin validar)."""
    wins = [o for o in obs if o.win]
    losses = [o for o in obs if not o.win and o.r < 0]

    def group(rows: list[pt.Obs]) -> dict[str, Any]:
        maes = [o.mae_r for o in rows if o.mae_r is not None]
        return {
            "n": len(rows),
            "r_medio": round(sum(o.r for o in rows) / len(rows), 4) if rows else None,
            "mae_r_medio": round(sum(maes) / len(maes), 4) if maes else None,
        }

    labels = ps.variable_labels()
    diffs = []
    variables = [v for v in ps.search_variables() if v.value_type != "numeric"]
    for v in variables:
        values = Counter(
            o.values.get(v.name) for o in wins + losses if o.values.get(v.name) is not None
        )
        known_w = [o for o in wins if o.values.get(v.name) is not None]
        known_l = [o for o in losses if o.values.get(v.name) is not None]
        if not known_w or not known_l:
            continue
        for value, n in values.items():
            if n < min_n:
                continue
            share_w = sum(1 for o in known_w if o.values[v.name] == value) / len(known_w)
            share_l = sum(1 for o in known_l if o.values[v.name] == value) / len(known_l)
            clause = pt.Clause(v.name, "eq", value=value)
            diffs.append(
                {
                    "condicion": pt.describe(pt.Condition((clause,)).spec(), labels),
                    "n": n,
                    "en_ganadoras": round(share_w, 4),
                    "en_perdedoras": round(share_l, 4),
                    "diferencia": round(share_w - share_l, 4),
                }
            )
    diffs.sort(key=lambda d: (-abs(d["diferencia"]), d["condicion"]))
    return {
        "ganadoras": group(wins),
        "perdedoras": group(losses),
        "diferencias": diffs[:DIFFERENCES_TOP],
        "nota": "descriptivo: proporción de cada condición entre ganadoras y perdedoras; "
        "no es una prueba estadística",
    }


def _drawdown(
    obs: list[pt.Obs], split: DataSplit | None, params: pt.SearchParams
) -> list[dict[str, Any]]:
    """Condiciones con más MAE (en R) que su complemento en entrenamiento, y lo mismo fuera de
    muestra. Tramos numéricos solo con entrenamiento."""
    if split is None:
        return []
    parts = ps.segments(obs, split)
    train, oos = parts[SplitSegment.TRAIN], parts[SplitSegment.OOS]
    labels = ps.variable_labels()
    rows = []
    for clause in pt.single_clauses(train, ps.search_variables(), params.quantile_bins):
        condition = pt.Condition((clause,))
        stats, _ = pt.evaluate(condition, train)
        if (
            stats["n"] < params.min_n_train
            or stats["n_complemento"] < params.min_n_train
            or stats["mae_r"] is None
            or stats["mae_r_complemento"] is None
        ):
            continue
        rows.append((stats["mae_r"] - stats["mae_r_complemento"], condition, stats))
    rows.sort(key=lambda r: (-r[0], r[1].key()))
    out = []
    for extra, condition, stats in rows[:DRAWDOWN_TOP]:
        if extra <= 0:
            break
        oos_stats, _ = pt.evaluate(condition, oos)
        out.append(
            {
                "condicion": pt.describe(condition.spec(), labels),
                "mae_r_extra": round(extra, 4),
                "label": "descriptiva, no validada",
                "entrenamiento": stats,
                "fuera_de_muestra": oos_stats if oos_stats["n"] else None,
            }
        )
    return out


def version_report(
    session: Session,
    settings: Settings,
    bot_id: uuid.UUID,
    version_id: uuid.UUID,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Informe de la sección 10 para una versión de bot."""
    version = session.get(BotVersion, version_id)
    if version is None or version.bot_id != bot_id:
        raise NotFound("versión de bot no encontrada")
    bot = session.get(Bot, bot_id)
    summary = overview(session, settings, version_id, symbol)
    items = [
        i for i in list_patterns(session, version_id=version_id, limit=500) if i["symbol"] == symbol
    ]
    run = _latest_run(session, version_id, symbol, ps.COMPLETED)
    obs = ps.load_observations(session, version_id, symbol, settings)
    split = ps.latest_split(session, ps.scope_dict(version, symbol))
    worst = _conditions(items, run, pt.TRAP)
    best = _conditions(items, run, pt.EDGE)
    pairs = [i for i in best if len((i["condition_spec"] or {}).get("clausulas", [])) == 2]
    return {
        "bot_id": bot_id,
        "bot_name": bot.name,
        "version_id": version_id,
        "version": version.version,
        "symbol": symbol,
        "resumen": summary,
        "split": _split_view(split),
        "ganadoras_vs_perdedoras": _winners_vs_losers(obs, settings.patterns_min_n_validation),
        "peores_condiciones": worst,
        "mejores_condiciones": best,
        "mejor_combinacion": pairs[0] if pairs else None,
        "aumentan_drawdown": _drawdown(obs, split, ps.search_params(settings)),
        "reglas": [i for i in items if i["origin"] == "REGLA"],
        "nota": NOT_VALIDATED_NOTE,
    }
