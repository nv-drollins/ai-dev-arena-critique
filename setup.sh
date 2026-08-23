#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# setup.sh  —  one-shot bring-up of the AI Dev Arena on a fresh DGX Spark head.
#               (The worker has a much simpler setup; see deploy/worker.md.)
#
#     bash setup.sh
#
# Requires:
#   - Python 3.11+
#   - Docker installed and `nvidia @ docker` working
#   - Reachable worker on 192.168.100.11 (100GbE link)
#   - Model weights already in ~/.cache/huggingface (not downloaded here —
#     they are ~200 GB)
# -----------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

step() { printf '\n\033[1;36m==>\033[0m %s\n' "$1"; }
ok()   { printf '  ✓ %s\n' "$1"; }

step "0. Sanity"
python3 --version
ok "python3 found"
command -v docker >/dev/null || { echo "docker not on PATH"; exit 1; }
ok "docker on PATH"

step "1. Create + load the Python venv"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck source=/dev/null
. .venv/bin/activate
python -m pip install --upgrade pip wheel >/dev/null
python -m pip install -r requirements.txt
ok "venv + reqs installed"

step "2. Make scripts executable"
chmod +x cluster/*.sh scripts/*.sh deploy/*.sh 2>/dev/null || true
ok "chmod done"

step "3. Bring up the Ray+Docker cluster head (rank 0 of TP=2)"
if [ ! -f cluster/start-ray-head.sh ]; then
  echo "  ✗ cluster/start-ray-head.sh missing — copy it into this repo first"; exit 1
fi
bash cluster/start-ray-head.sh &
HEAD_PID=$!
ok "Ray head starting (pid $HEAD_PID)"

step "4. Bring up the worker (Spark #2)"
echo "  On the WORKER, in another shell:  bash cluster/start-ray-worker.sh"
echo "  (The script is in this repo — push via deploy/deploy-worker-from-head.sh)"
ok "worker must be started separately (see above)"

step "5. Launch the model (gpt-oss-120b, TP=2, served on :8000)"
wait "${HEAD_PID}" 2>/dev/null || true      # head finished — Ray head is up
# give Ray a few seconds to see the worker
sleep 5
docker exec -it "$(docker ps --format '{{.Names}}' | grep -E '^node-[0-9]+' | head -1)" bash -c '
  vllm serve openai/gpt-oss-120b \
    --served-model-name gpt-oss-120b \
    --host 0.0.0.0 --port 8000 \
    --tensor-parallel-size 2 \
    --distributed-executor-backend ray \
    --dtype auto --trust-remote-code \
    --gpu-memory-utilization 0.85 \
    --max-model-len 32768 \
    --max-num-seqs 2
' &
VLLM_PID=$!
ok  "vLLM starting (pid $VLLM_PID)"

step "6. Wait for vLLM to be ready"
for i in $(seq 1 60); do
  sleep 10
  if curl -sf http://127.0.0.1:8000/v1/models >/dev/null 2>&1; then
    ok "vLLM is ready"; break
  fi
  printf '  waiting for vLLM… %s/600s\n' "$((i*10))"
done
curl -sf http://127.0.0.1:8000/v1/models | tee ~/v1-models.json || { echo "  ✗ vLLM never became ready"; exit 1; }

step "7. Start the orchestrator (uvicorn) on :8080"
MODEL_NAME=gpt-oss-120b
nohup .venv/bin/uvicorn orchestrator.main:app --host 0.0.0.0 --port 8080 \
  >> ~/uvicorn.log 2>&1 &
disown
sleep 3
if curl -s http://127.0.0.1:8080/ | head -c 80 | grep -q "AI Dev Arena"; then
  ok "orchestrator is up"
else
  echo "  ✗ orchestrator failed to start — check ~/uvicorn.log"
  exit 1
fi

step "✅ done — open:"
echo "  Operator:  http://192.168.1.159:8080/operator"
echo "  Arena:     http://192.168.1.159:8080/arena"
echo "  Theater:   http://192.168.1.159:8080/theater"
echo "  CLI:       python3 scripts/run_demo.py status"
