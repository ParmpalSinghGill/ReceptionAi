# ===========================================================================
# ReceptionAi — developer entry points.
#
# `make check` runs every gate CI runs, in the same order, so a developer can
# know a push will pass before making it. A local gate that diverges from CI is
# worse than no local gate: it teaches people to ignore it.
# ===========================================================================

SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE      ?= docker compose
PY           ?= python3
# Prefer the project venv when present, so `make test` does not depend on
# whatever happens to be on PATH.
# VENV is overridable because this repo may live on a filesystem that rejects
# symlinks (exFAT/NTFS), where `python -m venv` cannot create `.venv` in-tree.
# Put the venv anywhere and point at it:
#     make test VENV=/tmp/receptionai-venv
# or export RECEPTIONAI_VENV once in your shell profile.
# Search order: explicit override, then in-tree .venv, then ~/.receptionai-venv.
# The home-directory fallback exists because this repo may sit on NTFS/exFAT,
# where `python -m venv` cannot create `.venv` in-tree at all. Never put it in
# /tmp — that is cleared on reboot and takes your whole setup with it.
VENV         ?= $(or $(RECEPTIONAI_VENV),$(firstword $(wildcard .venv $(HOME)/.receptionai-venv) .venv))
VENV_PY      := $(VENV)/bin/python
PYTHON       := $(shell test -x $(VENV_PY) && echo $(VENV_PY) || echo $(PY))

# The migrator role, never the app role (contract §2.5). Overridable so the
# same target works against a test database.
# Read .env so host-run targets (migrate, tests) use the same ports and
# credentials as the stack. `-include` keeps a missing .env from being fatal.
# Only simple KEY=value lines are used; anything exotic belongs in the app.
-include .env
export

POSTGRES_PASSWORD ?= localpass
POSTGRES_DB       ?= receptionai
POSTGRES_PORT     ?= 5432

MIGRATION_DATABASE_URL ?= postgresql+asyncpg://receptionai_migrator:$(POSTGRES_PASSWORD)@localhost:$(POSTGRES_PORT)/$(POSTGRES_DB)

.PHONY: help up down restart reset logs ps migrate revision seed shell-db shell-redis \
        test test-unit test-integration test-rls lint format typecheck check \
        gates build install env clean

# --------------------------------------------------------------------- help ---
help:  ## Show this help
	@echo "ReceptionAi — make targets"
	@echo
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | sort \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "First run:  cp .env.example .env && make up && make migrate"

# ------------------------------------------------------------------ env ---
env: .env  ## Create .env from the example if absent
.env:
	@test -f .env || { cp .env.example .env; echo "created .env from .env.example"; }

# ------------------------------------------------------------------ stack ---
# One-shot services that run to completion and exit 0. `--wait` treats an
# exited container as a failed dependency, so a completely healthy stack would
# report failure — which teaches people to ignore the exit code. They are
# started first (dependents already gate on service_completed_successfully) and
# excluded from the wait set.
ONESHOT := minio-init
# Derived from the compose file rather than hardcoded, so a service added later
# is waited on automatically instead of being silently skipped.
WAIT_SERVICES = $(filter-out $(ONESHOT),$(shell $(COMPOSE) config --services 2>/dev/null))

up: env  ## Start the local stack and wait for healthchecks
	$(COMPOSE) up -d $(ONESHOT)
	$(COMPOSE) up -d --wait --wait-timeout 300 $(WAIT_SERVICES)
	@echo
	@echo "  API        http://localhost:$(or $(API_PORT),8000)      (needs 'make up-app')"
	@echo "  Web        http://localhost:$(or $(WEB_PORT),3000)      (needs 'make up-app')"
	@echo "  Temporal   http://localhost:$(or $(TEMPORAL_UI_PORT),8080)"
	@echo "  Grafana    http://localhost:$(or $(GRAFANA_PORT),3001)"
	@echo "  Langfuse   http://localhost:$(or $(LANGFUSE_PORT),3110)   (LLM traces)"
	@echo "  Prometheus http://localhost:$(or $(PROMETHEUS_PORT),9090)"
	@echo "  MinIO      http://localhost:$(or $(MINIO_CONSOLE_PORT),9001)"
	@echo
	@echo "Next:  make migrate"

