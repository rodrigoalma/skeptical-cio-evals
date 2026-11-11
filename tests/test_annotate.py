"""Local labeling page: sheet upgrade, saves, validation, summary, token and the tunnel hint."""

import csv
import json
import threading
import urllib.error
import urllib.request

import pytest

from scio import annotate, blind, cli, loaders, runner
from scio.llm import MockLLM

OLD_FIELDS = [
    "n",
    "call_id",
    "persona",
    "seller_model",
    "seller_prompt",
    "transcript_file",
    "your_pass_fail",
    "first_failure_note",
    "other_notes",
]
TOKEN = "test-token-123"


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


def _label(**fields):
    base = {
        "your_pass_fail": "",
        "first_failure_note": "",
        "failure_category": "",
        "first_failure_turn": "",
        "other_notes": "",
        "partner_notes": "",
    }
    base.update(fields)
    return base


def _rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _downgrade_sheet(path):
    """Rewrite a sheet in the columns the pilot used, keeping every value."""
    rows = _rows(path)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OLD_FIELDS)
        writer.writeheader()
        writer.writerows({name: row[name] for name in OLD_FIELDS} for row in rows)


def _request(url, headers=None, body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_sheet_upgrade_keeps_values_and_row_order(tmp_path):
    sheet = tmp_path / "labeling_sheet.csv"
    rows = [
        {
            "n": "1",
            "call_id": "a",
            "persona": "bank_cio",
            "seller_model": "haiku",
            "seller_prompt": "basic",
            "transcript_file": "transcripts/01__a.md",
            "your_pass_fail": "fail",
            "first_failure_note": "Quoted a number",
            "other_notes": "see turn 3",
        },
        {
            "n": "2",
            "call_id": "b",
            "persona": "payer_cto",
            "seller_model": "haiku",
            "seller_prompt": "coached",
            "transcript_file": "transcripts/02__b.md",
            "your_pass_fail": "",
            "first_failure_note": "",
            "other_notes": "",
        },
    ]
    with open(sheet, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OLD_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    assert blind.upgrade_sheet(sheet) is True
    columns, upgraded = blind.read_sheet(sheet)
    assert columns == blind.SHEET_FIELDS
    assert [r["call_id"] for r in upgraded] == ["a", "b"]
    assert upgraded[0]["your_pass_fail"] == "fail"
    assert upgraded[0]["first_failure_note"] == "Quoted a number"
    assert upgraded[0]["other_notes"] == "see turn 3"
    assert upgraded[0]["failure_category"] == upgraded[0]["first_failure_turn"] == ""
    assert blind.upgrade_sheet(sheet) is False


def test_sheet_upgrade_keeps_columns_it_does_not_know(tmp_path):
    sheet = tmp_path / "labeling_sheet.csv"
    with open(sheet, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OLD_FIELDS + ["reviewer"])
        writer.writeheader()
        writer.writerow({**{name: "" for name in OLD_FIELDS}, "call_id": "a", "reviewer": "RB"})
    blind.upgrade_sheet(sheet)
    columns, rows = blind.read_sheet(sheet)
    assert columns[-1] == "reviewer" and rows[0]["reviewer"] == "RB"


def test_store_opens_an_old_sheet_and_upgrades_it_on_disk(tmp_path):
    run_dir, track = _graded_run(tmp_path)
    blind.write(run_dir, track=track)
    sheet = run_dir / blind.OUT_NAME / blind.SHEET_NAME
    _downgrade_sheet(sheet)
    store = annotate.Store(run_dir, track=track)
    assert "failure_category" in _rows(store.sheet)[0]
    assert "partner_notes" in _rows(store.sheet)[0]
    assert len(store.calls_payload()["calls"]) == 8


def test_store_round_trip_saves_and_reloads(tmp_path):
    run_dir, track = _graded_run(tmp_path)
    store = annotate.Store(run_dir, track=track)
    call_id = store.calls_payload()["calls"][0]["call_id"]
    row, progress = store.label(
        call_id,
        _label(
            your_pass_fail="fail",
            first_failure_note="Promised a date",
            failure_category="Overpromise",
            first_failure_turn=2,
            other_notes="  trimmed  ",
            partner_notes="  Too organized  ",
        ),
    )
    assert row["first_failure_turn"] == "2" and row["other_notes"] == "trimmed"
    assert row["partner_notes"] == "Too organized"
    assert progress == {"labeled": 1, "total": 8}

    reloaded = annotate.Store(run_dir, track=track).calls_payload()["calls"]
    assert reloaded[0]["label"]["failure_category"] == "Overpromise"
    assert reloaded[0]["label"]["first_failure_turn"] == "2"
    assert reloaded[0]["label"]["partner_notes"] == "Too organized"
    assert reloaded[1]["label"]["your_pass_fail"] == ""


def test_atomic_write_leaves_no_temp_files_and_keeps_the_old_sheet(tmp_path, monkeypatch):
    run_dir, track = _graded_run(tmp_path)
    store = annotate.Store(run_dir, track=track)
    call_id = store.calls_payload()["calls"][0]["call_id"]
    store.label(call_id, _label(your_pass_fail="pass"))
    assert sorted(p.name for p in store.folder.iterdir()) == sorted(
        ["README.md", blind.SHEET_NAME, "selection_key.csv", "transcripts"]
    )

    before = store.sheet.read_text(encoding="utf-8")

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(blind.os, "replace", broken_replace)
    with pytest.raises(OSError, match="disk full"):
        store.label(call_id, _label(your_pass_fail="fail"))
    monkeypatch.undo()
    assert store.sheet.read_text(encoding="utf-8") == before
    assert not [p for p in store.folder.iterdir() if p.name.endswith(".tmp")]


def test_validation_rejects_bad_labels():
    bad = [
        (_label(your_pass_fail="maybe"), 4, "your_pass_fail must be"),
        (_label(first_failure_turn=0), 4, "between 1 and 4"),
        (_label(first_failure_turn=5), 4, "between 1 and 4"),
        (_label(first_failure_turn="2"), 4, "whole number"),
        (_label(first_failure_turn=True), 4, "whole number"),
        (_label(first_failure_turn=1.5), 4, "whole number"),
        (_label(failure_category="x" * 121), 4, "longer than 120"),
        (_label(other_notes=7), 4, "must be text"),
        (_label(partner_notes="y" * 2001), 4, "partner_notes is longer than 2000"),
        ({**_label(), "extra": "nope"}, 4, "Unknown label field"),
        ({k: v for k, v in _label().items() if k != "other_notes"}, 4, "Missing label field"),
    ]
    for fields, seller_turns, message in bad:
        with pytest.raises(blind.LabelError, match=message):
            blind.validate_label(fields, seller_turns)
    assert blind.validate_label(_label(your_pass_fail="fail", first_failure_turn=4), 4)["first_failure_turn"] == "4"
    assert blind.validate_label(_label(first_failure_turn=None), 4)["first_failure_turn"] == ""


def test_pilot_era_sheet_gains_partner_notes_and_keeps_its_values(tmp_path):
    # The sheet shape the pilot had before partner_notes existed, with a label already typed in.
    run_dir, track = _graded_run(tmp_path)
    blind.write(run_dir, track=track)
    sheet = run_dir / blind.OUT_NAME / blind.SHEET_NAME
    pilot_fields = [name for name in blind.SHEET_FIELDS if name != "partner_notes"]
    rows = _rows(sheet)
    rows[0].update({"your_pass_fail": "fail", "other_notes": "typed before the upgrade"})
    with open(sheet, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=pilot_fields)
        writer.writeheader()
        writer.writerows({name: row[name] for name in pilot_fields} for row in rows)
    before = {row["call_id"]: {name: row[name] for name in pilot_fields} for row in rows}

    store = annotate.Store(run_dir, track=track)
    upgraded = _rows(store.sheet)
    assert list(upgraded[0]) == blind.SHEET_FIELDS
    assert [row["call_id"] for row in upgraded] == list(before)
    for row in upgraded:
        assert {name: row[name] for name in pilot_fields} == before[row["call_id"]]
        assert row["partner_notes"] == ""
    assert upgraded[0]["other_notes"] == "typed before the upgrade"


def test_page_has_the_partner_realism_field():
    page = annotate.TEMPLATE.read_text(encoding="utf-8")
    assert 'id="partner-notes"' in page
    assert "Is this partner believable?" in page
    assert 'case "r":' in page


def test_unknown_call_id_is_rejected(tmp_path):
    run_dir, track = _graded_run(tmp_path)
    store = annotate.Store(run_dir, track=track)
    with pytest.raises(blind.LabelError, match="Unknown call id"):
        store.label("no_such_call", _label(your_pass_fail="pass"))


def test_summary_counts(tmp_path):
    run_dir, track = _graded_run(tmp_path)
    store = annotate.Store(run_dir, track=track)
    ids = [c["call_id"] for c in store.calls_payload()["calls"]]
    store.label(ids[0], _label(your_pass_fail="pass"))
    store.label(ids[1], _label(your_pass_fail="fail", failure_category="Overpromise", first_failure_turn=1))
    store.label(ids[2], _label(your_pass_fail="fail", failure_category="Overpromise", first_failure_turn=2))
    store.label(ids[3], _label(your_pass_fail="fail", failure_category="Missed concern", first_failure_turn=2))

    summary = store.summary()
    assert summary["progress"] == {"labeled": 4, "total": 8}
    assert summary["pass"] == 1 and summary["fail"] == 3
    assert summary["categories"] == [
        {"category": "Overpromise", "count": 2, "calls": [2, 3]},
        {"category": "Missed concern", "count": 1, "calls": [4]},
    ]
    assert summary["failures_by_turn"] == [{"turn": 1, "count": 1}, {"turn": 2, "count": 2}]


def test_payload_has_no_judge_data_and_numbers_seller_turns(tmp_path):
    run_dir, track = _graded_run(tmp_path)
    payload = annotate.Store(run_dir, track=track).calls_payload()
    text = json.dumps(payload).lower()
    for word in ("weighted_score", "judge", "debrief", "grade", "jev", "cost", "scores", "passed", "verdict"):
        assert word not in text, word

    call = payload["calls"][0]
    assert set(call) == {
        "n",
        "call_id",
        "persona_id",
        "partner",
        "partner_private_brief",
        "seller_model",
        "seller_prompt",
        "turns",
        "seller_turns",
        "label",
    }
    assert set(call["label"]) == set(blind.LABEL_FIELDS)
    seller = [t["seller_turn"] for t in call["turns"] if t["speaker"] == "seller"]
    assert seller == list(range(1, len(seller) + 1)) == list(range(1, call["seller_turns"] + 1))
    assert all(t["seller_turn"] is None for t in call["turns"] if t["speaker"] == "buyer")


def test_token_guards_every_route(tmp_path):
    run_dir, track = _graded_run(tmp_path)
    store = annotate.Store(run_dir, track=track)
    server = annotate.make_server(store, host="127.0.0.1", port=0, token=TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    api = {"X-Scio-Token": TOKEN}
    try:
        assert _request(f"{base}/")[0] == 403
        assert _request(f"{base}/?token=wrong")[0] == 403
        status, body = _request(f"{base}/?token={TOKEN}")
        assert status == 200 and b"<!doctype html>" in body

        assert _request(f"{base}/api/calls")[0] == 403
        assert _request(f"{base}/api/calls?token={TOKEN}")[0] == 403
        assert _request(f"{base}/api/calls", {"X-Scio-Token": "wrong"})[0] == 403
        status, body = _request(f"{base}/api/calls", api)
        assert status == 200 and len(json.loads(body)["calls"]) == 8
        assert _request(f"{base}/api/summary", api)[0] == 200
        assert _request(f"{base}/api/nothing", api)[0] == 404
        assert _request(f"{base}/api/nothing")[0] == 403

        call_id = store.calls_payload()["calls"][0]["call_id"]
        post = {**api, "Content-Type": "application/json"}
        assert _request(f"{base}/api/label", None, {"call_id": call_id})[0] == 403
        status, body = _request(f"{base}/api/label", post, {"call_id": call_id, **_label(your_pass_fail="maybe")})
        assert status == 400 and "your_pass_fail" in json.loads(body)["error"]
        status, body = _request(f"{base}/api/label", post, {"call_id": call_id, "your_pass_fail": "pass"})
        assert status == 400 and "Missing label field" in json.loads(body)["error"]
        status, body = _request(f"{base}/api/label", post, {"call_id": "nope", **_label(your_pass_fail="pass")})
        assert status == 400 and "Unknown call id" in json.loads(body)["error"]
        status, body = _request(
            f"{base}/api/label", post, {"call_id": call_id, **_label(your_pass_fail="pass", first_failure_turn=99)}
        )
        assert status == 400 and "between 1 and" in json.loads(body)["error"]
        status, body = _request(f"{base}/api/label", post, {"call_id": call_id, **_label(your_pass_fail="pass")})
        assert status == 200
        assert json.loads(body)["row"]["your_pass_fail"] == "pass"
        assert store.calls_payload()["calls"][0]["label"]["your_pass_fail"] == "pass"
    finally:
        server.shutdown()
        server.server_close()


def test_tunnel_hint_names_the_port_user_and_host():
    hint = annotate.tunnel_hint(8765, "agents", "rodrigos-mac-mini-1")
    assert hint == (
        "From another machine: ssh -L 8765:127.0.0.1:8765 agents@rodrigos-mac-mini-1, then open the same URL there."
    )


def test_cli_rejects_an_empty_host(tmp_path, monkeypatch):
    run_dir, _ = _graded_run(tmp_path)
    monkeypatch.setattr(cli, "load_dotenv", lambda *_: None)
    with pytest.raises(SystemExit, match="can't be empty"):
        cli.main(["annotate", str(run_dir), "--host", " "])


def test_cli_rejects_a_bad_port(tmp_path, monkeypatch):
    run_dir, _ = _graded_run(tmp_path)
    monkeypatch.setattr(cli, "load_dotenv", lambda *_: None)
    with pytest.raises(SystemExit, match="between 1 and 65535"):
        cli.main(["annotate", str(run_dir), "--port", "70000"])


def test_prepare_creates_the_folder_once_and_keeps_labels(tmp_path):
    run_dir, track = _graded_run(tmp_path)
    folder = annotate.prepare(run_dir, track=track)
    assert (folder / blind.SHEET_NAME).exists()
    store = annotate.Store(run_dir, track=track)
    call_id = store.calls_payload()["calls"][0]["call_id"]
    store.label(call_id, _label(your_pass_fail="pass"))
    assert annotate.prepare(run_dir, track=track) == folder
    assert annotate.Store(run_dir, track=track).calls_payload()["calls"][0]["label"]["your_pass_fail"] == "pass"
