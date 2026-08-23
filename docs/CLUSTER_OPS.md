# CLUSTER_OPS.md — install, run, and operate the two-Spark AI Dev Arena

This is the operator's manual. It assumes a **head Spark** (control plane +
vLLM rank 0) and one or more **worker Sparks** (vLLM rank 1+), connected by a
100GbE link. Every command below is a script already in this repo — nothing is
hand-typed.

```
   HEAD (Spark #1)                                  WORKER (Spark #2, #3, ...)
   ───────────────                                   ────────────────────────
   Ray head  +  vLLM rank 0        100GbE            Ray worker + 1 GPU ×N
   Arena UI (Operator/Arena/Theater)
   Orchestrator (uvicorn :8080)
   vLLM engine (:8000)  ◄──────── TP=2 ────────────►
```

The whole control surface lives in **`bin/`**:

| File | Role |
|---|---|
| `bin/arena.conf` | single source of truth for cluster config (IPs, image, ports, nodes) |
| `bin/install-head.sh`  | one-time install + bring-up on the HEAD |
| `bin/install-worker.sh`| one-time install + bring-up on a WORKER |
| `bin/sparkctl.sh` | **start / stop / restart / status / model / logs / doctor** (the control plane) |

---

## 1. Prerequisites

### 1.1 Hardware / network
| Req | Why |
|---|---|
| 2× NVIDIA DGX Spark (GB10, 128GB unified) | one model, TP=2 | 
| 100GbE link between them (`enp1s0f1np1` here) | fast NCCL all-reduce for tensor-parallel |
| Passwordless SSH head→worker | `sparkctl.sh` drives the worker over SSH |
| Reachable model weights | see §1.4 |

Verify passwordless SSH from the head first:
```
ssh nvidia@192.168.1.149 'echo ok'     # → ok, no password prompt
```

### 1.2 Node software (both head and worker)
| Package | Min | Install (Ubuntu) |
|---|---|---|
| Ubuntu 24.04 | — | preinstalled |
| Python 3.11+ | 3.11 | `apt install python3.11 python3.11-venv` |
| Docker Engine | 27+ | `apt install docker.io && systemctl enable --now docker` |
| NVIDIA driver + Container Toolkit | 595+ | `sudo nvidia-ctk runtime configure --runtime=docker && systemctl restart docker` |
| tmux | — | `apt install tmux`  *(scripts run Ray/vLLM inside tmux so they survive shell exit)* |
| git, curl | — | `apt install git curl` |
| (head only) gh CLI | 2.45+ | `apt install gh`  *(only if you push this repo via `gh`)* |

Confirm the GPU is visible:
```
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
# NVIDIA GB10, 201GiB    (approx; GB10 reports unified memory)
```

Verify Docker can see the GPU:
```
docker run --rm --gpus all nvcr.io/nvidia/vllm:26.05-py3 nvidia-smi | head -5
```

### 1.3 Access to the vLLM Docker image
The scripts pull `nvcr.io/nvidia/vllm:26.05-py3` (~30GB). That requires access
to the NVIDIA Container Registry. If you're already running it (most DGX
Sparks ship it), you're fine — `install-*.sh` detects a cached copy and skips
the pull. Otherwise:
```
docker login nvcr.io          # NVIDIA NGC credentials, or $NGC_API_KEY
docker pull nvcr.io/nvidia/vllm:26.05-py3
```

