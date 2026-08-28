#!/usr/bin/env bash
# bundle-offline.sh — capture everything needed to run the arena WITHOUT internet.
# Run this ON EACH NODE (head and worker) while you still have good connectivity.
# Writes a tarball to $DEST (default: a USB mount). Restore later with
# restore-offline.sh on the freshly-imaged node.
#
#   bash bin/bundle-offline.sh [DEST_DIR]        # e.g. /media/nvidia/USB
#
# What it captures (per node):
#   • the two vLLM Docker images (docker save)          ~40GB
#   • the HuggingFace model cache (~/.cache/huggingface) ~160GB on the node that pulled
#   • the repo working tree incl. .venv                  (this dir)
#   • the Hermes install (~/.hermes)                     head node
#   • staged cluster scripts in ~/ (start-ray-*, run_cluster, nemotron-super)
#   • a manifest of apt/pip so you can tell what's needed
#
# Size: hundreds of GB. Use a fast USB3/NVMe drive. --no-models skips weights if
# you're bundling them separately.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
# shellcheck source=bin/arena.conf
. "$HERE/arena.conf" 2>/dev/null || true

# Default: write into the repo's offline/ folder (created if needed), then copy that
# folder to USB manually — avoids the varying /media/$USER/<vendor>/ mount path.
# Override by passing an explicit DEST (e.g. a mounted USB path) as $1.
DEST="${1:-$REPO/offline}"
SKIP_MODELS=0
[ "${2:-}" = "--no-models" ] && SKIP_MODELS=1

if [ -t 1 ]; then G=$'\033[1;32m'; Y=$'\033[1;33m'; Rr=$'\033[1;31m'; Z=$'\033[0m'; else G= Y= Rr= Z=; fi
say()  { printf '%s==>%s %s\n' "$G" "$Z" "$*"; }
warn() { printf '%s!%s %s\n' "$Y" "$Z" "$*" >&2; }
die()  { printf '%s✗%s %s\n' "$Rr" "$Z" "$*" >&2; exit 1; }

# Create DEST if it doesn't exist (the repo offline/ folder won't exist yet).
mkdir -p "$DEST" || die "cannot create DEST '$DEST'"
# Warn if the target filesystem looks too small — the bundle is hundreds of GB and
# writing it into the repo means this disk needs the room ON TOP of the model cache.
avail_gb=$(df -BG --output=avail "$DEST" 2>/dev/null | tail -1 | tr -dc '0-9')
if [ -n "$avail_gb" ] && [ "$avail_gb" -lt 200 ]; then
  warn "only ${avail_gb}GB free on $DEST — the bundle can be 150-350GB. Free space or"
  warn "pass a USB path instead: bash bin/bundle-offline.sh /media/$USER/<vendor>"
fi
node="$(hostname)"
OUT="$DEST/arena-offline-$node"
mkdir -p "$OUT"
say "bundling node '$node' -> $OUT"
say "(copy this folder to your USB drive afterward)"

# 1. Docker images (both writer + Ray/critic images if present)
say "1/6 docker images (this is slow — ~40GB)"
imgs=()
for img in "${WRITER_IMAGE:-vllm/vllm-openai:v0.27.1}" "${VLLM_IMAGE:-nvcr.io/nvidia/vllm:26.05-py3}"; do
  docker image inspect "$img" >/dev/null 2>&1 && imgs+=("$img") || warn "image $img not present on this node — skipping"
done
if [ "${#imgs[@]}" -gt 0 ]; then
  docker save "${imgs[@]}" | pv 2>/dev/null | gzip -1 > "$OUT/docker-images.tar.gz" 2>/dev/null \
    || docker save "${imgs[@]}" | gzip -1 > "$OUT/docker-images.tar.gz"
  printf '%s\n' "${imgs[@]}" > "$OUT/docker-images.list"
fi

# 2. HF model cache (the big one — only on nodes that pulled weights)
if [ "$SKIP_MODELS" = 0 ] && [ -d "$HOME/.cache/huggingface" ]; then
  say "2/6 HuggingFace model cache (~/.cache/huggingface — can be ~160GB)"
  # vLLM downloads weights AS ROOT inside the container, so some files are root-owned
  # and unreadable to $USER. Use sudo for the tar, then hand the result back to $USER.
  if tar -C "$HOME" -cf "$OUT/hf-cache.tar" .cache/huggingface 2>/dev/null; then
    :  # user could read everything (rare)
  else
    warn "  some weights are root-owned (downloaded by the vLLM container) — using sudo"
    sudo tar -C "$HOME" -cf "$OUT/hf-cache.tar" .cache/huggingface \
      || die "sudo tar of the model cache failed"
    sudo chown "$USER:$USER" "$OUT/hf-cache.tar"
  fi
else
  warn "2/6 skipping model cache (--no-models or none present)"
fi

# 3. The repo working tree, incl .venv (so no pip install needed offline)
say "3/6 repo + .venv"
tar -C "$(dirname "$REPO")" -czf "$OUT/repo.tar.gz" "$(basename "$REPO")"

# 4. Hermes install (head node)
if [ -d "$HOME/.hermes" ]; then
  say "4/6 Hermes install (~/.hermes)"
  tar -C "$HOME" -czf "$OUT/hermes.tar.gz" .hermes
fi

# 5. Staged cluster scripts + parser in ~/
say "5/6 staged ~/ scripts"
tar -C "$HOME" -czf "$OUT/home-scripts.tar.gz" \
  $( for f in run_cluster.sh start-ray-head.sh start-ray-worker.sh nemotron-super restart-orch.sh launch-writer.sh; do [ -e "$HOME/$f" ] && echo "$f"; done ) 2>/dev/null || true

# 6. Manifest — what system packages/pip are present (so you can pre-stage on-site if needed)
say "6/7 manifest"
{
  echo "# node: $node   date: $(date -u +%FT%TZ)"
  echo "# docker images:"; printf '  %s\n' "${imgs[@]:-none}"
  echo "# system python3 packages (for the challenge grader):"
  /usr/bin/python3 -m pip list 2>/dev/null | grep -iE 'pytest|flask' || echo "  (pytest/flask NOT in system python3 — restore installs them from wheels)"
  echo "# apt (tmux/git/curl):"; for b in tmux git curl; do printf '  %s: %s\n' "$b" "$(command -v "$b" || echo MISSING)"; done
} > "$OUT/manifest.txt"

# 7. Grader wheels — the challenge grader runs /usr/bin/python3 -m pytest, so download
# pytest+flask (and deps) as wheels now, for a true offline --no-index install later.
say "7/7 grader wheels (pytest + flask, for offline install)"
mkdir -p "$OUT/wheels"
/usr/bin/python3 -m pip download --dest "$OUT/wheels" pytest flask >/dev/null 2>&1 \
  && say "  wheels saved ($(ls "$OUT/wheels" | wc -l) files)" \
  || warn "  could not download wheels (need internet now) — restore will try the .venv fallback"

sync
say "DONE — node '$node' bundled to $OUT"
du -sh "$OUT" 2>/dev/null
echo
echo "NEXT: copy the whole folder below onto your USB drive (the mount path varies,"
echo "e.g. /media/$USER/<vendor>/ — copy it wherever your USB mounts):"
echo "    $OUT"
echo
echo "At the event, on the matching freshly-imaged node, from the USB:"
echo "    bash <repo>/bin/restore-offline.sh /media/$USER/<vendor>/arena-offline-$node"
