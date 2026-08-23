#!/usr/bin/env bash
# fetch-critique-models.sh — download the writer + critic weights into the
# shared HF cache on THIS node, using a throwaway vLLM-image container (so we
# don't touch the running Ray/vLLM containers). Idempotent: hf skips existing files.
#
# Runs the download inside `nvcr.io/nvidia/vllm:26.05-py3` because that image
# already has huggingface_hub + hf_transfer and the right glibc. The host's
# ~/.cache/huggingface is bind-mounted so weights land in the real cache.
set -euo pipefail

VLLM_IMAGE="${VLLM_IMAGE:-nvcr.io/nvidia/vllm:26.05-py3}"
HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"
mkdir -p "$HF_CACHE"

WRITER="${WRITER_MODEL_HF:-nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4}"
CRITIC="${CRITIC_MODEL_HF:-nvidia/Llama-3.3-Nemotron-70B-Feedback}"

echo "[fetch] node $(hostname) — downloading into $HF_CACHE"
echo "[fetch] writer: $WRITER"
echo "[fetch] critic: $CRITIC"

docker run --rm --network host \
  -e HF_HUB_ENABLE_HF_TRANSFER=1 \
  -v "$HF_CACHE:/root/.cache/huggingface" \
  --entrypoint /bin/bash \
  "$VLLM_IMAGE" -c "
    set -e
    pip install -q --root-user-action=ignore hf_transfer >/dev/null 2>&1 || true
    for repo in '$WRITER' '$CRITIC'; do
      echo \"==== \$repo ====\"
      python3 - <<PY
from huggingface_hub import snapshot_download
p = snapshot_download('\$repo', max_workers=8)
print('  done:', p)
PY
    done
  "
echo "[fetch] node $(hostname) COMPLETE"
