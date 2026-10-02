"""Resultado de una operación cerrada: WIN, LOSS o BREAKEVEN.

Regla de breakeven (la misma para el análisis y las estadísticas):

- Con riesgo conocido (risk_amount > 0): BREAKEVEN si |neto| <= breakeven_r_fraction x riesgo
  (por defecto 0.05 R: con 100 USD de riesgo, cualquier neto entre -5 y +5 USD).
- Sin riesgo (abrió sin SL): BREAKEVEN si |neto| <= |comisión| (el precio volvió a la entrada
  y el resultado es solo el coste de la operación). Si la comisión es 0, solo neto = 0.

Fuera de la tolerancia: neto > 0 es WIN y neto < 0 es LOSS.
"""

from dataclasses import dataclass
from decimal import Decimal

from supervisor.models.enums import Outcome

DEFAULT_BREAKEVEN_R_FRACTION = 0.05


@dataclass(frozen=True)
class OutcomeResult:
    outcome: Outcome
    tolerance: float
    basis: str  # "riesgo" o "comision"


def classify_outcome(
    net: float | Decimal,
    risk: float | Decimal | None,
    commission: float | Decimal | None,
    breakeven_r_fraction: float = DEFAULT_BREAKEVEN_R_FRACTION,
) -> OutcomeResult:
    net_f = float(net)
    if risk is not None and float(risk) > 0:
        tolerance = breakeven_r_fraction * float(risk)
        basis = "riesgo"
    else:
        tolerance = abs(float(commission or 0))
        basis = "comision"
    if abs(net_f) <= tolerance:
        outcome = Outcome.BREAKEVEN
    elif net_f > 0:
        outcome = Outcome.WIN
    else:
        outcome = Outcome.LOSS
    return OutcomeResult(outcome, round(tolerance, 6), basis)


OUTCOME_TEXT = {
    Outcome.WIN: "GANADORA",
    Outcome.LOSS: "PERDEDORA",
    Outcome.BREAKEVEN: "BREAKEVEN",
}
