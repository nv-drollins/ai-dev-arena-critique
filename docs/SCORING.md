# Scoring — how the score is calculated and what it's based on

The score is an **out-of-100** computed by `orchestrator/scoring.py::score_session`
at the end of a run. It is the sum of **six weighted categories**. The weights are
per-challenge (they live in each `challenges/*.json` under `scoring_weights`) but
always total **100**:

| # | Category          | Weight | Based on                                        |
|---|-------------------|:------:|-------------------------------------------------|
| 1 | Time to first working result | 20 | elapsed seconds vs `max_duration` |
| 2 | Test pass rate    | 25     | tests + checks that PASSED ÷ total              |
| 3 | Code quality / review | 20   | size of the diff + whether tests were added     |
| 4 | Requirement completeness | 20 | fraction of (heuristic) requirements met |
| 5 | Efficiency / resource usage | 10 | diff size (smaller = better) |
| 6 | Human overrides   | 5      | how many times the operator rescued / intervened |

`overall = sum(each category score)`, `percentage = overall / max_possible * 100`.

Below is the **exact logic** for each (copied from the code, with the intuition).

---

### 1 · Time to first working result — 20 pts
```
elapsed <= 60s            -> 20
60s < elapsed <= max_dur  -> 20 down to 4, linear
elapsed > max_dur         -> 0
```
Interpolation: `20 - int((elapsed-60) * 16 / (max_dur-60))`. So a fast run
scores full; a run that grinds through the whole allowed window gets 4.
**This is the category most affected by model choice** — gpt-oss is fast here,
Nemotron bleeds points here.

### 2 · Test pass rate — 25 pts
```
all_checks = test_results (pytest) + check_results (other)
passed     = count of checks with passed == True
score      = int( (passed / len(all_checks)) * 25 )
```
This is the category that matters **most**: it's where real correctness lives.
A passing solution typically earns the full 25.

### 3 · Code quality / review — 20 pts
A proxy heuristic (no LLM-judge), based on how focused the change is:
```
no files changed               -> 0
diff_size   < 5,000 chars      -> 20 (focused)
diff_size 5,000–15,000         -> 14
diff_size     > 15,000         -> 8   (massive diff punished)
+ add 5 if a test file was touched, capped at 20
```
Rewriting large swaths of the file loses points; a surgical change gains them.
This is why the Replay/`golden` run of a large-file challenge can sit a few
points below the live one (replay copies a whole `app.py`, bumping `diff_size`).

### 4 · Requirement completeness — 20 pts
`fulfilled_requirements` is a per-requirement boolean list. A simple heuristic
(`detect_fulfilled_requirements`) derives it from whether the checks passed
(all pass ≈ all met). `score = int( sum(fulfilled)/len * 20 )`. If a challenge
tracks N requirements, this scales to them.

### 5 · Efficiency / resource usage — 10 pts
Small, surgical changes win:
```
diff_size < 3,000  -> 10
      3,000–8,000  -> 7
      8,000–15,000 -> 4
      > 15,000     -> 2
```
Overlaps (intentionally) with category 3 from the "resource" angle.

### 6 · Human overrides — 5 pts
```
score = max(0, 5 - overrides*2)
```
Each operator intervention (the ⚡ Fallback / resume) costs 2 points. A fully
autonomous clean run keeps all 5; two rescues cost 5 (→ 0). **Guaranteed to be
5 in `replay`, and 5 in `guardrailed` if the model did it cleanly.**

---

## A real example (Challenge D, gpt-oss-120b, guardrailed, 96/100)
```
overall 96 / 100 = 96%
  time_to_result            16/20   127s elapsed        # fast, but over 60s
  test_pass_rate            25/25   2/2 passed          # full marks — correctness
  code_quality              20/20   4 files changed     # focused
  requirement_completeness  20/20   1/1 requirements met
  efficiency                10/10   2725 chars changed  # surgical change
  human_overrides            5/5    0 overrides         # fully autonomous
```
Reading of that: the **two big buckets (tests + requirements) hit 100%** — the
real correctness. Time was 16/20 because it took ~127s (over the 60s sweet spot),
which is where model speed shows. Everything else was maxed because the change
was small and clean and no human touched it.

## Why the model matters for the score
- `gpt-oss-120b`: fast + compact patches → **high** Time + Efficiency, usually
  full Tests/Requirements. Typical **90–99**.
- `Nemotron-3-Super`: correct (often full Tests/Requirements) but 4–11 min →
  **low** Time, and a long reasoning trace can make the *visible* change look
  large → mid Efficiency. Typical **80–95**.

So two *correct* solutions can score differently purely on *how* they arrived:
fast and focused vs. slow and verbose. That's the honest tradeoff the demo shows.

## Where to read it / tune it
- Category math: `orchestrator/scoring.py`
- Per-challenge weights: `orchestrator/challenges/challenge_*.json` (`scoring_weights`)
- Live breakdown in the UI: the Arena right panel + every `run_demo.py` score table
