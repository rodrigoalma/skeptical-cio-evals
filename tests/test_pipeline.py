"""Offline tests. They use the mock model, so no API key is needed."""

import json
import time

import anthropic
import pytest

from scio import dashboard, fingerprint, loaders, report, runner
from scio.calibrate import calibrate, quadratic_kappa
from scio.grade import code_metrics, passed, weighted_score
from scio.llm import (
    AccountError,
    DEFAULT_BACKEND,
    MockLLM,
    Usage,
    cost_of,
    cost_totals,
    normalize_pricing,
)
from scio.simulate import _view, run_call


def test_personas_have_required_fields():
    personas = loaders.load_personas(track=loaders.DEFAULT_TRACK)
    assert len(personas) >= 5
    for p in personas.values():
        for key in ("name", "title", "company", "company_profile"):
            assert p["public"].get(key), f"{p['id']} missing public.{key}"
        for key in ("opening_line", "primary_concern", "reveals_primary_concern_when", "what_wins_them", "dealbreakers"):
            assert p["hidden"].get(key), f"{p['id']} missing hidden.{key}"


def test_seller_never_sees_hidden_brief():
    for p in loaders.load_personas(track=loaders.DEFAULT_TRACK).values():
        for name in loaders.seller_prompt_names(track=loaders.DEFAULT_TRACK):
            system = loaders.render(f"seller_{name}", track=loaders.DEFAULT_TRACK,
                                    fact_sheet="FS", account_brief=loaders.account_brief(p))
            assert p["hidden"]["primary_concern"].strip()[:60] not in system


def test_rubric_weights_sum_to_one():
    rubric = loaders.load_rubric(track=loaders.DEFAULT_TRACK)
    assert abs(sum(d["weight"] for d in rubric["dimensions"].values()) - 1.0) < 1e-9
    for d in rubric["dimensions"].values():
        assert set(d["anchors"]) == {1, 2, 3, 4, 5}


def test_cost_of_owns_usage_cost_calculation_and_normalizes_legacy_pricing():
    usages = [
        {"model": "claude-sonnet-5-5", "input_tokens": 1000, "output_tokens": 200},
        {"model": "claude-sonnet-5-5", "input_tokens": 500, "output_tokens": 100},
    ]
    legacy = {"claude-sonnet-5-5": [2.0, 10.0]}

    assert normalize_pricing(legacy) == {"claude-sonnet-5-5": {"input": 2.0, "output": 10.0}}
    assert cost_of(usages, legacy) == 0.006
    assert Usage(**usages[0]).cost(normalize_pricing(legacy)) == 0.004
    assert Usage(**{"model": "claude-sonnet-5-5", "input_tokens": 100}).cache_read_tokens == 0


