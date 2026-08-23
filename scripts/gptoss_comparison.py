#!/usr/bin/env python3
"""GPT-OSS comparison runner: start orchestrator, run all 3 challenges, compare."""
import asyncio, aiohttp, subprocess, time, json, os, sys

BASE = "http://127.0.0.1:8080"

def check_model():
    """Verify gpt-oss-120b is actually served."""
    try:
        r = subprocess.run(
            ["curl", "-s", "http://127.0.0.1:8000/v1/models"],
            capture_output=True, text=True, timeout=5
        )
        data = json.loads(r.stdout)
        return data["data"][0]["id"]
    except Exception as e:
        return str(e)

def check_orch_model():
    """Check what MODEL_NAME the orchestrator process has."""
    try:
        r = subprocess.run(
            ["curl", "-s", "http://127.0.0.1:8080/"],
            capture_output=True, text=True, timeout=5
        )
        return r.stdout.strip()
    except Exception as e:
        return str(e)

async def run_challenge(s, challenge_id):
    """Start a challenge, poll until completion, return (elapsed, score, pct, changes, status)."""
    async with s.post(f"{BASE}/api/session/start", json={
        "challenge_id": challenge_id, "mode": "live"
    }) as r:
        d = await r.json()
    if isinstance(d, list):
        return 0, 0, 0, 0, f"Error: {d[0]}"
    sid = d["session_id"]

    for _ in range(40):
        await asyncio.sleep(10)
        async with s.get(f"{BASE}/api/session/{sid}") as r:
            d = await r.json()
        if isinstance(d, list):
            # Session lost, restart
            async with s.post(f"{BASE}/api/session/start", json={
                "challenge_id": challenge_id, "mode": "live"
            }) as r2:
                d2 = await r2.json()
            if isinstance(d2, list):
                return 0, 0, 0, 0, f"Error: {d2[0]}"
            sid = d2["session_id"]
            continue

        sc = d.get("score")
        if d["status"] == "completed" and sc:
            changes = d.get("diff_size", 0)
            return d["elapsed"], sc["overall"], sc["percentage"], changes, "OK"
    return 0, 0, 0, 0, "Timeout"

async def main():
    model_name = check_model()
    print("=" * 70)
    print("MODEL COMPARISON RUN: gpt-oss-120b vs Nemotron-3-Super-120B")
    print("=" * 70)
    print(f"\nvLLM serving: {model_name}")

    if model_name != "gpt-oss-120b":
        print(f"\nERROR: Expected gpt-oss-120b but vLLM serves {model_name}")
        print("Cannot proceed with fair comparison.")
        sys.exit(1)

    orch_status = check_orch_model()
    print(f"Orchestrator: {orch_status}")

    if "running" not in orch_status:
        print("Orchestrator not reachable - check logs")
        sys.exit(1)

    challenges = [
        ("A", "Feature Sprint"),
        ("B", "Bug Bash"),
        ("C", "Performance"),
    ]

    nemotron_results = [
        ("A", "Feature Sprint", 121, 96, 96, 952),
        ("B", "Bug Bash", 121, 96, 96, 1375),
        ("C", "Performance", 121, 96, 96, 850),
    ]

    async with aiohttp.ClientSession() as s:
        results = []
        for cid, cname in challenges:
            print(f"\n>>> Challenge {cid}: {cname}")
            wall_start = time.time()
            elapsed, score, pct, changes, status = await run_challenge(s, cid)
            wall = time.time() - wall_start
            results.append((cid, cname, elapsed, wall, score, pct, changes, status))
            print(f"    Model elapsed: {elapsed:.0f}s | Wall: {wall:.0f}s | Score: {score}/100 ({pct}%) | {changes} chars changed | {status}")

        print("\n" + "=" * 70)
        print("HEAD-TO-HEAD COMPARISON")
        print("=" * 70)
        print(f"{'Challenge':<25} {'Model':<20} {'Score':<8} {'Time':<8} {'Chars':<8} {'Notes':<15}")
        print("-" * 85)
        for i, (cid, cname, elapsed, wall, score, pct, changes, status) in enumerate(results):
            n = nemotron_results[i]
            print(f"  {cid} {cname:<21s} Nemotron-3-Super   {n[3]:>3d}/100   {n[2]:>5.0f}s   {n[5]:>5d}  121s per challenge")
            print(f"  {'':25s} gpt-oss-120b        {score:>3d}/100   {elapsed:>5.0f}s   {changes:>5d}  {status}")
            print()

        # Stats
        gpt_avg_time = sum(r[2] for r in results) / len(results)
        gpt_avg_score = sum(r[4] for r in results) / len(results)
        print("AVERAGE:")
        print(f"  Nemotron-3-Super:  96/100 avg, 121s per challenge")
        print(f"  gpt-oss-120b:      {int(gpt_avg_score)}/100 avg, {int(gpt_avg_time)}s per challenge")
        print("=" * 70)

asyncio.run(main())
