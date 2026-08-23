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
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "frontend" / "static")), name="static")

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

    # Handle model response — apply patches or full-file replacement
    # Post-apply guard: a valid solution MUST actually change a non-test file.
    # If the model's patch search strings don't match (or it only touched
    # tests), we revert and use the golden fallback rather than ship a
    # test-only "fix" that fails the suite.
    solved = False

    if response and response.get("patches"):
        # Phase 4a: Apply targeted patches
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
                    new_content = current.replace(search_text, replace_text, 1)
                    fp.write_text(new_content)
                    changes.append({"file": target_file, "type": "modified"})
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

    if not solved and mode != "replay" and mode == "guardrailed":
        # Guardrailed: auto-rescue with the golden branch solution
        await broadcast("warning", "Agent response unusable — guardrails kicking in, applying fallback solution.")
        golden_branch = challenge.get("golden_branch", "")
        golden_target = REPOS_DIR / "sample-app" / golden_branch
        repo_target = work_dir / "sample-app"
        if golden_target.exists():
            for item in golden_target.iterdir():
                dst = repo_target / item.name
                if item.is_file():
                    import shutil
                    shutil.copy2(item, dst)
                    changes.append({"file": item.name, "type": "modified"})
        diff_output = subprocess.run(
            ["git", "diff"], cwd=work_dir / "sample-app",
            capture_output=True, text=True, timeout=10
        ).stdout
        changes_info = [{"file": c["file"], "type": c["type"]} for c in changes]
        await broadcast("edited", "Fallback solution applied.", diff=diff_output, files_changed=changes_info)
    elif not solved and mode != "replay" and mode == "live":
        # Live: honest outcome — the model's work stays as-is (or was reverted
        # if it was test-only). Press ⚡ Fallback to rescue, or let it validate
        # and show the real score.
        await broadcast("warning", "Agent did not ship a solvable change. Use ⚡ Fallback to rescue, or let it validate as-is.")

    # Phase 5: Validation
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
    s["score"] = score
    s["status"] = "reviewing" if CRITIC_ENABLED else "completed"
    s["completed_at"] = time.time()

    # Phase 7: Critique (writer→critic pipeline). Narrative layer on top of the
    # objective score — the critic gives a verdict + findings but does NOT move
    # the number. Runs the big 70B model TP=2 across both Sparks (the headline).
    if CRITIC_ENABLED:
        await broadcast("critiquing", "Sending to the reviewer (70B, across both Sparks)…",
                        score=score)
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
        await broadcast("completed", "Review complete!", score=score, critique=critique)
    else:
        await broadcast("completed", "Demo complete!", score=score)

    return score


async def call_llm(prompt_task: str, file_list: str, app_code: str, test_code: str):
    """Call vLLM via OpenAI-compatible API.

    Sends full code context, asks for targeted search/replace patches.
    Output is small (200-500 tokens) = 10-25s at Nemotron's ~19 tok/s.
    """
    from aiohttp import ClientSession, TCPConnector
    import re

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
                        # Writer output is small (patch JSON). Cap well under
                        # the model's context so prompt+output never overflows
                        # (Lightning-30B ctx=32768; 32000 output overflowed → 400).
                        "max_tokens": int(os.environ.get("WRITER_MAX_TOKENS", "8192")),
                        "temperature": 0.1,
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


def detect_fulfilled_requirements(challenge: dict, test_results: list) -> list[bool]:
    """Detect which requirements were fulfilled based on test/check results."""
    # Simple heuristic: if all tests pass ≈ all requirements met
    all_pass = all(r.get("passed", False) for r in test_results) if test_results else False
    if challenge["id"] == "A":
        return [all_pass, all_pass, all_pass]  # 3 requirements
    elif challenge["id"] == "B":
        return [all_pass, all_pass]  # 2 requirements
    elif challenge["id"] == "C":
        return [all_pass, all_pass]  # 2 requirements
    return [all_pass]


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


# --- Frontend serving ---

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
