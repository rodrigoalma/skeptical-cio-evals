"""Build dashboard.html: scorecard, persona matrix and head to head.

The combined dashboard reads every run under results/ and pools calls that
share a test version (see fingerprint.py). A single run's folder gets the
same page with just that run in it.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from statistics import mean

from . import fingerprint, loaders
from .llm import cost_of
from .runner import load_calls

TEMPLATE = Path(__file__).with_name("dashboard_template.html")


def _row(call: dict, run: str, ver: str, pricing: dict) -> dict:
    g = call["grade"]
    seller = call["usage"]["seller"]
    return {
        "id": call["id"],
        "run": run,
        "version": ver,
        "persona": call["persona"],
        "model": call["seller_model_name"],
        "prompt": loaders.prompt_name(call["seller_prompt"]),
        "effort": call.get("seller_effort") or "unrecorded",
        "ok": call.get("status", "ok") == "ok",
        "pass": g["passed"],
        "score": g["weighted_score"],
        "scores": g["judge"]["scores"],
        "surfaced": g["debrief"]["hidden_concern_surfaced"],
        "overpromise": g["judge"]["overpromise"],
        "next_meeting": g["debrief"]["would_take_next_meeting"],
        "cost": round(cost_of(seller, pricing), 6),
        "sec": round(mean(u["seconds"] for u in seller), 2) if seller else 0,
        "claims": [{"quote": c["quote"], "issue": c["issue"]}
                   for c in g["judge"]["unsupported_claims"] if c.get("severity") == "major"],
    }


def _label_variants(rows: list[dict], field: str, hashes: dict[tuple, str]) -> None:
    """Within a version, a persona or prompt edited between runs is really two
    things. Give each edit its own label, like coached·b71c."""
    seen = defaultdict(set)
    for r in rows:
        seen[(r["version"], r[field])].add(hashes.get((r["run"], r[field])))
    for r in rows:
        if len(seen[(r["version"], r[field])]) > 1:
            h = hashes.get((r["run"], r[field])) or "????"
            r[field] = f"{r[field]}·{h[:4]}"


def collect(run_dirs: list[Path], pricing: dict, use_git: bool = True) -> dict:
    rows, runs, versions = [], [], {}
    persona_h, prompt_h = {}, {}
    dimensions = []
    for run_dir in run_dirs:
        meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        calls = [c for c in load_calls(run_dir) if "grade" in c]
        if not calls:
            continue
        cfg = meta["config"]
        track = loaders.track_of(cfg)
        for dimension in loaders.load_rubric(track=track)["dimensions"]:
            if dimension not in dimensions:
                dimensions.append(dimension)
        fp = fingerprint.for_run(run_dir, meta, calls, use_git, track=track)
        run = run_dir.name
        models = cfg["models"]
        effort = cfg.get("effort") or {}
        runs.append({"name": run, "created": meta.get("created"), "mock": meta.get("mock", False),
                     "source": fp["source"] if fp else "unversioned",
                     "git": (fp or {}).get("git"),
                     "mismatch": ((fp or {}).get("check") or {}).get("opening_lines_mismatch", [])})
        if fp:
            for p, h in fp["personas"].items():
                persona_h[(run, p)] = h
            for p, h in fp["seller_prompts"].items():
                prompt_h[(run, p)] = h
        for c in calls:
            if fp:
                ver = fingerprint.call_version(fp, c, cfg, track=track)
                g = c["grade"]
                files = {**fp["files"], **((g.get("fingerprint") or {}).get("files") or fp.get("grading_files") or {})}
                settings = {**fp["settings"],
                            "judge_model": g.get("judge_model") or models[cfg["roles"]["judge"]],
                            "judge_effort": g.get("judge_effort") or effort.get("judge")}
            else:
                # Without a fingerprint nothing says what this run was tested
                # on, so it stays on its own.
                ver = f"run:{run}"
                files = {}
                settings = {"buyer_model": models[cfg["roles"]["buyer"]], "buyer_effort": effort.get("buyer"),
                            "judge_model": models[cfg["roles"]["judge"]], "judge_effort": effort.get("judge")}
            v = versions.setdefault(ver, {"id": ver, "files": files, "settings": settings, "runs": [], "sources": []})
            if run not in v["runs"]:
                v["runs"].append(run)
                v["sources"].append(runs[-1]["source"])
            rows.append(_row(c, run, ver, pricing))

    _label_variants(rows, "persona", persona_h)
    _label_variants(rows, "prompt", prompt_h)

    created = {r["name"]: r["created"] or "" for r in runs}
    ordered = sorted(versions.values(), key=lambda v: max(created[r] for r in v["runs"]))
    for i, v in enumerate(ordered):
        v["first"] = min(created[r] for r in v["runs"])
        v["last"] = max(created[r] for r in v["runs"])
        prev = next((p for p in reversed(ordered[:i]) if p["files"]), None) if v["files"] else None
        v["changed"] = sorted(
            [k for k in v["files"] if prev and prev["files"].get(k) != v["files"][k]]
            + [k for k in v["settings"] if prev and prev["settings"].get(k) != v["settings"][k]]
        ) if prev else None

    if not dimensions:
        dimensions = list(loaders.load_rubric(track=loaders.track_of(loaders.load_config()))["dimensions"])
    return {
        "runs": runs,
        "versions": ordered,
        "rows": rows,
        "dims": dimensions,
        "models": list(loaders.load_config()["models"]),
        "efforts": loaders.EFFORTS,
    }


def write(path: Path, run_dirs: list[Path], pricing: dict, title: str, use_git: bool = True) -> Path | None:
    data = collect(run_dirs, pricing, use_git)
    if not data["rows"]:
        return None
    data["title"] = title
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", payload)
    path.write_text(html, encoding="utf-8")
    return path


def build_combined(results: Path, include_mock: bool = False, use_git: bool = True) -> Path | None:
    run_dirs = sorted(p.parent for p in results.glob("*/run.json"))
    if not include_mock:
        run_dirs = [d for d in run_dirs if not json.loads((d / "run.json").read_text(encoding="utf-8")).get("mock")]
    # One price list for every run, so costs compare across runs.
    pricing = loaders.load_config()["pricing"]
    return write(results / "dashboard.html", run_dirs, pricing, "All runs", use_git)
