#!/usr/bin/env bash
#
# One command to bring the whole development environment up or down.
#
#   tools/dev.sh up       docker stack + migrations + seed + all four servers
#   tools/dev.sh down     stop the four servers (docker keeps running)
#   tools/dev.sh stop     stop the servers AND the docker stack
#   tools/dev.sh status    what is running, and on which port
#   tools/dev.sh logs api|media|marketing|web
#   tools/dev.sh restart  down, then up
#
# Servers run detached with their PIDs in .run/ and their logs in .run/*.log.
# Detached rather than a split terminal because there is no dependable
# multiplexer on every machine, and a Makefile that assumes tmux is a Makefile
# that fails on someone else's laptop.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

RUN_DIR="$REPO/.run"
mkdir -p "$RUN_DIR"

# Colours only when attached to a terminal, so piping to a file stays clean.
if [[ -t 1 ]]; then
  B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[2m'; N=$'\033[0m'
else
  B=''; G=''; Y=''; R=''; D=''; N=''
fi

say()  { printf '%s\n' "$*"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
err()  { printf '  %s✗%s %s\n' "$R" "$N" "$*" >&2; }

# ── Python interpreter ──────────────────────────────────────────────────────
# Same search order as the Makefile: explicit override, in-tree venv, then the
# home-directory fallback used when the repo lives on a filesystem without
# symlinks (NTFS/exFAT).
pick_python() {
  local candidates=("${RECEPTIONAI_VENV:-}/bin/python" "$REPO/.venv/bin/python" "$HOME/.receptionai-venv/bin/python")
  for c in "${candidates[@]}"; do
    [[ -x "$c" ]] && { echo "$c"; return; }
  done
  echo ""
}

PYTHON="$(pick_python)"
NEXT="$REPO/node_modules/next/dist/bin/next"

# ── Ports, read from .env so this never disagrees with the app ──────────────
port_of() {
  local key="$1" default="$2"
  local v
  v="$(grep -E "^${key}=" .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' || true)"
  echo "${v:-$default}"
}

# Prefixed to avoid collision: `cmd_up` sources .env (which defines WEB_PORT
# for docker-compose), and a bare WEB_PORT here would be silently overwritten
# mid-run — the script would then start the server on one port and print
# another.
DEV_API_PORT="$(port_of API_PORT 8000)"
DEV_MEDIA_PORT="$(port_of MEDIA_PORT 8100)"
DEV_MARKETING_PORT=3002
DEV_WEB_PORT=3003

# ── Process helpers ─────────────────────────────────────────────────────────
pidfile() { echo "$RUN_DIR/$1.pid"; }
logfile() { echo "$RUN_DIR/$1.log"; }

is_running() {
  local f; f="$(pidfile "$1")"
  [[ -f "$f" ]] || return 1
  local pid; pid="$(cat "$f" 2>/dev/null || true)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

port_busy() { curl -sf -m 2 -o /dev/null "http://localhost:$1${2:-/}" 2>/dev/null; }

start_one() {
  local name="$1" port="$2"; shift 2
  if is_running "$name"; then
    warn "$name already running (pid $(cat "$(pidfile "$name")"))"
    return 0
  fi
  # Someone else on our port — almost always a server from a previous session
  # whose pidfile we lost. Say so rather than starting a second one that will
  # fail confusingly on bind.
  if port_busy "$port" && [[ "$name" != "api" ]]; then
    warn "port $port is already serving something; not starting $name"
    return 0
  fi
  ( "$@" >"$(logfile "$name")" 2>&1 & echo $! >"$(pidfile "$name")" )
  ok "$name starting (pid $(cat "$(pidfile "$name")"), log: .run/$name.log)"
}

stop_one() {
  local name="$1"
  local f; f="$(pidfile "$name")"
  if ! is_running "$name"; then
    rm -f "$f"
    return 0
  fi
  local pid; pid="$(cat "$f")"
  # Kill the process group: `next dev` spawns children, and killing only the
  # parent leaves them holding the port.
  kill -TERM -- "-$(ps -o pgid= "$pid" | tr -d ' ')" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    is_running "$name" || break
    sleep 0.25
  done
  if is_running "$name"; then
    kill -KILL "$pid" 2>/dev/null || true
    warn "$name needed SIGKILL"
  else
    ok "$name stopped"
  fi
  rm -f "$f"
}

wait_for() {
  local label="$1" url="$2" timeout="${3:-90}"
  local start; start="$(date +%s)"
  while true; do
    if curl -sf -m 3 -o /dev/null "$url" 2>/dev/null; then
      ok "$label ready"
      return 0
    fi
    if (( $(date +%s) - start > timeout )); then
      err "$label did not come up within ${timeout}s"
      return 1
    fi
    sleep 1
  done
}

# ── Commands ────────────────────────────────────────────────────────────────
cmd_up() {
  say "${B}ReceptionAi — starting development environment${N}"
  say ""

  [[ -f .env ]] || { cp .env.example .env; ok ".env created from .env.example"; }

  if [[ -z "$PYTHON" ]]; then
    err "no Python venv found. Run: make setup"
    exit 1
  fi
  if [[ ! -f "$NEXT" ]]; then
    err "node_modules missing. Run: pnpm install  (see README if it OOMs on NTFS)"
    exit 1
  fi

  say "${D}docker stack${N}"
  # Delegate to `make up` rather than calling compose directly: it starts the
  # one-shot init containers separately from the long-running ones, because
  # `docker compose up --wait` treats a one-shot that exited 0 (minio-init) as
  # a failed dependency and aborts. Reimplementing that split here would mean
  # two places to get it wrong.
  if ! make up >/dev/null 2>&1; then
    err "docker stack failed to start. Run 'make up' to see why."
    exit 1
  fi
  ok "$(docker compose ps --status running --format '{{.Name}}' | wc -l | tr -d ' ') services healthy"

  say "${D}database${N}"
  set -a; . ./.env; set +a
  MIGRATION_DATABASE_URL="${MIGRATION_DATABASE_URL:-postgresql+asyncpg://receptionai_migrator:${POSTGRES_PASSWORD:-localpass}@localhost:${POSTGRES_PORT:-55432}/${POSTGRES_DB:-receptionai}}" \
    "$PYTHON" -m alembic upgrade head >/dev/null 2>&1 && ok "migrations at head"
  "$PYTHON" tools/seed_demo.py >/dev/null 2>&1 && ok "demo tenant seeded"

  say "${D}servers${N}"
  start_one api "$DEV_API_PORT" env "$PYTHON" -m uvicorn apps.api.main:app \
    --host 127.0.0.1 --port "$DEV_API_PORT"
  if [[ -n "${DEEPGRAM_API_KEY:-}" ]]; then
    start_one media "$DEV_MEDIA_PORT" env "$PYTHON" -m uvicorn apps.media.main:app \
      --factory --host 127.0.0.1 --port "$DEV_MEDIA_PORT"
  else
    warn "DEEPGRAM_API_KEY unset — voice worker not started (chat demo still works)"
  fi
  start_one marketing "$DEV_MARKETING_PORT" bash -c "cd '$REPO/apps/marketing' && exec node '$NEXT' dev -p $DEV_MARKETING_PORT"
  start_one web "$DEV_WEB_PORT" bash -c "cd '$REPO/apps/web' && exec node '$NEXT' dev -p $DEV_WEB_PORT"

  say ""
  wait_for "api"       "http://localhost:$DEV_API_PORT/healthz" 60 || true
  if [[ -n "${DEEPGRAM_API_KEY:-}" ]]; then
    wait_for "media"   "http://localhost:$DEV_MEDIA_PORT/healthz" 60 || true
  fi
  wait_for "marketing" "http://localhost:$DEV_MARKETING_PORT"   90 || true
  wait_for "web"       "http://localhost:$DEV_WEB_PORT"         90 || true

  say ""
  say "${B}Ready.${N}"
  say ""
  say "  ${G}Voice demo${N}     http://localhost:$DEV_MARKETING_PORT      ${D}← click \"Request a call\"${N}"
  say "  ${G}Demo chat${N}      http://localhost:$DEV_MARKETING_PORT      ${D}← click \"Try the chat instead\"${N}"
  say "  Admin portal    http://localhost:$DEV_WEB_PORT"
  say "  API docs        http://localhost:$DEV_API_PORT/docs"
  say ""
  say "  ${D}Grafana http://localhost:$(port_of GRAFANA_PORT 3001)  ·  Temporal http://localhost:$(port_of TEMPORAL_UI_PORT 8080)${N}"
  say ""
  say "  ${D}tools/dev.sh logs api   ·   tools/dev.sh down${N}"
}

cmd_down() {
  say "${B}Stopping servers${N}"
  for n in api media marketing web; do stop_one "$n"; done
  say ""
  say "${D}Docker stack is still up. 'tools/dev.sh stop' stops that too.${N}"
}

cmd_stop() {
  cmd_down
  say ""
  say "${B}Stopping docker stack${N}"
  docker compose down >/dev/null 2>&1 && ok "stack down"
}

cmd_status() {
  say "${B}Servers${N}"
  local rows=(
    "api:$DEV_API_PORT:/healthz"
    "media:$DEV_MEDIA_PORT:/healthz"
    "marketing:$DEV_MARKETING_PORT:/"
    "web:$DEV_WEB_PORT:/"
  )
  for row in "${rows[@]}"; do
    IFS=: read -r name port path <<<"$row"
    if is_running "$name" && port_busy "$port" "$path"; then
      printf '  %s✓%s %-10s :%-6s pid %s\n' "$G" "$N" "$name" "$port" "$(cat "$(pidfile "$name")")"
    elif is_running "$name"; then
      printf '  %s!%s %-10s :%-6s pid %s %s(process up, not answering yet)%s\n' \
        "$Y" "$N" "$name" "$port" "$(cat "$(pidfile "$name")")" "$D" "$N"
    else
      printf '  %s·%s %-10s :%-6s %sstopped%s\n' "$D" "$N" "$name" "$port" "$D" "$N"
    fi
  done
  say ""
  say "${B}Docker${N}"
  local n; n="$(docker compose ps --status running --format '{{.Name}}' 2>/dev/null | wc -l | tr -d ' ')"
  if [[ "$n" == "0" ]]; then
    printf '  %s·%s stack down\n' "$D" "$N"
  else
    printf '  %s✓%s %s services running\n' "$G" "$N" "$n"
  fi
}

cmd_logs() {
  local which="${1:-}"
  case "$which" in
    api|media|marketing|web) tail -f "$(logfile "$which")" ;;
    *) err "usage: tools/dev.sh logs api|media|marketing|web"; exit 1 ;;
  esac
}

case "${1:-up}" in
  up)      cmd_up ;;
  down)    cmd_down ;;
  stop)    cmd_stop ;;
  restart) cmd_down; say ""; cmd_up ;;
  status)  cmd_status ;;
  logs)    shift; cmd_logs "$@" ;;
  *)       err "usage: tools/dev.sh {up|down|stop|restart|status|logs}"; exit 1 ;;
esac
