"""Turn graded calls into report.md, results.csv and a transcript viewer."""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean

from . import loaders
from .llm import Usage
from .runner import load_calls

VIEWER_TEMPLATE = Path(__file__).with_name("viewer_template.html")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% confidence interval for a pass rate. Small samples are noisy and
    this makes that visible instead of hiding it."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _cost(usages: list[dict], pricing: dict) -> float:
    return sum(Usage(**u).cost(pricing) for u in usages)


def flatten(call: dict, pricing: dict, dims: list[str]) -> dict:
    g = call["grade"]
    seller_usage = call["usage"]["seller"]
    row = {
        "id": call["id"],
        "persona": call["persona"],
        "difficulty": call.get("difficulty"),
        "seller_model": call["seller_model_name"],
        "seller_prompt": call["seller_prompt"],
        "passed": g["passed"],
        "weighted_score": g["weighted_score"],
        **{f"score_{d}": g["judge"]["scores"][d] for d in dims},
        "concern_surfaced": g["debrief"]["hidden_concern_surfaced"],
        "concern_addressed": g["debrief"]["hidden_concern_addressed"],
        "dealbreaker_hit": g["debrief"]["dealbreaker_hit"],
        "next_meeting": g["debrief"]["would_take_next_meeting"],
        "buyer_trust": g["debrief"]["trust"],
        "overpromise": g["judge"]["overpromise"],
        "unsupported_claims": len(g["judge"]["unsupported_claims"]),
        "ended_by": call["ended_by"],
        **g["metrics"],
        "seller_cost_usd": round(_cost(seller_usage, pricing), 5),
        "seller_sec_per_turn": round(mean(u["seconds"] for u in seller_usage), 2) if seller_usage else 0,
    }
    return row


def _pct(xs) -> str:
    xs = list(xs)
    return f"{100 * sum(xs) / len(xs):.0f}%" if xs else "-"


def _avg(xs, fmt="{:.2f}") -> str:
    xs = list(xs)
    return fmt.format(mean(xs)) if xs else "-"


