import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typesafe_sdk import (
    TypeSafeAPIError,
    TypeSafeAuthenticationError,
    TypeSafePermissionDeniedError,
    TypeSafeRateLimitError,
)

from scio import calibrate, cli, jev, loaders, report, runner
from scio.grade import weighted_score
from scio.llm import AccountError, MockLLM


def _response(rubric, *, overpromise=0.25, model="jev-test", one_based=()):
    scores = {}
    for dimension in rubric["dimensions"]:
        levels = range(1, 6) if dimension in one_based else range(5)
        score = 4.25 if dimension in one_based else 2.5
        scores[dimension] = SimpleNamespace(score=score, legend={level: str(level) for level in levels})
    return SimpleNamespace(
        model=model,
        scores=scores,
        nouls={"overpromise": SimpleNamespace(noul=overpromise)},
        usage=SimpleNamespace(input_tokens=120, output_tokens=15),
    )


class _FakeClient:
    def __init__(self, response, requests):
        self.response = response
        self.requests = requests

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def system_one(self, **kwargs):
        self.requests.append(kwargs)
        return self.response


def _install_fake_client(monkeypatch, response):
    requests = []
    monkeypatch.setattr(jev, "TypeSafeClient", lambda **_: _FakeClient(response, requests))
    return requests


def _call(call_id="call-1", *, grade=True, jev_result=None):
    call = {
        "id": call_id,
        "persona": "bank_cio",
        "turns": [
            {"speaker": "buyer", "text": "We need to reduce operational risk."},
            {"speaker": "seller", "text": "What workflows are most exposed?"},
        ],
    }
    if grade:
        call["grade"] = {
            "debrief": {"hidden_concern_surfaced": True, "hidden_concern_addressed": True},
            "judge": {
                "scores": {dimension: 4 for dimension in loaders.load_rubric()["dimensions"]},
                "overpromise": False,
                "unsupported_claims": [],
            },
            "weighted_score": 4.0,
            "passed": True,
            "metrics": {},
            "grading_usage": {},
        }
    if jev_result is not None:
        call["jev"] = jev_result
    return call


def test_jev_builds_state_questions_and_maps_fractional_scores(monkeypatch):
    rubric = loaders.load_rubric()
    persona = loaders.load_personas(["bank_cio"])["bank_cio"]
    call = _call()
    monkeypatch.setattr(jev.loaders, "fact_sheet", lambda: "fact sheet for test")
    response = _response(rubric, one_based={"business_value"})
    requests = _install_fake_client(monkeypatch, response)

    result = jev.grade_call(call, persona, rubric)

    assert len(requests) == 1
    state = requests[0]["state"]
    assert state == {
        "fact_sheet": "fact sheet for test",
        "persona_hidden_brief": persona["hidden"],
        "transcript": [{"speaker": turn["speaker"], "text": turn["text"]} for turn in call["turns"]],
    }
    assert state["persona_hidden_brief"]["primary_concern"] == persona["hidden"]["primary_concern"]
    questions = requests[0]["questions"]
    assert questions["discovery"].instructions == rubric["dimensions"]["discovery"]["question"]
    assert list(questions["discovery"].criteria) == [
        rubric["dimensions"]["discovery"]["anchors"][level] for level in range(1, 6)
    ]
    assert "must NOT promise" in questions["overpromise"].instructions
    assert result["scores"]["discovery"] == 3.5
    assert result["scores"]["business_value"] == 4.25
    assert result["weighted_score"] == weighted_score(result["scores"], rubric)
    assert result["passed"] is True
    assert result["overpromise_probability"] == 0.25
    assert result["model"] == "jev-test"
    assert result["usage"] == {"input_tokens": 120, "output_tokens": 15}
    assert result["seconds"] >= 0
    assert result["fingerprint"]["model"] == "jev-test"
    assert result["fingerprint"]["fact_sheet"]
    assert result["fingerprint"]["judge_questions"]


@pytest.mark.parametrize(("probability", "expected"), [(0.49, True), (0.5, False)])
def test_jev_shadow_pass_uses_existing_pass_rules(monkeypatch, probability, expected):
    rubric = loaders.load_rubric()
    response = _response(rubric, overpromise=probability)
    _install_fake_client(monkeypatch, response)

    result = jev.grade_call(_call(), loaders.load_personas(["bank_cio"])["bank_cio"], rubric)

    assert result["passed"] is expected


def test_jev_uses_requested_model_when_response_omits_it(monkeypatch):
    rubric = loaders.load_rubric()
    _install_fake_client(monkeypatch, _response(rubric, model=None))

    result = jev.grade_call(_call(), loaders.load_personas(["bank_cio"])["bank_cio"], rubric)

    assert result["model"] == "jev-latest"
    assert result["fingerprint"]["model"] == "jev-latest"


def _write_run(run_dir: Path, calls: list[dict]) -> dict:
    cfg = loaders.load_config()
    cfg["run"]["workers"] = 1
    (run_dir / "calls").mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"config": cfg, "mock": True}), encoding="utf-8")
    for call in calls:
        (run_dir / "calls" / f"{call['id']}.json").write_text(json.dumps(call), encoding="utf-8")
    return cfg


