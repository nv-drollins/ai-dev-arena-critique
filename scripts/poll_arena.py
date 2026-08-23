#!/usr/bin/env python3
"""Poll arena session until complete, print results."""
import asyncio, aiohttp, json, sys

BASE = "http://localhost:8080"

async def main():
    sess_id = sys.argv[1] if len(sys.argv) > 1 and len(sys.argv[1]) > 4 else None
    challenge = sys.argv[1] if len(sys.argv) > 1 and len(sys.argv[1]) <= 4 else (sys.argv[2] if len(sys.argv) > 2 else "A")

    async with aiohttp.ClientSession() as sess:
        # Start new session if no SID provided
        if not sess_id:
            challenge = sys.argv[2] if len(sys.argv) > 2 else "A"
            async with sess.post(f"{BASE}/api/session/start", json={
                "challenge_id": challenge, "mode": "live"
            }) as resp:
                raw = await resp.json()
                if isinstance(raw, list) and len(raw) >= 1:
                    print(f"Error: {raw[0]}")
                    sys.exit(1)
                sess_id = raw["session_id"]
            print(f"Start session: {sess_id}")

        # Poll
        for i in range(40):
            await asyncio.sleep(10)
            async with sess.get(f"{BASE}/api/session/{sess_id}") as resp:
                raw = await resp.json()
                if isinstance(raw, list):
                    print(f"Session lost (orchestrator restart?), restarting...")
                    async with sess.post(f"{BASE}/api/session/start", json={
                        "challenge_id": challenge, "mode": "live"
                    }) as resp2:
                        new_raw = await resp2.json()
                        if isinstance(new_raw, list):
                            print(f"Error: {new_raw[0]}")
                            sys.exit(1)
                        sess_id = new_raw["session_id"]
                    print(f"New session: {sess_id}, continuing poll...")
                    continue
                d = raw
            status = d["status"]
            elapsed = d.get("elapsed", 0)
            score = d.get("score")
            if status == "completed" and score:
                print(f"\nCompleted in {elapsed:.0f}s")
                print(f"Score: {score['overall']}/{score['max_possible']} ({score['percentage']}%)")
                print(f"Changes: {len(d.get('changes', []))} files, {d.get('diff_size', 0)} chars")
                for k, v in score.get("breakdown", {}).items():
                    print(f"  {k}: {v['score']}/{v['max']} - {v['detail']}")
                for t in d.get("test_results", []):
                    tag = "PASS" if t["passed"] else "FAIL"
                    print(f"  TEST {tag}: {t['command'][:80]}")
                for t in d.get("check_results", []):
                    tag = "PASS" if t["passed"] else "FAIL"
                    print(f"  check {tag}: {t.get('output','')[:150]}")
                return
            elif status == "completed":
                print(f"Completed but no score after {elapsed:.0f}s")
                return
            else:
                print(f"P{i+1}: running {elapsed:.0f}s")
        print("Timeout waiting for completion")

asyncio.run(main())
