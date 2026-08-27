"""
AI Dev Arena — Orchestrator Backend

Manages demo sessions, agent runs, validation, scoring, and live progress
streaming to the Arena Display via WebSockets.

Endpoints:
  POST /api/session/start   — start a new demo session
  POST /api/session/reset   — reset to baseline state
  POST /api/session/fallback — switch to golden branch (rescue)
  GET  /api/session/{id}    — session status and score
  GET  /api/challenges      — list available challenges
  WS   /ws/arena/{id}       — live progress stream for Arena Display
  WS   /ws/theater/{id}     — live progress stream for Code Theater

Run:
  cd ai-dev-arena && .venv/bin/uvicorn orchestrator.main:app --host 0.0.0.0 --port 8080
"""
import asyncio
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from orchestrator.scoring import score_session

# --- Config ---

BASE_DIR = Path(__file__).parent.parent
CHALLENGES_DIR = BASE_DIR / "orchestrator" / "challenges"
REPOS_DIR = BASE_DIR / "challenge-repos"
RESULTS_DIR = BASE_DIR / "orchestrator" / "results"
SESSION_WORK_DIR = BASE_DIR / ".sessions"

# vLLM endpoints.
# WRITER = fast codegen model (Nemotron-Lightning-30B, one Spark, :8001).
# CRITIC = large review model (Llama-3.3-Nemotron-70B-Feedback, TP=2 both Sparks, :8002).
# VLLM_URL / MODEL_NAME kept as the WRITER aliases for back-compat with the
# inherited single-model code path.
WRITER_URL = os.environ.get("WRITER_URL", os.environ.get("VLLM_URL", "http://127.0.0.1:8001"))
WRITER_MODEL = os.environ.get("WRITER_MODEL", os.environ.get("MODEL_NAME", "nemotron-lightning-30b"))
CRITIC_URL = os.environ.get("CRITIC_URL", "http://127.0.0.1:8002")
CRITIC_MODEL = os.environ.get("CRITIC_MODEL", "llama33-nemotron-70b-feedback")
# When false (default until the critic engine is up), the pipeline runs
# writer→tests→score exactly like the base project and skips the critic call.
CRITIC_ENABLED = os.environ.get("CRITIC_ENABLED", "0") not in ("0", "", "false", "False")
# Back-compat aliases (inherited call_llm reads these):
VLLM_URL = WRITER_URL
MODEL_NAME = WRITER_MODEL

# --- State ---

app = FastAPI(title="AI Dev Arena Orchestrator")
# Mount /static only if the dir exists — StaticFiles raises at import time otherwise,
# which crashed the orchestrator on a fresh clone (git doesn't track empty dirs). The
# frontend is self-contained HTML/JS and doesn't currently use /static, so this is
# purely defensive.
_STATIC_DIR = BASE_DIR / "frontend" / "static"
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

sessions: dict[str, dict] = {}
ws_subscribers: dict[str, list[WebSocket]] = {}
event_buffers: dict[str, list[dict]] = {}


# --- Models ---

class StartRequest(BaseModel):
    challenge_id: str
    mode: str = "live"  # live | guardrailed | replay
    audience: str = "broad"


class ActionResult(BaseModel):
    phase: str
    message: str
    diff: Optional[str] = None
    terminal_output: Optional[str] = None
    files_changed: Optional[list] = None
    score: Optional[dict] = None


# --- Load challenges ---

def load_challenges():
    challenges = {}
    for fp in sorted(CHALLENGES_DIR.glob("challenge_*.json")):
        with open(fp) as f:
            ch = json.load(f)
        challenges[ch["id"]] = ch
    return challenges


CHALLENGES = load_challenges()


def restore_canonical_tests(work_dir: Path):
    """Overwrite the session's tests/ with the pristine canonical tests so a
    writer's broken test-file edit can't corrupt grading. app.py is left alone."""
    src_tests = REPOS_DIR / "sample-app" / "tests"
    dst_tests = work_dir / "sample-app" / "tests"
    if not src_tests.exists():
        return
    try:
        for f in src_tests.iterdir():
            if f.is_file() and f.suffix == ".py":
                shutil.copy2(f, dst_tests / f.name)
    except Exception:
        pass


def _critique_to_feedback(review: dict, test_results: list) -> str:
    """Distill the reviewer's structured critique + failing-test output into a
    compact, actionable feedback string for the writer's repair attempt."""
    if not isinstance(review, dict):
        return ""
    parts = []
    summary = (review.get("summary") or "").strip()
    if summary:
        parts.append(f"Reviewer summary: {summary}")
    for f in (review.get("findings") or [])[:5]:
        if isinstance(f, dict):
            note = f.get("note") or f.get("detail") or f.get("issue") or ""
            kind = f.get("kind") or f.get("severity") or ""
            if note:
                parts.append(f"- [{kind}] {note}" if kind else f"- {note}")
        elif isinstance(f, str):
            parts.append(f"- {f}")
    # Include the tail of any failing test output so the writer sees the exact error.
    for t in test_results:
        if not t.get("passed"):
            out = (t.get("output") or "").strip().splitlines()
            tail = [l for l in out if any(k in l for k in ("Error", "assert", "FAILED", "error"))][-4:]
            if tail:
                parts.append("Failing test output:\n" + "\n".join(tail))
            break
    return "\n".join(parts)[:1800]


def _apply_patches(response: dict, work_dir: Path, py_ok, fuzzy_replace) -> list:
    """Apply search/replace patches from a writer response, guarded so a patch
    that breaks Python (syntax/indent/import) is skipped. Returns the list of
    applied changes [{file, type}]. Shared by the first pass and the repair loop."""
    changes = []
    if not (response and response.get("patches")):
        return changes
    for patch in response["patches"]:
        target_file = patch.get("file", "app.py")
        search_text = patch.get("search", "").rstrip()
        replace_text = patch.get("replace", "").rstrip()
        if not search_text:
            continue
        fp = work_dir / "sample-app" / target_file
        try:
            current = fp.read_text()
            if search_text in current:
                new_content = current.replace(search_text, replace_text, 1)
                if py_ok(target_file, new_content):
                    fp.write_text(new_content)
                    changes.append({"file": target_file, "type": "patch-exact"})
                else:
                    fz = fuzzy_replace(current, search_text, replace_text)
                    if fz and fz != current and py_ok(target_file, fz):
                        fp.write_text(fz)
                        changes.append({"file": target_file, "type": "patch-fuzzy"})
            else:
                fz = fuzzy_replace(current, search_text, replace_text)
                if fz and fz != current and py_ok(target_file, fz):
                    fp.write_text(fz)
                    changes.append({"file": target_file, "type": "patch-fuzzy"})
        except Exception:
            continue
    return changes


# --- Repo management ---

