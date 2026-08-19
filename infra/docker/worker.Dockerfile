# syntax=docker/dockerfile:1.7
# ===========================================================================
# apps/worker — Temporal workers (workflows + activities), contract §3.
#
# One image serves every queue class. TASK_QUEUE is injected per Deployment /
# compose service (bookings | notifications | ingestion), so the three
# deployments are the same artefact with different configuration — not three
# images that can drift.
#
# Build from the repository root:
#     docker build -f infra/docker/worker.Dockerfile .
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

# Dependency manifest only, so editing an activity does not re-resolve the
# dependency tree.
COPY pyproject.toml ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /app/.venv && \
    uv pip install --python /app/.venv/bin/python -r pyproject.toml

# --------------------------------------------------------------------------
# Stage 2: build — workspace source on top of the cached dependency layer.
# --------------------------------------------------------------------------
FROM deps AS build

COPY packages/ packages/
COPY apps/worker/ apps/worker/
COPY apps/__init__.py* apps/
COPY migrations/ migrations/
COPY alembic.ini* ./
COPY tools/ tools/

ENV PYTHONPATH=/app

# --------------------------------------------------------------------------
# Stage 3: runtime.
#
# debian-slim rather than distroless, and unlike the API this is not a
# healthcheck compromise but a hard requirement (§E18.2): workers transcode
# audio and need ffmpeg, libopus and libsndfile — real shared libraries that
# distroless does not carry. The larger attack surface is accepted and
# compensated for in Kubernetes with a read-only root filesystem, dropped
# capabilities, and a default-deny NetworkPolicy (§E19.7).
# --------------------------------------------------------------------------
FROM python:3.12-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS runtime

ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown

LABEL org.opencontainers.image.source="https://github.com/receptionai/receptionai" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_TIME}" \
      org.opencontainers.image.title="receptionai-worker"

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
    OTEL_SERVICE_NAME=receptionai-worker \
    OTEL_PYTHON_LOG_CORRELATION=true \
    TASK_QUEUE=bookings

WORKDIR /app
COPY --from=build --chown=65532:65532 /app/.venv    /app/.venv
COPY --from=build --chown=65532:65532 /app/packages /app/packages
COPY --from=build --chown=65532:65532 /app/apps     /app/apps

USER 65532

# A worker serves no HTTP port, so the healthcheck asks the only question that
# matters: is the process still able to import its own entrypoint and reach
# Temporal? The worker is expected to expose a `--health-check` flag; until it
# does, this degrades to an import check, which still catches the common
# failure (a bad deploy where the module does not load).
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import importlib; importlib.import_module('apps.worker.main')" || exit 1

ENTRYPOINT ["python", "-m", "apps.worker.main"]
