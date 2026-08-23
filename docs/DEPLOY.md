# Deploy — head vs worker subsets, over the fast link

## The key property (and why it's the *right* model for a 3-node or 4-node arena)
**The worker node runs no arena code at all.** Just:
- A Ray worker (Docker, `nvcr.io/nvidia/vllm`)
- One GPU
- The HuggingFace model cache (150–250 GB, not in the repo)

The "subset" the worker needs is tiny: just the Ray-vLLM launch scripts.
The whole AI-Dev-Arena application (orchestrator + challenges + frontend +
UIs + scoring + telemetry) runs on the head only.

That gives you the deployment model you're asking about, verbatim:
> clone the repo on the head, then over the 100GbE link ship just the
> worker subset to node2, node3, …

This file documents exactly that flow, and provides the script.

## Repo layout → which node gets what

```
ai-dev-arena/                            # ← cloned on BOTH nodes (repo only)
├── cluster/
│   ├── run_cluster.sh                   # ← both (identical, used to join Ray)
│   ├── start-ray-head.sh                # ← HEAD   (brings up cluster head)
│   ├── start-ray-worker.sh              # ← WORKER (joins cluster as a worker)
│   ├── launch-gptoss.sh                 # ← HEAD   (launches vLLM TP=2)
│   ├── launch-nemotron-super.sh         # ← HEAD   (launches Nemotron)
│   └── parsers/super_v3_reasoning_parser.py  # mounted on BOTH (small, ~1KB)
│
├── deploy/
│   ├── head.md                          # docs: what the head owns
│   ├── worker.md                        # docs: what the worker owns
│   └── deploy-worker-from-head.sh       # ⭐ the fast-link subset deployer
│
├── orchestrator/ frontend/ challenge-repos/ docs/ scripts/  # ← HEAD only
```

So the repo is cloned on both (it's small and git makes it trivial to keep in
sync), but **only the worker's files are the ones that have to keep their
contents in lock-step across the nodes** — everything else is head-local.

## One-time bring-up (already done on your current 2-Spark rig, documented here for the record)

### Head (Spark #1)
1. `git clone <repo> ai-dev-arena && cd ai-dev-arena`
2. `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`
3. `chmod +x cluster/*.sh scripts/*.sh`
4. Bring up the cluster head (Docker): `bash cluster/start-ray-head.sh`
   — reads `MASTER_ADDR=$VLLM_HOST_IP` from the script and starts
   `nvcr.io/nvidia/vllm` with Ray head + a GPU.
5. (Worker must be up first or next — step below.)
6. Launch the model: `bash cluster/launch-gptoss.sh` (gpt-oss-120b, TP=2,
   served on `:8000` as `gpt-oss-120b`).
7. Start the orchestrator:
   `.venv/bin/uvicorn orchestrator.main:app --host 0.0.0.0 --port 8080`

### Worker (Spark #2)
1. `git clone <repo> ai-dev-arena && cd ai-dev-arena`
2. `chmod +x cluster/*.sh`
3. Bring up the worker inside Docker: `bash cluster/start-ray-worker.sh`
   — `HEAD_NODE_IP` / `VLLM_HOST_IP` are already set in the script.
4. **That's it.** No venv, no uvicorn, nothing else. It just *joins* the Ray
   cluster on the 100GbE link and offers its GPU to the vLLM engine.

## Day-to-day: update the repo on both nodes (trivial)

```bash
# on the head:
cd ai-dev-arena && git pull
# and ship to the worker over the fast link (this is the "script" — a one-liner
# if you like, and it's the whole of deploy-worker-from-head.sh):
bash deploy/deploy-worker-from-head.sh
```

`deploy-worker-from-head.sh` does:
1. Reads `WORKER_TARGET` (default: `nvidia@192.168.1.149`) and `REPO_BRANCH`
   (default: current branch).
2. On the worker, `git fetch && git reset --hard origin/<REPO_BRANCH>` (or
   `git submodule update` if you add any).
3. Restarts the Ray worker inside its Docker container (`docker exec node-… bash
   start-ray-worker.sh`), and re-checks with `docker exec node-… ray status`
   that the worker re-registered with the head.
4. Prints a summary + a hint ("you can now `bash cluster/launch-gptoss.sh` on the
   head to reload the model").

## Scaling to 3 or 4 Sparks — the real advantage of this layout

Because the worker side is just "join the Ray cluster and offer one GPU,"
*adding a node is literally one command more*, and the arena code doesn't change:

```
# add a 3rd Spark as node3:
# on node3 (fresh): git clone the repo → bash cluster/start-ray-worker.sh
# on the head: bash cluster/launch-gptoss.sh --tensor-parallel-size 3
# (that's it — you can swap the TP size at any time, as long as you clean-stop the
#  Ray placement group before, see SPARK-STACK-SWAP.md in docs/ for the dance)

# the orchestra: telemetry.py's SPARK_NODES env var picks up node3 automatically
#   (comma-separated name,host pairs; the gauges render one per node)
```

So the answer to your question in one line:
> "Is it possible to have a subset of the project that just gets deployed to the
> worker nodes? Or is it possible to download the project to the head and install
> a subset on the worker via script?"

**Both are what this repo does.** The repo is on both (tiny, git-synced). The
*execution* is only on the head. The worker gets `cluster/run_cluster.sh` +
`start-ray-worker.sh` + its GPU — no more. `deploy/deploy-worker-from-head.sh`
is the push-over-the-fast-link script. And the `telemetry.py` probe is
already N-node (you've been running it on 2; it'll render 3 or 4 identically).

## Known pitfalls & how we sidestep them in this layout

| Pitfall | Why it happens here | What the layout / repo avoids |
|---|---|---|
| Stale GPU placement groups after model swap | vLLM hard-killed leaves the ranks in the Ray placement group, so the next `vllm serve` can't allocate GPUs and hangs | `scripts/spark-model-swap.sh` does `ray stop` (both head and worker) + `docker start node-…` (re-register) before relaunch |
| Worker drifts from head after a repo update on the head only | `git pull` runs on one node and the worker is behind | `deploy-worker-from-head.sh` pushes and resets the worker |
| Different `HF_HOME` mounts across nodes (rank mismatch) | Different HF cache contents on the two Sparks can produce an OOR in vLLM because of uneven sharding | Both nodes use the *same* `nvcr.io/nvidia/vllm` image and the *same* HF model; the only mount diff is the small parser file (both nodes use `cluster/parsers/super_v3_reasoning_parser.py`) |
| WS / UIs can only reach the head | They're FastAPI routes on the head | Correct — that's where they should be; workers don't serve HTTP |

## If you want it even more "repo as the source of truth"
- Turn `deploy-worker-from-head.sh` into a CI job, or:
- Put a `git rev-list HEAD --format=%H` check into `cluster/start-ray-worker.sh`
  as a guard (it already reuses the same image on both sides, so a git SHA
  pin is a cheap extra guard).
- If you have a 4th Spark, the only head-side change is `--tensor-parallel-size 2
  → 4` in `cluster/launch-*.sh`; the worker side stays identical.
