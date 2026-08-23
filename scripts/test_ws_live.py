#!/usr/bin/env python3
"""Test that WS streaming delivers live events — fixed to accept challenge arg."""
import asyncio, aiohttp, json, sys

async def main():
    challenge_id = sys.argv[1] if len(sys.argv) > 1 else "C"
    print(f"Testing WS live events for Challenge {challenge_id}")

    events = []
    async with aiohttp.ClientSession() as s:
        # Start session
        r = await s.post("http://127.0.0.1:8080/api/session/start",
            json={"challenge_id": challenge_id, "mode": "live"})
        d = await r.json()
        sid = d["session_id"]
        print(f"Session: {sid}")

        # Connect WS and collect events
        async with s.ws_connect(f"ws://127.0.0.1:8080/ws/arena/{sid}") as ws:
            async def collector():
                try:
                    while True:
                        msg = await ws.receive_json()
                        events.append(msg)
                        if msg.get("score"):
                            break
                except Exception:
                    pass
            asyncio.create_task(collector())

            # Wait for completion - longer timeout for model reasoning
            for attempt in range(60):
                await asyncio.sleep(10)
                try:
                    r2 = await s.get(f"http://127.0.0.1:8080/api/session/{sid}")
                    dd = await r2.json()
                except Exception:
                    print("  Poll failed, retrying...")
                    continue
                if dd.get("score"):
                    break

            print(f"\nEvents received via live WebSocket: {len(events)}")
            for e in events:
                t = round(e.get("elapsed", 0), 1)
                print(f"  [{t}s] {e['phase']}: {e.get('message', '')[:80]}")
            if dd.get("score"):
                print(f"\nFinal score: {dd['score']['overall']} / {dd['score']['max_possible']}")

asyncio.run(main())
