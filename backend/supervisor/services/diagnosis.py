"""Diagnóstico de una versión de bot: qué está fallando, qué cambiar y qué funciona mejor.

Solo lectura frente a MT5: las sugerencias son TEXTO para la persona que edita el EA; nada de
aquí modifica ni controla los bots. Lo único que escribe es el propio diagnóstico
(diagnosis_runs, de solo inserción) y, si se pide desde el dashboard, un experimento del
laboratorio (fase 10).

Datos (las mismas operaciones que la fase 9): cerradas, con riesgo conocido, de una versión de
bot y opcionalmente un símbolo, con su tramo del split congelado de la fase 9. Para la réplica
de salidas se cargan, operación por operación, sus velas M1 desde la entrada hasta el
horizonte (supervisor.analytics.exit_replay); las estadísticas están en
supervisor.analytics.diagnosis.

Caché e historia: antes de calcular nada se obtiene un hash de las entradas con consultas
baratas (operaciones y sus campos, número y versiones de DNA y análisis, recuento y última
vela M1 de la ventana de réplica, split, trampas y ventajas validadas, parámetros y
DIAGNOSIS_VERSION). Si coincide con una ejecución guardada no se recalcula ("sin_cambios"):
(versión, hash) es único en la base. Cada cálculo nuevo es una fila más: la historia queda.

Cuándo: el worker, con la misma cadencia que los patrones (una vez al día, como mucho
patterns_max_scopes_per_pass versiones), si no hay diagnóstico, si cambió el split o la
versión del diagnóstico, o si cerraron patterns_new_trades operaciones nuevas; y a demanda con
`supervisor-cli diagnose`, la API o el botón del dashboard.
"""

import hashlib
import json
import logging
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from supervisor.analytics import diagnosis as dg
from supervisor.analytics import exit_replay as er
from supervisor.analytics import lab
from supervisor.analytics import pattern_search as ps
from supervisor.analytics import patterns as pt
from supervisor.analytics.market import floor_time
from supervisor.analytics.statistics import TradeResult, compute_metrics
from supervisor.config import Settings
from supervisor.models import (
    Account,
    Bot,
    BotVersion,
    DataSplit,
    DiagnosisRun,
    Hypothesis,
    PatternTest,
    PriceBar,
    Trade,
)
from supervisor.models.enums import HypothesisStatus, SplitSegment
from supervisor.services import lab as lab_svc
from supervisor.services import stats as stats_svc
from supervisor.services.errors import NotFound

log = logging.getLogger("supervisor.diagnosis")

DIAGNOSIS_VERSION = "1.0.0"
CREATED, UNCHANGED = "creado", "sin_cambios"
MANUAL, WORKER = "MANUAL", "WORKER"
HISTORY_LIMIT = 50
SEGMENT_OF = {
    SplitSegment.TRAIN: dg.TRAIN,
    SplitSegment.VALIDATION: dg.VALIDATION,
    SplitSegment.OOS: dg.OOS,
    SplitSegment.FORWARD: dg.FORWARD,
}


def diagnosis_params(settings: Settings) -> dg.Params:
    return dg.Params(
        min_n_train=settings.patterns_min_n_train,
        min_n_validation=settings.patterns_min_n_validation,
        min_n_oos=settings.patterns_min_n_oos,
        breakeven_r=settings.breakeven_r_fraction,
        tp_reach_fraction=settings.diagnosis_tp_reach_fraction,
        after_loss_minutes=settings.diagnosis_after_loss_minutes,
        loss_streak=settings.diagnosis_loss_streak,
        min_sample=settings.stats_min_sample,
        curve_points=settings.lab_curve_points,
    )


def replay_params(settings: Settings) -> dict[str, Any]:
    return {
        "horizonte_factor": settings.diagnosis_horizon_factor,
        "horizonte_min_minutos": settings.diagnosis_horizon_min_minutes,
        "horizonte_max_minutos": settings.diagnosis_horizon_max_minutes,
        "max_operaciones": settings.diagnosis_max_replay_trades,
        "cobertura_minima": settings.analysis_min_bar_coverage,
        "ambiguedad": "si una vela toca el stop y el objetivo, primero el stop",
        "spread_ventas": "spread al entrar del DNA (última vela M1 cerrada antes de entrar)",
        "costes": "comisión + swap reales de la operación, en R, sumados a cada variante",
    }


