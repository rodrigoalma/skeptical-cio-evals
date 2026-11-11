"""Blind error analysis: no grades in the transcripts, stable order, safe refusal."""

import csv
import json
import shutil

import pytest

from scio import blind, cli, loaders, runner
from scio.llm import MockLLM


def _graded_run(tmp_path, name="run"):
    cfg = loaders.load_config()
    cfg["run"].update(
        {
            "personas": "bank_cio,payer_cto",
            "seller_models": ["haiku"],
            "seller_prompts": ["basic", "coached"],
            "seller_efforts": ["medium"],
            "trials": 2,
        }
    )
    run_dir = tmp_path / name
    track = loaders.track_of(cfg)
    runner.write_run_meta(cfg, run_dir, mock=True, track=track)

    def quiet(*a):
        return None

    runner.simulate_all(MockLLM(), cfg, run_dir, track=track, log=quiet)
    runner.grade_all(MockLLM(), cfg, run_dir, track=track, log=quiet)
    return run_dir, track


def _rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_blind_writes_transcripts_sheet_and_key(tmp_path):
    run_dir, track = _graded_run(tmp_path)
    out = blind.write(run_dir, track=track)

    assert out == run_dir / "error_analysis"
    calls = runner.load_calls(run_dir)
    transcripts = sorted((out / "transcripts").glob("*.md"))
    sheet = _rows(out / "labeling_sheet.csv")
    key = _rows(out / "selection_key.csv")
    assert len(calls) == len(transcripts) == len(sheet) == len(key) == 8
    assert (out / "README.md").exists()
    assert [r["n"] for r in sheet] == [str(i) for i in range(1, 9)]
    for row in sheet:
        assert (out / row["transcript_file"]).exists()
        assert row["your_pass_fail"] == row["first_failure_note"] == row["other_notes"] == ""
    assert [r["call_id"] for r in sheet] == [r["call_id"] for r in key]


def test_blind_transcripts_leak_no_grades(tmp_path):
    run_dir, track = _graded_run(tmp_path)
    out = blind.write(run_dir, track=track)

    texts = [p.read_text(encoding="utf-8") for p in (out / "transcripts").glob("*.md")]
    assert all("SELLER:" in t and "BUYER:" in t for t in texts)
    for text in texts:
        for word in ("weighted_score", "debrief", "overpromise", "passed", "judge", "grade", "jev", "cost"):
            assert word not in text.lower(), word
    sheet_text = (out / "labeling_sheet.csv").read_text(encoding="utf-8")
    for word in ("weighted_score", "judge_passed", "debrief", "overpromise"):
        assert word not in sheet_text


def test_blind_order_is_deterministic_for_a_seed(tmp_path):
    run_dir, track = _graded_run(tmp_path)
    first = blind.write(run_dir, track=track, seed=7)
    order = [r["call_id"] for r in _rows(first / "labeling_sheet.csv")]
    shutil.rmtree(first)

    again = blind.write(run_dir, track=track, seed=7)
    assert [r["call_id"] for r in _rows(again / "labeling_sheet.csv")] == order
    shutil.rmtree(again)

    other = blind.write(run_dir, track=track, seed=8)
    other_order = [r["call_id"] for r in _rows(other / "labeling_sheet.csv")]
    assert sorted(other_order) == sorted(order)
    assert other_order != order


def test_blind_sample_has_n_calls_and_repeats_for_a_seed(tmp_path):
    run_dir, track = _graded_run(tmp_path)
    out = blind.write(run_dir, track=track, n=3, seed=7)
    sample = [r["call_id"] for r in _rows(out / "labeling_sheet.csv")]
    assert len(sample) == len(set(sample)) == 3
    shutil.rmtree(out)

    again = blind.write(run_dir, track=track, n=3, seed=7)
    assert [r["call_id"] for r in _rows(again / "labeling_sheet.csv")] == sample


def test_blind_rejects_sample_sizes_outside_the_pool(tmp_path):
    run_dir, track = _graded_run(tmp_path)
    with pytest.raises(SystemExit, match="--n must be between 1 and 8"):
        blind.write(run_dir, track=track, n=9)
    with pytest.raises(SystemExit, match="--n must be between 1 and 8"):
        blind.write(run_dir, track=track, n=0)
    assert not (run_dir / "error_analysis").exists()


def test_blind_skips_ungraded_and_truncated_calls(tmp_path):
    run_dir, track = _graded_run(tmp_path)
    paths = sorted((run_dir / "calls").glob("*.json"))
    truncated = json.loads(paths[0].read_text(encoding="utf-8"))
    truncated["status"] = "truncated"
    paths[0].write_text(json.dumps(truncated), encoding="utf-8")
    ungraded = json.loads(paths[1].read_text(encoding="utf-8"))
    del ungraded["grade"]
    paths[1].write_text(json.dumps(ungraded), encoding="utf-8")

    out = blind.write(run_dir, track=track)
    ids = {r["call_id"] for r in _rows(out / "labeling_sheet.csv")}
    assert len(ids) == 6
    assert truncated["id"] not in ids and ungraded["id"] not in ids


def test_blind_refuses_an_existing_folder(tmp_path):
    run_dir, track = _graded_run(tmp_path)
    blind.write(run_dir, track=track)
    with pytest.raises(SystemExit, match="already exists"):
        blind.write(run_dir, track=track)


def test_blind_cli_writes_the_folder(tmp_path, monkeypatch):
    run_dir, _ = _graded_run(tmp_path)
    monkeypatch.setattr(cli, "load_dotenv", lambda *_: None)
    cli.main(["blind", str(run_dir), "--n", "2", "--seed", "3"])
    assert len(_rows(run_dir / "error_analysis" / "labeling_sheet.csv")) == 2