def reset_repo(work_dir: Path, challenge_id: str):
    """Reset sample-app to baseline state."""
    repo_src = REPOS_DIR / "sample-app"
    target = work_dir / "sample-app"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True)
    shutil.copytree(repo_src, target, dirs_exist_ok=True)
    # Init git baseline
    subprocess.run(["git", "init"], cwd=target, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=target, capture_output=True)
    subprocess.run(["git", "config", "user.email", "arena@demo"], cwd=target, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Arena"], cwd=target, capture_output=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=target, capture_output=True)


def apply_golden(work_dir: Path, challenge_id: str, branch: str):
    """Apply golden branch solution.

    `branch` may be "performance" or "golden/performance" — both accepted.
    Golden files live at <repos>/sample-app/golden/<name>/  (NOT a double golden/).
    """
    branch = branch or ""
    if not branch.startswith("golden/"):
        branch = ("golden/" + branch).strip("/") if branch else ""
    target = REPOS_DIR / "sample-app" / branch
    repo_target = work_dir / "sample-app"
    if target.exists():
        for item in target.iterdir():
            dst = repo_target / item.name
            if item.is_dir():
                shutil.copytree(item, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dst)


# --- Agent runtime ---

# Per-audience phrasing: the SAME underlying phases, but worded for who's
# watching. This is a real, visible difference — the Arena/Operator/Theater all
# render these message strings, so "Broad" gets plainer language, "Developers"
# gets implementation detail, and "Executives" gets outcomes.
AUDIENCE_PHRASES = {
    "broad": {
        "inspecting": "Looking around the codebase...",
        "planning":   "Figuring out what to change...",
        "calling_llm":"Thinking it through...",
        "editing":    "Making the change...",
        "testing":    "Checking it works...",
    },
    "developers": {
        "inspecting": "Inspecting repository structure...",
        "planning":   "Drafting the implementation plan...",
        "calling_llm":"Generating patches + test updates...",
        "editing":    "Applying targeted diffs...",
        "testing":    "Running pytest + validation...",
    },
    "executives": {
        "inspecting": "Understanding the requirement...",
        "planning":   "Planning the solution...",
        "calling_llm":"Working the problem...",
        "editing":    "Implementing the fix...",
        "testing":    "Verifying the result...",
    },
}


def audience_word(phase: str, audience: str) -> str:
    """Best-effort phase label for a given audience; falls back neutrally."""
    a = (audience or "broad").lower()
    table = AUDIENCE_PHRASES.get(a, AUDIENCE_PHRASES["broad"])
    return table.get(phase, {"inspecting": "Inspecting...", "planning": "Planning...",
                             "calling_llm": "Thinking...", "editing": "Working...",
                             "testing": "Testing...", "editing": "Implementing..."}.get(phase, phase))


async def run_agent(session_id: str, challenge: dict, work_dir: Path):
    """Run the AI agent against the challenge using vLLM + patch generation.

    Sends progress events via broadcast.
    Returns a result dict with changes, outputs, etc.
    """
    s = sessions[session_id]
    repo_path = str(work_dir / "sample-app")

    # Build the actual prompt
    prompt = challenge["prompt"].replace("{{repo_path}}", repo_path)

    # Track actions
    actions = []
    changes = []
    terminal_outputs = []
    start_time = time.time()

    async def broadcast(phase: str, message: str, **kwargs):
        event = {
            "phase": phase,
            "message": message,
            "elapsed": round(time.time() - start_time, 1),
            **kwargs,
        }
        await _broadcast(session_id, event)

    # Audience-aware wording + demo mode (live / guardrailed / replay)
    audience = (s.get("audience") or "broad").lower()
    word = lambda p: AUDIENCE_PHRASES.get(audience, AUDIENCE_PHRASES["broad"]).get(p)
    mode = (s.get("mode") or "live").lower()

    # Phase 1: Repo inspection
    await broadcast("inspecting", word("inspecting") or "Inspecting the repository...")
    try:
        result = subprocess.run(
            ["find", ".", "-type", "f", "-not", "-path", "./.git/*"],
            cwd=work_dir / "sample-app", capture_output=True, text=True, timeout=10
        )
        file_list = result.stdout.strip()
    except Exception:
        file_list = "app.py, tests/test_app.py, README.md"

    # Read source
    app_py = (work_dir / "sample-app" / "app.py").read_text()
    test_py = (work_dir / "sample-app" / "tests" / "test_app.py").read_text()

    # Phase 2: Planning
    await broadcast("planning", word("planning") or "Creating an implementation plan...", files_changed=[])

    # REPLAY mode — scripted, OFFLINE, no LLM call. Applies the "golden" pacing
    # so you can rehearse the full show flow with no model/network on-site.
    if mode == "replay":
        await broadcast("editing", word("editing") or "Working the solution...")
        await asyncio.sleep(2.0)
        golden_branch = challenge.get("golden_branch", "")
        apply_golden(work_dir, challenge["id"], golden_branch)
        # Record what we just applied so scoring sees real changes
        import shutil as _repl_shutil
        _gdir = REPOS_DIR / "sample-app" / (golden_branch if golden_branch.startswith("golden/") else "golden/" + golden_branch)
        if _gdir.exists():
            for it in _gdir.iterdir():
                if it.is_file():
                    changes.append({"file": it.name, "type": "modified"})
        diff_output = subprocess.run(
            ["git", "diff"], cwd=work_dir / "sample-app",
            capture_output=True, text=True, timeout=10).stdout
        changes_info = [{"file": c["file"], "type": c["type"]} for c in changes]
        await broadcast("edited", "Solution applied (replay).", diff=diff_output, files_changed=changes_info)
        s["human_overrides"] = 0  # scripted run, no interventions
        response = None           # skip the LLM patch block below
    elif mode == "agentic":
        # AGENTIC mode — Hermes drives local Nemotron as an autonomous agent:
        # reads the repo, edits app.py, runs pytest, iterates. It edits files on
        # disk directly, so we set response=None (no patch-application step) and
        # feed the git-detected changes into the same validation/scoring path.
        changes = await run_agentic(work_dir, challenge, broadcast)
        s["human_overrides"] = 0
        response = None
    else:
        # LIVE / GUARRAILED — call the LLM (guarded by this else-branch)
        # Phase 3: Call the LLM — send live heartbeat events
        await broadcast("calling_llm", word("calling_llm") or "Thinking it through (may take 20-40s)...")

        # Start heartbeats so UI updates during the long model wait
        call_task = asyncio.create_task(call_llm(prompt, file_list, app_py, test_py))

        heartbeat_msgs = [
            "🧠 AI dev agent is analyzing the codebase...",
            "📝 Identifying changes needed...",
            "✏️ Generating implementation patches...",
            "🔍 Validating search/replace patterns...",
        ]

        async def send_heartbeats():
            for i, msg in enumerate(heartbeat_msgs):
                await asyncio.sleep(15)
                if call_task.done():
                    return  # Model responded early — stop heartbeats
                try:
                    await broadcast("calling_llm", msg)
                except Exception:
                    pass

        hb_task = asyncio.create_task(send_heartbeats())

        try:
            response = await call_task
        except Exception as e:
            await broadcast("error", f"Model call failed: {e}", terminal_output=str(e)[:500])
            response = None
        finally:
            hb_task.cancel()
            try:
                await hb_task
            except asyncio.CancelledError:
                pass

    # Handle model response — apply patches or full-file replacement.
    # Patch application uses two stages to survive the writer's search-strings
    # quoting code with slightly different whitespace/indent:
    def _normalise_block(text):
        """Collapse vertical whitespace so blocks match even when line-counts differ."""
        import re as _re
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        return "\n".join(lines)

    def _fuzzy_replace(original: str, search_block: str, replace_block: str) -> str | None:
        """Try to locate search_block in original using normalised whitespace
        line-by-line and replace with replace_block.  Returns new text or None."""
        import re as _re
        # Build a normalised "signature" for the search block (one token per non-empty line)
        sig = _normalise_block(search_block).splitlines()
        lines = original.splitlines(True)  # keep \n so replacement lands whole-lines
        best = None; best_pos = best_len = -1

        def score(win):
            # compare normalised window against signature (Levenshtein-ish char ratio)
            w_sig = _normalise_block("".join(win)).splitlines()
            if not w_sig and not sig: return 0.0
            s1, s2 = "".join(w_sig), "".join(sig)
            # fast Jaccard on words
            u1, u2 = set(_re.findall(r"\w+", s1)), set(_re.findall(r"\w+", s2))
            if not (u1 or u2): return 0.5
            return len(u1 & u2) / len(u1 | u2)

        for pos in range(len(lines)):
            for win_len in range(max(2, len(sig)), min(len(sig)+6, len(lines)-pos)+1):
                window = lines[pos:pos+win_len]
                s = score(window)
                if s >= 0.78 and (best is None or (s > best) or (s == best and win_len < best_len)):
                    best = s; best_pos, best_len = pos, win_len

        if best is not None:
            # Re-indent the replacement so its base indent matches the matched
            # location — the writer often quotes the block at column 0 while the
            # real code is nested, which would produce an IndentationError.
            def _base_indent(block_lines):
                for l in block_lines:
                    if l.strip():
                        return len(l) - len(l.lstrip())
                return 0
            matched_indent = _base_indent(lines[best_pos:best_pos+best_len])
            rep_lines = replace_block.splitlines(True)
            rep_indent = _base_indent(rep_lines)
            shift = matched_indent - rep_indent
            if shift > 0:
                rep_lines = [(" " * shift + l if l.strip() else l) for l in rep_lines]
            elif shift < 0:
                cut = -shift
                rep_lines = [(l[cut:] if l[:cut].isspace() else l.lstrip() if l.strip() else l) for l in rep_lines]

            new_lines = lines[:best_pos] + rep_lines + lines[best_pos+best_len:]
            txt = "".join(new_lines)
            if original.endswith("\n") and not txt.endswith("\n"):
                txt += "\n"
            # Safety: never ship a fuzzy result that doesn't parse — fall through
            # to the golden fallback instead of corrupting app.py.
            import ast as _ast
            try:
                _ast.parse(txt)
            except SyntaxError:
                return None
            return txt

        # Last resort — if the replacement is a full-file replacement and search
        # is ~70%+ of the file content, allow direct swap (parse-checked).
        if len(_normalise_block(search_block)) / max(len(_normalise_block(original)),1) > 0.5:
            import ast as _ast
            try:
                _ast.parse(replace_block)
                return replace_block
            except SyntaxError:
                return None

        return None

    def _py_ok(fname: str, content: str) -> bool:
        """True if content is safe to write. For .py files: must parse AND (for
        app.py) import cleanly — catches syntax errors, bad indentation, and
        runtime import failures like Flask's duplicate-endpoint error from a
        writer that adds a route without removing the old one."""
        if not fname.endswith(".py"):
            return True
        import ast as _ast
        try:
            _ast.parse(content)
        except SyntaxError:
            return False
        if fname != "app.py":
            return True   # test files: parse is enough (canonical restore grades)
        # Import-smoke app.py in a subprocess so Flask route/decorator errors surface.
        import tempfile, subprocess, os as _os, sys as _sys
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         dir=str(work_dir / "sample-app")) as tf:
            tf.write(content); tmp = tf.name
        try:
            r = subprocess.run(
                [_sys.executable, "-c", f"import importlib.util,sys;"
                 f"spec=importlib.util.spec_from_file_location('_probe',r'{tmp}');"
                 f"m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)"],
                cwd=str(work_dir / "sample-app"), capture_output=True,
                text=True, timeout=15,
            )
            return r.returncode == 0
        except Exception:
            return False
        finally:
            try: _os.unlink(tmp)
            except OSError: pass

    # Post-apply guard: a valid solution MUST actually change a non-test file.
    # If the model's patch search strings don't match (or it only touched
    # tests), we revert and use the golden fallback rather than ship a
    # test-only "fix" that fails the suite.
    solved = False

    if response and response.get("patches"):
        # Phase 4a: Apply targeted patches — exact first, then fuzzy fallback
        await broadcast("editing", f"Applying {len(response['patches'])} patch(es)...", diff="")
        for patch in response["patches"]:
            target_file = patch.get("file", "app.py")
            search_text = patch.get("search", "").rstrip()
            replace_text = patch.get("replace", "").rstrip()
            if not search_text:
                continue
            fp = work_dir / "sample-app" / target_file
            try:
                current = fp.read_text()
                if search_text in current:
                    # Stage 1: exact substring match (fast path)
                    new_content = current.replace(search_text, replace_text, 1)
                    if _py_ok(target_file, new_content):
                        fp.write_text(new_content)
                        changes.append({"file": target_file, "type": "patch-exact"})
                    else:
                        # exact match but the replacement breaks Python — try fuzzy
                        # (which re-indents), else skip so we don't ship broken code
                        fuzzy = _fuzzy_replace(current, search_text, replace_text)
                        if fuzzy and fuzzy != current and _py_ok(target_file, fuzzy):
                            fp.write_text(fuzzy)
                            changes.append({"file": target_file, "type": "patch-fuzzy"})
                else:
                    # Stage 2: fuzzy replacement to survive whitespace/token drift
                    fuzzy = _fuzzy_replace(current, search_text, replace_text)
                    if fuzzy and fuzzy != current and _py_ok(target_file, fuzzy):
                        fp.write_text(fuzzy)
                        changes.append({"file": target_file, "type": "patch-fuzzy"})
            except Exception as ep:
                await broadcast("warning", f"Failed to apply patch for {target_file}: {ep}", terminal_output=str(ep)[:200])

        src_applied = [c["file"] for c in changes if "test" not in c["file"]]
        if src_applied:
            diff_output = subprocess.run(
                ["git", "diff"], cwd=work_dir / "sample-app",
                capture_output=True, text=True, timeout=10
            ).stdout
            changes_info = [{"file": c["file"], "type": c["type"]} for c in changes]
            await broadcast("edited", "Patches applied.", diff=diff_output, files_changed=changes_info)
            solved = True
        else:
            # Nothing useful applied — revert partial/test-only edits for a clean fallback
            subprocess.run(["git", "checkout", "--", "."], cwd=work_dir / "sample-app",
                           capture_output=True, timeout=10)
            changes.clear()
            await broadcast("warning", "Agent edits did not change source code, applying fallback solution.")

    elif response and response.get("new_app_py"):
        # Phase 4b: Full-file replacement (legacy path)
        await broadcast("editing", "Applying full file replacement...", diff="")
        (work_dir / "sample-app" / "app.py").write_text(response["new_app_py"])
        changes.append({"file": "app.py", "type": "modified"})
        if response.get("new_test_py"):
            (work_dir / "sample-app" / "tests" / "test_app.py").write_text(response["new_test_py"])
            changes.append({"file": "tests/test_app.py", "type": "modified"})
        diff_output = subprocess.run(
            ["git", "diff"], cwd=work_dir / "sample-app",
            capture_output=True, text=True, timeout=10
        ).stdout
        changes_info = [{"file": c["file"], "type": c["type"]} for c in changes]
        await broadcast("edited", "Changes applied.", diff=diff_output, files_changed=changes_info)
        solved = True

    # NOTE: Guardrailed rescue happens AFTER scoring now (score-gated), so a
    # genuinely good writer solution is kept and only weak runs get the golden.
    # Here we only handle the pure-failure messaging for the two modes.
    if not solved and mode != "replay" and mode == "guardrailed":
        await broadcast("warning", "Agent solution incomplete — will guard-rail after scoring if it falls short.")
    elif not solved and mode != "replay" and mode == "live":
        # Live: honest outcome — the model's work stays as-is (or was reverted
        # if it was test-only). Press ⚡ Fallback to rescue, or let it validate
        # and show the real score.
        await broadcast("warning", "Agent did not ship a solvable change. Use ⚡ Fallback to rescue, or let it validate as-is.")

    # Phase 5: Validation
    # Grade against the PRISTINE test files, not the writer-editable ones. The
    # writer is allowed to touch tests/, and a broken test-file edit (e.g. a
    # syntax error) would otherwise crash pytest collection and tank EVERY test
    # → wild score swings. Restore canonical tests so grading is deterministic;
    # the writer's app.py (the real work) is untouched.
    restore_canonical_tests(work_dir)
    await broadcast("testing", word("testing") or "Running test suite and validation checks...")
    test_results = await run_validation(work_dir, challenge)

    for tr in test_results:
        status = "✅ PASS" if tr["passed"] else "❌ FAIL"
        await broadcast("testing", f"{status}: {tr['command']}", terminal_output=tr.get("output", "")[:500])

    # Capture diff size for scoring
    diff_for_scoring = subprocess.run(
        ["git", "diff"], cwd=work_dir / "sample-app",
        capture_output=True, text=True, timeout=10
    ).stdout

    # Phase 6: Scoring
    elapsed = time.time() - start_time
    s["elapsed"] = elapsed
    s["test_results"] = [t for t in test_results if "pytest" in t["command"]]
    s["check_results"] = [t for t in test_results if "pytest" not in t["command"]]
    s["changes"] = changes
    s["diff_size"] = len(diff_for_scoring)
    s["diff"] = diff_for_scoring
    s["human_overrides"] = s.get("human_overrides", 0)
    s["fulfilled_requirements"] = detect_fulfilled_requirements(challenge, test_results)
    s["scoring_weights"] = challenge.get("scoring_weights", {})

    score = score_session(s)

    # ── Review-repair loop (Fully Live only) ────────────────────────────────
    # If the writer's first attempt scored below target, run the reviewer NOW,
    # feed its findings back to the writer for ONE repair attempt, then re-apply
    # / re-validate / re-score. Keep whichever attempt scored higher. This is the
    # writer↔critic feedback loop — it lets the 70B's review actually improve the
    # result instead of just narrating it. Guardrailed/replay are unaffected.
    REPAIR_TARGET = int(os.environ.get("LIVE_REPAIR_TARGET", "85"))
    MAX_REPAIRS = int(os.environ.get("LIVE_MAX_REPAIRS", "2"))
    repair_attempt = 0
    while (mode == "live" and CRITIC_ENABLED
           and score.get("overall", 0) < REPAIR_TARGET
           and repair_attempt < MAX_REPAIRS):
        repair_attempt += 1
        await broadcast("critiquing",
            f"Score {score.get('overall')} < {REPAIR_TARGET} — reviewer is generating fix guidance (attempt {repair_attempt})…",
            score=score, write_elapsed=(time.time() - start_time))

        # 1) Get the reviewer's findings on the current diff.
        try:
            review = await run_critic(challenge=challenge, diff=diff_for_scoring,
                                      test_results=test_results, broadcast=broadcast)
        except Exception as e:
            await broadcast("warning", f"Reviewer unavailable for repair: {e}")
            break
        feedback = _critique_to_feedback(review, test_results)
        if not feedback:
            break

        # 2) Snapshot current state so we can revert if the repair is worse.
        prev_score, prev_changes = score, list(changes)
        subprocess.run(["git", "stash", "push", "-q", "-m", f"repair-{repair_attempt}"],
                       cwd=work_dir / "sample-app", capture_output=True, timeout=10)

        # 3) Ask the writer again WITH the feedback.
        await broadcast("editing", f"Writer is applying reviewer feedback (attempt {repair_attempt})…")
        cur_app = (work_dir / "sample-app" / "app.py").read_text()
        cur_test = (work_dir / "sample-app" / "tests" / "test_app.py").read_text()
        repair_resp = await call_llm(prompt, file_list, cur_app, cur_test,
                                     repair_feedback=feedback)

        # 4) Apply the repair patches (same guarded apply as the first pass).
        repair_changes = _apply_patches(repair_resp, work_dir, _py_ok, _fuzzy_replace)
        src_applied = [c for c in repair_changes if "test" not in c["file"]]
        if not src_applied:
            # repair produced nothing usable — restore the previous attempt
            subprocess.run(["git", "stash", "pop", "-q"], cwd=work_dir / "sample-app",
                           capture_output=True, timeout=10)
            await broadcast("editing", "Repair produced no usable change — keeping first attempt.")
            break

        # 5) Re-validate + re-score the repaired code.
        restore_canonical_tests(work_dir)
        new_tests = await run_validation(work_dir, challenge)
        new_diff = subprocess.run(["git", "diff"], cwd=work_dir / "sample-app",
                                  capture_output=True, text=True, timeout=10).stdout
        s["test_results"] = [t for t in new_tests if "pytest" in t["command"]]
        s["check_results"] = [t for t in new_tests if "pytest" not in t["command"]]
        s["changes"] = repair_changes
        s["diff_size"] = len(new_diff); s["diff"] = new_diff
        s["fulfilled_requirements"] = detect_fulfilled_requirements(challenge, new_tests)
        new_score = score_session(s)

        # 6) Keep the better of the two.
        if new_score.get("overall", 0) >= prev_score.get("overall", 0):
            score = new_score; changes = repair_changes
            test_results = new_tests; diff_for_scoring = new_diff
            # drop the stash (we're keeping the repaired working tree)
            subprocess.run(["git", "stash", "drop", "-q"], cwd=work_dir / "sample-app",
                           capture_output=True, timeout=10)
            await broadcast("edited",
                f"Repair improved the score: {prev_score.get('overall')} → {new_score.get('overall')}.",
                diff=new_diff, score=new_score,
                files_changed=[{"file": c["file"], "type": c["type"]} for c in repair_changes])
        else:
            # repair was worse — revert to the first attempt
            subprocess.run(["git", "checkout", "--", "."], cwd=work_dir / "sample-app",
                           capture_output=True, timeout=10)
            subprocess.run(["git", "stash", "pop", "-q"], cwd=work_dir / "sample-app",
                           capture_output=True, timeout=10)
            score, changes = prev_score, prev_changes
            s["test_results"] = [t for t in test_results if "pytest" in t["command"]]
            s["check_results"] = [t for t in test_results if "pytest" not in t["command"]]
            s["changes"] = changes; s["diff"] = diff_for_scoring; s["diff_size"] = len(diff_for_scoring)
            s["fulfilled_requirements"] = detect_fulfilled_requirements(challenge, test_results)
            await broadcast("edited",
                f"Repair scored lower ({new_score.get('overall')}) — kept the first attempt ({prev_score.get('overall')}).",
                score=prev_score, diff=diff_for_scoring,
                files_changed=[{"file": c["file"], "type": c["type"]} for c in prev_changes])
            break

    # Guardrailed rescue (score-gated): if the writer's own solution scored below
    # the bar, apply the golden solution and re-validate/re-score. Genuinely good
    # writer runs (>= threshold) are kept as-is — the AI gets real credit.
    GUARDRAIL_MIN = int(os.environ.get("GUARDRAIL_MIN_SCORE", "85"))
    if mode == "guardrailed" and score.get("overall", 0) < GUARDRAIL_MIN:
        await broadcast("warning",
            f"Score {score.get('overall')} below {GUARDRAIL_MIN} — applying golden solution (guardrail).")
        golden_branch = challenge.get("golden_branch", "")
        golden_target = REPOS_DIR / "sample-app" / golden_branch
        repo_target = work_dir / "sample-app"
        if golden_target.exists():
            import shutil
            for item in golden_target.iterdir():
                if item.is_file():
                    shutil.copy2(item, repo_target / item.name)
            if not any(c["file"] == "app.py" for c in changes):
                changes.append({"file": "app.py", "type": "modified"})
        # re-validate + re-score against the golden
        test_results = await run_validation(work_dir, challenge)
        for tr in test_results:
            st = "✅ PASS" if tr["passed"] else "❌ FAIL"
            await broadcast("testing", f"{st}: {tr['command']}", terminal_output=tr.get("output", "")[:500])
        diff_for_scoring = subprocess.run(
            ["git", "diff"], cwd=work_dir / "sample-app",
            capture_output=True, text=True, timeout=10).stdout
        s["test_results"] = [t for t in test_results if "pytest" in t["command"]]
        s["check_results"] = [t for t in test_results if "pytest" not in t["command"]]
        s["changes"] = changes
        s["diff_size"] = len(diff_for_scoring)
        s["diff"] = diff_for_scoring
        s["fulfilled_requirements"] = detect_fulfilled_requirements(challenge, test_results)
        score = score_session(s)
        await broadcast("edited", "Golden solution applied (guardrail).",
                        diff=diff_for_scoring,
                        files_changed=[{"file": c["file"], "type": c["type"]} for c in changes])

    s["score"] = score
    s["status"] = "reviewing" if CRITIC_ENABLED else "completed"
    s["completed_at"] = time.time()

    # Phase 7: Critique (writer→critic pipeline). Narrative layer on top of the
    # objective score — the critic gives a verdict + findings but does NOT move
    # the number. Runs the big 70B model TP=2 across both Sparks (the headline).
    if CRITIC_ENABLED:
        # write_elapsed = time spent writing + testing (everything before review).
        # The Arena freezes the WRITING clock here and starts the REVIEWING clock.
        review_start = time.time()
        s["write_elapsed"] = elapsed
        await broadcast("critiquing", "Sending to the reviewer (70B, across both Sparks)…",
                        score=score, write_elapsed=elapsed)
        try:
            critique = await run_critic(
                challenge=challenge,
                diff=diff_for_scoring,
                test_results=test_results,
                on_token=lambda partial: None,  # (streaming hook; wired below)
                broadcast=broadcast,
            )
        except Exception as e:
            critique = {"verdict": "unavailable", "summary": f"Critic unavailable: {e}",
                        "findings": [], "error": str(e)[:300]}
            await broadcast("warning", f"Critic call failed: {e}")
        s["critique"] = critique
        s["status"] = "completed"
        s["review_elapsed"] = time.time() - review_start
        # Re-score now that the reviewer's verdict is in: code_quality + efficiency
        # switch from diff-size proxies to the 70B's actual quality judgment.
        score = score_session(s)
        s["score"] = score
        await broadcast("completed", "Review complete!", score=score, critique=critique,
                        write_elapsed=elapsed, review_elapsed=s["review_elapsed"],
                        diff=diff_for_scoring,
                        files_changed=[{"file": c["file"], "type": c["type"]} for c in changes])
    else:
        await broadcast("completed", "Demo complete!", score=score,
                        diff=diff_for_scoring,
                        files_changed=[{"file": c["file"], "type": c["type"]} for c in changes])

    return score


