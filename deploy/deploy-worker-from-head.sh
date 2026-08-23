#!/usr/bin/env bash
# =============================================================================
# deploy-worker-from-head.sh
#
# Push the repo (and its worker-relevant files) from the HEAD to the WORKER
# over the high-speed link, then cleanly restart the worker's Ray-vLLM
# container, and verify the worker re-registered with the head.
#
# Usage (from the head):
#   bash deploy/deploy-worker-from-head.sh
#   WORKER_TARGET=nvidia@192.168.1.149 bash deploy/deploy-worker-from-head.sh
#   BRANCH=main bash deploy/deploy-worker-from-head.sh
#
# Env vars (all have sensible defaults for your two-Spark arena):
#   WORKER_TARGET   nvidia@192.168.1.149
#   BRANCH          (current branch of this repo, or "main")
#   FAST_IFNAME     enp1s0f1np1   (the 100GbE link; informational only)
#   RERUN_DOCKER    1             # set 0 to skip docker restart
#   SILENT_SSH      0             # set 1 to add -q to ssh
# =============================================================================
set -euo pipefail

WORKER_TARGET="${WORKER_TARGET:-nvidia@192.168.1.149}"
BRANCH="${BRANCH:-$(git -C "$(dirname "$0")/../" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)}"
FAST_IFNAME="${FAST_IFNAME:-enp1s0f1np1}"
RERUN_DOCKER="${RERUN_DOCKER:-1}"
SILENT_SSH="${SILENT_SSH:-0}"

REPO_ROOT="$(cd "$(dirname "$0")/../" && pwd)"
REPO_NAME="$(basename "$REPO_ROOT")"

SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=6)
if [ "$SILENT_SSH" = "1" ]; then SSH_OPTS+=(-q); fi

