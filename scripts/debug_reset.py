#!/usr/bin/env python3
"""Debug: test the full run_agent flow manually."""
import sys, os, time, subprocess, shutil, json
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent  # ai-dev-arena
REPOS_DIR = BASE_DIR / "challenge-repos"
SESSION_WORK_DIR = BASE_DIR / ".sessions"
CHALLENGES_DIR = BASE_DIR / "orchestrator" / "challenges"

# Load challenge A
with open(CHALLENGES_DIR / "challenge_a_feature_sprint.json") as f:
    challenge = json.load(f)

print("Challenge A loaded:", challenge["id"])

# Test reset_repo
session_id = "debug_test"
work_dir = SESSION_WORK_DIR / session_id
repo_src = REPOS_DIR / "sample-app"

print(f"repo_src exists: {repo_src.exists()}")
print(f"repo_src contents: {[p.name for p in repo_src.iterdir()]}")

if work_dir.exists():
    shutil.rmtree(work_dir)

print(f"Coping {repo_src} → {work_dir}")
shutil.copytree(repo_src, work_dir)
print(f"Copied. Contents: {[p.name for p in work_dir.iterdir()]}")

print(f"cd to {work_dir}/sample-app and git init...")
sample = work_dir / "sample-app"
print(f"sample exists: {sample.exists()}")
print(f"sample contents: {[p.name for p in sample.iterdir()]}")

r = subprocess.run(["git", "init"], cwd=sample, capture_output=True, text=True)
print(f"git init: rc={r.returncode}, {r.stdout}, {r.stderr}")

r = subprocess.run(["git", "add", "."], cwd=sample, capture_output=True, text=True)
print(f"git add: rc={r.returncode}")

r = subprocess.run(["git", "commit", "-m", "baseline"], cwd=sample, capture_output=True, text=True)
print(f"git commit: rc={r.returncode}, stderr={r.stderr[:200]}")

# Test reading files
app_py = sample / "app.py"
print(f"app.py exists: {app_py.exists()}, size: {app_py.stat().st_size}")

test_py = sample / "tests" / "test_app.py"
print(f"test_app.py exists: {test_py.exists()}, size: {test_py.stat().st_size}")

# Cleanup
shutil.rmtree(work_dir)
print("Debug test complete - all good!")
