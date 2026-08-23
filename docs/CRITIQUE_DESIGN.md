# CRITIQUE_DESIGN.md — writer + critic on two DGX Sparks

> Status: **CRITIC VALIDATED ON HARDWARE (2026-08-23).** The 70B critic runs
> tensor-parallel across both Sparks and produces real code reviews. Writer model
> selection is being finalized (plain NVFP4 fails on the pinned vLLM; BF16 pending).

## 0. Hardware validation results (2026-08-23, on the two-Spark rig)

| Item | Result |
|---|---|
| **Critic loads TP=2 across both Sparks** | ✅ `llama33-nemotron-70b-feedback` serving on :8002; Ray worker rank connected from 192.168.100.11 over the 100GbE link |
| **Critic produces usable reviews** | ✅ verdict `ship-with-nits` + 2 real findings (missing input validation, clarity) that the tests don't catch + a concrete better-way. Clean JSON (fenced). |
| **Critic review latency** | ⏱ ~80s for a full review (max_tokens=600). In the 60–120s budget; streamed so it never looks frozen. Tune down via `max_tokens` for a punchier stage pace. |
| **Critic arch/parser** | ✅ plain `LlamaForCausalLM`, BF16, no reasoning parser needed |
| **Writer NVFP4 (plain)** | ❌ on the **critic's** image (`26.05`, vllm 0.20.1.dev): `KeyError: layers.1.mixer.experts.w2_weight_scale`. ✅ **model card requires `vllm/vllm-openai:v0.27.1`** — pulling that as the writer's own image. |
| **Writer DSpark variant** | ✅ **REHABILITATED** — it's the model card's *recommended* speculative-decoding drafter for DGX Spark (`--speculative_config`), makes the writer FASTER. Not a standalone model, but pairs with the NVFP4 writer. |
| **Writer BF16** | ⏳ downloading (~60GB) — the fallback under test; else gpt-oss-120b |
| **Option A (writer+critic share GPUs)** | ❌ **RULED OUT on GB10.** The critic's Ray placement group reserves **2.0/2.0 GPU** (both whole GPUs). Co-loading a 60GB BF16 writer beside the ~70GB critic shard exceeds the **128GB unified memory** and thrashed the head node into a ~20-min unrecoverable state (network-alive, SSH couldn't fork a shell) → required a hard reboot. **The writer MUST be small (NVFP4/quantized ~20GB) so writer(~20GB)+critic(~70GB)≈90GB fits, OR run sequentially.** BF16 writer (60GB) is memory-incompatible with a resident 70B critic. |

### ⚠ Hard lesson (2026-08-23): memory ceiling is the real constraint
GB10 has **128GB unified** CPU+GPU memory per Spark. The 70B critic (TP=2) puts
~70GB on each node. That leaves **~55GB** for a co-resident writer — which rules
out the 60GB BF16 writer entirely. The writer must be **≤~40GB** to co-exist:
- ✅ a working **NVFP4 writer (~22GB)** — needs a vLLM build that loads its MoE export
- ✅ **gpt-oss-120b** is MoE and was already co-running fine before
- ❌ **BF16 30B writer (60GB)** — do NOT co-load with the 70B critic; it thrashes the node
Never launch a second large engine on a node already hosting a 70B TP shard
without checking `free -g` headroom first. When in doubt, **sequence** (Option B).

## 1. What the demo is *for* (the story, in priority order)

1. **Larger models run on clustered Sparks.** The **critic** (Llama-3.3-Nemotron-70B)
   is served **tensor-parallel across BOTH Sparks** — it's the model that *needs*
   the cluster, and the audience watches it review code live. ✅ **PROVEN.**

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
2. Model licenses / gated access
3. Does `Llama-3.3-Nemotron-70B-Feedback` want a reasoning parser like Super did?
   (Check its model card / chat template before wiring vLLM.)
   — **Resolved:** critic arch is plain `LlamaForCausalLM`, BF16, no special parser.
4. **⚠ Writer arch = `nemotron_h` (hybrid Mamba-Transformer), quant `MIXED_PRECISION`
   NVFP4.** Config parses fine, but `nemotron_h` in vLLM can be version-sensitive.
   **Test the actual `vllm serve` of the writer EARLY** (first thing once weights
   land) — if the pinned vLLM image (26.05) doesn't support nemotron_h cleanly,
   options: (a) newer vLLM image, (b) the BF16 writer variant instead of NVFP4,
   (c) fall back to gpt-oss as the writer for the demo.
   — **TESTED 2026-08-23 on the rig:** the pinned vLLM (`0.20.1.dev`, image 26.05)
   LOADS the nemotron_h arch (Mamba splitting-ops present, NVFP4 detected) but the
   plain `…-NVFP4` checkpoint fails at weight-load with
   `KeyError: 'layers.1.mixer.experts.w2_weight_scale'` (nemotron_h.py:730) — the
   model's NVFP4 MoE export is slightly ahead of this loader. **Decision:** pull
   and test the **`…-NVFP4-DSpark`** (Spark-tuned) variant first, then **`…-BF16`**
   (~60GB, standard weight names → most likely compatible) as the safe fallback.
   gpt-oss-120b remains the ultimate writer fallback (proven working).
   — **DSpark is NOT a standalone model.** Its config is `Qwen3DSparkModel`,
   6 layers, with `draft_vocab_size`/`eagle_aux_hidden_state_layer_ids`/`dflash_config`
   → it's a **speculative-decoding DRAFT head** that accelerates a base model, not a
   writer. Ruled out. **Real fallback path: `…-BF16` (~60GB, downloading).** If BF16
   also hits a nemotron_h loader issue, the writer becomes **gpt-oss-120b** (proven)
   and Nemotron is showcased via the critic only — still fully on-message (open
   NVIDIA model reviewing code, across both Sparks).
4. Critic prompt: the exact template that gets *useful* review, not "looks good to me".
5. Demo timing budget: writer (~20–60s) + critic (~60–120s) — is that the right
   pace for a stage, or do we cap the critic's max_tokens for punchiness?

## 7. Non-goals (v1)
- The critic **fixing** the code (that's a multi-turn agentic loop → a v2).
- Changing the scoring math (critique is narrative in v1).
- More than 2 models / more than 2 Sparks (the roster is N-node already; not needed yet).
