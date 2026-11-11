"""Load config, personas, prompts and the rubric from the repo."""

from __future__ import annotations

import re
from pathlib import Path
from string import Template
from typing import Any, NoReturn

import yaml

from .llm import normalize_cache_write_multipliers, normalize_pricing

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TRACK = "sales"

# Effort levels from least to most thinking.
EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def load_yaml(path: Path) -> Any:  # YAML can be a mapping (config, rubric) or a list (smoke cases)
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


def current_costs() -> tuple[dict, dict]:
    """Use the current price list for comparable costs across saved runs."""
    cfg = load_config()
    return cfg["pricing"], cfg["cache_write_multipliers"]


def track_of(cfg: dict) -> str:
    return cfg.get("track", DEFAULT_TRACK)


def available_tracks() -> list[str]:
    tracks_dir = ROOT / "tracks"
    tracks = (
        sorted(path.name for path in tracks_dir.iterdir() if path.is_dir() and path.name != DEFAULT_TRACK)
        if tracks_dir.is_dir()
        else []
    )
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


# A partner file can hold several scenarios. Each one becomes its own case, with the
# partner's fields merged in. Case ids are "<partner id>__<scenario id>".
SCENARIO_SEP = "__"
DIFFICULTIES = ("easy", "medium", "hard")
PARTNER_PUBLIC_KEYS = ("name", "title", "company", "company_profile")
SCENARIO_KEYS = ("id", "difficulty", "public", "seller_brief", "hidden")
SCENARIO_HIDDEN_TEXT = (
    "disposition",
    "opening_line",
    "primary_concern",
    "competitor_pull",
    "reveals_primary_concern_when",
)
SCENARIO_HIDDEN_LISTS = ("wont_share", "objections", "what_wins_them", "dealbreakers")
SCENARIO_HIDDEN_KEYS = SCENARIO_HIDDEN_TEXT + SCENARIO_HIDDEN_LISTS
_ID = re.compile(r"[a-z][a-z0-9_]*")


def _fail(source: str, message: str) -> NoReturn:
    raise SystemExit(f"{source}: {message}")


def _check_text(source: str, where: str, value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        _fail(source, f"{where} must be non-empty text.")


def _check_items(source: str, where: str, value: Any) -> None:
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v.strip() for v in value):
        _fail(source, f"{where} must be a non-empty list of text items.")


def _check_id(source: str, where: str, value: Any) -> None:
    if not isinstance(value, str) or not _ID.fullmatch(value) or SCENARIO_SEP in value:
        _fail(source, f"{where} must use lowercase letters, digits and single underscores, not {value!r}.")


