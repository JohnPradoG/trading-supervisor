"""Laboratorio de estrategias (fase 10): experimentos, filtro contrafactual, backtests del
Strategy Tester y comparación de versiones (secciones 12 y 13).

- Un experimento documenta un cambio sobre una versión base (#001, #002...). Lo que lo define
  no cambia después (trigger en la base de datos); sus resultados son revisiones de solo
  inserción (FILTRO, BACKTEST o MANUAL).
- FILTRO: contrafactual sobre las operaciones reales de la versión base (ver
  supervisor.analytics.lab), con el split congelado de la fase 9. El titular es fuera de
  muestra.
- BACKTEST: resultados del Strategy Tester (supervisor.analytics.backtest_report) en
  backtest_runs, siempre con origen BACKTEST. No crean operaciones: nunca se mezclan con las
  estadísticas en vivo.
- Comparación: métricas lado a lado de cada brazo (original, filtrado, versión candidata,
  backtests) o de dos versiones cualesquiera, con avisos de muestra y las trampas de cada una.
"""

import hashlib
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from supervisor.analytics import backtest_report as bt
from supervisor.analytics import lab
from supervisor.analytics import pattern_search as ps
from supervisor.analytics import patterns as pt
from supervisor.analytics.rules import RULES
from supervisor.analytics.statistics import TradeResult
from supervisor.config import Settings
from supervisor.models import (
    BacktestRun,
    Bot,
    BotVersion,
    DataSplit,
    Experiment,
    ExperimentResult,
    Hypothesis,
)
from supervisor.models.enums import ExperimentStatus, HypothesisStatus, TradeSource
from supervisor.services import patterns as patterns_svc
from supervisor.services import stats as stats_svc
from supervisor.services.errors import Conflict, NotFound, ServiceError

STATUS_TEXT = {
    ExperimentStatus.DRAFT: "borrador",
    ExperimentStatus.RUNNING: "en curso",
    ExperimentStatus.COMPLETED: "terminado",
    ExperimentStatus.ABANDONED: "abandonado",
}
KIND_TEXT = {"FILTRO": "filtro contrafactual", "BACKTEST": "backtest", "MANUAL": "nota"}
WARNING_BACKTEST = (
    "Backtest del Strategy Tester: no son operaciones reales (sin deslizamiento ni spread real "
    "garantizados) y nunca se mezclan con las estadísticas en vivo."
)
COMPARISON_NOTE = (
    "Una diferencia entre brazos solo es orientativa si sus intervalos al 95 % se solapan o "
    "alguno tiene muestra pequeña. Lo que mejora dentro de muestra o en un backtest no es una "
    "ventaja demostrada: la prueba es fuera de muestra y, después, en forward."
)
_LOCK_KEY = 0x7E5701  # pg_advisory_xact_lock para numerar experimentos sin carreras


def _jsonable(value: Any) -> Any:
    return jsonable_encoder(value)


def _labels() -> dict[str, str]:
    return ps.variable_labels()


def _rule_spec(code: str) -> dict[str, Any] | None:
    rule = next((r for r in RULES if r.code == code), None)
    if rule is None or not ps.rule_testable(rule):
        return None
    return ps.rule_spec(rule)


def _version(session: Session, version_id: uuid.UUID, what: str = "versión") -> BotVersion:
    version = session.get(BotVersion, version_id)
    if version is None:
        raise NotFound(f"{what} de bot no encontrada")
    return version


def version_label(session: Session, version_id: uuid.UUID | None) -> str | None:
    if version_id is None:
        return None
    version = session.get(BotVersion, version_id)
    if version is None:
        return None
    bot = session.get(Bot, version.bot_id)
    return f"{bot.name} v{version.version}"


def get_experiment(session: Session, ref: str | int | uuid.UUID) -> Experiment:
    """Por número (7, "7", "#007") o por id."""
    if isinstance(ref, uuid.UUID):
        exp = session.get(Experiment, ref)
    else:
        text_ref = str(ref).strip().lstrip("#")
        if text_ref.isdigit():
            exp = session.scalar(select(Experiment).where(Experiment.number == int(text_ref)))
        else:
            try:
                exp = session.get(Experiment, uuid.UUID(text_ref))
            except ValueError:
                exp = None
    if exp is None:
        raise NotFound("experimento no encontrado")
    return exp


# Alta ----------------------------------------------------------------------------------------


