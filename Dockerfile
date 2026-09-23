# Keep the uv version aligned with .github/workflows/python-tests.yml.
FROM ghcr.io/astral-sh/uv:0.12.17 AS uv
FROM python:3.12-slim AS python-base

FROM python-base AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY app ./app
# --locked rejects stale/missing locks instead of resolving new versions.
# Install a wheel, not an editable link into this build stage.
RUN uv sync --locked --no-dev --no-editable \
    && uv pip check --python /app/.venv/bin/python \
    && uv pip freeze --python /app/.venv/bin/python

FROM python-base AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    PORT=8000 \
    EXPORT_DIR=/data/exports
WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
# Keep non-secret inputs for inventory verification; never sync at startup.
COPY pyproject.toml uv.lock ./
RUN mkdir -p /data/exports
EXPOSE 8000

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
