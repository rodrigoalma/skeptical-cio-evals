"""Load config, personas, prompts and the rubric from the repo."""

from __future__ import annotations

from pathlib import Path
from string import Template

import yaml

from .llm import normalize_cache_write_multipliers, normalize_pricing

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TRACK = "sales"

# Effort levels from least to most thinking.
EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_config(path: Path | None = None) -> dict:
    cfg = load_yaml(path or ROOT / "config.yaml")
    cfg.setdefault("track", DEFAULT_TRACK)
    cfg["pricing"] = normalize_pricing(cfg.get("pricing"), cfg.get("models", {}), require_cache_read=True)
    cfg["cache_write_multipliers"] = normalize_cache_write_multipliers(
        cfg.get("cache_write_multipliers"), required=True
    )
    return cfg


def track_of(cfg: dict) -> str:
    return cfg.get("track", DEFAULT_TRACK)


def available_tracks() -> list[str]:
    tracks_dir = ROOT / "tracks"
    tracks = sorted(path.name for path in tracks_dir.iterdir()
                    if path.is_dir() and path.name != DEFAULT_TRACK) if tracks_dir.is_dir() else []
    return [DEFAULT_TRACK, *tracks]


def track_dir_path(*, track: str) -> Path:
    if track == DEFAULT_TRACK:
        return Path(".")
    if not track or Path(track).name != track or track in {".", ".."}:
        raise ValueError(f"Unknown track {track!r}. Available: {', '.join(available_tracks())}")
    relative = Path("tracks") / track
    if not (ROOT / relative).is_dir():
        raise ValueError(f"Unknown track {track!r}. Available: {', '.join(available_tracks())}")
    return relative


def track_root(*, track: str) -> Path:
    return ROOT / track_dir_path(track=track)


def track_file_path(kind: str, name: str | None = None, *, track: str) -> Path:
    track_dir = track_dir_path(track=track)
    if name is not None and (Path(name).name != name or name in {".", ".."} or "/" in name or "\\" in name):
        raise ValueError(f"Invalid name for track file {kind!r}: {name!r}")
    paths = {
        "fact_sheet": Path("data/fact_sheet.md"),
        "personas_dir": Path("data/personas"),
        "smoke_cases": Path("data/smoke/known_cases.yaml"),
        "rubric": Path("rubric.yaml"),
        "buyer_prompt": Path("prompts/buyer.md"),
        "judge_prompt": Path("prompts/judge.md"),
        "debrief_prompt": Path("prompts/debrief.md"),
        "prompts_dir": Path("prompts"),
    }
    if kind == "persona":
        if name is None:
            raise ValueError("A persona id is required for a persona path")
        relative = Path("data/personas") / f"{name}.yaml"
    elif kind == "seller_prompt":
        if name is None:
            raise ValueError("A prompt name is required for a seller prompt path")
        relative = Path("prompts") / f"seller_{name}.md"
    elif kind == "prompt":
        if name is None:
            raise ValueError("A prompt name is required for a prompt path")
        relative = Path("prompts") / f"{name}.md"
    else:
        try:
            relative = paths[kind]
        except KeyError:
            raise ValueError(f"Unknown track file kind {kind!r}") from None
    return track_dir / relative


def track_git_paths(*, track: str) -> tuple[str, ...]:
    if track == DEFAULT_TRACK:
        return ("data", "prompts", "rubric.yaml")
    return (track_dir_path(track=track).as_posix(),)


def load_rubric(path: Path | None = None, *, track: str) -> dict:
    return load_yaml(path or ROOT / track_file_path("rubric", track=track))


def load_personas(selection: str | list[str] = "all", *, track: str) -> dict[str, dict]:
    personas = {}
    persona_dir = ROOT / track_file_path("personas_dir", track=track)
    for path in sorted(persona_dir.glob("*.yaml")):
        p = load_yaml(path)
        personas[p["id"]] = p
    if selection in ("all", ["all"]):
        return personas
    wanted = selection.split(",") if isinstance(selection, str) else selection
    missing = [w for w in wanted if w not in personas]
    if missing:
        raise SystemExit(f"Unknown persona(s): {', '.join(missing)}. Available: {', '.join(personas)}")
    return {w: personas[w] for w in wanted}


def fact_sheet(*, track: str) -> str:
    return (ROOT / track_file_path("fact_sheet", track=track)).read_text(encoding="utf-8")


def render(prompt_name: str, *, track: str, **values: str) -> str:
    """Fill a prompt template from prompts/<name>.md using ${placeholders}."""
    path = ROOT / track_file_path("prompt", prompt_name, track=track)
    text = path.read_text(encoding="utf-8")
    return Template(text).substitute(**values)


def seller_prompt_names(*, track: str) -> list[str]:
    prompts = ROOT / track_file_path("prompts_dir", track=track)
    return sorted(p.stem.removeprefix("seller_") for p in prompts.glob("seller_*.md"))


# The seller prompts used to be called v1 and v2. Runs from before the rename
# still say that in their call files, so map the old names when reading them.
LEGACY_PROMPT_NAMES = {"v1": "basic", "v2": "coached"}


def prompt_name(name: str) -> str:
    return LEGACY_PROMPT_NAMES.get(name, name)


def as_yaml(obj) -> str:
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, width=100).strip()


def account_brief(persona: dict) -> str:
    """What the seller is allowed to know before the call."""
    return as_yaml(persona["public"])
