# CRITIQUE_DESIGN.md — writer + critic on two DGX Sparks

> Status: **DESIGN / scaffolding**. This repo is seeded from `ai-dev-arena`
> (single-model). This doc captures the plan for the two-model pipeline before
> the build. Nothing here is load-tested on hardware yet — items marked
> **⚠ VALIDATE** must be proven on the real Sparks before the demo.

## 1. What the demo is *for* (the story, in priority order)

1. **Larger models run on clustered Sparks.** The **critic** (Llama-3.3-Nemotron-70B)
   is served **tensor-parallel across BOTH Sparks** — it's the model that *needs*
   the cluster, and the audience watches it review code live.
2. **The Spark is a developer's machine.** We show **code generation + code review**
   — a real dev workflow (write, test, critique, improve), not a chatbot toy.
   "This box helps you with your actual work."
3. **Open models.** Both models are NVIDIA open models (Nemotron family). The
   critique moment is the natural place to talk about open weights, on-prem,
   no data leaving the building.

## 2. The two models

| Role | Model | Size | TP | Where | Why |
|---|---|---|---|---|---|
| **Writer** | `nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4` | 30B MoE (~3B active) | **1** | one Spark | Fast decode (A3B) → punchy live codegen, no 4-min stalls |
| **Critic** | `nvidia/Llama-3.3-Nemotron-70B-Feedback` | 70B dense | **2** | **both Sparks** | Purpose-built by NVIDIA for *feedback*; the "big model needs the cluster" headline |

The split is deliberate: the **fast, small** model does the interactive part; the
**large** model does the part that justifies the hardware.

## 3. Topology — the load-bearing question ⚠ VALIDATE

We want the writer on ONE Spark and the critic spanning BOTH — **concurrently**.
That means node-1's GPU is shared: it holds the critic's rank-0 shard AND (if the
writer lives there) the writer. Options, in order of preference:

- **Option A — writer on node-2 only, critic TP=2 across both (RECOMMENDED)**
  - node-2 GPU: critic rank-1 shard **+** the whole writer (30B NVFP4 ≈ 18GB)
  - node-1 GPU: critic rank-0 shard only
  - Two separate `vllm serve` processes (ports 8001 writer, 8002 critic), each with
    its own `--gpu-memory-utilization` budget so they don't fight for VRAM.
  - **⚠ VALIDATE**: does vLLM+Ray allow a TP=1 engine and a TP=2 engine to coexist
    on overlapping GPUs via fractional placement groups? If Ray reserves whole GPUs
    per placement group, we fall back to Option B.
- **Option B — sequence them** (writer serves, critic loads on demand, or vice-versa).
  - Safe, always works, but adds model-load latency (~30–60s) mid-demo. Mitigate by
    pre-warming the critic during the writer's "thinking" phase, or by keeping both
    resident if memory allows.
- **Option C — critic gets the cluster, writer runs off-cluster** (e.g. the writer on
  a 3rd Spark or a laptop). Cleanest isolation, needs extra hardware.

**Decision: build for A, keep B as the demo-safe fallback.** The `bin/` toolchain
already knows how to bring up TP=2; we add a second, TP=1 launcher and a
memory-fraction knob. First real task of the build is to prove A on the rig.

Memory sanity (128GB/Spark, unified):
- critic 70B dense: BF16 ≈ 140GB total → ~70GB/Spark across TP=2
- writer 30B NVFP4 ≈ 18GB, all on node-2
- node-2 load ≈ 70 + 18 = ~88GB + KV cache → **tight but plausible**; node-1 ≈ 70GB.
- **⚠ VALIDATE** the exact NVFP4 vs BF16 footprints + KV cache headroom on-rig.

## 4. Pipeline (what happens per challenge)

```
Challenge
  │
  ▼
[Writer: Nemotron-Lightning-30B]  → patch  (fast, streamed live)
  │
  ▼
[Tests: pytest]  ← objective ground truth (unchanged from ai-dev-arena)
  │
  ▼
[Critic: Llama-3.3-Nemotron-70B-Feedback]   (streamed live — this is the
  │   inputs: challenge prompt + writer's diff + test output              cluster moment)
  │   NOT given: the golden solution as an answer key
  ▼
[Critique output]
  • verdict: SHIP / NEEDS-WORK
  • 1–3 findings: correctness · style · "did it meet the spec?"
  • one "better way" suggestion (optional)
  │
  ▼
[Score]  = the existing 0–100 (tests etc.)   ← objective, unchanged
[Critique panel]  = narrative layer, shown alongside the score
```

Design rules:
- **The critic cannot edit code.** It only opines. (Keeps `human_overrides`
  scoring clean and the roles honest.)
- **The critic never sees the golden solution** as an answer key — it reviews the
  *diff against the tests*, like a real reviewer would.
- **Tests remain the ground truth.** Critique is additive; it does not move the
  0–100 number in v1. (A later "quality gate" mode could, behind a flag.)
- **Stream the critic's tokens live.** The 70B is slower per token than the writer;
  a visible stream turns "dead 90s freeze" into "watch it think" (the lesson from
  the Nemotron-Super CoT work in the parent repo).

## 5. What changes vs. ai-dev-arena

| Area | Change |
|---|---|
| `bin/arena.conf` | ✅ done — WRITER_*/CRITIC_* model+TP+port split |
| `bin/launch-*` | NEW — one TP=1 writer launcher + one TP=2 critic launcher (+ mem-fraction) |
| `bin/sparkctl.sh` | extend `model`/`status` to manage TWO engines (writer+critic) |
| `orchestrator/main.py` | after tests, add a `run_critic()` call → `session["critique"]` |
| `orchestrator/` config | two OpenAI base-URLs (WRITER_PORT, CRITIC_PORT) instead of one |
| `frontend/arena.html` + `theater.html` | NEW "Critique" panel (verdict + findings), streamed |
| `docs/` | this file + updated CLUSTER_OPS for two engines |

## 6. Open questions to answer during the build
1. **⚠ Option A concurrency** — can writer TP=1 + critic TP=2 coexist on the rig?
   (First thing to test. Determines the whole runtime shape.)
2. Model licenses / gated access — confirm both are pullable from HF on the rig
   (the current cache has Super-120B + gpt-oss; these two are new downloads).
3. Does `Llama-3.3-Nemotron-70B-Feedback` want a reasoning parser like Super did?
   (Check its model card / chat template before wiring vLLM.)
4. Critic prompt: the exact template that gets *useful* review, not "looks good to me".
5. Demo timing budget: writer (~20–60s) + critic (~60–120s) — is that the right
   pace for a stage, or do we cap the critic's max_tokens for punchiness?

## 7. Non-goals (v1)
- The critic **fixing** the code (that's a multi-turn agentic loop → a v2).
- Changing the scoring math (critique is narrative in v1).
- More than 2 models / more than 2 Sparks (the roster is N-node already; not needed yet).
