#!/usr/bin/env bash
# launch-writer.sh — start the WRITER vLLM (Nemotron-3.5-Lightning-30B) on the
# writer node with everything the agentic demo needs:
#   • native tool-calling (step3p5 parser)  — so Hermes can drive it as an agent
#   • MTP speculative decoding               — ~2 tokens/pass, on-brand NVIDIA speedup
#   • prefix caching (+ chunked prefill)     — reuses the repo-context prefix each turn
#   • 64K context                            — Hermes requires >= 64K
#   • gpu-memory-utilization 0.24            — fits the MTP draft head alongside the
#                                              co-resident 70B critic TP shard (0.25 OOMs)
#
# Run this ON the writer node (the box serving :8001).
set -e

docker rm -f arena-writer >/dev/null 2>&1 || true
docker run -d --name arena-writer --network host --gpus all --shm-size 10.24g \
  -e VLLM_ALLOW_LONG_MAX_MODEL_LEN=1 \
  -v /home/nvidia/.cache/huggingface:/root/.cache/huggingface \
  vllm/vllm-openai:v0.27.1 \
  --model nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4 \
  --served-model-name nemotron-lightning-30b \
  --host 0.0.0.0 --port 8001 --tensor-parallel-size 1 --trust-remote-code \
  --reasoning-parser nemotron_v3 --gpu-memory-utilization 0.24 \
  --max-model-len 65536 --max-num-seqs 2 \
  --enable-prefix-caching \
  --enable-auto-tool-choice --tool-call-parser step3p5 \
  --speculative-config '{"method": "mtp", "num_speculative_tokens": 1}'

echo "arena-writer starting — watch: docker logs -f arena-writer"
echo "ready when: curl -s http://localhost:8001/v1/models | grep nemotron-lightning-30b"
