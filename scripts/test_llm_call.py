#!/usr/bin/env python3
"""Quick test: verify the orchestrator's LLM call path works end-to-end."""
import asyncio
import aiohttp
import json
import re

VLLM_URL = "http://127.0.0.1:8000/v1/chat/completions"
MODEL = "nvidia/nemotron-3-super"

SYSTEM = """You are a skilled software engineer. Return ONLY a JSON object with these keys:
reasoning_summary, new_app_py, new_test_py, tests_added_n
"""

TASK = """Here is the Flask app at /tmp/test-app/app.py:

def calculate_discount(subtotal, promo_code=None):
    discount = 0.0
    return round(subtotal - discount, 2)

Your task: Add promo code support. SAVE10=10%, WELCOME20=20%, VIP50=50%.
Return JSON with new_app_py containing the full updated function.
"""

async def main():
    async with aiohttp.ClientSession() as sess:
        async with sess.post(VLLM_URL, json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": TASK},
            ],
            "max_tokens": 2000,
            "temperature": 0.1,
        }) as resp:
            data = await resp.json()
            content = data["choices"][0]["message"]["content"]
            print(f"Response length: {len(content)} chars")
            print(f"First 300 chars:\n{content[:300]}")
            
            # Try to parse JSON
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                try:
                    result = json.loads(json_match.group())
                    print(f"\nParsed keys: {list(result.keys())}")
                    if "new_app_py" in result:
                        print(f"new_app_py length: {len(result['new_app_py'])}")
                    else:
                        print("WARNING: no new_app_py key found")
                    # Check for promo code keywords
                    for code in ["SAVE10", "WELCOME20", "VIP50"]:
                        if code in result.get("new_app_py", ""):
                            print(f"✅ {code} found in response")
                except json.JSONDecodeError as e:
                    print(f"JSON parse error: {e}")
                    print(f"Matched: {json_match.group()[:500]}")

asyncio.run(main())
