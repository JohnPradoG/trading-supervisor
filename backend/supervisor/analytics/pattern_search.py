"""Búsqueda de patrones y validación de trampas (fase 9): datos, splits y persistencia.

La estadística está en supervisor.analytics.patterns (funciones puras). Aquí:

1. **Datos.** Operaciones CERRADAS de una versión de bot (y opcionalmente un símbolo; nunca
   se mezclan bots ni versiones), solo con riesgo conocido (R = neto / riesgo), ordenadas por
   hora de cierre. Variables al entrar: las del Trading DNA vigente marcadas como buscables y
   disponibles, la dirección, si es una reentrada tras SL y cuatro hechos previos a la entrada
   del análisis (precio a favor de EMA200/EMA50 en H1 y M15).
2. **Split congelado.** La primera vez que hay patterns_min_trades operaciones se crea un
   data_split cronológico (60/20/20 por defecto, sin barajar): ENTRENAMIENTO hasta train_end,
   VALIDACIÓN hasta validation_end, FUERA DE MUESTRA hasta oos_end. Es de solo inserción. Lo
   que cierra después de oos_end es FORWARD.
3. **Búsqueda (solo con entrenamiento).** Condiciones simples y pares, muestra mínima, test
   de Welch, FDR de Benjamini-Hochberg sobre todas las probadas. Las que pasan como TRAMPA o
   VENTAJA se guardan como hipótesis (PROPOSED) con su prueba TRAIN.
4. **Validación.** Mismo sentido, muestra mínima e intervalo de la expectativa sin cruzar 0.
   Si falla: REJECTED. Si pasa: TESTING.
5. **Fuera de muestra, una sola vez.** Mismo criterio: VALIDATED o REJECTED. Si la muestra
   fuera de muestra no llega al mínimo, queda TESTING y se evalúa una única vez cuando el
   tramo fuera de muestra más el forward la alcance. Un índice único impide una segunda
   prueba OOS de la misma hipótesis.
6. **Forward.** Las VALIDATED se comprueban con las operaciones cerradas después de su
   forward_from: DECAYED si la expectativa pasa a ser significativamente contraria.
7. **Repetición.** Sin operaciones nuevas no se repite nada (ni se vuelve a mirar el OOS).
   Con patterns_new_trades operaciones nuevas después del último split se crea un split nuevo
   con todos los datos y se repite la búsqueda (una familia nueva, contada aparte). Una
   condición que ya tiene una hipótesis viva en ese alcance no se duplica.
8. **Reglas de la fase 6.** Las reglas cuyas condiciones son hechos previos a la entrada se
   contrastan con el mismo circuito (familia propia para el FDR): las de pérdidas como
   trampa, las de ganancias como ventaja. Las que describen lo que pasó durante la operación
   (MAE, MFE, TP tocado) no son condiciones de entrada y no se contrastan.
"""

import logging
import math
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from supervisor.analytics import patterns as pt
from supervisor.analytics.dna_features import FEATURES
from supervisor.analytics.outcome import classify_outcome
from supervisor.analytics.rules import RULES, Rule
from supervisor.config import Settings
from supervisor.models import BotVersion, DataSplit, Hypothesis, PatternRun, PatternTest, Trade
from supervisor.models.enums import HypothesisStatus, Outcome, SplitSegment, TradeStatus

log = logging.getLogger("supervisor.patterns")

LIVE = (HypothesisStatus.PROPOSED, HypothesisStatus.TESTING, HypothesisStatus.VALIDATED)
COMPLETED, INSUFFICIENT = "COMPLETADA", "MUESTRA_INSUFICIENTE"
TRAIN_METHOD = "welch_t+bh_fdr"
CHECK_METHOD = "ic95_t_expectativa"

