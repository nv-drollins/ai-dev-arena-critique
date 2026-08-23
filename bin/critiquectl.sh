#!/usr/bin/env bash
# critiquectl.sh — control the TWO-engine writer+critic stack from the HEAD.
#
#   critiquectl.sh status                 both engines + orchestrator + cluster
#   critiquectl.sh start writer|critic|both
#   critiquectl.sh stop  writer|critic|both     (clean SIGTERM, releases GPUs)
#   critiquectl.sh restart writer|critic|both
#   critiquectl.sh orch                   (re)start orchestrator with CRITIC_ENABLED=1
#   critiquectl.sh logs writer|critic|orch
#
# Depends on: a healthy Ray cluster (bring it up with bin/sparkctl.sh start).
# The writer runs TP=1 on WRITER_HOST_SPARK; the critic runs TP=2 across both.
# Both engines run inside the per-node Ray container; ports keep them distinct.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=bin/arena.conf
. "$HERE/arena.conf"

SSH=(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 -o BatchMode=yes)
HEAD_SSH="${SPARK_HEAD##*=}"           # nvidia@192.168.1.159
WRITER_SSH="nvidia@${WRITER_HOST_SPARK}"

on_head()   { "${SSH[@]}" "$HEAD_SSH" "$@"; }
on_writer() { "${SSH[@]}" "$WRITER_SSH" "$@"; }

served_on() {  # $1 ssh target, $2 port -> served model id or empty
  "${SSH[@]}" "$1" "curl -s -m4 http://localhost:$2/v1/models" 2>/dev/null \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"][0]["id"])' 2>/dev/null
}

# ---- status ----------------------------------------------------------------
cmd_status() {
  c "CRITIQUE STACK STATUS"
  local w cr
  w=$(served_on "$WRITER_SSH" "$WRITER_PORT")
  [ -n "$w" ] && ok "writer  :$WRITER_PORT ($WRITER_HOST_SPARK)  $w" \
             || warn "writer  :$WRITER_PORT ($WRITER_HOST_SPARK)  DOWN"
  cr=$(served_on "$HEAD_SSH" "$CRITIC_PORT")
  [ -n "$cr" ] && ok "critic  :$CRITIC_PORT (TP=$CRITIC_TP, both Sparks)  $cr" \
              || warn "critic  :$CRITIC_PORT  DOWN"
  local orch; orch=$(on_head "curl -s -m4 http://localhost:$ORCH_PORT/" 2>/dev/null | tr -d '\n' | head -c 50)
  [ -n "$orch" ] && ok "orchestrator :$ORCH_PORT  ${orch}…" || warn "orchestrator: down"
  # is the orchestrator actually running with the critic enabled?
  local ce; ce=$(on_head "curl -s -m4 http://localhost:$ORCH_PORT/api/config 2>/dev/null" | grep -o '\"critic_enabled\":[^,}]*' 2>/dev/null)
  [ -n "$ce" ] && ok "orchestrator config: $ce"
}

# ---- start -----------------------------------------------------------------
_launch_writer() {
  # Writer runs its OWN container (arena-writer, v0.27.1 image) — it does NOT
  # need the Ray container. launch-writer.sh ssh-hops to WRITER_HOST_SPARK itself.
  if [ -n "$(served_on "$WRITER_SSH" "$WRITER_PORT")" ]; then ok "writer already serving — skip"; return 0; fi
  c "launching WRITER ($WRITER_SERVED, TP=1, own v0.27.1 container) on $WRITER_HOST_SPARK…"
  on_writer "cd ~/ai-dev-arena-critique && tmux kill-session -t writer 2>/dev/null; \
    tmux new-session -d -s writer 'WRITER_HOST_SPARK=$WRITER_HOST_SPARK bash bin/launch-writer.sh 2>&1 | tee ~/writer.log; sleep 86400'"
  _wait_served "$WRITER_SSH" "$WRITER_PORT" "writer"
}
_launch_critic() {
  local box; box=$(on_head "docker ps --format '{{.Names}}' | grep -E '^node-[0-9]+\$' | head -1")
  [ -n "$box" ] || { err "no Ray container on head — start the cluster first"; return 1; }
  if [ -n "$(served_on "$HEAD_SSH" "$CRITIC_PORT")" ]; then ok "critic already serving — skip"; return 0; fi
  c "launching CRITIC ($CRITIC_SERVED, TP=$CRITIC_TP across both Sparks)… (2–5 min load)"
  on_head "cd ~/ai-dev-arena-critique && tmux kill-session -t critic 2>/dev/null; \
    tmux new-session -d -s critic 'bash bin/launch-critic.sh 2>&1 | tee ~/critic.log; sleep 86400'"
  _wait_served "$HEAD_SSH" "$CRITIC_PORT" "critic"
}
_wait_served() {  # $1 ssh, $2 port, $3 label
  local i cur
  for i in $(seq 1 $((MODEL_READY_TIMEOUT/6))); do
    cur=$(served_on "$1" "$2")
    [ -n "$cur" ] && { ok "$3 ready: $cur ($((i*6))s)"; return 0; }
    c "waiting for $3 … ($((i*6))s/${MODEL_READY_TIMEOUT}s)"; sleep 6
  done
  err "$3 did not become ready in ${MODEL_READY_TIMEOUT}s (logs: critiquectl.sh logs $3)"; return 1
}

