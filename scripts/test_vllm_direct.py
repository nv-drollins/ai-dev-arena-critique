#!/usr/bin/env python3
"""Quick test: call vLLM directly and inspect the response structure."""
import asyncio, aiohttp, json

async def main():
    async with aiohttp.ClientSession() as sess:
        async with sess.post(
            "http://127.0.0.1:8000/v1/chat/completions",
            json={
                "model": "nvidia/nemotron-3-super",
                "messages": [
                    {"role": "system", "content": "Return ONLY a valid JSON object with keys: answer, explanation."},
                    {"role": "user", "content": "What is 2+2?"},
                ],
                "max_tokens": 200,
                "temperature": 0.1,
            },
            timeout=60,
        ) as resp:
            data = await resp.json()
    
    print("Full response keys:", list(data.keys()))
    if "choices" in data:
        msg = data["choices"][0]["message"]
        print("Message keys:", list(msg.keys()))
        print("content:", msg.get("content", "(none)"))
        print("reasoning:", msg.get("reasoning", "(none)")[:300] if msg.get("reasoning") else "(none)")
    else:
        print("ERROR:", data.get("error"))

asyncio.run(main())
