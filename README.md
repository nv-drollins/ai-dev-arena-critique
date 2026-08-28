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
| **Hardware** | 2× NVIDIA DGX Spark (GB10 / Grace Blackwell), physically stacked and connected over the high-speed link. Follow NVIDIA's guide: **[Connect two Sparks (stacked)](https://build.nvidia.com/spark/connect-two-sparks/stacked-sparks)** |
| **OS / drivers** | Linux with the NVIDIA driver (visible under `/proc/driver/nvidia`) |
| **Container** | Docker + `nvidia-container-toolkit`; the `vllm/vllm-openai:v0.27.1` image (~30 GB, pulled on install) |
| **Runtime** | Python 3.11+, `tmux`, `git`, `curl`, `ssh` between the two boxes (key-based) — these must already be present; the installer checks for them, it does not install them |
| **Cluster** | **[Ray](https://www.ray.io/)** — the distributed framework vLLM uses to run the 70B reviewer tensor-parallel across both Sparks. Set up automatically by `install-head.sh` / `install-worker.sh` (inside the vLLM container), so you don't install it yourself |
| **Agent** | **[Hermes Agent](https://hermes-agent.nousresearch.com)** on the head node — drives the writer as an autonomous agent. **`install-head.sh` installs it and creates the `nemo` profile for you** (needs internet on first install); no manual step required |
| **Weights** | `nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4` (writer) + `llama-3.3-nemotron-70b-feedback` (reviewer), auto-downloaded into each Spark's `~/.cache/huggingface` on first launch (several hundred GB — **not** in the repo) |

Roles used below: **head** = the Spark running the orchestrator + reviewer shard;
**worker** = the Spark serving the writer (`:8001`) + the reviewer's other TP shard.
Node IPs and ports live in [`bin/arena.conf`](bin/arena.conf) — edit it for your LAN.

### One-time Docker prerequisite (run on BOTH Sparks, before the installers)

The DGX Spark base image ships Docker + driver + container toolkit, but a fresh user
still needs to be in the `docker` group and have the NVIDIA runtime registered. Do
this **once per node, then log out and back in** so the group takes effect:

```bash
sudo usermod -aG docker $USER
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
# then: log out and back in (the group change only applies to a new login session)
```

Verify it worked (no sudo, and the nvidia runtime is listed):

```bash
docker info | grep -i runtimes    # should include 'nvidia'
```

The installers **check** for this and stop with the command above if it's missing —
they no longer modify group membership themselves (that's what forced the awkward
mid-install re-login before).

---

## Installation

> **What the installer does:** checks prerequisites, pulls the vLLM image, creates a
> Python venv + installs `requirements.txt`, **installs Hermes Agent + creates the
> agentic `nemo` profile**, and brings up the **Ray** cluster + the orchestrator.
> **What it does *not* do:** install Python/Docker/the NVIDIA driver — those are
> prerequisites you set up first.

One-time bring-up. **Run `install-head.sh` on the HEAD FIRST and let it get Ray up,
THEN run `install-worker.sh` on the worker** — the worker joins the head's Ray, so
the head must be listening first. (The worker installer now waits for the head's Ray
before joining, but doing them in order is the reliable path.)

```bash
git clone https://github.com/nv-drollins/ai-dev-arena-critique.git ai-dev-arena
cd ai-dev-arena
# Optional overrides — CREATE bin/arena.conf.local (it does NOT exist in the repo;
# it's gitignored). Put ONLY the vars you're changing in it, e.g. node IPs and your
# HF token. Example (adjust to your cluster):
#   cat > bin/arena.conf.local <<'EOF'
#   HEAD_NODE_IP="192.168.1.159"
#   WRITER_HOST_SPARK="192.168.1.149"
#   HF_TOKEN="hf_your_token_here"
#   EOF
# It's sourced last: arena.conf.local > env > arena.conf defaults.

# 1) FIRST, on the HEAD Spark — wait for it to finish (Ray head up on :6379):
bash bin/install-head.sh      # prereqs, vLLM image, venv + deps, Hermes + nemo
                              # profile, Ray head, orchestrator on :8080

# 2) THEN, on the WORKER Spark:
bash bin/install-worker.sh    # waits for the head's Ray, then the worker joins it
```

> **Missing small tools?** Pass `--install-deps` to either installer to auto-`apt`
> the userland bits (`tmux`/`git`/`curl`): `bash bin/install-head.sh --install-deps`.
> Docker, the NVIDIA driver, and the container toolkit are **detected, not
> auto-installed** (they touch system daemons / kernel modules) — the script tells
> you exactly what to install if any are missing. The head installer also checks it
> can reach the worker over key-based SSH and prints the `ssh-copy-id` fix if not.
>
> **Docker not usable as your user?** The installers *check* that `docker info` works
> without sudo and that the NVIDIA runtime is registered — they stop with the exact
> fix if not. Do the [one-time Docker prerequisite](#one-time-docker-prerequisite-run-on-both-sparks-before-the-installers)
> (group + runtime, then re-login) before running them.
>
> **Hermes setup wizard (Quick / Full / Blank Slate)?** `install-head.sh` runs the
> Hermes installer, which may prompt you to pick a setup style. **Choose Blank
> Slate**, and when it asks for a provider pick **"Leave Unchanged"**. The arena
> drives Hermes through the `nemo` profile (created for you in the same install
> step, pointed at the local writer), so the base config only needs to *exist* — you
> don't want the wizard pre-wiring a hosted provider or messaging platforms you
> won't use. Quick Setup also works (just ignore its model choice — the `nemo`
> profile overrides it); avoid Full Setup (it configures gateways/tools the demo
> doesn't need). If a step insists on a model and won't skip, pick anything.
>
> The remaining wizard prompts: for **terminal backend** choose **"Keep current
> (local)"** (the agent must run tools on the box), and for the final **"What next?"**
> choose **"Start with everything disabled — finish now (most minimal)"**. The `nemo`
> profile supplies everything the arena uses, so the minimal base is exactly right.

Then bring up the two models and the orchestrator — **launch the writer on the
worker, the critic on the head, then start the orchestrator on the head:**

```bash
# 1) WRITER — on the WORKER (tool-calling + MTP + prefix caching):
bash bin/launch-writer.sh     # serves nemotron-lightning-30b on :8001
                              # first run DOWNLOADS ~21GB of weights — be patient

# 2) CRITIC — on the HEAD (70B, tensor-parallel across BOTH Sparks via Ray):
#    FIRST verify the cluster has 2 GPUs (else the 70B hangs forever waiting for
#    the worker to join — a silent failure mode):
docker exec "$(docker ps --format '{{.Names}}' | grep -E '^node-[0-9]+$' | head -1)" \
  ray status | grep GPU        # must show '.../2.0 GPU'. If it shows /1.0, the worker
                               # didn't join — restart Ray on the worker:
                               #   (on worker) tmux new -d -s ray-worker 'bash ~/start-ray-worker.sh'
#    Then launch the critic (runs FOREGROUND — wrap in tmux; ~141GB first-run download):
tmux new -s critic 'bash bin/launch-critic.sh'   # serves on :8002; detach Ctrl-b then d

# 3) ORCHESTRATOR — on the HEAD (once both models are serving):
bash bin/restart-orch.sh      # starts the arena on :8080, wired to writer + critic

# Verify the whole stack in one shot (run on the HEAD):
bash bin/verify-cluster.sh    # ✓/✗ per component (Ray 2 GPUs, writer, critic,
                              # grader pytest, orchestrator) with the fix for each ✗
```

**Model weights download on first launch, not during install** (they're hundreds of
GB — not in the repo). So `launch-writer.sh` (~21GB) and especially `launch-critic.sh`
(~141GB) will sit "downloading" for a while the first time before they start serving.

> **Faster downloads:** set a HuggingFace token to avoid unauthenticated rate limits
> (matters most for the 141GB critic). Put `HF_TOKEN="hf_..."` in `bin/arena.conf.local`
> **before running the installers** (the token must be in the Ray containers' env when
> they start — an `export` after they're already up won't reach the worker's shard).
> Without it you'll see "You are sending unauthenticated requests to the HF Hub" and
> slower/throttled pulls.

Watch the writer with `docker logs -f arena-writer` (worker); watch the critic in its
tmux session (`tmux attach -t critic` on the head). Each is ready when
`curl -s http://localhost:PORT/v1/models` returns the model (writer :8001, critic :8002).

Watch the critic's **download size** climb toward ~141GB (run on the head):

```bash
du -sh ~/.cache/huggingface/hub/*Llama*70[Bb]*      # writer:  *Nemotron-3.5-Lightning*
watch -n 10 'du -sh ~/.cache/huggingface/hub/*Llama*70[Bb]*'   # live, refreshes every 10s
```

`install-head.sh` already installed Hermes and wired the `nemo` profile to the writer
(host/port come from `arena.conf`); the orchestrator spawns `hermes chat -p nemo` per
agentic run — that profile is what makes the agent talk to your local Nemotron. To
customize the agent profile, override `HERMES_*` in `arena.conf` before installing —
see [Cluster ops](docs/CLUSTER_OPS.md).

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

## Offline / air-gapped deployment (for events with no internet)

Take the whole thing to a venue with no/poor internet: **capture** everything on a
node with internet, copy it to a USB drive, then **restore** it onto a freshly-imaged
node on-site — no downloads required. Do this per node (head and worker).

### A. Capture (do this ahead of time, WITH internet)

On a fully working cluster (both models pulled, everything running), run on **each**
Spark:

```bash
cd ~/ai-dev-arena
bash bin/bundle-offline.sh
```

- Auto-detects the node's role and writes to `offline/arena-offline-<role>-<node>/`
  inside the repo (e.g. `arena-offline-worker-spark-ce66`). Override with
  `ROLE=head|worker bash bin/bundle-offline.sh` if needed.
- Captures: both vLLM Docker images, the HuggingFace model cache (~160GB on the
  node that pulled it), the repo + `.venv`, Hermes (`~/.hermes`), staged `~/` cluster
  scripts, and `pytest`/`flask` wheels for the challenge grader.
- Uses `sudo` for the model cache (vLLM downloads weights as root) and `pigz` for
  fast parallel compression. **It's resumable** — re-running skips any step whose
  output already exists (delete a specific `.tar`/`.tar.gz` in the output folder to
  redo just that one).
- **Then copy the `offline/arena-offline-<role>-<node>/` folder to your USB drive.**
  It's large (150–350GB per node) — use a fast USB3/NVMe drive and expect it to take
  a while.

### B. Restore (at the event, on the freshly-imaged node, NO internet)

Per node, in order:

1. **One-time Docker prerequisite** (same as a normal install), then log out/in:
   ```bash
   sudo usermod -aG docker $USER
   sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
   # log out and back in
   ```
2. **Restore the matching bundle** from the USB (head bundle → head node, worker →
   worker). The restore script only needs the repo, so get it from the bundle's own
   `repo.tar.gz` first, or clone if you have a moment of connectivity:
   ```bash
   # unpack just the repo from the bundle so you have the scripts:
   tar -C ~ -xzf /media/$USER/<vendor>/arena-offline-<role>-<node>/repo.tar.gz
   cd ~/ai-dev-arena
   bash bin/restore-offline.sh /media/$USER/<vendor>/arena-offline-<role>-<node>
   ```
   This loads the Docker images, unpacks the model cache (with `sudo`), the repo +
   `.venv`, Hermes, and the staged scripts, and installs the grader wheels offline.

3. **Bring up the cluster and models** — same order as a normal install, but nothing
   downloads:
   ```bash
   # HEAD first (Ray head), then WORKER joins:
   #   head:   bash ~/start-ray-head.sh     (or via bin/start.sh)
   #   worker: bash ~/start-ray-worker.sh
   # then:
   bash bin/verify-cluster.sh        # confirm 2 GPUs before launching the critic
   bash bin/launch-writer.sh         # on the WORKER
   tmux new -s critic 'bash bin/launch-critic.sh'   # on the HEAD (loads from cache, no download)
   bash bin/restart-orch.sh          # on the HEAD
   bash bin/verify-cluster.sh        # all ✓ = demo ready
   ```

**Tips:**
- The **model weights live on whichever node pulled them** — the worker bundle has
  the writer (~21GB), the head bundle has the critic (~141GB). Restore each on its
  matching node.
- On restore, the models load from `~/.cache/huggingface` — you'll see the launchers
  come up **without** the long download, straight to loading + serving.
- If `verify-cluster.sh` shows anything but all ✓, it prints the exact fix for each ✗.

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
