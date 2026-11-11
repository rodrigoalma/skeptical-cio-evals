"""Scenario personas: partner files expand into cases, and each side sees the right parts."""

import copy
from typing import Any

import pytest
import yaml

from scio import fingerprint, grade, loaders
from scio.llm import MockLLM


def _scenario(partner, index=0):
    return partner["scenarios"][index]


def _partner() -> dict[str, Any]:
    first: dict[str, Any] = {
        "id": "first",
        "difficulty": "hard",
        "public": {"call_context": "Quarterly review."},
        "seller_brief": {"gitlab_goal": "Agree three accounts."},
        "hidden": {
            "disposition": "Guarded.",
            "opening_line": "Thanks for joining.",
            "primary_concern": "Behind on consumption.",
            "competitor_pull": "A rival tool.",
            "reveals_primary_concern_when": "Requires more than one good question.",
            "wont_share": ["Targets"],
            "objections": ["Show me the accounts."],
            "what_wins_them": ["Named accounts"],
            "dealbreakers": ["Promising a roadmap"],
        },
    }
    second = copy.deepcopy(first)
    second["id"] = "second"
    second["seller_brief"]["gitlab_goal"] = "Another goal."
    second["hidden"]["opening_line"] = "Different opener."
    return {
        "id": "partner",
        "language": "en",
        "public": {
            "name": "Pat Example",
            "title": "Director",
            "company": "Example Co",
            "company_profile": "A test partner.",
        },
        "hidden": {
            "personality": "Calm.",
            "background": ["Shared fact."],
            "public_reporting": ["None."],
            "competitor_ties": ["A rival."],
        },
        "scenarios": [first, second],
    }


