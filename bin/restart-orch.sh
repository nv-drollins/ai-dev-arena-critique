#!/usr/bin/env bash
# restart-orch.sh — (re)start the Arena orchestrator with the agentic-demo env.
# Run this ON the head node from the repo root. Idempotent: kills any existing
# orchestrator (by port AND process name) so a stale in-memory instance can't
# survive a "restart", waits for the port to free, then relaunches.
#
# All addressing comes from bin/arena.conf — no IPs are hardcoded here.
set -euo pipefail
cd "$(dirname "$0")/../"

# shellcheck source=bin/arena.conf
. ./bin/arena.conf

# --- preflight: fail early with a clear "install/fix X first" message ----------
die() { printf '\033[1;31m✗ %s\033[0m\n' "$1" >&2; exit 1; }
[ -d .venv ] || die ".venv not found — run bin/install-head.sh first (it creates the venv + installs deps)."
[ -x .venv/bin/uvicorn ] || die "uvicorn missing from .venv — run: .venv/bin/pip install -r requirements.txt"
[ -f orchestrator/main.py ] || die "orchestrator/main.py not found — are you in the ai-dev-arena repo root?"
command -v ss   >/dev/null 2>&1 || die "'ss' not found (iproute2) — needed to check the port. Install iproute2."
command -v curl >/dev/null 2>&1 || die "'curl' not found — needed for the health check."

arena_require_roster || die "node roster not configured (see bin/arena.conf.local)"

fuser -k "${ORCH_PORT}/tcp" >/dev/null 2>&1 || true
pkill -9 -f "uvicorn orchestrator.main:app" >/dev/null 2>&1 || true
sleep 2
for _ in $(seq 1 10); do
  ss -tlnp 2>/dev/null | grep -q ":${ORCH_PORT} " || break
  sleep 1
done

# Agentic-demo wiring — derived from bin/arena.conf, override via env/.local.
WRITER_URL="${WRITER_URL:-http://${WRITER_HOST_SPARK}:${WRITER_PORT}}"
WRITER_MODEL="${WRITER_MODEL:-$WRITER_SERVED}"
CRITIC_URL="${CRITIC_URL:-http://localhost:${CRITIC_PORT}}"
CRITIC_MODEL="${CRITIC_MODEL:-$CRITIC_SERVED}"

# Build the telemetry roster from the configured nodes. The head is polled
# locally ("localhost"); every worker over SSH by its LAN address.
if [ -z "${SPARK_NODES_JSON:-}" ]; then
  _head_name="${SPARK_HEAD%%=*}"
  _nodes="{\"name\":\"${_head_name}\",\"host\":\"localhost\",\"role\":\"head\"}"
  for _w in $SPARK_WORKERS; do
    _wname="${_w%%=*}"
    _whost="${_w##*=}"; _whost="${_whost##*@}"
    _nodes="${_nodes},{\"name\":\"${_wname}\",\"host\":\"${_whost}\",\"role\":\"worker\"}"
  done
  SPARK_NODES_JSON="{\"sparks\":[${_nodes}]}"
fi

c "orchestrator :$ORCH_PORT  writer=$WRITER_URL  critic=$CRITIC_URL"

WRITER_URL="$WRITER_URL" WRITER_MODEL="$WRITER_MODEL" \
CRITIC_URL="$CRITIC_URL" CRITIC_MODEL="$CRITIC_MODEL" \
SPARK_NODES_JSON="$SPARK_NODES_JSON" \
CRITIC_ENABLED=1 LIVE_MAX_REPAIRS=2 \
nohup .venv/bin/uvicorn orchestrator.main:app --host 0.0.0.0 --port "$ORCH_PORT" \
  >> ~/uvicorn2.log 2>&1 &
disown

sleep 5
if curl -s -m5 "http://localhost:${ORCH_PORT}/" >/dev/null 2>&1; then
  ok "orchestrator up (pid $(fuser "${ORCH_PORT}/tcp" 2>/dev/null | tr -d ' '))"
else
  warn "orchestrator did not respond — check ~/uvicorn2.log"
fi
