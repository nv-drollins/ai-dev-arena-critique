# Architecture — two Sparks, one model, three displays

## The big picture
```
                 ┌────────────────────────────  HEAD  (Spark #1, GB10) ────────────────────────────┐
                 │                                                                                 │
                 │  uvicorn orchestrator.main:app  ──  serves /operator /arena /theater            │
                 │  FastAPI (:8080)                 WebSocket /ws/{arena,theater,<sid>             │
                 │        │                          REST  /api/session/start, /api/telemetry …    │
                 │        │                                                                        │
                 │        │ 1) prompt        2) vLLM OpenAI-compat API  ──►  Tensor-Parallel-2     │
                 │        │                                                     │   (2 ranks)      │
                 │        ▼                                                     │        │         │
                 │   call_llm()  ──────────────────────────────────────────────┘        │ 100GbE   │
                 │                                                                       ▼         │
   100GbE ───────│   ┌───────────────┐   ┌────────────────────────────┐        ┌──────────────┐    │
   (enp1s0/nvLink)│   │  vLLM engine  │   │  GPU (GB10 Grace-Blackwell)│        │   WORKER     │   │
                 │   │  + Ray head    │   └────────────────────────────┘        │  (Spark #2)  │   │
                 │   └───────────────┘                                          │  Ray worker  │   │
                 │                                                              │  + 1 GPU      │  │
                 └────────────────────────────┬─────────────────────────────────┴──────────────────┘
                                                                                       │
                                                                                       ▼
                                                                    nvcr.io/nvidia/vllm docker container
```

## Where what lives

### Head node (Spark #1)
- **Orchestrator** — `orchestrator/main.py` on FastAPI/uvicorn, port **8080**.
  - Serves the three HTML dashboards in `frontend/`
  - Runs the agent (`run_agent`) against the chosen challenge
  - Stores sessions + event buffers in memory
  - Streams events to Arena + Theater over WebSocket
  - Calls **vLLM** for inference (OpenAI-compatible, port 8000)
  - Runs `run_validation` (pytest + challenge-specific checks)
  - Feeds results to `orchestrator/scoring.py`
- **vLLM engine** — inside the Ray head Docker container, `tensor-parallel-size 2`. It owns the **rank-0** slice of the model.
- **Model sharding** — each Spark holds a full copy of the HF cache (~150–250 GB); vLLM shards *tensors* across the two, so a single 120B-class model runs as one logical engine.
- **Ray head** — inside the same docker container. It's the cluster coordinator; the worker connects to it via `MASTER_ADDR`.

### Worker node (Spark #2)
- **Ray worker** — inside its own Docker container (`nvcr.io/nvidia/vllm…`). Runs the **rank-1** slice of the model, owns one GPU.
- **No project copy.** No `ai-dev-arena/`, no orchestrator, no venv, no git. Just the Ray/vLLM runtime + the HF cache. This is a property of how vLLM+Ray is deployed — the worker is a pure compute participant, not a service node.
- Only scripts it uses: `run_cluster.sh`, `start-ray-worker.sh` (both in this repo under `cluster/`).

## The request flow (Operator → Start)
1. Operator `POST /api/session/start {challenge_id, mode, audience}`
2. Orchestrator `reset_repo()` (copies `sample-app` into `.sessions/<sid>/sample-app`, git-init baselines it), creates session, spawns `run_agent(session_id, challenge, work_dir)` in an asyncio task, returns `{session_id}`.
3. `run_agent` streams events via `_broadcast` to the WebSocket subscribers for that session (both the Arena and the Theater tab, if open).
4. Branches on `mode`:
   - `replay` — apply golden, broadcast "edited" → validation
   - `live` / `guardrailed` — call `call_llm` against vLLM `/v1/chat/completions` with the full code context (file tree + app.py + test_app.py), expect JSON with search/replace `patches`. Heartbeat events fire every ~15s while waiting.
5. Applies patches to a scratch copy. If a patch's `search` doesn't match the file, that patch is rejected (with a warning event) and the others are still evaluated.
6. If the model left no source change (e.g. it only changed tests, or all patches failed):
   - `guardrailed`: auto-applies the `golden_branch` solution (via `apply_golden`)
   - `live`: emits a warning event but leaves it; the operator can press **⚡ Fallback** to force golden
7. Runs `run_validation` (the command list in `challenge.validation.tests` + `.checks`), captures pytest/`--benchmark` output.
8. Computes `score_session` (see SCORING.md).
9. Broadcasts `completed` with the score, sets `status=completed`.

## Cross-tab / cross-device refresh
- Operator → Arena/Theater: `BroadcastChannel('arena_notify')` + a `localStorage` fallback, fired on Start and on Complete.
- Both viewers poll `/api/running-session` every 2s and *auto-discover* any new session regardless (so even a cold browser tab, or a new display on a different machine, picks up the new run in one poll).
- The Arena's cluster panel polls `/api/telemetry` every 3s — that call is the one that reads GPU metrics on the head plus (via SSH) the worker.

## Real-time cluster gauges — how it works
`orchestrator/telemetry.py::collect_telemetry()`:
1. Polls HEAD directly (local process calls).
2. Polls every node in `SPARK_NODES` (currently node2/192.168.1.149) over local-SSH (passwordless). Each node's probe is a 1-second `bash -c` that runs:
   - `nvidia-smi dmon -s u -c 2 --format=csv,noheader` → `sm` column = **GPU compute utilization** (this is the *real* metric on GB10 — the classic `utilization.gpu` query is unreliable there when a big MoE decode happens in short bursts).
   - `cat /proc/stat` → CPU utilization (sampled twice)
   - `cat /proc/meminfo` → memory used/total
3. Node2's `nvidia-smi` is the one that owns the rank-1 GPU and shows the **real** SM load.
4. Results are cached by the frontend and rendered as two arc gauges per Spark — GPU + GPU-memory + CPU — plus a model badge that reads vLLM's `GET /v1/models` (what's *actually* served right now, not what we think is served).

Scalable: adding a 3rd Spark = append to the `SPARK_NODES` env var on the head (comma-separated `name,host` pairs); the frontend auto-renders one gauge card per node — no other code change.

## Model + memory notes (GB10 / Grace Blackwell)
- **Unified memory.** The GB10 CPU and GPU share one DRAM pool, so:
  - `nvidia-smi --query-gpu=memory.used` often returns `[N/A]` — the arena gauges read **DRAM** via `/proc/meminfo`, which is the metric that matters for a model+KV-cache running.
  - The "GPU 95% / mem 94%" you see during a decode is the DRAM pool *in heavy use by the model and its activations*, and SM utilization (from `nvidia-smi dmon`) hitting 90+% is the actual compute load.
- **KV cache** — vLLM pre-allocates ~50 GiB for the KV pool across the two GPUs (see the vLLM log lines at startup). That's what pushes mem_util so high even when the model isn't being *queried*.

## Where each file is
| Concern | File(s) |
|---|---|
| Serves UI, sessions, agent loop | `orchestrator/main.py` |
| Scoring | `orchestrator/scoring.py` |
| Telemetry | `orchestrator/telemetry.py` |
| Challenge definitions | `orchestrator/challenges/*.json` |
| The app + tests + golden solutions | `challenge-repos/sample-app/` |
| Frontends | `frontend/{operator,arena,theater}.html` |
| Ray/vLLM bring-up | `cluster/run_cluster.sh`, `cluster/start-ray-head.sh`, `cluster/start-ray-worker.sh`, `cluster/launch-gptoss.sh`, `cluster/launch-nemotron-super.sh`, `cluster/parsers/super_v3_reasoning_parser.py` |
