# AI Dev Arena — Demo Script

A walkthrough guide for presenting the **AI Dev Arena** to attendees. This is a live,
all-NVIDIA demo: an autonomous AI coding agent fixes real software bugs on two NVIDIA
DGX Sparks — with a second, larger model acting as the senior code reviewer.

---

## The one-sentence pitch

> "Watch an AI software engineer running **entirely on local NVIDIA hardware** read a
> real codebase, write a fix, test it, and get code-reviewed by a second AI — all with
> no cloud, no internet, on two DGX Sparks sitting right here."

---

## The stack (what's actually running)

| Role | Model | Where it runs |
|------|-------|---------------|
| **Writer** (the coding agent) | Nemotron-3.5-Lightning-30B | Worker Spark, single GPU |
| **Reviewer** (the senior critic) | Llama-3.3-Nemotron-70B-Feedback | **Tensor-parallel across BOTH Sparks** |

- The **writer** is driven by the **Hermes** agent framework — it doesn't just spit out
  a patch, it *acts*: reads files, edits code, runs the tests, reads the failures, and
  iterates, just like a developer.
- The **reviewer** is the on-brand showpiece: a 70B model too big for one Spark, so it
  runs **split across both** — this is the "large model spanning the cluster" story.
- Acceleration: **MTP speculative decoding + prefix caching**. 100% on-box, air-gapped.

**Talking point:** *"Nothing here calls out to the internet. Both models, the whole
pipeline — it's all on these two boxes. This exact setup runs in a locked-down or
air-gapped environment."*

---

## The four interfaces

