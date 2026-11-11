"""Blind error analysis: transcripts with the grades kept out of sight.

`scio blind` writes a folder with the transcripts of graded calls, a sheet for
your own pass/fail notes, and a separate key with the judge's verdicts. You read
and label first, then open the key. Labels made after seeing the judge's verdict
only show agreement, not what went wrong.
"""

from __future__ import annotations

import csv
import random
from pathlib import Path

from . import loaders
from .runner import load_calls
from .simulate import format_transcript

OUT_NAME = "error_analysis"
DEFAULT_SEED = 7
SHEET_FIELDS = [
    "n",
    "call_id",
    "persona",
    "seller_model",
    "seller_prompt",
    "transcript_file",
    "your_pass_fail",
    "first_failure_note",
    "other_notes",
]
KEY_FIELDS = ["n", "call_id", "judge_passed", "weighted_score"]


def eligible_calls(calls: list[dict]) -> list[dict]:
    """Graded calls whose seller turns were not clipped by the token limit."""
    return sorted(
        (c for c in calls if "grade" in c and c.get("status", "ok") == "ok"),
        key=lambda c: c["id"],
    )


def choose(calls: list[dict], n: int | None, seed: int) -> list[dict]:
    """A seeded sample of n calls (or all of them), in a seeded shuffled order.

    Sorting by call id first means the file order on disk doesn't change the result.
    """
    pool = eligible_calls(calls)
    if not pool:
        raise SystemExit("No graded, non-truncated calls in this run. Run `scio grade` first.")
    rng = random.Random(seed)
    if n is None:
        chosen = list(pool)
    else:
        if n < 1 or n > len(pool):
            raise SystemExit(
                f"--n must be between 1 and {len(pool)}, the number of graded, non-truncated calls in this run."
            )
        chosen = rng.sample(pool, n)
    rng.shuffle(chosen)
    return chosen


def transcript_text(call: dict, persona: dict, number: int) -> str:
    public = persona["public"]
    header = [
        f"# Call {number}",
        "",
        f"- Call id: {call['id']}",
        f"- Persona: {call['persona']} ({public['name']}, {public['title']}, {public['company']})",
        f"- Seller model: {call['seller_model_name']}",
        f"- Seller prompt: {loaders.prompt_name(call['seller_prompt'])}",
        "",
        "---",
        "",
    ]
    return "\n".join(header) + format_transcript(call["turns"]) + "\n"


def _write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def _readme(run_name: str, count: int, n: int | None, seed: int) -> str:
    how = (
        "all graded, non-truncated calls"
        if n is None
        else f"a random sample of {count} graded, non-truncated calls, sampled with seed {seed}"
    )
    return f"""# Blind labeling set for {run_name}

This folder holds {how} from run `{run_name}`. Order is shuffled with seed {seed}.

## Label first

1. Read each file in `transcripts/` in the order of the sheet. Each one gives the
   persona, the seller model and prompt, and the full call. None has a grade.
2. In `labeling_sheet.csv`, fill in `your_pass_fail` with `pass` or `fail` for the
   whole call. Judge it as the partner's company or the seller's manager would: did
   this call do its job for them, and did it avoid overpromising?
3. In `first_failure_note`, write the first thing that went wrong. Leave it empty if
   nothing did. Put anything else in `other_notes`.
4. Do not open `selection_key.csv`, `viewer.html`, `report.md`, `dashboard.html`,
   `results.csv` or `calls/` until every call is labeled. They contain the judge's
   grades, and opening them early would anchor your labels.

## After labeling

Group the `first_failure_note` entries into failure categories, and count how many
calls fall in each. Then open `selection_key.csv` and compare your `pass` or `fail`
with `judge_passed`.
"""


def write(run_dir: Path, *, track: str, n: int | None = None, seed: int = DEFAULT_SEED) -> Path:
    out = run_dir / OUT_NAME
    if out.exists():
        raise SystemExit(
            f"{out} already exists. Delete it, or keep the labels you already made, before blinding this run again."
        )
    chosen = choose(load_calls(run_dir), n, seed)
    personas = loaders.load_personas("all", track=track)
    width = max(2, len(str(len(chosen))))

    (out / "transcripts").mkdir(parents=True)
    sheet: list[dict] = []
    key: list[dict] = []
    for number, call in enumerate(chosen, 1):
        name = f"{number:0{width}d}__{call['id']}.md"
        text = transcript_text(call, personas[call["persona"]], number)
        (out / "transcripts" / name).write_text(text, encoding="utf-8")
        sheet.append(
            {
                "n": number,
                "call_id": call["id"],
                "persona": call["persona"],
                "seller_model": call["seller_model_name"],
                "seller_prompt": loaders.prompt_name(call["seller_prompt"]),
                "transcript_file": f"transcripts/{name}",
                "your_pass_fail": "",
                "first_failure_note": "",
                "other_notes": "",
            }
        )
        key.append(
            {
                "n": number,
                "call_id": call["id"],
                "judge_passed": call["grade"]["passed"],
                "weighted_score": call["grade"]["weighted_score"],
            }
        )

    _write_csv(out / "labeling_sheet.csv", SHEET_FIELDS, sheet)
    _write_csv(out / "selection_key.csv", KEY_FIELDS, key)
    (out / "README.md").write_text(_readme(run_dir.name, len(chosen), n, seed), encoding="utf-8")
    return out
