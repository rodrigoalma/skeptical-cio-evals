"""Binary checks: a rubric with checks and no dimensions grades on its checks.

Sales and sa keep their 1 to 5 dimensions and weighted score. These tests use
canned judge answers, so pass and fail don't depend on the mock's random draws.
"""

from __future__ import annotations

import copy
import csv
import json
import re

import pytest
import yaml

from scio import calibrate, dashboard, grade, jev, loaders, report, runner, smoke
from scio.llm import MockLLM

GITLAB = "gitlab_psm"
CASE = "ai_lab_partnerships_lead__evaluation_kickoff"

DEBRIEF = {
    "hidden_concern_surfaced": True,
    "hidden_concern_addressed": True,
    "dealbreaker_hit": False,
    "dealbreaker_detail": "",
    "next_step_agreed": "Criteria session",
    "would_take_next_meeting": True,
    "trust": 4,
    "comment": "Fine.",
}


def _checks_judge(passed=True, unsupported_claims=()):
    return {
        "reasoning": "Checked each one.",
        "unsupported_claims": list(unsupported_claims),
        "checks": {"pursued_gitlab_goal": {"passed": passed, "evidence": "Can we agree GitLab's goal?"}},
        "best_moment": "Asked for the goal.",
        "worst_moment": "None.",
    }


def _dimension_judge(score):
    dims = loaders.load_rubric(track="sales")["dimensions"]
    return {
        "reasoning": "Checked each dimension.",
        "unsupported_claims": [],
        "scores": {d: score for d in dims},
        "best_moment": "x",
        "worst_moment": "y",
    }


class CannedLLM(MockLLM):
    """Returns a fixed debrief and a fixed judge answer."""

    def __init__(self, judge):
        self.judge_answer = judge

    def structured(self, model, system, prompt, schema, max_tokens=16000, effort=None):
        _, usage = super().structured(model, system, prompt, schema, max_tokens, effort)
        if system.startswith("You are a strict"):
            return copy.deepcopy(self.judge_answer), usage
        return copy.deepcopy(DEBRIEF), usage


def _call(persona_id):
    return {
        "id": "call-1",
        "persona": persona_id,
        "turns": [
            {"speaker": "buyer", "text": "Hello."},
            {"speaker": "seller", "text": "What does success look like?"},
        ],
    }


def _grade(track, case_id, judge):
    persona = loaders.load_personas([case_id], track=track)[case_id]
    rubric = loaders.load_rubric(track=track)
    return grade.grade_call(
        _call(case_id), persona, CannedLLM(judge), "buyer-model", "judge-model", rubric, "high", track=track
    )


def test_gitlab_rubric_grades_on_checks_only():
    rubric = loaders.load_rubric(track=GITLAB)
    assert "dimensions" not in rubric
    assert list(rubric["checks"]) == ["pursued_gitlab_goal"]
    assert rubric["pass_criteria"] == {
        "hidden_concern_surfaced": True,
        "hidden_concern_addressed": True,
        "no_overpromise": True,
        "pursued_gitlab_goal": True,
    }
    assert grade.describe_pass(rubric) == (
        "the buyer's hidden concern came out, the seller handled it, the seller made no major unsupported "
        "claims, and the judge's check \"Pursued GitLab's goal\" passes"
    )


def test_check_verdicts_decide_pass_and_leave_no_score():
    result = _grade(GITLAB, CASE, _checks_judge(passed=True))
    assert result["weighted_score"] is None
    assert "scores" not in result["judge"]
    assert result["passed"] is True


def test_a_failed_check_fails_the_call():
    assert _grade(GITLAB, CASE, _checks_judge(passed=False))["passed"] is False


def test_a_major_overpromise_still_fails_a_checks_call():
    claim = {"quote": "We'll make it the default.", "issue": "forbidden promise", "severity": "major"}
    result = _grade(GITLAB, CASE, _checks_judge(unsupported_claims=[claim]))
    assert result["judge"]["overpromise"] is True
    assert result["passed"] is False


def test_judge_schema_for_checks_asks_for_verdicts_and_no_scores():
    schema = grade.judge_schema(loaders.load_rubric(track=GITLAB))
    assert "scores" not in schema["properties"]
    assert set(schema["required"]) == {"reasoning", "unsupported_claims", "checks", "best_moment", "worst_moment"}
    check = schema["properties"]["checks"]["properties"]["pursued_gitlab_goal"]
    assert set(check["required"]) == {"passed", "evidence"}
    assert check["additionalProperties"] is False


