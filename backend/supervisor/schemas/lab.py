"""Esquemas del laboratorio (fase 10)."""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from supervisor.models.enums import ExperimentStatus


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ExperimentCreate(_In):
    """Experimento nuevo. `filter` es opcional: {"modo": "excluir" | "solo", "condicion":
    {"tipo": "busqueda", "clausulas": [{"variable": "tendencia_h1_rel", "op": "eq", "valor":
    "EN_CONTRA"}]}} (las variables y operadores de la fase 9). Con `hypothesis_id` y sin
    `filter`, el filtro sale de la hipótesis (una trampa: saltarse esas entradas)."""

    title: str = Field(min_length=1, max_length=200)
    base_version_id: uuid.UUID
    change_description: str = Field(min_length=1, max_length=10_000)
    candidate_version_id: uuid.UUID | None = None
    hypothesis_id: uuid.UUID | None = None
    symbol: str | None = Field(default=None, min_length=1, max_length=64)
    filter: dict[str, Any] | None = None
    filter_from_hypothesis: bool = True


class ResultsRevisionIn(_In):
    """Revisión MANUAL: una nota, resultados externos (JSON libre), el estado o la
    conclusión. Nunca sobrescribe revisiones anteriores."""

    notes: str = Field(min_length=1, max_length=10_000)
    results: dict[str, Any] | None = None
    status: ExperimentStatus | None = None
    conclusion: str | None = Field(default=None, max_length=10_000)


class FilterRunIn(_In):
    notes: str | None = Field(default=None, max_length=10_000)
