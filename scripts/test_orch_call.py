#!/usr/bin/env python3
"""Test the orchestrator's call_llm with full prompt."""
import asyncio, aiohttp, time, json, sys, os

os.chdir("/home/nvidia/ai-dev-arena")
sys.path.insert(0, "/home/nvidia/ai-dev-arena/.venv/lib/python3.12/site-packages")

async def test():
    from orchestrator.main import call_llm, VLLM_URL, MODEL_NAME

    print("Using model:", MODEL_NAME, "at", VLLM_URL)

    app = open("challenge-repos/sample-app/app.py").read()
    test_code = open("challenge-repos/sample-app/tests/test_app.py").read()
    ch = json.load(open("orchestrator/challenges/challenge_a_feature_sprint.json"))
    prompt = ch["prompt"]

    start = time.time()
    result = await call_llm(prompt, "files...", app, test_code)
    elapsed = time.time() - start
    print("Wall time: %.1fs" % elapsed)
    print("reasoning_summary:", result.get("reasoning_summary", "(none)")[:200])
    if result.get("patches"):
        print("patches: %d" % len(result["patches"]))
        for p in result["patches"]:
            s = p.get("search", "")[:60]
            r = p.get("replace", "")[:60]
            print("  file=%s search=\"%s...\" replace=\"%s...\"" % (p.get("file"), s, r))
    else:
        print("No patches found!")

asyncio.run(test())
