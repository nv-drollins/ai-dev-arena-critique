#!/usr/bin/env bash
# restore-offline.sh — restore an arena bundle onto a freshly-imaged node WITHOUT
# internet. Run on the matching node (head bundle -> head node, worker -> worker),
# after the one-time Docker prerequisite (group + nvidia runtime, then re-login).
#
#   bash bin/restore-offline.sh /media/nvidia/USB/arena-offline-<role>-<node>
#
# It loads the Docker images, unpacks the HF model cache, the repo+.venv, Hermes,
# and the staged ~/ scripts — everything the launchers need, no download required.
set -euo pipefail

if [ -t 1 ]; then G=$'\033[1;32m'; Y=$'\033[1;33m'; Rr=$'\033[1;31m'; Z=$'\033[0m'; else G= Y= Rr= Z=; fi
say()  { printf '%s==>%s %s\n' "$G" "$Z" "$*"; }
warn() { printf '%s!%s %s\n' "$Y" "$Z" "$*" >&2; }
die()  { printf '%s✗%s %s\n' "$Rr" "$Z" "$*" >&2; exit 1; }

SRC="${1:-}"
[ -n "$SRC" ] && [ -d "$SRC" ] || die "usage: bash bin/restore-offline.sh <bundle-dir>  (e.g. /media/$USER/arena-offline-<role>-<node>)"

# prereq sanity (same as the installers require)
command -v docker >/dev/null 2>&1 || die "Docker not installed."
docker info >/dev/null 2>&1 || die "Docker not usable as $USER — do the one-time prereq (usermod -aG docker + re-login) first."

# 1. Docker images
if [ -f "$SRC/docker-images.tar.gz" ]; then
  say "1/5 loading Docker images (slow)"
  gunzip -c "$SRC/docker-images.tar.gz" | docker load
else warn "1/5 no docker-images.tar.gz in bundle — skipping"; fi

# 2. HF model cache (contains root-owned files from the vLLM container — extract with
# sudo so the original ownership is preserved; the container reads them as root).
if [ -f "$SRC/hf-cache.tar" ]; then
  say "2/5 restoring HuggingFace model cache -> ~/.cache/huggingface"
  mkdir -p "$HOME/.cache"
  sudo tar -C "$HOME" -xf "$SRC/hf-cache.tar" || tar -C "$HOME" -xf "$SRC/hf-cache.tar"
else warn "2/5 no hf-cache.tar (models may need to come from the other node or a download)"; fi

# 3. Repo + .venv  (restore next to $HOME unless already present)
if [ -f "$SRC/repo.tar.gz" ]; then
  say "3/5 restoring repo + .venv -> ~/"
  tar -C "$HOME" -xzf "$SRC/repo.tar.gz"
else warn "3/5 no repo.tar.gz — clone the repo manually (needs internet)"; fi

# 4. Hermes
if [ -f "$SRC/hermes.tar.gz" ]; then
  say "4/5 restoring Hermes (~/.hermes)"
  tar -C "$HOME" -xzf "$SRC/hermes.tar.gz"
  # Hermes's venv python is a symlink into ~/.local/share/uv — restore that too, or
  # the agent can't launch (0 tool actions, never touches the model).
  if [ -f "$SRC/uv-python.tar.gz" ]; then
    say "  restoring uv-managed Python + tools (~/.local/share/uv)"
    mkdir -p "$HOME/.local/share"
    tar -C "$HOME" -xzf "$SRC/uv-python.tar.gz"
  fi
  # Verify the venv python actually resolves; self-heal if it doesn't.
  VPY="$HOME/.hermes/hermes-agent/venv/bin/python"
  if "$VPY" --version >/dev/null 2>&1; then
    say "  Hermes venv python OK ($("$VPY" --version 2>&1))"
  else
    warn "  Hermes venv python is broken (symlink target missing)."
    if [ -x "$HOME/.hermes/bin/uv" ]; then
      pyver="$(grep -oE 'version_info = [0-9.]+' "$HOME/.hermes/hermes-agent/venv/pyvenv.cfg" 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)"
      warn "  attempting to rebuild it with uv (needs internet): uv python install ${pyver:-3.11}"
      PATH="$HOME/.hermes/bin:$PATH" uv python install "${pyver:-3.11}" 2>/dev/null \
        && ( "$VPY" --version >/dev/null 2>&1 && say "  venv python rebuilt OK" \
             || warn "  still broken — agentic mode won't work until Hermes is reinstalled" ) \
        || warn "  no internet to rebuild — reinstall Hermes on-site for agentic mode"
    fi
  fi
else warn "4/5 no hermes.tar.gz (head-node bundle only — fine on the worker)"; fi

# 5. Staged ~/ scripts
if [ -f "$SRC/home-scripts.tar.gz" ]; then
  say "5/5 restoring staged ~/ scripts"
  tar -C "$HOME" -xzf "$SRC/home-scripts.tar.gz"
else warn "5/5 no home-scripts.tar.gz"; fi

# grader deps: challenge tests run via /usr/bin/python3 — must have pytest+flask.
# Install from the bundled wheels (fully offline, --no-index).
if ! /usr/bin/python3 -c "import pytest, flask" >/dev/null 2>&1; then
  if [ -d "$SRC/wheels" ] && ls "$SRC/wheels"/*.whl >/dev/null 2>&1; then
    say "installing grader deps (pytest+flask) from bundled wheels (offline)"
    /usr/bin/python3 -m pip install --user --break-system-packages --no-index \
      --find-links "$SRC/wheels" pytest flask 2>/dev/null \
      && say "  grader ready" \
      || warn "  wheel install failed — try: /usr/bin/python3 -m pip install --user --break-system-packages --no-index --find-links $SRC/wheels pytest flask"
  else
    warn "system python3 lacks pytest/flask and no wheels in the bundle."
    warn "  re-run bundle-offline.sh with internet to capture them, or install manually on-site."
  fi
fi

say "RESTORE DONE. Next: bring up the cluster (head Ray, worker Ray join),"
say "then launch-writer / launch-critic / restart-orch — all offline. Verify with:"
say "    bash bin/verify-cluster.sh"
