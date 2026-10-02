"""Utilidades compartidas por los módulos del worker."""

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from supervisor.models import Trade


def quantize(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def set_quality(trade: Trade, key: str, value: Any) -> None:
    """Pone o quita (value=None) una clave de data_quality. Se reasigna el dict entero porque
    SQLAlchemy no detecta cambios internos en JSONB."""
    current = trade.data_quality or {}
    new = dict(current)
    if value is None:
        new.pop(key, None)
    else:
        new[key] = value
    if new != current:
        trade.data_quality = new


def add_warning(trade: Trade, text: str) -> None:
    """Añade un aviso (sin repetir) a data_quality["avisos"]."""
    warnings = list((trade.data_quality or {}).get("avisos", []))
    if text not in warnings:
        warnings.append(text)
        set_quality(trade, "avisos", warnings[-50:])
