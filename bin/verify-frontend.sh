#!/usr/bin/env bash
# verify-frontend.sh — guard the display pages against stale-session regressions.
# Run from anywhere with the orchestrator up. Exits 0 if the displays will show
# the CURRENT run, non-zero otherwise, with the fix for each ✗.
#
#   bash bin/verify-frontend.sh
#
# WHY THIS EXISTS: both display pages once latched onto the first session they
# saw and never updated again. A booth screen left open showed the first run's
# challenge and score forever — every later run looked stale, and it put a wrong
# score into recorded footage before anyone noticed. Nothing errored; the page
# was simply lying. These checks are cheap and catch it before an audience does.
#
# Checks:
#   1. arena.html  — the discovery poll is never cancelled (it must outlive the
#                    first attach so later runs re-attach)
#   2. live.html   — an explicit ?session_id= wins over /api/running-session
#   3. deployed    — the files being SERVED match this checkout
#   4. live        — /arena, /live, /operator actually respond
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
# shellcheck source=bin/arena.conf
. "$HERE/arena.conf" 2>/dev/null || true
ORCH_PORT="${ORCH_PORT:-8080}"
arena_require_roster || exit 1
HEAD_SSH="${SPARK_HEAD##*=}"
HEAD_HOST="${HEAD_SSH##*@}"
ARENA_DIR="${ARENA_DIR:-~/ai-dev-arena}"

if [ -t 1 ]; then G=$'\033[1;32m'; R=$'\033[1;31m'; Z=$'\033[0m'; else G= R= Z=; fi
pass=0; fail=0
ok()  { printf '  %s✓%s %s\n' "$G" "$Z" "$1"; pass=$((pass+1)); }
bad() { printf '  %s✗%s %s\n' "$R" "$Z" "$1"; [ -n "${2:-}" ] && printf '      fix: %s\n' "$2"; fail=$((fail+1)); }
has() { [ "$(grep -c "$2" "$REPO/frontend/$1")" = "$3" ]; }

echo "── AI Dev Arena frontend health ──"

# 1. arena.html: the poll must never be torn down
if has arena.html 'clearInterval(window._discInterval)' 0; then
  ok "arena: discovery poll is never cancelled"
else
  bad "arena: discovery poll gets cancelled — later runs will show STALE data" \
      "remove every clearInterval(window._discInterval) in frontend/arena.html"
fi
if has arena.html '_discInterval = setInterval' 1; then
  ok "arena: discovery poll is started"
else
  bad "arena: discovery poll missing — the page will never auto-attach" \
      "restore: window._discInterval = setInterval(discover, 2000)"
fi

# 2. live.html: explicit session beats last-run
s_line=$(grep -n "fetch('/api/session/'+SID)"   "$REPO/frontend/live.html" | head -1 | cut -d: -f1)
r_line=$(grep -n "fetch('/api/running-session')" "$REPO/frontend/live.html" | head -1 | cut -d: -f1)
if [ -n "$s_line" ] && [ -n "$r_line" ] && [ "$s_line" -lt "$r_line" ]; then
  ok "live: explicit ?session_id= resolves before /api/running-session"
else
  bad "live: running-session is consulted first — pages mislabel the challenge" \
      "in resolveSession(), look up the explicit SID before /api/running-session"
fi

# 3. what's deployed is what's in git
for f in arena.html live.html; do
  mine=$(md5sum "$REPO/frontend/$f" | cut -d' ' -f1)
  theirs=$(ssh -o BatchMode=yes -o ConnectTimeout=8 "$HEAD_SSH" \
           "md5sum $ARENA_DIR/frontend/$f 2>/dev/null | cut -d' ' -f1" 2>/dev/null)
  if [ -n "$theirs" ] && [ "$mine" = "$theirs" ]; then
    ok "$f deployed matches this checkout"
  else
    bad "$f on the head differs from this checkout (serving old code)" \
        "scp frontend/$f $HEAD_SSH:$ARENA_DIR/frontend/ && restart the orchestrator"
  fi
done

# 4. the pages actually serve
for pg in arena live operator; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m8 "http://${HEAD_HOST}:${ORCH_PORT}/${pg}")
  [ "$code" = 200 ] && ok "GET /$pg → 200" \
    || bad "GET /$pg → ${code:-no response}" "is the orchestrator up? bin/restart-orch.sh"
done

echo "──────────────────────────────────"
if [ "$fail" -eq 0 ]; then
  printf '%s✓ ALL %d CHECKS PASS — displays will follow the current run%s\n' "$G" "$pass" "$Z"
else
  printf '%s✗ %d/%d checks failed — see the fixes above%s\n' "$R" "$fail" "$((pass+fail))" "$Z"
fi
exit $(( fail > 0 ))