async def run_agentic(work_dir: Path, challenge: dict, broadcast) -> list:
    """AGENTIC mode: drive Hermes as an autonomous coding agent on local Nemotron.

    Instead of a one-shot patch call, Hermes reads the repo, edits files, runs
    pytest, reads tracebacks and iterates — all with real tools. We spawn it as a
    subprocess in the session's sample-app dir, stream its tool-call activity to
    the Arena/Theater via broadcast(), and return the list of changed files.
    Downstream validation/scoring is identical to the other modes.
    """
    repo = work_dir / "sample-app"
    from shlex import quote as shlex_quote
    hermes_bin = os.environ.get(
        "HERMES_BIN",
        os.path.expanduser("~/.hermes/hermes-agent/venv/bin/python") + " -m hermes_cli.main")
    profile = os.environ.get("HERMES_PROFILE_AGENTIC", "nemo")

    # Build a focused task prompt. Kept deliberately tight: the demo needs a fast,
    # surgical fix — over-exploration (re-reading files, extra sanity checks) is what
    # blew agentic runs out to 5-7 min. Push for minimal steps + a single confirming test.
    test_cmd = ""
    for t in (challenge.get("validation", {}).get("tests", []) or []):
        test_cmd = t.replace("{{repo_path}}", str(repo))
        break
    # Optional per-challenge hint (challenge JSON: "agent_hint"). Used to point the
    # agent straight at the fix on harder tasks (e.g. C's O(n^2) perf problem) so it
    # doesn't burn time discovering the problem class. Keeps the fix authentic — it
    # still writes + verifies the change — just skips the exploration.
    hint = (challenge.get("agent_hint") or "").strip()
    hint_line = f"\nHint: {hint}\n" if hint else ""
    task = (
        f"You are in a Flask app at {repo}. Fix this issue in app.py: "
        f"{challenge.get('title','')} — {challenge.get('description','')}. "
        f"Work FAST and minimally — this is a live demo:\n"
        f"1. Read app.py ONCE to find the relevant code.\n"
        f"2. Make the SMALLEST change to app.py that fixes it (one focused edit).\n"
        f"3. Run the tests ONCE to confirm: {test_cmd}\n"
        f"4. Only if a test fails, make ONE more targeted fix and re-run; otherwise STOP.\n"
        f"{hint_line}"
        f"Do NOT edit files under tests/. Do NOT write extra scripts or explore beyond app.py. "
        f"Do NOT re-read files you've already seen. Stop as soon as the tests pass."
    )

    cmd = f'cd {shlex_quote(str(repo))} && {hermes_bin} chat -p {shlex_quote(profile)} --yolo -q {shlex_quote(task)}'

    await broadcast("calling_llm", "🤖 Agentic Hermes (local Nemotron) is taking over — reading the repo…")

    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(repo),
    )

    # Stream Hermes stdout, translating tool activity into clean, labeled Arena/
    # Theater steps (📖 Read / ✏️ Edit / 🧪 Test / 🔎 Search) instead of raw CLI noise.
    tool_count = 0
    import re as _re
    seen_recent = []   # dedupe identical consecutive steps

    def _classify(text: str):
        """Map a Hermes CLI line to a clean (emoji, label) step, or None to skip.
        Hermes (non-quiet) prints tool activity as:
          ┊ 📖 preparing read_file…      (file tools: 'preparing <tool_name>')
          ┊ 💻 $   <shell command>  0.3s  (terminal tool)
        """
        t = _re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text).strip("\r ").strip()
        low = t.lower()
        if not t:
            return None
        # Terminal command: "💻 $ <cmd>". Skip the "preparing terminal…" placeholder
        # (the real "$ <cmd>" line follows separately).
        if "preparing terminal" in low:
            return None
        if "💻" in text or _re.search(r"\$\s{2,}\S", t):
            if "$" not in t:
                return None
            cmd = _re.sub(r"^.*?\$\s+", "", t)
            cmd = _re.sub(r"\s+[\d.]+s(\s+\[exit \d+\])?\s*$", "", cmd).strip()[:120]
            if not cmd:
                return None
            if "pytest" in low:
                return ("🧪", "Running the test suite")
            if "app.py" in low and "python" in low:
                return ("▶️", "Running the app to check the fix")
            if cmd.startswith(("ls", "cat", "find", "grep", "head", "tail")):
                return ("📂", f"Exploring: {cmd}")
            return ("💻", f"Shell: {cmd}")
        # File tools: "preparing <tool_name>"
        mprep = _re.search(r"preparing\s+([a-z_]+)", low)
        if mprep:
            tool = mprep.group(1)
            return {
                "read_file": ("📖", "Reading the source code"),
                "edit_file": ("✏️", "Editing app.py"),
                "patch":     ("✏️", "Applying a fix to app.py"),
                "write_file":("✏️", "Writing changes to app.py"),
                "search_files": ("🔎", "Searching the codebase"),
            }.get(tool, ("🔧", f"Using {tool}"))
        return None

    async def pump():
        nonlocal tool_count
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                text = raw.decode("utf-8", "replace")
                step = _classify(text)
                if step:
                    emoji, label = step
                    # collapse duplicate consecutive steps (Hermes echoes previews)
                    if seen_recent and seen_recent[-1] == label:
                        continue
                    seen_recent.append(label)
                    if len(seen_recent) > 3:
                        seen_recent.pop(0)
                    tool_count += 1
                    await broadcast("editing", f"{emoji} {label}")
                else:
                    low = _re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text).lower()
                    if "reasoning" in low or "thinking" in low:
                        await broadcast("calling_llm", "🧠 Agent is reasoning about the fix…")
        except Exception:
            pass

    pump_task = asyncio.create_task(pump())
    try:
        await asyncio.wait_for(proc.wait(), timeout=int(os.environ.get("AGENTIC_TIMEOUT", "240")))
    except asyncio.TimeoutError:
        proc.kill()
        await broadcast("warning", "Agentic run hit the time limit — scoring whatever it produced.")
    finally:
        pump_task.cancel()
        try:
            await pump_task
        except asyncio.CancelledError:
            pass

    # Determine changed files from git (the agent edited them directly on disk).
    diff_stat = subprocess.run(
        ["git", "diff", "--name-only"], cwd=repo, capture_output=True, text=True, timeout=10).stdout
    changes = [{"file": f.strip(), "type": "modified"} for f in diff_stat.splitlines() if f.strip()]
    await broadcast("edited", f"🤖 Agent finished after {tool_count} tool actions.",
                    files_changed=changes)
    return changes


