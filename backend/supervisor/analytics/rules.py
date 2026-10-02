"""Reglas declarativas que convierten hechos (FACT) en hipótesis (HYPOTHESIS).

Cada regla dice en qué resultados aplica y qué condiciones deben cumplir los valores de
`evidence` de ciertos hechos. Si se cumplen, genera una hipótesis redactada como factor
posible ("pudo haber..."), enlazada a esos hechos (`supported_by`) y con confianza
"no_validada": una regla es una idea a contrastar, no una conclusión. La fase 9 tomará
`condition_spec()` de cada regla para probarla con datos de entrenamiento, validación y
fuera de muestra.

Cambiar un umbral, una condición o un texto = subir RULESET_VERSION (y `version` de la regla):
los análisis existentes no se tocan y el repaso crea versiones nuevas.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from supervisor.models.enums import FindingKind, Outcome

RULESET_VERSION = 1
CONFIDENCE_UNVALIDATED = "no_validada"

_OPS = {
    "existe": lambda value, _: True,
    "==": lambda value, ref: value == ref,
    ">=": lambda value, ref: value is not None and value >= ref,
    "<=": lambda value, ref: value is not None and value <= ref,
    ">": lambda value, ref: value is not None and value > ref,
    "<": lambda value, ref: value is not None and value < ref,
}


@dataclass(frozen=True)
class Condition:
    fact: str
    field: str | None = None
    op: str = "existe"
    value: Any = None

    def matches(self, facts: dict[str, dict[str, Any]]) -> bool:
        if self.fact not in facts:
            return False
        if self.op == "existe":
            return True
        evidence = facts[self.fact]
        if self.field not in evidence:
            return False
        return _OPS[self.op](evidence[self.field], self.value)

    def spec(self) -> dict[str, Any]:
        return {"hecho": self.fact, "campo": self.field, "op": self.op, "valor": self.value}


@dataclass(frozen=True)
class Rule:
    code: str
    version: int
    outcomes: tuple[Outcome, ...]
    conditions: tuple[Condition, ...]
    text: str  # admite {HECHO.campo}

    def condition_spec(self) -> dict[str, Any]:
        return {
            "regla": self.code,
            "version": self.version,
            "resultados": [o.value for o in self.outcomes],
            "condiciones": [c.spec() for c in self.conditions],
        }


@dataclass(frozen=True)
class Finding:
    kind: FindingKind
    code: str
    text: str
    evidence: dict[str, Any] = field(default_factory=dict)
    confidence: str | None = None
    rule_version: int | None = None
    supported_by: tuple[str, ...] = ()


L, W, B = Outcome.LOSS, Outcome.WIN, Outcome.BREAKEVEN

RULES: tuple[Rule, ...] = (
    Rule(
        "H_CONTRA_TENDENCIA_H1",
        1,
        (L,),
        (Condition("PRECIO_VS_EMA200_H1", "a_favor", "==", False),),
        "La entrada pudo haber tenido menor probabilidad por operar contra la tendencia de H1 "
        "(precio del otro lado de la EMA200 H1).",
    ),
    Rule(
        "H_CONTRA_TENDENCIA_M15",
        1,
        (L,),
        (Condition("PRECIO_VS_EMA50_M15", "a_favor", "==", False),),
        "La entrada pudo haber ido contra el impulso de corto plazo (precio del otro lado de "
        "la EMA50 M15).",
    ),
    Rule(
        "H_A_FAVOR_TENDENCIA_H1",
        1,
        (W,),
        (Condition("PRECIO_VS_EMA200_H1", "a_favor", "==", True),),
        "Operar a favor de la tendencia de H1 (precio del lado de la EMA200 H1 que favorece "
        "la dirección) pudo haber contribuido al resultado.",
    ),
    Rule(
        "H_VOLATILIDAD_ALTA",
        1,
        (L,),
        (Condition("VOLATILIDAD_ATR_H1", "percentil", ">=", 80),),
        "La volatilidad alta al entrar (ATR H1 en el percentil {VOLATILIDAD_ATR_H1.percentil} "
        "de los días anteriores) pudo haber facilitado que el precio alcanzara el SL.",
    ),
    Rule(
        "H_SL_TP_MAL_UBICADO",
        1,
        (L,),
        (
            Condition("MAE_VS_SL", "fraccion", ">=", 0.9),
            Condition("MFE_VS_TP", "fraccion", ">=", 0.5),
        ),
        "El SL o el TP pudieron estar mal ubicados: el precio recorrió el "
        "{MFE_VS_TP.porcentaje} % del camino al TP y aun así llegó al "
        "{MAE_VS_SL.porcentaje} % de la distancia al SL.",
    ),
    Rule(
        "H_PERDEDORA_CON_MFE",
        1,
        (L,),
        (Condition("EXCURSIONES", "mfe_r", ">=", 1.0),),
        "Pudo haber faltado gestión de la posición (breakeven o cierre parcial): llegó a ir "
        "{EXCURSIONES.mfe_r} R a favor antes de cerrar en pérdida.",
    ),
    Rule(
        "H_DEVOLVIO_MFE",
        1,
        (W,),
        (
            Condition("CAPTURA_MFE", "captura", "<=", 0.4),
            Condition("EXCURSIONES", "mfe_r", ">=", 1.0),
        ),
        "La salida pudo haber sido tardía o el objetivo demasiado lejano: solo capturó el "
        "{CAPTURA_MFE.porcentaje} % del movimiento favorable máximo.",
    ),
    Rule(
        "H_TP_TOCADO_SIN_GANAR",
        1,
        (L, B),
        (Condition("MFE_VS_TP", "supero", "==", True),),
        "El TP inicial pudo haberse movido o no ejecutado: el precio llegó a su nivel y la "
        "operación no terminó ganadora.",
    ),
    Rule(
        "H_SPREAD_ALTO",
        1,
        (L,),
        (Condition("SPREAD_ENTRADA", "fraccion_sl", ">=", 0.1),),
        "El coste del spread al entrar ({SPREAD_ENTRADA.porcentaje_sl} % de la distancia al "
        "SL) pudo haber pesado en el resultado.",
    ),
    Rule(
        "H_REENTRADA_PRECIPITADA",
        1,
        (L,),
        (Condition("REENTRADA_TRAS_SL"),),
        "La reentrada poco después de un SL pudo haber sido precipitada.",
    ),
    Rule(
        "H_NOTICIA_CERCANA",
        1,
        (L,),
        (Condition("NOTICIA_CERCANA"),),
        "Una noticia cercana a la entrada pudo haber influido en el movimiento.",
    ),
)

_PLACEHOLDER = re.compile(r"\{([A-Z0-9_]+)\.([a-z0-9_]+)\}")


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def _render(template: str, facts: dict[str, dict[str, Any]]) -> str:
    return _PLACEHOLDER.sub(lambda m: _fmt(facts[m.group(1)].get(m.group(2))), template)


def evaluate(
    outcome: Outcome, facts: dict[str, dict[str, Any]], rules: tuple[Rule, ...] = RULES
) -> list[Finding]:
    """Hipótesis de las reglas que se cumplen. `facts` es {código del hecho: evidence}."""
    findings = []
    for rule in rules:
        if outcome not in rule.outcomes:
            continue
        if not all(c.matches(facts) for c in rule.conditions):
            continue
        supported = tuple(dict.fromkeys(c.fact for c in rule.conditions))
        findings.append(
            Finding(
                kind=FindingKind.HYPOTHESIS,
                code=rule.code,
                text=_render(rule.text, facts),
                evidence={
                    "condiciones": [c.spec() for c in rule.conditions],
                    "valores": [
                        {
                            "hecho": c.fact,
                            "campo": c.field,
                            "valor": facts[c.fact].get(c.field) if c.field else True,
                        }
                        for c in rule.conditions
                    ],
                    "validacion": "pendiente: fase 9 (entrenamiento, validación, fuera de muestra)",
                },
                confidence=CONFIDENCE_UNVALIDATED,
                rule_version=rule.version,
                supported_by=supported,
            )
        )
    return findings


def ruleset_spec() -> list[dict[str, Any]]:
    return [rule.condition_spec() for rule in RULES]
