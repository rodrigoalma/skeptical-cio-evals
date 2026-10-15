"""Load config, personas, prompts and the rubric from the repo."""

from __future__ import annotations

from pathlib import Path
from string import Template

import yaml

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
PROMPTS = ROOT / "prompts"

# Effort levels from least to most thinking.
EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_config(path: Path | None = None) -> dict:
    return load_yaml(path or ROOT / "config.yaml")


def load_rubric(path: Path | None = None) -> dict:
    return load_yaml(path or ROOT / "rubric.yaml")


def load_personas(selection: str | list[str] = "all") -> dict[str, dict]:
    personas = {}
    for path in sorted((DATA / "personas").glob("*.yaml")):
        p = load_yaml(path)
        personas[p["id"]] = p
    if selection in ("all", ["all"]):
        return personas
    wanted = selection.split(",") if isinstance(selection, str) else selection
    missing = [w for w in wanted if w not in personas]
    if missing:
        raise SystemExit(f"Unknown persona(s): {', '.join(missing)}. Available: {', '.join(personas)}")
    return {w: personas[w] for w in wanted}


def fact_sheet() -> str:
    return (DATA / "fact_sheet.md").read_text(encoding="utf-8")


def render(prompt_name: str, **values: str) -> str:
    """Fill a prompt template from prompts/<name>.md using ${placeholders}."""
    text = (PROMPTS / f"{prompt_name}.md").read_text(encoding="utf-8")
    return Template(text).substitute(**values)


def seller_prompt_names() -> list[str]:
    return sorted(p.stem.removeprefix("seller_") for p in PROMPTS.glob("seller_*.md"))


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
