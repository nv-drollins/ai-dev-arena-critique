# ⚡ AI Dev Arena — Agentic Edition

**An all-NVIDIA, on-box autonomous coding demo running on two DGX Sparks.**

An autonomous coding agent — **Hermes driving a local Nemotron-3.5-Lightning-30B** —
reads a real buggy repo, edits the code, runs the tests, and iterates until they
pass. A large **70B reviewer** (Llama-3.3-Nemotron-70B-Feedback, **tensor-parallel
across both Sparks**) then judges the result — its verdict drives the code-quality
and efficiency scores. Everything is streamed live to three displays: the **Arena**
scoreboard, the **Code Theater** (files / diff / terminal / agent activity), and the
**Operator** console.

Nothing leaves the room: both models are NVIDIA open weights, served locally by vLLM,
air-gapped.

```
📋 Challenge  →  🤖 Agentic Hermes (Nemotron-30B, local)  →  🔎 70B Review  →  📊 Score
                 read → edit → test → iterate                both Sparks       /100
                 (self-verifies against pytest)              verdict → score
```

---

## What makes it interesting

- **A real autonomous agent, not a chatbot** — the agent explores the repo, runs
  `pytest` itself, reads tracebacks, and fixes iteratively, all with tool calls.
- **All-NVIDIA, on-box** — writer (30B) + reviewer (70B) are both local Nemotron
  models on the Sparks. No external API, no data leaving the room.
- **The 70B reviewer's verdict drives the score** — code-quality + efficiency come
  from a real senior-model review (`ship` / `ship-with-nits` / `needs-work`), not a
  diff-size heuristic.
- **Accelerated with MTP speculative decoding + prefix caching** — Nemotron's native
  Multi-Token Prediction draft head (~2 tokens/pass) on Blackwell.
- **4 challenges** (A/B/C/D) on the same Flask sample app, each graded by a fixed
  pytest spec; **6-category scoring** out of 100 with a live breakdown.
- **Two demo modes**: `agentic` (the star) and `replay` (a scripted golden run — the
  reliable fallback for a hard challenge or a no-network rehearsal).

---

## Prerequisites

