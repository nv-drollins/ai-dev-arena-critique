#!/usr/bin/env bash
# ============================================================================
# spark-model-swap.sh — hot-swap the served model across the two DGX Sparks
#
#   ./spark-model-swap.sh status
#   ./spark-model-swap.sh gptoss        # 120B MoE, fast (default for demos)
#   ./spark-model-swap.sh nemotron      # 120B reasoning MoE (slower, CoT)
#   ./spark-model-swap.sh swap          # toggle to the other model
#
# Safe by design:
#  * Clean-shutdown of vLLM FIRST (SIGTERM) so its GPUs are released back to
#    Ray — this avoids the "Current node has no GPU available" placement-group
#    deadlock from hard-killing.
#  * If GPUs are still reserved after a clean stop (the deadlock case), it does
#    a FULL clean restart: ray stop on BOTH nodes, then re-bootstrap head+worker
#    (containers are --rm so they must be relaunched), then launch.
#  * Ends by repointing the Arena orchestrator at the new served-model name and
#    restarting it (fuser -k 8080, nohup+disown — the known-good pattern).
#
# Run from anywhere with key-based SSH to both Sparks.
# ============================================================================
set -uo pipefail

NODE1="nvidia@192.168.1.159"       # head / orchestrator / vLLM serve
NODE2="nvidia@192.168.1.149"       # worker
SSH=(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=6 -o BatchMode=yes)

# model -> launcher (on node1 home) and the served name the orchestrator uses
declare -A LAUNCHER=( [gptoss]=launch-gptoss.sh [nemotron]=launch-nemotron-super.sh )
declare -A SERVED=(   [gptoss]=gpt-oss-120b     [nemotron]=nvidia/nemotron-3-super )