def _write_track(tmp_path, monkeypatch, partner):
    personas = tmp_path / "tracks" / "demo" / "data" / "personas"
    personas.mkdir(parents=True)
    (personas / "partner.yaml").write_text(yaml.safe_dump(partner, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(loaders, "ROOT", tmp_path)


def _words(text):
    """Whitespace-normalized text, because the YAML dumper wraps long strings."""
    return " ".join(text.split())


MUTATIONS = {
    "goal missing": (lambda p: _scenario(p)["seller_brief"].clear(), "seller_brief must hold only gitlab_goal"),
    "unknown scenario field": (lambda p: _scenario(p).update(extra="x"), "unknown field"),
    "duplicate scenario": (lambda p: _scenario(p, 1).update(id="first"), "appears more than once"),
    "double underscore id": (lambda p: _scenario(p).update(id="a__b"), "must use lowercase"),
    "bad difficulty": (lambda p: _scenario(p).update(difficulty="extreme"), "difficulty must be one of"),
    "empty wont_share": (
        lambda p: _scenario(p)["hidden"].update(wont_share=[]),
        "hidden.wont_share must be a non-empty list",
    ),
    "missing disposition": (lambda p: _scenario(p)["hidden"].pop("disposition"), "missing hidden field"),
    "scenario field at partner level": (
        lambda p: p["hidden"].update(disposition="x"),
        "holds scenario field",
    ),
    "difficulty on partner": (lambda p: p.update(difficulty="hard"), "belongs on each scenario"),
    "partner company missing": (lambda p: p["public"].pop("company"), "public.company"),
}


def test_plain_persona_files_load_exactly_as_on_disk():
    for track in ("sales", "sa"):
        for case_id, case in loaders.load_personas("all", track=track).items():
            on_disk = loaders.load_yaml(loaders.ROOT / loaders.track_file_path("persona", case_id, track=track))
            assert case == on_disk, (track, case_id)


def test_scenarios_expand_into_cases_with_merged_fields(tmp_path, monkeypatch):
    _write_track(tmp_path, monkeypatch, _partner())
    cases = loaders.load_personas("all", track="demo")
    assert list(cases) == ["partner__first", "partner__second"]
    first = cases["partner__first"]
    assert first["public"]["name"] == "Pat Example"
    assert first["public"]["call_context"] == "Quarterly review."
    assert first["hidden"]["personality"] == "Calm."
    assert first["hidden"]["background"] == ["Shared fact."]
    assert first["hidden"]["disposition"] == "Guarded."
    assert first["seller_brief"] == {"gitlab_goal": "Agree three accounts."}
    assert first["difficulty"] == "hard"
    assert first["partner_id"] == "partner" and first["scenario_id"] == "first"
    assert "seller_brief" not in first["hidden"] and "gitlab_goal" not in first["hidden"]
    assert "gitlab_goal" not in first["public"]
    assert cases["partner__second"]["hidden"]["opening_line"] == "Different opener."


def test_selection_takes_case_ids_or_a_partner_id(tmp_path, monkeypatch):
    _write_track(tmp_path, monkeypatch, _partner())
    assert list(loaders.load_personas(["partner__second"], track="demo")) == ["partner__second"]
    assert list(loaders.load_personas("partner", track="demo")) == ["partner__first", "partner__second"]
    with pytest.raises(SystemExit, match="Unknown persona"):
        loaders.load_personas("partner__nope", track="demo")


def test_a_case_id_maps_to_its_partner_file(tmp_path, monkeypatch):
    _write_track(tmp_path, monkeypatch, _partner())
    assert loaders.persona_file_path("partner__first", track="demo").name == "partner.yaml"
    assert loaders.persona_file_path("bank_cio", track="sales").name == "bank_cio.yaml"


@pytest.mark.parametrize("name", list(MUTATIONS))
def test_malformed_partner_files_fail_fast(tmp_path, monkeypatch, name):
    mutate, message = MUTATIONS[name]
    partner = _partner()
    mutate(partner)
    _write_track(tmp_path, monkeypatch, partner)
    with pytest.raises(SystemExit, match=message) as error:
        loaders.load_personas("all", track="demo")
    assert "partner.yaml" in str(error.value)


def test_account_brief_has_the_goal_only_for_scenarios(tmp_path, monkeypatch):
    _write_track(tmp_path, monkeypatch, _partner())
    brief = _words(loaders.account_brief(loaders.load_personas("all", track="demo")["partner__first"]))
    assert "Agree three accounts." in brief and "Quarterly review." in brief
    plain = {"id": "plain", "public": {"name": "Plain", "title": "Lead", "company": "Acme", "company_profile": "A."}}
    assert loaders.account_brief(plain) == loaders.as_yaml(plain["public"])


def test_gitlab_goal_reaches_the_seller_and_not_the_buyer():
    case_id = "ai_lab_partnerships_lead__evaluation_kickoff"
    case = loaders.load_personas([case_id], track="gitlab_psm")[case_id]
    goal = _words(case["seller_brief"]["gitlab_goal"])
    assert goal in _words(loaders.account_brief(case))
    seller_system = loaders.render(
        "seller_coached", track="gitlab_psm", fact_sheet="FS", account_brief=loaders.account_brief(case)
    )
    assert goal in _words(seller_system)
    buyer_system = loaders.render(
        "buyer",
        track="gitlab_psm",
        persona_public=loaders.as_yaml(case["public"]),
        persona_hidden=loaders.as_yaml(case["hidden"]),
    )
    assert goal not in _words(buyer_system)
    assert "[END_CALL]" in buyer_system


class _RecordingLLM(MockLLM):
    def __init__(self):
        self.judge_prompts = []

    def structured(self, model, system, prompt, schema, max_tokens=16000, effort=None):
        if system.startswith("You are a strict"):
            self.judge_prompts.append(prompt)
        return super().structured(model, system, prompt, schema, max_tokens, effort)


def _call(persona_id):
    return {
        "id": "call-1",
        "persona": persona_id,
        "turns": [
            {"speaker": "buyer", "text": "Hello."},
            {"speaker": "seller", "text": "What does success look like?"},
        ],
    }


def test_judge_sees_the_goal_for_scenarios_and_nothing_extra_otherwise():
    case_id = "ai_lab_partnerships_lead__evaluation_kickoff"
    case = loaders.load_personas([case_id], track="gitlab_psm")[case_id]
    llm = _RecordingLLM()
    grade.grade_call(
        _call(case_id),
        case,
        llm,
        "buyer-model",
        "judge-model",
        loaders.load_rubric(track="gitlab_psm"),
        judge_effort="high",
        track="gitlab_psm",
    )
    assert _words(case["seller_brief"]["gitlab_goal"]) in _words(llm.judge_prompts[-1])
    assert "<gitlab_goal>" in llm.judge_prompts[-1]

    plain = next(iter(loaders.load_personas("all", track="sa").values()))
    grade.grade_call(
        _call(plain["id"]),
        plain,
        llm,
        "buyer-model",
        "judge-model",
        loaders.load_rubric(track="sa"),
        judge_effort="high",
        track="sa",
    )
    assert "<gitlab_goal>" not in llm.judge_prompts[-1]


def test_fingerprint_hashes_each_case_and_plain_files_keep_their_hash():
    cfg = loaders.load_config()
    cases = loaders.load_personas("all", track="gitlab_psm")
    base = fingerprint.simulation(cfg, cases, {"coached": "coached"}, track="gitlab_psm")["personas"]
    assert set(base) == set(cases)

    scenario_changed = copy.deepcopy(cases)
    scenario_changed["ai_lab_partnerships_lead__evaluation_kickoff"]["hidden"]["primary_concern"] += " Extra."
    after = fingerprint.simulation(cfg, scenario_changed, {"coached": "coached"}, track="gitlab_psm")["personas"]
    assert {cid for cid in base if base[cid] != after[cid]} == {"ai_lab_partnerships_lead__evaluation_kickoff"}

    partner_changed = copy.deepcopy(cases)
    for case in partner_changed.values():
        if case["partner_id"] == "ai_lab_partnerships_lead":
            case["hidden"]["personality"] += " Changed."
    after = fingerprint.simulation(cfg, partner_changed, {"coached": "coached"}, track="gitlab_psm")["personas"]
    moved = {cid for cid in base if base[cid] != after[cid]}
    assert moved == {cid for cid in base if cid.startswith("ai_lab_partnerships_lead__")}

    plain_text = (loaders.ROOT / "data/personas/bank_cio.yaml").read_text(encoding="utf-8")
    sales = fingerprint.simulation(cfg, ["bank_cio"], {}, track="sales")["personas"]
    assert sales["bank_cio"] == fingerprint.file_hash(plain_text)


def test_every_gitlab_scenario_is_a_hard_case_with_a_goal_and_a_guarded_reveal():
    cases = loaders.load_personas("all", track="gitlab_psm")
    assert len(cases) == 9
    for case_id, case in cases.items():
        assert case["difficulty"] == "hard", case_id
        assert case["seller_brief"]["gitlab_goal"].strip(), case_id
        assert len(case["hidden"]["wont_share"]) >= 2, case_id
        assert case["hidden"]["reveals_primary_concern_when"].startswith("Requires more than one good question"), (
            case_id
        )