def filter_from_hypothesis(h: Hypothesis) -> dict[str, Any]:
    """Una trampa se convierte en "saltarse esas entradas"; una ventaja, en "operar solo
    esas"."""
    return {"modo": lab.EXCLUDE if h.kind == pt.TRAP else lab.ONLY, "condicion": h.condition_spec}


def create_experiment(
    session: Session,
    *,
    title: str,
    base_version_id: uuid.UUID,
    change_description: str,
    candidate_version_id: uuid.UUID | None = None,
    hypothesis_id: uuid.UUID | None = None,
    symbol: str | None = None,
    filter_spec: dict[str, Any] | None = None,
    filter_from_hypothesis_: bool = True,
) -> Experiment:
    _version(session, base_version_id, "versión base")
    if candidate_version_id is not None:
        _version(session, candidate_version_id, "versión candidata")
        if candidate_version_id == base_version_id:
            raise ServiceError("la versión candidata debe ser distinta de la base")
    hypothesis = None
    if hypothesis_id is not None:
        hypothesis = session.get(Hypothesis, hypothesis_id)
        if hypothesis is None:
            raise NotFound("hipótesis no encontrada")
        if symbol is None:
            symbol = hypothesis.symbol
    if filter_spec is not None:
        try:
            filter_spec = lab.normalize_filter(filter_spec, ps.search_variables(), _rule_spec)
        except lab.FilterError as exc:
            raise ServiceError(str(exc)) from exc
    elif hypothesis is not None and filter_from_hypothesis_:
        if hypothesis.kind not in (pt.TRAP, pt.EDGE):
            raise ServiceError("la hipótesis no es una trampa ni una ventaja: indica el filtro")
        filter_spec = filter_from_hypothesis(hypothesis)
    if filter_spec is not None:
        ps.condition_for(filter_spec["condicion"])  # evaluable

    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
    number = (session.scalar(select(func.max(Experiment.number))) or 0) + 1
    exp = Experiment(
        number=number,
        code=f"#{number:03d}",
        title=title,
        base_version_id=base_version_id,
        candidate_version_id=candidate_version_id,
        hypothesis_id=hypothesis_id,
        symbol=symbol,
        change_description=change_description,
        filter_spec=filter_spec,
    )
    session.add(exp)
    session.flush()
    return exp


def create_from_trap(session: Session, settings: Settings, hypothesis_id: uuid.UUID) -> Experiment:
    """Experimento a partir de una trampa validada (dashboard): filtro "saltarse esas
    entradas" en su versión y símbolo, con el contrafactual ya calculado (revisión 1)."""
    h = session.get(Hypothesis, hypothesis_id)
    if h is None:
        raise NotFound("trampa no encontrada")
    if h.status != HypothesisStatus.VALIDATED or h.kind != pt.TRAP or h.bot_version_id is None:
        raise ServiceError("solo se puede crear un experimento desde una trampa validada")
    label = version_label(session, h.bot_version_id)
    exp = create_experiment(
        session,
        title=f"Filtro: evitar «{h.statement}»"[:200],
        base_version_id=h.bot_version_id,
        change_description=(
            f"Agregar un filtro que no entre cuando: {h.statement}. Trampa validada fuera de "
            f"muestra en {label}{f' ({h.symbol})' if h.symbol else ''}."
        ),
        hypothesis_id=h.id,
        symbol=h.symbol,
    )
    run_filter(session, settings, exp, notes="Creado desde la página Trampas del dashboard.")
    return exp


# Revisiones de resultados ----------------------------------------------------------------------


def _lock(session: Session, exp: Experiment) -> None:
    session.execute(select(Experiment.id).where(Experiment.id == exp.id).with_for_update()).first()


def _next_revision(session: Session, exp: Experiment) -> int:
    current = session.scalar(
        select(func.max(ExperimentResult.revision)).where(ExperimentResult.experiment_id == exp.id)
    )
    return (current or 0) + 1


def _add_result(
    session: Session,
    exp: Experiment,
    kind: str,
    results: dict[str, Any],
    notes: str | None = None,
    backtest_run_id: uuid.UUID | None = None,
) -> ExperimentResult:
    _lock(session, exp)
    row = ExperimentResult(
        experiment_id=exp.id,
        revision=_next_revision(session, exp),
        kind=kind,
        notes=notes,
        results=_jsonable(results),
        backtest_run_id=backtest_run_id,
    )
    session.add(row)
    if exp.status == ExperimentStatus.DRAFT and kind != "MANUAL":
        exp.status = ExperimentStatus.RUNNING
    session.flush()
    return row