c()  { printf '\033[1;34m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
ok() { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
warn(){ printf '\033[1;33m  !\033[0m %s\n' "$*"; }
err(){ printf '\033[1;31m  ✗\033[0m %s\n' "$*" >&2; }

container_of() {  # $1 = node -> first node-XXXXX container
  "${SSH[@]}" "$1" "docker ps --format '{{.Names}}' | grep -E '^node-[0-9]+\$' | head -1" 2>/dev/null
}

current_model() {
  "${SSH[@]}" "$NODE1" "curl -s -m4 http://localhost:8000/v1/models" 2>/dev/null \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"][0]["id"])' 2>/dev/null
}

gpu_reserved_line() {  # ray status GPU reservation on node1
  C=$(container_of "$NODE1"); [ -z "$C" ] && echo "no-container"
  "${SSH[@]}" "$NODE1" "docker exec $C ray status 2>/dev/null" \
    | grep -iE 'GPU' | grep -iE 'reserved|used of' | head -1
}

wait_for_model() {  # $1 = served name id we expect on :8000
  local want=$1 i
  # Nemotron-120B loads ~5-6 min across the two Sparks; gpt-oss ~2 min
  for i in $(seq 1 80); do
    cur=$(current_model)
    if echo "$cur" | grep -qE "$want"; then ok "vLLM serving $want"; return 0; fi
    # fail fast if engine is erroring in the model's log
    if "${SSH[@]}" "$NODE1" "ls ~/vllm-*.log >/dev/null 2>&1 && grep -m1 -q 'Engine core initialization failed\|EngineCore failed to start' ~/vllm-*.log 2>/dev/null"; then
      err "vLLM engine failed to start — check ~/vllm-*.log"; return 1
    fi
    c "waiting for $want ... ($i/80, $((i*6))s)"
    sleep 6
  done
  err "timed out waiting for $want"; return 1
}

stop_vllm_clean() {  # SIGTERM the engine tree in the node1 container, then force
  C=$(container_of "$NODE1")
  c "cleanly stopping vLLM (releasing 2 GPUs to Ray)..."
  "${SSH[@]}" "$NODE1" "docker exec $C bash -lc '
     pkill -TERM -f \"vllm serve\"      2>/dev/null
     sleep 12
     pkill -TERM -f \"VLLM::EngineCore\" 2>/dev/null
     sleep 6
     pkill -9   -f \"vllm\"           2>/dev/null
     pkill -9   -f \"VLLM::\"         2>/dev/null' " 2>/dev/null
  sleep 4
}

cluster_is_healthy() {  # ray sees 2+ nodes AND 2 GPUs with 0 in use on Total Usage
  local C gpu
  C=$(container_of "$NODE1"); [ -z "$C" ] && return 1
  # "0.0/2.0 GPU (...)" under Total Usage is the healthy shape
  gpu=$("${SSH[@]}" "$NODE1" "docker exec $C ray status 2>/dev/null" \
    | awk '/^Total Usage/,/^From request|^$/{ if ($0 ~ /GPU/) print $0 } ' \
    | grep -oE '[0-9.]+/[0-9.]+ GPU' | head -1)
  case "$gpu" in
    0.0/2.0*GPU*|0.0/2.0*) return 0 ;;
  esac
  # fallback: no "used of/reserved" non-zero line
  local bad
  bad=$("${SSH[@]}" "$NODE1" "docker exec $C ray status 2>/dev/null" \
    | grep -iE 'GPU' | grep -vE '^\s*[0.]+/[0-9.]+ +GPU' | grep -iE 'reserved|used of')
  [ -z "$bad" ]
}

full_cluster_restart() {
  warn "GPUs still reserved — doing a FULL clean cluster restart (this is the known deadlock recovery)"
  local C1 C2
  C1=$(container_of "$NODE1"); C2=$(container_of "$NODE2")
  # stop workers first, then head; containers are --rm so they exit (expected)
  c "ray stop on BOTH nodes (containers will exit — expected)"
  "${SSH[@]}" "$NODE2" "docker exec ${C2:-node-x} ray stop 2>/dev/null; true"
  sleep 3
  "${SSH[@]}" "$NODE1" "docker exec ${C1:-node-x} ray stop 2>/dev/null; true"
  sleep 5
  # remove any leftover dead containers
  "${SSH[@]}" "$NODE1" "docker ps -aq --filter name=node- | xargs -r docker rm -f 2>/dev/null; true"
  "${SSH[@]}" "$NODE2" "docker ps -aq --filter name=node- | xargs -r docker rm -f 2>/dev/null; true"
  # re-bootstrap head, then worker
  c "re-launching Ray head (node1)..."
  "${SSH[@]}" "$NODE1" "tmux new-session -d -s ray-head 'bash ~/start-ray-head.sh 2>&1 | tee -a ~/ray-head.log'; true"
  c "waiting for head container + ray up..."
  local i
  for i in $(seq 1 30); do
    C1=$(container_of "$NODE1"); [ -n "$C1" ] && ok "head container: $C1" && break; sleep 4
  done
  [ -n "${C1:-}" ] || { err "head container failed to appear"; return 1; }
  c "re-launching Ray worker (node2)..."
  "${SSH[@]}" "$NODE2" "tmux new-session -d -s ray-worker 'bash ~/start-ray-worker.sh 2>&1 | tee ~/ray-worker.log'; true"
  c "waiting for full cluster (2 nodes, 2 free GPUs)..."
  for i in $(seq 1 40); do
    if cluster_is_healthy; then ok "cluster healthy (2 nodes, 2 GPUs free)"; return 0; sleep 0; fi
    c "waiting for cluster ... ($i/40)"; sleep 5
  done
  err "cluster did not become healthy"; return 1
}

restart_orchestrator() {  # $1 = served model name
  c "repointing Arena orchestrator at $1 and restarting (port 8080)..."
  "${SSH[@]}" "$NODE1" "cd ~/ai-dev-arena && fuser -k 8080/tcp 2>/dev/null; sleep 2; \
     MODEL_NAME=$1 nohup .venv/bin/uvicorn orchestrator.main:app --host 0.0.0.0 --port 8080 >> ~/uvicorn.log 2>&1 & \
     disown; sleep 5; curl -s http://localhost:8080/" 2>/dev/null \
     | sed 's/^  ✓ orchestrator: //'
  ok "orchestrator serving with MODEL_NAME=$1"
}

do_status() {
  c "SPARK STACK STATUS"
  cur=$(current_model); [ -n "$cur" ] && ok "served model: $cur" || warn "no model on :8000"
  res=$(gpu_reserved_line); [ -n "$res" ] && ok "ray GPUs: $res"
  C=$(container_of "$NODE1"); [ -n "$C" ] && ok "node1 container: $C"
  C2=$(container_of "$NODE2"); [ -n "$C2" ] && ok "node2 container: $C2"
  "${SSH[@]}" "$NODE1" "curl -s -m4 http://localhost:8080/" 2>/dev/null \
    | sed 's/  */ /g' | sed 's/^/  orchestrator: /'
  echo "  --- cluster telemetry ---"
  "${SSH[@]}" "$NODE1" "curl -s -m6 http://localhost:8080/api/telemetry" 2>/dev/null \
    | python3 ~/ai-dev-arena/scripts/_status_telem_print.py 2>/dev/null
  echo; ok "done"
}

do_swap() {  # $1 = target key (gptoss|nemotron)
  local target=$1 want served cur
  want="${LAUNCHER[$target]}"; served="${SERVED[$target]}"
  cur=$(current_model)
  if [ -n "$cur" ] && echo "$cur" | grep -qE "$served|${target}"; then
    ok "already serving $target ($cur) — just repointing orchestrator"
    restart_orchestrator "$served"; do_status; return 0
  fi

  # 1) clean stop
  stop_vllm_clean

  # 2) ensure cluster healthy (free GPUs) — fast path or full-restart fallback
  ok "stopped. checking GPU availability..."
  sleep 6
  if ! cluster_is_healthy; then
    # give it a couple more seconds, then recover hard
    for i in 1 2 3; do c "re-check GPUs ($i/3)"; sleep 5; cluster_is_healthy && break; done
  fi
  if ! cluster_is_healthy; then
    full_cluster_restart || { err "cannot bring cluster up; aborting (model NOT swapped)"; return 1; }
  fi

  # 3) launch target
  c "launching $target via ~/$want (tensor-parallel-2 across both Sparks)..."
  local LOG="~/vllm-${target}.log"
  "${SSH[@]}" "$NODE1" "tmux kill-session -t vllm-serve 2>/dev/null; \
     tmux new-session -d -s vllm-serve 'bash ~/$want 2>&1 | tee $LOG'; true"
  c "loading model across both Sparks (2–4 min)..."
  wait_for_model "$served" || return 1

  # 4) repoint + restart orchestrator
  restart_orchestrator "$served"
  echo; ok "MODEL SWAP COMPLETE -> $served"
  do_status
}

case "${1:-}" in
  status)   do_status ;;
  gptoss|nemotron) do_swap "$1" ;;
  swap)
    cur=$(current_model)
    if echo "${cur:-}" | grep -qi "nemotron"; then do_swap gptoss; else do_swap nemotron; fi ;;
  ""|help|-h|--help)
    echo "usage: $0 {status|gptoss|nemotron|swap}"; exit 0;;
  *) err "unknown command: $1"; echo "usage: $0 {status|gptoss|nemotron|swap}"; exit 2;;
esac
