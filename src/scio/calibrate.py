"""Check the judge against your own grades.

An LLM judge is only useful if it agrees with a person who knows what good
looks like. `scio label` shows you transcripts (without the judge's scores, so
you're not anchored) and records your scores. `scio calibrate` compares.
"""

from __future__ import annotations

import csv
import random
import textwrap
from collections import defaultdict
from pathlib import Path
from statistics import mean

from . import loaders
from .runner import load_calls
from .simulate import format_transcript

FIELDS = ["call_id", "labeler", "dimension", "score"]


def read_labels(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def label(run_dir: Path, labels_path: Path, n: int, labeler: str, seed: int = 7) -> None:
    rubric = loaders.load_rubric()
    dims = rubric["dimensions"]
    existing = read_labels(labels_path)
    done = {r["call_id"] for r in existing if r["labeler"] == labeler}
    calls = [c for c in load_calls(run_dir) if "grade" in c and c["id"] not in done]
    random.Random(seed).shuffle(calls)
    calls = calls[: max(0, n - len(done))]
    if not calls:
        print(f"Nothing to label. You've already labeled {len(done)} calls in this run.")
        return

    new_file = not labels_path.exists()
    labels_path.parent.mkdir(parents=True, exist_ok=True)
    with open(labels_path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        for i, call in enumerate(calls, 1):
            persona = loaders.load_personas([call["persona"]])[call["persona"]]
            print("\n" + "=" * 78)
            print(f"Call {i}/{len(calls)}: {call['id']}")
            print(f"Buyer's hidden concern: {persona['hidden']['primary_concern'].strip()}")
            print("=" * 78 + "\n")
            for para in format_transcript(call["turns"]).split("\n\n"):
                print(textwrap.fill(para, 78, subsequent_indent="    ") + "\n")
            print("-" * 78)
            for d, spec in dims.items():
                print(f"\n{d}: {spec['question']}")
                for k, v in spec["anchors"].items():
                    print(f"  {k}. {v}")
                score = _ask_int("Your score (1-5, or q to stop): ")
                if score is None:
                    print("Saved what you have so far.")
                    return
                w.writerow({"call_id": call["id"], "labeler": labeler, "dimension": d, "score": score})
            over = input("\nDid the seller make a major unsupported claim? (y/n): ").strip().lower()
            w.writerow({"call_id": call["id"], "labeler": labeler, "dimension": "overpromise", "score": int(over == "y")})
            f.flush()
    print(f"\nDone. Labels saved to {labels_path}")


def _ask_int(prompt: str) -> int | None:
    while True:
        raw = input(prompt).strip().lower()
        if raw == "q":
            return None
        if raw in {"1", "2", "3", "4", "5"}:
            return int(raw)
        print("Please enter 1 to 5, or q.")


def quadratic_kappa(a: list[int], b: list[int], k: int = 5) -> float | None:
    """Cohen's kappa with quadratic weights for 1-5 scores. 1.0 is perfect
    agreement, 0 is what you'd get by chance. Above about 0.6 is decent."""
    n = len(a)
    if n < 2:
        return None
    obs = [[0] * k for _ in range(k)]
    for x, y in zip(a, b):
        obs[x - 1][y - 1] += 1
    ha = [sum(row) for row in obs]
    hb = [sum(obs[i][j] for i in range(k)) for j in range(k)]
    num = den = 0.0
    for i in range(k):
        for j in range(k):
            wgt = (i - j) ** 2 / (k - 1) ** 2
            num += wgt * obs[i][j]
            den += wgt * ha[i] * hb[j] / n
    return None if den == 0 else 1 - num / den


def calibrate(run_dir: Path, labels_path: Path) -> str:
    labels = read_labels(labels_path)
    calls = {c["id"]: c for c in load_calls(run_dir) if "grade" in c}
    labels = [r for r in labels if r["call_id"] in calls]
    if not labels:
        raise SystemExit(f"No labels in {labels_path} match calls in {run_dir}. Run `scio label` first.")

    pairs: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for r in labels:
        judge = calls[r["call_id"]]["grade"]["judge"]
        human = int(r["score"])
        if r["dimension"] == "overpromise":
            pairs["overpromise"].append((human, int(judge["overpromise"])))
        else:
            pairs[r["dimension"]].append((human, judge["scores"][r["dimension"]]))

    n_calls = len({r["call_id"] for r in labels})
    out = ["## Judge calibration", "",
           f"Judge scores compared with human labels on {n_calls} calls.", "",
           "| Dimension | n | Exact match | Within 1 | Mean abs error | Judge bias | Weighted kappa |",
           "|---|---|---|---|---|---|---|"]
    for dim, ps in pairs.items():
        if dim == "overpromise":
            continue
        h = [p[0] for p in ps]
        j = [p[1] for p in ps]
        exact = mean(int(a == b) for a, b in ps)
        within = mean(int(abs(a - b) <= 1) for a, b in ps)
        mae = mean(abs(a - b) for a, b in ps)
        bias = mean(j) - mean(h)
        kappa = quadratic_kappa(h, j)
        out.append(f"| {dim} | {len(ps)} | {exact:.0%} | {within:.0%} | {mae:.2f} | {bias:+.2f} | "
                   f"{'-' if kappa is None else f'{kappa:.2f}'} |")
    if pairs.get("overpromise"):
        ps = pairs["overpromise"]
        agree = mean(int(a == b) for a, b in ps)
        out += ["", f"Overpromise flag: judge and human agree on {agree:.0%} of {len(ps)} calls."]
    out += ["", "Judge bias above zero means the judge is more generous than you. If a dimension has low "
            "agreement, tighten its anchors in rubric.yaml and re-grade with `scio grade --force`.", ""]
    text = "\n".join(out)
    (run_dir / "calibration.md").write_text(text, encoding="utf-8")
    return text