async def call_llm(prompt_task: str, file_list: str, app_code: str, test_code: str,
                   repair_feedback: str = ""):
    """Call vLLM via OpenAI-compatible API.

    Sends full code context, asks for targeted search/replace patches.
    Output is small (200-500 tokens) = 10-25s at Nemotron's ~19 tok/s.

    repair_feedback: when set, this is a SECOND attempt — the reviewer's findings
    on the first attempt are injected so the writer fixes what it got wrong.
    """
    from aiohttp import ClientSession, TCPConnector
    import re

    repair_block = ""
    if repair_feedback:
        repair_block = f"""

⚠️ THIS IS A REPAIR ATTEMPT. Your previous change was reviewed and did NOT fully
pass. Address this reviewer feedback precisely — do not repeat the same mistake:
{repair_feedback}

Common fixes: if a route/function already exists, MODIFY it in place (do not add a
duplicate — Flask raises 'View function mapping is overwriting an existing endpoint').
Make sure your patch.search targets the EXISTING code so it actually applies.
"""

    meta_prompt = f"""You are a software engineer modifying a Flask web application.

FULL app.py ({len(app_code)} chars):
---
{app_code}
---

FULL tests/test_app.py ({len(test_code)} chars):
---
{test_code[:3000]}
---

TASK:
{prompt_task}
{repair_block}
Return ONLY a valid JSON object:
{{
  "reasoning_summary": "Brief explanation of what you changed",
  "patches": [
    {{"file": "app.py", "search": "exact existing substring to replace", "replace": "replacement code"}},
    {{"file": "tests/test_app.py", "search": "exact existing substring", "replace": "new test code"}}
  ],
  "tests_added_n": 0
}}

RULES:
- Each patch.search must be an EXACT substring from the original code (exact whitespace/tab indentation)
- Each patch.replace is what to substitute in its place
- You MUST include at least one patch to app.py that changes source behavior — a patch that ONLY adds tests is NOT a valid solution and will fail the test suite
- Include ONLY blocks that need changing — most tasks need 1-2 patches
- If new tests are needed, add them in a SEPARATE patch to tests/test_app.py (never in the same patch as the fix)
- Keep patches small: 3-30 lines per patch
- Return at most 3 patches total
- Return ONLY valid JSON, no explanation text outside the JSON"""

    msg = None
    finish_reason = ""
    for attempt in range(1, 4):
        try:
            async with ClientSession(connector=TCPConnector(limit=10)) as sess:
                async with sess.post(
                    f"{VLLM_URL}/v1/chat/completions",
                    json={
                        "model": MODEL_NAME,
                        "messages": [
                            {"role": "system", "content": "You are a coding assistant. Return ONLY valid JSON with search/replace patches."},
                            {"role": "user", "content": meta_prompt},
                        ],
                        # Nemotron-Lightning is a reasoning model. For this
                        # patch-generation task we do NOT want it to burn the whole
                        # token budget on chain-of-thought (that left content empty
                        # → no patch → Fully-Live failures). Per the model card:
                        #   enable_thinking=False   → skip CoT, answer directly
                        #   force_nonempty_content=True → always emit content (JSON)
                        # Both are the card's recommended settings for coding agents.
                        "chat_template_kwargs": {
                            "enable_thinking": os.environ.get("WRITER_THINK", "0") == "1",
                            "force_nonempty_content": True,
                        },
                        # With enable_thinking=False the model answers directly, so
                        # a small budget is plenty (patch JSON is a few hundred toks).
                        "max_tokens": int(os.environ.get("WRITER_MAX_TOKENS", "4096")),
                        "temperature": float(os.environ.get("WRITER_TEMP", "0.6")),
                    },
                    timeout=900,
                ) as resp:
                    status = resp.status
                    raw = await resp.text()
                    print(f"vLLM HTTP {status}, body length={len(raw)}")
                    if status != 200:
                        print(f"vLLM non-200 body (first 500): {raw[:500]}")
                        raise RuntimeError(f"HTTP {status}: {raw[:200]}")
                    data = json.loads(raw)
                    if "error" in data:
                        raise RuntimeError(f"vLLM error: {data['error']}")
                    if not data.get("choices"):
                        raise RuntimeError(f"No choices in response: {raw[:300]}")
                    msg = data["choices"][0]["message"]
                    finish_reason = data["choices"][0].get("finish_reason", "")
            break
        except Exception as e:
            print(f"Model call attempt {attempt}/3 failed: {e}")
            if attempt == 3:
                print("All attempts failed — returning legacy format fallback")
                return {"new_app_py": None}
            await asyncio.sleep(3 * attempt)

    if not msg:                       # all retries failed (defensive; loop returns above)
        return {"new_app_py": None}
    # Reasoning models can return content=null with the text in "reasoning".
    # msg.get("content","") returns None (not "") when the key exists as null,
    # so coerce explicitly or len() throws "NoneType has no len()".
    raw_content = msg.get("content") or ""
    raw_reasoning = msg.get("reasoning") or ""
    content = raw_content if raw_content else raw_reasoning
    print(f"Model response: content={len(raw_content)} reasoning={len(raw_reasoning)} chars, finish={finish_reason}")
    print(f"Content first 400: {content[:400]}")
    print(f"Content last 200: {content[-200:]}")

    # Parse JSON - try multiple strategies for robustness
    try:
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group())
            print(f"JSON parsed successfully, keys: {list(result.keys())}")
            return result
    except json.JSONDecodeError as e:
        print(f"JSON parse error (strategy 1): {e}")
        try:
            depth = 0
            start = content.rfind('{')
            if start >= 0:
                for i in range(start, len(content)):
                    if content[i] == '{':
                        depth += 1
                    elif content[i] == '}':
                        depth -= 1
                        if depth == 0:
                            result = json.loads(content[start:i+1])
                            print(f"JSON parsed successfully (strategy 2), keys: {list(result.keys())}")
                            return result
        except Exception as e2:
            print(f"JSON parse error (strategy 2): {e2}")
    print("All JSON parsing strategies failed - returning legacy fallback")
    return {"reasoning_summary": "Failed to parse model response."}


