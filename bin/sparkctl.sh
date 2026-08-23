#!/usr/bin/env bash
# =============================================================================
# sparkctl.sh — drive the AI Dev Arena cluster from the HEAD node.
#
#   sparkctl.sh status                model, GPUs, container, orchestrator, telemetry
#   sparkctl.sh start [head|worker|all]    idempotent: bring up whatever's down
#   sparkctl.sh stop   [model|cluster|all] tear down (default: model+orch only, keeps Ray up)
#   sparkctl.sh restart [model|cluster|all|orch]  stop+start, default=model
#   sparkctl.sh model  {gptoss|nemotron}  hot-swap the served model
#   sparkctl.sh attach  <ray-head|ray-worker|vllm-serve>      tmux attach
#   sparkctl.sh logs    <vllm|head|worker|orch>               tail a log
#   sparkctl.sh doctor                          deep diagnostics on both nodes
#
# Everything is idempotent: run any of these any number of times.
#
# This script is the single control plane. install-head.sh / install-worker.sh
# do the one-time prereq + first bring-up, after which you live on sparkctl.sh.
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=bin/arena.conf
. ./bin/arena.conf

# node roster from config
SSH=(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 -o BatchMode=yes)
NODES=()
if [ -n "${SPARK_HEAD:-}" ]; then NODES+=("${SPARK_HEAD##*=}"); fi
if [ -n "${SPARK_WORKERS:-}" ]; then
  for w in $SPARK_WORKERS; do NODES+=("${w##*=}"); done
fi
[ ${#NODES[@]} -ge 1 ] || { err "no nodes configured (SPARK_HEAD / SPARK_WORKERS in bin/arena.conf)"; exit 3; }
HEAD="${NODES[0]}"
WORKERS=("${NODES[@]:1}")
c_host() { docker ps --format '{{.Names}}' | grep -E '^node-[0-9]+$' | head -1; }
run_on() { local node="$1"; shift; "${SSH[@]}" "$node" "$@"; }

# -------- commands ----------------------------------------------------------
cmd_status() {
  c "AI DEV ARENA — STATUS"
  for n in "${NODES[@]}"; do
    local role; [ "$n" = "$HEAD" ] && role="head " || role="worker"
    local host="${n#*@}"
    local box; box=$(run_on "$n" "docker ps --format '{{.Names}}' | grep -E '^node-[0-9]+\$' | head -1") 2>/dev/null
    if [ -n "${box:-}" ]; then
      ok "$role $host  ray: $box"
    else
      warn "$role $host  ray: DOWN"
    fi
  done
  # model + orchestrator (head)
  local cur; cur=$(run_on "$HEAD" "curl -s -m4 http://localhost:$VLLM_PORT/v1/models" 2>/dev/null \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"][0]["id"])' 2>/dev/null)
  [ -n "${cur:-}" ] && ok "model on :$VLLM_PORT  $cur" || warn "model: none on :$VLLM_PORT"
  local orch; orch=$(run_on "$HEAD" "curl -s -m4 http://localhost:$ORCH_PORT/" 2>/dev/null | tr -d '\n' | head -c 60)
  [ -n "${orch:-}" ] && ok "orchestrator :$ORCH_PORT  ${orch}..." || warn "orchestrator: down"
  # telemetry
  run_on "$HEAD" "cd ai-dev-arena && curl -s -m6 http://localhost:$ORCH_PORT/api/telemetry | \
    python3 scripts/_status_telem_print.py" 2>/dev/null | sed 's/^/  /'
  echo
  ok "tip: sparkctl.sh doctor  (deeper)   sparkctl.sh attach vllm-serve"
}

cmd_start() {
  local what="${1:-all}"
  case "$what" in
    head)   _start_head ;;
    worker) for w in "${WORKERS[@]:-}"; do _start_worker "$w"; done ;;
    all|"") _start_head; for w in "${WORKERS[@]:-}"; do _start_worker "$w"; done
           _start_model_if_cluster_up; _start_orch ;;
    model)  _start_model_if_cluster_up ;;
    orch)   _start_orch ;;
    *) err "unknown start target: $what"; exit 2;;
  esac
}

