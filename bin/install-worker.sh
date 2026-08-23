#!/usr/bin/env bash
# =============================================================================
# install-worker.sh — one-shot install of AI Dev Arena on a WORKER Spark.
#
# A worker has a MUCH simpler job than the head: it offers a GPU to the Ray
# cluster, that's it.  No Python venv, no orchestrator, no UI, no uvicorn.
#
# What it does:
#   0) prereqs (python3, docker, nvidia, tmux, ssh)
#   1) pulls the vLLM image (if not already cached)
#   2) ensures the Nemotron parser file exists at $PARSER_FILE
#      (both nodes mount the same file — content must be identical, so
#      copy from the local repo if not already present)
#   3) starts the Ray worker (tmux session "ray-worker", container "node-XXXX")
#   4) waits for the head's GCS to register us
#
# Idempotent: re-running it reuses the existing container.
#
# Override any variable from bin/arena.conf before running, e.g.:
#   SPARK_ROLE=worker bash install-worker.sh
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=bin/arena.conf
. ./bin/arena.conf

# This IS the worker installer — pin the role regardless of conf default.
export SPARK_ROLE="${SPARK_ROLE:-worker}"
[ "$SPARK_ROLE" = "auto" ] && SPARK_ROLE="worker"

ok "installing on a WORKER Spark (role: ${SPARK_ROLE:-worker})"

need() { command -v "$1" >/dev/null 2>&1 || { err "required binary '$1' not found"; exit 3; }; }
need python3
need docker
need tmux
need curl
[ -d /proc/driver/nvidia ] || { err "no NVIDIA driver /proc/driver/nvidia"; exit 3; }
ok "prereqs present"

step() { printf '\n\033[1;36m==>\033[0m %s\n' "$1"; }

step "1. docker: vLLM image"
if docker image inspect "$VLLM_IMAGE" >/dev/null 2>&1; then
  ok "image $VLLM_IMAGE present"
elif docker pull "$VLLM_IMAGE" >/dev/null 2>&1; then
  ok "image pulled"
else
  err "failed to pull $VLLM_IMAGE — do you have nvcr.io access? (docker login nvcr.io)"
  exit 4
fi

step "2. parser file (identical on head & worker — content must match bit-for-bit)"
if [ ! -f "$PARSER_FILE" ]; then
  src="$(find . -name 'super_v3_reasoning_parser.py' 2>/dev/null | head -1)"
  if [ -n "$src" ]; then
    mkdir -p "$(dirname "$PARSER_FILE")"
    cp "$src" "$PARSER_FILE"
    ok "copied parser -> $PARSER_FILE"
  else
    err "parser file not found in the repo. For gpt-oss it's not needed; for Nemotron, copy it from the head node:"
    err "  scp head:nvidia@<head>:$PARSER_FILE $PARSER_FILE"
    warn "continuing — Nemotron model will fail to serve on this node"
  fi
else
  ok "parser already at $PARSER_FILE"
fi

step "3. start Ray WORKER"
existing=$(docker ps --format '{{.Names}}' | grep -E '^node-[0-9]+$' | head -1)
if [ -n "$existing" ]; then
  ok "already running worker: $existing (skipping)"
else
  docker ps -aq --filter name=node- | xargs -r docker rm -f >/dev/null 2>&1 || true
  tmux kill-session -t ray-worker 2>/dev/null || true
  tmux new-session -d -s ray-worker "bash ~/start-ray-worker.sh 2>&1 | tee ~/ray-worker.log; sleep 86400"
  i=0
  while [ $i -lt 12 ]; do
    existing=$(docker ps --format '{{.Names}}' | grep -E '^node-[0-9]+$' | head -1)
    [ -n "$existing" ] && break
    i=$((i+1)); sleep 5
  done
  [ -n "$existing" ] || { err "worker container did not come up in 60s — tmux attach -t ray-worker"; exit 5; }
  ok "worker container: $existing"
fi

step "4. wait for head to register us"
for i in $(seq 1 20); do
  # inside the worker container, `ray status` only succeeds once the head's
  # GCS is reachable and we've registered.
  if docker exec "$existing" ray status 2>/dev/null | grep -qE 'GPU|Node IP'; then
    ok "worker is registered with the head (see head-side sparkctl.sh status for a full picture)"
    break
  fi
  echo "  ... ($i/20)"
  sleep 4
done
# not fatal — sometimes the head-side view is the authoritative one

echo
ok "========================================================"
ok "  WORKER INSTALL COMPLETE"
ok "  role: $SPARK_ROLE   container: ${existing:-?}"
ok "========================================================"
ok "worker has NOTHING to serve on its own — the HEAD is the user-facing node."
ok "to check overall health:  ~/ai-dev-arena/bin/sparkctl.sh status   (from the HEAD)"
