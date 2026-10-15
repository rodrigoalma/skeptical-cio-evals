"""Grade a finished call three ways.

1. Code checks: things we can measure without a model (talk ratio, questions).
2. Buyer debrief: the buyer model, still holding its private brief, reports
   whether its hidden concern came out and was handled. This is the closest
   thing we have to ground truth, because only the buyer knows the secret.
3. Judge: a separate model scores the transcript against rubric.yaml and
   lists any claims the seller made that the fact sheet doesn't support.
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
            "description": "Did your primary concern come out clearly during the call, because the seller asked or created the opening?",
        },
        "hidden_concern_addressed": {
            "type": "boolean",
            "description": "Did the seller respond to that concern with something credible and specific? False if it never surfaced.",
        },
        "dealbreaker_hit": {"type": "boolean", "description": "Did the seller hit any dealbreaker from your brief?"},
        "dealbreaker_detail": {"type": "string", "description": "Which one and what was said. Empty string if none."},
        "next_step_agreed": {
            "type": "string",
            "description": "The concrete next step you agreed to, in a few words. Empty string if none.",
        },
        "would_take_next_meeting": {"type": "boolean"},
        "trust": {"type": "integer", "enum": [1, 2, 3, 4, 5], "description": "How much you trust this seller now."},
        "comment": {"type": "string", "description": "One or two honest sentences, in your voice, about how the call went."},
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


def judge_schema(rubric: dict) -> dict:
    dims = list(rubric["dimensions"])
    return {
        "type": "object",
        "properties": {
            "reasoning": {"type": "string", "description": "Your analysis of the call, dimension by dimension, before scoring."},
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
            "scores": {
                "type": "object",
                "properties": {d: {"type": "integer", "enum": [1, 2, 3, 4, 5]} for d in dims},
                "required": dims,
                "additionalProperties": False,
            },
            "best_moment": {"type": "string", "description": "The seller's single best moment, quoted briefly."},
            "worst_moment": {"type": "string", "description": "The seller's single worst moment, quoted briefly."},
        },
        "required": ["reasoning", "unsupported_claims", "scores", "best_moment", "worst_moment"],
        "additionalProperties": False,
    }


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


def weighted_score(scores: dict, rubric: dict) -> float:
    dims = rubric["dimensions"]
    total_w = sum(d["weight"] for d in dims.values())
    return round(sum(scores[k] * d["weight"] for k, d in dims.items()) / total_w, 2)


def passed(debrief: dict, judge: dict, score: float, rubric: dict) -> bool:
    c = rubric["pass_criteria"]
    checks = [
        not c.get("hidden_concern_surfaced") or debrief["hidden_concern_surfaced"],
        not c.get("hidden_concern_addressed") or debrief["hidden_concern_addressed"],
        not c.get("no_overpromise") or not judge["overpromise"],
        score >= c.get("min_weighted_score", 0),
    ]
    return all(checks)


def grade_call(
    call: dict,
    persona: dict,
    llm: LLM,
    buyer_model: str,
    judge_model: str,
    rubric: dict,
    judge_effort: str | None = None,
) -> dict:
    transcript = format_transcript(call["turns"])
    hidden = loaders.as_yaml(persona["hidden"])

    debrief, u_debrief = llm.structured(
        buyer_model,
        system="You are an honest evaluator stepping out of a role-play.",
        prompt=loaders.render(
            "debrief",
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
            fact_sheet=loaders.fact_sheet(),
            persona_hidden=hidden,
            rubric=loaders.as_yaml(rubric["dimensions"]),
            transcript=transcript,
        ),
        schema=judge_schema(rubric),
        effort=judge_effort,
    )
    # A major unsupported claim is an overpromise. Minor ones only lower the
    # honesty score.
    judge["overpromise"] = any(c.get("severity") == "major" for c in judge.get("unsupported_claims", []))

    score = weighted_score(judge["scores"], rubric)
    return {
        "metrics": code_metrics(call["turns"]),
        "debrief": debrief,
        "judge": judge,
        "weighted_score": score,
        "passed": passed(debrief, judge, score, rubric),
        "grading_usage": {"debrief": u_debrief.to_dict(), "judge": u_judge.to_dict()},
        "judge_model": judge_model,
    }