def add_manual_result(
    session: Session,
    exp: Experiment,
    *,
    notes: str,
    results: dict[str, Any] | None = None,
    status: ExperimentStatus | None = None,
    conclusion: str | None = None,
) -> ExperimentResult:
    """Revisión MANUAL: una nota, resultados externos o la conclusión. La conclusión vigente
    se guarda también en el experimento; la historia queda en las revisiones."""
    payload: dict[str, Any] = {"tipo": "MANUAL", "datos": results or {}}
    if status is not None:
        payload["estado"] = status.value
    if conclusion is not None:
        payload["conclusion"] = conclusion
    row = _add_result(session, exp, "MANUAL", payload, notes)
    if status is not None:
        exp.status = status
    if conclusion is not None:
        exp.conclusion = conclusion
    session.flush()
    return row


def _split_for(session: Session, exp: Experiment) -> lab.Split | None:
    version = session.get(BotVersion, exp.base_version_id)
    split, origin = None, "ultimo_split"
    if exp.hypothesis_id is not None:
        h = session.get(Hypothesis, exp.hypothesis_id)
        if (
            h is not None
            and h.split_id is not None
            and h.bot_version_id == exp.base_version_id
            and h.symbol == exp.symbol
        ):
            split, origin = session.get(DataSplit, h.split_id), "hipotesis"
    if split is None:
        split = ps.latest_split(session, ps.scope_dict(version, exp.symbol))
    if split is None:
        return None
    return lab.Split(str(split.id), split.validation_end, split.oos_end, split.train_end, origin)


def lab_trades(
    session: Session, settings: Settings, version_id: uuid.UUID, symbol: str | None
) -> tuple[list[lab.LabTrade], int]:
    """Operaciones reales cerradas con riesgo conocido de la versión (las de la fase 9) y
    cuántas cerradas quedan fuera por no tener riesgo."""
    obs = ps.load_observations(session, version_id, symbol, settings)
    rows = stats_svc.load_rows(
        session,
        stats_svc.StatsFilters(version_id=version_id, symbol=symbol),
        settings.stats_max_trades,
    )
    by_id = {r.result.trade_id: r.result for r in rows if r.source != TradeSource.BACKTEST}
    trades = [lab.LabTrade(o, by_id[o.trade_id]) for o in obs if o.trade_id in by_id]
    return trades, len(by_id) - len(trades)


def run_filter(
    session: Session, settings: Settings, exp: Experiment, notes: str | None = None
) -> ExperimentResult:
    """Calcula el filtro contrafactual con los datos de ahora y lo guarda como revisión."""
    if exp.filter_spec is None:
        raise ServiceError(
            "el experimento no tiene filtro estructurado: el contrafactual necesita una "
            "condición (o importa un backtest)"
        )
    condition = ps.condition_for(exp.filter_spec["condicion"])
    trades, excluded = lab_trades(session, settings, exp.base_version_id, exp.symbol)
    split = _split_for(session, exp)
    result = lab.counterfactual(
        trades,
        exp.filter_spec,
        condition,
        split,
        stats_svc.params_from(settings),
        settings.lab_bootstrap_samples,
        from_hypothesis=exp.hypothesis_id is not None,
        min_trades_for_split=settings.patterns_min_trades,
    )
    if excluded:
        result["avisos"].append(
            f"{excluded} operaciones cerradas sin riesgo conocido (sin SL) no entran: no se "
            "pueden medir en R ni evaluar igual que en la fase 9"
        )
    result["filtro"] = {**exp.filter_spec, "texto": lab.filter_text(exp.filter_spec, _labels())}
    result["poblacion"] = {
        "version_id": exp.base_version_id,
        "version": version_label(session, exp.base_version_id),
        "symbol": exp.symbol,
        "operaciones": len(trades),
        "cerradas_sin_riesgo": excluded,
    }
    result["parametros"] = {
        "min_sample": settings.stats_min_sample,
        "bootstrap_remuestreos": settings.lab_bootstrap_samples,
        "intervalos": "win rate: Wilson; expectativa: t de Student; profit factor y drawdown "
        "máximo: bootstrap percentil con semilla fija",
    }
    result["calculado_en"] = datetime.now(UTC)
    return _add_result(session, exp, "FILTRO", result, notes)


# Backtests -------------------------------------------------------------------------------------