### 1.4 Model weights (the big one)
The models are **not** in this repo (they're 50–250GB each). They must exist in
the HuggingFace cache **on both nodes** at `~/.cache/huggingface` (the directory
is bind-mounted into the container). Already present on the current rig:

| Model | HF id | Served name |
|---|---|---|
| gpt-oss-120b  | `openai/gpt-oss-120b` | `gpt-oss-120b` (fast, default) |
| Nemotron-3-Super | `nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4` | `nvidia/nemotron-3-super` (slow, CoT) |

To pull a model fresh on **both** nodes:
```
huggingface-cli download openai/gpt-oss-120b --local-dir ~/.cache/huggingface
# repeat on the worker (must be identical contents)
```
> Both ranks read the same weights, so the cache directory contents should
> match between head and worker. Mismatched caches can cause OOM/faults in vLLM.

### 1.5 The repo
```
git clone https://github.com/nv-drollins/spark-cluster-ai-dev ai-dev-arena
cd ai-dev-arena
```

---

## 2. Installing

Run `install-head.sh` on the HEAD and `install-worker.sh` on each WORKER. They
can run in **parallel** (two terminals). Each is **idempotent** — safe to
re-run.

```
# HEAD  (Spark #1)
bash bin/install-head.sh

# WORKER (Spark #2) — run on that machine, or from the head:
ssh nvidia@192.168.1.149 'bash ~/ai-dev-arena/bin/install-worker.sh'
```

### What `install-head.sh` does (in order)
1. Checks prereqs (python3/docker/tmux/git/curl/NVIDIA driver).
2. Pulls the vLLM image if not cached.
3. Ensures the Nemotron parser file exists (needed only for the Nemotron model).
4. Creates the Python venv + installs `requirements.txt`.
5. Stages the cluster launch scripts into `~/` (`run_cluster.sh`,
   `start-ray-head.sh`, `launch-gptoss.sh`, …) — the working layout those expect.
6. Starts the **Ray head** in a tmux session (`ray-head`), container `node-XXXX`.
7. Waits for the worker to join (Ray shows 2 GPUs).
8. Launches the default model (gpt-oss-120b, TP=2) in a tmux session
   (`vllm-serve`) and waits until `:8000/v1/models` reports it.
9. Starts the **Arena orchestrator** (uvicorn, `:8080`).

### What `install-worker.sh` does
1. Same prereqs/image/parser checks (no venv, no orchestrator — a worker is
   pure compute).
2. Starts the **Ray worker** in a tmux session (`ray-worker`), container
   `node-XXXX`, pointing at the head's address.
3. Waits until the head's Ray registers the worker.

After both finish, `sparkctl.sh status` should show:
```
  ✓ head    ray: node-XXXX   ✓ worker  ray: node-YYYYY
  ✓ model on :8000  gpt-oss-120b
  ✓ orchestrator :8080  {"service":"AI Dev Arena Orchestrator","status":"running"…}
```

---

## 3. Running day-to-day (start / stop / restart)

`bin/sparkctl.sh` is the whole control plane. Run it **from the head**. It
drives every node over SSH and is idempotent — calling a command twice is safe.

```
sparkctl.sh status              # what's up? (model, GPUs, containers, telemetry)
sparkctl.sh start all           # bring up anything that's down (head→worker→model→orch)
sparkctl.sh stop  all           # full teardown (orch → model → workers → head)
sparkctl.sh restart model       # kill vLLM, relaunch the model, restart orchestrator
sparkctl.sh model gptoss        # hot-swap to gpt-oss-120b
sparkctl.sh model nemotron      # hot-swap to Nemotron-3-Super
sparkctl.sh logs vllm           # tail the vLLM log
sparkctl.sh logs worker         # tail the worker's Ray log
sparkctl.sh doctor              # deep diagnostics on every node
```

### Semantics of each verb
| Verb | What happens |
|---|---|
| `start all` | head Ray (if down) → each worker Ray (if down) → model (if cluster has ≥2 GPUs and no model running) → orchestrator. Safe to run repeatedly. |
| `stop all` | stops in reverse: orchestrator → vLLM engine (clean SIGTERM so GPUs are released back to Ray) → workers → head. |
| `restart model` | clean-stops vLLM, reuses the existing Ray cluster if it's still healthy, relaunches the model, restarts the orchestrator. **This is the normal "reboot the demo" command.** |
| `restart cluster` | full Ray teardown + rebuild (head then worker) + model + orchestrator. Use after a power cycle or a GPU deadlock. |
| `model X` | hot-swaps the served model, waiting for `:8000/v1/models` to report the new name before repointing the orchestrator. |

### The tmux model (why this survives shell exit)
`run_cluster.sh` runs Ray with `--block` and has an `EXIT` trap that
`docker stop`s its own container the moment the launching shell dies. So a plain
`bash start-ray-head.sh &` would tear the cluster down when the SSH session
drops. All the scripts therefore start Ray/vLLM **inside tmux sessions**
(`ray-head`, `ray-worker`, `vllm-serve`) that persist independently. To tail
one:
```
sparkctl.sh attach vllm-serve     # → tells you the ssh+tmux attach command
```

### Model hot-swap — the one real gotcha
vLLM holds its GPUs in a **Ray placement group**. If you hard-kill vLLM (or
`docker kill`) without letting it release them, the next launch hangs with
"no GPU available". `sparkctl.sh stop model` / `restart model` / `model X`
therefore do a **clean SIGTERM first** (`pkill -TERM -f "vllm serve"`, wait,
then escalate), and if the GPUs are *still* reserved they fall back to a full
clean cluster restart (Ray `ray stop` on both nodes, relaunch head→worker).
You should never need to `kill -9` a vLLM process by hand — let `sparkctl.sh`
do it.

---

## 4. Scaling to more Sparks
The cluster is already N-node. To add a 3rd Spark as another worker:
1. On the new Spark: run `bin/install-worker.sh` (it auto-joins the head as-is).
2. On the head, bump `VLLM_IMAGE`/launch to `--tensor-parallel-size 3`
   (edit `cluster/launch-gptoss.sh`) and `sparkctl.sh restart model`.
3. The telemetry gauges (`/api/telemetry`) render one card per node
   automatically if the new node is in `SPARK_NODES_JSON`
   (see `bin/arena.conf` / `orchestrator/telemetry.py`).

---

## 5. Troubleshooting
| Symptom | Likely cause / fix |
|---|---|
| `status` shows a node `ray: DOWN` | that node's tmux session died → `sparkctl.sh start <head|worker>` |
| model launches then "no GPU available" | stale placement group → `sparkctl.sh restart cluster` |
| Arena blank, orchestrator down | `sparkctl.sh logs orch`; then `sparkctl.sh restart model` |
| worker joined but head sees 1 GPU | worker's `VLLM_HOST_IP` differs from its 100GbE IP → check `bin/arena.conf` |
| `nvidia-smi` memory shows `[N/A]` | normal on GB10 (unified memory) — telemetry reads DRAM from `/proc/meminfo` instead |
| Nemotron won't serve | parser file missing or head/worker caches differ → `sparkctl.sh doctor` |

Quick health snapshot from the head:
```
sparkctl.sh doctor
```

---

## 6. File map (where things live)
| Path | What |
|---|---|
| `bin/arena.conf` | cluster config (edit IPs/ports/model here) |
| `bin/install-head.sh` | HEAD one-time install + bring-up |
| `bin/install-worker.sh` | WORKER one-time install + bring-up |
| `bin/sparkctl.sh` | start / stop / restart / status / model / logs / doctor |
| `bin/start.sh` / `stop.sh` / `restart.sh` | named convenience wrappers → `sparkctl.sh {start all · stop · restart model}` |
| `cluster/run_cluster.sh` | Ray-in-Docker bring-up (head or worker) |
| `cluster/start-ray-head.sh` / `start-ray-worker.sh` | role-specific Ray launchers |
| `cluster/launch-gptoss.sh` / `launch-nemotron-super.sh` | model launchers |
| `cluster/parsers/super_v3_reasoning_parser.py` | Nemotron parser (mounted on both) |
| `orchestrator/main.py` | FastAPI app (UIs, agent loop, vLLM client) |
| `orchestrator/telemetry.py` | N-node gauge data source |
| `deploy/deploy-worker-from-head.sh` | push the worker subset over the fast link |