# ---- stop (clean SIGTERM so GPUs release back to Ray) ----------------------
_stop_engine() {  # $1 ssh, $2 label, $3 tmux-session, $4 vllm-match
  c "stopping $2 (clean SIGTERM, releasing GPUs)…"
  local box; box=$("${SSH[@]}" "$1" "docker ps --format '{{.Names}}' | grep -E '^node-[0-9]+\$' | head -1")
  [ -n "$box" ] && "${SSH[@]}" "$1" "docker exec $box bash -lc '\
    pkill -TERM -f \"$4\"; sleep 10; pkill -9 -f \"$4\"' 2>/dev/null; true"
  "${SSH[@]}" "$1" "tmux kill-session -t $3 2>/dev/null; true"
  ok "$2 stopped"
}
_stop_writer() {  # writer is its OWN container (arena-writer) on WRITER_HOST_SPARK
  c "stopping writer (docker rm arena-writer)…"
  "${SSH[@]}" "$WRITER_SSH" "docker rm -f arena-writer 2>/dev/null; tmux kill-session -t writer 2>/dev/null; true"
  ok "writer stopped"
}

# ---- orchestrator with critic enabled --------------------------------------
_start_orch() {
  c "starting orchestrator with CRITIC_ENABLED=1 (writer→critic pipeline)…"
  on_head "cd ~/ai-dev-arena-critique && fuser -k $ORCH_PORT/tcp >/dev/null 2>&1; sleep 2; \
    WRITER_URL=http://$WRITER_HOST_SPARK:$WRITER_PORT WRITER_MODEL=$WRITER_SERVED \
    CRITIC_URL=http://localhost:$CRITIC_PORT CRITIC_MODEL=$CRITIC_SERVED \
    CRITIC_ENABLED=1 \
    nohup .venv/bin/uvicorn orchestrator.main:app --host 0.0.0.0 --port $ORCH_PORT \
      >> ~/uvicorn.log 2>&1 & disown; sleep 4; curl -s http://localhost:$ORCH_PORT/ | head -c 40" >/dev/null
  ok "orchestrator up (critic enabled)"
}

cmd_logs() {
  local t="${1:?usage: critiquectl.sh logs <writer|critic|orch>}"
  case "$t" in
    writer) on_writer "tail -n 60 ~/writer.log" ;;
    critic) on_head   "tail -n 60 ~/critic.log" ;;
    orch)   on_head   "tail -n 60 ~/uvicorn.log" ;;
    *) err "unknown log: $t"; exit 2 ;;
  esac
}

case "${1:-help}" in
  status) cmd_status ;;
  start)  case "${2:-both}" in
            writer) _launch_writer ;;
            critic) _launch_critic ;;
            both|"") _launch_writer; _launch_critic; _start_orch ;;
            *) err "start: writer|critic|both"; exit 2;; esac ;;
  stop)   case "${2:-both}" in
            writer) _stop_writer ;;
            critic) _stop_engine "$HEAD_SSH" critic critic "vllm serve" ;;
            both|"") _stop_writer
                     _stop_engine "$HEAD_SSH" critic critic "vllm serve" ;;
            *) err "stop: writer|critic|both"; exit 2;; esac ;;
  restart) shift; "$0" stop "${1:-both}"; sleep 4; "$0" start "${1:-both}" ;;
  orch)   _start_orch ;;
  logs)   shift; cmd_logs "${1:-}" ;;
  help|-h|--help|"") sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) err "unknown: $1"; echo "usage: critiquectl.sh {status|start|stop|restart|orch|logs}"; exit 2 ;;
esac
