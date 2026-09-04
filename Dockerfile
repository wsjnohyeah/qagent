FROM ghcr.io/astral-sh/uv:0.12.9 AS uv
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
COPY configs ./configs
RUN mkdir -p /app/work/object-store && chown -R app:app /app

USER app
EXPOSE 8000
CMD ["uvicorn", "agentic_quant.api:app", "--host", "0.0.0.0", "--port", "8000"]

