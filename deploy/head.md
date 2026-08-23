# Head subset (SPARK #1 in a 2-Spark arena)

This is the "control plane" — everything user-facing.

| Item | Path | Purpose |
|---|---|---|
| `uvicorn orchestrator.main:app` | `orchestrator/` | Serves the 3 UIs, runs the agent, calls vLLM, scores, streams |
| `vllm serve` (rank 0 of TP=2) | inside the Ray head Docker container | The model engine |
| `cluster/start-ray-head.sh` | `cluster/` | Starts Ray head inside Docker on this node |
| `cluster/launch-gptoss.sh` | `cluster/` | Launches `openai/gpt-oss-120b` as the served model (TP=2, `:8000`) |
| `cluster/launch-nemotron-super.sh` | `cluster/` | Launches `nvidia/nemotron-3-super` (the *reasoning* model) |
| `scripts/run_demo.py` | `scripts/` | The demo CLI (all 4 challenges, status, live stream) |
| `scripts/spark-model-swap.sh` | `scripts/` | Swaps between gpt-oss-120b and Nemotron-3-Super (clean GPU release) |
| `challenge-repos/` | `challenge-repos/` | The sample app + tests + golden solutions (read-only to the head's agent at runtime; it writes into `.sessions/`) |

## Ports

- **8080** — orchestrator HTTP+WS (Arena, Theater, Operator, /api/*)
- **8000** — vLLM HTTP (OpenAI-compatible) — internal to the head; the
  orchestrator is the only client

## What the head does NOT do
It does **not** run any "worker-specific" code. It's the coordinator *and*
rank-0 of the model. The worker is rank-1 and nothing else.
