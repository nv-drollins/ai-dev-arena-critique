#!/usr/bin/env bash
# Start AI Dev Arena orchestrator on Node 1
set -euo pipefail
cd /home/nvidia/ai-dev-arena
pkill -f "uvicorn orchestrator" 2>/dev/null || true
sleep 1
.venv/bin/uvicorn orchestrator.main:app --host 0.0.0.0 --port 8080
