"""Hechos previos a la entrada calculados al ABRIR la operación (función pura).

El análisis de la fase 6 se hace al cerrar, así que hasta ahora una operación abierta no tenía
"precio frente a la EMA200 H1" ni el percentil de volatilidad: TRAMPA_ACTIVA no podía avisar
de una trampa basada en ellos y el diagnóstico no los tenía para las operaciones abiertas.

Solución mínima: el Trading DNA (que ya se calcula al abrir, sin lookahead) guarda también
estos hechos en `trade_dna.inputs["hechos_previos"]`, con la MISMA forma de `evidence` que los
hechos de la fase 6 (mismos códigos y campos), para que las reglas y las condiciones de la
fase 9 se evalúen igual al abrir que al cerrar:

- PRECIO_VS_EMA200_H1 / EMA50_H1 / EMA200_M15 / EMA50_M15: `a_favor`, `posicion`, `ema`...
  con supervisor.analytics.market.market_context (la misma función y las mismas velas
  cerradas antes de la entrada que usa el análisis de la fase 6).
- VOLATILIDAD_ATR_H1: `percentil`, `atr` (misma función).
- SPREAD_ENTRADA: `spread_puntos`, `fraccion_sl`, `porcentaje_sl`. Diferencia documentada:
  aquí es el spread de la última vela M1 CERRADA antes de la entrada (la del DNA); la fase 6
  usa la vela del minuto de entrada, que termina después de entrar. Al abrir solo se puede usar
  la anterior (sin lookahead).
- REENTRADA_TRAS_SL: si la operación está enlazada como reentrada tras un SL.
- NOTICIA_CERCANA: solo si hay fuente de noticias y alguna a menos de la ventana del DNA.

Cuando la operación cierra, la búsqueda de patrones y el diagnóstico prefieren los hechos del
análisis (fase 6); estos se usan si todavía no hay análisis (operaciones abiertas). Versionado:
PRE_ENTRY_VERSION entra en los parámetros del DNA; cambiarla crea versiones nuevas del DNA.
"""

from typing import Any

PRE_ENTRY_VERSION = 1
EMA_KEYS = (
    ("ema200_h1", "PRECIO_VS_EMA200_H1"),
    ("ema50_h1", "PRECIO_VS_EMA50_H1"),
    ("ema200_m15", "PRECIO_VS_EMA200_M15"),
    ("ema50_m15", "PRECIO_VS_EMA50_M15"),
)


def _r(value: float, places: int = 4) -> float:
    return round(value, places)


def build_pre_entry_facts(
    *,
    buy: bool,
    entry_price: float,
    point: float | None,
    initial_sl: float | None,
    context: dict[str, Any],
    spread_points: int | None,
    reentry: bool,
    news: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """{"version", "hechos": {código: evidence}, "sin_datos": {código: motivo},
    "incompleto": bool}. `context` es la salida de market.market_context."""
    facts: dict[str, dict[str, Any]] = {}
    missing: dict[str, str] = {}
    for key, code in EMA_KEYS:
        ema = context["emas"].get(key) or {"ok": False, "motivo": "sin calcular"}
        if not ema["ok"]:
            missing[code] = ema["motivo"]
            continue
        value = float(ema["valor"])
        above = entry_price > value
        favor = above if buy else entry_price < value
        facts[code] = {
            "ema": _r(value, 6),
            "precio": entry_price,
            "posicion": "ENCIMA" if above else ("DEBAJO" if entry_price < value else "IGUAL"),
            "a_favor": favor,
            "distancia_puntos": _r((entry_price - value) / point) if point else None,
            "ultima_vela": ema["ultima_vela"],
        }
    vol = context["volatilidad"]
    if vol["ok"]:
        facts["VOLATILIDAD_ATR_H1"] = {
            "atr": _r(float(vol["atr"]), 6),
            "percentil": vol["percentil"],
            "muestras": vol["muestras"],
        }
    else:
        missing["VOLATILIDAD_ATR_H1"] = vol["motivo"]
    if spread_points is None:
        missing["SPREAD_ENTRADA"] = "sin spread de la última vela M1 cerrada antes de la entrada"
    else:
        evidence: dict[str, Any] = {
            "spread_puntos": int(spread_points),
            "fuente": "ultima_vela_cerrada_antes_de_entrar",
        }
        if initial_sl is not None and point:
            sl_points = abs(entry_price - initial_sl) / point
            if sl_points > 0:
                evidence["fraccion_sl"] = _r(spread_points / sl_points)
                evidence["porcentaje_sl"] = _r(spread_points / sl_points * 100, 1)
        facts["SPREAD_ENTRADA"] = evidence
    if reentry:
        facts["REENTRADA_TRAS_SL"] = {"inferida": True}
    if news:
        facts["NOTICIA_CERCANA"] = {"noticias": news[:10]}
    incomplete = any(
        code.startswith("PRECIO_VS") or code == "VOLATILIDAD_ATR_H1" for code in missing
    )
    return {
        "version": PRE_ENTRY_VERSION,
        "hechos": facts,
        "sin_datos": missing,
        "incompleto": incomplete,
    }


def fact_values(facts: dict[str, dict[str, Any]] | None) -> dict[str, Any]:
    """Valores de las variables booleanas de la fase 9 (hecho_ema*_a_favor) desde unos
    hechos (del análisis o previos a la entrada)."""
    from supervisor.analytics.pattern_search import FACT_VARIABLES

    out: dict[str, Any] = {}
    for name, code, _ in FACT_VARIABLES:
        evidence = (facts or {}).get(code)
        out[name] = evidence.get("a_favor") if evidence else None
    return out
