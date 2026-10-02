FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --create-home --uid 10001 supervisor
WORKDIR /app

COPY backend/pyproject.toml ./
COPY backend/supervisor ./supervisor
RUN pip install .

COPY backend/alembic.ini ./
COPY backend/migrations ./migrations

USER supervisor
# La fase 3 añade aquí el arranque de la API; por ahora la imagen aplica migraciones.
CMD ["alembic", "upgrade", "head"]
