"""Tramos para variables numéricas (agrupar estadísticas por DNA y condiciones de la fase 9).

- Cortes por cuantiles: con los valores ordenados v_0..v_(n-1), el corte del cuantil q es
  v_floor(q x n) (rango más cercano, sin interpolar: siempre es un valor observado). Se
  quitan los cortes repetidos y el que coincide con el mínimo (un tramo vacío no aporta).
- Tramos a partir de los cortes c_1 < ... < c_k: (-inf, c_1), [c_1, c_2), ..., [c_k, +inf).
  Así cada valor cae en exactamente un tramo.
"""

import bisect
from collections.abc import Sequence


def quantile_edges(values: Sequence[float], parts: int) -> list[float]:
    """Cortes de `parts` tramos de frecuencia parecida (p. ej. 4 = cuartiles)."""
    if parts < 2 or not values:
        return []
    ordered = sorted(values)
    n = len(ordered)
    edges: list[float] = []
    for k in range(1, parts):
        edge = ordered[min(n - 1, (k * n) // parts)]
        if edge > ordered[0] and (not edges or edge > edges[-1]):
            edges.append(edge)
    return edges


def bin_index(edges: Sequence[float], value: float) -> int:
    """Índice del tramo (0..len(edges)) que contiene el valor."""
    return bisect.bisect_right(edges, value)


def _fmt(value: float) -> str:
    text = f"{value:.4g}"
    return text


def bin_bounds(edges: Sequence[float], index: int) -> tuple[float | None, float | None]:
    low = edges[index - 1] if index > 0 else None
    high = edges[index] if index < len(edges) else None
    return low, high


def bin_label(edges: Sequence[float], index: int) -> str:
    low, high = bin_bounds(edges, index)
    if low is None and high is None:
        return "todos"
    if low is None:
        return f"< {_fmt(high)}"
    if high is None:
        return f">= {_fmt(low)}"
    return f"[{_fmt(low)}, {_fmt(high)})"
