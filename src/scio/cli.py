"""Command line entry point.

    scio smoke               check the grader on known good/bad calls first
    scio run                 simulate, grade and report in one go
    scio run --mock          same, offline, no API key needed
    scio grade RUN_DIR       (re)grade calls in a run
    scio report RUN_DIR      rebuild report.md and viewer.html
    scio label RUN_DIR       hand-grade calls for judge calibration
    scio calibrate RUN_DIR   compare the judge with your labels
    scio personas            list buyer personas
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from . import calibrate as cal
from . import dashboard, loaders, report, runner, smoke
from .llm import make_llm

RESULTS = loaders.ROOT / "results"
EFFORTS = loaders.EFFORTS
LABELS = loaders.ROOT / "labels" / "human_labels.csv"


def _csv(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def _check_key(mock: bool) -> None:
    if not mock and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. Put it in .env or export it, or try `scio run --mock` to test offline.")


def _check_efforts(cfg: dict, roles=("buyer", "judge"), seller: bool = True) -> None:
    named = {f"effort.{r}": [cfg.get("effort", {}).get(r)] for r in roles}
    if seller:
        named["run.seller_efforts"] = cfg["run"].get("seller_efforts") or [None]
    bad = [f"{k}={v}" for k, vs in named.items() for v in vs if v not in EFFORTS]
    if bad:
        sys.exit(f"Missing or unknown effort: {', '.join(bad)}. Use one of: {', '.join(EFFORTS)}")


def _apply_overrides(cfg: dict, args) -> dict:
    r = cfg["run"]
    if args.seller_models:
        r["seller_models"] = _csv(args.seller_models)
    if args.seller_prompts:
        r["seller_prompts"] = _csv(args.seller_prompts)
    r["seller_prompts"] = [loaders.prompt_name(p) for p in r["seller_prompts"]]
    if args.seller_efforts:
        r["seller_efforts"] = _csv(args.seller_efforts)
    if args.personas:
        r["personas"] = args.personas
    if args.trials:
        r["trials"] = args.trials
    if args.workers:
        r["workers"] = args.workers
    if args.judge:
        cfg["roles"]["judge"] = args.judge
    if args.buyer:
        cfg["roles"]["buyer"] = args.buyer
    unknown = [m for m in r["seller_models"] + [cfg["roles"]["judge"], cfg["roles"]["buyer"]] if m not in cfg["models"]]
    if unknown:
        sys.exit(f"Unknown model name(s): {', '.join(unknown)}. Defined in config.yaml: {', '.join(cfg['models'])}")
    bad_prompts = [p for p in r["seller_prompts"] if p not in loaders.seller_prompt_names()]
    if bad_prompts:
        sys.exit(f"No prompts/seller_<name>.md for: {', '.join(bad_prompts)}")
    _check_efforts(cfg)
    return cfg


def _estimate(cfg: dict) -> int:
    r = cfg["run"]
    n_personas = len(loaders.load_personas(r["personas"]))
    return n_personas * len(r["seller_models"]) * len(r["seller_prompts"]) * len(r["seller_efforts"]) * r["trials"]


def cmd_run(args) -> None:
    _check_key(args.mock)
    cfg = _apply_overrides(loaders.load_config(), args)
    run_dir = Path(args.resume) if args.resume else runner.new_run_dir(RESULTS, args.mock)
    run_dir.mkdir(parents=True, exist_ok=True)
    runner.write_run_meta(cfg, run_dir, args.mock)
    print(f"Run folder: {run_dir}")
    print(f"{_estimate(cfg)} calls planned.")
    llm = make_llm(args.mock)
    runner.simulate_all(llm, cfg, run_dir)
    if args.no_grade:
        return
    runner.grade_all(llm, cfg, run_dir)
    path = report.build(run_dir, cfg)
    print(f"\nReport: {path}\nViewer: {run_dir / 'viewer.html'}\nDashboard: {run_dir / 'dashboard.html'}"
          "\nRun `scio dashboard` to update the one across all runs.")


def _meta_cfg(run_dir: Path) -> tuple[dict, bool]:
    import json

    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    return meta["config"], meta.get("mock", False)


def cmd_grade(args) -> None:
    run_dir = Path(args.run_dir)
    cfg, mock = _meta_cfg(run_dir)
    if args.judge:
        cfg["roles"]["judge"] = args.judge
    _check_efforts(cfg, roles=("judge",), seller=False)
    _check_key(mock)
    runner.grade_all(make_llm(mock), cfg, run_dir, force=args.force)
    print(f"Report: {report.build(run_dir, cfg)}")


def cmd_report(args) -> None:
    print(f"Report: {report.build(Path(args.run_dir))}")


def cmd_dashboard(args) -> None:
    path = dashboard.build_combined(Path(args.results), include_mock=args.include_mock, use_git=not args.no_git)
    if not path:
        sys.exit(f"No graded runs found in {args.results}." + ("" if args.include_mock else " Mock runs are skipped unless you pass --include-mock."))
    print(f"Dashboard: {path}")


def cmd_label(args) -> None:
    cal.label(Path(args.run_dir), Path(args.labels), args.n, args.labeler)


def cmd_calibrate(args) -> None:
    run_dir = Path(args.run_dir)
    print(cal.calibrate(run_dir, Path(args.labels)))
    report.build(run_dir)


def cmd_smoke(args) -> None:
    _check_key(args.mock)
    cfg = loaders.load_config()
    if args.judge:
        cfg["roles"]["judge"] = args.judge
    _check_efforts(cfg, roles=("judge",), seller=False)
    ok = smoke.run(make_llm(args.mock), cfg)
    sys.exit(0 if ok else 1)


def cmd_personas(args) -> None:
    for pid, p in loaders.load_personas().items():
        pub = p["public"]
        print(f"{pid:20} {p.get('difficulty', ''):7} {pub['name']}, {pub['title']}, {pub['company']}")


def main(argv: list[str] | None = None) -> None:
    load_dotenv(loaders.ROOT / ".env")  # an exported variable takes precedence
    ap = argparse.ArgumentParser(prog="scio", description="Skeptical CIO: evals for AI solutions architects.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("smoke", help="Check the grader on known good and bad calls.")
    p.add_argument("--judge", help="Model name for the judge")
    p.add_argument("--mock", action="store_true", help="Offline mock model, no API calls")
    p.set_defaults(fn=cmd_smoke)

    p = sub.add_parser("run", help="Simulate, grade and report.")
    p.add_argument("--seller-models", help="Comma list of model names from config.yaml, e.g. haiku,sonnet")
    p.add_argument("--seller-prompts", help="Comma list, e.g. basic,coached")
    p.add_argument("--seller-efforts", help=f"Comma list of seller effort levels: {','.join(EFFORTS)}")
    p.add_argument("--personas", help="Comma list of persona ids, or 'all'")
    p.add_argument("--trials", type=int, help="Calls per combination")
    p.add_argument("--workers", type=int, help="Calls in parallel")
    p.add_argument("--judge", help="Model name for the judge")
    p.add_argument("--buyer", help="Model name for the buyer")
    p.add_argument("--mock", action="store_true", help="Offline mock model, no API calls")
    p.add_argument("--resume", help="Existing run folder to continue")
    p.add_argument("--no-grade", action="store_true", help="Only simulate")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("grade", help="Grade calls in a run folder.")
    p.add_argument("run_dir")
    p.add_argument("--force", action="store_true", help="Re-grade calls that already have grades")
    p.add_argument("--judge", help="Override the judge model")
    p.set_defaults(fn=cmd_grade)

    p = sub.add_parser("report", help="Rebuild report.md and viewer.html.")
    p.add_argument("run_dir")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("dashboard", help="Build one dashboard across every run in results/.")
    p.add_argument("--results", default=str(RESULTS), help="Folder holding the runs")
    p.add_argument("--include-mock", action="store_true", help="Include mock runs")
    p.add_argument("--no-git", action="store_true", help="Don't use git to work out the version of old runs")
    p.set_defaults(fn=cmd_dashboard)

    p = sub.add_parser("label", help="Hand-grade calls for judge calibration.")
    p.add_argument("run_dir")
    p.add_argument("--labels", default=str(LABELS))
    p.add_argument("--n", type=int, default=20, help="Total calls to label in this run")
    p.add_argument("--labeler", default=os.environ.get("USER", "me"))
    p.set_defaults(fn=cmd_label)

    p = sub.add_parser("calibrate", help="Compare judge scores with your labels.")
    p.add_argument("run_dir")
    p.add_argument("--labels", default=str(LABELS))
    p.set_defaults(fn=cmd_calibrate)

    p = sub.add_parser("personas", help="List buyer personas.")
    p.set_defaults(fn=cmd_personas)

    args = ap.parse_args(argv)
    try:
        args.fn(args)
    except runner.FATAL_ERRORS as e:
        detail = e.body.get("error", {}).get("message", e.message) if isinstance(e.body, dict) else e.message
        sys.exit(f"The API rejected the request: {detail}\nCheck ANTHROPIC_API_KEY in .env or your environment.")


if __name__ == "__main__":
    main()
