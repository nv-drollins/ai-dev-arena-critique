# ⚡ AI Dev Arena — Trade-Show Demo

**A two-NVIDIA-DGX-Spark "AI software engineer" live demo.** One 120B class model,
split across two GB10 Sparks (tensor-parallel 2 over the 100GbE link), "solves"
real coding challenges from a Flask checkout app — live-streamed to three
displays: the **Arena** scoreboard, the **Code Theater** (files/diff/terminal),
and the **Operator** console (pick challenge, mode, audience — press Start).

Built and tuned on two real DGX Sparks (Grace Blackwell, GB10). Ships with:

- **4 challenges** (A/B/C/D) on the same sample app, each with a golden solution
- **6-category scoring engine** (out of 100) with a live breakdown
- **3 demo modes** that are actually different: `live`, `guardrailed`, `replay`
- **Audience-aware phrasing** (broad / developers / executives)
- **Real-time cluster gauges** (per-Spark GPU + GPU-memory) + model badge
- **A terminal demo CLI** — run all four, see live stream + score table
- **Model hot-swap** script (gpt-oss-120b ↔ Nemotron-3-Super)
- **Head → worker deploy** over the fast link (worker runs *only* Ray+vLLM)

---

## Quick start (two Sparks already set up)

```bash
# 0) the two Sparks' cluster is up (run_cluster.sh --head / --worker), vLLM
#    is serving. Open the Operator:
http://<head>:8080/operator      # pick challenge, mode, audience → Start
http://<head>:8080/arena         # scoreboard + live phases + cluster gauges
http://<head>:8080/theater       # files / diff / terminal / activity

# 1) Or drive it all from the terminal:
python3 scripts/run_demo.py status       # model + both Sparks' telemetry
python3 scripts/run_demo.py D            # run D (default: guardrailed + broad)
python3 scripts/run_demo.py all          # D → C → B → A, live stream + scores
```

The Operator and the CLI both hit the same orchestrator on the head node.

---

## The models (and why)

| Served name | What it is | Per-challenge time | Notes |
|---|---|---|---|
| `gpt-oss-120b` | 120B MoE (OpenAI gpt-oss) | **~30–90s** | **Recommended demo model** — fast, writes clean patches to spec |
| `nvidia/nemotron-3-super` | Nemotron-3-Super (NVFP4 MoE) | **4–11 min** | *Reasoning* model — always emits a long chain-of-thought, slower per token. Great for "deep" story, too slow for a punchy stage run |

Both run tensor-parallel-2 across the two Sparks. Swap with
`scripts/spark-model-swap.sh` (releases the GPUs cleanly and repoints the
orchestrator). See [SCORING.md](docs/SCORING.md) for why the model choice moves
the `time_to_result` and `efficiency` buckets.

---

## What each part is

| Piece | Where | Role |
|---|---|---|
| **Orchestrator** (FastAPI/uvicorn) | `orchestrator/` | Serves the 3 UIs, runs the agent, calls vLLM, validates, scores, streams over WebSocket |
| **Scoring engine** | `orchestrator/scoring.py` | 6-category 0–100 score |
| **Telemetry** | `orchestrator/telemetry.py` | Per-Spark GPU / mem / CPU (N-Spark scalable) + model |
| **Challenges** | `orchestrator/challenges/*.json` | A/B/C/D definitions (prompt, golden branch, validation, weights) |
| **Sample app** | `challenge-repos/sample-app/` | The Flask app + `tests/` + `golden/<branch>/` reference solutions |
| **Frontend** | `frontend/{operator,arena,theater}.html` | The three displays (vanilla HTML/JS) |
| **Cluster scripts** | `cluster/` | Ray+vLLM head/worker bring-up, model launchers, Nemotron parser |
| **Control toolchain** | `bin/` | `install-head.sh` / `install-worker.sh` (one-time bring-up) + `sparkctl.sh` (start/stop/restart/status/model/doctor) + `start.sh`/`stop.sh`/`restart.sh` (named wrappers) + `arena.conf` (config) |
| **Scripts** | `scripts/` | `run_demo.py` (CLI), `spark-model-swap.sh`, tests, `ws_test.py` |
| **Deploy** | `deploy/` | Head vs worker subsets + `deploy-worker-from-head.sh` |

---

## Docs

👉 **[Concepts: Demo Modes · Audience · Challenges](docs/CONCEPTS.md)**
👉 **[Scoring: the 6 categories and the math](docs/SCORING.md)**
👉 **[Architecture: two Sparks, one model, the flow](docs/ARCHITECTURE.md)**
👉 **[Deploy: head vs worker subsets, fast-link install](docs/DEPLOY.md)**
👉 **[Cluster ops: install / start / stop / restart / model-swap](docs/CLUSTER_OPS.md)**

---

## Layout

```
ai-dev-arena/
├── orchestrator/        # FastAPI app (main.py) + scoring.py + telemetry.py
│   └── challenges/      # A..D challenge JSON (prompt, golden, validation, weights)
├── challenge-repos/
│   └── sample-app/      # Flask app, tests/, golden/<branch>/ reference solutions
├── frontend/
│   ├── operator.html    # control console (challenge/mode/audience/start)
│   ├── arena.html       # scoreboard + phases + cluster gauges + model
│   └── theater.html     # files changed / diff / terminal / activity
├── cluster/             # run_cluster.sh, start-ray-head/worker.sh, launchers, parser
├── scripts/
│   ├── run_demo.py      # ⭐ terminal CLI for all 4 challenges + status
│   ├── spark-model-swap.sh
│   ├── test_ws_live.py · ws_test.py
├── deploy/              # head/ + worker/ subsets + deploy-worker-from-head.sh
├── requirements.txt
├── setup.sh             # head-node bring-up (venv, deps, vLLM)
└── docs/                # CONCEPTS · SCORING · ARCHITECTURE · DEPLOY
```

---

## Notes for a live audience

- **Replay** is the offline rehearsal: no model, no network — it runs the "golden"
  path and lets you walk the flow if the cluster is down. It *does* still validate
  against the tests, so it scores honestly (it's the reference implementation).
- **Guardrailed** (default) auto-rescues with the golden solution if the model
  doesn't ship a solvable change — the safe choice for a stage demo.
- **Live** is the honest one: no auto-rescue. If the model stumbles you can press
  **⚡ Fallback** yourself, or let it validate and show the real score.
- Pressing **Start** on the Operator instantly refreshes the Arena and Theater
  even if they're on a different device/browser.

## Requirements

Two DGX Sparks (GB10 / Grace Blackwell) on a high-speed link (100GbE), Docker,
Python 3.11+, the `nvcr.io/nvidia/vllm` image. Model weights are downloaded into
each Spark's `~/.cache/huggingface` on first launch (several hundred GB) — this is
**not** in the repo.
