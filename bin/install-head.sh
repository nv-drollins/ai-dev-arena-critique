#!/usr/bin/env bash
# =============================================================================
# install-head.sh — one-shot install of AI Dev Arena on the HEAD Spark.
#
# What it does (in order):
#   0) checks required prereqs (python3, docker, nvidia, tmux, git, ssh-keygen)
#   1) pulls the vLLM Docker image (if not already present, ~30 GB)
#   2) ensures the Nemotron parser file exists at $PARSER_FILE
#   3) creates the ai-dev-arena checkout (or uses the existing one)
#   4) installs the Python venv + requirements
#   5) starts the Ray head (tmux session "ray-head", container "node-XXXX")
#   6) waits for the worker to connect
#   7) launches the default model (gpt-oss-120b) on the Ray cluster
#   8) starts the Arena orchestrator (uvicorn on :8080)
#
# After this:
#   operator  http://<head-lan-ip>:8080/operator
#   arena     http://<head-lan-ip>:8080/arena
#   theater   http://<head-lan-ip>:8080/theater
#
# If the worker has NOT been installed yet, step 6 will fail cleanly and the
# script will print a big "install the worker next" banner. This is expected:
# install-head.sh and install-worker.sh are designed to run in parallel on
# two different Sparks.
#
# Idempotent: safe to re-run any number of times; existing state is reused.
#
# Override any variable from bin/arena.conf by exporting it first, e.g.:
#   VLLM_IMAGE=nvcr.io/nvidia/vllm:26.04-py3 bash install-head.sh
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."                       # repo root
# shellcheck source=bin/arena.conf
. ./bin/arena.conf

# --- 0. prereqs --------------------------------------------------------------
step() { printf '\n\033[1;36m==>\033[0m %s\n' "$1"; }
ok "installing on the HEAD Spark (role: ${SPARK_ROLE})"

need() { command -v "$1" >/dev/null 2>&1 || { err "required binary '$1' not found on PATH"; exit 3; }; }
need python3
need docker
need tmux
need git
need curl
[ -d /proc/driver/nvidia ] || { err "no NVIDIA driver visible under /proc/driver/nvidia"; exit 3; }
ok "prereqs present: python3/docker/tmux/git/curl + NVIDIA driver"

# Hermes Agent drives the writer as an autonomous agent (agentic mode). We install
# it and set up the profile automatically further down (step 3b) once the writer's
# host/port are known from arena.conf. Replay mode works without it.

step "1. docker: pull vLLM image (if not already cached)"
if docker image inspect "$VLLM_IMAGE" >/dev/null 2>&1; then
  ok "image $VLLM_IMAGE is already present"
elif docker pull "$VLLM_IMAGE" >/dev/null 2>&1 || \
     [ "$(gh auth status 2>/dev/null | grep -c logged)" -ge 1 ]; then
  ok "image $VLLM_IMAGE pulled"
else
  warn "docker pull of $VLLM_IMAGE failed — do you have access to nvcr.io?"
  warn "if you do, run:  docker login nvcr.io  &&  docker pull $VLLM_IMAGE"
  exit 4
fi

step "2. parser file"
if [ ! -f "$PARSER_FILE" ]; then
  src="$(find . -name 'super_v3_reasoning_parser.py' 2>/dev/null | head -1)"
  if [ -n "$src" ]; then
    mkdir -p "$(dirname "$PARSER_FILE")"
    cp "$src" "$PARSER_FILE"
    ok "copied parser -> $PARSER_FILE"
  else
    warn "no parser file found in the repo — Nemotron model will not work"
    warn "gpt-oss is fine. (See docs/CLUSTER_OPS.md #known-pitfalls.)"
  fi
else
  ok "parser already at $PARSER_FILE"
fi