FACT_VARIABLES = (
    ("hecho_ema200_h1_a_favor", "PRECIO_VS_EMA200_H1", "Precio a favor de la EMA200 H1"),
    ("hecho_ema50_h1_a_favor", "PRECIO_VS_EMA50_H1", "Precio a favor de la EMA50 H1"),
    ("hecho_ema200_m15_a_favor", "PRECIO_VS_EMA200_M15", "Precio a favor de la EMA200 M15"),
    ("hecho_ema50_m15_a_favor", "PRECIO_VS_EMA50_M15", "Precio a favor de la EMA50 M15"),
)
# Hechos del análisis que se conocen al entrar (los demás miden lo que pasó después).
PRE_ENTRY_FACTS = {
    "PRECIO_VS_EMA200_H1",
    "PRECIO_VS_EMA50_H1",
    "PRECIO_VS_EMA200_M15",
    "PRECIO_VS_EMA50_M15",
    "VOLATILIDAD_ATR_H1",
    "SPREAD_ENTRADA",
    "REENTRADA_TRAS_SL",
    "NOTICIA_CERCANA",
    "TIEMPO_ENTRADA",
}
RULE_STATEMENTS = {
    "H_CONTRA_TENDENCIA_H1": "Entrar contra la tendencia H1 (precio del otro lado de la EMA200 H1)",
    "H_CONTRA_TENDENCIA_M15": "Entrar contra el impulso de M15 (precio del otro lado de la "
    "EMA50 M15)",
    "H_A_FAVOR_TENDENCIA_H1": "Entrar a favor de la tendencia H1 (precio del lado de la "
    "EMA200 H1 que favorece la operación)",
    "H_VOLATILIDAD_ALTA": "Entrar con volatilidad alta (ATR H1 en el percentil 80 o más)",
    "H_SPREAD_ALTO": "Entrar con un spread de al menos el 10 % de la distancia al SL",
    "H_REENTRADA_PRECIPITADA": "Reentrar poco después de un SL del mismo bot",
    "H_NOTICIA_CERCANA": "Entrar con una noticia cercana",
}


def search_variables() -> list[pt.Variable]:
    out = [
        pt.Variable(f.name, f.value_type, f.label, f.group)
        for f in FEATURES
        if f.searchable and f.available
    ]
    out.append(pt.Variable("direccion", "categorical", "Dirección", "operacion"))
    out.append(pt.Variable("reentrada", "boolean", "Reentrada tras SL", "operacion"))
    out += [pt.Variable(name, "boolean", label, "analisis") for name, _, label in FACT_VARIABLES]
    return out


def variable_labels() -> dict[str, str]:
    return {v.name: v.label for v in search_variables()}


def search_params(settings: Settings) -> pt.SearchParams:
    return pt.SearchParams(
        min_n_train=settings.patterns_min_n_train,
        min_n_validation=settings.patterns_min_n_validation,
        min_n_oos=settings.patterns_min_n_oos,
        min_n_forward=settings.patterns_min_n_forward,
        fdr_q=settings.patterns_fdr_q,
        quantile_bins=settings.patterns_quantile_bins,
        max_pairs=settings.patterns_max_pairs,
    )


# Reglas de la fase 6 ------------------------------------------------------------------------


def rule_testable(rule: Rule) -> bool:
    return all(c.fact in PRE_ENTRY_FACTS for c in rule.conditions) and len(rule.outcomes) == 1


def rule_kind(rule: Rule) -> str:
    return pt.TRAP if rule.outcomes[0] == Outcome.LOSS else pt.EDGE


def rule_spec(rule: Rule) -> dict[str, Any]:
    return {
        "tipo": "regla",
        "regla": rule.code,
        "version": rule.version,
        "texto": RULE_STATEMENTS.get(rule.code, rule.code),
        "condiciones": [c.spec() for c in rule.conditions],
    }


def rule_key(code: str, version: int) -> str:
    return pt.condition_key({"tipo": "regla", "regla": code, "version": version})


@dataclass(frozen=True)
class RuleCondition:
    """Condición de una regla sobre los hechos del análisis vigente de cada operación."""

    rule: Rule

    def known(self, obs: pt.Obs) -> bool:
        if obs.facts is None:
            return False
        return all(c.op == "existe" or c.fact in obs.facts for c in self.rule.conditions)

    def matches(self, obs: pt.Obs) -> bool:
        return obs.facts is not None and all(c.matches(obs.facts) for c in self.rule.conditions)


def condition_for(spec: dict[str, Any]) -> Any:
    """Objeto evaluable (matches/known) de una condition_spec guardada."""
    if spec.get("tipo") == "regla":
        rule = next((r for r in RULES if r.code == spec["regla"]), None)
        if rule is None:
            raise LookupError(f"regla desconocida: {spec['regla']}")
        return RuleCondition(rule)
    return pt.Condition.from_spec(spec)


# Datos ---------------------------------------------------------------------------------------


def scope_dict(version: BotVersion, symbol: str | None) -> dict[str, Any]:
    return {"bot_id": str(version.bot_id), "bot_version_id": str(version.id), "symbol": symbol}


def _closed_with_risk(version_id: uuid.UUID, symbol: str | None):
    conds = [
        Trade.bot_version_id == version_id,
        Trade.status == TradeStatus.CLOSED,
        Trade.net_profit.is_not(None),
        Trade.risk_amount > 0,
    ]
    if symbol:
        conds.append(Trade.symbol == symbol)
    return conds