def test_list_price_and_estimated_billed_costs_account_for_cached_tokens():
    prices = {"claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.1}}
    multipliers = {"5m": 1.25, "1h": 2.0}
    usage = Usage(
        model="claude-sonnet-5-5",
        input_tokens=100,
        output_tokens=20,
        cache_read_tokens=30,
        cache_write_5m_tokens=40,
        cache_write_1h_tokens=10,
    )
    serialized = usage.to_dict()

    assert usage.input_tokens + usage.cache_read_tokens + usage.cache_write_5m_tokens + usage.cache_write_1h_tokens == 180
    assert cost_of([serialized], prices) == 0.00056
    assert cost_totals({"seller": [serialized]}, prices, multipliers) == {
        "list_price": {"seller": 0.00056},
        "list_price_total": 0.00056,
        "estimated_billed_total": 0.000543,
    }


def test_legacy_pricing_without_cache_read_only_works_for_uncached_usage():
    legacy_shapes = [
        {"claude-sonnet-5-5": [2.0, 10.0]},
        {"claude-sonnet-5-5": {"input": 2.0, "output": 10.0}},
    ]
    old_usage = {"model": "claude-sonnet-5-5", "input_tokens": 100, "output_tokens": 20}

    for prices in legacy_shapes:
        assert "cache_read" not in normalize_pricing(prices)["claude-sonnet-5-5"]
        assert cost_of([old_usage], prices) == 0.0004
        assert cost_totals({"seller": [old_usage]}, prices)["estimated_billed_total"] == 0.0004


def test_cache_tokens_require_a_cache_read_price():
    prices = {"claude-sonnet-5-5": {"input": 2.0, "output": 10.0}}
    usage = Usage(model="claude-sonnet-5-5", cache_write_5m_tokens=1).to_dict()

    with pytest.raises(ValueError, match="cache_read.*claude-sonnet-5-5"):
        cost_of([usage], prices)
    with pytest.raises(ValueError, match="cache_read.*claude-sonnet-5-5"):
        cost_totals({"seller": [usage]}, prices, {"5m": 1.25, "1h": 2.0})


def test_usage_cost_rejects_models_without_pricing():
    usage = Usage(model="claude-unknown-model", input_tokens=100)

    with pytest.raises(ValueError, match="claude-unknown-model"):
        usage.cost(normalize_pricing({}))


def test_load_config_validates_cache_pricing_and_multipliers(tmp_path):
    cfg = loaders.load_config()
    current_pricing = cfg["pricing"]
    assert cfg["cache_write_multipliers"] == {"5m": 1.25, "1h": 2.0}
    path = tmp_path / "config.yaml"
    path.write_text(loaders.as_yaml(cfg), encoding="utf-8")

    assert loaders.load_config(path)["pricing"] == current_pricing

    cfg["pricing"]["claude-haiku-5-5"] = {"input": 0.1, "cache_read": 0.01}
    path.write_text(loaders.as_yaml(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="claude-haiku-5-5.*missing output"):
        loaders.load_config(path)

    cfg = loaders.load_config()
    del cfg["pricing"]["claude-haiku-5-5"]["cache_read"]
    path.write_text(loaders.as_yaml(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="claude-haiku-5-5.*missing cache_read"):
        loaders.load_config(path)

    cfg = loaders.load_config()
    cfg["pricing"]["claude-haiku-5-5"]["input"] = "0.1"
    path.write_text(loaders.as_yaml(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="claude-haiku-5-5.*input price must be"):
        loaders.load_config(path)

    cfg = loaders.load_config()
    del cfg["pricing"]["claude-haiku-5-5"]
    path.write_text(loaders.as_yaml(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="Missing pricing.*claude-haiku-5-5"):
        loaders.load_config(path)

    cfg = loaders.load_config()
    cfg["cache_write_multipliers"] = {"5m": 1.25}
    path.write_text(loaders.as_yaml(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="cache_write_multipliers is missing 1h"):
        loaders.load_config(path)

    cfg = loaders.load_config()
    cfg["cache_write_multipliers"]["5m"] = "1.25"
    path.write_text(loaders.as_yaml(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="cache_write_multipliers.5m must be"):
        loaders.load_config(path)


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
    p = loaders.load_personas(["bank_cio"], track=loaders.DEFAULT_TRACK)["bank_cio"]
    call = run_call(MockLLM(), p, "coached", "claude-haiku-5-5", "claude-sonnet-5-5",
                    max_seller_turns=8, track=loaders.DEFAULT_TRACK)
    assert call["ended_by"] == "buyer"
    assert call["turns"][0]["speaker"] == "buyer"
    assert len(call["usage"]["seller"]) == sum(t["speaker"] == "seller" for t in call["turns"])
    assert all(
        usage[field] == 0
        for records in call["usage"].values()
        for usage in records
        for field in ("cache_read_tokens", "cache_write_5m_tokens", "cache_write_1h_tokens")
    )
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
    rubric = loaders.load_rubric(track=loaders.DEFAULT_TRACK)
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
    cfg["run"].update(personas="bank_cio,latam_si_lead", seller_models=["haiku"], seller_prompts=["basic", "coached"],
                      seller_efforts=["medium"], trials=1)
    run_dir = tmp_path / "run"
    track = loaders.track_of(cfg)
    runner.write_run_meta(cfg, run_dir, mock=True, track=track)
    llm = MockLLM()
    quiet = lambda *a: None
    runner.simulate_all(llm, cfg, run_dir, track=track, log=quiet)
    runner.grade_all(llm, cfg, run_dir, track=track, log=quiet)
    calls = runner.load_calls(run_dir)
    assert len(calls) == 4 and all("grade" in c for c in calls)

    meta_path = run_dir / "run.json"
    meta = json.loads(meta_path.read_text())
    meta["config"]["pricing"] = {
        model: [rates["input"], rates["output"]]
        for model, rates in cfg["pricing"].items()
    }
    meta["config"].pop("cache_write_multipliers")
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    path = report.build(run_dir, track=track)
    text = path.read_text()
    assert "Mock run" in text and "| haiku | coached | medium |" in text
    assert "Estimated billed with prompt caching:" in text
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
    runner.simulate_all(llm, cfg, run_dir, track=track, log=quiet)
    assert len(runner.load_calls(run_dir)) == 4


def test_smoke_cases_load_and_grade_offline():
    from scio import smoke
    cases = smoke.load_cases(track=loaders.DEFAULT_TRACK)
    assert {c["id"] for c in cases} >= {"strong_discovery", "feature_monologue", "overpromise"}
    for c in cases:
        assert c["turns"][0]["speaker"] == "buyer"
        assert all(t["speaker"] in ("buyer", "seller") for t in c["turns"])
    # The mock can't judge, so we only check the plumbing runs.
    cfg = loaders.load_config()
    smoke.run(MockLLM(), cfg, track=loaders.track_of(cfg), log=lambda *a: None)


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
    walk(judge_schema(loaders.load_rubric(track=loaders.DEFAULT_TRACK)))


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


def test_account_error_stops_the_run(tmp_path):
    def fn(job):
        raise AccountError("api_credits", "Your credit balance is too low.")

    jobs = [(str(i),) for i in range(10)]
    with pytest.raises(AccountError):
        runner._parallel(fn, jobs, 1, lambda _: None, tmp_path, "simulate")
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
    cfg = loaders.load_config()
    track = loaders.track_of(cfg)
    ok = smoke.run(RefusingJudge(), cfg, track=track, log=lines.append)
    assert not ok
    assert any(l.startswith("[ERR ] strong_discovery") and "refusal" in l for l in lines)
    assert sum(l.startswith(("[ok  ]", "[MISS]")) for l in lines) == len(smoke.load_cases(track=track)) - 1


def test_effort_sweep_is_recorded_and_reported(tmp_path):
    cfg = loaders.load_config()
    cfg["run"].update(personas="bank_cio", seller_models=["haiku"], seller_prompts=["coached"],
                      seller_efforts=["high", "low"], trials=1)
    run_dir = tmp_path / "run"
    track = loaders.track_of(cfg)
    runner.write_run_meta(cfg, run_dir, mock=True, track=track)
    quiet = lambda *a: None
    runner.simulate_all(MockLLM(), cfg, run_dir, track=track, log=quiet)
    runner.grade_all(MockLLM(), cfg, run_dir, track=track, log=quiet)

    calls = {c["seller_effort"]: c for c in runner.load_calls(run_dir)}
    assert set(calls) == {"low", "high"}
    assert calls["low"]["id"] == "bank_cio__haiku__coached__low__t1"
    assert all(u["effort"] == "low" for u in calls["low"]["usage"]["seller"])
    assert all(u["effort"] == cfg["effort"]["buyer"] for u in calls["low"]["usage"]["buyer"])
    assert calls["low"]["grade"]["judge_effort"] == cfg["effort"]["judge"]

    text = report.build(run_dir, track=track, cfg=cfg).read_text()
    low, high = text.index("| haiku | coached | low |"), text.index("| haiku | coached | high |")
    assert low < high  # effort levels sort by level, not alphabetically
    assert "seller_effort" in (run_dir / "results.csv").read_text().splitlines()[0]


def test_bad_or_missing_effort_is_rejected():
    from scio import cli

    cfg = loaders.load_config()
    cli._check_efforts(cfg)
    for bad in (["medium", "huge"], []):
        cfg["run"]["seller_efforts"] = bad
        with pytest.raises(SystemExit):
            cli._check_efforts(cfg)
    cfg = loaders.load_config()
    cfg["effort"]["judge"] = None
    with pytest.raises(SystemExit):
        cli._check_efforts(cfg)


def test_runs_from_before_the_prompt_rename_still_work(tmp_path):
    cfg = loaders.load_config()
    cfg["run"].update(personas="bank_cio", seller_models=["haiku"], seller_prompts=["coached"],
                      seller_efforts=["medium"], trials=1)
    run_dir = tmp_path / "run"
    track = loaders.track_of(cfg)
    runner.write_run_meta(cfg, run_dir, mock=True, track=track)
    quiet = lambda *a: None
    runner.simulate_all(MockLLM(), cfg, run_dir, track=track, log=quiet)
    runner.grade_all(MockLLM(), cfg, run_dir, track=track, log=quiet)

    # Make it look like a call saved when the prompt was still called v2.
    new = run_dir / "calls" / "bank_cio__haiku__coached__medium__t1.json"
    call = json.loads(new.read_text())
    call.update(id="bank_cio__haiku__v2__medium__t1", seller_prompt="v2")
    new.unlink()
    (run_dir / "calls" / f"{call['id']}.json").write_text(json.dumps(call))

    runner.simulate_all(MockLLM(), cfg, run_dir, track=track, log=quiet)  # resume finds the old call
    assert [c["id"] for c in runner.load_calls(run_dir)] == ["bank_cio__haiku__v2__medium__t1"]
    assert "| haiku | coached | medium |" in report.build(run_dir, track=track, cfg=cfg).read_text()


def _mock_run(tmp_path, name, **run):
    cfg = loaders.load_config()
    cfg["run"].update({"personas": "bank_cio", "seller_models": ["haiku"], "seller_prompts": ["coached"],
                       "seller_efforts": ["medium"], "trials": 1, **run})
    run_dir = tmp_path / name
    track = loaders.track_of(cfg)
    runner.write_run_meta(cfg, run_dir, mock=True, track=track)
    quiet = lambda *a: None
    runner.simulate_all(MockLLM(), cfg, run_dir, track=track, log=quiet)
    runner.grade_all(MockLLM(), cfg, run_dir, track=track, log=quiet)
    return cfg, run_dir


def test_fingerprint_is_recorded_and_changes_with_the_test():
    cfg = loaders.load_config()
    track = loaders.track_of(cfg)
    fp = fingerprint.record(cfg, ["bank_cio"], ["coached"], track=track)
    assert fp["source"] == "recorded" and set(fp["personas"]) == {"bank_cio"}
    assert fingerprint.record(cfg, ["bank_cio"], ["coached"], track=track)["files"] == fp["files"]

    # Seller choices are what you compare, so they don't change the version.
    other = fingerprint.record({**cfg, "run": {**cfg["run"], "seller_models": ["opus"]}},
                               ["bank_cio"], ["basic"], track=track)
    assert fingerprint.differences(fp, other) == []

    judge = fingerprint.grading("judge-a", "high", "buyer", {"rubric.yaml": "x"}, DEFAULT_BACKEND,
                                track=track)
    assert fingerprint.version(fp, judge) != fingerprint.version(fp, {**judge, "files": {"rubric.yaml": "y"}})
    edited = {**fp, "files": {**fp["files"], "data/fact_sheet.md": "changed"}}
    assert fingerprint.differences(fp, edited) == ["data/fact_sheet.md"]


def test_resume_refuses_a_run_whose_files_changed(tmp_path):
    cfg, run_dir = _mock_run(tmp_path, "run")
    meta = json.loads((run_dir / "run.json").read_text())
    meta["fingerprint"]["files"]["data/fact_sheet.md"] = "something else"
    (run_dir / "run.json").write_text(json.dumps(meta))
    with pytest.raises(SystemExit, match="fact_sheet"):
        runner.write_run_meta(cfg, run_dir, mock=True, track=loaders.track_of(cfg))


def test_dashboard_pools_runs_on_the_same_version(tmp_path):
    _mock_run(tmp_path, "a")
    _mock_run(tmp_path, "b", personas="bank_cio,payer_cto")
    _, c = _mock_run(tmp_path, "c", max_seller_turns=3)  # a different test

    data = dashboard.collect(sorted(tmp_path.iterdir()), loaders.load_config()["pricing"])
    by_version = {v["id"]: v["runs"] for v in data["versions"]}
    assert sorted(by_version.values()) == [["a", "b"], ["c"]]
    assert data["versions"][-1]["changed"] == ["max_seller_turns"]
    assert len(data["rows"]) == 4

    path = dashboard.build_combined(tmp_path, include_mock=True)
    assert "/*__DATA__*/null" not in path.read_text() and not dashboard.build_combined(tmp_path)


def test_old_runs_get_their_version_from_git(tmp_path, monkeypatch):
    _, new = _mock_run(tmp_path, "new")
    _, old = _mock_run(tmp_path, "old")
    meta = json.loads((old / "run.json").read_text())
    del meta["fingerprint"]
    meta["mock"] = False
    (old / "run.json").write_text(json.dumps(meta))
    for p in (old / "calls").glob("*.json"):
        call = json.loads(p.read_text())
        del call["grade"]["fingerprint"]
        call["seller_prompt"] = "v2"
        p.write_text(json.dumps(call))

    # Pretend git's last commit holds the files as they are on disk, with the
    # seller prompt under its old name.
    def fake_git(*args):
        if args[0] == "log":
            return "abc123\n"
        rel = args[1].split(":", 1)[1].replace("seller_v2.md", "seller_coached.md")
        return (loaders.ROOT / rel).read_text(encoding="utf-8")
    monkeypatch.setattr(fingerprint, "_git", fake_git)

    data = dashboard.collect([new, old], loaders.load_config()["pricing"])
    assert len(data["versions"]) == 1 and data["versions"][0]["sources"] == ["recorded", "git"]
    saved = json.loads((old / "run.json").read_text())["fingerprint"]
    assert saved["git"]["commit"] == "abc123" and saved["check"]["opening_lines_mismatch"] == []
    assert {r["prompt"] for r in data["rows"]} == {"coached"}

    # A run whose calls don't open with the persona's line at that commit is flagged.
    del saved["check"]
    p = next((old / "calls").glob("*.json"))
    call = json.loads(p.read_text())
    call["turns"][0]["text"] = "Something else entirely."
    p.write_text(json.dumps(call))
    meta = json.loads((old / "run.json").read_text())
    del meta["fingerprint"]
    (old / "run.json").write_text(json.dumps(meta))
    data = dashboard.collect([old], loaders.load_config()["pricing"])
    assert data["runs"][0]["mismatch"] == ["bank_cio"]
