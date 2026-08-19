# syntax=docker/dockerfile:1.7
# ===========================================================================
# apps/media — realtime voice sessions. Contract amendment A1.
#
# A separate image because A1 makes it a separate process, for reasons that are
# part of the contract rather than implementation detail:
#
#   - A slow LLM call in a request handler head-of-line blocks audio frames on
#     the same event loop.
#   - Voice scales on concurrent-call count; the API scales on RPS.
#   - Media pods need maxUnavailable: 0 and a long termination grace so a
#     deploy never cuts a live call. Colocating with apps/api would force API
#     deploys to wait on in-flight calls.
#
# This image exports receptionai_voice_active_sessions and
# receptionai_voice_session_capacity (A7), whose RATIO — not an absolute
# session count — is the HPA signal in §E19.4. Voice workers sit at 15-25% CPU
# while completely full, so CPU-based autoscaling never fires.
#
# Build from the repository root:
#     docker build -f infra/docker/media.Dockerfile .
# ===========================================================================

FROM python:3.12-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS deps

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.5.11@sha256:0ac957607303916420297a4c9c213bb33fbd3c888f9cd7f4f7273596ebf42b85 /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /app/.venv && \
    uv pip install --python /app/.venv/bin/python -r pyproject.toml

# --------------------------------------------------------------------------
# Stage 2: build.
# --------------------------------------------------------------------------
FROM deps AS build

COPY packages/ packages/
COPY apps/media/ apps/media/
COPY apps/__init__.py* apps/
COPY tools/ tools/

ENV PYTHONPATH=/app

# --------------------------------------------------------------------------
# Stage 3: runtime. Audio codecs make distroless impossible here for the same
# reason as the worker — real shared libraries, not a shell convenience.
# --------------------------------------------------------------------------
FROM python:3.12-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS runtime

ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown

LABEL org.opencontainers.image.source="https://github.com/receptionai/receptionai" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_TIME}" \
      org.opencontainers.image.title="receptionai-media"

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ffmpeg libopus0 libsndfile1 ca-certificates curl; \
    rm -rf /var/lib/apt/lists/*; \
    groupadd --gid 65532 nonroot; \
    useradd --uid 65532 --gid 65532 --create-home --shell /usr/sbin/nologin nonroot

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PATH="/app/.venv/bin:$PATH" \
    GIT_SHA="${GIT_SHA}" \
    OTEL_SERVICE_NAME=receptionai-media \
    OTEL_PYTHON_LOG_CORRELATION=true \
    MEDIA_PORT=8100

WORKDIR /app
COPY --from=build --chown=65532:65532 /app/.venv    /app/.venv
COPY --from=build --chown=65532:65532 /app/packages /app/packages
COPY --from=build --chown=65532:65532 /app/apps     /app/apps

USER 65532

# 8100 serves both /health and /metrics — the latter carries the A7 gauges the
# HPA reads, so a media pod that cannot serve this port is unscalable as well
# as unhealthy.
EXPOSE 8100

HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8100/health || exit 1

ENTRYPOINT ["python", "-m", "apps.media.main"]