def _safe_name(name: str | None) -> str | None:
    if not name:
        return None
    base = re.split(r"[\\/]", name)[-1]
    return re.sub(r"[^\w.\- ]", "_", base)[:200] or None


def backtest_results(parsed: bt.ParsedBacktest) -> list[TradeResult]:
    balance = parsed.initial_deposit
    out = []
    for i, t in enumerate(sorted(parsed.trades, key=lambda t: t.close_time)):
        duration = (
            int((t.close_time - t.entry_time).total_seconds()) if t.entry_time is not None else None
        )
        out.append(
            TradeResult(
                trade_id=f"bt{i:06d}",
                close_time=t.close_time,
                net=t.net,
                balance=balance,
                duration_seconds=duration,
                account_id="backtest",
            )
        )
        if balance is not None:
            balance += t.net
    return out


def import_backtest(
    session: Session,
    settings: Settings,
    exp: Experiment,
    data: bytes,
    *,
    filename: str | None = None,
    version_id: uuid.UUID | None = None,
    label: str | None = None,
    symbol: str | None = None,
) -> tuple[BacktestRun, ExperimentResult]:
    if len(data) > settings.lab_upload_max_bytes:
        raise ServiceError(f"archivo demasiado grande (máximo {settings.lab_upload_max_bytes} B)")
    version_id = version_id or exp.base_version_id
    _version(session, version_id)
    digest = hashlib.sha256(data).hexdigest()
    previous = session.scalar(
        select(BacktestRun).where(
            BacktestRun.experiment_id == exp.id, BacktestRun.file_sha256 == digest
        )
    )
    if previous is not None:
        raise Conflict(
            f"ese archivo ya se importó en este experimento (backtest {previous.id}); no se duplica"
        )
    try:
        parsed = bt.parse_backtest(data)
    except bt.BacktestFormatError as exc:
        raise ServiceError(f"no se pudo leer el archivo del probador: {exc}") from exc
    symbols = parsed.symbols
    symbol = symbol or parsed.summary.get("simbolo") or ",".join(symbols) or "desconocido"
    if label is None:
        label = (
            "base"
            if version_id == exp.base_version_id
            else "candidata"
            if version_id == exp.candidate_version_id
            else "backtest"
        )
    start, end = parsed.period()
    results = backtest_results(parsed)
    metrics = lab.summary_metrics(results, stats_svc.params_from(settings), 0)
    metrics.update(
        lab.bootstrap_intervals(
            results,
            stats_svc.params_from(settings),
            settings.lab_bootstrap_samples,
            int(digest[:16], 16),
        )
    )
    warnings = [WARNING_BACKTEST, *parsed.warnings]
    if len(symbols) > 1:
        warnings.append(f"el archivo tiene varios símbolos: {', '.join(symbols)}")
    run = BacktestRun(
        experiment_id=exp.id,
        bot_version_id=version_id,
        source=TradeSource.BACKTEST,
        label=label[:64],
        symbol=symbol[:64],
        period_start=start,
        period_end=end,
        model=(parsed.summary.get("modelo") or "desconocido")[:64],
        initial_deposit=parsed.initial_deposit,
        report_file=_safe_name(filename),
        report_format=parsed.format,
        file_sha256=digest,
        metrics=_jsonable(metrics),
        trades=_jsonable(
            {
                "operaciones": [
                    {
                        "entrada": t.entry_time,
                        "cierre": t.close_time,
                        "simbolo": t.symbol,
                        "direccion": t.direction,
                        "volumen": t.volume,
                        "neto": t.net,
                    }
                    for t in parsed.trades
                ],
                "deals": len(parsed.deals),
                "informe": parsed.summary,
                "avisos": warnings,
                "horas": "las del servidor del probador, sin convertir",
            }
        ),
    )
    session.add(run)
    session.flush()
    result = _add_result(
        session,
        exp,
        "BACKTEST",
        {
            "tipo": "BACKTEST",
            "backtest_run_id": run.id,
            "etiqueta": run.label,
            "version": version_label(session, version_id),
            "formato": parsed.format,
            "simbolo": run.symbol,
            "periodo": [start, end],
            "deposito_inicial": parsed.initial_deposit,
            "metricas": metrics,
            "informe": parsed.summary,
            "avisos": warnings,
        },
        f"Importado {run.report_file or 'archivo'} ({parsed.format}).",
        run.id,
    )
    return run, result


