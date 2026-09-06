# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /bin/
WORKDIR /app

# Install dependencies first (cached unless the lockfile changes)
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra postgres

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra postgres

ENV PATH="/app/.venv/bin:$PATH" \
    NEXUM_ENVIRONMENT=production \
    NEXUM_DATABASE_URL=sqlite:////data/nexum.db
RUN mkdir -p /data && useradd --create-home nexum && chown -R nexum:nexum /app /data
USER nexum
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status == 200 else 1)"
CMD ["sh", "-c", "nexum init-db && uvicorn nexum.main:app --host 0.0.0.0 --port 8000"]
