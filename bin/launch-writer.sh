#!/usr/bin/env bash
# launch-writer.sh — serve the WRITER (Nemotron-3.5-Lightning-30B-A3B) on ONE Spark.
#
#   Writer = fast interactive codegen. 30B MoE (~3B active), NVFP4 (~22GB).
#   Tensor-parallel-1 → lives entirely on a single GPU. We pin it to the
#   WRITER_HOST_SPARK node so the OTHER Spark's GPU is free for the critic's shard.
#
# Served on :$WRITER_PORT as $WRITER_SERVED (OpenAI-compatible).
#
# GPU memory: writer + critic must SHARE GPUs on the writer's node (critic runs
# TP=2 across both). --gpu-memory-utilization is deliberately modest so the two
# engines coexist. Tune via WRITER_GPU_FRAC. See docs/CRITIQUE_DESIGN.md §3.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=bin/arena.conf
. "$HERE/arena.conf"

WRITER_GPU_FRAC="${WRITER_GPU_FRAC:-0.25}"     # writer is small; leave room for critic
WRITER_MAXLEN="${WRITER_MAXLEN:-32768}"

# Run inside the Ray container on the writer's host. If we're already on that
# host, exec locally; otherwise ssh to it. The demo drives this from the head.
_run() {
  local box
  box=$(docker ps --format '{{.Names}}' | grep -E '^node-[0-9]+$' | head -1)
  [ -n "$box" ] || { echo "no Ray container on $(hostname) — start the cluster first"; exit 1; }
  echo "[writer] container $box on $(hostname) — serving $WRITER_SERVED (TP=$WRITER_TP, :$WRITER_PORT)"
  docker exec "$box" /bin/bash -lc "
    set -euo pipefail
    export VLLM_ALLOW_LONG_MAX_MODEL_LEN=1
    vllm serve '$WRITER_MODEL_HF' \
      --served-model-name '$WRITER_SERVED' \
      --host 0.0.0.0 --port '$WRITER_PORT' \
      --tensor-parallel-size '$WRITER_TP' \
      --dtype auto --trust-remote-code \
      --gpu-memory-utilization '$WRITER_GPU_FRAC' \
      --max-model-len '$WRITER_MAXLEN' \
      --max-num-seqs 2
  "
}

# If invoked on the head but the writer belongs on another Spark, hop there.
MY_IPS="$(hostname -I 2>/dev/null || true)"
if [ -n "${WRITER_HOST_SPARK:-}" ] && ! echo "$MY_IPS" | grep -qw "$WRITER_HOST_SPARK"; then
  echo "[writer] pinning to $WRITER_HOST_SPARK (this host is not it) — ssh over"
  exec ssh -o StrictHostKeyChecking=no "nvidia@$WRITER_HOST_SPARK" \
    "cd ~/ai-dev-arena-critique && bash bin/launch-writer.sh"
fi
_run
