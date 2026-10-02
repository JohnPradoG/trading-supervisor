FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --create-home --uid 10001 supervisor
WORKDIR /app

COPY backend/pyproject.toml ./
# Incluye las plantillas y estáticos del dashboard (package-data en pyproject.toml).
COPY backend/supervisor ./supervisor
RUN pip install .

COPY backend/alembic.ini ./
COPY backend/migrations ./migrations

USER supervisor
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"
# 2 workers: suficiente para 1 vCPU; la ingesta es ligera (validar + INSERT).
CMD ["uvicorn", "supervisor.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