up-app: env  ## Start the stack INCLUDING api/workers/media/web
	$(COMPOSE) --profile app up -d --wait --wait-timeout 600

down:  ## Stop the stack, keep data
	$(COMPOSE) --profile app down --remove-orphans

reset:  ## Stop the stack and DESTROY all volumes (re-runs DB init scripts)
	@echo "This deletes all local data — Postgres, MinIO, Temporal history."
	@read -p "Continue? [y/N] " ok && [ "$$ok" = "y" ]
	$(COMPOSE) --profile app down -v --remove-orphans

setup:  ## Create the Python venv and install dependencies (run this first)
	@if [ ! -x "$(VENV_PY)" ]; then \
	  echo "Creating venv at $(VENV) ..."; \
	  $(PY) -m venv --copies "$(VENV)" 2>/dev/null || { \
	    echo "In-tree venv failed (this repo is probably on NTFS/exFAT, which"; \
	    echo "cannot create symlinks). Falling back to $(HOME)/.receptionai-venv."; \
	    $(PY) -m venv --copies "$(HOME)/.receptionai-venv"; \
	  }; \
	fi
	"$(VENV_PY)" -m pip install -q --upgrade pip
	"$(VENV_PY)" -m pip install -q -e ".[dev]"
	@echo ""
	@echo "venv ready: $(VENV)"
	@echo "Next:  make up && make migrate && make test"

restart: down up  ## Stop and start again

ps:  ## Show service status
	$(COMPOSE) ps

logs:  ## Tail logs (make logs S=postgres for one service)
	$(COMPOSE) logs -f --tail=100 $(S)

# ------------------------------------------------------------- migrations ---
migrate: ## Run Alembic migrations as the privileged migrator role
	MIGRATION_DATABASE_URL="$(MIGRATION_DATABASE_URL)" $(PYTHON) -m alembic upgrade head

revision:  ## Autogenerate a migration:  make revision M="add bookings"
	@test -n "$(M)" || { echo "usage: make revision M=\"message\""; exit 1; }
	MIGRATION_DATABASE_URL="$(MIGRATION_DATABASE_URL)" \
	  $(PYTHON) -m alembic revision --autogenerate -m "$(M)"

# ------------------------------------------------------------------ shells ---
shell-db:  ## psql as the UNPRIVILEGED app role (what the app actually sees)
	@echo "Connected as receptionai_app — RLS applies. Set a tenant first:"
	@echo "  SET app.tenant_id = '<uuid>';"
	$(COMPOSE) exec -e PGPASSWORD=$${POSTGRES_PASSWORD:-localpass} postgres \
	  psql -h 127.0.0.1 -U receptionai_app -d $${POSTGRES_DB:-receptionai}

shell-db-admin:  ## psql as the superuser (bypasses RLS — for inspection only)
	$(COMPOSE) exec postgres psql -U $${POSTGRES_SUPERUSER:-postgres} -d $${POSTGRES_DB:-receptionai}

shell-redis:  ## redis-cli
	$(COMPOSE) exec redis redis-cli

# ------------------------------------------------------------------- tests ---
test: test-unit test-rls  ## Run unit + RLS suites

test-unit:  ## Unit tests (no services required)
	@if [ -d tests/unit ] && find tests/unit -name 'test_*.py' -print -quit | grep -q .; then \
	  $(PYTHON) -m pytest tests/unit -q; \
	else echo "no unit tests yet"; fi

test-integration:  ## Integration tests (needs the stack up)
	@if [ -d tests/integration ] && find tests/integration -name 'test_*.py' -print -quit | grep -q .; then \
	  $(PYTHON) -m pytest tests/integration -q; \
	else echo "no integration tests yet"; fi

# The suite that proves the core tenancy claim (contract §2). It spins its own
# PostgreSQL via testcontainers and connects as a non-owner without BYPASSRLS,
# because a suite running as the superuser passes with RLS entirely absent.
test-rls:  ## RLS tenant-isolation proofs — these gate merges
	@if [ -d tests/rls ] && find tests/rls -name 'test_*.py' -print -quit | grep -q .; then \
	  TESTCONTAINERS_RYUK_DISABLED=true $(PYTHON) -m pytest tests/rls -q; \
	else echo "no RLS tests yet"; fi

