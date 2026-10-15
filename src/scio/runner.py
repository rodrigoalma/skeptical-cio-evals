"""Orchestrate a full run: simulate calls, grade them, save everything to disk.

Each call is saved as its own JSON file under results/<run>/calls/. If a run
is interrupted, running it again skips calls that are already on disk, so you
never pay twice for the same conversation.
"""

from __future__ import annotations

import json
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import anthropic

from . import fingerprint, loaders
from .grade import grade_call
from .llm import ClaudeCodeFatalError, LLM, backend_of
from .simulate import run_call


# Account-level errors: every other call would fail the same way, so stop the run.
FATAL_ERRORS = (anthropic.AuthenticationError, anthropic.PermissionDeniedError, ClaudeCodeFatalError)


def call_id(persona: str, model: str, prompt: str, effort: str, trial: int) -> str:
    return f"{persona}__{model}__{prompt}__{effort}__t{trial}"


def new_run_dir(base: Path, mock: bool) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return base / (f"{stamp}-mock" if mock else stamp)


def _save(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_calls(run_dir: Path) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((run_dir / "calls").glob("*.json"))]


def simulate_all(llm: LLM, cfg: dict, run_dir: Path, log=print) -> None:
    r = cfg["run"]
    models = cfg["models"]
    personas = loaders.load_personas(r["personas"])
    buyer_model = models[cfg["roles"]["buyer"]]

    jobs = []
    for pid, persona in personas.items():
        for m in r["seller_models"]:
            for prompt in r["seller_prompts"]:
                for effort in r["seller_efforts"]:
                    for trial in range(1, r["trials"] + 1):
                        cid = call_id(pid, m, prompt, effort, trial)
                        path = run_dir / "calls" / f"{cid}.json"
                        # A run started before the rename saved this call under v1/v2.
                        old = {v: k for k, v in loaders.LEGACY_PROMPT_NAMES.items()}.get(prompt)
                        old_path = run_dir / "calls" / f"{call_id(pid, m, old, effort, trial)}.json" if old else None
                        if not path.exists() and not (old_path and old_path.exists()):
                            jobs.append((cid, path, persona, m, prompt, effort))

    log(f"Simulating {len(jobs)} calls ({r['workers']} at a time)...")

    def work(job):
        cid, path, persona, m, prompt, effort = job
        call = run_call(
            llm,
            persona,
            prompt,
            seller_model=models[m],
            buyer_model=buyer_model,
            max_seller_turns=r["max_seller_turns"],
            max_tokens=r["max_tokens_per_turn"],
            seller_effort=effort,
            buyer_effort=cfg["effort"]["buyer"],
        )
        call["id"] = cid
        call["seller_model_name"] = m
        _save(path, call)
        return cid

    _parallel(work, jobs, r["workers"], log, run_dir, "simulate")


def grade_all(llm: LLM, cfg: dict, run_dir: Path, force: bool = False, log=print) -> None:
    rubric = loaders.load_rubric()
    personas = loaders.load_personas("all")
    models = cfg["models"]
    buyer_model = models[cfg["roles"]["buyer"]]
    judge_model = models[cfg["roles"]["judge"]]

    jobs = []
    for path in sorted((run_dir / "calls").glob("*.json")):
        call = json.loads(path.read_text(encoding="utf-8"))
        if force or "grade" not in call:
            jobs.append((path, call))

    log(f"Grading {len(jobs)} calls (judge: {judge_model})...")
    grade_fp = fingerprint.grading(judge_model, cfg["effort"]["judge"], buyer_model, fingerprint.grading_files(),
                                   backend_of(cfg))

    def work(job):
        path, call = job
        call["grade"] = grade_call(
            call, personas[call["persona"]], llm, buyer_model, judge_model, rubric,
            judge_effort=cfg["effort"]["judge"],
        )
        call["grade"]["judge_model"] = judge_model
        call["grade"]["judge_effort"] = cfg["effort"]["judge"]
        call["grade"]["fingerprint"] = grade_fp
        _save(path, call)
        return call["id"]

    _parallel(work, jobs, cfg["run"]["workers"], log, run_dir, "grade")


def _job_id(job) -> str:
    first = job[0]
    return first if isinstance(first, str) else Path(first).stem


def _parallel(fn, jobs, workers, log, run_dir: Path, stage: str) -> None:
    """Run jobs in parallel. Failures never become a score: they go to
    errors.jsonl with a failure class, and re-running retries just those."""
    done = failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, j): j for j in jobs}
        for fut in as_completed(futures):
            try:
                cid = fut.result()
                done += 1
                log(f"  [{done + failed}/{len(jobs)}] {cid}")
            except FATAL_ERRORS:
                pool.shutdown(cancel_futures=True)
                raise
            except Exception as e:  # keep going; one bad call shouldn't sink the run
                failed += 1
                kind = getattr(e, "kind", None) or ("timeout" if "Timeout" in type(e).__name__ else "harness_error")
                usage = getattr(e, "usage", None)
                record = {
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "stage": stage,
                    "id": _job_id(futures[fut]),
                    "failure_class": kind,
                    "error": f"{type(e).__name__}: {e}",
                    "usage": usage.to_dict() if usage else None,
                }
                with open(run_dir / "errors.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps(record) + "\n")
                log(f"  [{done + failed}/{len(jobs)}] FAILED ({kind}): {record['id']}")
                log(traceback.format_exc(limit=2))
    if failed:
        log(f"{failed} failed (see errors.jsonl). Run the same command again to retry just those.")


def write_run_meta(cfg: dict, run_dir: Path, mock: bool) -> None:
    fp = fingerprint.record(cfg, loaders.load_personas(cfg["run"]["personas"]), cfg["run"]["seller_prompts"])
    meta_path = run_dir / "run.json"
    if not meta_path.exists():
        _save(meta_path, {"created": datetime.now().isoformat(timespec="seconds"), "mock": mock, "config": cfg,
                          "fingerprint": fp})
        return
    # Resuming. Calls made on different files would be a different test, so
    # refuse rather than mix them into one run.
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    old = meta.get("fingerprint")
    if not old:
        return
    changed = fingerprint.differences(old, fp)
    if changed:
        raise SystemExit(f"Can't resume {run_dir.name}: these changed since it started: {', '.join(changed)}. "
                         "Start a new run instead.")
    meta["fingerprint"] = fingerprint.merge(old, fp)
    _save(meta_path, meta)
