import json
from types import SimpleNamespace

import pytest
import yaml

from scio import cli, fingerprint, loaders, runner, smoke
from scio.llm import DEFAULT_BACKEND


def _write_track(root, name="technical"):
    track = root / "tracks" / name
    (track / "data" / "personas").mkdir(parents=True)
    (track / "data" / "smoke").mkdir()
    (track / "prompts").mkdir()
    (track / "data" / "fact_sheet.md").write_text("technical fact sheet", encoding="utf-8")
    (track / "data" / "personas" / "bank_cio.yaml").write_text(
        yaml.safe_dump({"id": "bank_cio", "public": {"name": "Test", "title": "CIO", "company": "Bank"},
                        "hidden": {"opening_line": "Hello"}}),
        encoding="utf-8",
    )
    (track / "data" / "smoke" / "known_cases.yaml").write_text(
        "- id: basic\n  turns:\n    - buyer: hello\n  expect: {}\n", encoding="utf-8"
    )
    (track / "rubric.yaml").write_text("dimensions: {technical_discovery: {}}\n", encoding="utf-8")
    (track / "prompts" / "seller_arch.md").write_text("${fact_sheet}", encoding="utf-8")
    (track / "prompts" / "buyer.md").write_text("${persona_public}", encoding="utf-8")
    (track / "prompts" / "judge.md").write_text("${rubric}", encoding="utf-8")
    (track / "prompts" / "debrief.md").write_text("${transcript}", encoding="utf-8")
    return track


def test_track_resolution_lists_tracks_and_rejects_unknown(tmp_path, monkeypatch):
    technical = _write_track(tmp_path)
    monkeypatch.setattr(loaders, "ROOT", tmp_path)

    assert loaders.track_root(track="sales") == tmp_path
    assert loaders.track_root(track="technical") == technical
    assert loaders.track_dir_path(track="sales").as_posix() == "."
    assert loaders.track_file_path("fact_sheet", track="technical").as_posix() == "tracks/technical/data/fact_sheet.md"
    assert loaders.available_tracks() == ["sales", "technical"]
    with pytest.raises(ValueError, match="Unknown track"):
        loaders.track_root(track="missing")
    with pytest.raises(ValueError, match="Unknown track"):
        loaders.track_root(track="../outside")


def test_track_loaders_read_from_track_root(tmp_path, monkeypatch):
    _write_track(tmp_path)
    monkeypatch.setattr(loaders, "ROOT", tmp_path)

    personas = loaders.load_personas("all", track="technical")
    assert personas["bank_cio"]["public"]["name"] == "Test"
    assert loaders.fact_sheet(track="technical") == "technical fact sheet"
    assert list(loaders.load_rubric(track="technical")["dimensions"]) == ["technical_discovery"]
    assert loaders.render("seller_arch", track="technical", fact_sheet="Rendered") == "Rendered"
    assert loaders.seller_prompt_names(track="technical") == ["arch"]
    assert smoke.load_cases(track="technical")[0]["turns"] == [{"speaker": "buyer", "text": "hello"}]


def test_track_fingerprints_preserve_sales_shape_and_separate_other_tracks(tmp_path, monkeypatch):
    cfg = loaders.load_config()
    sales = fingerprint.record(cfg, ["bank_cio"], ["coached"], track=loaders.DEFAULT_TRACK)
    explicit_sales = fingerprint.record({**cfg, "track": "sales"}, ["bank_cio"], ["coached"],
                                        track=loaders.DEFAULT_TRACK)
    assert sales == explicit_sales
    assert "track" not in sales["settings"]
    assert set(sales["files"]) == {"data/fact_sheet.md", "prompts/buyer.md"}

    _write_track(tmp_path)
    monkeypatch.setattr(loaders, "ROOT", tmp_path)
    technical_cfg = {**cfg, "track": "technical"}
    technical = fingerprint.record(technical_cfg, ["bank_cio"], ["arch"], track="technical")
    assert technical["settings"]["track"] == "technical"
    assert "track" in fingerprint.differences(sales, technical)

    sales_grade = fingerprint.grading("judge", "high", "buyer", {}, DEFAULT_BACKEND,
                                      track=loaders.DEFAULT_TRACK)
    technical_grade = fingerprint.grading("judge", "high", "buyer", {}, DEFAULT_BACKEND, track="technical")
    assert "track" not in sales_grade["settings"]
    assert technical_grade["settings"]["track"] == "technical"
    assert fingerprint.version(sales, sales_grade) != fingerprint.version(technical, technical_grade)
    assert fingerprint.jev_grading({}, "sheet", {}, "jev", track="technical")["track"] == "technical"


def test_resume_refuses_a_track_change(tmp_path, monkeypatch):
    cfg = loaders.load_config()
    cfg["run"].update({"personas": "bank_cio", "seller_prompts": ["coached"]})
    run_dir = tmp_path / "run"
    runner.write_run_meta(cfg, run_dir, mock=True, track=loaders.track_of(cfg))

    _write_track(tmp_path)
    monkeypatch.setattr(loaders, "ROOT", tmp_path)
    changed = {**cfg, "track": "technical", "run": {**cfg["run"], "seller_prompts": ["arch"]}}
    with pytest.raises(SystemExit, match="track"):
        runner.write_run_meta(changed, run_dir, mock=True, track=loaders.track_of(changed))


def test_cli_validates_track_and_resume_track_override(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match="Unknown track"):
        cli._check_track("missing")

    monkeypatch.setattr(cli, "load_dotenv", lambda *_: None)
    with pytest.raises(SystemExit):
        cli.main(["run", "--track", "missing"])

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    cfg = loaders.load_config()
    (run_dir / "run.json").write_text(json.dumps({"config": cfg, "mock": True}), encoding="utf-8")
    args = SimpleNamespace(resume=str(run_dir), track="technical", backend=None)
    with pytest.raises(SystemExit, match="track changed"):
        cli.cmd_run(args)


def test_personas_cli_uses_config_track_and_accepts_override(tmp_path, monkeypatch, capsys):
    _write_track(tmp_path)
    monkeypatch.setattr(loaders, "ROOT", tmp_path)
    monkeypatch.setattr(loaders, "available_tracks", lambda: ["sales", "technical"])
    monkeypatch.setattr(loaders, "load_config", lambda: {"track": "technical"})
    monkeypatch.setattr(cli, "load_dotenv", lambda *_: None)

    cli.main(["personas"])
    assert "bank_cio" in capsys.readouterr().out

    cli.main(["personas", "--track", "technical"])
    assert "bank_cio" in capsys.readouterr().out
