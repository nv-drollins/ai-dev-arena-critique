#!/usr/bin/env bash
# launch-critic.sh — serve the CRITIC (Llama-3.3-Nemotron-70B-Feedback)
# TENSOR-PARALLEL ACROSS BOTH SPARKS. This is the demo headline: a 70B model
# that needs the cluster, reviewing code live.
#
#   Critic = 70B dense, BF16 (~141GB) → ~70GB per Spark at TP=2.
#   Served on :$CRITIC_PORT as $CRITIC_SERVED (OpenAI-compatible).
#
# Runs from the HEAD container (rank 0); Ray spans the worker for rank 1.
# GPU memory shares with the writer on the writer's node — hence the modest
# fraction. Tune via CRITIC_GPU_FRAC. See docs/CRITIQUE_DESIGN.md §3.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=bin/arena.conf
. "$HERE/arena.conf"

CRITIC_GPU_FRAC="${CRITIC_GPU_FRAC:-0.70}"     # leaves headroom for the writer's shard
CRITIC_MAXLEN="${CRITIC_MAXLEN:-16384}"        # review prompt is big-ish; output modest

# --- preflight: fail early with a clear "install/fix X first" message ----------
command -v docker >/dev/null 2>&1 || { echo "✗ Docker not found — install Docker + nvidia-container-toolkit first." >&2; exit 1; }
docker info >/dev/null 2>&1        || { echo "✗ Docker daemon not reachable — is it running?" >&2; exit 1; }
[ -d /proc/driver/nvidia ]        || { echo "✗ NVIDIA driver not visible under /proc/driver/nvidia." >&2; exit 1; }

C=$(docker ps --format '{{.Names}}' | grep -E '^node-[0-9]+$' | head -1)
[ -n "$C" ] || { echo "✗ no Ray container on $(hostname) — start the cluster first: bin/start.sh (or bin/install-head.sh)" >&2; exit 1; }
echo "[critic] container $C — serving $CRITIC_SERVED (TP=$CRITIC_TP across both Sparks, :$CRITIC_PORT)"

docker exec "$C" /bin/bash -lc "
  set -euo pipefail
  export VLLM_ALLOW_LONG_MAX_MODEL_LEN=1
  vllm serve '$CRITIC_MODEL_HF' \
    --served-model-name '$CRITIC_SERVED' \
    --host 0.0.0.0 --port '$CRITIC_PORT' \
    --tensor-parallel-size '$CRITIC_TP' \
    --distributed-executor-backend ray \
    --dtype auto --trust-remote-code \
    --gpu-memory-utilization '$CRITIC_GPU_FRAC' \
    --max-model-len '$CRITIC_MAXLEN' \
    --max-num-seqs 1
"
