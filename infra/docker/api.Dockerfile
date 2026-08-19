# syntax=docker/dockerfile:1.7
# ===========================================================================
# apps/api — FastAPI (HTTP, WS, SSE). Contract §9: thin, no business logic.
#
# Base images are pinned by digest (§E21.3): a mutable tag can be re-pushed, so
# the image CI verified is not provably the image that runs.
#
# Build from the REPOSITORY ROOT:
#     docker build -f infra/docker/api.Dockerfile .
# The context is the root because the app imports packages/, and a build
# context that excludes them cannot produce a working image.
# ===========================================================================

# --------------------------------------------------------------------------
# Stage 1: deps. Split from the build so a source-only change reuses the
# installed dependency layer — the difference between a 20-second and a
# 4-minute CI build (§E18.1).
#
# The ONLY files copied here are the ones that affect dependency resolution.
# Copying source into this stage would invalidate the layer on every edit and
# defeat the entire split.
# --------------------------------------------------------------------------
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

# Dependencies only — never the project itself, whose source has not been
# copied yet. `--no-install-project` is what makes that possible.
#
# uv.lock is not yet committed; when it is, add `COPY uv.lock ./` above and
# swap this for `uv sync --frozen --no-install-project --no-dev`, which is
# reproducible. Until then this resolves at build time, which is honest about
# being non-hermetic rather than pretending a lock exists.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /app/.venv && \
    uv pip install --python /app/.venv/bin/python -r pyproject.toml

# --------------------------------------------------------------------------
# Stage 2: build. Adds the workspace source on top of the cached deps layer.
# This is the target docker-compose runs, so the local stack keeps hot reload
# and a shell for `alembic`, `pytest` and debugging.
# --------------------------------------------------------------------------
FROM deps AS build

COPY packages/ packages/
COPY apps/api/ apps/api/
COPY apps/__init__.py* apps/
COPY migrations/ migrations/
COPY alembic.ini* ./
COPY tools/ tools/

# Importable without installation: /app is on the path, and packages/ and apps/
# are plain directories. Matches how tests and tools run in CI.
ENV PYTHONPATH=/app

# --------------------------------------------------------------------------
# Stage 3: runtime. Non-root, minimal, no build tooling.
#
# 05 §E18.1 specifies distroless here, and that is right for production: an
# attacker with RCE in a slim image gets curl, apt, bash and a writable /tmp;
# in distroless they get a Python interpreter and nothing else. This file uses
# slim instead, for one reason stated plainly rather than hidden: distroless
# has no shell, so it cannot run a container HEALTHCHECK, and the brief for
# this stack requires working healthchecks. The compensating controls are
# below — non-root, no package manager at runtime, no build tools.
#
# When these images move to Kubernetes, switch this stage to
# gcr.io/distroless/python3-debian12:nonroot and delete the HEALTHCHECK:
# k8s probes own liveness/readiness and a Docker HEALTHCHECK is dead weight
# there (§E18.1).
# --------------------------------------------------------------------------
FROM python:3.12-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS runtime

ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown

LABEL org.opencontainers.image.source="https://github.com/receptionai/receptionai" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_TIME}" \
      org.opencontainers.image.title="receptionai-api"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PATH="/app/.venv/bin:$PATH" \
    GIT_SHA="${GIT_SHA}" \
    OTEL_SERVICE_NAME=receptionai-api \
    OTEL_PYTHON_LOG_CORRELATION=true

# curl is here solely for the HEALTHCHECK below. uid 65532 matches the
# distroless `nonroot` uid, so a switch to distroless needs no file-ownership
# changes and no surprise permission errors on mounted volumes.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates; \
    rm -rf /var/lib/apt/lists/*; \
    groupadd --gid 65532 nonroot; \
    useradd --uid 65532 --gid 65532 --create-home --shell /usr/sbin/nologin nonroot

WORKDIR /app
COPY --from=build --chown=65532:65532 /app/.venv    /app/.venv
COPY --from=build --chown=65532:65532 /app/packages /app/packages
COPY --from=build --chown=65532:65532 /app/apps     /app/apps

USER 65532
EXPOSE 8000

# start-period covers first-boot import cost so a slow cold start is not
# reported as a failure. Hitting /health/ready rather than /health: readiness
# reflects dependencies (DB, Redis), which is what "is this container useful"
# actually means.
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health/ready || exit 1

# --workers 1 deliberately: one process per container, scaled by replica count.
# Multiple uvicorn workers inside one container hide per-process memory leaks
# from the HPA, complicate graceful shutdown, and make CPU autoscaling lie.
#
# --timeout-keep-alive 65 exceeds the ALB's 60s idle timeout so the load
# balancer closes idle connections rather than the server; the reverse ordering
# produces spurious 502s (§E18.1).
ENTRYPOINT ["python", "-m", "uvicorn"]
CMD ["apps.api.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", \
     "--loop", "uvloop", "--http", "httptools", \
     "--no-server-header", "--proxy-headers", \
     "--forwarded-allow-ips", "10.0.0.0/8", \
     "--timeout-keep-alive", "65"]