# ---- Critic (writer→critic pipeline) ---------------------------------------

CRITIC_SYSTEM = (
    "You are a senior staff software engineer doing a focused code review. "
    "You review a proposed change (a git diff) against the task and the test "
    "results. You are precise, concrete, and kind. You do NOT rewrite the code "
    "yourself — you give a verdict and a short list of findings. The automated "
    "test suite is the ground truth for correctness; you add judgment about "
    "quality, edge cases, and clarity that tests can miss."
)


def _critic_prompt(challenge: dict, diff: str, test_results: list) -> str:
    tests_passed = all(t.get("passed") for t in test_results if "pytest" in t.get("command", ""))
    test_summary = "\n".join(
        f"  [{'PASS' if t.get('passed') else 'FAIL'}] {t.get('command','')}"
        for t in test_results
    ) or "  (no tests run)"
    task = challenge.get("prompt") or challenge.get("description") or challenge.get("title", "")
    return f"""## Task the developer was given
{task}

## Automated test results (ground truth for correctness)
{test_summary}
Overall: {"ALL TESTS PASS" if tests_passed else "SOME TESTS FAILED"}

## The change under review (git diff)
```diff
{diff[:8000] if diff else "(no changes were made)"}
```

## Your review
Respond with ONLY a JSON object, no prose outside it:
{{
  "verdict": "ship" | "ship-with-nits" | "needs-work",
  "summary": "one-sentence overall judgment",
  "findings": [
    {{"kind": "correctness" | "style" | "spec" | "edge-case",
      "severity": "info" | "minor" | "major",
      "note": "specific, actionable observation"}}
  ],
  "better_way": "optional: one concrete suggestion for a cleaner/safer approach, or null"
}}

Rules:
- If all tests pass and the code is clean, verdict "ship" with 0-2 info findings.
- Base "correctness" findings on the diff + test results, not speculation.
- At most 3 findings. Be concrete (name the function/line), never generic.
- "better_way" is optional and at most 2 sentences."""