def test_sales_and_sa_still_compute_weighted_scores():
    for track in ("sales", "sa"):
        rubric = loaders.load_rubric(track=track)
        dims = list(rubric["dimensions"])
        assert set(grade.judge_schema(rubric)["properties"]["scores"]["required"]) == set(dims)
        assert grade.weighted_score({d: 4 for d in dims}, rubric) == 4.0

    sales_case = "bank_cio"
    high = _grade("sales", sales_case, _dimension_judge(4))
    assert high["weighted_score"] == 4.0
    assert high["passed"] is True
    low = _grade("sales", sales_case, _dimension_judge(3))
    assert low["weighted_score"] == 3.0
    assert low["passed"] is False  # below the 3.5 threshold


def test_passed_refuses_a_check_the_judge_never_answered():
    rubric = loaders.load_rubric(track=GITLAB)
    with pytest.raises(SystemExit, match="pursued_gitlab_goal"):
        grade.passed(DEBRIEF, {"overpromise": False}, None, rubric)


def _gitlab_rubric() -> dict:
    return copy.deepcopy(loaders.load_rubric(track=GITLAB))


def _rename_check(rubric: dict, new_id: str) -> None:
    rubric["checks"][new_id] = rubric["checks"].pop("pursued_gitlab_goal")


MALFORMED = {
    "check without pass_when": (lambda r: r["checks"]["pursued_gitlab_goal"].pop("pass_when"), "is missing pass_when"),
    "check with an unknown field": (
        lambda r: r["checks"]["pursued_gitlab_goal"].update(weight=1),
        "unknown field(s): weight",
    ),
    "check with blank text": (
        lambda r: r["checks"]["pursued_gitlab_goal"].update(fail_when=" "),
        "checks.pursued_gitlab_goal.fail_when must be non-empty text",
    ),
    "check id with capitals": (lambda r: _rename_check(r, "Pursued"), "check id 'Pursued' must use"),
    "check label blank": (
        lambda r: r["checks"]["pursued_gitlab_goal"].update(label=" "),
        "checks.pursued_gitlab_goal.label must be non-empty text",
    ),
    "check id named like a built-in": (
        lambda r: _rename_check(r, "no_overpromise"),
        "is the name of a built-in pass criterion",
    ),
    "checks not a mapping": (lambda r: r.update(checks=["pursued_gitlab_goal"]), "checks must be a non-empty mapping"),
    "criterion nobody answers": (
        lambda r: r["pass_criteria"].update(pursued_goal=True),
        "pass_criteria.pursued_goal is not a built-in criterion or a check",
    ),
    "check criterion not true or false": (
        lambda r: r["pass_criteria"].update(pursued_gitlab_goal="yes"),
        "pass_criteria.pursued_gitlab_goal must be true or false",
    ),
    "threshold without dimensions": (
        lambda r: r["pass_criteria"].update(min_weighted_score=3.5),
        "min_weighted_score needs dimensions",
    ),
    "neither dimensions nor checks": (lambda r: r.pop("checks"), "define dimensions, checks or both"),
    "unknown top-level field": (lambda r: r.update(scores={}), "unknown top-level field(s): scores"),
    "empty pass criteria": (lambda r: r.update(pass_criteria={}), "pass_criteria must be a non-empty mapping"),
}


@pytest.mark.parametrize("name", list(MALFORMED))
def test_malformed_checks_fail_fast(tmp_path, name):
    mutate, message = MALFORMED[name]
    rubric = _gitlab_rubric()
    mutate(rubric)
    path = tmp_path / "rubric.yaml"
    path.write_text(yaml.safe_dump(rubric, sort_keys=False), encoding="utf-8")
    with pytest.raises(SystemExit, match=re.escape(message)):
        loaders.load_rubric(path, track=GITLAB)


def test_dimension_weights_are_still_required(tmp_path):
    rubric = copy.deepcopy(loaders.load_rubric(track="sales"))
    rubric["dimensions"]["discovery"].pop("weight")
    path = tmp_path / "rubric.yaml"
    path.write_text(yaml.safe_dump(rubric, sort_keys=False), encoding="utf-8")
    with pytest.raises(SystemExit, match="dimensions.discovery needs a positive numeric weight"):
        loaders.load_rubric(path, track="sales")


def test_label_and_calibrate_refuse_a_checks_only_track(tmp_path):
    with pytest.raises(SystemExit, match="scio annotate"):
        calibrate.label(tmp_path / "missing", tmp_path / "labels.csv", 5, "me", track=GITLAB)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    cfg = {**loaders.load_config(), "track": GITLAB}
    (run_dir / "run.json").write_text(json.dumps({"config": cfg, "mock": True}), encoding="utf-8")
    with pytest.raises(SystemExit, match="scio annotate"):
        calibrate.calibrate(run_dir, tmp_path / "labels.csv")