_ray_up() {  # $1 = node ssh target -> already has a running node-* container?
  [ -n "$(run_on "$1" "docker ps --format '{{.Names}}' | grep -E '^node-[0-9]+\$' | head -1" 2>/dev/null)" ]
}
_start_head() {
  if _ray_up "$HEAD"; then ok "head already running — skipping"; return 0; fi
  c "start ray HEAD on $HEAD"
  run_on "$HEAD" "tmux kill-session -t ray-head 2>/dev/null; \
    docker ps -aq --filter name=node- | xargs -r docker rm -f; \
    tmux new-session -d -s ray-head 'bash ~/start-ray-head.sh 2>&1 | tee ~/ray-head.log; sleep 86400'" >/dev/null
  _wait_container "$HEAD" && ok "head container up" || err "head container failed"
}
_start_worker() {
  local w="$1"
  if _ray_up "$w"; then ok "worker ($w) already running — skipping"; return 0; fi
  c "start ray WORKER on $w"
  run_on "$w" "tmux kill-session -t ray-worker 2>/dev/null; \
    docker ps -aq --filter name=node- | xargs -r docker rm -f; \
    tmux new-session -d -s ray-worker 'bash ~/start-ray-worker.sh 2>&1 | tee ~/ray-worker.log; sleep 86400'" >/dev/null
  _wait_container "$w" && ok "worker container up" || err "worker failed"
}
_wait_container() {
  local node="$1" i
  for i in $(seq 1 15); do
    [ -n "$(run_on "$node" "docker ps --format '{{.Names}}' | grep -E '^node-[0-9]+\$' | head -1" 2>/dev/null)" ] && return 0
    sleep 4
  done
  return 1
}

_cluster_ready() {
  local box; box=$(run_on "$HEAD" "docker ps --format '{{.Names}}' | grep -E '^node-[0-9]+\$' | head -1" 2>/dev/null)
  [ -n "${box:-}" ] || return 1
  run_on "$HEAD" "docker exec $box ray status 2>/dev/null" | grep -oE '[0-9.]+/[0-9.]+ GPU' | grep -qE '/[2-9]\.'
}