def count_observations(session: Session, version_id: uuid.UUID, symbol: str | None) -> int:
    """Operaciones que entrarían en la búsqueda (cerradas y con riesgo conocido)."""
    return session.scalar(select(func.count()).where(*_closed_with_risk(version_id, symbol))) or 0


def load_observations(
    session: Session, version_id: uuid.UUID, symbol: str | None, settings: Settings
) -> list[pt.Obs]:
    stmt = (
        select(
            Trade.trade_id,
            Trade.close_time,
            Trade.net_profit,
            Trade.risk_amount,
            Trade.commission,
            Trade.direction,
            Trade.reentry_of_trade_id,
            Trade.entry_price,
            Trade.initial_sl,
            Trade.symbol_point,
            Trade.mae_points,
        )
        .where(*_closed_with_risk(version_id, symbol))
        .order_by(Trade.close_time, Trade.trade_id)
        .limit(settings.stats_max_trades)
    )
    rows = session.execute(stmt).all()
    ids = [r.trade_id for r in rows]
    dna: dict[uuid.UUID, dict[str, Any]] = {}
    pre_entry: dict[uuid.UUID, dict[str, dict[str, Any]]] = {}
    facts: dict[uuid.UUID, dict[str, dict[str, Any]]] = {}
    for start in range(0, len(ids), 5000):
        chunk = ids[start : start + 5000]
        for trade_id, features, previous in session.execute(
            text(
                "SELECT DISTINCT ON (trade_id) trade_id, features,"
                " inputs->'hechos_previos'->'hechos' FROM trade_dna"
                " WHERE trade_id = ANY(:ids) ORDER BY trade_id, dna_version DESC"
            ),
            {"ids": chunk},
        ):
            dna[trade_id] = features
            if previous is not None:
                pre_entry[trade_id] = previous
        latest = dict(
            session.execute(
                text(
                    "SELECT DISTINCT ON (trade_id) id, trade_id FROM trade_analyses"
                    " WHERE trade_id = ANY(:ids) ORDER BY trade_id, analysis_version DESC"
                ),
                {"ids": chunk},
            ).all()
        )
        for trade_id in latest.values():
            facts.setdefault(trade_id, {})
        if latest:
            for analysis_id, code, evidence in session.execute(
                text(
                    "SELECT analysis_id, code, evidence FROM analysis_findings"
                    " WHERE analysis_id = ANY(:a) AND kind = 'FACT' AND code = ANY(:codes)"
                ),
                {"a": list(latest), "codes": sorted(PRE_ENTRY_FACTS)},
            ):
                facts[latest[analysis_id]][code] = evidence
    searchable = [f.name for f in FEATURES if f.searchable and f.available]
    out = []
    for r in rows:
        net, risk = float(r.net_profit), float(r.risk_amount)
        features = dna.get(r.trade_id, {})
        values: dict[str, Any] = {name: features.get(name) for name in searchable}
        values["direccion"] = r.direction.value
        values["reentrada"] = r.reentry_of_trade_id is not None
        # Hechos del análisis (al cerrar); sin análisis, los previos a la entrada del DNA
        # (supervisor.analytics.pre_entry), con la misma forma.
        trade_facts = facts.get(r.trade_id, pre_entry.get(r.trade_id))
        for name, code, _ in FACT_VARIABLES:
            evidence = (trade_facts or {}).get(code)
            values[name] = evidence.get("a_favor") if evidence else None
        mae_r = None
        if r.mae_points is not None and r.initial_sl is not None and r.symbol_point:
            sl_points = abs(float(r.entry_price) - float(r.initial_sl)) / float(r.symbol_point)
            if sl_points > 0:
                mae_r = float(r.mae_points) / sl_points
        outcome = classify_outcome(net, risk, r.commission, settings.breakeven_r_fraction).outcome
        out.append(
            pt.Obs(
                trade_id=str(r.trade_id),
                close_time=r.close_time,
                r=net / risk,
                win=outcome == Outcome.WIN,
                values=values,
                mae_r=mae_r,
                facts=trade_facts,
            )
        )
    return out


@dataclass(frozen=True)
class Cutoffs:
    train_end: datetime
    validation_end: datetime
    oos_end: datetime


