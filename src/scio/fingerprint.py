"""Record what a run was tested on, so runs can be pooled safely.

Two calls share a test version when they used the same fact sheet, buyer
setup, rubric and judge. Calls on the same version can be added together on
the dashboard. The seller model, prompt and effort are left out on purpose:
those are what you compare. Personas and seller prompts get a hash each, so
editing one splits that persona or prompt instead of the whole version.

Runs from before fingerprints existed get one from git: the files as they
were in the last commit before the run started.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Callable

import yaml

from . import loaders
from .llm import DEFAULT_BACKEND, backend_of

SIM_FILES = ("fact_sheet", "buyer_prompt")
# The judge grades honesty against the fact sheet, so it counts for grading too.
GRADE_FILES = ("fact_sheet", "rubric", "judge_prompt", "debrief_prompt")

Reader = Callable[[str], str]


def file_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def short_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:8]


def _read_disk(rel: str) -> str:
    return (loaders.ROOT / rel).read_text(encoding="utf-8")


def _hash_files(kinds: tuple[str, ...], *, track: str, read: Reader) -> dict[str, str]:
    return {
        path.as_posix(): file_hash(read(path.as_posix()))
        for kind in kinds
        for path in [loaders.track_file_path(kind, track=track)]
    }


def simulation(cfg: dict, persona_ids, prompt_files: dict[str, str], *, track: str,
               read: Reader | None = None) -> dict:
    """prompt_files maps the prompt's name to its file stem, which differ only
    for runs saved before the basic/coached rename."""
    r = cfg["run"]
    read_file = read or _read_disk
    settings = {
        "buyer_model": cfg["models"][cfg["roles"]["buyer"]],
        "buyer_effort": (cfg.get("effort") or {}).get("buyer"),
        "max_seller_turns": r.get("max_seller_turns"),
        "max_tokens_per_turn": r.get("max_tokens_per_turn"),
    }
    backend = backend_of(cfg)
    if backend != DEFAULT_BACKEND:
        settings["backend"] = backend
    if track != loaders.DEFAULT_TRACK:
        settings["track"] = track
    return {
        "files": _hash_files(SIM_FILES, track=track, read=read_file),
        "settings": settings,
        "personas": {
            p: file_hash(read_file(loaders.track_file_path("persona", p, track=track).as_posix()))
            for p in sorted(persona_ids)
        },
        "seller_prompts": {
            n: file_hash(read_file(loaders.track_file_path("seller_prompt", stem, track=track).as_posix()))
            for n, stem in sorted(prompt_files.items())
        },
    }


def grading_files(*, track: str, read: Reader | None = None) -> dict:
    return _hash_files(GRADE_FILES, track=track, read=read or _read_disk)


def grading(judge_model: str, judge_effort, buyer_model: str, files: dict, backend: str,
            *, track: str) -> dict:
    # The buyer model also writes the debrief, so it is part of grading.
    settings = {"judge_model": judge_model, "judge_effort": judge_effort, "debrief_model": buyer_model}
    if backend != DEFAULT_BACKEND:
        settings["backend"] = backend
    if track != loaders.DEFAULT_TRACK:
        settings["track"] = track
    return {"files": files, "settings": settings}


def jev_grading(rubric: dict, fact_sheet: str, questions: dict, model: str,
                *, track: str) -> dict:
    """Fingerprint the inputs that determine a Jev shadow grade."""
    fingerprint = {
        "rubric": short_hash(rubric),
        "fact_sheet": file_hash(fact_sheet),
        "judge_questions": short_hash(questions),
        "model": model,
    }
    if track != loaders.DEFAULT_TRACK:
        fingerprint["track"] = track
    return fingerprint


def version(sim: dict, grade: dict) -> str:
    return short_hash({"simulation": {"files": sim["files"], "settings": sim["settings"]}, "grading": grade})


def git_state(*, track: str) -> dict | None:
    try:
        commit = _git("rev-parse", "HEAD").strip()
        dirty = _git("status", "--porcelain", "--", *loaders.track_git_paths(track=track)).strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return {"commit": commit, "dirty": bool(dirty)}


def record(cfg: dict, persona_ids, prompt_names, *, track: str) -> dict:
    fp = simulation(cfg, persona_ids, {n: n for n in prompt_names}, track=track)
    return {**fp, "source": "recorded", "git": git_state(track=track)}


def differences(old: dict, new: dict) -> list[str]:
    """What changed between two fingerprints of the same run, ignoring
    personas and prompts that only one of them used."""
    out = [k for k in old["files"] if old["files"][k] != new["files"].get(k)]
    out += [k for k in old["settings"] if old["settings"][k] != new["settings"].get(k)]
    if (backend_of(old["settings"]) != backend_of(new["settings"])
            and "backend" not in out):
        out.append("backend")
    old_track = loaders.track_of(old["settings"])
    new_track = loaders.track_of(new["settings"])
    if old_track != new_track and "track" not in out:
        out.append("track")
    for group, kind in (("personas", "persona"), ("seller_prompts", "seller_prompt")):
        out += [loaders.track_file_path(kind, k, track=new_track).as_posix()
                for k in old[group] if k in new[group] and old[group][k] != new[group][k]]
    return out


def merge(old: dict, new: dict) -> dict:
    """Add personas and prompts a resumed run picked up."""
    return {**old, "personas": {**new["personas"], **old["personas"]},
            "seller_prompts": {**new["seller_prompts"], **old["seller_prompts"]}}


# ---------------------------------------------------------------- old runs

def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=loaders.ROOT, capture_output=True, text=True, check=True).stdout


def from_git(meta: dict, calls: list[dict], *, track: str) -> dict | None:
    """Fingerprint a run from before fingerprints, using the files in the last
    commit before it started. Returns None when git can't tell."""
    created = meta.get("created")
    if not created or not calls:
        return None
    try:
        cfg = meta["config"]
        if track != loaders.DEFAULT_TRACK or loaders.track_of(cfg) != loaders.DEFAULT_TRACK:
            return None
        # A date with no timezone is read in this machine's timezone, which is
        # how run.json saved it.
        commit = _git("log", "-1", "--format=%H", f"--before={created}", "HEAD").strip()
        if not commit:
            return None

        def read(rel: str) -> str:
            return _git("show", f"{commit}:{rel}")

        prompts = {loaders.prompt_name(c["seller_prompt"]): c["seller_prompt"] for c in calls}
        fp = simulation(cfg, {c["persona"] for c in calls}, prompts, track=loaders.DEFAULT_TRACK, read=read)
        files = grading_files(track=loaders.DEFAULT_TRACK, read=read)
        openings = {p: yaml.safe_load(read(loaders.track_file_path("persona", p, track=track).as_posix()))["hidden"]["opening_line"].strip()
                    for p in fp["personas"]}
    except (OSError, subprocess.CalledProcessError, KeyError, TypeError, yaml.YAMLError):
        return None
    # Spot check: every call opens with its persona's scripted line, so a
    # mismatch means the run used files that differ from this commit.
    mismatched = sorted({c["persona"] for c in calls if c["turns"] and c["turns"][0]["text"].strip() != openings[c["persona"]]})
    return {**fp, "grading_files": files, "source": "git", "git": {"commit": commit, "dirty": None},
            "check": {"opening_lines_mismatch": mismatched}}


def for_run(run_dir: Path, meta: dict, calls: list[dict], use_git: bool = True, *, track: str) -> dict | None:
    """The run's fingerprint, filling it in from git (and saving it) for old runs."""
    if meta.get("fingerprint"):
        return meta["fingerprint"]
    if not use_git or meta.get("mock"):
        return None
    fp = from_git(meta, calls, track=track)
    if fp:
        meta["fingerprint"] = fp
        tmp = run_dir / "run.json.tmp"
        tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(run_dir / "run.json")
    return fp


def call_version(fp: dict, call: dict, cfg: dict, *, track: str) -> str:
    g = call.get("grade") or {}
    if g.get("fingerprint"):
        grade = g["fingerprint"]
    else:
        models = cfg["models"]
        grade = grading(g.get("judge_model") or models[cfg["roles"]["judge"]],
                        g.get("judge_effort") or (cfg.get("effort") or {}).get("judge"),
                        models[cfg["roles"]["buyer"]], fp.get("grading_files") or {}, backend_of(cfg),
                        track=track)
    return version(fp, grade)