_start_model_if_cluster_up() {
  tmux has-session -t vllm-serve 2>/dev/null && { ok "model session already running"; return 0; }
  if ! _cluster_ready; then
    warn "cluster not ready yet (need 2+ GPUs) — skipping model; re-run 'sparkctl.sh start model' in ~30s"
    return 1
  fi
  c "launching model (TP=2 across the cluster) — this takes 2–5 min"
  run_on "$HEAD" "tmux kill-session -t vllm-serve 2>/dev/null; \
    tmux new-session -d -s vllm-serve 'bash ~/launch-gptoss.sh 2>&1 | tee ~/vllm-serve.log; sleep 86400'" >/dev/null
  _wait_model 2>/dev/null || warn "timed out waiting for model — use 'sparkctl.sh logs vllm'"
}
_wait_model() {
  local i=0 cur
  while [ $i -lt $((MODEL_READY_TIMEOUT/6)) ]; do
    cur=$(run_on "$HEAD" "curl -s -m3 http://localhost:$VLLM_PORT/v1/models" 2>/dev/null \
      | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"][0]["id"])' 2>/dev/null)
    [ -n "${cur:-}" ] && { ok "model ready: $cur ($((i*6))s)"; return 0; }
    i=$((i+1)); sleep 6
  done
  err "model did not become ready in ${MODEL_READY_TIMEOUT}s"; return 1
}

_start_orch() {
  run_on "$HEAD" "cd ai-dev-arena && fuser -k $ORCH_PORT/tcp 2>/dev/null; sleep 2; \
    MODEL_NAME=$DEFAULT_MODEL nohup .venv/bin/uvicorn orchestrator.main:app --host 0.0.0.0 --port $ORCH_PORT \
      >> ~/uvicorn.log 2>&1 & disown; sleep 5; curl -s http://localhost:$ORCH_PORT/ | head -c 40" >/dev/null
  ok "orchestrator (re)started"
}

cmd_stop() {
  local what="${1:-all}"
  case "$what" in
    orch)   c "stopping orchestrator"; run_on "$HEAD" "fuser -k $ORCH_PORT/tcp" 2>/dev/null || true; ok "orch stopped";;
    model)  c "cleanly stopping vLLM (releases GPUs to Ray)"; \
            run_on "$HEAD" "docker exec \$(docker ps --format '{{.Names}}' | grep -E '^node-[0-9]+\$' | head -1) bash -lc '\
              pkill -TERM -f \"vllm serve\"; sleep 12; pkill -TERM -f \"VLLM::EngineCore\"; sleep 6; \
              pkill -9 -f \"vllm\"; pkill -9 -f \"VLLM::\"' 2>/dev/null; true"; ok "model stopped";;
    worker) for w in "${WORKERS[@]:-}"; do c "stop worker $w"; run_on "$w" "docker exec \$(docker ps --format '{{.Names}}' | grep -E '^node-[0-9]+\$' | head -1) ray stop; docker ps -aq --filter name=node- | xargs -r docker rm -f; tmux kill-session -t ray-worker" 2>/dev/null; ok "worker stopped"; done;;
    head)   c "stop head (DROPS the whole cluster)"; run_on "$HEAD" "docker exec \$(docker ps --format '{{.Names}}' | grep -E '^node-[0-9]+\$' | head -1) ray stop; docker ps -aq --filter name=node- | xargs -r docker rm -f; tmux kill-session -t ray-head" 2>/dev/null; ok "head stopped";;
    all|"")  cmd_stop orch; cmd_stop model; cmd_stop worker; cmd_stop head; ok "FULL TEARDOWN complete";;
    *) err "unknown stop target: $what"; exit 2;;
  esac
}

cmd_restart() {
  local what="${1:-model}"
  c "restart: $what"
  case "$what" in
    model)   cmd_stop model; sleep 4; _ensure_cluster; _start_model_if_cluster_up; _start_orch;;
    cluster) cmd_stop worker; cmd_stop head; sleep 3; _start_head; for w in "${WORKERS[@]:-}"; do _start_worker "$w"; done
             _cluster_wait; _start_model_if_cluster_up; _start_orch;;
    all)     cmd_stop all; cmd_start all;;
    orch)    cmd_stop orch; _start_orch;;
    *) err "unknown restart target: $what"; exit 2;;
  esac
  ok "restart complete"
}
_cluster_wait() {
  local i=0
  until _cluster_ready; do
    [ $i -ge 30 ] && { err "cluster not ready in 120s"; return 1; }; sleep 4; i=$((i+1))
  done
}
_ensure_cluster() { _cluster_ready && return 0; warn "cluster not ready — bringing head+workers up"; cmd_start head; for w in "${WORKERS[@]:-}"; do cmd_start worker "$w" 2>/dev/null || run_on "$w" "tmux new-session -d -s ray-worker 'bash ~/start-ray-worker.sh 2>&1 | tee ~/ray-worker.log; sleep 86400'" >/dev/null; done; _cluster_wait; }