# ------------------------------------------------------------------- lint ---
lint:  ## ruff check + format check
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

format:  ## Apply ruff formatting and safe fixes
	$(PYTHON) -m ruff check --fix .
	$(PYTHON) -m ruff format .

typecheck:  ## mypy --strict
	$(PYTHON) -m mypy packages/ apps/

# The contract gates. Structural rules, not style — each exists because the
# failure it prevents is silent.
gates:  ## Run the contract enforcement gates
	$(PY) tools/check_layering.py
	$(PY) tools/check_uuid_usage.py
	$(PY) tools/check_image_pinning.py

# ------------------------------------------------------------------ check ---
# Everything CI runs, in CI's order. Pass this and a push should be green.
check: lint gates typecheck test  ## Run every gate CI runs
	@echo
	@echo "All local gates passed."

# ------------------------------------------------------------------ build ---
build:  ## Build the api, worker and media images
	docker build -f infra/docker/api.Dockerfile    --target runtime -t receptionai/api:local .
	docker build -f infra/docker/worker.Dockerfile --target runtime -t receptionai/worker:local .
	docker build -f infra/docker/media.Dockerfile  --target runtime -t receptionai/media:local .

install: setup  ## Alias for `setup`, kept because muscle memory says `install`

# ── Everything, one command ─────────────────────────────────────────────────
dev:  ## START EVERYTHING: docker + migrations + seed + all three servers
	@tools/dev.sh up

dev-down:  ## Stop the three dev servers (docker keeps running)
	@tools/dev.sh down

dev-stop:  ## Stop the dev servers AND the docker stack
	@tools/dev.sh stop

dev-restart:  ## Restart the dev servers
	@tools/dev.sh restart

dev-status:  ## What is running, and on which port
	@tools/dev.sh status

dev-logs:  ## Tail one server's log:  make dev-logs S=api|marketing|web
	@tools/dev.sh logs $(or $(S),api)

# ── Individual dev servers (foreground) ─────────────────────────────────────
# `make dev` runs all three detached, which is what you usually want. These
# run ONE in the foreground instead — for a debugger, or when you want the
# logs inline and Ctrl-C to stop exactly the thing you are looking at.
#
# `next` is invoked through its resolved path rather than `pnpm dev` because a
# pnpm workspace needs symlinks in node_modules/.bin, which NTFS/exFAT cannot
# create. On ext4, `pnpm --filter <app> dev` works too.
NEXT := node $(CURDIR)/node_modules/next/dist/bin/next

dev-api:  ## Run the API (reads .env, incl. your provider keys) — port 8000
	@test -f .env || { echo "no .env — run: cp .env.example .env"; exit 1; }
	set -a && . ./.env && set +a && \
	  $(PYTHON) -m uvicorn apps.api.main:app --reload --port $${API_PORT:-8000}

dev-media:  ## Run the realtime voice worker (browser calls) — port 8100
	@test -f .env || { echo "no .env — run: cp .env.example .env"; exit 1; }
	@grep -q '^DEEPGRAM_API_KEY=.\+' .env || \
	  { echo "DEEPGRAM_API_KEY is not set in .env — voice needs it for STT and TTS"; exit 1; }
	set -a && . ./.env && set +a && \
	  $(PYTHON) -m uvicorn apps.media.main:app --factory \
	    --reload --port $${MEDIA_PORT:-8100}

dev-marketing:  ## Run the public site with the demo chat — port 3002
	cd apps/marketing && $(NEXT) dev -p 3002

dev-web:  ## Run the admin portal — port 3003
	cd apps/web && $(NEXT) dev -p 3003

seed:  ## Seed the demo tenant (Northgate Dental). Idempotent.
	set -a && . ./.env && set +a && $(PYTHON) tools/seed_demo.py

clean:  ## Remove caches and build artefacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache coverage.xml htmlcov