def expand_persona(p: dict[str, Any], *, source: str) -> list[dict[str, Any]]:
    """The persona cases a file defines: one per scenario, or the file itself if it has none.

    A file without scenarios is returned unchanged, so tracks without them load as before.
    """
    if "scenarios" not in p:
        return [p]
    pid = p["id"]
    _check_id(source, "id", pid)
    _check_text(source, "language", p.get("language"))
    if "difficulty" in p:
        _fail(source, "difficulty belongs on each scenario, not on the partner.")
    public = p.get("public")
    if not isinstance(public, dict):
        _fail(source, "public must be a mapping.")
    for key in PARTNER_PUBLIC_KEYS:
        _check_text(source, f"public.{key}", public.get(key))
    hidden = p.get("hidden")
    if not isinstance(hidden, dict) or not hidden:
        _fail(source, "hidden must be a non-empty mapping.")
    shared = sorted(set(hidden) & set(SCENARIO_HIDDEN_KEYS))
    if shared:
        _fail(source, f"hidden holds scenario field(s) {', '.join(shared)}. Move them into the scenarios.")
    scenarios = p["scenarios"]
    if not isinstance(scenarios, list) or not scenarios:
        _fail(source, "scenarios must be a non-empty list.")

    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for number, s in enumerate(scenarios, 1):
        if not isinstance(s, dict):
            _fail(source, f"scenario {number} must be a mapping.")
        sid = s.get("id")
        _check_id(source, f"scenario {number} id", sid)
        if sid in seen:
            _fail(source, f"scenario id {sid!r} appears more than once.")
        seen.add(sid)
        where = f"scenario {sid!r}"
        unknown = sorted(set(s) - set(SCENARIO_KEYS))
        if unknown:
            _fail(source, f"{where} has unknown field(s): {', '.join(unknown)}.")
        if s.get("difficulty") not in DIFFICULTIES:
            _fail(source, f"{where} difficulty must be one of: {', '.join(DIFFICULTIES)}.")
        s_public = s.get("public")
        if not isinstance(s_public, dict) or set(s_public) != {"call_context"}:
            _fail(source, f"{where} public must hold only call_context.")
        _check_text(source, f"{where} public.call_context", s_public["call_context"])
        brief = s.get("seller_brief")
        if not isinstance(brief, dict) or set(brief) != {"gitlab_goal"}:
            _fail(source, f"{where} seller_brief must hold only gitlab_goal.")
        _check_text(source, f"{where} seller_brief.gitlab_goal", brief["gitlab_goal"])
        s_hidden = s.get("hidden")
        if not isinstance(s_hidden, dict):
            _fail(source, f"{where} hidden must be a mapping.")
        missing = [key for key in SCENARIO_HIDDEN_KEYS if key not in s_hidden]
        if missing:
            _fail(source, f"{where} is missing hidden field(s): {', '.join(missing)}.")
        unknown_hidden = sorted(set(s_hidden) - set(SCENARIO_HIDDEN_KEYS))
        if unknown_hidden:
            _fail(source, f"{where} has unknown hidden field(s): {', '.join(unknown_hidden)}.")
        for key in SCENARIO_HIDDEN_TEXT:
            _check_text(source, f"{where} hidden.{key}", s_hidden[key])
        for key in SCENARIO_HIDDEN_LISTS:
            _check_items(source, f"{where} hidden.{key}", s_hidden[key])
        cases.append(
            {
                "id": f"{pid}{SCENARIO_SEP}{sid}",
                "partner_id": pid,
                "scenario_id": sid,
                "difficulty": s["difficulty"],
                "language": p["language"],
                "public": {**public, **s_public},
                "seller_brief": {"gitlab_goal": brief["gitlab_goal"]},
                "hidden": {**hidden, **s_hidden},
            }
        )
    return cases


def load_personas(selection: str | list[str] = "all", *, track: str) -> dict[str, dict]:
    """Persona cases in the track by case id. A selection item is a case id, or a partner id
    for all of that partner's scenarios."""
    cases: dict[str, dict] = {}
    persona_dir = ROOT / track_file_path("personas_dir", track=track)
    for path in sorted(persona_dir.glob("*.yaml")):
        p = load_yaml(path)
        if not isinstance(p, dict) or "id" not in p:
            raise SystemExit(f"{path}: a persona file must be a mapping with an id.")
        for case in expand_persona(p, source=str(path)):
            if case["id"] in cases:
                raise SystemExit(f"{path}: persona id {case['id']!r} is already defined.")
            cases[case["id"]] = case
    if selection in ("all", ["all"]):
        return cases
    by_partner: dict[str, list[str]] = {}
    for case_id, case in cases.items():
        by_partner.setdefault(case.get("partner_id", case_id), []).append(case_id)
    wanted = selection.split(",") if isinstance(selection, str) else selection
    chosen: dict[str, dict] = {}
    missing = []
    for item in wanted:
        if item in cases:
            chosen[item] = cases[item]
        elif item in by_partner:
            for case_id in by_partner[item]:
                chosen[case_id] = cases[case_id]
        else:
            missing.append(item)
    if missing:
        raise SystemExit(f"Unknown persona(s): {', '.join(missing)}. Available: {', '.join(cases)}")
    return chosen


def persona_file_path(persona_id: str, *, track: str) -> Path:
    """The YAML file that defines a persona id or a scenario case id."""
    partner = persona_id.split(SCENARIO_SEP, 1)[0]
    return track_file_path("persona", partner, track=track)


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


def gitlab_goal(persona: dict) -> str:
    """What GitLab wants from this meeting. Only scenario cases have one. The seller sees it, the partner doesn't."""
    return persona.get("seller_brief", {}).get("gitlab_goal", "")


def account_brief(persona: dict) -> str:
    """What the seller is allowed to know before the call."""
    brief = dict(persona["public"])
    goal = gitlab_goal(persona)
    if goal:
        brief["gitlab_goal"] = goal
    return as_yaml(brief)
