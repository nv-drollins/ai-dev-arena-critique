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

# Pass --install-deps to auto-apt-install the small userland tools (tmux/curl/git).
# Docker + the NVIDIA driver are detected, never auto-installed (system daemon /
# kernel-level — install those yourself first).
INSTALL_DEPS=0
[ "${1:-}" = "--install-deps" ] && INSTALL_DEPS=1
need_pkg() {
  command -v "$1" >/dev/null 2>&1 && return 0
  if [ "$INSTALL_DEPS" = 1 ] && command -v apt-get >/dev/null 2>&1; then
    warn "'$1' missing — installing '$2' (apt)…"
    sudo apt-get update -qq && sudo apt-get install -y -qq "$2" && command -v "$1" >/dev/null 2>&1 && { ok "installed $2"; return 0; }
  fi
  err "required '$1' not found. Install:  sudo apt-get install -y $2   (or re-run with --install-deps)"; exit 3
}
need_system() { command -v "$1" >/dev/null 2>&1 || { err "required '$1' not found — $2"; exit 3; }; }

need_system python3 "install Python 3.11+ (sudo apt-get install -y python3)"
need_system docker  "install Docker (https://docs.docker.com/engine/install/ubuntu/)"
need_pkg tmux tmux
need_pkg curl curl
docker info >/dev/null 2>&1 || { if sudo docker info >/dev/null 2>&1; then warn "docker works via sudo but not as $USER — 0b will fix the group."; else err "Docker daemon not reachable — start it: sudo systemctl start docker"; exit 3; fi; }
[ -d /proc/driver/nvidia ] || { err "NVIDIA driver not visible under /proc/driver/nvidia — install it first (not auto-installed)."; exit 3; }
ok "prereqs present: python3/docker(+daemon)/tmux/curl + NVIDIA driver"

# --- 0b. wire Docker for this user + the NVIDIA container runtime (idempotent) ---
if ! id -nG "$USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
  sudo usermod -aG docker "$USER" && ok "added $USER to docker group" || warn "run: sudo usermod -aG docker $USER"
  NEWGRP_NEEDED=1
else ok "$USER already in docker group"; fi
if ! docker info 2>/dev/null | grep -qi 'Runtimes:.*nvidia' && command -v nvidia-ctk >/dev/null 2>&1; then
  sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker \
    && ok "nvidia runtime configured + docker restarted" \
    || warn "run: sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker"
else ok "nvidia runtime already registered with Docker"; fi
if [ "${NEWGRP_NEEDED:-0}" = 1 ] && ! groups 2>/dev/null | grep -qw docker; then
  echo; warn "You were just added to the 'docker' group, but this shell isn't in it yet."
  warn "Finish setup + re-run:  exit  (log out/in), then  cd ~/ai-dev-arena && bash bin/install-worker.sh"
  warn "(The re-run skips this step — you'll already be in the group.)"
  exit 0
fi

step() { printf '\n\033[1;36m==>\033[0m %s\n' "$1"; }

step "1. docker: vLLM images (Ray/critic image + the writer image)"
if docker image inspect "$VLLM_IMAGE" >/dev/null 2>&1; then
  ok "image $VLLM_IMAGE present"
elif docker pull "$VLLM_IMAGE" >/dev/null 2>&1; then
  ok "image pulled"
else
  err "failed to pull $VLLM_IMAGE — do you have nvcr.io access? (docker login nvcr.io)"
  exit 4
fi
# The WRITER runs on this node via launch-writer.sh with a DIFFERENT image
# (Docker Hub vllm-openai, has the MTP/step3p5 features). Pull it now so
# launch-writer.sh doesn't fail its preflight.
if docker image inspect "$WRITER_IMAGE" >/dev/null 2>&1; then
  ok "writer image $WRITER_IMAGE present"
elif docker pull "$WRITER_IMAGE" >/dev/null 2>&1; then
  ok "writer image pulled ($WRITER_IMAGE)"
else
  warn "could not pull writer image $WRITER_IMAGE — pull it before launch-writer.sh: docker pull $WRITER_IMAGE"
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

step "2b. stage cluster scripts into ~/"
# start-ray-worker.sh runs `bash ~/start-ray-worker.sh`, which sources ~/run_cluster.sh.
# Stage them from the repo first — without this the Ray join fails silently (the file
# doesn't exist), the worker never joins, and the 70B critic hangs forever waiting for
# the 2nd GPU.
for s in run_cluster.sh start-ray-worker.sh; do
  [ -f "cluster/$s" ] && cp -f "cluster/$s" ~/ && ok "staged ~/$s" \
    || warn "cluster/$s not found in repo — cannot stage (Ray join will fail)"
done

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