def test_cli_jev_skips_existing_grades_and_force_regrades(monkeypatch, tmp_path):
    run_dir = tmp_path / "run"
    existing = _call("existing", jev_result={"model": "old"})
    missing = _call("missing")
    ungraded = _call("ungraded", grade=False)
    _write_run(run_dir, [existing, missing, ungraded])
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(cli, "load_dotenv", lambda *_: None)
    monkeypatch.setattr(cli.report, "build", lambda *_: run_dir / "report.md")
    rubric = loaders.load_rubric()
    requests = _install_fake_client(monkeypatch, _response(rubric))

    cli.main(["jev", str(run_dir)])
    assert len(requests) == 1
    cli.main(["jev", str(run_dir), "--force"])
    assert len(requests) == 3
    saved = {call["id"]: call for call in runner.load_calls(run_dir)}
    assert saved["existing"]["jev"]["model"] == "jev-test"
    assert saved["missing"]["jev"]["model"] == "jev-test"
    assert "jev" not in saved["ungraded"]


def test_jev_cli_fails_fast_without_key(monkeypatch, tmp_path):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(runner, "jev_all", lambda *_args, **_kwargs: pytest.fail("should not grade"))

    with pytest.raises(SystemExit, match="TYPESAFE_API_KEY is not set"):
        cli.cmd_jev(SimpleNamespace(run_dir=str(tmp_path), force=False))


def test_jev_errors_are_logged_and_auth_errors_stop(tmp_path, monkeypatch):
    rubric = loaders.load_rubric()
    failed_run = tmp_path / "failed"
    _write_run(failed_run, [_call()])

    def fail_client(**_):
        class Client(_FakeClient):
            def system_one(self, **_kwargs):
                raise RuntimeError("offline")

        return Client(None, [])

    monkeypatch.setattr(jev, "TypeSafeClient", fail_client)
    runner.jev_all(loaders.load_config(), failed_run, log=lambda *_: None)
    error = json.loads((failed_run / "errors.jsonl").read_text(encoding="utf-8"))
    assert error["stage"] == "jev"
    assert error["id"] == "call-1"

    auth_run = tmp_path / "auth"
    _write_run(auth_run, [_call()])
    auth_error = TypeSafeAuthenticationError(401, {"error": "unauthorized"}, {}, "bad key")

    def auth_client(**_):
        class Client(_FakeClient):
            def system_one(self, **_kwargs):
                raise auth_error

        return Client(None, [])

    monkeypatch.setattr(jev, "TypeSafeClient", auth_client)
    with pytest.raises(AccountError, match="typesafe_auth"):
        runner.jev_all(loaders.load_config(), auth_run, log=lambda *_: None)
    assert not (auth_run / "errors.jsonl").exists()


@pytest.mark.parametrize(
    ("error_type", "status", "kind"),
    [
        (TypeSafeAuthenticationError, 401, "typesafe_auth"),
        (TypeSafePermissionDeniedError, 403, "typesafe_permission"),
        (TypeSafeAPIError, 402, "typesafe_quota"),
        (TypeSafeRateLimitError, 429, "typesafe_quota"),
    ],
)
def test_jev_maps_account_errors_to_fatal_account_errors(error_type, status, kind):
    error = error_type(status, {"error": "account failure"}, {}, "account failure")

    assert jev._account_failure(error).kind == kind


def test_report_adds_jev_comparison_without_changing_opus_headline(tmp_path):
    cfg = loaders.load_config()
    cfg["run"].update(personas="bank_cio", seller_models=["haiku"], seller_prompts=["basic"],
                       seller_efforts=["medium"], trials=1)
    run_dir = tmp_path / "mock-run"
    runner.write_run_meta(cfg, run_dir, mock=True)
    quiet = lambda *_: None
    runner.simulate_all(MockLLM(), cfg, run_dir, log=quiet)
    runner.grade_all(MockLLM(), cfg, run_dir, log=quiet)
    for path in (run_dir / "calls").glob("*.json"):
        call = json.loads(path.read_text(encoding="utf-8"))
        call["jev"] = {
            "scores": {dimension: float(score) for dimension, score in call["grade"]["judge"]["scores"].items()},
            "overpromise_probability": float(call["grade"]["judge"]["overpromise"]),
            "weighted_score": call["grade"]["weighted_score"],
            "passed": call["grade"]["passed"],
            "model": "jev-test",
            "usage": {"input_tokens": 120, "output_tokens": 15},
            "seconds": 0.5,
            "fingerprint": {},
        }
        path.write_text(json.dumps(call), encoding="utf-8")

    text = report.build(run_dir, cfg).read_text(encoding="utf-8")

    assert "## Jev shadow judge" in text
    assert "| discovery |" in text
    assert "Weighted scores within 1 point: 100% of calls." in text
    assert "Pass agreement: 100%." in text
    assert "Usage: 120 input + 15 output tokens (135 total)." in text
    assert "## Headline" in text
    assert "| Seller model | Prompt | Effort | Calls | Pass rate" in text


def test_calibrate_jev_rounds_scores_and_cli_selects_jev(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    call = _call()
    call["jev"] = {
        "scores": {"discovery": 3.5},
        "overpromise_probability": 0.49,
    }
    _write_run(run_dir, [call])
    labels = tmp_path / "labels.csv"
    labels.write_text(
        "call_id,labeler,dimension,score\ncall-1,me,discovery,4\ncall-1,me,overpromise,0\n",
        encoding="utf-8",
    )

    text = calibrate.calibrate(run_dir, labels, judge="jev")

    assert "## Jev calibration" in text
    assert "| discovery | 1 | 100% |" in text
    assert "Overpromise flag: judge and human agree on 100% of 1 calls." in text

    seen = {}
    monkeypatch.setattr(cli, "load_dotenv", lambda *_: None)
    monkeypatch.setattr(cli.cal, "calibrate", lambda _run, _labels, judge: seen.setdefault("judge", judge) or "")
    monkeypatch.setattr(cli.report, "build", lambda *_: run_dir / "report.md")
    cli.main(["calibrate", str(run_dir), "--labels", str(labels), "--judge", "jev"])
    assert seen["judge"] == "jev"