async def run_critic(challenge, diff, test_results, on_token=None, broadcast=None):
    """Call the CRITIC model (Llama-3.3-Nemotron-70B-Feedback, TP=2 both Sparks).

    Streams tokens (so the UI shows it 'thinking' instead of a dead freeze),
    then parses a JSON verdict. Returns a dict:
      {verdict, summary, findings:[{kind,severity,note}], better_way, raw, elapsed_s}
    """
    from aiohttp import ClientSession, TCPConnector

    prompt = _critic_prompt(challenge, diff, test_results)
    t0 = time.time()
    collected = []
    last_beat = 0.0

    async with ClientSession(connector=TCPConnector(limit=10)) as sess:
        async with sess.post(
            f"{CRITIC_URL}/v1/chat/completions",
            json={
                "model": CRITIC_MODEL,
                "messages": [
                    {"role": "system", "content": CRITIC_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 1200,
                "temperature": 0.2,
                "stream": True,
            },
            timeout=600,
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"critic HTTP {resp.status}: {body[:200]}")
            async for line in resp.content:
                if not line:
                    continue
                txt = line.decode("utf-8", "ignore").strip()
                if not txt.startswith("data:"):
                    continue
                payload = txt[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    delta = json.loads(payload)["choices"][0]["delta"].get("content", "")
                except Exception:
                    continue
                if not delta:
                    continue
                collected.append(delta)
                if on_token:
                    on_token("".join(collected))
                # heartbeat to the UI at most ~1/s so the reviewer looks alive
                now = time.time()
                if broadcast and (now - last_beat) > 1.0:
                    last_beat = now
                    preview = "".join(collected)[-280:]
                    await broadcast("critiquing", "Reviewer is analysing the change…",
                                    critique_stream=preview)

    raw = "".join(collected)
    parsed = _parse_critic_json(raw)
    parsed["raw"] = raw
    parsed["elapsed_s"] = round(time.time() - t0, 1)
    return parsed


def _parse_critic_json(raw: str) -> dict:
    """Best-effort extract the critic's JSON verdict; degrade gracefully to prose."""
    import re
    default = {"verdict": "review", "summary": raw.strip()[:200] or "No review produced.",
               "findings": [], "better_way": None}
    if not raw:
        return default
    # strip code fences
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return default
    for candidate in (m.group(0),):
        try:
            d = json.loads(candidate)
            d.setdefault("verdict", "review")
            d.setdefault("summary", "")
            d.setdefault("findings", [])
            d.setdefault("better_way", None)
            if not isinstance(d["findings"], list):
                d["findings"] = []
            return d
        except Exception:
            continue
    return default


async def run_validation(work_dir: Path, challenge: dict):
    """Run validation tests and checks for a challenge."""
    results = []
    repo = work_dir / "sample-app"
    validation = challenge.get("validation", {})

    for cmd in validation.get("tests", []):
        cmd = cmd.replace("{{repo_path}}", str(repo))
        try:
            r = subprocess.run(
                cmd, shell=True, cwd=repo,
                capture_output=True, text=True, timeout=validation.get("timeout_seconds", 60)
            )
            passed = r.returncode == 0
            results.append({
                "command": cmd,
                "passed": passed,
                "output": r.stdout[-1000:] + r.stderr[-500:],
            })
        except subprocess.TimeoutExpired:
            results.append({"command": cmd, "passed": False, "output": "TIMEOUT"})

    for cmd in validation.get("checks", []):
        cmd = cmd.replace("{{repo_path}}", str(repo))
        try:
            r = subprocess.run(
                cmd, shell=True, cwd=repo,
                capture_output=True, text=True, timeout=30
            )
            passed = r.returncode == 0
            results.append({
                "command": cmd,
                "passed": passed,
                "output": r.stdout[-500:],
            })
        except Exception as e:
            results.append({"command": cmd, "passed": False, "output": str(e)})

    return results


def _test_pass_fraction(test_results: list) -> float:
    """Fraction of INDIVIDUAL tests that passed, parsed from pytest's summary
    line (reuses scoring._pytest_counts so parsing stays consistent & robust).
    Falls back to command-level pass ratio if no pytest summary is present."""
    from orchestrator.scoring import _pytest_counts
    passed = failed = 0
    for r in test_results:
        cp, cf = _pytest_counts(r.get("output", "") or "")
        passed += cp
        failed += cf
    total = passed + failed
    if total > 0:
        return passed / total
    # fallback: fraction of commands that exited 0
    if test_results:
        return sum(1 for r in test_results if r.get("passed")) / len(test_results)
    return 0.0


def detect_fulfilled_requirements(challenge: dict, test_results: list) -> list[bool]:
    """Requirements fulfilled ∝ fraction of individual tests passing (partial credit).

    Previously all-or-nothing (all tests pass → all reqs met), which zeroed out
    requirement_completeness whenever even one edge-case test failed — capping
    otherwise-good runs. Now N requirements get round(frac * N) marked fulfilled,
    so a 2/3 pass rate yields proportional credit.
    """
    n_req = {"A": 3, "B": 2, "C": 2, "D": 3}.get(challenge["id"], 1)
    frac = _test_pass_fraction(test_results)
    n_met = round(frac * n_req)
    return [i < n_met for i in range(n_req)]


# --- WebSocket ---

async def _broadcast(session_id: str, event: dict):
    """Send event to all subscribers AND buffer it for late joiners."""
    event_buffers.setdefault(session_id, []).append(event)
    for ws in ws_subscribers.get(session_id, []):
        try:
            await ws.send_json(event)
        except Exception:
            pass


@app.websocket("/ws/arena/{session_id}")
async def arena_ws(websocket: WebSocket, session_id: str):
    await websocket.accept()
    ws_subscribers.setdefault(session_id, []).append(websocket)
    for ev in event_buffers.get(session_id, []):
        try:
            await websocket.send_json(ev)
        except Exception:
            pass
    try:
        while True:
            try:
                await websocket.receive()
            except WebSocketDisconnect:
                break
            except RuntimeError:
                break
    finally:
        try:
            ws_subscribers[session_id].remove(websocket)
        except (ValueError, KeyError):
            pass


@app.websocket("/ws/theater/{session_id}")
async def theater_ws(websocket: WebSocket, session_id: str):
    await websocket.accept()
    ws_subscribers.setdefault(session_id, []).append(websocket)
    for ev in event_buffers.get(session_id, []):
        try:
            await websocket.send_json(ev)
        except Exception:
            pass
    try:
        while True:
            try:
                await websocket.receive()
            except WebSocketDisconnect:
                break
            except RuntimeError:
                break
    finally:
        try:
            ws_subscribers[session_id].remove(websocket)
        except (ValueError, KeyError):
            pass


# --- REST endpoints ---

@app.get("/")
async def root():
    return {"service": "AI Dev Arena Orchestrator", "status": "running"}


@app.get("/api/config")
async def api_config():
    """Runtime model wiring — lets the UI show writer/critic names + critic state."""
    return {
        "writer_model": WRITER_MODEL,
        "writer_url": WRITER_URL,
        "critic_model": CRITIC_MODEL,
        "critic_url": CRITIC_URL,
        "critic_enabled": CRITIC_ENABLED,
    }


@app.get("/api/challenges")
async def list_challenges():
    return {"challenges": CHALLENGES}


@app.get("/api/telemetry")
async def get_telemetry():
    """Live GPU/CPU/memory across all DGX Spark nodes + model in use.

    Add a 3rd/4th Spark by extending the SPARK_NODES_JSON env var —
    this endpoint and the dashboard pick it up automatically.
    """
    from orchestrator.telemetry import collect_telemetry
    data = await collect_telemetry()
    # Friendly display name for the model badge
    served = (data.get("model") or {}).get("served")
    if served:
        data["model"]["display"] = served.split("/")[-1]
    # Two-model demo: surface both roles + which node(s) each runs on and how much
    # GPU memory THAT MODEL uses on THAT node (from per-process nvidia-smi, so the
    # numbers reflect the model's own footprint — not the node total).
    sparks = data.get("sparks", [])

    def _model_mem_gb(spark, role):
        """Sum GPU memory (GB) used by `role` processes on this spark, or None."""
        procs = [p for p in spark.get("gpu_procs", []) if p.get("role") == role]
        if not procs:
            return None
        return round(sum(p.get("mem_bytes", 0) for p in procs) / 1e9)

    def _nodes_for(role):
        """List of {name, mem_gb} for every spark actually running `role`."""
        out = []
        for s in sparks:
            gb = _model_mem_gb(s, role)
            if gb:  # only include nodes where this model is actually resident
                out.append({"name": s.get("name"), "role": s.get("role"), "mem_gb": gb})
        return out

    writer_nodes = _nodes_for("writer")
    critic_nodes = _nodes_for("critic")
    data["models"] = {
        "writer": {"name": WRITER_MODEL, "nodes": writer_nodes},
        "critic": ({"name": CRITIC_MODEL, "nodes": critic_nodes}
                   if CRITIC_ENABLED else None),
    }
    return data


@app.get("/api/model")
async def get_model():
    """Current served model (for the Operator/health display)."""
    from orchestrator.telemetry import collect_telemetry
    data = await collect_telemetry()
    return data.get("model", {})


@app.post("/api/session/start")
async def start_session(req: StartRequest):
    challenge = CHALLENGES.get(req.challenge_id)
    if not challenge:
        return {"error": f"Challenge {req.challenge_id} not found"}, 404

    session_id = str(uuid.uuid4())[:8]
    work_dir = SESSION_WORK_DIR / session_id
    work_dir.mkdir(parents=True, exist_ok=True)

    reset_repo(work_dir, req.challenge_id)

    session = {
        "id": session_id,
        "challenge_id": req.challenge_id,
        "mode": req.mode,
        "audience": req.audience,
        "status": "running",
        "started_at": time.time(),
        "elapsed": 0,
        "work_dir": str(work_dir),
        "test_results": [],
        "check_results": [],
        "changes": [],
        "diff_size": 0,
        "human_overrides": 0,
        "fulfilled_requirements": [],
        "scoring_weights": challenge.get("scoring_weights", {}),
        "score": None,
    }
    sessions[session_id] = session
    ws_subscribers[session_id] = []

    # Start agent in background
    asyncio.create_task(
        run_agent(session_id, challenge, work_dir)
    )

    return {"session_id": session_id, "status": "running"}


@app.post("/api/session/{session_id}/fallback")
async def fallback_session(session_id: str):
    session = sessions.get(session_id)
    if not session:
        return {"error": "Session not found"}, 404

    challenge = CHALLENGES.get(session["challenge_id"])
    work_dir = Path(session["work_dir"])

    apply_golden(work_dir, session["challenge_id"], challenge["golden_branch"])
    session["status"] = "fallback"
    session["human_overrides"] = session.get("human_overrides", 0) + 1

    return {"status": "fallback_applied"}


@app.post("/api/session/{session_id}/reset")
async def reset_session(session_id: str):
    session = sessions.get(session_id)
    if not session:
        return {"error": "Session not found"}, 404

    work_dir = Path(session["work_dir"])
    challenge = CHALLENGES.get(session["challenge_id"])
    reset_repo(work_dir, session["challenge_id"])
    session["status"] = "ready"

    return {"status": "reset_complete"}


@app.get("/api/running-session")
async def running_session():
    """Return the most recent running or recently-completed session for live viewers."""
    latest = None
    latest_time = 0
    for sid, s in sessions.items():
        st = s.get("started_at", 0)
        if st > latest_time:
            latest_time = st
            latest = s
    if latest:
        s = {k: v for k, v in latest.items() if k != "work_dir"}
        return {
            "session_id": s["id"],
            "status": s["status"],
            "elapsed": s.get("elapsed", 0),
            "challenge_id": s.get("challenge_id"),
        }
    return {"session_id": None, "status": "none"}


@app.get("/api/session/{session_id}")
async def get_session(session_id: str):
    session = sessions.get(session_id)
    if not session:
        return {"error": "Session not found"}, 404

    # Return session without work_dir path (internal)
    s = {k: v for k, v in session.items() if k != "work_dir"}
    return s


# --- Live App (spawn the real Flask app: before + after the AI's edit) ---

@app.post("/api/session/{session_id}/live-app")
async def start_live_app(session_id: str, role: str = "both"):
    """Spawn app instances and return ports.

    role=both (default) → (re)spawn BEFORE (pristine) and AFTER (edited).
    role=after           → respawn only AFTER (e.g. after the challenge completes,
                           to pick up the writer's final edited code).
    role=before          → respawn only BEFORE.
    """
    from orchestrator import live_apps
    session = sessions.get(session_id)
    if not session:
        return {"error": "Session not found"}, 404
    work_dir = Path(session["work_dir"])
    loop = asyncio.get_running_loop()
    out = {}
    try:
        if role in ("both", "before"):
            out["before"] = await loop.run_in_executor(None, live_apps.start_before, session_id, work_dir)
        if role in ("both", "after"):
            out["after"] = await loop.run_in_executor(None, live_apps.start_after, session_id, work_dir)
    except Exception as e:
        # Never leak a 500/plain-text body to the frontend (it expects JSON).
        return {"error": f"live-app spawn failed: {e}", "before": out.get("before"), "after": out.get("after")}
    return out


@app.get("/api/session/{session_id}/live-app")
async def live_app_status(session_id: str):
    from orchestrator import live_apps
    return live_apps.status(session_id)


@app.delete("/api/session/{session_id}/live-app")
async def stop_live_app(session_id: str):
    from orchestrator import live_apps
    live_apps.stop(session_id)
    return {"stopped": True}




import jinja2
from fastapi.responses import HTMLResponse

env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(BASE_DIR / "frontend")))


@app.get("/arena")
async def arena_page(session_id: str = ""):
    template = env.get_template("arena.html")
    return HTMLResponse(template.render(session_id=session_id))


@app.get("/theater")
async def theater_page(session_id: str = ""):
    template = env.get_template("theater.html")
    return HTMLResponse(template.render(session_id=session_id))


@app.get("/operator")
async def operator_page():
    template = env.get_template("operator.html")
    return HTMLResponse(template.render(challenges=CHALLENGES))


@app.get("/storefront")
async def storefront_page():
    """The real shopping-cart storefront UI. Point it at a live app instance via
    ?api=http://host:port&state=before|after (used inside the /live split view)."""
    return HTMLResponse((BASE_DIR / "frontend" / "storefront.html").read_text())


@app.get("/live")
async def live_page(session_id: str = ""):
    """Split view: the actual app BEFORE vs AFTER the AI's edit, both live."""
    template = env.get_template("live.html")
    return HTMLResponse(template.render(session_id=session_id))