def chronological_cutoffs(obs: list[pt.Obs], settings: Settings) -> Cutoffs | None:
    """Cortes por número de operaciones (ordenadas por cierre). None si no son estrictamente
    crecientes (horas de cierre repetidas justo en el corte)."""
    n = len(obs)
    i_train = math.ceil(n * settings.patterns_train_fraction) - 1
    i_val = (
        math.ceil(n * (settings.patterns_train_fraction + settings.patterns_validation_fraction))
        - 1
    )
    if not 0 <= i_train < i_val < n - 1:
        return None
    cut = Cutoffs(obs[i_train].close_time, obs[i_val].close_time, obs[-1].close_time)
    if not cut.train_end < cut.validation_end < cut.oos_end:
        return None
    return cut


def segment_of(close_time: datetime, split: DataSplit) -> SplitSegment:
    if close_time <= split.train_end:
        return SplitSegment.TRAIN
    if close_time <= split.validation_end:
        return SplitSegment.VALIDATION
    if close_time <= split.oos_end:
        return SplitSegment.OOS
    return SplitSegment.FORWARD


def segments(obs: list[pt.Obs], split: DataSplit) -> dict[SplitSegment, list[pt.Obs]]:
    out: dict[SplitSegment, list[pt.Obs]] = {s: [] for s in SplitSegment}
    for o in obs:
        out[segment_of(o.close_time, split)].append(o)
    return out


def latest_split(session: Session, scope: dict[str, Any]) -> DataSplit | None:
    return session.scalar(
        select(DataSplit)
        .where(DataSplit.scope == scope)
        .order_by(DataSplit.frozen_at.desc(), DataSplit.oos_end.desc())
        .limit(1)
    )


# Ejecución -----------------------------------------------------------------------------------


@dataclass
class RunReport:
    status: str  # COMPLETADA, MUESTRA_INSUFICIENTE, SIN_DATOS_NUEVOS
    n_trades: int
    message: str
    run: PatternRun | None = None
    split: DataSplit | None = None
    counts: Counter = field(default_factory=Counter)


@dataclass
class _Recorder:
    """Acumula lo que una ejecución va a guardar. pattern_runs es de solo inserción y las
    hipótesis y pruebas la referencian: primero se decide todo y al final se inserta la
    ejecución con sus números, después las hipótesis nuevas y después las pruebas."""

    run_id: uuid.UUID | None
    hypotheses: list[Hypothesis] = field(default_factory=list)
    tests: list[PatternTest] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)

    def flush(self, session: Session, run: PatternRun | None) -> None:
        if run is not None:
            session.add(run)
            session.flush()
        session.add_all(self.hypotheses)
        session.flush()
        session.add_all(self.tests)
        session.flush()


def _test_row(
    rec: _Recorder,
    hypothesis: Hypothesis,
    split: DataSplit,
    segment: SplitSegment,
    stats: dict[str, Any],
    *,
    passed: bool | None,
    reason: str,
    method: str,
    family: int,
    p_adjusted: float | None = None,
) -> None:
    ci = stats.get("expectancy_r_ic95") or [None, None]
    rec.tests.append(
        PatternTest(
            id=uuid.uuid4(),
            hypothesis_id=hypothesis.id,
            split_id=split.id,
            run_id=rec.run_id,
            segment=segment,
            n=stats["n"],
            win_rate=stats["win_rate"],
            expectancy=stats["expectancy_r"],
            profit_factor=stats["profit_factor_r"],
            ci_low=ci[0],
            ci_high=ci[1],
            p_value=stats.get("p_valor"),
            p_adjusted=p_adjusted,
            tests_in_family=family,
            method=method,
            passed=passed,
            details={**stats, "motivo": reason},
        )
    )


def _set_status(h: Hypothesis, status: HypothesisStatus, note: str) -> None:
    h.status = status
    h.status_note = note


def _check_new(
    rec: _Recorder,
    h: Hypothesis,
    condition: Any,
    split: DataSplit,
    parts: dict[SplitSegment, list[pt.Obs]],
    params: pt.SearchParams,
) -> None:
    """Validación y, si pasa, el único test fuera de muestra de una hipótesis que acaba de
    pasar entrenamiento."""
    stats, _ = pt.evaluate(condition, parts[SplitSegment.VALIDATION])
    ok, reason = pt.segment_check(h.kind, stats, params.min_n_validation)
    _test_row(
        rec,
        h,
        split,
        SplitSegment.VALIDATION,
        stats,
        passed=ok,
        reason=reason,
        method=CHECK_METHOD,
        family=1,
    )
    if not ok:
        _set_status(h, HypothesisStatus.REJECTED, f"validación: {reason}")
        rec.counts["rechazadas"] += 1
        return
    rec.counts["pasan_validacion"] += 1
    _set_status(h, HypothesisStatus.TESTING, f"validación superada: {reason}")
    _check_oos(rec, h, condition, split, parts[SplitSegment.OOS], params)


