#!/usr/bin/env bash
# check-no-hardcoded-ips.sh — fail if a site-specific IP address has crept into
# the repo. Run it before committing (or from CI).
#
#   bash bin/check-no-hardcoded-ips.sh
#
# WHY: this repo is meant to be portable. Every node address must come from
# bin/arena.conf (overridden by the gitignored bin/arena.conf.local), never
# from a literal baked into a script, a doc, or the orchestrator.
#
# ALLOWED literals (not site-specific):
#   0.0.0.0           bind-all
#   127.0.0.1         loopback
#   255.255.255.x     netmasks
#   192.0.2.x         RFC 5737 TEST-NET-1 — the documentation example range
#   $CLUSTER_SUBNET   the private stacked-Spark link, which is a documented
#                     NVIDIA convention and lives in exactly one place
set -uo pipefail
cd "$(dirname "$0")/.."

# Matches a dotted quad, but not a version string like 1.2.3 (needs 4 octets).
IP_RE='(^|[^0-9.])([0-9]{1,3}\.){3}[0-9]{1,3}([^0-9.]|$)'

ALLOW='0\.0\.0\.0|127\.0\.0\.1|255\.255\.255|192\.0\.2\.|0\.1\.2\.3'

hits="$(grep -rnE "$IP_RE" . \
        --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=.venv \
        --exclude-dir=venv --exclude-dir=__pycache__ \
        --exclude='arena.conf.local' \
        --exclude='check-no-hardcoded-ips.sh' 2>/dev/null \
      | grep -vE "$ALLOW" || true)"

# The one sanctioned default (the private stacked-Spark fast link) is confined to
# bin/arena.conf — allow it there, and nowhere else in the repo.
hits="$(printf '%s\n' "$hits" | grep -vE '^\./bin/arena\.conf:[0-9]+:.*192\.168\.100\.' || true)"
hits="$(printf '%s\n' "$hits" | sed '/^$/d')"

if [ -n "$hits" ]; then
  printf '\033[1;31m✗ hardcoded IP addresses found:\033[0m\n'
  printf '%s\n' "$hits" | sed 's/^/   /'
  printf '\nMove them into bin/arena.conf as ${VAR:-default}, or use a placeholder\n'
  printf 'like <worker-host> / 192.0.2.x in docs.\n'
  exit 1
fi

printf '\033[1;32m✓ no hardcoded IPs\033[0m\n'
