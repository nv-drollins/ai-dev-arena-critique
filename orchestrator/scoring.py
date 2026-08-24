"""Scoring engine for AI Dev Arena.

Implements the 6-category scoring model from the demo plan:
  - Time to first working result    (20 pts)
  - Test pass rate                  (25 pts)
  - Code quality / review score     (20 pts)
  - Requirement completeness        (20 pts)
  - Efficiency / resource usage     (10 pts)
  - Human override count            (5 pts)

Returns a breakdown + overall score out of 100.
"""
import subprocess
import time
import re


# pytest's terminal summary line, e.g.:  "===== 2 passed, 1 failed in 0.34s ====="
# or "== 1 error in 0.08s ==".  Read counts ONLY from that final summary so we
# never accidentally match numbers printed inside test output / source snippets.
_SUMMARY_RE = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped)\b")


def _pytest_counts(out: str) -> tuple[int, int]:
    """Return (passed, failed) from a pytest run's FINAL summary line.
    failed counts failures + errors; skipped is ignored. (0, 0) if not pytest."""
    summary = None
    for ln in reversed((out or "").splitlines()):
        s = ln.strip().strip("= ")
        if _SUMMARY_RE.search(s) and ("passed" in s or "failed" in s or "error" in s):
            summary = s
            break
    if not summary:
        return 0, 0
    passed = failed = 0
    for n, kind in _SUMMARY_RE.findall(summary):
        if kind == "passed":
            passed += int(n)
        elif kind.startswith("error") or kind == "failed":
            failed += int(n)
    return passed, failed


def score_session(session: dict) -> dict:
    """Score a completed session.

    session contains:
      - elapsed: seconds from start to final result
      - max_duration: seconds allowed (usually 300)
      - test_results: list of {"command": "...", "passed": bool, "output": "..."}
      - check_results: list of {"command": "...", "passed": bool, "output": "..."}
      - changes: list of {"file": "...", "type": "added|modified|removed"}
      - human_overrides: int — number of times operator intervened
      - prompt_requirements: list of requirement strings extracted from challenge prompt
      - fulfilled_requirements: list of booleans — one per requirement
      - diff_size: total characters in generated diff (proxy for efficiency)
    """
    weights = session.get("scoring_weights", {
        "time_to_result": 20,
        "test_pass_rate": 25,
        "code_quality": 20,
        "requirement_completeness": 20,
        "efficiency": 10,
        "human_overrides": 5,
    })

    breakdown = {}

    # 1. Time to first working result (20 pts)
    elapsed = session["elapsed"]
    max_dur = session.get("max_duration", 300)
    if elapsed <= 60:
        time_score = 20
    elif elapsed <= max_dur:
        # Linear interpolation: 60s = 20pts, max_dur = 4pts
        time_score = max(4, 20 - int((elapsed - 60) * 16 / (max_dur - 60)))
    else:
        time_score = 0
    breakdown["time_to_result"] = {"score": time_score, "max": weights["time_to_result"], "detail": f"{elapsed:.0f}s elapsed"}

    # 2. Test pass rate (25 pts) — count INDIVIDUAL tests, not whole commands.
    test_results = session.get("test_results", [])
    check_results = session.get("check_results", [])
    all_checks = test_results + check_results
    ind_pass = ind_total = 0
    for r in all_checks:
        out = r.get("output", "") or ""
        cp, cf = _pytest_counts(out)
        if cp or cf:
            ind_pass += cp
            ind_total += cp + cf
        else:
            # non-pytest check (e.g. a CLI --check): fall back to its exit status
            ind_total += 1
            if r.get("passed", False):
                ind_pass += 1
    if ind_total:
        passed = ind_pass
        rate = ind_pass / ind_total
        test_score = int(rate * weights["test_pass_rate"])
        detail = f"{ind_pass}/{ind_total} passed"
    else:
        passed = 0; test_score = 0; detail = "0/0 passed"
    breakdown["test_pass_rate"] = {"score": test_score, "max": weights["test_pass_rate"], "detail": detail}

    # 3. Code quality / review (20 pts)
    # Proxy: number of changes × structure of changes
    changes = session.get("changes", [])
    if changes:
        # Fewer focused changes = higher quality (up to a point)
        # Penalize massive diffs
        diff_size = session.get("diff_size", 0)
        if diff_size < 5000:
            quality_score = weights["code_quality"]
        elif diff_size < 15000:
            quality_score = int(weights["code_quality"] * 0.7)
        else:
            quality_score = int(weights["code_quality"] * 0.4)
        # Bonus if test files were added/modified
        test_files = [c for c in changes if "test" in c["file"].lower()]
        if test_files:
            quality_score = min(quality_score + 5, weights["code_quality"])
    else:
        quality_score = 0
    breakdown["code_quality"] = {"score": quality_score, "max": weights["code_quality"], "detail": f"{len(changes)} files changed"}

    # 4. Requirement completeness (20 pts)
    requirements = session.get("fulfilled_requirements", [])
    if requirements:
        req_score = int(sum(requirements) / len(requirements) * weights["requirement_completeness"])
    else:
        req_score = 0
    breakdown["requirement_completeness"] = {
        "score": req_score,
        "max": weights["requirement_completeness"],
        "detail": f"{sum(requirements)}/{len(requirements)} requirements met" if requirements else "no requirements tracked",
    }

    # 5. Efficiency / resource usage (10 pts)
    # Lower diff_size relative to what was needed = more efficient.
    # A session that shipped no changes gets nothing here (efficiency has no
    # meaning without a change) — prevents "empty run = max efficiency".
    diff_size = session.get("diff_size", 0)
    if not session.get("changes"):
        eff_score = 0
    elif diff_size < 3000:
        eff_score = 10
    elif diff_size < 8000:
        eff_score = 7
    elif diff_size < 15000:
        eff_score = 4
    else:
        eff_score = 2
    breakdown["efficiency"] = {"score": eff_score, "max": weights.get("efficiency", 10),
                               "detail": f"{diff_size} chars changed" + (" (no changes)" if not session.get("changes") else "")}

    # 6. Human overrides (5 pts)
    overrides = session.get("human_overrides", 0)
    override_score = max(0, 5 - overrides * 2)
    breakdown["human_overrides"] = {"score": override_score, "max": weights["human_overrides"], "detail": f"{overrides} overrides"}

    total = sum(b["score"] for b in breakdown.values())
    max_possible = sum(v for v in weights.values())

    return {
        "overall": total,
        "max_possible": max_possible,
        "percentage": int(total / max_possible * 100),
        "breakdown": breakdown,
        "timestamp": time.time(),
    }
