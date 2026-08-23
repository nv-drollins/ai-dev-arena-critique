#!/usr/bin/env python3
"""Quick WS live-stream tester — args: challenge_id mode audience
   Prints each event live (phase: message), then prints the score at the end."""
import asyncio, aiohttp, json, sys

async def main():
    challenge = sys.argv[1] if len(sys.argv) > 1 else "C"
    mode      = sys.argv[2] if len(sys.argv) > 2 else "live"
    audience  = sys.argv[3] if len(sys.argv) > 3 else "broad"
    base = "http://127.0.0.1:8080"
    timeout = int(sys.argv[4]) if len(sys.argv) > 4 else 90
    print(f"Challenge {challenge} | mode={mode} | audience={audience} | timeout={timeout}s")

    async with aiohttp.ClientSession() as s:
        r = await s.post(f"{base}/api/session/start",
            json={"challenge_id": challenge, "mode": mode, "audience": audience})
        d = await r.json()
        sid = d["session_id"]
        print(f"Session: {sid}")

        saw_score = False
        try:
            async with s.ws_connect(f"ws://127.0.0.1:8080/ws/arena/{sid}", timeout=2) as ws:
                async def reader():
                    nonlocal saw_score
                    async for m in ws:
                        if m.type not in (1, None):  # text message
                            continue
                        ev = json.loads(m.data)
                        ph = ev.get("phase")
                        msg = ev.get("message")
                        el = ev.get("elapsed")
                        print(f"  [{el}s] {ph}: {msg}")
                        if ev.get("score"):
                            sc = ev["score"]
                            print(f"\n=== SCORE: {sc['overall']}/{sc['max_possible']} "
                                  f"({sc.get('percentage','?')}%) ===")
                            for cat, v in sc.get("breakdown", {}).items():
                                tag = "ok " if v["score"] >= v["max"] * 0.85 else "!! "
                                print(f"  {tag}{cat:32} {v['score']:3}/{v['max']:<3}  "
                                      f"{v.get('detail','')}")
                            saw_score = True
                            break
                        if ph in ("completed", "error"):
                            # may not carry score — fetch from REST as fallback
                            pass

                # poller fallback
                async def poller():
                    nonlocal saw_score
                    end = False
                    for _ in range(timeout // 3):
                        await asyncio.sleep(3)
                        try:
                            rr = await s.get(f"{base}/api/session/{sid}")
                            dd = await rr.json()
                            if dd.get("score") and not saw_score:
                                sc = dd["score"]
                                print(f"\n=== SCORE (via REST): {sc['overall']}/{sc['max_possible']} "
                                      f"({sc.get('percentage','?')}%) ===")
                                for cat, v in sc.get("breakdown", {}).items():
                                    print(f"      {cat:32} {v['score']:3}/{v['max']:<3}  {v.get('detail','')}")
                                saw_score = True
                                end = True
                                break
                            if dd.get("status") == "completed" and not saw_score:
                                # completed but no score yet, keep polling briefly
                                continue
                        except Exception:
                            pass
                    if not saw_score and not end:
                        print("(no score arrived within timeout)")

                rt = await asyncio.gather(reader(), poller(), return_exceptions=True)
                for e in rt:
                    if e and not e.cancelled():
                        print(f"  [reader/poller] {type(e).__name__}: {e}")
        except Exception as e:
            print(f"  WS error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
