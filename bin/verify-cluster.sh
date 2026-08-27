#!/usr/bin/env bash
# verify-cluster.sh — one-shot health check for the whole AI Dev Arena stack.
# Run on the HEAD after install. Exits 0 if everything the demo needs is up,
# non-zero otherwise, with a clear ✓/✗ per component and the fix for each ✗.
#
#   bash bin/verify-cluster.sh
#
# Checks, in dependency order:
#   1. Ray cluster has 2 GPUs (worker joined)
#   2. Writer  vLLM serving on :8001  (nemotron-lightning-30b)
#   3. Critic  vLLM serving on :8002  (llama33-nemotron-70b-feedback)
#   4. System python3 can run the grader (pytest + flask importable)
#   5. Orchestrator up on :8080 and wired to writer + critic
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=bin/arena.conf
. "$HERE/arena.conf" 2>/dev/null || true

WRITER_PORT="${WRITER_PORT:-8001}"
CRITIC_PORT="${CRITIC_PORT:-8002}"
ORCH_PORT="${ORCH_PORT:-8080}"
WRITER_HOST_SPARK="${WRITER_HOST_SPARK:-192.168.1.149}"

if [ -t 1 ]; then G=$'\033[1;32m'; R=$'\033[1;31m'; Y=$'\033[1;33m'; Z=$'\033[0m'; else G= R= Y= Z=; fi
pass=0; fail=0
say_ok()   { printf '  %s✓%s %s\n' "$G" "$Z" "$1"; pass=$((pass+1)); }
say_bad()  { printf '  %s✗%s %s\n' "$R" "$Z" "$1"; [ -n "${2:-}" ] && printf '      fix: %s\n' "$2"; fail=$((fail+1)); }

echo "── AI Dev Arena cluster health ──"

# 1. Ray: 2 GPUs
C=$(docker ps --format '{{.Names}}' 2>/dev/null | grep -E '^node-[0-9]+$' | head -1)
if [ -z "$C" ]; then
  say_bad "Ray head container not running" "on head: tmux new -d -s ray-head 'bash ~/start-ray-head.sh'"
else
  gpu=$(docker exec "$C" ray status 2>/dev/null | grep -oE '[0-9.]+/[0-9.]+ GPU' | head -1)
  if echo "$gpu" | grep -qE '/2\.0 GPU'; then say_ok "Ray cluster: 2 GPUs ($gpu)"
  else say_bad "Ray sees '$gpu' — worker not joined" "on worker: tmux new -d -s ray-worker 'bash ~/start-ray-worker.sh'"; fi
fi

# 2. Writer
if curl -s -m4 "http://${WRITER_HOST_SPARK}:${WRITER_PORT}/v1/models" 2>/dev/null | grep -q nemotron; then
  say_ok "Writer serving on ${WRITER_HOST_SPARK}:${WRITER_PORT}"
else
  say_bad "Writer not serving on ${WRITER_HOST_SPARK}:${WRITER_PORT}" "on worker: bash bin/launch-writer.sh"
fi

# 3. Critic
if curl -s -m4 "http://localhost:${CRITIC_PORT}/v1/models" 2>/dev/null | grep -q llama; then
  say_ok "Critic serving on :${CRITIC_PORT}"
else
  say_bad "Critic not serving on :${CRITIC_PORT} (still loading? 70B takes a while after download)" \
          "on head: tmux new -s critic 'bash bin/launch-critic.sh'  (check ~/critic.log)"
fi

# 4. Grader interpreter (challenge tests run via SYSTEM python3, not the venv)
SYS_PY=/usr/bin/python3; [ -x "$SYS_PY" ] || SYS_PY="$(PATH=/usr/bin:/bin command -v python3)"
if "$SYS_PY" -c "import pytest, flask" >/dev/null 2>&1; then
  say_ok "Grader ready: $SYS_PY has pytest + flask"
else
  say_bad "$SYS_PY missing pytest/flask — challenge tests will score 0/N" \
          "$SYS_PY -m pip install --user --break-system-packages pytest flask"
fi

# 5. Orchestrator up + wired
cfg=$(curl -s -m4 "http://localhost:${ORCH_PORT}/api/config" 2>/dev/null)
if [ -z "$cfg" ]; then
  say_bad "Orchestrator not up on :${ORCH_PORT}" "on head: bash bin/restart-orch.sh"
else
  w=$(printf '%s' "$cfg" | grep -o '"writer_model":"[^"]*"' | cut -d'"' -f4)
  cr=$(printf '%s' "$cfg" | grep -o '"critic_model":"[^"]*"' | cut -d'"' -f4)
  ce=$(printf '%s' "$cfg" | grep -o '"critic_enabled":[a-z]*' | cut -d: -f2)
  if [ -n "$w" ] && [ -n "$cr" ] && [ "$ce" = "true" ]; then
    say_ok "Orchestrator up + wired (writer=$w critic=$cr)"
  else
    say_bad "Orchestrator up but not fully wired (writer=$w critic=$cr enabled=$ce)" "bash bin/restart-orch.sh"
  fi
fi

echo "─────────────────────────────────"
if [ "$fail" -eq 0 ]; then
  printf '%s✓ ALL %d CHECKS PASS — demo ready%s\n' "$G" "$pass" "$Z"
  exit 0
else
  printf '%s✗ %d/%d checks failed%s — see the fixes above\n' "$R" "$fail" "$((pass+fail))" "$Z"
  exit 1
fi