def _check_oos(
    rec: _Recorder,
    h: Hypothesis,
    condition: Any,
    split: DataSplit,
    oos: list[pt.Obs],
    params: pt.SearchParams,
    forward_included: bool = False,
) -> None:
    """Evalúa UNA vez fuera de muestra. Si la condición no llega a la muestra mínima no se
    registra nada y queda TESTING esperando (se mira con fuera de muestra + forward)."""
    stats, _ = pt.evaluate(condition, oos)
    if stats["n"] < params.min_n_oos:
        h.status_note = (
            f"validación superada; esperando muestra fuera de muestra: n={stats['n']} "
            f"(mínimo {params.min_n_oos})"
        )
        rec.counts["esperando_oos"] += 1
        return
    ok, reason = pt.segment_check(h.kind, stats, params.min_n_oos)
    _test_row(
        rec,
        h,
        split,
        SplitSegment.OOS,
        stats,
        passed=ok,
        reason=reason,
        method=CHECK_METHOD + ("+forward" if forward_included else ""),
        family=1,
    )
    h.forward_from = max([split.oos_end, *(o.close_time for o in oos)])
    if ok:
        _set_status(h, HypothesisStatus.VALIDATED, f"fuera de muestra: {reason}")
        rec.counts["validadas"] += 1
    else:
        _set_status(h, HypothesisStatus.REJECTED, f"fuera de muestra: {reason}")
        rec.counts["rechazadas"] += 1


def _new_hypothesis(
    rec: _Recorder,
    *,
    scope: dict[str, Any],
    version: BotVersion,
    symbol: str | None,
    split: DataSplit | None,
    spec: dict[str, Any],
    key: str,
    kind: str,
    origin: str,
    statement: str,
    note: str,
) -> Hypothesis:
    h = Hypothesis(
        id=uuid.uuid4(),
        statement=statement,
        condition_spec=spec,
        scope=scope,
        status=HypothesisStatus.PROPOSED,
        kind=kind,
        origin=origin,
        bot_version_id=version.id,
        symbol=symbol,
        condition_key=key,
        split_id=split.id if split else None,
        run_id=rec.run_id,
        status_note=note,
    )
    rec.hypotheses.append(h)
    return h


def _live(session: Session, version_id: uuid.UUID, symbol: str | None) -> dict[str, Hypothesis]:
    stmt = select(Hypothesis).where(
        Hypothesis.bot_version_id == version_id,
        symbol_filter(Hypothesis.symbol, symbol),
        Hypothesis.status.in_(LIVE),
    )
    return {h.condition_key: h for h in session.scalars(stmt)}


def _summary_item(c: pt.CandidateStats, labels: dict[str, str]) -> dict[str, Any]:
    return {
        "condicion": pt.describe(c.condition.spec(), labels),
        "spec": c.condition.spec(),
        "n": c.n,
        "win_rate": c.stats["win_rate"],
        "win_rate_ic95": c.stats["win_rate_ic95"],
        "expectancy_r": c.stats["expectancy_r"],
        "expectancy_r_ic95": c.stats["expectancy_r_ic95"],
        "profit_factor_r": c.stats["profit_factor_r"],
        "expectancy_r_complemento": c.stats["expectancy_r_complemento"],
        "diferencia_r": c.stats["diferencia_r"],
        "p_valor": c.welch.p_value,
        "p_ajustado": c.p_adjusted,
        "mae_r": c.stats["mae_r"],
        "mae_r_complemento": c.stats["mae_r_complemento"],
    }


