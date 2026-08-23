#!/usr/bin/env bash
# start.sh — bring the whole cluster up (idempotent; safe to re-run).
# Thin wrapper around sparkctl.sh so start/stop/restart are obvious named scripts.
set -euo pipefail
cd "$(dirname "$0")/../"
exec bash bin/sparkctl.sh start all