def build(run_dir: Path, cfg: dict | None = None) -> Path:
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    cfg = cfg or meta["config"]
    pricing = cfg["pricing"]
    rubric = loaders.load_rubric()
    dims = list(rubric["dimensions"])

    calls = [c for c in load_calls(run_dir) if "grade" in c]
    if not calls:
        raise SystemExit("No graded calls found. Run `scio grade` first.")
    rows = [flatten(c, pricing, dims) for c in calls]

    # results.csv: one row per call, for your own analysis in a spreadsheet
    with open(run_dir / "results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["seller_model"], r["seller_prompt"])].append(r)
    configs = sorted(groups)

    lines = [f"# Skeptical CIO eval report: `{run_dir.name}`", ""]
    if meta.get("mock"):
        lines += ["> **Mock run.** These numbers come from the offline mock model and mean nothing. "
                  "They only show that the pipeline works.", ""]
    buyer = cfg["models"][cfg["roles"]["buyer"]]
    judge = cfg["models"][cfg["roles"]["judge"]]
    lines += [
        f"{len(rows)} graded calls across {len({r['persona'] for r in rows})} personas. "
        f"Buyer played by `{buyer}`, judged by `{judge}`.",
        "",
        "## Headline",
        "",
        "A call **passes** when the buyer's hidden concern came out, the seller handled it, the seller made "
        f"no major unsupported claims, and the weighted rubric score is at least "
        f"{rubric['pass_criteria']['min_weighted_score']}.",
        "",
        "| Seller model | Prompt | Calls | Pass rate (95% CI) | Score | Concern surfaced | Concern handled "
        "| Overpromise | Next meeting | Talk ratio | Cost / call | Sec / turn |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for key in configs:
        g = groups[key]
        k = sum(r["passed"] for r in g)
        lo, hi = wilson(k, len(g))
        lines.append(
            f"| {key[0]} | {key[1]} | {len(g)} | {100 * k / len(g):.0f}% ({100 * lo:.0f}-{100 * hi:.0f}) "
            f"| {_avg(r['weighted_score'] for r in g)} | {_pct(r['concern_surfaced'] for r in g)} "
            f"| {_pct(r['concern_addressed'] for r in g)} | {_pct(r['overpromise'] for r in g)} "
            f"| {_pct(r['next_meeting'] for r in g)} | {_avg(r['talk_ratio'] for r in g)} "
            f"| ${_avg((r['seller_cost_usd'] for r in g), '{:.4f}')} | {_avg((r['seller_sec_per_turn'] for r in g), '{:.1f}')} |"
        )

    lines += ["", "## Rubric scores (judge, 1 to 5)", "",
              "| Seller model | Prompt | " + " | ".join(dims) + " |",
              "|---|---|" + "---|" * len(dims)]
    for key in configs:
        g = groups[key]
        lines.append(f"| {key[0]} | {key[1]} | " + " | ".join(_avg(r[f"score_{d}"] for r in g) for d in dims) + " |")

    personas = sorted({r["persona"] for r in rows})
    lines += ["", "## Pass rate by persona", "",
              "| Persona | " + " | ".join(f"{m} / {p}" for m, p in configs) + " |",
              "|---|" + "---|" * len(configs)]
    for pid in personas:
        cells = []
        for key in configs:
            g = [r for r in groups[key] if r["persona"] == pid]
            cells.append(f"{sum(r['passed'] for r in g)}/{len(g)}" if g else "-")
        lines.append(f"| {pid} | " + " | ".join(cells) + " |")

    claims = []
    for c in calls:
        for cl in c["grade"]["judge"]["unsupported_claims"]:
            if cl.get("severity") == "major":
                claims.append((c["id"], cl))
    if claims:
        lines += ["", "## Major unsupported claims", "",
                  "Things sellers said that the fact sheet doesn't back up. These are the moments that "
                  "would get a real SA in trouble.", ""]
        for cid, cl in claims[:12]:
            lines.append(f"- **{cid}**: \"{cl['quote']}\" ({cl['issue']})")

    total = defaultdict(float)
    for c in calls:
        total["seller"] += _cost(c["usage"]["seller"], pricing)
        total["buyer"] += _cost(c["usage"]["buyer"], pricing)
        total["grading"] += _cost(list(c["grade"]["grading_usage"].values()), pricing)
    lines += ["", "## What this run cost", "",
              f"Seller ${total['seller']:.2f}, buyer ${total['buyer']:.2f}, grading ${total['grading']:.2f}. "
              f"Total ${sum(total.values()):.2f}.", ""]

    cal = run_dir / "calibration.md"
    if cal.exists():
        lines += ["", cal.read_text(encoding="utf-8")]
    else:
        lines += ["## Judge calibration", "",
                  "Not done yet. Run `scio label` to hand-grade some calls, then `scio calibrate` "
                  "to see how often the judge agrees with you.", ""]

    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    _write_viewer(run_dir, calls, rows, dims, meta)
    return run_dir / "report.md"


def _write_viewer(run_dir: Path, calls: list[dict], rows: list[dict], dims: list[str], meta: dict) -> None:
    by_id = {r["id"]: r for r in rows}
    payload = {
        "run": run_dir.name,
        "mock": meta.get("mock", False),
        "dims": dims,
        "calls": [
            {
                "id": c["id"],
                "persona": c["persona"],
                "model": c["seller_model_name"],
                "prompt": c["seller_prompt"],
                "turns": c["turns"],
                "ended_by": c["ended_by"],
                "row": by_id[c["id"]],
                "debrief": c["grade"]["debrief"],
                "judge": c["grade"]["judge"],
            }
            for c in calls
        ],
    }
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = VIEWER_TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", data)
    (run_dir / "viewer.html").write_text(html, encoding="utf-8")