def run_patterns(
    session: Session,
    settings: Settings,
    version_id: uuid.UUID,
    symbol: str | None = None,
    now: datetime | None = None,
) -> RunReport:
    """Una pasada de la búsqueda en un alcance. Idempotente: sin operaciones nuevas no crea
    split ni vuelve a probar nada (solo el OOS pendiente y el forward de las validadas)."""
    now = now or datetime.now(UTC)
    version = session.get(BotVersion, version_id)
    if version is None:
        raise LookupError(f"no existe la versión {version_id}")
    params = search_params(settings)
    scope = scope_dict(version, symbol)
    obs = load_observations(session, version_id, symbol, settings)
    split = latest_split(session, scope)
    last_close = obs[-1].close_time if obs else None

    if split is None:
        if len(obs) < settings.patterns_min_trades:
            return _insufficient(session, version, symbol, obs, settings, now, last_close)
    elif sum(1 for o in obs if o.close_time > split.oos_end) < settings.patterns_new_trades:
        rec = _Recorder(None)
        _maintain(session, rec, version, symbol, obs, params)
        rec.flush(session, None)
        return RunReport(
            "SIN_DATOS_NUEVOS",
            len(obs),
            "sin operaciones nuevas suficientes desde el último split (hacen falta "
            f"{settings.patterns_new_trades}): no se repite la búsqueda ni se vuelve a mirar "
            "el fuera de muestra; solo se comprueba el forward",
            split=split,
            counts=rec.counts,
        )

    cut = chronological_cutoffs(obs, settings)
    if cut is None:
        return _insufficient(session, version, symbol, obs, settings, now, last_close)
    split = DataSplit(
        id=uuid.uuid4(),
        scope=scope,
        train_end=cut.train_end,
        validation_end=cut.validation_end,
        oos_end=cut.oos_end,
        notes=(
            f"{len(obs)} operaciones cerradas con riesgo; cortes cronológicos "
            f"{settings.patterns_train_fraction:.0%} / {settings.patterns_validation_fraction:.0%}"
            " / resto por hora de cierre"
        ),
    )
    session.add(split)
    session.flush()
    parts = segments(obs, split)
    train = parts[SplitSegment.TRAIN]
    labels = variable_labels()
    rec = _Recorder(uuid.uuid4())
    live = _live(session, version.id, symbol)

    # 1. Búsqueda en entrenamiento.
    result = pt.search(train, search_variables(), params)
    pending: list[tuple[Hypothesis, Any]] = []
    for cand in result.candidates:
        if cand.kind is None:
            continue
        key = cand.condition.key()
        if key in live:
            rec.counts["ya_existentes"] += 1
            continue
        reason = (
            f"{pt.KIND_TEXT[cand.kind]} en entrenamiento: expectativa "
            f"{cand.stats['expectancy_r']:+.2f} R frente a "
            f"{cand.stats['expectancy_r_complemento']:+.2f} R del complemento, "
            f"p ajustado (BH, {result.tested} candidatas) {cand.p_adjusted:.4f} <= {params.fdr_q}"
        )
        h = _new_hypothesis(
            rec,
            scope=scope,
            version=version,
            symbol=symbol,
            split=split,
            spec=cand.condition.spec(),
            key=key,
            kind=cand.kind,
            origin="BUSQUEDA",
            statement=pt.describe(cand.condition.spec(), labels),
            note=reason,
        )
        _test_row(
            rec,
            h,
            split,
            SplitSegment.TRAIN,
            cand.stats,
            passed=True,
            reason=reason,
            method=TRAIN_METHOD,
            family=result.tested,
            p_adjusted=cand.p_adjusted,
        )
        rec.counts["pasan_entrenamiento"] += 1
        pending.append((h, cand.condition))

    # 2. Reglas de la fase 6 (familia propia para el FDR).
    rows = []
    for rule in RULES:
        if not rule_testable(rule):
            continue
        key = rule_key(rule.code, rule.version)
        existing = live.get(key)
        if existing is not None and existing.split_id is not None:
            continue  # ya se probó en su split
        cond = RuleCondition(rule)
        stats, welch = pt.evaluate(cond, train)
        enough = (
            welch is not None
            and stats["n"] >= params.min_n_train
            and stats["n_complemento"] >= params.min_n_train
        )
        rows.append((rule, existing, cond, stats, welch if enough else None))
    tested = [row for row in rows if row[4] is not None]
    adjusted = pt.benjamini_hochberg([row[4].p_value for row in tested])
    p_by_rule = {row[0].code: p for row, p in zip(tested, adjusted, strict=True)}
    for rule, existing, cond, stats, welch in rows:
        kind, spec = rule_kind(rule), rule_spec(rule)
        if welch is None:
            note = (
                f"muestra insuficiente en entrenamiento: n={stats['n']}, complemento "
                f"{stats['n_complemento']} (mínimo {params.min_n_train}); se probará con más "
                "datos"
            )
            if existing is None:
                _new_hypothesis(
                    rec,
                    scope=scope,
                    version=version,
                    symbol=symbol,
                    split=None,
                    spec=spec,
                    key=rule_key(rule.code, rule.version),
                    kind=kind,
                    origin="REGLA",
                    statement=spec["texto"],
                    note=note,
                )
            else:
                existing.status_note = note
            rec.counts["reglas_sin_muestra"] += 1
            continue
        p_adj = p_by_rule[rule.code]
        passed = pt.train_kind(stats, p_adj, params.fdr_q) == kind
        reason = (
            f"regla como {pt.KIND_TEXT[kind]}: expectativa {stats['expectancy_r']:+.2f} R frente "
            f"a {stats['expectancy_r_complemento']:+.2f} R, p ajustado (BH, {len(tested)} "
            f"reglas) {p_adj:.4f}"
        )
        if existing is None:
            h = _new_hypothesis(
                rec,
                scope=scope,
                version=version,
                symbol=symbol,
                split=split,
                spec=spec,
                key=rule_key(rule.code, rule.version),
                kind=kind,
                origin="REGLA",
                statement=spec["texto"],
                note=reason,
            )
        else:
            h = existing
            h.split_id = split.id  # run_id no: la ejecución se inserta al final
        _test_row(
            rec,
            h,
            split,
            SplitSegment.TRAIN,
            stats,
            passed=passed,
            reason=reason,
            method=TRAIN_METHOD,
            family=len(tested),
            p_adjusted=p_adj,
        )
        rec.counts["reglas_probadas"] += 1
        if passed:
            pending.append((h, cond))
        else:
            _set_status(h, HypothesisStatus.REJECTED, f"entrenamiento: {reason}")
            rec.counts["rechazadas"] += 1

    # 3. Validación y fuera de muestra (una vez) de lo que pasó entrenamiento.
    for h, condition in pending:
        _check_new(rec, h, condition, split, parts, params)

    # 4. Hipótesis de splits anteriores: OOS pendiente y forward.
    _maintain(session, rec, version, symbol, obs, params, skip_split=split.id)

    counts = rec.counts
    run = PatternRun(
        id=rec.run_id,
        bot_version_id=version.id,
        symbol=symbol,
        split_id=split.id,
        status=COMPLETED,
        n_trades=len(obs),
        n_train=len(train),
        n_validation=len(parts[SplitSegment.VALIDATION]),
        n_oos=len(parts[SplitSegment.OOS]),
        n_forward=len(parts[SplitSegment.FORWARD]),
        candidates_generated=result.generated,
        candidates_tested=result.tested,
        candidates_low_sample=result.low_sample,
        passed_train=counts["pasan_entrenamiento"],
        passed_validation=counts["pasan_validacion"],
        validated=counts["validadas"],
        rejected=counts["rechazadas"],
        last_close_time=last_close,
        params={
            **params.as_dict(),
            "fraccion_entrenamiento": settings.patterns_train_fraction,
            "fraccion_validacion": settings.patterns_validation_fraction,
        },
        summary={
            "trampas_candidatas": [
                _summary_item(c, labels) for c in pt.top(result.candidates, pt.TRAP, 10)
            ],
            "ventajas_candidatas": [
                _summary_item(c, labels) for c in pt.top(result.candidates, pt.EDGE, 10)
            ],
            "pares_truncados": result.pairs_truncated,
            "recuentos": dict(counts),
            "familia_reglas": len(tested),
        },
        started_at=now,
    )
    rec.flush(session, run)
    return RunReport(
        COMPLETED,
        len(obs),
        f"{result.tested} candidatas probadas ({result.generated} generadas, "
        f"{result.low_sample} sin muestra mínima); {counts['pasan_entrenamiento']} pasan "
        f"entrenamiento, {counts['pasan_validacion']} validación y {counts['validadas']} "
        "fuera de muestra",
        run=run,
        split=split,
        counts=counts,
    )


