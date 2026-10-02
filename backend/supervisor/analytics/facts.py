"""Hechos (FACT) de una operación cerrada, calculados SOLO con los datos presentes en sus
entradas (`inputs`, ver analyzer.collect_inputs). Función pura: mismas entradas, mismos hechos.

Si un dato falta, el hecho no se genera y se añade una nota de calidad (data_quality) que
explica qué falta: nunca se supone un valor. En particular, que no haya noticias registradas
no significa que no hubiera noticias: se anota "sin datos de noticias".

Unidades: "puntos" son múltiplos de `point` del símbolo (como `points` de la operación); "R"
es una distancia o un resultado dividido entre el riesgo inicial (distancia al SL inicial).
Las excursiones vienen de velas M1 de precio Bid: en ventas es una aproximación.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from supervisor.analytics.outcome import OUTCOME_TEXT, classify_outcome
from supervisor.analytics.rules import Finding, evaluate
from supervisor.analytics.sessions import time_info
from supervisor.models.enums import FindingKind, Outcome

EXIT_TEXT = {
    "SL": "por stop loss",
    "TP": "por take profit",
    "EXPERT": "por el propio EA",
    "MANUAL": "manualmente (terminal, móvil o web)",
    "STOP_OUT": "por stop out del broker",
    "MIXED": "en varios cierres con motivos distintos",
    "UNKNOWN": "por un motivo que MT5 no informó",
}


@dataclass
class AnalysisResult:
    outcome: Outcome
    findings: list[Finding]
    data_quality: list[dict[str, str]]

    @property
    def facts(self) -> list[Finding]:
        return [f for f in self.findings if f.kind == FindingKind.FACT]

    @property
    def hypotheses(self) -> list[Finding]:
        return [f for f in self.findings if f.kind == FindingKind.HYPOTHESIS]


def _f(value: Any) -> float | None:
    return None if value is None else float(value)


def _r(value: float, places: int = 4) -> float:
    return round(value, places)


def _money(value: float, currency: str) -> str:
    return f"{value:+.2f} {currency}"


def _num(value: float, places: int = 2) -> str:
    return f"{value:.{places}f}"


class _Builder:
    def __init__(self) -> None:
        self.facts: list[Finding] = []
        self.quality: list[dict[str, str]] = []

    def fact(self, code: str, text: str, **evidence: Any) -> None:
        self.facts.append(Finding(FindingKind.FACT, code, text, evidence))

    def note(self, code: str, text: str) -> None:
        self.quality.append({"code": code, "text": text})


def build_analysis(inputs: dict[str, Any]) -> AnalysisResult:
    op = inputs["operacion"]
    events = inputs["eventos"]
    market = inputs["mercado"]
    params = inputs["parametros"]
    currency = inputs["cuenta"]["moneda"]
    b = _Builder()

    buy = op["direccion"] == "BUY"
    entry = float(op["precio_entrada"])
    entry_time = datetime.fromisoformat(op["entrada"])
    point = _f(op["punto"])
    net = float(op["neto"])
    risk = _f(op["riesgo"])
    sl, tp = _f(op["sl_inicial"]), _f(op["tp_inicial"])
    sl_dist = abs(entry - sl) / point if sl is not None and point else None
    tp_dist = abs(tp - entry) / point if tp is not None and point else None
    if not point:
        b.note("SIN_PUNTO_SIMBOLO", "sin point del símbolo: no se miden distancias en puntos")

    # Resultado -------------------------------------------------------------------------------
    result = classify_outcome(net, risk, _f(op["comision"]), params["breakeven_r_fraction"])
    outcome = result.outcome
    gross, fees, swap = float(op["bruto"]), float(op["comision"]), float(op["swap"])
    b.fact(
        "RESULTADO",
        f"Resultado {OUTCOME_TEXT[outcome]}: neto {_money(net, currency)} (bruto "
        f"{_money(gross, currency)}, comisión {_money(fees, currency)}, swap "
        f"{_money(swap, currency)}).",
        resultado=outcome.value,
        neto=net,
        bruto=gross,
        comision=fees,
        swap=swap,
        moneda=currency,
        tolerancia_breakeven=result.tolerance,
        base_tolerancia=result.basis,
    )
    if risk is not None and risk > 0:
        r_multiple = net / risk
        b.fact(
            "MULTIPLO_R",
            f"Resultado de {r_multiple:+.2f} R (neto {_num(net)} / riesgo inicial {_num(risk)}).",
            r=_r(r_multiple),
            neto=net,
            riesgo=risk,
            riesgo_pct=_f(op["riesgo_pct"]),
        )
    else:
        b.note("SIN_RIESGO", "sin riesgo inicial (abrió sin SL o sin datos del símbolo): no hay R")

    # Tiempo ----------------------------------------------------------------------------------
    seconds = int(op["duracion_s"])
    b.fact(
        "DURACION",
        f"Duró {seconds / 60:.1f} minutos.",
        segundos=seconds,
        minutos=_r(seconds / 60, 2),
    )
    reason = op["motivo_salida"] or "UNKNOWN"
    b.fact(
        "MOTIVO_SALIDA",
        f"Cerró {EXIT_TEXT.get(reason, reason)}.",
        motivo=reason,
    )
    info = time_info(entry_time)
    b.fact(
        "TIEMPO_ENTRADA",
        f"Entró un {info['dia_nombre']} a las {info['hora_utc']:02d} h UTC, sesión "
        f"{info['sesion']}.",
        **info,
    )

    # Excursiones -----------------------------------------------------------------------------
    mfe, mae = _f(op["mfe_puntos"]), _f(op["mae_puntos"])
    excursion = op.get("excursion") or {}
    if mfe is None or mae is None:
        b.note("SIN_EXCURSION", "sin MFE/MAE: no hay velas M1 de la operación")
    else:
        evidence: dict[str, Any] = {
            "mfe_puntos": mfe,
            "mae_puntos": mae,
            "mfe_dinero": _f(op["mfe_dinero"]),
            "mae_dinero": _f(op["mae_dinero"]),
            "cobertura_velas": excursion.get("cobertura"),
        }
        text = f"MFE {_num(mfe, 1)} puntos y MAE {_num(mae, 1)} puntos"
        if sl_dist:
            evidence["mfe_r"] = _r(mfe / sl_dist)
            evidence["mae_r"] = _r(mae / sl_dist)
            text += f" ({mfe / sl_dist:+.2f} R / {mae / sl_dist:+.2f} R)"
        b.fact("EXCURSIONES", text + ".", **evidence)
        if excursion and not excursion.get("completa", True):
            b.note(
                "EXCURSION_INCOMPLETA",
                f"velas M1 incompletas durante la operación (cobertura "
                f"{excursion.get('cobertura', 0):.0%}): MFE/MAE pueden quedarse cortos",
            )
        if tp_dist:
            ratio = mfe / tp_dist
            reached = mfe >= tp_dist
            b.fact(
                "MFE_VS_TP",
                (
                    f"El movimiento favorable máximo ({_num(mfe, 1)} puntos) superó la distancia "
                    f"al TP inicial ({_num(tp_dist, 1)} puntos)."
                    if reached
                    else f"El movimiento favorable máximo llegó al {ratio:.0%} de la distancia "
                    f"al TP inicial ({_num(tp_dist, 1)} puntos)."
                ),
                mfe_puntos=mfe,
                distancia_tp_puntos=_r(tp_dist),
                fraccion=_r(ratio),
                porcentaje=_r(ratio * 100, 1),
                supero=reached,
            )
        if sl_dist:
            ratio = abs(mae) / sl_dist
            b.fact(
                "MAE_VS_SL",
                f"El movimiento adverso máximo llegó al {ratio:.0%} de la distancia al SL "
                f"inicial ({_num(sl_dist, 1)} puntos).",
                mae_puntos=mae,
                distancia_sl_puntos=_r(sl_dist),
                fraccion=_r(ratio),
                porcentaje=_r(ratio * 100, 1),
            )
        points = _f(op["puntos"])
        if mfe > 0 and points is not None:
            capture = points / mfe
            b.fact(
                "CAPTURA_MFE",
                f"Capturó el {capture:.0%} del movimiento favorable máximo "
                f"({_num(points, 1)} de {_num(mfe, 1)} puntos).",
                puntos=points,
                mfe_puntos=mfe,
                captura=_r(capture),
                porcentaje=_r(capture * 100, 1),
            )
    if sl is None:
        b.note("SIN_SL", "abrió sin SL: no se compara el MAE con el SL")
    if tp is None:
        b.note("SIN_TP", "abrió sin TP: no se compara el MFE con el TP")

    if reason == "TP":
        b.fact(
            "TP_ALCANZADO",
            f"Alcanzó el TP {seconds / 60:.1f} minutos después de la entrada.",
            minutos=_r(seconds / 60, 2),
            fuente="cierre por TP",
        )
    elif market.get("toque_tp"):
        touched = datetime.fromisoformat(market["toque_tp"])
        minutes = max(0.0, (touched - entry_time).total_seconds() / 60)
        b.fact(
            "TP_ALCANZADO",
            f"El precio tocó el nivel del TP inicial unos {minutes:.0f} minutos después de la "
            "entrada (según velas M1, ±1 minuto), aunque no cerró por TP.",
            minutos=_r(minutes, 2),
            fuente="velas M1",
            vela=market["toque_tp"],
        )

    # Gestión: SL/TP, parciales, aumentos, reentrada --------------------------------------------
    sl_mods = [e for e in events if e["tipo"] == "SL_MODIFIED"]
    tp_mods = [e for e in events if e["tipo"] == "TP_MODIFIED"]
    if sl_mods or tp_mods:
        b.fact(
            "MODIFICACIONES_SL_TP",
            f"SL modificado {len(sl_mods)} veces y TP {len(tp_mods)} veces.",
            sl=len(sl_mods),
            tp=len(tp_mods),
            inferidas=sum(1 for e in sl_mods + tp_mods if e["inferido"]),
        )
    for event in sl_mods:
        level = _f(event["sl"])
        if level is not None and (level >= entry if buy else level <= entry):
            moved = datetime.fromisoformat(event["hora"])
            minutes = (moved - entry_time).total_seconds() / 60
            b.fact(
                "SL_A_BREAKEVEN",
                f"El SL se movió a breakeven o mejor ({level:g}) {minutes:.1f} minutos después "
                "de la entrada.",
                sl=level,
                hora=event["hora"],
                minutos=_r(minutes, 2),
                inferido=event["inferido"],
            )
            break
    partials = [e for e in events if e["tipo"] == "PARTIAL_CLOSE"]
    if partials:
        b.fact(
            "CIERRES_PARCIALES",
            f"{len(partials)} cierre(s) parcial(es) antes del cierre final.",
            cantidad=len(partials),
            volumenes=[e["volumen"] for e in partials],
            precios=[e["precio"] for e in partials],
            horas=[e["hora"] for e in partials],
        )
    increases = [e for e in events if e["tipo"] == "POSITION_INCREASED"]
    if increases:
        b.fact(
            "AUMENTOS_POSICION",
            f"La posición se aumentó {len(increases)} vez/veces.",
            cantidad=len(increases),
            volumenes=[e["volumen"] for e in increases],
        )
    reentry = next((e for e in events if e["tipo"] == "REENTRY"), None)
    if op["reentrada_de"] and reentry is not None:
        minutes = reentry["extra"].get("minutos_desde_cierre_por_sl")
        b.fact(
            "REENTRADA_TRAS_SL",
            f"Es una reentrada {minutes} minutos después de un cierre por SL del mismo bot "
            "(inferida por ventana de tiempo).",
            operacion_previa=op["reentrada_de"],
            minutos=minutes,
        )

    # Mercado ---------------------------------------------------------------------------------
    bar = market.get("vela_entrada")
    if bar is None:
        b.note("SIN_VELA_ENTRADA", "no hay vela M1 del minuto de entrada: sin spread de entrada")
    elif bar.get("spread") is None:
        b.note("SIN_SPREAD", "la vela M1 de entrada no trae spread")
    else:
        spread = int(bar["spread"])
        evidence = {"spread_puntos": spread, "vela": bar["hora"]}
        text = f"Spread de la vela M1 de entrada: {spread} puntos"
        if sl_dist:
            evidence["fraccion_sl"] = _r(spread / sl_dist)
            evidence["porcentaje_sl"] = _r(spread / sl_dist * 100, 1)
            text += f" ({spread / sl_dist:.1%} de la distancia al SL)"
        b.fact("SPREAD_ENTRADA", text + ".", **evidence)

    for key, ema in market["emas"].items():
        period, tf = key.removeprefix("ema").split("_")
        code = f"PRECIO_VS_EMA{period}_{tf.upper()}"
        label = f"EMA{period} {tf.upper()}"
        if not ema["ok"]:
            b.note(f"{code}_SIN_DATOS", f"{label} no calculada: {ema['motivo']}")
            continue
        value = float(ema["valor"])
        above = entry > value
        favor = above if buy else entry < value
        side = "por encima" if above else ("por debajo" if entry < value else "justo en")
        distance = (entry - value) / point if point else None
        b.fact(
            code,
            f"Al entrar, el precio ({entry:g}) estaba {side} de la {label} ({value:.5g}), "
            f"{'a favor' if favor else 'en contra'} de la dirección {op['direccion']}.",
            ema=_r(value, 6),
            precio=entry,
            posicion="ENCIMA" if above else ("DEBAJO" if entry < value else "IGUAL"),
            a_favor=favor,
            distancia_puntos=_r(distance) if distance is not None else None,
            velas=ema["velas"],
            cobertura=ema["cobertura"],
            ultima_vela=ema["ultima_vela"],
        )

    vol = market["volatilidad"]
    if vol["ok"]:
        b.fact(
            "VOLATILIDAD_ATR_H1",
            f"ATR({vol['periodo']}) H1 al entrar: {vol['atr']:.5g}, percentil "
            f"{vol['percentil']:.0f} de los {vol['dias']} días anteriores.",
            atr=_r(float(vol["atr"]), 6),
            percentil=vol["percentil"],
            muestras=vol["muestras"],
            mediana=_r(float(vol["mediana"]), 6),
            dias=vol["dias"],
        )
    else:
        b.note("VOLATILIDAD_SIN_DATOS", f"percentil de ATR H1 no calculado: {vol['motivo']}")

    news = inputs["noticias"]
    if news:
        b.fact(
            "NOTICIA_CERCANA",
            f"{len(news)} noticia(s) registrada(s) a menos de {params['ventana_noticias_min']} "
            "minutos de la entrada.",
            noticias=news,
        )
    else:
        b.note(
            "SIN_DATOS_NOTICIAS",
            "sin datos de noticias: todavía no hay fuente de calendario, así que no se puede "
            "afirmar ni descartar una noticia cercana",
        )

    if not op["despliegue"]:
        b.note("SIN_DESPLIEGUE", "operación sin despliegue asignado: no cuenta para ningún bot")

    facts_by_code = {f.code: f.evidence for f in b.facts}
    hypotheses = evaluate(outcome, facts_by_code)
    return AnalysisResult(outcome, b.facts + hypotheses, b.quality)
