#!/usr/bin/env bash
# restart-orch.sh — (re)start the Arena orchestrator with the agentic-demo env.
# Run this ON the head node from the repo root. Idempotent: kills any existing
# orchestrator (by port AND process name) so a stale in-memory instance can't
# survive a "restart", waits for :8080 to free, then relaunches.
set -euo pipefail
cd "$(dirname "$0")/../"

fuser -k 8080/tcp >/dev/null 2>&1 || true
pkill -9 -f "uvicorn orchestrator.main:app" >/dev/null 2>&1 || true
sleep 2
for _ in $(seq 1 10); do
  ss -tlnp 2>/dev/null | grep -q ":8080 " || break
  sleep 1
done

# Agentic-demo wiring (edit here or override via the environment):
WRITER_URL="${WRITER_URL:-http://192.168.1.149:8001}"
WRITER_MODEL="${WRITER_MODEL:-nemotron-lightning-30b}"
CRITIC_URL="${CRITIC_URL:-http://localhost:8002}"
CRITIC_MODEL="${CRITIC_MODEL:-llama33-nemotron-70b-feedback}"

WRITER_URL="$WRITER_URL" WRITER_MODEL="$WRITER_MODEL" \
CRITIC_URL="$CRITIC_URL" CRITIC_MODEL="$CRITIC_MODEL" \
CRITIC_ENABLED=1 LIVE_MAX_REPAIRS=2 \
nohup .venv/bin/uvicorn orchestrator.main:app --host 0.0.0.0 --port 8080 \
  >> ~/uvicorn2.log 2>&1 &
disown

sleep 5
if curl -s -m5 http://localhost:8080/ >/dev/null 2>&1; then
  echo "orchestrator up (pid $(fuser 8080/tcp 2>/dev/null | tr -d ' '))"
else
  echo "WARNING: orchestrator did not respond — check ~/uvicorn2.log"
fi
