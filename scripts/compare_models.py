#!/usr/bin/env python3
"""Model comparison: run all 3 challenges and report scores/times."""
import asyncio, aiohttp, time, json

BASE = "http://127.0.0.1:8080"

# Update the orchestrator's model name
async def set_model(model_name):
    # Update via environment - restart orchestrator with new MODEL_NAME
    async with aiohttp.ClientSession() as s:
        pass

async def run_challenge(s, challenge_id):
    """Start a challenge, poll until done, return (elapsed, score, score_pct)."""
    async with s.post(f"{BASE}/api/session/start", json={
        "challenge_id": challenge_id, "mode": "live"
    }) as r:
        d = await r.json()
    if isinstance(d, list):
        return None, None, None, f"Error: {d[0]}"
    sid = d["session_id"]

    for i in range(40):
        await asyncio.sleep(10)
        async with s.get(f"{BASE}/api/session/{sid}") as r:
            d = await r.json()
        if isinstance(d, list):
            # Session lost - restart
            async with s.post(f"{BASE}/api/session/start", json={
                "challenge_id": challenge_id, "mode": "live"
            }) as r2:
                d2 = await r2.json()
            if isinstance(d2, list):
                return 0, 0, 0, f"Error: {d2[0]}"
            sid = d2["session_id"]
            continue

        sc = d.get("score")
        if d["status"] == "completed" and sc:
            return d["elapsed"], sc["overall"], sc["percentage"], "OK"
    return 0, 0, 0, "Timeout"

async def wait_for_vllm(model_name):
    """Wait until vLLM is serving the specified model."""
    async with aiohttp.ClientSession() as s:
        for i in range(120):
            try:
                async with s.get(f"{BASE.split(':8080')[0].replace('127.0.0.1','127.0.0.1')}:8000/v1/models") as r:
                    data = await r.json()
                models = [m["id"] for m in data.get("data", [])]
                if model_name in models:
                    return True
            except Exception:
                pass
            await asyncio.sleep(5)
            if i % 6 == 0:
                print(f"  Waiting for {model_name}... ({(i+1)*5}s)")
    return False

async def main():
    print("=" * 60)
    print("MODEL COMPARISON: gpt-oss-120b vs Nemotron-3-Super-120B")
    print("=" * 60)

    async with aiohttp.ClientSession() as s:
        # Wait for gpt-oss-120b to be ready
        print("\nWaiting for gpt-oss-120b to load...")
        ready = await wait_for_vllm("gpt-oss-120b")
        if not ready:
            print("Model did not load in time")
            return

        # Verify it's actually the right model
        async with s.get("http://127.0.0.1:8000/v1/models") as r:
            data = await r.json()
        print("Active model:", [m["id"] for m in data.get("data", [])])

        # Run all 3 challenges
        challenges = [("A", "Feature Sprint"), ("B", "Bug Bash"), ("C", "Performance")]
        results = []

        for cid, cname in challenges:
            print(f"\n--- Challenge {cid} ({cname}) ---")
            start = time.time()
            elapsed, score, pct, status = await run_challenge(s, cid)
            wall = time.time() - start
            results.append((cid, cname, elapsed, wall, score, pct, status))
            print(f"  Model elapsed: {elapsed:.0f}s | Wall: {wall:.0f}s | Score: {score}/100 ({pct}%) | {status}")

        # Summary
        print("\n" + "=" * 60)
        print("SUMMARY: gpt-oss-120b on Two DGX Sparks")
        print("=" * 60)
        for cid, cname, elapsed, wall, score, pct, status in results:
            print(f"  {cid} {cname:20s}: {score:3d}/100 ({pct:2d}%) - {elapsed:.0f}s model / {wall:.0f}s wall")

        print("\nvs Nemotron baseline:")
        print("  A Feature Sprint:  96/100 (96%) - 121s")
        print("  B Bug Bash:        96/100 (96%) - 121s")
        print("  C Performance:     96/100 (96%) - 121s")

asyncio.run(main())