def test_jev_refuses_a_checks_only_track():
    with pytest.raises(SystemExit, match="scio annotate"):
        jev.grade_call(_call(CASE), loaders.load_personas([CASE], track=GITLAB)[CASE], _gitlab_rubric(), track=GITLAB)


def _graded(judge_checks):
    return {
        "passed": True,
        "weighted_score": None,
        "debrief": DEBRIEF,
        "judge": {"overpromise": False, "checks": judge_checks},
    }


def test_smoke_reports_each_check_and_rejects_unknown_expect_keys(monkeypatch):
    cfg = loaders.load_config()
    case = {"id": "strong", "persona": CASE, "expect": {"passed": True, "pursued_gitlab_goal": True}}
    monkeypatch.setattr(smoke, "load_cases", lambda *, track: [{**case, "turns": []}])
    monkeypatch.setattr(
        smoke, "grade_call", lambda *a, **k: _graded({"pursued_gitlab_goal": {"passed": True, "evidence": "x"}})
    )
    lines: list[str] = []
    assert smoke.run(MockLLM(), cfg, track=GITLAB, log=lines.append) is True
    row = next(line for line in lines if "strong" in line)
    assert "pursued_gitlab_goal=True" in row and "score" not in row

    monkeypatch.setattr(smoke, "load_cases", lambda *, track: [{**case, "expect": {"pursued_goal": True}, "turns": []}])
    with pytest.raises(SystemExit, match="pursued_goal"):
        smoke.run(MockLLM(), cfg, track=GITLAB, log=lambda *_: None)


def _checks_run(tmp_path):
    cfg = loaders.load_config()
    cfg["track"] = GITLAB
    cfg["run"].update(
        {
            "personas": CASE,
            "seller_models": ["haiku"],
            "seller_prompts": ["coached"],
            "seller_efforts": ["medium"],
            "trials": 1,
        }
    )
    run_dir = tmp_path / "run"
    runner.write_run_meta(cfg, run_dir, mock=True, track=GITLAB)

    def quiet(*_):
        return None

    runner.simulate_all(MockLLM(), cfg, run_dir, track=GITLAB, log=quiet)
    runner.grade_all(MockLLM(), cfg, run_dir, track=GITLAB, log=quiet)
    return run_dir


def test_report_and_dashboard_show_checks_and_no_scores(tmp_path):
    run_dir = _checks_run(tmp_path)

    text = report.build(run_dir, track=GITLAB).read_text(encoding="utf-8")
    assert "| Score |" not in text
    assert "Rubric scores" not in text
    assert "## Checks" in text and "Pursued GitLab's goal" in text
    assert "yes/no checks" in text

    with open(run_dir / "results.csv", newline="", encoding="utf-8") as f:
        header = next(csv.reader(f))
    assert "check_pursued_gitlab_goal" in header and "evidence_pursued_gitlab_goal" in header

    pricing = loaders.load_config()["pricing"]
    data = dashboard.collect([run_dir], pricing, use_git=False)
    assert data["dims"] == []
    assert data["checks"] == [{"id": "pursued_gitlab_goal", "label": "Pursued GitLab's goal"}]
    assert all(r["score"] is None for r in data["rows"])
    path = dashboard.write(run_dir / "dashboard.html", [run_dir], pricing, "Checks", use_git=False)
    assert path is not None
    assert "/*__DATA__*/null" not in path.read_text(encoding="utf-8")


def test_check_label_is_shown_when_the_rubric_gives_one(tmp_path):
    run_dir = _checks_run(tmp_path)
    report.build(run_dir, track=GITLAB)
    assert "Pursued GitLab's goal" in (run_dir / "viewer.html").read_text(encoding="utf-8")


def test_check_label_falls_back_to_the_humanized_id(tmp_path, monkeypatch):
    rubric = _gitlab_rubric()
    spec = rubric["checks"]["pursued_gitlab_goal"]
    assert loaders.check_label("pursued_gitlab_goal", spec) == "Pursued GitLab's goal"
    del spec["label"]
    assert loaders.check_label("pursued_gitlab_goal", spec) == "Pursued gitlab goal"

    run_dir = _checks_run(tmp_path)
    monkeypatch.setattr(loaders, "load_rubric", lambda path=None, *, track: copy.deepcopy(rubric))
    text = report.build(run_dir, track=GITLAB).read_text(encoding="utf-8")
    assert "| Pursued gitlab goal |" in text
    assert "Pursued GitLab's goal" not in text
    data = dashboard.collect([run_dir], loaders.load_config()["pricing"], use_git=False)
    assert data["checks"] == [{"id": "pursued_gitlab_goal", "label": "Pursued gitlab goal"}]
