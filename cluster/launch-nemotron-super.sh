#!/usr/bin/env bash
set -euo pipefail
C=$(docker ps --format '{{.Names}}' | grep -E '^node-[0-9]+$' | head -1)
echo "Using container: $C"
docker exec "$C" /bin/bash -lc "
  set -euo pipefail
  export VLLM_NVFP4_GEMM_BACKEND=marlin
  export VLLM_ALLOW_LONG_MAX_MODEL_LEN=1
  export VLLM_FLASHINFER_ALLREDUCE_BACKEND=trtllm
  export VLLM_USE_FLASHINFER_MOE_FP4=0
  vllm serve nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4 \
    --served-model-name nvidia/nemotron-3-super \
    --host 0.0.0.0 \
    --port 8000 \
    --tensor-parallel-size 2 \
    --distributed-executor-backend ray \
    --dtype auto \
    --kv-cache-dtype fp8 \
    --trust-remote-code \
    --gpu-memory-utilization 0.75 \
    --enable-chunked-prefill \
    --max-num-seqs 1 \
    --max-model-len 65536 \
    --moe-backend marlin \
    --mamba_ssm_cache_dtype float16 \
    --quantization fp4 \
    --reasoning-parser-plugin /app/super_v3_reasoning_parser.py \
    --reasoning-parser super_v3 \
    --enable-auto-tool-choice \
    --tool-call-parser qwen3_coder
"

