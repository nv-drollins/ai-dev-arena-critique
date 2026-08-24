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

# Run the writer in its OWN container (needs vLLM v0.27.1, newer than the Ray
# container's image). Standalone — not tensor-parallel, not in the Ray cluster.
_run() {
  # MEMORY SAFETY GUARD (learned the hard way 2026-08-23): GB10 has 128GB unified
  # memory. A resident 70B critic shard (~70GB) + a 60GB BF16 writer exceeds it and
  # thrashes the node unrecoverable. Refuse to launch if free < guard.
  local free_gb; free_gb=$(free -g | awk '/Mem/{print $7}')   # "available" column
  local need_gb="${WRITER_MIN_FREE_GB:-30}"
  if [ -n "$free_gb" ] && [ "$free_gb" -lt "$need_gb" ]; then
    echo "[writer] ✗ REFUSING to launch: only ${free_gb}GB available, need >=${need_gb}GB."
    echo "[writer]   A big model is probably resident. Co-loading here risks thrashing the node."
    echo "[writer]   Use a SMALL writer (NVFP4 ~22GB), stop the other engine, or run on the other Spark."
    echo "[writer]   Override with WRITER_MIN_FREE_GB=0 (danger)."
    exit 2
  fi
  docker image inspect "$WRITER_IMAGE" >/dev/null 2>&1 || {
    echo "[writer] pulling $WRITER_IMAGE (first run)…"; docker pull "$WRITER_IMAGE"; }

  # Build the vllm args. DSpark speculative decoding is optional (faster on GB10).
  local spec_args=""
  if [ "$WRITER_DSPARK" = "1" ]; then
    spec_args="--speculative_config.model $WRITER_DSPARK_HF --speculative_config.num_speculative_tokens $WRITER_SPEC_TOKENS"
    echo "[writer] DSpark speculative decoding ON (drafter=$WRITER_DSPARK_HF, k=$WRITER_SPEC_TOKENS)"
  fi

  echo "[writer] launching $WRITER_SERVED on $(hostname) — image $WRITER_IMAGE, ${free_gb}GB free, :$WRITER_PORT"
  docker rm -f arena-writer >/dev/null 2>&1 || true
  docker run --rm --name arena-writer \
    --network host --gpus all --shm-size 10.24g \
    -v "$HF_CACHE:/root/.cache/huggingface" \
    -e VLLM_ALLOW_LONG_MAX_MODEL_LEN=1 \
    "$WRITER_IMAGE" \
    --model "$WRITER_MODEL_HF" \
    --served-model-name "$WRITER_SERVED" \
    --host 0.0.0.0 --port "$WRITER_PORT" \
    --tensor-parallel-size "$WRITER_TP" \
    --trust-remote-code \
    --reasoning-parser nemotron_v3 \
    --gpu-memory-utilization "$WRITER_GPU_FRAC" \
    --max-model-len "$WRITER_MAXLEN" \
    --max-num-seqs 2 \
    $spec_args
}

# If invoked on the head but the writer belongs on another Spark, hop there.
MY_IPS="$(hostname -I 2>/dev/null || true)"
if [ -n "${WRITER_HOST_SPARK:-}" ] && ! echo "$MY_IPS" | grep -qw "$WRITER_HOST_SPARK"; then
  echo "[writer] pinning to $WRITER_HOST_SPARK (this host is not it) — ssh over"
  exec ssh -o StrictHostKeyChecking=no "nvidia@$WRITER_HOST_SPARK" \
    "cd ~/ai-dev-arena-critique && bash bin/launch-writer.sh"
fi
_run
