# syntax=docker/dockerfile:1
ARG TORCH_EXTRA=cu132

FROM python:3.11-slim-bookworm AS builder
ARG TORCH_EXTRA
COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /usr/local/bin/uv

# some coqui TTS dependencies have no prebuilt wheels on every platform
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0
WORKDIR /app

# dependencies first, so code changes don't invalidate the (large) torch layer
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --extra legacy --extra api --extra ${TORCH_EXTRA}

COPY .project-root ./
COPY src ./src
COPY configs ./configs
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra legacy --extra api --extra ${TORCH_EXTRA}


FROM python:3.11-slim-bookworm
RUN useradd --create-home --uid 1000 app
WORKDIR /app
# the project is installed editable, so configs/ and .project-root are found relative to src/
COPY --from=builder --chown=app:app /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility
USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

ENTRYPOINT ["tts-ui-api", "server.host=0.0.0.0", "server.port=8000"]
CMD ["device=cuda"]
