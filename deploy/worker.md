# Worker subset (SPARK #2 in a 2-Spark arena)

This node only offers a GPU and joins the Ray cluster.

| Item | Path | Purpose |
|---|---|---|
| `run_cluster.sh` | `cluster/` | Generic Ray+vLLM bring-up helper (identical on head/worker) |
| `start-ray-worker.sh` | `cluster/` | Joins the Ray cluster as a worker (inside Docker) |
| `parsers/super_v3_reasoning_parser.py` | `cluster/parsers/` | Nemotron reasoning parser (read-only mount, ~1 KB) — used by vLLM on both ranks when the Nemotron model is active |

## What the worker does NOT have (by design)

- ❌ The arena repo code (orchestrator, UIs, sessions, scoring) — head only
- ❌ A Python venv — head only
- ❌ Any project git state — head only
- ❌ Any HTTP listener — it joins Ray, that's it

## Bring-up (one-liner)
```bash
bash cluster/start-ray-worker.sh
```

## Restart (e.g. after a repo update)
```bash
bash cluster/start-ray-worker.sh       # idempotent: kills the old Ray worker and re-runs the same
# (or the deployer script from the head: deploy/deploy-worker-from-head.sh)
```

## When the worker's model is updated
`deploy/deploy-worker-from-head.sh` handles this by:
1. Pushing the repo over the 100GbE link
2. `git reset --hard origin/<branch>` (or fetch+pull)
3. `docker exec -it node-<current> bash -c 'exit'` (or `docker stop`) to
   cleanly detach the worker from the Ray placement group
4. Re-running `start-ray-worker.sh`

The head then does `bash cluster/launch-*.sh` to bring the model back up with
the new rank assignment.
