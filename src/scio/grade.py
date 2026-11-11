"""Grade a finished call three ways.

1. Code checks: things we can measure without a model (talk ratio, questions).
2. Buyer debrief: the buyer model, still holding its private brief, reports
   whether its hidden concern came out and was handled. This is the closest
   thing we have to ground truth, because only the buyer knows the secret.
3. Judge: a separate model scores the transcript against the track rubric
   (1 to 5 dimensions, yes/no checks, or both) and lists any claims the seller
   made that the fact sheet doesn't support.
"""

from __future__ import annotations

import re

from . import loaders
from .llm import LLM
from .simulate import format_transcript

DEBRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "hidden_concern_surfaced": {
            "type": "boolean",
            "description": (
                "Did your primary concern come out clearly during the call, because the seller asked or created the "
                "opening?"
            ),
        },
        "hidden_concern_addressed": {
            "type": "boolean",
            "description": (
                "Did the seller respond to that concern with something credible and specific? "
                "False if it never surfaced."
            ),
        },
        "dealbreaker_hit": {"type": "boolean", "description": "Did the seller hit any dealbreaker from your brief?"},
        "dealbreaker_detail": {"type": "string", "description": "Which one and what was said. Empty string if none."},
        "next_step_agreed": {
            "type": "string",
            "description": "The concrete next step you agreed to, in a few words. Empty string if none.",
        },
        "would_take_next_meeting": {"type": "boolean"},
        "trust": {"type": "integer", "enum": [1, 2, 3, 4, 5], "description": "How much you trust this seller now."},
        "comment": {
            "type": "string",
            "description": "One or two honest sentences, in your voice, about how the call went.",
        },
    },
    "required": [
        "hidden_concern_surfaced",
        "hidden_concern_addressed",
        "dealbreaker_hit",
        "dealbreaker_detail",
        "next_step_agreed",
        "would_take_next_meeting",
        "trust",
        "comment",
    ],
    "additionalProperties": False,
}

CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "passed": {
            "type": "boolean",
            "description": "True only if the check's pass_when holds and its fail_when does not.",
        },
        "evidence": {
            "type": "string",
            "description": 'The seller\'s words that decide the check, quoted briefly. "none" if nothing decides it.',
        },
    },
    "required": ["passed", "evidence"],
    "additionalProperties": False,
}


def judge_schema(rubric: dict) -> dict:
    """The judge's output. Dimensions give scores; checks give a verdict and evidence each."""
    dims = list(rubric.get("dimensions", {}))
    checks = list(rubric.get("checks", {}))
    properties: dict = {
        "reasoning": {
            "type": "string",
            "description": (
                "Your analysis of the call, dimension by dimension, before scoring."
                if dims
                else "Your analysis of the call, check by check, before deciding each one."
            ),
        },
        "unsupported_claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "quote": {"type": "string", "description": "The seller's words."},
                    "issue": {"type": "string", "description": "Why it is unsupported or a forbidden promise."},
                    "severity": {"type": "string", "enum": ["minor", "major"]},
                },
                "required": ["quote", "issue", "severity"],
                "additionalProperties": False,
            },
        },
    }
    if dims:
        properties["scores"] = {
            "type": "object",
            "properties": {d: {"type": "integer", "enum": [1, 2, 3, 4, 5]} for d in dims},
            "required": dims,
            "additionalProperties": False,
        }
    if checks:
        properties["checks"] = {
            "type": "object",
            "properties": {c: CHECK_SCHEMA for c in checks},
            "required": checks,
            "additionalProperties": False,
        }
    properties["best_moment"] = {
        "type": "string",
        "description": "The seller's single best moment, quoted briefly.",
    }
    properties["worst_moment"] = {
        "type": "string",
        "description": "The seller's single worst moment, quoted briefly.",
    }
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def code_metrics(turns: list[dict]) -> dict:
    seller = [t["text"] for t in turns if t["speaker"] == "seller"]
    buyer = [t["text"] for t in turns if t["speaker"] == "buyer"]
    sw = sum(len(t.split()) for t in seller)
    bw = sum(len(t.split()) for t in buyer)
    questions = [t.count("?") for t in seller]
    first_q = next((i + 1 for i, q in enumerate(questions) if q), None)
    return {
        "seller_turns": len(seller),
        "seller_words": sw,
        "buyer_words": bw,
        "talk_ratio": round(sw / (sw + bw), 2) if sw + bw else 0.0,
        "avg_seller_turn_words": round(sw / len(seller), 1) if seller else 0.0,
        "seller_questions": sum(questions),
        "first_question_turn": first_q,
        "seller_used_spanish": any(_looks_spanish(t) for t in seller),
    }


