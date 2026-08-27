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

# Pick up optional config (HF_TOKEN etc.) from arena.conf if present next to this script.
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=bin/arena.conf
[ -f "$HERE/arena.conf" ] && . "$HERE/arena.conf" 2>/dev/null || true

# --- preflight: fail early with a clear "install/fix X first" message ----------
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$1" >&2; exit 1; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$1" >&2; }
IMG="vllm/vllm-openai:v0.27.1"
MODEL_DIR="nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4"

command -v docker >/dev/null 2>&1 || die "Docker not found — install Docker + nvidia-container-toolkit first."
docker info >/dev/null 2>&1        || die "Docker daemon not reachable — is it running? (sudo systemctl start docker)"
[ -d /proc/driver/nvidia ]        || die "NVIDIA driver not visible under /proc/driver/nvidia — install the driver first."
docker image inspect "$IMG" >/dev/null 2>&1 \
  || die "vLLM image '$IMG' not present — run: docker pull $IMG   (or run bin/install-head.sh)."
# weights are downloaded on first launch, but warn if the cache looks empty (avoids a
# silent multi-hundred-GB download that looks like a hang)
if ! ls ~/.cache/huggingface/hub/ 2>/dev/null | grep -q "Nemotron-3.5-Lightning"; then
  warn "Nemotron weights not in ~/.cache/huggingface yet — first launch will DOWNLOAD them (several hundred GB, can take a while)."
fi
# heads-up if something is already on :8001 (we replace the container anyway)
if ss -tlnp 2>/dev/null | grep -q ":8001 "; then
  warn "something is already listening on :8001 — the old arena-writer container will be replaced."
fi

docker rm -f arena-writer >/dev/null 2>&1 || true
docker run -d --name arena-writer --network host --gpus all --shm-size 10.24g \
  -e VLLM_ALLOW_LONG_MAX_MODEL_LEN=1 \
  ${HF_TOKEN:+-e HF_TOKEN="$HF_TOKEN"} \
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
