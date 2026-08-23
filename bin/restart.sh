#!/usr/bin/env bash
# restart.sh — stop + start.
#
#   restart.sh              restart the model (clean vLLM stop -> relaunch -> orch)
#   restart.sh model        (same as above — default)
#   restart.sh cluster      full Ray teardown + rebuild + model + orchestrator
#   restart.sh all          full stop then full start
#   restart.sh orch         restart only the orchestrator
#
# The DEFAULT ('model') is the normal "reboot the demo" path: it keeps the Ray
# cluster up, cleanly stops vLLM (releasing its GPUs), relaunches the model,
# and restarts the orchestrator.
set -euo pipefail
cd "$(dirname "$0")/../"
exec bash bin/sparkctl.sh restart "${1:-model}"
