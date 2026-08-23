#!/usr/bin/env python3
"""Exact replication of orchestrator call_llm() to diagnose the empty-error bug."""
import asyncio, aiohttp, json, re, sys, os, time

VLLM_URL = "http://127.0.0.1:8000"
MODEL_NAME = "nvidia/nemotron-3-super"

os.chdir("/home/nvidia/ai-dev-arena")

ch = json.load(open("orchestrator/challenges/challenge_a_feature_sprint.json"))
prompt_task = ch["prompt"].replace("{{repo_path}}", "/home/nvidia/ai-dev-arena/.sessions/test/sample-app")

app_code = open("challenge-repos/sample-app/app.py").read()
test_code = open("challenge-repos/sample-app/tests/test_app.py").read()

meta_prompt = (
    "You are a software engineer modifying a Flask web application.\n\n"
    f"FULL app.py ({len(app_code)} chars):\n---\n{app_code}\n---\n\n"
    f"FULL tests/test_app.py ({len(test_code)} chars):\n---\n{test_code[:3000]}\n---\n\n"
    "TASK:\n" + prompt_task + "\n\n"
    "Return ONLY a valid JSON object with keys:\n"
    "  reasoning_summary, patches (list of {file, search, replace}), tests_added_n\n\n"
    "RULES:\n"
    "- Each patch.search must be an EXACT substring from the original code\n"
    "- Each patch.replace is what to substitute in its place\n"
    "- Include ONLY blocks that need changing — most tasks need 1-2 patches\n"
    "- Keep patches small: 3-30 lines per patch\n"
    "- Return at most 3 patches total\n"
    "- Return ONLY valid JSON, no explanation text outside the JSON"
)

async def main():
    print(f"Calling {VLLM_URL} with {MODEL_NAME}")
    print(f"Meta-prompt length: {len(meta_prompt)} chars")

    payloads = []
    start = time.time()

    async with aiohttp.ClientSession() as sess:
        try:
            async with sess.post(
                f"{VLLM_URL}/v1/chat/completions",
                json={
                    "model": MODEL_NAME,
                    "messages": [
                        {"role": "system", "content": "You are a coding assistant. Return ONLY valid JSON with search/replace patches."},
                        {"role": "user", "content": meta_prompt},
                    ],
                    "max_tokens": 5000,
                    "temperature": 0.1,
                },
                timeout=600,
            ) as resp:
                payloads.append(await resp.json())

        except Exception as e:
            print(f"REQUEST FAILED: {type(e).__name__}: {e}")
            return

    wall = time.time() - start
    print(f"\nWall time: {wall:.1f}s")

    payload = payloads[0]
    msg = payload["choices"][0]["message"]
    content = msg.get("content") or msg.get("reasoning", "") or ""
    finish_reason = payload["choices"][0].get("finish_reason", "")

    print(f"Content length: {len(content)} chars")
    print(f"Finish reason: {finish_reason}")

    if "reasoning" in msg:
        print(f"reasoning field present, length: {len(msg['reasoning'])} chars")
        print(f"reasoning first 300: ...{msg['reasoning'][-300:]}")

    print(f"\nContent first 500:\n{content[:500]}")
    print(f"\nContent last 300:\n...{content[-300:]}")

    json_match = re.search(r'\{.*\}', content, re.DOTALL)
    if json_match:
        try:
            parsed = json.loads(json_match.group())
            print(f"\nParsed JSON keys: {list(parsed.keys())}")
            if parsed.get("patches"):
                print(f"Patches: {len(parsed['patches'])}")
                for p in parsed["patches"]:
                    print(f"  file={p.get('file')} search_len={len(p.get('search',''))} replace_len={len(p.get('replace',''))}")
            elif parsed.get("new_app_py"):
                print(f"Legacy format: new_app_py length={len(parsed['new_app_py'])}")
        except json.JSONDecodeError as e:
            print(f"JSON parse error: {e}")
            print(f"Matched first 300: {json_match.group()[:300]}")
    else:
        print("No JSON found in response!")

asyncio.run(main())