step() { printf '\n\033[1;36m%20s\033[0m %s\n' "$1" "$2"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
err()  { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; }

# ---------------------------------------------------------------------------
# 0. sanity
# ---------------------------------------------------------------------------
step "[0/6]" "Sanity checks"
ok "worker target = $WORKER_TARGET"
ok "branch = $BRANCH"
ok "repo = $REPO_ROOT"

# ---------------------------------------------------------------------------
# 1. verify the worker is reachable + is running the expected Docker image
# ---------------------------------------------------------------------------
step "[1/6]" "Verify worker is reachable and has Docker up"
if ! "${SSH_OPTS[@]}" "$WORKER_TARGET" 'echo up' >/dev/null 2>&1; then
  err "worker $WORKER_TARGET not reachable over SSH"; exit 1
fi
ok "SSH to $WORKER_TARGET works"

W_DOCKER_OK="$("${SSH_OPTS[@]}" "$WORKER_TARGET" \
  'docker ps --format "{{.Images}} {{.Names}}" 2>/dev/null | grep -E "^nvcr.io/nvidia/vllm node-[0-9]+" || true' 2>&1)"
W_NODE="$(echo "$W_DOCKER_OK" | awk '{print $NF}')"
if [ -z "${W_NODE:-}" ]; then
  warn "no vLLM/Docker node-* container currently running on worker — will start fresh after push"
else
  ok "worker's current vLLM/ray container: $W_NODE"
fi

# ---------------------------------------------------------------------------
# 2. tar a minimal 'worker subset' from the head and push it over the link
#    (this is the 100GbE push — a few KB, so it takes <0.5s)
# ---------------------------------------------------------------------------
step "[2/6]" "Build + push worker subset over ${FAST_IFNAME}"
SUBSET=(
  "cluster/run_cluster.sh"
  "cluster/start-ray-worker.sh"
  "cluster/parsers"
  "deploy/worker.md"
)
TMP="$(mktemp -d /tmp/arena-deploy.XXXXXX)"
for p in "${SUBSET[@]}"; do
  mkdir -p "$TMP/$(dirname "$p")"
  cp -a "$REPO_ROOT/$p" "$TMP/$p"
done

# ship via stdin over the fast link (tar → ssh)
tar -C "$TMP" -c .    \
  | ssh "${SSH_OPTS[@]}" "$WORKER_TARGET" \
      'set -e; mkdir -p ~/ai-dev-arena; tar -C ~/ai-dev-arena -x -; \
        echo "  worker files:"; cd ~/ai-dev-arena; find cluster -type f; echo; \
        bash -n cluster/run_cluster.sh && bash -n cluster/start-ray-worker.sh && echo "syntax OK"'

ok "subset pushed ($WORKER_TARGET)"

# ---------------------------------------------------------------------------
# 3. (optional) clean-detach the worker's Ray+vLLM container from the head
#    so it can re-register with the fresh config
# ---------------------------------------------------------------------------
step "[3/6]" "Cleanly detach the old Ray-vLLM worker container"
if [ "$RERUN_DOCKER" = "1" ]; then
  # If there are multiple container names pick the first node-*
  "${SSH_OPTS[@]}" "$WORKER_TARGET" bash -s <<'REMOTE'
set +e
for c in $(docker ps --format "{{.Names}}" | grep -E '^node-[0-9]+$'); do
  docker stop "$c" 2>/dev/null
done
exit 0
REMOTE
  ok "stopped old worker container"
else
  warn "skipping docker restart (RERUN_DOCKER=0)"
fi

# ---------------------------------------------------------------------------
# 4. Re-run the worker's Ray+Docker bring-up
# ---------------------------------------------------------------------------
step "[4/6]" "Start the worker container + Ray worker"
"${SSH_OPTS[@]}" "$WORKER_TARGET" 'cd ~/ai-dev-arena && bash cluster/start-ray-worker.sh' &
WORKER_PID=$!
# wait for container to come up (up to 60s)
for i in $(seq 1 12); do
  sleep 5
  W_CONTAINER="$("${SSH_OPTS[@]}" "$WORKER_TARGET" 'docker ps --format "{{.Names}}" | grep -E "^node-[0-9]+$" | head -1')"
  if [ -n "${W_CONTAINER:-}" ]; then break; fi
done
wait "${WORKER_PID}" 2>/dev/null || true
W_CONTAINER="${W_CONTAINER:-none}"
[ "${W_CONTAINER}" != "none" ] && ok "worker container: $W_CONTAINER" || warn "worker did not report a running container in 60s"

# ---------------------------------------------------------------------------
# 5. Verify the worker re-registered with the HEAD's Ray cluster
#    (run from the head — the head's docker has the Ray head)
# ---------------------------------------------------------------------------
step "[5/6]" "Verify worker is registered with head's Ray"
H_CONTAINER="$(docker ps --format "{{.Names}}" | grep -E '^node-[0-9]+$' | head -1)"
if [ -n "${H_CONTAINER:-}" ]; then
  RAY_STATUS="$(docker exec "$H_CONTAINER" ray status 2>&1 | sed -n '/Total Resources/,/^$/p')"
  RAY_NODES="$(echo "$RAY_STATUS" | grep -ci 'Active nodes' || true)"
  TOTAL_GPUS="$(echo "$RAY_STATUS" | grep -oE '[0-9]+\.[0-9]+ GPU' | head -1)"
  echo "  --- ray status (Total Resources) ---"
  echo "$RAY_STATUS" | sed 's/^/  | /'
  echo "  ------------------------------------"
  ok "worker re-registered (total GPUs reported by head: ${TOTAL_GPUS:-?})"
else
  warn "no head docker container visible on this node — skip head-side verify"
fi

# ---------------------------------------------------------------------------
# 6. Print a summary + next step
# ---------------------------------------------------------------------------
step "[6/6]" "Done"
ok "Worker: $WORKER_TARGET  (container: ${W_CONTAINER:-?})"
ok "Branch:  $BRANCH"
ok "Next step on the HEAD:  bash cluster/launch-gptoss.sh  (or launch-nemotron-super.sh)"

rm -rf "$TMP"
