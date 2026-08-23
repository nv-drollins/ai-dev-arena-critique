# CRITIQUE_RUNBOOK.md — running the writer+critic demo live

The operator's script for the two-model demo. Assumes the cluster is installed
(see CLUSTER_OPS.md) and both models are in the HF cache on both Sparks.

## The story you're telling (say these out loud)

1. **"One large model, running across two Sparks."** The reviewer is a **70B**
   model — too big for one box — served **tensor-parallel across both Sparks**.
   When it reviews the code, point at the CLUSTER panel: both GPUs light up.
2. **"The Spark is a developer's machine."** We're not doing a chatbot — we're
   doing a *real developer loop*: write code → run tests → get a senior-level
   review. This is the box on your desk helping you with your actual work.
3. **"These are open models."** Both are NVIDIA open-weight Nemotron models,
   running **entirely on-prem** — no code leaves the room. Good moment to talk
   about open models vs. closed APIs for enterprises with IP/compliance concerns.

## The two models

| Role | Model | Runs on | Why the audience cares |
|---|---|---|---|
| Writer | Nemotron-3.5-Lightning-30B-A3B | ONE Spark (fast) | Instant codegen — no waiting |
| Critic | Llama-3.3-Nemotron-70B-Feedback | BOTH Sparks (TP=2) | The "big model needs the cluster" moment |

## Pre-flight (do this before the audience arrives)

```bash
# 1. cluster up (Ray on both Sparks)
bash bin/sparkctl.sh start        # or: bin/start.sh
bash bin/sparkctl.sh status       # confirm 2 nodes, 2 GPUs

# 2. bring up BOTH engines (writer ~1min, critic ~2-5min to load 70B across TP=2)
bash bin/critiquectl.sh start     # writer + critic + orchestrator(critic-enabled)
bash bin/critiquectl.sh status    # confirm writer :8001 + critic :8002 both serving

# 3. open the displays
#    Operator: http://<head>:8080/operator
#    Arena:    http://<head>:8080/arena     (the scoreboard + REVIEW panel)
#    Theater:  http://<head>:8080/theater   (files/diff/terminal + review feed)

# 4. do ONE warm-up run end-to-end so the first live one is fast (KV cache warm)
```

## Running a challenge live

1. On the **Operator**, pick a challenge (D — Loyalty Rollout is the meatiest).
2. Mode: **Guardrailed** (safe default). Audience: match your crowd.
3. Press **Start**. Watch:
   - Writer generates the patch (fast, streamed) → **"that's the 30B, on one Spark"**
   - Tests run (pytest) → objective pass/fail
   - **Reviewer kicks in** → CLUSTER panel: both GPUs spike → **"now the 70B, across both Sparks"**
   - Review panel fills: verdict (SHIP / NEEDS-WORK) + findings + a "better way"
4. Talk to the **review content** — it's the differentiator. "The tests say it
   works; the reviewer tells you if it's *good*."

## Timing budget (set expectations)
- Writer: ~20–60s (Lightning is fast — that's the point).
- Critic: ~60–120s (70B, streamed so it never looks frozen).
- Total per run: ~2–3 min. Fine for a stage; do the warm-up run first.

## If something goes wrong (fallbacks)

| Symptom | Fix |
|---|---|
| Critic won't come up / OOM | See "Two engines, shared GPUs" below — likely fell into the Option-B case |
| Critic slow to first token | Normal for 70B; the live stream shows it's alive. Talk over it. |
| Whole thing wedged | `bin/critiquectl.sh restart both` (clean stop + relaunch) |
| Demo cluster down entirely | `bin/sparkctl.sh restart cluster` then `critiquectl.sh start` |
| Need to fall back to single-model | The base `bin/sparkctl.sh model gptoss` still works (writer-only, no review) |

## Two engines, shared GPUs — the concurrency reality ⚠

The writer (TP=1, one Spark) and critic (TP=2, both Sparks) SHARE the GPU on the
writer's node. Whether they run **concurrently** depends on vLLM/Ray placement:

- **If concurrency works (Option A):** both stay resident; switching from writer
  to critic is instant. This is the smooth demo. `critiquectl.sh start` brings up
  both. GPU memory fractions are tuned in `bin/launch-writer.sh`
  (`WRITER_GPU_FRAC`) and `bin/launch-critic.sh` (`CRITIC_GPU_FRAC`).
- **If it doesn't (Option B fallback):** run them sequentially — writer serves,
  and the critic loads on demand after the tests. Adds ~30–60s mid-run. Mitigate
  by pre-warming: start the critic during the writer's "thinking" phase.

**This is validated on the rig before the demo** — see the concurrency test in
docs/CRITIQUE_DESIGN.md §3. The runbook assumes Option A; if the rig forces B,
the pre-warm timing is the only change.

## After the demo
```bash
bash bin/critiquectl.sh stop both     # clean SIGTERM, releases GPUs
# (leave the Ray cluster up for the next session, or bin/sparkctl.sh stop all)
```