def _hash(data: Any) -> str:
    raw = json.dumps(jsonable_encoder(data), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def get_version(session: Session, version_id: uuid.UUID) -> BotVersion:
    version = session.get(BotVersion, version_id)
    if version is None:
        raise NotFound("versión de bot no encontrada")
    return version


# Carga -----------------------------------------------------------------------------------------


def _load_trades(
    session: Session, version_id: uuid.UUID, symbol: str | None, settings: Settings
) -> list[Any]:
    stmt = (
        select(
            Trade.trade_id,
            Trade.symbol,
            Trade.direction,
            Trade.entry_time,
            Trade.entry_price,
            Trade.initial_sl,
            Trade.initial_tp,
            Trade.close_time,
            Trade.close_price,
            Trade.gross_profit,
            Trade.commission,
            Trade.swap,
            Trade.net_profit,
            Trade.risk_amount,
            Trade.balance_at_entry,
            Trade.duration_seconds,
            Trade.exit_reason,
            Trade.mfe_points,
            Trade.symbol_point,
            Trade.reentry_of_trade_id,
            Trade.account_id,
            Trade.data_quality,
            Account.broker_id,
        )
        .join(Account, Account.id == Trade.account_id)
        .where(*ps._closed_with_risk(version_id, symbol))
        .order_by(Trade.close_time, Trade.trade_id)
        .limit(settings.stats_max_trades)
    )
    return list(session.execute(stmt).all())


def _dna_spreads(session: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, float]:
    out: dict[uuid.UUID, float] = {}
    for start in range(0, len(ids), 5000):
        rows = session.execute(
            text(
                "SELECT DISTINCT ON (trade_id) trade_id, features->'spread_puntos' FROM trade_dna"
                " WHERE trade_id = ANY(:ids) ORDER BY trade_id, dna_version DESC"
            ),
            {"ids": ids[start : start + 5000]},
        )
        for trade_id, spread in rows:
            if isinstance(spread, int | float) and not isinstance(spread, bool):
                out[trade_id] = float(spread)
    return out


def _complex_trades(session: Session, ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Operaciones con cierres parciales o aumentos: la réplica (todo el volumen inicial a la
    vez) no las representa y se excluyen de ella."""
    out: set[uuid.UUID] = set()
    for start in range(0, len(ids), 5000):
        rows = session.execute(
            text(
                "SELECT DISTINCT trade_id FROM trade_events WHERE trade_id = ANY(:ids)"
                " AND event_type IN ('PARTIAL_CLOSE', 'POSITION_INCREASED', 'POSITION_DECREASED')"
            ),
            {"ids": ids[start : start + 5000]},
        )
        out.update(r[0] for r in rows)
    return out


def _f(value: Any) -> float | None:
    return None if value is None else float(value)


def _segment(close_time: datetime, split: DataSplit | None) -> str | None:
    if split is None:
        return None
    return SEGMENT_OF[ps.segment_of(close_time, split)]


def _horizon(row: Any, settings: Settings) -> int:
    return er.horizon_minutes(
        int(row.duration_seconds or 0),
        settings.diagnosis_horizon_factor,
        settings.diagnosis_horizon_min_minutes,
        settings.diagnosis_horizon_max_minutes,
    )


def _bars(session: Session, row: Any, minutes: int) -> list[er.M1Bar]:
    start = floor_time(row.entry_time, 1)
    rows = session.execute(
        select(PriceBar.bar_time, PriceBar.high, PriceBar.low, PriceBar.close, PriceBar.spread)
        .where(
            PriceBar.broker_id == row.broker_id,
            PriceBar.symbol == row.symbol,
            PriceBar.timeframe == "M1",
            PriceBar.bar_time > start,
            PriceBar.bar_time <= start + timedelta(minutes=minutes),
        )
        .order_by(PriceBar.bar_time)
    ).all()
    return [
        er.M1Bar(b.bar_time.astimezone(UTC), float(b.high), float(b.low), float(b.close), b.spread)
        for b in rows
    ]


def _replay_trade(
    row: Any, bars: list[er.M1Bar], spread_points: float | None
) -> tuple[er.ReplayTrade, str | None]:
    entry = float(row.entry_price)
    point = float(row.symbol_point)
    buy = row.direction.value == "BUY"
    note = None
    spread = spread_points
    if spread is None and bars and bars[0].spread is not None:
        spread, note = float(bars[0].spread), "spread de la primera vela de la réplica"
    if spread is None:
        spread, note = 0.0, "sin spread: venta sin corregir"
    tp = _f(row.initial_tp)
    trade = er.ReplayTrade(
        trade_id=str(row.trade_id),
        buy=buy,
        entry_time=row.entry_time.astimezone(UTC),
        entry_price=entry,
        risk_dist=abs(entry - float(row.initial_sl)),
        tp_dist=abs(tp - entry) if tp is not None else None,
        spread_price=0.0 if buy else spread * point,
        cost_r=(float(row.commission or 0) + float(row.swap or 0)) / float(row.risk_amount),
        real_exit=row.exit_reason.value if row.exit_reason else None,
        real_close_time=row.close_time.astimezone(UTC),
        real_close_price=float(row.close_price),
        bars=tuple(bars),
    )
    return trade, (note if not buy else None)


REAL_TO_REPLAY = {"SL": er.SL, "TP": er.TP, "STOP_OUT": er.SL}


def _replay_all(
    session: Session,
    rows: list[Any],
    spreads: dict[uuid.UUID, float],
    complex_ids: set[uuid.UUID],
    split: DataSplit | None,
    settings: Settings,
) -> tuple[list[dg.ExitRow], dict[str, bool | None], dict[str, Any]]:
    """Réplica de todas las variantes por operación (una consulta de velas por operación, las
    más recientes primero hasta el tope) y "SL demasiado ajustado" (¿llegó luego al TP?)."""
    rules = [er.BASELINE, *er.exit_grid()]
    coverage_min = settings.analysis_min_bar_coverage
    reasons: Counter = Counter()
    exit_rows: list[dg.ExitRow] = []
    sl_then_tp: dict[str, bool | None] = {}
    matched = diffs = 0
    abs_diff = 0.0
    notes: Counter = Counter()
    budget = settings.diagnosis_max_replay_trades
    for row in sorted(rows, key=lambda r: r.close_time, reverse=True):
        if budget <= 0:
            reasons["fuera_del_tope"] += 1
            continue
        if not row.symbol_point or row.close_price is None:
            reasons["sin_point_o_cierre"] += 1
            continue
        if row.trade_id in complex_ids:
            reasons["parciales_o_aumentos"] += 1
            continue
        duration_min = (row.duration_seconds or 0) / 60
        if duration_min > settings.diagnosis_horizon_max_minutes:
            reasons["mas_larga_que_el_horizonte"] += 1
            continue
        bars = _bars(session, row, _horizon(row, settings))
        close_minute = floor_time(row.close_time, 1)
        start = floor_time(row.entry_time, 1)
        expected = int((close_minute - start).total_seconds() // 60)
        inside = sum(1 for b in bars if b.time <= close_minute)
        if not bars or (expected > 0 and inside / expected < coverage_min):
            reasons["velas_insuficientes"] += 1
            continue
        budget -= 1
        trade, note = _replay_trade(row, bars, spreads.get(row.trade_id))
        if note:
            notes[note] += 1
        results = {}
        for rule in rules:
            res = er.replay(trade, rule)
            if res is not None:
                results[rule.key] = res
        base = results.get(er.BASELINE.key)
        if base is None:
            reasons["velas_insuficientes"] += 1
            continue
        real = trade.real_exit or "UNKNOWN"
        expected_reason = REAL_TO_REPLAY.get(real, er.EA)
        matched += base.reason == expected_reason
        abs_diff += abs(base.r_price - float(row.gross_profit or 0) / float(row.risk_amount))
        diffs += 1
        if real == "SL" and trade.tp_dist:
            touched = er.reached_after(
                trade, trade.tp_dist / trade.risk_dist, trade.real_close_time
            )
            sl_then_tp[trade.trade_id] = touched is not None
        exit_rows.append(
            dg.ExitRow(
                trade_id=trade.trade_id,
                close_time=row.close_time,
                segment=_segment(row.close_time, split),
                real_r=float(row.net_profit) / float(row.risk_amount),
                results={k: v.r for k, v in results.items()},
            )
        )
    info = {
        "replicadas": len(exit_rows),
        "excluidas": dict(reasons),
        "notas_spread": dict(notes),
        "fidelidad": {
            "motivo_igual_al_real": round(matched / diffs, 4) if diffs else None,
            "diferencia_media_r": round(abs_diff / diffs, 4) if diffs else None,
            "nota": "réplica de las reglas actuales frente a la operación real (R de precio): "
            "motivo de salida y diferencia media. Si la réplica se parece poco a lo real (el EA "
            "gestiona la salida de otra forma), las sugerencias de salida valen menos.",
        },
        "parametros": replay_params(settings),
    }
    return list(reversed(exit_rows)), sl_then_tp, info


# Hash de entradas ------------------------------------------------------------------------------


def _hypotheses(
    session: Session, version_id: uuid.UUID, symbol: str | None, kind: str | None = None
) -> list[Hypothesis]:
    stmt = select(Hypothesis).where(
        Hypothesis.bot_version_id == version_id,
        Hypothesis.status == HypothesisStatus.VALIDATED,
    )
    if symbol:
        stmt = stmt.where((Hypothesis.symbol.is_(None)) | (Hypothesis.symbol == symbol))
    else:
        stmt = stmt.where(Hypothesis.symbol.is_(None))
    if kind:
        stmt = stmt.where(Hypothesis.kind == kind)
    return list(session.scalars(stmt.order_by(Hypothesis.created_at, Hypothesis.id)))


def inputs_fingerprint(
    session: Session,
    settings: Settings,
    version: BotVersion,
    symbol: str | None,
    rows: list[Any],
    split: DataSplit | None,
) -> dict[str, Any]:
    ids = [r.trade_id for r in rows]
    trades_hash = _hash(
        [
            [
                r.trade_id,
                r.entry_time,
                r.close_time,
                r.entry_price,
                r.close_price,
                r.initial_sl,
                r.initial_tp,
                r.net_profit,
                r.risk_amount,
                r.commission,
                r.swap,
                r.exit_reason,
                r.mfe_points,
                r.reentry_of_trade_id,
            ]
            for r in rows
        ]
    )
    dna = session.execute(
        text(
            "SELECT count(*), coalesce(sum(dna_version), 0) FROM trade_dna WHERE trade_id = ANY(:i)"
        ),
        {"i": ids},
    ).one()
    analyses = session.execute(
        text(
            "SELECT count(*), coalesce(sum(analysis_version), 0) FROM trade_analyses"
            " WHERE trade_id = ANY(:i)"
        ),
        {"i": ids},
    ).one()
    bars = []
    horizon = timedelta(minutes=settings.diagnosis_horizon_max_minutes)
    by_market: dict[tuple, list[Any]] = {}
    for r in rows:
        by_market.setdefault((r.broker_id, r.symbol), []).append(r)
    for (broker_id, sym), items in sorted(by_market.items(), key=lambda x: (str(x[0][0]), x[0][1])):
        count, last = session.execute(
            select(func.count(), func.max(PriceBar.bar_time)).where(
                PriceBar.broker_id == broker_id,
                PriceBar.symbol == sym,
                PriceBar.timeframe == "M1",
                PriceBar.bar_time >= min(i.entry_time for i in items),
                PriceBar.bar_time <= max(i.close_time for i in items) + horizon,
            )
        ).one()
        bars.append([str(broker_id), sym, count, last])
    hypotheses = [
        [h.id, h.kind, h.status.value, h.updated_at]
        for h in _hypotheses(session, version.id, symbol)
    ]
    return {
        "diagnostico": DIAGNOSIS_VERSION,
        "version_id": version.id,
        "symbol": symbol,
        "split": split.id if split else None,
        "operaciones": trades_hash,
        "n": len(rows),
        "dna": list(dna),
        "analisis": list(analyses),
        "velas": bars,
        "hipotesis": hypotheses,
        "parametros": diagnosis_params(settings).as_dict(),
        "replica": replay_params(settings),
    }


# Diagnóstico -------------------------------------------------------------------------------------


def _trap_matches(
    session: Session, settings: Settings, version_id: uuid.UUID, symbol: str | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Trampas y ventajas validadas de la fase 9 con las operaciones que cumplen su condición."""
    hypotheses = _hypotheses(session, version_id, symbol)
    if not hypotheses:
        return [], []
    obs = ps.load_observations(session, version_id, symbol, settings)
    tests = {
        t.hypothesis_id: t
        for t in session.scalars(
            select(PatternTest).where(
                PatternTest.hypothesis_id.in_([h.id for h in hypotheses]),
                PatternTest.segment == SplitSegment.OOS,
            )
        )
    }
    traps, edges = [], []
    for h in hypotheses:
        try:
            condition = ps.condition_for(h.condition_spec)
        except LookupError:
            continue
        test = tests.get(h.id)
        details = (test.details or {}) if test else {}
        item = {
            "id": str(h.id),
            "statement": h.statement,
            "trade_ids": [o.trade_id for o in obs if condition.matches(o)],
            "fuera_de_muestra": {
                k: details.get(k)
                for k in ("n", "win_rate", "win_rate_ic95", "expectancy_r", "expectancy_r_ic95")
            }
            if test
            else None,
            "origen": h.origin,
        }
        (traps if h.kind == pt.TRAP else edges).append(item)
    return traps, edges


def _summary(
    rows: list[Any],
    trades: list[dg.DiagTrade],
    split: DataSplit | None,
    params: dg.Params,
    settings: Settings,
    replay: dict[str, Any],
) -> dict[str, Any]:
    results = [
        TradeResult(
            trade_id=str(r.trade_id),
            close_time=r.close_time,
            net=float(r.net_profit),
            risk=float(r.risk_amount),
            commission=_f(r.commission),
            balance=_f(r.balance_at_entry),
            duration_seconds=r.duration_seconds,
            account_id=str(r.account_id),
        )
        for r in rows
    ]
    money = compute_metrics(results, stats_svc.params_from(settings))
    r_metrics = dg.r_stats([t.r for t in trades], params)
    curve = 0.0
    peak = 0.0
    dd_r = 0.0
    for t in sorted(trades, key=lambda x: (x.close_time, x.trade_id)):
        curve += t.r
        peak = max(peak, curve)
        dd_r = max(dd_r, peak - curve)
    segments = Counter(t.segment for t in trades if t.segment)
    if split is None:
        sufficiency = {
            "estado": "insuficiente",
            "mensaje": ps.insufficient_message(len(trades), settings)
            + " Sin split no se puede validar ningún cambio: se muestra lo que falla "
            "(descriptivo) y nada se presenta como resultado.",
        }
    else:
        sufficiency = {
            "estado": "suficiente",
            "mensaje": (
                f"Split congelado de la fase 9: {segments.get(dg.TRAIN, 0)} operaciones de "
                f"entrenamiento (donde se eligen los cambios), {segments.get(dg.VALIDATION, 0)} de "
                f"validación y {segments.get(dg.OOS, 0)} fuera de muestra (el titular); "
                f"{segments.get(dg.FORWARD, 0)} después (forward, solo se muestran). "
                f"{replay['replicadas']} con velas suficientes para la réplica de salidas."
            ),
        }
    sufficiency.update(
        {
            "split": None
            if split is None
            else {
                "id": split.id,
                "train_end": split.train_end,
                "validation_end": split.validation_end,
                "oos_end": split.oos_end,
            },
            "tramos": {dg.SEGMENT_NAMES[k]: v for k, v in segments.items()},
            "replicadas": replay["replicadas"],
        }
    )
    return {
        "n": len(trades),
        "periodo": [rows[0].close_time, rows[-1].close_time] if rows else None,
        "r": r_metrics,
        "win_rate": r_metrics["win_rate"],
        "win_rate_ic95": r_metrics["win_rate_ic95"],
        "expectancy_r": r_metrics["expectancy_r"],
        "expectancy_r_ic95": r_metrics["expectancy_r_ic95"],
        "profit_factor": money["profit_factor"],
        "profit_factor_r": r_metrics["profit_factor_r"],
        "neto": money["cumulative_return"],
        "max_drawdown": money["max_drawdown"],
        "max_drawdown_r": round(dd_r, 4),
        "nota_win_rate": dg.win_rate_note(r_metrics),
        "aviso_muestra": money["sample_warning"],
        "suficiencia": sufficiency,
    }


def build_report(
    session: Session,
    settings: Settings,
    version: BotVersion,
    symbol: str | None,
    rows: list[Any],
    split: DataSplit | None,
) -> dict[str, Any]:
    params = diagnosis_params(settings)
    ids = [r.trade_id for r in rows]
    spreads = _dna_spreads(session, ids)
    complex_ids = _complex_trades(session, ids)
    exit_rows, sl_then_tp, replay = _replay_all(
        session, rows, spreads, complex_ids, split, settings
    )
    trades = []
    for r in rows:
        risk = float(r.risk_amount)
        sl_dist = abs(float(r.entry_price) - float(r.initial_sl))
        point = _f(r.symbol_point)
        mfe_r = tp_r = None
        if point and sl_dist > 0:
            if r.mfe_points is not None:
                mfe_r = float(r.mfe_points) * point / sl_dist
            if r.initial_tp is not None:
                tp_r = abs(float(r.initial_tp) - float(r.entry_price)) / sl_dist
        trades.append(
            dg.DiagTrade(
                trade_id=str(r.trade_id),
                entry_time=r.entry_time.astimezone(UTC),
                close_time=r.close_time,
                r=float(r.net_profit) / risk,
                exit_reason=r.exit_reason.value if r.exit_reason else None,
                mfe_r=mfe_r,
                tp_r=tp_r,
                spread_points=spreads.get(r.trade_id),
                reentry=r.reentry_of_trade_id is not None,
                segment=_segment(r.close_time, split),
                sl_then_tp=sl_then_tp.get(str(r.trade_id)),
            )
        )
    trades = dg.with_sequence(trades, params)
    has_split = split is not None
    exits = dg.exit_suggestions(exit_rows, params, has_split)
    filters = dg.filter_suggestions(trades, params, has_split)
    suggestions = dg.rank_suggestions(exits["sugerencias"] + filters["sugerencias"])
    traps, edges = _trap_matches(session, settings, version.id, symbol)
    failures = dg.failure_modes(trades, params, traps)
    for f in failures:
        related = [s for s in suggestions if s["familia"] in f["familias"]]
        f["cambios"] = [s["clave"] for s in related]
        if f["codigo"].startswith("TRAMPA:"):
            f["estado"] = "trampa validada"
        elif related:
            f["estado"] = min((s["estado"] for s in related), key=lambda s: dg.STATUS_RANK[s])
        else:
            f["estado"] = dg.NO_DATA if not has_split else "sin cambio propuesto"
    validated = [s for s in suggestions if s["estado"] == dg.VALIDATED and not s["no_recomendada"]]
    edge_items = []
    by_id = {t.trade_id: t for t in trades}
    for e in edges:
        group = [by_id[i].r for i in e["trade_ids"] if i in by_id]
        info = {k: v for k, v in e.items() if k != "trade_ids"}
        edge_items.append({**info, "n": len(group), "grupo": dg.r_stats(group, params)})
    report = {
        "bot_id": version.bot_id,
        "bot_name": session.get(Bot, version.bot_id).name,
        "version_id": version.id,
        "version": version.version,
        "symbol": symbol,
        "diagnosis_version": DIAGNOSIS_VERSION,
        "resumen": _summary(rows, trades, split, params, settings, replay),
        "fallos": failures,
        "cambios": suggestions,
        "trampas_win_rate": exits["trampas_win_rate"],
        "funciona": {
            "ventajas_validadas": edge_items,
            "mejores_horarios": dg.best_timing(trades, params),
            "cambios_validados": [
                {"clave": s["clave"], "texto": s["texto"], "preferida": s["preferida"]}
                for s in validated
            ],
        },
        "variantes": {
            "salida": {
                "probadas": exits["variantes_probadas"],
                "familias": exits["familias"],
                "sin_mejora_en_entrenamiento": exits["sin_mejora_en_entrenamiento"],
            },
            "filtros": {"probados": filters["filtros_probados"], "familias": filters["familias"]},
            "total": exits["variantes_probadas"] + filters["filtros_probados"],
        },
        "replica": replay,
        "parametros": params.as_dict(),
        "avisos": [
            "Solo texto para quien edita el EA: el supervisor no modifica ni controla los bots.",
            "Los cambios se eligen solo con el tramo de ENTRENAMIENTO del split congelado de la "
            "fase 9 y se evalúan una vez en validación y fuera de muestra. El titular es fuera "
            "de muestra; entrenamiento es optimista.",
            '"validada" = la mejora de la expectativa por operación tiene un IC95 por encima '
            'de 0 en validación y fuera de muestra con la muestra mínima. "candidata, no '
            'validada" no es un resultado.',
            lab.WARNING_COUNTERFACTUAL,
        ],
    }
    return jsonable_encoder(report)


def compute_diagnosis(
    session: Session,
    settings: Settings,
    version_id: uuid.UUID,
    symbol: str | None = None,
    trigger: str = MANUAL,
) -> tuple[str, DiagnosisRun]:
    """Calcula y guarda el diagnóstico si sus entradas cambiaron; si no, devuelve el último
    con las mismas entradas ("sin_cambios")."""
    version = get_version(session, version_id)
    symbol = (symbol or "").strip()[:64] or None
    rows = _load_trades(session, version_id, symbol, settings)
    split = ps.latest_split(session, ps.scope_dict(version, symbol))
    fingerprint = inputs_fingerprint(session, settings, version, symbol, rows, split)
    digest = _hash(fingerprint)
    previous = session.scalar(
        select(DiagnosisRun).where(
            DiagnosisRun.bot_version_id == version_id, DiagnosisRun.inputs_hash == digest
        )
    )
    if previous is not None:
        return UNCHANGED, previous
    report = build_report(session, settings, version, symbol, rows, split)
    run = DiagnosisRun(
        id=uuid.uuid4(),
        bot_version_id=version_id,
        symbol=symbol,
        split_id=split.id if split else None,
        inputs_hash=digest,
        diagnosis_version=DIAGNOSIS_VERSION,
        n_trades=len(rows),
        last_close_time=rows[-1].close_time if rows else None,
        trigger=trigger,
        params={
            "diagnostico": diagnosis_params(settings).as_dict(),
            "replica": replay_params(settings),
        },
        report=report,
    )
    session.add(run)
    session.flush()
    return CREATED, run


# Consultas ----------------------------------------------------------------------------------------


def _scope_filter(symbol: str | None):
    return ps.symbol_filter(DiagnosisRun.symbol, symbol)


def latest_run(
    session: Session, version_id: uuid.UUID, symbol: str | None = None
) -> DiagnosisRun | None:
    return session.scalar(
        select(DiagnosisRun)
        .where(DiagnosisRun.bot_version_id == version_id, _scope_filter(symbol))
        .order_by(DiagnosisRun.created_at.desc(), DiagnosisRun.id)
        .limit(1)
    )


def run_summary(run: DiagnosisRun) -> dict[str, Any]:
    report = run.report or {}
    summary = report.get("resumen") or {}
    changes = report.get("cambios") or []
    return {
        "id": run.id,
        "created_at": run.created_at,
        "trigger": run.trigger,
        "diagnosis_version": run.diagnosis_version,
        "inputs_hash": run.inputs_hash,
        "n_trades": run.n_trades,
        "last_close_time": run.last_close_time,
        "split_id": run.split_id,
        "expectancy_r": summary.get("expectancy_r"),
        "win_rate": summary.get("win_rate"),
        "validadas": sum(1 for c in changes if c.get("estado") == dg.VALIDATED),
        "fallos": len(report.get("fallos") or []),
    }


def run_view(run: DiagnosisRun) -> dict[str, Any]:
    return {**run_summary(run), "symbol": run.symbol, "report": run.report}


def diagnosis_for(
    session: Session,
    bot_id: uuid.UUID,
    version_id: uuid.UUID,
    symbol: str | None = None,
    limit: int = HISTORY_LIMIT,
) -> dict[str, Any]:
    """El último diagnóstico y la historia (de más reciente a más antiguo)."""
    version = get_version(session, version_id)
    if version.bot_id != bot_id:
        raise NotFound("versión de bot no encontrada")
    runs = list(
        session.scalars(
            select(DiagnosisRun)
            .where(DiagnosisRun.bot_version_id == version_id, _scope_filter(symbol))
            .order_by(DiagnosisRun.created_at.desc(), DiagnosisRun.id)
            .limit(limit)
        )
    )
    return {
        "bot_id": bot_id,
        "version_id": version_id,
        "symbol": symbol,
        "latest": run_view(runs[0]) if runs else None,
        "history": [run_summary(r) for r in runs],
        "message": None
        if runs
        else "Todavía no hay diagnóstico de esta versión: el worker lo calcula una vez al día "
        "(o ahora con supervisor-cli diagnose --version <id>).",
    }


def get_run(session: Session, run_id: uuid.UUID) -> DiagnosisRun:
    run = session.get(DiagnosisRun, run_id)
    if run is None:
        raise NotFound("diagnóstico no encontrado")
    return run


def suggestion(run: DiagnosisRun, key: str) -> dict[str, Any]:
    for item in (run.report or {}).get("cambios", []):
        if item["clave"] == key:
            return item
    raise NotFound("sugerencia no encontrada en ese diagnóstico")


def overview_cards(session: Session, limit: int = 20) -> list[dict[str, Any]]:
    """Resumen: una tarjeta por versión con despliegue activo y diagnóstico (alcance de toda
    la versión): su peor fallo y su mejor sugerencia validada."""
    rows = session.execute(
        text(
            "SELECT DISTINCT ON (r.bot_version_id) r.id FROM diagnosis_runs r"
            " JOIN deployments d ON d.bot_version_id = r.bot_version_id AND d.ended_at IS NULL"
            " WHERE r.symbol IS NULL"
            " ORDER BY r.bot_version_id, r.created_at DESC"
        )
    ).all()
    cards = []
    for (run_id,) in rows[:limit]:
        run = session.get(DiagnosisRun, run_id)
        report = run.report or {}
        failures = report.get("fallos") or []
        validated = [
            c
            for c in report.get("cambios") or []
            if c["estado"] == dg.VALIDATED and not c["no_recomendada"]
        ]
        summary = report.get("resumen") or {}
        cards.append(
            {
                "run_id": run.id,
                "bot_name": report.get("bot_name"),
                "version": report.get("version"),
                "version_id": run.bot_version_id,
                "created_at": run.created_at,
                "n": summary.get("n"),
                "win_rate": summary.get("win_rate"),
                "expectancy_r": summary.get("expectancy_r"),
                "top_fallo": failures[0] if failures else None,
                "top_cambio": validated[0] if validated else None,
            }
        )
    cards.sort(key=lambda c: (c["bot_name"] or "", c["version"] or ""))
    return cards


def scopes_page(session: Session, limit: int = 100) -> list[dict[str, Any]]:
    """Versiones con operaciones cerradas y su último diagnóstico (página Diagnóstico)."""
    versions = ps.versions_with_trades(session, limit)
    out = []
    for version_id in versions:
        version = session.get(BotVersion, version_id)
        bot = session.get(Bot, version.bot_id)
        run = latest_run(session, version_id)
        out.append(
            {
                "bot": bot,
                "version": version,
                "run": run_summary(run) if run else None,
            }
        )
    out.sort(key=lambda x: (x["bot"].name, x["version"].version))
    return out


# Experimento desde una sugerencia -------------------------------------------------------------


def create_experiment_from(
    session: Session, settings: Settings, run_id: uuid.UUID, key: str
) -> Any:
    """ "Crear experimento" (fase 10) desde una sugerencia del diagnóstico. Un filtro que se
    puede expresar como condición de entrada se crea con su filtro y se calcula el
    contrafactual; un cambio de salida (o un filtro secuencial) se crea con la descripción del
    cambio y una revisión MANUAL con los números del diagnóstico."""
    run = get_run(session, run_id)
    item = suggestion(run, key)
    exp_def = item["experimento"]
    exp = lab_svc.create_experiment(
        session,
        title=exp_def["titulo"],
        base_version_id=run.bot_version_id,
        change_description=exp_def["cambio"],
        symbol=run.symbol,
        filter_spec=exp_def["filtro"],
    )
    if exp.filter_spec is not None:
        lab_svc.run_filter(
            session, settings, exp, notes=f"Creado desde el diagnóstico {run.id} ({key})."
        )
    else:
        lab_svc.add_manual_result(
            session,
            exp,
            notes=f"Números del diagnóstico {run.id} ({key}); réplica sobre entradas reales.",
            results={
                "sugerencia": item["clave"],
                "estado": item["estado"],
                "texto": item["texto"],
                "tramos": item["tramos"],
                "avisos": item["avisos"],
            },
        )
    return exp


# Worker -----------------------------------------------------------------------------------------


def needs_run(session: Session, settings: Settings, version_id: uuid.UUID) -> bool:
    """Barato: sin diagnóstico, otra versión del diagnóstico, otro split o suficientes
    operaciones nuevas cerradas desde el último."""
    last = latest_run(session, version_id)
    if last is None or last.diagnosis_version != DIAGNOSIS_VERSION:
        return True
    version = session.get(BotVersion, version_id)
    split = ps.latest_split(session, ps.scope_dict(version, None))
    if (split.id if split else None) != last.split_id:
        return True
    conds = ps._closed_with_risk(version_id, None)
    if last.last_close_time is not None:
        conds.append(Trade.close_time > last.last_close_time)
    new = session.scalar(select(func.count()).where(*conds)) or 0
    return new >= settings.patterns_new_trades


def diagnosis_pass(session_factory: sessionmaker[Session], settings: Settings) -> Counter:
    """Trabajo del worker tras la búsqueda de patrones (misma cadencia y mismo tope de
    versiones por pasada); cada versión en su transacción."""
    stats: Counter = Counter()
    with session_factory() as session:
        versions = ps.versions_with_trades(session, settings.patterns_max_scopes_per_pass)
    for version_id in versions:
        try:
            with session_factory() as session, session.begin():
                if not needs_run(session, settings, version_id):
                    stats["al_dia"] += 1
                    continue
                status, _ = compute_diagnosis(session, settings, version_id, None, WORKER)
                stats[status] += 1
        except Exception:
            stats["error"] += 1
            log.exception("diagnóstico fallido", extra={"version": str(version_id)})
    if stats:
        log.info("repaso de diagnósticos", extra=dict(stats))
    return stats