cmd_model() {
  local target="${1:-}"
  [ -n "$target" ] || { err "specify model: gptoss | nemotron"; exit 2; }
  local launcher served
  case "$target" in
    gptoss)   launcher=launch-gptoss.sh;          served=gpt-oss-120b ;;
    nemotron) launcher=launch-nemotron-super.sh;  served=nvidia/nemotron-3-super ;;
    *) err "unknown model: $target (gptoss|nemotron)"; exit 2;;
  esac
  c "hot-swap model -> $served"
  run_on "$HEAD" "cp -f ai-dev-arena/cluster/$launcher ~/"
  # stop current cleanly
  run_on "$HEAD" "docker exec \$(docker ps --format '{{.Names}}' | grep -E '^node-[0-9]+\$' | head -1) bash -lc '\
    pkill -TERM -f \"vllm serve\"; sleep 12; pkill -9 -f \"vllm\"; pkill -9 -f \"VLLM::\"' 2>/dev/null; true"
  # ensure cluster GPUs are free
  _ensure_cluster
  run_on "$HEAD" "tmux kill-session -t vllm-serve 2>/dev/null; \
    tmux new-session -d -s vllm-serve 'bash ~/$launcher 2>&1 | tee ~/vllm-serve.log; sleep 86400'" >/dev/null
  _wait_model || { err "serving $served failed"; exit 1; }
  run_on "$HEAD" "cd ai-dev-arena && fuser -k $ORCH_PORT/tcp 2>/dev/null; sleep 2; \
    MODEL_NAME=$served nohup .venv/bin/uvicorn orchestrator.main:app --host 0.0.0.0 --port $ORCH_PORT >> ~/uvicorn.log 2>&1 & disown; sleep 4" >/dev/null
  ok "MODEL SWAP COMPLETE -> $served"; cmd_status
}

cmd_attach() {
  local t="${1:?usage: sparkctl.sh attach <ray-head|ray-worker|vllm-serve>}"
  local node="$HEAD"
  [ "$t" = "ray-worker" ] && node="${WORKERS[0]:-}"
  err "attach on that node:  ssh ${node#*@} tmux attach -t $t"
  exit 1
}
cmd_logs() {
  local t="${1:?usage: sparkctl.sh logs <vllm|head|worker|orch>}"
  local node="$HEAD" f
  case "$t" in
    vllm)   f=~/vllm-serve.log ;;
    head)   f=~/ray-head.log ;;
    worker) f=~/ray-worker.log; node="${WORKERS[0]:-$HEAD}" ;;
    orch)   f=~/uvicorn.log ;;
    *) err "unknown log target: $t"; exit 2;;
  esac
  run_on "$node" "tail -n 60 $f"
}

cmd_doctor() {
  c "DOCTOR — deep diagnostics"
  for n in "${NODES[@]}"; do
    echo "  ---- ${n%%@*} (${n#*@}) ----"
    run_on "$n" "echo '  nvidia:'; nvidia-smi --query-gpu=name,memory.total,temperature.gpu --format=csv,noheader,nounits 2>/dev/null | sed 's/^/    /'; \
      echo '  ray container:'; docker ps --format '{{.Names}}' | grep -E '^node-[0-9]+\$' | sed 's/^/    /' | head -1; \
      echo '  tmux:'; tmux ls 2>/dev/null | sed 's/^/    /' | head -4; \
      echo '  100GbE link:'; for i in \$(ls /sys/class/net | grep -iE 'enp1s0f|enP7'); do echo \"    \$i \$(cat /sys/class/net/\$i/operstate 2>/dev/null)\"; done" 2>/dev/null
  done
  local box; box=$(run_on "$HEAD" "docker ps --format '{{.Names}}' | grep -E '^node-[0-9]+\$' | head -1" 2>/dev/null)
  [ -n "${box:-}" ] && { echo "  ---- ray status (head) ----"; run_on "$HEAD" "docker exec $box ray status" 2>/dev/null | sed 's/^/    /' | head -25; }
  ok "doctor complete"
}

# -------- dispatch ----------------------------------------------------------
case "${1:-help}" in
  status) cmd_status ;;
  start)  shift; cmd_start "${1:-all}" ;;
  stop)   shift; cmd_stop "${1:-all}" ;;
  restart) shift; cmd_restart "${1:-model}" ;;
  model)  shift; cmd_model "${1:-}" ;;
  attach) shift; cmd_attach "${1:-}" ;;
  logs)   shift; cmd_logs "${1:-}" ;;
  doctor) cmd_doctor ;;
  help|-h|--help|"")
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *) err "unknown command: ${1}"; echo "usage: sparkctl.sh {status|start|stop|restart|model|attach|logs|doctor}"; exit 2;;
esac
