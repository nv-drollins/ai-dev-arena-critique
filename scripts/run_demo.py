#!/usr/bin/env python3
"""
AI Dev Arena — Demo CLI

One tool to run all four challenges from your terminal (or the show desk). 
Streams every live phase + the full scoring breakdown.

Usage:
  python run_demo.py A | B | C | D      # run one challenge (default: live mode)
  python run_demo.py A -m replay        # offline scripted run (no LLM call)
  python run_demo.py A -a executives    # audience-tuned phrasing (see CONCEPTS)
  python run_demo.py A -m guardrailed -a developers -v
  python run_demo.py status             # current model + cluster state
  python run_demo.py all                # run D -> C -> B -> A (longest first)

Defaults: mode=guardrailed (safe default for demos — auto-rescues if needed),
          audience=broad (plain-English phrasing for the big screen).

Requires: aiohttp installed (the arena venv has it).
"""
import argparse, asyncio, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

async def _print(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# WS streamer (works with both websockets lib and aiohttp fallback)
# ---------------------------------------------------------------------------

async def stream_ws(sid: str, stop: asyncio.Event, timeout: float = 600.0,
                    url: str = None):
    """Connect to /ws/arena/{sid} and print every event live. Stop when the
    session ends. Tries `websockets` first, falls back to aiohttp."""
    import urllib.parse
    host = url or "ws://127.0.0.1:8080"
    target = f"{host}/ws/arena/{sid}"

    # 1) Try `websockets`
    try:
        import websockets
        async with websockets.connect(target, open_timeout=30, max_size=2**24) as ws:
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
                ev = json.loads(msg)
                await _emit(ev)
                if ev.get("score") or ev.get("phase") in ("completed", "error"):
                    break
        return
    except ImportError:
        pass  # fall through to aiohttp

    # 2) Fallback: aiohttp (already in the arena venv)
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(target, timeout=2.0) as ws:
            while True:
                m = await asyncio.wait_for(ws.receive(), timeout=timeout)
                if m.type in (3, 4, 5):  # close/closing/closed
                    break
                if m.type != 1:
                    continue
                ev = json.loads(m.data)
                await _emit(ev)
                if ev.get("score") or ev.get("phase") in ("completed", "error"):
                    break


async def _emit(ev: dict):
    ph = ev.get("phase")
    msg = ev.get("message", "")
    el = ev.get("elapsed")
    icon = {
        "inspecting": "🔍", "planning": "🧠", "calling_llm": "💭", "editing": "📝",
        "edited":   "➜", "testing":  "⚙️", "completed": "✅", "error": "❌",
        "warning":  "⚠️", "fallback": "🪂",
    }.get(ph, "•")
    # ANSI colors for key lines
    ph_col = f"\033[36m{ph}\033[0m"
    if ev.get("score"):
        print(f"\n🏁 completed — {msg}\n", flush=True)
        print_scoring(ev["score"])
        print_flush_session_summary(ev)
        return
    print(f"  {el:>6.1f}s  {icon} {ph_col}: {msg}", flush=True)
    if ev.get("terminal_output"):
        for line in ev["terminal_output"].strip().rsplit("\n", 8):
            print(f"          {line}", flush=True)


def print_scoring(score: dict):
    overall = score.get("overall")
    maxp = score.get("max_possible", 100)
    pct = score.get("percentage")
    bar_n = 25
    bar_full = int(overall / maxp * bar_n) if maxp else 0
    bar = "▰" * bar_full + "▱" * (bar_n - bar_full)
    print(f"\n{'='*72}")
    print(f"  FINAL SCORE   {overall}/{maxp}   {bar}   {pct}%")
    print(f"{'='*72}")
    for name in ("time_to_result", "test_pass_rate", "code_quality",
                 "requirement_completeness", "efficiency", "human_overrides"):
        d = score.get("breakdown", {}).get(name, {})
        s, m = d.get("score", 0), d.get("max", 0)
        det = d.get("detail", "")
        ok = s >= m * 0.85 and m > 0
        mark = "✓" if ok else " "
        print(f"  {mark} {name:28} {s:3}/{m:<3}  {det}")
    print(f"{'='*72}\n", flush=True)


def print_flush_session_summary(ev: dict):
    sc = ev.get("score", {})
    print("  Phases seen via WS:", ev.get("phase"), "— end of stream.", flush=True)


# ---------------------------------------------------------------------------
# REST helpers
# ---------------------------------------------------------------------------

async def http(method: str, path: str, body: dict | None = None,
               base: str = "http://127.0.0.1:8080"):
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.request(method, f"{base}{path}", json=body) as r:
            text = await r.text()
            return r.status, (json.loads(text) if text and text != "null" else None)


async def status():
    import aiohttp
    base = "http://127.0.0.1:8080"
    ok, d = await http("GET", "/api/telemetry")
    print(f"  📡 {base}  (HTTP {ok})")
    if not d:
        print("  (orchestrator unreachable — did you start it?)")
        return
    print(f"\n  🧠 Model     {d.get('model',{}).get('display', d.get('model',{}).get('served','?'))}"
          f"   (served as {d.get('model',{}).get('served','?')}, "
          f"root {d.get('model',{}).get('root','?')})")
    print(f"  🖥️  Sparks ({len(d.get('sparks', []))}):")
    for sp in d.get("sparks", []):
        icon = "🟢" if sp.get("online") else "🔴"
        role = (sp.get("role") or "").upper().ljust(7)
        print(f"    {icon} {sp.get('name','?'):12} {role:8} "
              f"GPU {sp.get('gpu_util',0):>3}%   mem {sp.get('mem_util',0):>3}%   "
              f"cpu {sp.get('cpu_util',0):>3}%")
    ok2, r = await http("GET", "/api/running-session")
    last = r if r and r.get("session_id") else None
    if last:
        print(f"\n  ▶ Session   {last['session_id']}  "
              f"status={last['status']}  challenge={last.get('challenge_id','?')}")
    else:
        print("\n  ▶ no session currently tracked")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

async def run(challenge: str, mode: str, audience: str, verbose: bool,
              timeout: int = 600):
    ok, r = await http("POST", "/api/session/start",
                       body={"challenge_id": challenge, "mode": mode,
                             "audience": audience})
    if r is None or "error" in (r or {}):
        print(f"❌ start failed: {ok} {r}")
        return 1
    sid = r["session_id"]
    print(f"\n  ▶ {challenge} | mode={mode} | audience={audience}")
    print(f"  ▶ session {sid}\n  (live stream; Ctrl-C to abort)\n")
    t0 = asyncio.get_event_loop().time()
    try:
        await stream_ws(sid, None, timeout=timeout)
    except asyncio.CancelledError:
        print("\n  ⏸ cancelled by user", flush=True)
        return 1
    dt = asyncio.get_event_loop().time() - t0
    if verbose:
        print(f"  (ws stream took {dt:.1f}s wall time)", flush=True)
    return 0


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="run_demo.py",
        description="AI Dev Arena — one command, all four challenges.")
    p.add_argument("challenge", choices=["A","B","C","D","all","status"])
    p.add_argument("-m","--mode", default="guardrailed",
                   choices=["live","guardrailed","replay"],
                   help="live = no auto-rescues  |  guardrailed = auto-rescue (default)  "
                        "|  replay = scripted, no LLM (offline rehearsal)")
    p.add_argument("-a","--audience", default="broad",
                   choices=["broad","developers","executives"],
                   help="audience-tuned phrasing (see CONCEPTS.md)")
    p.add_argument("-v","--verbose", action="store_true")
    p.add_argument("-t","--timeout", type=int, default=600,
                   help="per-challenge timeout in seconds (default 600)")
    return p.parse_args(argv)


def main(argv):
    args = parse_args(argv)
    if args.challenge == "status":
        return asyncio.run(status())
    if args.challenge == "all":
        order = ["D", "C", "B", "A"]  # longest first, then short to long
        ret = 0
        for ch in order:
            code = asyncio.run(run(ch, args.mode, args.audience, args.verbose,
                                   args.timeout))
            if code != 0:
                ret = code
            print("\n" + "="*72 + "\n")
        return ret
    code = asyncio.run(run(args.challenge, args.mode, args.audience,
                           args.verbose, args.timeout))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