def _insufficient(
    session: Session,
    version: BotVersion,
    symbol: str | None,
    obs: list[pt.Obs],
    settings: Settings,
    now: datetime,
    last_close: datetime | None,
) -> RunReport:
    """Sin datos para un split: se registra (una vez por recuento de operaciones) y se
    devuelve el aviso."""
    message = insufficient_message(len(obs), settings)
    previous = session.scalar(
        select(PatternRun)
        .where(PatternRun.bot_version_id == version.id, symbol_filter(PatternRun.symbol, symbol))
        .order_by(PatternRun.finished_at.desc())
        .limit(1)
    )
    run = previous
    if previous is None or previous.n_trades != len(obs) or previous.status != INSUFFICIENT:
        run = PatternRun(
            id=uuid.uuid4(),
            bot_version_id=version.id,
            symbol=symbol,
            status=INSUFFICIENT,
            n_trades=len(obs),
            last_close_time=last_close,
            params=search_params(settings).as_dict(),
            summary={"mensaje": message},
            started_at=now,
        )
        session.add(run)
        session.flush()
    return RunReport(INSUFFICIENT, len(obs), message, run=run)


def insufficient_message(n: int, settings: Settings) -> str:
    return (
        f"Con {n} operaciones cerradas todavía no se puede validar ninguna trampa; se necesitan "
        f"al menos {settings.patterns_min_trades} (con riesgo conocido) para separar "
        "entrenamiento, validación y fuera de muestra."
    )