Open these in browser tabs before the demo (replace `<head-ip>` with the head Spark's IP):

### 1. Operator Console — `http://<head-ip>:8080/operator`
**Your control panel.** This is what the presenter drives. Pick a challenge, pick the
mode (leave it on **🤖 Agentic**), and hit **Start**. It also shows the **Live Stack**
panel (the models + hardware powering the demo) and a **workflow diagram** of the
pipeline. *Drive the demo from here; project the Arena or Theater for the audience.*

### 2. Arena Display — `http://<head-ip>:8080/arena`
**The scoreboard + live status.** Shows the current challenge, the current phase
(Reading → Writing → Testing → Reviewing → Complete), the **cluster gauges** (both
Sparks' GPU load), the two models, and the **live scoreboard** as the run completes.
*This is the best "big picture" screen to project.*

### 3. Theater — `http://<head-ip>:8080/theater`
**The agent's live activity feed + the diff.** Watch the agent narrate what it's doing
in plain steps (📖 reading → ✏️ editing → 🧪 testing → iterate), see the **files it
changed** and the actual **code diff**, the live **test output**, and the **70B
reviewer's written critique**. *This is the "wow, it's actually working" screen — great
for a technical audience.*

### 4. The Live App (the software being fixed)
The agent is fixing a real little **e-commerce web app** (products, cart, checkout).
For some challenges you can open the running app and interact with it to *see the fix
work* — e.g. after the loyalty challenge, type a customer name into the loyalty lookup
and watch it return their tier and discount. *Use this to make the fix tangible.*

---

## How the score is determined

When a run finishes, it's scored out of **100** across six dimensions. This is
**honest** scoring — it reflects what actually happened, not a rigged 100 every time.

| Dimension | Max pts | What it measures |
|-----------|---------|------------------|
| **Test pass rate** | 25 | What fraction of the challenge's tests now pass |
| **Time to result** | 20 | How fast — full marks near ~60s, scaling down for longer runs |
| **Code quality** | 20 | **Driven by the 70B reviewer's verdict** (not a proxy) |
| **Requirement completeness** | 20 | How many of the challenge's stated requirements were met |
| **Efficiency** | 10 | Clean, focused solution — also tied to the reviewer's verdict |
| **Human overrides** | 5 | Full marks when the agent did it fully autonomously |

**The reviewer's verdict maps to the quality/efficiency points like this:**
`ship` → 100% · `ship-with-nits` → 85% · `needs-work` → 50% · `reject` → 20%.

**Key talking point:** *"The 70B reviewer isn't rubber-stamping — it gives a real
verdict, and that verdict drives a third of the score. On the easy fixes it says
'ship-with-nits'; on the genuinely hard one it says 'needs-work' — and the score
reflects that. That authenticity is the point: you're watching real AI results, not a
scripted success."*

---

## The four challenges

Each is a real, self-contained coding task against the sample app. Recommended demo
order: **B → A → C → D** (fastest and most reliable first, hardest last).

### A — Add Promo Code to Checkout  *(feature)*
**What it is:** Add a promo-code field to checkout — validate codes (e.g. `SAVE10`,
`WELCOME20`), apply the discount, update the order total, and add tests.
**What to watch:** The agent adds a new feature from a spec (not just a fix). ~2 min,
typically scores in the 90s.

### B — Fix Broken Filter on Empty Search  *(bug fix)* ⭐ start here
**What it is:** Users report that filtering orders by status breaks when the search box
is empty. The agent finds the root cause and fixes it.
**What to watch:** The classic "find the bug, fix it, tests go green" loop. Fastest and
most reliable — **~1 minute, usually 95-100.** Best opener.

### C — Improve Dashboard Render Performance  *(optimization)*
**What it is:** The dashboard is slow with 10,000 rows. Make it fast **without changing
the UI** — the agent has to find and fix an inefficient (O(n²)) algorithm.
**What to watch:** This is *reasoning about performance*, not just correctness — a
harder class of problem. ~1-2 min. *If it occasionally takes a bit longer, that's the
agent genuinely working through the optimization — a nice "watch it think" moment.*

### D — Implement a Customer Loyalty Program  *(build-from-spec, the hard one)*
**What it is:** Build a whole tiered loyalty feature to a fixed spec: compute a
customer's tier from their **past order history** (BRONZE < $100, SILVER ≥ $100, GOLD ≥
$200), expose it via a new API endpoint, and apply the tier discount at checkout.
**What to watch:** This is the **honest hard case** — building a multi-part feature from
scratch. The agent often gets it *partway*, and the 70B reviewer correctly says
**"needs-work."** That's the demo's most powerful moment: *"even the AI only gets so far
on a genuinely hard task — and the senior reviewer catches it."* Sometimes it nails it,
sometimes not — that variability is real and worth calling out.

**Demo tip for D:** to *show the loyalty feature working* in the Live App, look up a
customer with enough past spend — **Carol** or **Grace** are SILVER (5% off). (Alice is
BRONZE at $79.98, so she correctly shows 0% — a good "the numbers are real" aside.)

---

## Suggested 5-minute flow

1. **Set the scene** (30s): "Two DGX Sparks, no internet, an AI software engineer and
   an AI code reviewer." Show the **Operator's Live Stack** panel.
2. **Run Challenge B** (~90s): project the **Theater**. Narrate as it reads → edits →
   tests → the reviewer critiques. Land on the score.
3. **Run Challenge C or A** (~2 min): show it handling a different *kind* of task
   (performance / new feature).
4. **Run Challenge D** (~2-4 min): the honest hard one. Let the "needs-work" verdict
   land — this is what makes it credible.
5. **Make it tangible** (30s): open the Live App, do the loyalty lookup (Carol/Grace),
   show the fix is real.

---

## Quick troubleshooting (for the presenter)

- **Before you start, run `bash bin/verify-cluster.sh` on the head** — all checks
  should be ✓ (Ray 2 GPUs, writer, critic, grader, orchestrator, **Hermes agent venv**).
  If any ✗, it prints the exact fix.
- **Agent finishes in ~0 seconds / "0 tool actions"** → the Hermes agent venv is broken;
  `verify-cluster.sh`'s "Hermes agent venv" check catches this.
- **A run scores unusually low with 0/N tests** → the challenge grader (system python3)
  is missing pytest/flask; again, `verify-cluster.sh` flags it.
- **Cluster gauges stuck on "scanning…"** → cosmetic; reload the Arena page. The demo
  still works.
- **Scores vary run-to-run** → expected and honest. The agent is doing real work with a
  non-zero temperature; not every run is identical. Lean into it.
