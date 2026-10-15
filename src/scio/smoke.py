"""Check the grader on hand-written calls with known answers.

Run this before a full paid run, and again after changing the rubric or the
judge prompt. It costs a handful of grader calls. If a known-good call fails
or a known-bad call passes, fix the grader first.
"""

from __future__ import annotations

from . import loaders
from .grade import grade_call
from .llm import LLM

SMOKE_FILE = loaders.DATA / "smoke" / "known_cases.yaml"


def load_cases() -> list[dict]:
    cases = loaders.load_yaml(SMOKE_FILE)
    for c in cases:
        c["turns"] = [
            {"speaker": speaker, "text": " ".join(str(text).split())}
            for t in c["turns"]
            for speaker, text in t.items()
        ]
    return cases


def run(llm: LLM, cfg: dict, log=print) -> bool:
    rubric = loaders.load_rubric()
    personas = loaders.load_personas("all")
    buyer_model = cfg["models"][cfg["roles"]["buyer"]]
    judge_model = cfg["models"][cfg["roles"]["judge"]]
    judge_effort = cfg.get("effort", {}).get("judge")

    all_ok = True
    log(f"Grading {len(load_cases())} known cases with judge {judge_model}...\n")
    for case in load_cases():
        g = grade_call(case, personas[case["persona"]], llm, buyer_model, judge_model, rubric, judge_effort)
        got = {
            "passed": g["passed"],
            "hidden_concern_surfaced": g["debrief"]["hidden_concern_surfaced"],
            "overpromise": g["judge"]["overpromise"],
        }
        misses = {k: (v, got[k]) for k, v in case["expect"].items() if got[k] != v}
        status = "ok  " if not misses else "MISS"
        all_ok &= not misses
        log(f"[{status}] {case['id']:18} score {g['weighted_score']:.2f}  " +
            "  ".join(f"{k}={got[k]}" for k in case["expect"]))
        for k, (want, have) in misses.items():
            log(f"         expected {k}={want}, got {have}")
    log("\nAll known cases graded as expected." if all_ok else
        "\nThe grader disagreed with at least one known case. Read its reasoning before trusting a full run.")
    return all_ok
