"""Offline tests. They use the mock model, so no API key is needed."""

import json
import time

import anthropic
import pytest

from scio import loaders, report, runner
from scio.calibrate import calibrate, quadratic_kappa
from scio.grade import code_metrics, passed, weighted_score
from scio.llm import MockLLM
from scio.simulate import _view, run_call


def test_personas_have_required_fields():
    personas = loaders.load_personas()
    assert len(personas) >= 5
    for p in personas.values():
        for key in ("name", "title", "company", "company_profile"):
            assert p["public"].get(key), f"{p['id']} missing public.{key}"
        for key in ("opening_line", "primary_concern", "reveals_primary_concern_when", "what_wins_them", "dealbreakers"):
            assert p["hidden"].get(key), f"{p['id']} missing hidden.{key}"


def test_seller_never_sees_hidden_brief():
    for p in loaders.load_personas().values():
        for name in loaders.seller_prompt_names():
            system = loaders.render(f"seller_{name}", fact_sheet="FS", account_brief=loaders.account_brief(p))
            assert p["hidden"]["primary_concern"].strip()[:60] not in system


def test_rubric_weights_sum_to_one():
    rubric = loaders.load_rubric()
    assert abs(sum(d["weight"] for d in rubric["dimensions"].values()) - 1.0) < 1e-9
    for d in rubric["dimensions"].values():
        assert set(d["anchors"]) == {1, 2, 3, 4, 5}


def test_message_views_alternate_and_start_with_user():
    turns = [
        {"speaker": "buyer", "text": "hi"},
        {"speaker": "seller", "text": "hello?"},
        {"speaker": "buyer", "text": "ok"},
    ]
    for side in ("seller", "buyer"):
        msgs = _view(turns, side)
        assert msgs[0]["role"] == "user"
        assert all(a["role"] != b["role"] for a, b in zip(msgs, msgs[1:]))


def test_mock_call_ends_and_records_usage():
    p = loaders.load_personas(["bank_cio"])["bank_cio"]
    call = run_call(MockLLM(), p, "v2", "claude-haiku-5-5", "claude-sonnet-5-5", max_seller_turns=8)
    assert call["ended_by"] == "buyer"
    assert call["turns"][0]["speaker"] == "buyer"
    assert len(call["usage"]["seller"]) == sum(t["speaker"] == "seller" for t in call["turns"])
    assert "[END_CALL]" not in json.dumps(call["turns"])


def test_code_metrics():
    m = code_metrics([
        {"speaker": "buyer", "text": "one two three four"},
        {"speaker": "seller", "text": "Why? How?"},
    ])
    assert m["seller_questions"] == 2
    assert m["first_question_turn"] == 1
    assert m["talk_ratio"] == round(2 / 6, 2)


def test_pass_logic():
    rubric = loaders.load_rubric()
    good = {k: 4 for k in rubric["dimensions"]}
    s = weighted_score(good, rubric)
    debrief = {"hidden_concern_surfaced": True, "hidden_concern_addressed": True}
    assert passed(debrief, {"overpromise": False}, s, rubric)
    assert not passed(debrief, {"overpromise": True}, s, rubric)
    assert not passed({**debrief, "hidden_concern_surfaced": False}, {"overpromise": False}, s, rubric)


def test_kappa():
    assert quadratic_kappa([1, 2, 3, 4, 5], [1, 2, 3, 4, 5]) == 1.0
    assert quadratic_kappa([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) < 0


def test_full_mock_run(tmp_path):
    cfg = loaders.load_config()
    cfg["run"].update(personas="bank_cio,latam_si_lead", seller_models=["haiku"], seller_prompts=["v1", "v2"], trials=1)
    run_dir = tmp_path / "run"
    runner.write_run_meta(cfg, run_dir, mock=True)
    llm = MockLLM()
    quiet = lambda *a: None
    runner.simulate_all(llm, cfg, run_dir, log=quiet)
    runner.grade_all(llm, cfg, run_dir, log=quiet)
    calls = runner.load_calls(run_dir)
    assert len(calls) == 4 and all("grade" in c for c in calls)

    path = report.build(run_dir, cfg)
    text = path.read_text()
    assert "Mock run" in text and "| haiku | v2 |" in text
    html = (run_dir / "viewer.html").read_text()
    assert "/*__DATA__*/null" not in html

    labels = tmp_path / "labels.csv"
    labels.write_text(
        "call_id,labeler,dimension,score\n"
        + "".join(f"{c['id']},me,discovery,3\n" for c in calls)
    )
    out = calibrate(run_dir, labels)
    assert "discovery" in out

    # Re-running skips calls already on disk
    runner.simulate_all(llm, cfg, run_dir, log=quiet)
    assert len(runner.load_calls(run_dir)) == 4


def test_smoke_cases_load_and_grade_offline():
    from scio import smoke
    cases = smoke.load_cases()
    assert {c["id"] for c in cases} >= {"strong_discovery", "feature_monologue", "overpromise"}
    for c in cases:
        assert c["turns"][0]["speaker"] == "buyer"
        assert all(t["speaker"] in ("buyer", "seller") for t in c["turns"])
    # The mock can't judge, so we only check the plumbing runs.
    smoke.run(MockLLM(), loaders.load_config(), log=lambda *a: None)


def test_structured_schemas_are_strict():
    from scio.grade import DEBRIEF_SCHEMA, judge_schema

    def walk(s):
        if s.get("type") == "object":
            assert s.get("additionalProperties") is False
            assert set(s["required"]) == set(s["properties"])
            for v in s["properties"].values():
                walk(v)
        if s.get("type") == "array":
            walk(s["items"])
        assert "minimum" not in s and "maximum" not in s

    walk(DEBRIEF_SCHEMA)
    walk(judge_schema(loaders.load_rubric()))


def test_auth_error_stops_the_run(tmp_path):
    calls = []

    def fn(job):
        calls.append(job)
        if len(calls) > 1:
            time.sleep(0.2)  # a call already in flight finishes; queued ones must be cancelled
        # Built without __init__ so the test does not depend on the SDK's HTTP library.
        raise anthropic.AuthenticationError.__new__(anthropic.AuthenticationError)

    jobs = [(str(i),) for i in range(10)]
    with pytest.raises(anthropic.AuthenticationError):
        runner._parallel(fn, jobs, 1, lambda _: None, tmp_path, "simulate")
    assert len(calls) <= 2
    assert not (tmp_path / "errors.jsonl").exists()


def test_smoke_reports_a_refusal_and_keeps_going():
    from scio import smoke
    from scio.llm import ModelError

    class RefusingJudge(MockLLM):
        def structured(self, model, system, prompt, schema, max_tokens=16000, effort=None):
            if "Appreciate the honesty" in prompt:  # only in the strong_discovery transcript
                raise ModelError("refusal", "the model declined this request (category: none)")
            return super().structured(model, system, prompt, schema, max_tokens, effort)

    lines = []
    ok = smoke.run(RefusingJudge(), loaders.load_config(), log=lines.append)
    assert not ok
    assert any(l.startswith("[ERR ] strong_discovery") and "refusal" in l for l in lines)
    assert sum(l.startswith(("[ok  ]", "[MISS]")) for l in lines) == len(smoke.load_cases()) - 1
