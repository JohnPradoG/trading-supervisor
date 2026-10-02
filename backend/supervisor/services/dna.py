"""Consulta del Trading DNA de una operación (solo lectura)."""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from supervisor.analytics.dna_features import FEATURES, GROUPS
from supervisor.models import Trade, TradeDna
from supervisor.services.errors import NotFound


@dataclass(frozen=True)
class TradeDnaView:
    trade: Trade
    dna: TradeDna | None
    history: list[TradeDna]


def get_trade_dna(
    session: Session, trade_id: uuid.UUID, version: int | None = None
) -> TradeDnaView:
    """DNA vigente (la mayor dna_version) o el de `version`, con la lista de versiones."""
    trade = session.get(Trade, trade_id)
    if trade is None:
        raise NotFound("operación no encontrada")
    history = list(
        session.scalars(
            select(TradeDna)
            .where(TradeDna.trade_id == trade_id)
            .order_by(TradeDna.dna_version.desc())
        )
    )
    selected = None
    if history:
        wanted = history[0].dna_version if version is None else version
        selected = next((d for d in history if d.dna_version == wanted), None)
    if version is not None and selected is None:
        raise NotFound(f"la operación no tiene la versión {version} del DNA")
    return TradeDnaView(trade=trade, dna=selected, history=history)


def sections(dna: TradeDna) -> list[dict[str, Any]]:
    """Variables agrupadas por sección, en el orden del catálogo. Las que el DNA no tiene
    (calculado con otro conjunto de variables) salen como NULL con ese motivo."""
    features = dna.features or {}
    reasons = dna.null_reasons or {}
    out = []
    for group, title in GROUPS:
        items = []
        for spec in FEATURES:
            if spec.group != group:
                continue
            present = spec.name in features
            value = features.get(spec.name)
            reason = reasons.get(spec.name)
            if not present:
                reason = f"no existe en el conjunto de variables v{dna.feature_set_version}"
            items.append(
                {
                    "name": spec.name,
                    "label": spec.label,
                    "value_type": spec.value_type,
                    "timeframe": spec.timeframe,
                    "unit": spec.unit,
                    "value": value,
                    "null_reason": reason if value is None else None,
                }
            )
        out.append({"group": group, "title": title, "features": items})
    return out


def feature_catalog() -> list[dict[str, Any]]:
    return [
        {
            "name": f.name,
            "version": f.version,
            "group": f.group,
            "label": f.label,
            "value_type": f.value_type,
            "timeframe": f.timeframe,
            "unit": f.unit,
            "searchable": f.searchable,
            "available": f.available,
            "categories": list(f.categories) or None,
            "bins": list(f.bins) or None,
            "formula": f.formula,
            "null_policy": f.null_policy,
        }
        for f in FEATURES
    ]
