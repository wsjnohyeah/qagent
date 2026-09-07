FROM ghcr.io/astral-sh/uv:0.12.9@sha256:8b940d3a9d65bed080436972241af2e21c84b5e8c9193f7014ed71479ee795ff AS uv
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS runtime

ARG SOURCE_GIT_SHA=UNAVAILABLE

LABEL org.opencontainers.image.revision=${SOURCE_GIT_SHA}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SOURCE_GIT_SHA=${SOURCE_GIT_SHA} \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --group build --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --group build --no-editable --no-build-isolation
COPY configs ./configs
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
RUN mkdir -p /app/work/object-store && chown -R app:app /app

USER app
EXPOSE 8000
CMD ["uvicorn", "agentic_quant.api:app", "--host", "0.0.0.0", "--port", "8000"]
