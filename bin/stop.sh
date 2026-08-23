#!/usr/bin/env bash
# stop.sh — tear the cluster down.
#
#   stop.sh            full teardown (orch -> model -> workers -> head)
#   stop.sh model      stop only vLLM + orchestrator (keeps the Ray cluster warm)
#   stop.sh worker     stop the worker Ray node
#   stop.sh head       stop the head Ray node (drops the cluster)
#
# DESTRUCTIVE: this takes the demo offline. Use 'stop.sh model' for a quick
# "release the GPUs" without killing Ray.
set -euo pipefail
cd "$(dirname "$0")/../"
exec bash bin/sparkctl.sh stop "${1:-all}"
