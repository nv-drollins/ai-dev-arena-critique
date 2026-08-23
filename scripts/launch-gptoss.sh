#!/usr/bin/env bash
# Launch gpt-oss-120b on the two-Spark Ray cluster
set -euo pipefail
C=$(docker ps --format '{{.Names}}' | grep -E '^node-[0-9]+$' | head -1)
echo "Using container: $C"
docker exec "$C" /bin/bash -lc "
  set -euo pipefail
  export VLLM_ALLOW_LONG_MAX_MODEL_LEN=1
  vllm serve openai/gpt-oss-120b \
    --served-model-name gpt-oss-120b \
    --host 0.0.0.0 \
    --port 8000 \
    --tensor-parallel-size 2 \
    --distributed-executor-backend ray \
    --dtype auto \
    --trust-remote-code \
    --gpu-memory-utilization 0.85 \
    --max-model-len 32768 \
    --max-num-seqs 2
"