def symbol_filter(column, symbol: str | None):
    return column == symbol if symbol else column.is_(None)


def _maintain(
    session: Session,
    rec: _Recorder,
    version: BotVersion,
    symbol: str | None,
    obs: list[pt.Obs],
    params: pt.SearchParams,
    skip_split: uuid.UUID | None = None,
) -> None:
    """Hipótesis de splits anteriores: fuera de muestra pendiente (una sola vez) y forward de
    las validadas."""
    stmt = (
        select(Hypothesis)
        .where(
            Hypothesis.bot_version_id == version.id,
            symbol_filter(Hypothesis.symbol, symbol),
            Hypothesis.status.in_((HypothesisStatus.TESTING, HypothesisStatus.VALIDATED)),
            Hypothesis.split_id.is_not(None),
        )
        .order_by(Hypothesis.created_at, Hypothesis.id)
    )
    for h in session.scalars(stmt):
        if h.split_id == skip_split:
            continue
        split = session.get(DataSplit, h.split_id)
        condition = condition_for(h.condition_spec)
        if h.status == HypothesisStatus.TESTING:
            done = session.scalar(
                select(PatternTest.id).where(
                    PatternTest.hypothesis_id == h.id, PatternTest.segment == SplitSegment.OOS
                )
            )
            if done is None:
                later = [o for o in obs if o.close_time > split.validation_end]
                _check_oos(rec, h, condition, split, later, params, forward_included=True)
            continue
        _forward(session, rec, h, condition, split, obs, params)


def _forward(
    session: Session,
    rec: _Recorder,
    h: Hypothesis,
    condition: Any,
    split: DataSplit,
    obs: list[pt.Obs],
    params: pt.SearchParams,
) -> None:
    since = h.forward_from or split.oos_end
    stats, _ = pt.evaluate(condition, [o for o in obs if o.close_time > since])
    last = session.scalar(
        select(PatternTest.n)
        .where(PatternTest.hypothesis_id == h.id, PatternTest.segment == SplitSegment.FORWARD)
        .order_by(PatternTest.run_at.desc(), PatternTest.n.desc())
        .limit(1)
    )
    if stats["n"] == 0 or last == stats["n"]:
        return  # nada nuevo que comprobar
    decayed, reason = pt.forward_decayed(h.kind, stats, params.min_n_forward)
    _test_row(
        rec,
        h,
        split,
        SplitSegment.FORWARD,
        stats,
        passed=not decayed,
        reason=reason,
        method=CHECK_METHOD,
        family=1,
    )
    rec.counts["forward"] += 1
    if decayed:
        _set_status(h, HypothesisStatus.DECAYED, f"forward: {reason}")
        rec.counts["caducadas"] += 1
    else:
        h.status_note = f"validada fuera de muestra; {reason}"


# Worker --------------------------------------------------------------------------------------


def versions_with_trades(session: Session, limit: int) -> list[uuid.UUID]:
    """Versiones de bot con operaciones cerradas, la de cierre más reciente primero."""
    rows = session.execute(
        select(Trade.bot_version_id)
        .where(Trade.status == TradeStatus.CLOSED, Trade.bot_version_id.is_not(None))
        .group_by(Trade.bot_version_id)
        .order_by(text("max(trades.close_time) DESC"))
        .limit(limit)
    ).all()
    return [r[0] for r in rows]


def patterns_pass(session_factory: sessionmaker[Session], settings: Settings) -> Counter:
    """Trabajo periódico del worker: repasa como mucho patterns_max_scopes_per_pass versiones
    (cada una en su transacción)."""
    stats: Counter = Counter()
    with session_factory() as session:
        versions = versions_with_trades(session, settings.patterns_max_scopes_per_pass)
    for version_id in versions:
        try:
            with session_factory() as session, session.begin():
                report = run_patterns(session, settings, version_id)
                stats[report.status] += 1
                stats["validadas"] += report.counts["validadas"]
                stats["caducadas"] += report.counts["caducadas"]
        except Exception:
            stats["error"] += 1
            log.exception("búsqueda de patrones fallida", extra={"version": str(version_id)})
    if stats:
        log.info("repaso de patrones", extra=dict(stats))
    return stats
