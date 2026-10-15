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

from . import loaders
from .grade import grade_call
from .llm import LLM
from .simulate import run_call


def call_id(persona: str, model: str, prompt: str, trial: int) -> str:
    return f"{persona}__{model}__{prompt}__t{trial}"


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
                for trial in range(1, r["trials"] + 1):
                    cid = call_id(pid, m, prompt, trial)
                    path = run_dir / "calls" / f"{cid}.json"
                    if not path.exists():
                        jobs.append((cid, path, persona, m, prompt))

    log(f"Simulating {len(jobs)} calls ({r['workers']} at a time)...")

    def work(job):
        cid, path, persona, m, prompt = job
        call = run_call(
            llm,
            persona,
            prompt,
            seller_model=models[m],
            buyer_model=buyer_model,
            max_seller_turns=r["max_seller_turns"],
            max_tokens=r["max_tokens_per_turn"],
            seller_temperature=r["seller_temperature"],
            buyer_temperature=r["buyer_temperature"],
        )
        call["id"] = cid
        call["seller_model_name"] = m
        _save(path, call)
        return cid

    _parallel(work, jobs, r["workers"], log)


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

    def work(job):
        path, call = job
        call["grade"] = grade_call(call, personas[call["persona"]], llm, buyer_model, judge_model, rubric)
        call["grade"]["judge_model"] = judge_model
        _save(path, call)
        return call["id"]

    _parallel(work, jobs, cfg["run"]["workers"], log)


def _parallel(fn, jobs, workers, log) -> None:
    done = failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, j): j for j in jobs}
        for fut in as_completed(futures):
            try:
                cid = fut.result()
                done += 1
                log(f"  [{done + failed}/{len(jobs)}] {cid}")
            except Exception as e:  # keep going; one bad call shouldn't sink the run
                failed += 1
                log(f"  [{done + failed}/{len(jobs)}] FAILED: {e}")
                log(traceback.format_exc(limit=2))
    if failed:
        log(f"{failed} failed. Run the same command again to retry just those.")


def write_run_meta(cfg: dict, run_dir: Path, mock: bool) -> None:
    meta = {"created": datetime.now().isoformat(timespec="seconds"), "mock": mock, "config": cfg}
    meta_path = run_dir / "run.json"
    if not meta_path.exists():
        _save(meta_path, meta)