| | |
|---|---|
| **Hardware** | 2× NVIDIA DGX Spark (GB10 / Grace Blackwell), one high-speed link between them |
| **OS / drivers** | Linux with the NVIDIA driver (visible under `/proc/driver/nvidia`) |
| **Container** | Docker + `nvidia-container-toolkit`; the `vllm/vllm-openai:v0.27.1` image (~30 GB, pulled on install) |
| **Runtime** | Python 3.11+, `tmux`, `git`, `curl`, `ssh` between the two boxes (key-based) |
| **Agent** | [Hermes Agent](https://hermes-agent.nousresearch.com) installed on the head node (drives the writer as an agent) |
| **Weights** | `nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4` (writer) + `llama-3.3-nemotron-70b-feedback` (reviewer), auto-downloaded into each Spark's `~/.cache/huggingface` on first launch (several hundred GB — **not** in the repo) |

Roles used below: **head** = the Spark running the orchestrator + reviewer shard;
**worker** = the Spark serving the writer (`:8001`) + the reviewer's other TP shard.
Node IPs and ports live in [`bin/arena.conf`](bin/arena.conf) — edit it for your LAN.

---

## Installation

One-time bring-up, run **in parallel** on the two boxes (they wait for each other):

```bash
git clone https://github.com/nv-drollins/spark-cluster-ai-dev.git ai-dev-arena
cd ai-dev-arena
cp bin/arena.conf bin/arena.conf.local   # edit node IPs/ports for your cluster

# on the HEAD Spark:
bash bin/install-head.sh      # prereq check, pull vLLM image, venv + deps,
                              # Ray head, orchestrator on :8080

# on the WORKER Spark (in parallel):
bash bin/install-worker.sh    # prereq check, Ray worker joins the head
```

Then the two agentic-specific pieces:

```bash
# 1) Writer vLLM with tool-calling + MTP + prefix caching (run on the WORKER):
bash bin/launch-writer.sh     # serves nemotron-lightning-30b on :8001

# 2) A Hermes profile pointed at the local writer (run on the HEAD):
hermes profile create nemo
hermes config set model.provider custom            -p nemo
hermes config set model.base_url http://<worker-ip>:8001/v1 -p nemo
hermes config set model.default  nemotron-lightning-30b     -p nemo
hermes config set model.api_key  dummy-key         -p nemo
hermes config set model.context_length 64000       -p nemo
hermes config set model.max_tokens     8192        -p nemo --force
hermes config set agent.max_turns      12          -p nemo --force
```

The orchestrator spawns `hermes chat -p nemo` per agentic run — that profile is what
makes the agent talk to your local Nemotron.

Open the displays:

```
http://<head-ip>:8080/operator    # pick a challenge → Start
http://<head-ip>:8080/arena        # scoreboard + phases + cluster gauges
http://<head-ip>:8080/theater      # files / diff / terminal / live agent activity
```

---

## Start / Stop / Restart

Named wrappers around [`bin/sparkctl.sh`](bin/sparkctl.sh) (which is the full control
tool — run it with no args for the complete verb list):

```bash
bash bin/start.sh            # bring the whole cluster up (idempotent)
bash bin/stop.sh             # full teardown (orchestrator → models → Ray)
bash bin/stop.sh model       # quick "release the GPUs" — keeps Ray warm
bash bin/restart.sh          # restart the model + orchestrator layer
bash bin/sparkctl.sh status  # model, GPUs, containers, orchestrator, telemetry
```

Component-level control (when you only need to bounce one thing):

```bash
bash bin/launch-writer.sh    # (re)start the writer vLLM  (on the worker)
bash bin/launch-critic.sh    # (re)start the 70B reviewer (TP=2 across both)
bash bin/restart-orch.sh     # (re)start just the orchestrator with the agentic env
```

`restart-orch.sh` is the one to reach for after editing `orchestrator/` code — it
kills any stale instance (by port *and* process name) and relaunches with the
writer/critic env wired in.

---

## Using it

**Operator console** (`/operator`):
1. Pick a challenge (A feature · B bug · C performance · D loyalty).
2. Mode is **🤖 Agentic** by default (Replay is the scripted fallback).
3. Press **Start** — the Arena and Theater refresh automatically.

Watch the **Theater** for the agent's live tool activity (📖 read → ✏️ edit → 🧪 test
→ ↻ iterate), then the 70B review, then the score. The **Arena** shows the phase
timeline, two clocks (writing vs reviewing), the cluster gauges, and the scoreboard.

**Challenge notes:** A/B/C land in the 90s in ~1–2 min; **C** (performance) has an
`agent_hint` so it goes straight to the O(n²) fix; **D** (build a loyalty feature
from scratch) is the honest hard one — it scores in the 50s–70s and the reviewer
says `needs-work`. Use **Replay** on D if you need a guaranteed clean walkthrough.

---

## What each part is

| Piece | Where | Role |
|---|---|---|
| **Orchestrator** (FastAPI/uvicorn) | `orchestrator/main.py` | Serves the 3 UIs, spawns the agent, runs validation, scores, streams over WebSocket |
| **Agentic runner** | `orchestrator/main.py` → `run_agentic()` | Spawns `hermes chat -p nemo`, streams tool activity, collects the git diff |
| **Scoring engine** | `orchestrator/scoring.py` | 6-category 0–100 score; code-quality + efficiency driven by the 70B verdict |
| **Telemetry** | `orchestrator/telemetry.py` | Per-Spark GPU / mem / CPU (N-Spark scalable) + model |
| **Challenges** | `orchestrator/challenges/*.json` | A/B/C/D (prompt, golden branch, validation, weights, optional `agent_hint`) |
| **Sample app** | `challenge-repos/sample-app/` | The Flask app + `tests/` + `golden/<branch>/` reference solutions |
| **Frontend** | `frontend/{operator,arena,theater}.html` | The three displays (vanilla HTML/JS) |
| **Control** | `bin/` | `install-head.sh` / `install-worker.sh`, `sparkctl.sh`, `start`/`stop`/`restart`, `launch-writer.sh` / `launch-critic.sh` / `restart-orch.sh`, `arena.conf` |

---

## Docs

- **[Concepts: modes · challenges · scoring](docs/CONCEPTS.md)**
- **[Scoring: the 6 categories and the math](docs/SCORING.md)**
- **[Architecture: two Sparks, the agentic flow](docs/ARCHITECTURE.md)**
- **[Cluster ops: install / start / stop / restart](docs/CLUSTER_OPS.md)**