step "3. python venv + requirements"
[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
# idempotent: skip pip if the venv already has the deps (avoids network on re-run
# and a hard-fail if requirements.txt is absent on a pre-existing install)
if python - <<'PY'
try:
    import fastapi, uvicorn, aiohttp, pytest
except Exception:
    raise SystemExit(1)
PY
then
  ok "venv already has deps — skipping pip"
else
  [ -f requirements.txt ] || { err "requirements.txt not found (expected in a fresh clone)"; exit 3; }
  python -m pip install --quiet --upgrade pip wheel
  python -m pip install --quiet -r requirements.txt
  ok "venv + deps installed"
fi

# --- 3b. Hermes Agent + agentic profile --------------------------------------
# Install Hermes (if missing) and create the profile that drives the writer as an
# agent. Idempotent: re-running is safe. Reads writer host/port from arena.conf.
# Resolve a hermes command: PATH first, then the standard venv install location.
hermes_cmd() {
  if command -v hermes >/dev/null 2>&1; then echo "hermes";
  elif [ -x "$HOME/.hermes/hermes-agent/venv/bin/python" ]; then
    echo "$HOME/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main";
  else echo ""; fi
}
HERMES="$(hermes_cmd)"
if [ -z "$HERMES" ]; then
  step "3b. install Hermes Agent (agentic mode needs it)"
  if curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash; then
    export PATH="$HOME/.local/bin:$PATH"; hash -r 2>/dev/null || true
    HERMES="$(hermes_cmd)"
    [ -n "$HERMES" ] && ok "Hermes Agent installed" \
      || warn "Hermes installed but not found yet — open a new shell and re-run install-head.sh to finish the profile"
  else
    warn "Hermes install failed — install manually, then re-run this script:"
    warn "    curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"
  fi
fi

if [ -n "$HERMES" ]; then
  step "3b. Hermes profile '${HERMES_PROFILE}' -> writer ${WRITER_HOST_SPARK}:${WRITER_PORT}"
  $HERMES profile create "$HERMES_PROFILE" >/dev/null 2>&1 || true   # no-op if it exists
  hset() { $HERMES config set "$1" "$2" --force -p "$HERMES_PROFILE" >/dev/null 2>&1 || true; }
  hset model.provider        custom
  hset model.base_url        "http://${WRITER_HOST_SPARK}:${WRITER_PORT}/v1"
  hset model.default         "$WRITER_SERVED"
  hset model.api_key         dummy-key
  hset model.context_length  "$HERMES_CONTEXT_LEN"
  hset model.max_tokens      "$HERMES_MAX_TOKENS"
  hset agent.max_turns       "$HERMES_MAX_TURNS"
  hset auxiliary.compression.context_length "$HERMES_CONTEXT_LEN"
  hset auxiliary.compression.max_tokens     "$HERMES_MAX_TOKENS"
  ok "Hermes profile '${HERMES_PROFILE}' ready — agentic mode good to go"
else
  warn "skipping Hermes profile (Hermes not available) — agentic mode unavailable; replay still works"
fi

step "4. stage helper scripts into ~/"
# The launch scripts (cluster/launch-*.sh) assume they run from $HOME and
# reference ~/run_cluster.sh, ~/start-ray-*.sh. Stage the ones we need.
for s in run_cluster.sh start-ray-head.sh start-ray-worker.sh launch-gptoss.sh launch-nemotron-super.sh; do
  [ -f "cluster/$s" ] && cp -f "cluster/$s" ~/
done
[ -f cluster/parsers/super_v3_reasoning_parser.py ] && \
  mkdir -p ~/nemotron-super && cp -f cluster/parsers/super_v3_reasoning_parser.py ~/nemotron-super/
ok "cluster helper scripts staged in ~/"

step "4b. start Ray HEAD"
if docker ps --format '{{.Names}}' | grep -qE '^node-[0-9]+$'; then
  existing_box=$(docker ps --format '{{.Names}}' | grep -E '^node-[0-9]+$' | head -1)
  warn "already have a running Ray container: ${existing_box} — skipping"
  ok "skipping (idempotent)"
else
  # Clean any dead leftovers first
  docker ps -aq --filter name=node- | xargs -r docker rm -f >/dev/null 2>&1 || true
  # start-ray-head.sh runs run_cluster.sh which traps EXIT and would kill the
  # container when the shell leaves — so we run it under a tmux session.
  tmux kill-session -t ray-head 2>/dev/null || true
  tmux new-session -d -s ray-head "bash ~/start-ray-head.sh 2>&1 | tee ~/ray-head.log; sleep 86400"
  # wait up to 60s for the container to appear
  i=0
  while [ $i -lt 12 ]; do
    C=$(docker ps --format '{{.Names}}' | grep -E '^node-[0-9]+$' | head -1)
    [ -n "$C" ] && break
    i=$((i+1)); sleep 5
  done
  [ -n "${C:-}" ] || { err "Ray head did not come up in 60s — try: tmux attach -t ray-head"; exit 5; }
  ok "Ray head container: $C"
fi

step "5. wait for the WORKER to join Ray"
# head's ray status shows /1.0 GPU until the worker joins
C=$(docker ps --format '{{.Names}}' | grep -E '^node-[0-9]+$' | head -1)
cluster_ok=0
for i in $(seq 1 30); do
  line=$(docker exec "$C" ray status 2>/dev/null | grep -oE '[0-9.]+/[0-9.]+ GPU' | head -1)
  if echo "$line" | grep -qE '/[2-9]\.'; then ok "cluster sees ${line} (worker is joined)"; cluster_ok=1; break; fi
  echo "  waiting ... ($i/30, cluster sees: ${line:-?})"
  sleep 4
done
[ "$cluster_ok" = 1 ] || warn "worker not joined — install it next:  ssh <worker> '~/ai-dev-arena/bin/install-worker.sh'"

step "6. launch the default model"
if tmux has-session -t vllm-serve 2>/dev/null; then
  warn "vllm-serve tmux session already running — skipping (use sparkctl.sh restart to force)"
else
  tmux new-session -d -s vllm-serve \
    "bash ~/launch-gptoss.sh 2>&1 | tee ~/vllm-serve.log; sleep 86400"
  i=0
  while [ $i -lt $((MODEL_READY_TIMEOUT/6)) ]; do
    cur=$(curl -s -m3 "http://127.0.0.1:$VLLM_PORT/v1/models" 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"][0]["id"])' 2>/dev/null)
    if [ -n "${cur:-}" ] && echo "$cur" | grep -qi gpt-oss; then
      ok "model ready: $cur"
      break
    fi
    i=$((i+1))
    echo "  waiting ... ($((i*6))s / ${MODEL_READY_TIMEOUT}s — first download of a model can take a while)"
    sleep 6
  done
  [ -n "${cur:-}" ] || { err "model did not come up in ${MODEL_READY_TIMEOUT}s — check /tmp/vllm-serve.log via tmux attach -t vllm-serve"; exit 6; }
fi

step "7. start the Arena orchestrator"
fuser -k $ORCH_PORT/tcp >/dev/null 2>&1; sleep 2
MODEL_NAME="$DEFAULT_MODEL" \
nohup .venv/bin/uvicorn orchestrator.main:app --host 0.0.0.0 --port "$ORCH_PORT" \
  >> ~/uvicorn.log 2>&1 &
disown
sleep 4
if curl -sf "http://127.0.0.1:$ORCH_PORT/" | head -c 80 | grep -qi "AI Dev Arena"; then
  ok "orchestrator is up on :$ORCH_PORT"
else
  err "orchestrator failed — see ~/uvicorn.log"
  exit 7
fi

echo
ok "========================================================"
ok "  HEAD INSTALL COMPLETE"
ok "  operator:  http://$(hostname -I | awk '{print $1}'):$ORCH_PORT/operator"
ok "  arena:     http://$(hostname -I | awk '{print $1}'):$ORCH_PORT/arena"
ok "  theater:   http://$(hostname -I | awk '{print $1}'):$ORCH_PORT/theater"
ok "========================================================"
ok "next steps:"
ok "  * from another machine:  ssh <head> '~/ai-dev-arena/bin/sparkctl.sh status'"
ok "  * to switch models:      '~/ai-dev-arena/bin/sparkctl.sh model nemotron'"
ok "  * to stop everything:    '~/ai-dev-arena/bin/sparkctl.sh stop'"
