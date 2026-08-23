#!/usr/bin/env python3
"""Test fallback: start a session, trigger fallback early, then poll."""
import asyncio, aiohttp

async def main():
    async with aiohttp.ClientSession() as s:
        # Start session A
        async with s.post("http://127.0.0.1:8080/api/session/start",
            json={"challenge_id": "A", "mode": "live"}) as r:
            d = await r.json()
        sid = d["session_id"]
        print("Started:", sid)
        
        # Wait 8s then trigger fallback while model still thinking
        await asyncio.sleep(8)
        async with s.post("http://127.0.0.1:8080/api/session/" + sid + "/fallback") as r:
            fb = await r.text()
        print("Fallback:", fb)
        
        # Poll for completion
        for i in range(30):
            await asyncio.sleep(10)
            async with s.get("http://127.0.0.1:8080/api/session/" + sid) as r:
                d = await r.json()
            if isinstance(d, list):
                print("Session lost")
                return
            sc = d.get("score")
            st = d["status"]
            el = d.get("elapsed", 0)
            if sc:
                print("Done in %ds - Score %s/%s (%s%%)" % (int(el), sc["overall"], sc["max_possible"], sc["percentage"]))
                for k, v in sc.get("breakdown", {}).items():
                    print("  %s: %s/%s - %s" % (k, v["score"], v["max"], v["detail"]))
                for t in d.get("test_results", []) + d.get("check_results", []):
                    tag = "PASS" if t["passed"] else "FAIL"
                    cmd = t["command"][:80]
                    print("  %s: %s" % (tag, cmd))
                return
            print("p%d: %s %ds" % (i+1, st, int(el)))
        print("Timeout")

asyncio.run(main())