# Vistas ----------------------------------------------------------------------------------------


def result_view(r: ExperimentResult) -> dict[str, Any]:
    return {
        "revision": r.revision,
        "kind": r.kind,
        "kind_text": KIND_TEXT.get(r.kind, r.kind),
        "notes": r.notes,
        "backtest_run_id": r.backtest_run_id,
        "created_at": r.created_at,
        "results": r.results,
    }


def backtest_view(session: Session, run: BacktestRun) -> dict[str, Any]:
    metrics = run.metrics or {}
    return {
        "id": run.id,
        "label": run.label,
        "source": run.source.value,
        "bot_version_id": run.bot_version_id,
        "version": version_label(session, run.bot_version_id),
        "symbol": run.symbol,
        "period_start": run.period_start,
        "period_end": run.period_end,
        "model": run.model,
        "initial_deposit": run.initial_deposit,
        "report_file": run.report_file,
        "report_format": run.report_format,
        "n_trades": metrics.get("n_trades"),
        "net": metrics.get("cumulative_return"),
        "warnings": (run.trades or {}).get("avisos", []),
        "imported_at": run.imported_at,
    }


def _hypothesis_view(session: Session, hypothesis_id: uuid.UUID | None) -> dict[str, Any] | None:
    if hypothesis_id is None:
        return None
    h = session.get(Hypothesis, hypothesis_id)
    if h is None:
        return None
    return {
        "id": h.id,
        "statement": h.statement,
        "kind": h.kind,
        "status": h.status.value,
        "label": patterns_svc.label(h.status, h.kind),
        "symbol": h.symbol,
    }


def experiment_summary(session: Session, exp: Experiment) -> dict[str, Any]:
    latest = session.scalar(
        select(ExperimentResult)
        .where(ExperimentResult.experiment_id == exp.id, ExperimentResult.kind == "FILTRO")
        .order_by(ExperimentResult.revision.desc())
        .limit(1)
    )
    revisions = session.scalar(select(func.count()).where(ExperimentResult.experiment_id == exp.id))
    headline = None
    if latest is not None:
        res = latest.results
        seg = res["tramos"][res["titular"]]
        headline = {
            "tramo": res["titular"],
            "revision": latest.revision,
            "original": {k: seg["original"].get(k) for k in ("n_trades", "expectancy_r")},
            "filtrado": {k: seg["filtrado"].get(k) for k in ("n_trades", "expectancy_r")},
        }
    return {
        "id": exp.id,
        "number": exp.number,
        "code": exp.code,
        "title": exp.title,
        "status": exp.status.value,
        "status_text": STATUS_TEXT[exp.status],
        "base_version_id": exp.base_version_id,
        "base_version": version_label(session, exp.base_version_id),
        "candidate_version_id": exp.candidate_version_id,
        "candidate_version": version_label(session, exp.candidate_version_id),
        "hypothesis_id": exp.hypothesis_id,
        "symbol": exp.symbol,
        "has_filter": exp.filter_spec is not None,
        "revisions": revisions or 0,
        "headline": headline,
        "created_at": exp.created_at,
        "updated_at": exp.updated_at,
    }


def list_experiments(session: Session, limit: int = 200) -> list[dict[str, Any]]:
    rows = session.scalars(select(Experiment).order_by(Experiment.number.desc()).limit(limit))
    return [experiment_summary(session, e) for e in rows]


def experiment_detail(session: Session, exp: Experiment) -> dict[str, Any]:
    results = session.scalars(
        select(ExperimentResult)
        .where(ExperimentResult.experiment_id == exp.id)
        .order_by(ExperimentResult.revision)
    ).all()
    runs = session.scalars(
        select(BacktestRun)
        .where(BacktestRun.experiment_id == exp.id)
        .order_by(BacktestRun.imported_at, BacktestRun.id)
    ).all()
    return {
        **experiment_summary(session, exp),
        "change_description": exp.change_description,
        "filter": exp.filter_spec,
        "filter_text": lab.filter_text(exp.filter_spec, _labels()) if exp.filter_spec else None,
        "hypothesis": _hypothesis_view(session, exp.hypothesis_id),
        "conclusion": exp.conclusion,
        "results": [result_view(r) for r in results],
        "backtests": [backtest_view(session, r) for r in runs],
    }


# Comparación ----------------------------------------------------------------------------------


