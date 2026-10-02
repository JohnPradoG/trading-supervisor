"""Día, hora y sesión de mercado de un instante, siempre calculados en UTC.

Sesiones (horas UTC fijas, sin ajuste por horario de verano: en verano la sesión real empieza
una hora antes; se documenta como limitación en lugar de adivinar la zona del broker):

- Asia:        00:00-09:00 (Tokio)
- Londres:     07:00-16:00
- Nueva York:  12:00-21:00

Las sesiones se solapan; para agrupar se usa una etiqueta única por hora:

- ASIA            00-07
- LONDRES         07-12 (incluye el solape con Asia de 07 a 09)
- LONDRES_NY      12-16 (solape Londres-Nueva York)
- NUEVA_YORK      16-21
- FUERA_SESION    21-24 (cierre de Nueva York y apertura de Sídney)
"""

from datetime import UTC, datetime

WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")

SESSION_HOURS = {
    "asia": (0, 9),
    "londres": (7, 16),
    "nueva_york": (12, 21),
}

_LABELS = (
    (0, 7, "ASIA"),
    (7, 12, "LONDRES"),
    (12, 16, "LONDRES_NY"),
    (16, 21, "NUEVA_YORK"),
    (21, 24, "FUERA_SESION"),
)
SESSION_LABELS = tuple(label for _, _, label in _LABELS)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("se necesita una hora con zona horaria")
    return value.astimezone(UTC)


def session_label(value: datetime) -> str:
    hour = _utc(value).hour
    for start, end, label in _LABELS:
        if start <= hour < end:
            return label
    raise AssertionError("hora fuera de rango")  # pragma: no cover


def session_flags(value: datetime) -> dict[str, bool]:
    hour = _utc(value).hour
    return {name: start <= hour < end for name, (start, end) in SESSION_HOURS.items()}


def time_info(value: datetime) -> dict:
    """Día de la semana (0 = lunes), hora UTC y sesión de un instante."""
    utc = _utc(value)
    return {
        "dia_semana": utc.weekday(),
        "dia_nombre": WEEKDAYS[utc.weekday()],
        "hora_utc": utc.hour,
        "sesion": session_label(utc),
        "sesiones_activas": session_flags(utc),
    }
