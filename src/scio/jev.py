"""Run Jev as a separate shadow judge for completed calls."""

from __future__ import annotations

from statistics import mean
from time import perf_counter

from typesafe_sdk import Noul, Score, TypeSafeAPIError, TypeSafeClient, TypeSafeError

from . import fingerprint, loaders
from .grade import passed, weighted_score
from .llm import AccountError

MODEL = "jev-latest"
OVERPROMISE_INSTRUCTIONS = (
    "Did the seller make a major unsupported claim? Answer yes only if a seller statement is false "
    "according to `fact_sheet`, or promises something in its 'Things the seller must NOT promise' section. "
    "Do not count general opinions, questions, offers to follow up, or claims that are only vague or inflated. "
    "Evaluate seller turns in `transcript` against `fact_sheet`."
)


def _question_specs(rubric: dict) -> dict:
    questions = {}
    for dimension, spec in rubric["dimensions"].items():
        anchors = spec["anchors"]
        questions[dimension] = {
            "type": "score",
            "instructions": spec["question"],
            "criteria": [anchors[level] for level in sorted(anchors, key=int)],
        }
    questions["overpromise"] = {"type": "noul", "instructions": OVERPROMISE_INSTRUCTIONS}
    return questions


def build_state(call: dict, persona: dict, *, track: str) -> dict:
    return {
        "fact_sheet": loaders.fact_sheet(track=track),
        "persona_hidden_brief": persona["hidden"],
        "transcript": [{"speaker": turn["speaker"], "text": turn["text"]} for turn in call["turns"]],
    }


def _questions(specs: dict) -> dict:
    questions = {}
    for name, spec in specs.items():
        if spec["type"] == "score":
            questions[name] = Score(instructions=spec["instructions"], criteria=spec["criteria"])
        elif spec["type"] == "noul":
            questions[name] = Noul(instructions=spec["instructions"])
        else:
            raise ValueError(f"Unknown Jev question type: {spec['type']}")
    return questions


def _rubric_score(answer) -> float:
    levels = sorted(int(level) for level in answer.legend)
    if levels not in (list(range(5)), list(range(1, 6))):
        raise ValueError(f"Jev score legend must use levels 0-4 or 1-5, got {levels}")
    score = float(answer.score) + (1 - levels[0])
    if not 1 <= score <= 5:
        raise ValueError(f"Jev score is outside the rubric range after mapping: {score}")
    return score


def _account_failure(error: TypeSafeAPIError) -> AccountError | None:
    kinds = {401: "typesafe_auth", 403: "typesafe_permission", 402: "typesafe_quota", 429: "typesafe_quota"}
    kind = kinds.get(error.status)
    return AccountError(kind, str(error)) if kind else None


def grade_call(call: dict, persona: dict, rubric: dict, *, track: str) -> dict:
    state = build_state(call, persona, track=track)
    specs = _question_specs(rubric)
    questions = _questions(specs)
    started = perf_counter()
    try:
        with TypeSafeClient(model=MODEL) as client:
            response = client.system_one(state=state, questions=questions)
    except TypeSafeAPIError as error:
        fatal = _account_failure(error)
        if fatal:
            raise fatal from error
        raise
    except TypeSafeError as error:
        if "API key" in str(error):
            raise AccountError("typesafe_auth", str(error)) from error
        raise

    model = response.model or MODEL
    scores = {dimension: _rubric_score(response.scores[dimension]) for dimension in rubric["dimensions"]}
    overpromise_probability = float(response.nouls["overpromise"].noul)
    score = weighted_score(scores, rubric)
    debrief = call["grade"]["debrief"]
    shadow_passed = passed(debrief, {"overpromise": overpromise_probability >= 0.5}, score, rubric)
    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    fact_sheet = state["fact_sheet"]

    return {
        "scores": scores,
        "overpromise_probability": overpromise_probability,
        "weighted_score": score,
        "passed": shadow_passed,
        "model": model,
        "usage": usage,
        "seconds": round(perf_counter() - started, 2),
        "fingerprint": fingerprint.jev_grading(rubric, fact_sheet, specs, model, track=track),
    }


def comparison(calls: list[dict], dimensions: list[str]) -> dict | None:
    graded = [call for call in calls if "jev" in call]
    if not graded:
        return None
    input_tokens = sum(call["jev"]["usage"]["input_tokens"] for call in graded)
    output_tokens = sum(call["jev"]["usage"]["output_tokens"] for call in graded)
    return {
        "dimensions": {
            dimension: {
                "opus_mean": mean(call["grade"]["judge"]["scores"][dimension] for call in graded),
                "jev_mean": mean(call["jev"]["scores"][dimension] for call in graded),
            }
            for dimension in dimensions
        },
        "weighted_within_one": mean(
            abs(call["grade"]["weighted_score"] - call["jev"]["weighted_score"]) <= 1 for call in graded
        ),
        "pass_agreement": mean(call["grade"]["passed"] == call["jev"]["passed"] for call in graded),
        "mean_seconds": mean(call["jev"]["seconds"] for call in graded),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "calls": len(graded),
    }
