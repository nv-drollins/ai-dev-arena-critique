# Concepts — Demo Modes, Audience, and the four Challenges

This explains, in the terms the Operator uses them, what the three **Demo Modes**
and the one **Audience** selector actually do, plus what each challenge asks.

> Everything below is backed by code — see `orchestrator/main.py` (mode +
> audience) and `orchestrator/challenges/*.json` (challenges).

---

## Demo Modes

The mode is stored on the session and read by the agent runtime. It is a real
branch, not a label.

### `fully live`  →  **`live`**
The honest model. The model is asked to do the work; if it returns something
that does not actually change the app (e.g. it only added tests, or its patch
didn't match), the run is **not** silently rescued:

- It broadcasts a warning: *"Agent did not ship a solvable change. Use ⚡ Fallback
  to rescue, or let it validate as-is."*
- The Operator's **⚡ Fallback** button applies the golden solution.
- If you let it validate as-is, you get the real (possibly low) score.

Use `live` when you want to *show an AI actually trying* — the struggle is part
of the story. **Trade-off**: it can end with a low score, which is the point of
an honest demo.

### `guardrailed`  →  **`guardrailed`** (default)
Same as `live`, except when the model fails to produce a solvable change, the
**guardrails kick in automatically**: *"guardrails kicking in, applying fallback
solution"* and the golden solution is applied before validation. This is the
safe choice for a stage demo — you always land on a working result.

> Note the difference from `replay`: in `guardrailed` the model is genuinely
> doing the work; the golden solution is a *safety net*. In `replay` the golden
> solution is *the path taken*.

### `replay`  →  **`replay`**
Scripted, **offline, no LLM call**. The agent applies the reference (golden)
solution on a short paced timeline, then still runs the real validation and
scoring. Why this mode exists:

- **Rehearsal**: walk the whole show flow with no model, no network, no cluster
  load — great for setting up on-site or if you can't risk a 120B inference on
  a packed stage.
- **Deterministic**: always the same (correct) behavior and the same score band.
- **Fast**: completes in a few seconds.

It deliberately still scores against the tests (and shows the real `git diff`),
so the scoreboard is truthful even in playback.

**Which one for the show?** If you want a clean, always-good result: `guardrailed`.
If you want to show a real AI working (with the possibility of a rescue, which
is a great "watch a human and an AI team up" moment): `live`. If the cluster is
uncertain or you're just setting up: `replay`.

---

## Audience

A single selector that changes the *tone of the narration* the Arena, Operator,
and Theater show during a run — the underlying phases are identical, only the
words change. It's meant for whoever is watching the big screen.

| Audience | Example "thinking" phase wording |
|---|---|
| **broad** (default) | "Figuring out what to change…", "Thinking it through…", "Making the change…", "Checking it works…" — plain English, no jargon |
| **developers** | "Inspecting repository structure…", "Drafting the implementation plan…", "Generating patches + test updates…", "Applying targeted diffs…", "Running pytest + validation…" — implementation detail |
| **executives** | "Understanding the requirement…", "Planning the solution…", "Working the problem…", "Implementing the fix…", "Verifying the result…" — outcome language |

Pick it for the room in front of you. It does not change what the model does,
how it's scored, or the tests run — only how the same events are phrased.

---

## The four challenges (same sample app)

All four operate on the **same** small Flask checkout app in
`challenge-repos/sample-app/` so the demo stays focused and comparable. Each has
a baseline `app.py`, pre-existing `tests/test_app.py`, a `golden/<branch>/`
reference solution, its own validation command(s), and its own scoring weights.

### A — Feature Sprint: *Add Promo Code to Checkout*
Add a promo-code field: recognized codes `SAVE10` (10% off), `WELCOME20` (20%
off), `VIP50` (50% off); invalid codes error; update the order total; add tests.
- **Model skill emphasized:** feature implementation + edge cases.
- **Length for gpt-oss-120b:** short (~30s).

### B — Bug Bash: *Fix Broken Filter on Empty Search*
The order filter crashes (`TypeError`) when the search query is empty and a
status filter is present. Find the cause and fix it; existing tests must pass.
- **Model skill emphasized:** debugging a subtle bug.
- **Length:** short (~30–70s).

### C — Performance Pit Stop: *Improve Dashboard Render*
The dashboard is O(n²) per row and renders >4s for 10,000 rows. Profile, remove
the quadratic behavior, keep the output structure, get the benchmark under 2s.
- **Model skill emphasized:** optimization + profiling.
- **Length:** short (~30–90s).

### D — Loyalty Rollout: *Implement a Customer Loyalty Program*
Add a tiered program (BRONZE < $100 = 0%, SILVER $100–199 = 5%, GOLD ≥ $200 =
10%) by cumulative non-cancelled spend; a new `GET /api/loyalty/<customer>`
endpoint; apply the discount in `POST /api/cart`; keep backward compatibility.
Graded against a **fixed spec** in `tests/test_loyalty.py`.
- **Model skill emphasized:** multi-file feature from a spec.
- **Length:** the longest of the set (~80–130s on gpt-oss) — the best "showcase"
  because it's clearly a real feature but still completes in a demo-friendly
  window (nowhere near Nemotron's 4–11 min).

> "But not Nemotron long?" — exactly the target. D is designed to be longer than
> B/C (so the audience watches real work) but stays under ~2 minutes. If you want
> it even longer, raise the order dataset size in `sample-app/app.py` (more
> order rows) or require a 4th behavior, e.g. free shipping for GOLD.

---

## A note on fairness

The model is graded on **what the HTTP surface does** (the `tests/*` files), not
on internal function names. Any correct implementation passes the spec. Each
challenge's validation only uses its own command set, so running A does not
require D's tests to exist, etc.