def version_traps(session: Session, version_id: uuid.UUID, symbol: str | None) -> dict[str, Any]:
    items = [
        i
        for i in patterns_svc.list_patterns(session, version_id=version_id, kind=pt.TRAP)
        if i["symbol"] == symbol
    ]
    validated = [
        {
            "id": i["id"],
            "statement": i["statement"],
            "fuera_de_muestra": (i["fuera_de_muestra"] or {}).get("expectancy_r"),
        }
        for i in items
        if i["validated"]
    ]
    candidates = sum(
        1 for i in items if i["status"] in (HypothesisStatus.PROPOSED, HypothesisStatus.TESTING)
    )
    return {"validadas": validated, "candidatas": candidates}


def _segment_metrics(
    results_by_segment: dict[str, list[TradeResult]], settings: Settings, salt: str
) -> dict[str, Any]:
    params = stats_svc.params_from(settings)
    return {
        name: lab.summary_metrics(items, params, settings.lab_bootstrap_samples, salt)
        for name, items in results_by_segment.items()
    }


def _arm_warnings(metrics: dict[str, Any]) -> list[str]:
    out = []
    for name, m in metrics.items():
        if m.get("sample_warning"):
            out.append(f"{name.replace('_', ' ')}: {m['sample_warning']}")
    return out


def version_arm(
    session: Session,
    settings: Settings,
    version_id: uuid.UUID,
    symbol: str | None,
    key: str,
    split: lab.Split | None = None,
) -> tuple[dict[str, Any], list[TradeResult]]:
    """Brazo de una versión: todas sus operaciones reales cerradas (no backtest)."""
    _version(session, version_id)
    rows = stats_svc.load_rows(
        session,
        stats_svc.StatsFilters(version_id=version_id, symbol=symbol),
        settings.stats_max_trades,
    )
    results = [r.result for r in rows if r.source != TradeSource.BACKTEST]
    parts: dict[str, list[TradeResult]] = {lab.ALL: results}
    if split is not None:
        parts[lab.IN_SAMPLE] = [r for r in results if r.close_time <= split.validation_end]
        parts[lab.OUT_OF_SAMPLE] = [r for r in results if r.close_time > split.validation_end]
    metrics = _segment_metrics(parts, settings, key)
    arm = {
        "clave": key,
        "etiqueta": version_label(session, version_id),
        "fuente": "REAL",
        "version_id": version_id,
        "symbol": symbol,
        "metricas": metrics,
        "curva": lab.equity_curve(results, settings.lab_curve_points),
        "trampas": version_traps(session, version_id, symbol),
        "avisos": _arm_warnings(metrics),
    }
    return arm, results


def _backtest_arm(session: Session, settings: Settings, run: BacktestRun) -> dict[str, Any]:
    ops = (run.trades or {}).get("operaciones", [])
    results = [
        TradeResult(f"bt{i:06d}", datetime.fromisoformat(o["cierre"]), float(o["neto"]))
        for i, o in enumerate(ops)
    ]
    warnings = list((run.trades or {}).get("avisos", []))
    if (run.metrics or {}).get("sample_warning"):
        warnings.append(f"todo: {run.metrics['sample_warning']}")
    return {
        "clave": f"backtest-{run.id}",
        "etiqueta": f"Backtest {run.label or ''} · {version_label(session, run.bot_version_id)}",
        "fuente": "BACKTEST",
        "version_id": run.bot_version_id,
        "symbol": run.symbol,
        "periodo": [run.period_start, run.period_end],
        "metricas": {lab.ALL: run.metrics},
        "curva": lab.equity_curve(results, settings.lab_curve_points),
        "trampas": None,
        "avisos": warnings,
    }