_SPANISH = re.compile(r"\b(que|para|gracias|claro|ustedes|también|cómo|qué|proyecto|equipo|entonces)\b", re.I)


def _looks_spanish(text: str) -> bool:
    return len(_SPANISH.findall(text)) >= 3


def weighted_score(scores: dict, rubric: dict) -> float | None:
    """The weighted 1 to 5 score. A rubric with only checks has no score: None, not 0."""
    if "dimensions" not in rubric:
        return None
    dims = rubric["dimensions"]
    total_w = sum(d["weight"] for d in dims.values())
    return round(sum(scores[k] * d["weight"] for k, d in dims.items()) / total_w, 2)


_CRITERION_WORDS = {
    "hidden_concern_surfaced": "the buyer's hidden concern came out",
    "hidden_concern_addressed": "the seller handled it",
    "no_overpromise": "the seller made no major unsupported claims",
}


def describe_pass(rubric: dict) -> str:
    """The pass rule in words, for reports. Built from pass_criteria so it can't drift from passed()."""
    parts = []
    for key, required in rubric["pass_criteria"].items():
        if key == loaders.THRESHOLD:
            parts.append(f"the weighted rubric score is at least {required}")
        elif required:
            if key in _CRITERION_WORDS:
                parts.append(_CRITERION_WORDS[key])
            else:
                name = loaders.check_label(key, rubric["checks"][key])
                parts.append(f'the judge\'s check "{name}" passes')
    return _join(parts)


def _join(parts: list[str]) -> str:
    if len(parts) <= 2:
        return " and ".join(parts)
    return ", ".join(parts[:-1]) + ", and " + parts[-1]


def _criterion(key: str, debrief: dict, judge: dict) -> bool:
    """The verdict for one pass criterion: the debrief, the judge's overpromise flag, or one of its checks."""
    if key in ("hidden_concern_surfaced", "hidden_concern_addressed"):
        return debrief[key]
    if key == "no_overpromise":
        return not judge["overpromise"]
    if key in judge.get("checks", {}):
        return judge["checks"][key]["passed"]
    raise SystemExit(f"Pass criterion {key!r} is not built in, and the judge gave no verdict for it.")


def passed(debrief: dict, judge: dict, score: float | None, rubric: dict) -> bool:
    """Every criterion marked true must hold, and the weighted score must reach the threshold."""
    for key, required in rubric["pass_criteria"].items():
        if key == loaders.THRESHOLD:
            if score < required:
                return False
        elif required and not _criterion(key, debrief, judge):
            return False
    return True


def grade_call(
    call: dict,
    persona: dict,
    llm: LLM,
    buyer_model: str,
    judge_model: str,
    rubric: dict,
    judge_effort: str | None = None,
    *,
    track: str,
) -> dict:
    transcript = format_transcript(call["turns"])
    hidden = loaders.as_yaml(persona["hidden"])

    debrief, u_debrief = llm.structured(
        buyer_model,
        system="You are an honest evaluator stepping out of a role-play.",
        prompt=loaders.render(
            "debrief",
            track=track,
            persona_public=loaders.as_yaml(persona["public"]),
            persona_hidden=hidden,
            transcript=transcript,
        ),
        schema=DEBRIEF_SCHEMA,
    )

    judge, u_judge = llm.structured(
        judge_model,
        system="You are a strict, fair grader of sales engineering calls.",
        prompt=loaders.render(
            "judge",
            track=track,
            fact_sheet=loaders.fact_sheet(track=track),
            persona_hidden=hidden,
            gitlab_goal=loaders.gitlab_goal(persona),
            rubric=loaders.as_yaml(rubric.get("dimensions", {})),
            checks=loaders.as_yaml(rubric.get("checks", {})),
            transcript=transcript,
        ),
        schema=judge_schema(rubric),
        effort=judge_effort,
    )
    # A major unsupported claim is an overpromise. Minor ones only lower the
    # honesty score.
    judge["overpromise"] = any(c.get("severity") == "major" for c in judge.get("unsupported_claims", []))

    score = weighted_score(judge.get("scores", {}), rubric)
    return {
        "metrics": code_metrics(call["turns"]),
        "debrief": debrief,
        "judge": judge,
        "weighted_score": score,
        "passed": passed(debrief, judge, score, rubric),
        "grading_usage": {"debrief": u_debrief.to_dict(), "judge": u_judge.to_dict()},
        "judge_model": judge_model,
    }
