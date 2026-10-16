# RA API image. Build context: backend/ (see render.yaml at the repo root).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.28 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, so a code-only change reuses this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev \
 && useradd --create-home --uid 10001 ra \
 && chown -R ra /app
USER ra

ENV PATH="/app/.venv/bin:$PATH" \
    PORT=8000
EXPOSE 8000
CMD ["sh", "docker-start.sh"]