def experiment_comparison(session: Session, settings: Settings, exp: Experiment) -> dict[str, Any]:
    """Brazos del experimento lado a lado, calculados con los datos de ahora (sin guardar
    nada): original y filtrado (mismas operaciones que el contrafactual), versión candidata
    (operaciones reales) y backtests importados."""
    split = _split_for(session, exp)
    arms: list[dict[str, Any]] = []
    warnings: list[str] = []
    trades, excluded = lab_trades(session, settings, exp.base_version_id, exp.symbol)
    parts = lab.segment_trades(trades, split)
    base_label = version_label(session, exp.base_version_id)
    base_traps = version_traps(session, exp.base_version_id, exp.symbol)

    def arm(key: str, label: str, source: str, chosen: dict[str, list[lab.LabTrade]]) -> dict:
        metrics = _segment_metrics(
            {name: [t.result for t in items] for name, items in chosen.items()}, settings, key
        )
        return {
            "clave": key,
            "etiqueta": label,
            "fuente": source,
            "version_id": exp.base_version_id,
            "symbol": exp.symbol,
            "metricas": metrics,
            "curva": lab.equity_curve(
                [t.result for t in chosen[lab.ALL]], settings.lab_curve_points
            ),
            "trampas": base_traps,
            "avisos": _arm_warnings(metrics),
        }

    arms.append(arm("original", f"Original · {base_label}", "REAL", parts))
    if exp.filter_spec is not None:
        condition = ps.condition_for(exp.filter_spec["condicion"])
        kept = {
            name: lab.apply_filter(items, condition, exp.filter_spec["modo"])[0]
            for name, items in parts.items()
        }
        filtered = arm(
            "filtrado", f"Filtrado (contrafactual) · {base_label}", "CONTRAFACTUAL", kept
        )
        filtered["avisos"].insert(0, lab.WARNING_COUNTERFACTUAL)
        arms.append(filtered)
    if excluded:
        warnings.append(
            f"Original y filtrado usan las {len(trades)} operaciones con riesgo conocido; "
            f"{excluded} cerradas sin SL quedan fuera."
        )
    if exp.candidate_version_id is not None:
        candidate, _ = version_arm(
            session, settings, exp.candidate_version_id, exp.symbol, "candidata"
        )
        candidate["etiqueta"] = f"Candidata · {candidate['etiqueta']}"
        arms.append(candidate)
    runs = session.scalars(
        select(BacktestRun)
        .where(BacktestRun.experiment_id == exp.id)
        .order_by(BacktestRun.imported_at, BacktestRun.id)
    ).all()
    arms += [_backtest_arm(session, settings, r) for r in runs]
    if split is None:
        warnings.append(lab.WARNING_NO_SPLIT.format(min_trades=settings.patterns_min_trades))
    else:
        warnings.append(lab.WARNING_IN_SAMPLE)
    return {
        "experiment_id": exp.id,
        "code": exp.code,
        "title": exp.title,
        "titular": lab.OUT_OF_SAMPLE if split is not None else lab.ALL,
        "split": None
        if split is None
        else {"id": split.id, "origen": split.origin, "validation_end": split.validation_end},
        "brazos": arms,
        "avisos": warnings,
        "nota": COMPARISON_NOTE,
        "calculado_en": datetime.now(UTC),
    }


def compare_two_versions(
    session: Session,
    settings: Settings,
    version_a: uuid.UUID,
    version_b: uuid.UUID,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Sección 13: dos versiones cualesquiera (del mismo bot o no) lado a lado, con sus
    operaciones reales, avisos de muestra, trampas de cada una y la diferencia de expectativa
    en R (Welch) cuando las dos tienen riesgo conocido."""
    if version_a == version_b:
        raise ServiceError("elige dos versiones distintas")
    arm_a, res_a = version_arm(session, settings, version_a, symbol, "a")
    arm_b, res_b = version_arm(session, settings, version_b, symbol, "b")
    r_a = [r.net / r.risk for r in res_a if r.risk]
    r_b = [r.net / r.risk for r in res_b if r.risk]
    welch = pt.welch_test(r_b, r_a)
    difference = None
    if welch is not None:
        difference = {
            "diferencia_r": round(welch.diff, 4),
            "ic95": [round(welch.ci_low, 4), round(welch.ci_high, 4)],
            "p_valor": round(welch.p_value, 6),
            "metodo": "Welch (B - A, en R por operación)",
        }
    small = [
        a["etiqueta"]
        for a in (arm_a, arm_b)
        if a["metricas"][lab.ALL]["n_trades"] < settings.stats_min_sample
    ]
    return {
        "a": arm_a,
        "b": arm_b,
        "diferencia_expectativa": difference,
        "avisos": [f"Muestra pequeña: {', '.join(small)}"] if small else [],
        "nota": COMPARISON_NOTE,
        "calculado_en": datetime.now(UTC),
    }


def versions_for_select(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        select(BotVersion.id, BotVersion.version, Bot.name)
        .join(Bot, Bot.bot_id == BotVersion.bot_id)
        .order_by(Bot.name, BotVersion.released_at, BotVersion.created_at)
    ).all()
    return [{"id": r.id, "label": f"{r.name} v{r.version}"} for r in rows]
