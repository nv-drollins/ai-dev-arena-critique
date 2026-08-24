"""live_apps.py — spawn the REAL sample Flask app (before + after the AI's edit)
as live web servers, so the Arena can show the actual application changing.

Per session we run up to two instances:
  * BEFORE — the pristine baseline app (git checkout of the challenge start state)
  * AFTER  — the app.py as it stands after the writer applied its patch

Each runs `python app.py` (which starts Flask on $PORT) in its own process.
Ports are allocated from a pool. Processes are tracked and reaped on stop.
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

# port pool for live app instances (before/after × a few concurrent sessions)
_PORT_BASE = int(os.environ.get("LIVE_APP_PORT_BASE", "5100"))
_PORT_MAX = _PORT_BASE + 40

# session_id -> {"before": {proc, port}, "after": {proc, port}}
_LIVE: dict[str, dict] = {}


def _free_port() -> int:
    for p in range(_PORT_BASE, _PORT_MAX):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:   # nothing listening
                return p
    raise RuntimeError("no free port in live-app pool")


def _spawn(app_dir: Path, port: int) -> subprocess.Popen:
    env = dict(os.environ, PORT=str(port))
    # Use the head venv's python (has flask). app.py with no args → app.run().
    py = sys.executable
    log = open(app_dir / f"live-{port}.log", "w")
    return subprocess.Popen([py, "app.py"], cwd=str(app_dir),
                            env=env, stdout=log, stderr=log)


def _wait_ready(port: int, timeout: float = 12.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


def start_after(session_id: str, work_dir: Path) -> dict:
    """Start (or restart) the AFTER instance from the session's current app.py."""
    _stop_one(session_id, "after")
    app_dir = Path(work_dir) / "sample-app"
    port = _free_port()
    proc = _spawn(app_dir, port)
    ready = _wait_ready(port)
    _LIVE.setdefault(session_id, {})["after"] = {"proc": proc, "port": port}
    return {"port": port, "ready": ready}


def start_before(session_id: str, work_dir: Path) -> dict:
    """Start the BEFORE instance from a pristine copy of the challenge baseline.

    The baseline is the git-committed start state of the session repo. We stash
    a clean copy under sample-app-before/ so edits to the live AFTER app never
    affect it.
    """
    _stop_one(session_id, "before")
    import shutil
    src = Path(work_dir) / "sample-app"
    before_dir = Path(work_dir) / "sample-app-before"
    # Always (re)build the BEFORE tree so it reflects the true pristine baseline,
    # never the writer's edits. Copy the session tree for structure, then restore
    # app.py + tests to the git baseline (HEAD = pre-edit "baseline" commit).
    if before_dir.exists():
        shutil.rmtree(before_dir, ignore_errors=True)
    shutil.copytree(src, before_dir, dirs_exist_ok=True)
    # Restore the pristine app.py/tests from the baseline commit. The session repo
    # committed "baseline" BEFORE the writer touched anything, so HEAD is pristine.
    restored = False
    try:
        r = subprocess.run(["git", "checkout", "HEAD", "--", "app.py", "tests/"],
                           cwd=str(before_dir), capture_output=True, text=True, timeout=10)
        restored = (r.returncode == 0)
    except Exception:
        restored = False
    # Fallback: if git restore failed (e.g. .git didn't copy), pull the pristine
    # app.py straight from the source repo that sessions are cloned from.
    if not restored:
        from pathlib import Path as _P
        pristine = _P(__file__).parent.parent / "challenge-repos" / "sample-app" / "app.py"
        if pristine.exists():
            shutil.copy2(pristine, before_dir / "app.py")
    port = _free_port()
    proc = _spawn(before_dir, port)
    ready = _wait_ready(port)
    _LIVE.setdefault(session_id, {})["before"] = {"proc": proc, "port": port}
    return {"port": port, "ready": ready}


def status(session_id: str) -> dict:
    """Return {before:{port,alive}, after:{port,alive}} for a session."""
    out = {}
    for role in ("before", "after"):
        inst = _LIVE.get(session_id, {}).get(role)
        if inst and inst["proc"].poll() is None:
            out[role] = {"port": inst["port"], "alive": True}
        else:
            out[role] = None
    return out


def _stop_one(session_id: str, role: str):
    inst = _LIVE.get(session_id, {}).get(role)
    if inst:
        try:
            inst["proc"].terminate()
            inst["proc"].wait(timeout=5)
        except Exception:
            try:
                inst["proc"].kill()
            except Exception:
                pass
        _LIVE[session_id][role] = None


def stop(session_id: str):
    for role in ("before", "after"):
        _stop_one(session_id, role)
    _LIVE.pop(session_id, None)


def stop_all():
    for sid in list(_LIVE.keys()):
        stop(sid)
