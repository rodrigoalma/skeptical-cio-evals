import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scio import cli, fingerprint, loaders, runner
from scio.llm import (
    BACKENDS,
    DEFAULT_BACKEND,
    ClaudeCodeFatalError,
    ClaudeCodeLLM,
    ModelError,
    MockLLM,
    backend_of,
    make_llm,
)


def _response(**overrides):
    response = {
        "result": "next line",
        "structured_output": None,
        "stop_reason": "end_turn",
        "is_error": False,
        "subtype": "success",
        "api_error_status": None,
        "usage": {
            "input_tokens": 10,
            "cache_creation_input_tokens": 2,
            "cache_read_input_tokens": 3,
            "output_tokens": 7,
        },
        "modelUsage": {
            "claude-sonnet-5-5": {
                "canonicalModel": "claude-sonnet-5-5-20260101",
                "inputTokens": 15,
                "outputTokens": 7,
            }
        },
        "duration_ms": 5,
    }
    response.update(overrides)
    return response


def _fake_run(response=None, *, returncode=0, stderr="", raw_stdout=None, seen=None):
    def run(command, **kwargs):
        if seen is not None:
            seen["command"] = command
            seen["kwargs"] = kwargs
        assert Path(kwargs["cwd"]).is_dir()
        assert list(Path(kwargs["cwd"]).iterdir()) == []
        stdout = raw_stdout if raw_stdout is not None else json.dumps(response or _response())
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    return run


def test_claude_cli_builds_isolated_argv_and_maps_usage(monkeypatch):
    seen = {}
    monkeypatch.setattr("scio.llm.subprocess.run", _fake_run(seen=seen))
    llm = ClaudeCodeLLM()
    text, usage = llm.complete(
        "claude-sonnet-5-5",
        "system rules",
        [
            {"role": "user", "content": "buyer says hello"},
            {"role": "assistant", "content": "seller says hi"},
        ],
        effort="high",
    )

    command = seen["command"]
    prompt = (
        "This is the conversation so far. Reply with only your next turn as the assistant side.\n"
        "Do not repeat the transcript or add a speaker label.\n\n"
        "Other party: buyer says hello\nAssistant: seller says hi\n\nAssistant:"
    )
    assert command == [
        "claude", "-p", "--model", "claude-sonnet-5-5", "--effort", "high",
        "--system-prompt", "system rules", "--tools", "", "--strict-mcp-config",
        "--setting-sources", "", "--no-session-persistence", "--output-format", "json", prompt,
    ]
    assert seen["kwargs"]["stdin"] == subprocess.DEVNULL
    assert seen["kwargs"]["timeout"] == 300
    assert command[-1].endswith("Assistant:")
    assert "Other party: buyer says hello" in command[-1]
    assert "Assistant: seller says hi" in command[-1]
    assert text == "next line"
    assert usage.input_tokens == 15
    assert usage.output_tokens == 7
    assert usage.served_model == "claude-sonnet-5-5-20260101"
    assert usage.effort == "high"


def test_claude_cli_removes_api_auth_from_subprocess_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-api-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-auth-token")
    monkeypatch.setenv("PATH", "/test/path")
    seen = {}
    monkeypatch.setattr("scio.llm.subprocess.run", _fake_run(seen=seen))

    ClaudeCodeLLM().complete("claude-sonnet-5-5", "sys", [])

    subprocess_env = seen["kwargs"]["env"]
    assert "ANTHROPIC_API_KEY" not in subprocess_env
    assert "ANTHROPIC_AUTH_TOKEN" not in subprocess_env
    assert subprocess_env["PATH"] == "/test/path"
    assert os.environ["ANTHROPIC_API_KEY"] == "test-api-key"
    assert os.environ["ANTHROPIC_AUTH_TOKEN"] == "test-auth-token"


def test_structured_cli_passes_schema_and_maps_tool_use(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "scio.llm.subprocess.run",
        _fake_run(_response(structured_output={"score": 4}, stop_reason="tool_use"), seen=seen),
    )
    schema = {"type": "object", "properties": {"score": {"type": "integer"}}}
    result, usage = ClaudeCodeLLM().structured("claude-sonnet-5-5", "sys", "grade this", schema)

    command = seen["command"]
    schema_json = command[command.index("--json-schema") + 1]
    assert json.loads(schema_json) == schema
    assert "--effort" not in command
    assert "--bare" not in command
    assert result == {"score": 4}
    assert usage.stop_reason == "end_turn"


def test_structured_cli_falls_back_to_result_json_and_reports_bad_json(monkeypatch):
    monkeypatch.setattr(
        "scio.llm.subprocess.run",
        _fake_run(_response(result='{"score": 3}')),
    )
    assert ClaudeCodeLLM().structured("claude-sonnet-5-5", "sys", "grade", {})[0] == {"score": 3}

    monkeypatch.setattr("scio.llm.subprocess.run", _fake_run(_response(result="not json")))
    with pytest.raises(ModelError) as error:
        ClaudeCodeLLM().structured("claude-sonnet-5-5", "sys", "grade", {})
    assert error.value.kind == "bad_json"

    monkeypatch.setattr("scio.llm.subprocess.run", _fake_run(raw_stdout="not JSON"))
    with pytest.raises(ModelError) as error:
        ClaudeCodeLLM().complete("claude-sonnet-5-5", "sys", [])
    assert error.value.kind == "cli_error"


def test_cli_failures_and_fatal_account_errors(monkeypatch):
    monkeypatch.setattr("scio.llm.subprocess.run", _fake_run(_response(result="Please explain the usage limit.")))
    text, _ = ClaudeCodeLLM().complete("claude-sonnet-5-5", "sys", [])
    assert text == "Please explain the usage limit."

    monkeypatch.setattr("scio.llm.subprocess.run", _fake_run(_response(is_error=True, result="bad request")))
    with pytest.raises(ModelError) as error:
        ClaudeCodeLLM().complete("claude-sonnet-5-5", "sys", [])
    assert error.value.kind == "cli_error"

    monkeypatch.setattr(
        "scio.llm.subprocess.run",
        _fake_run(raw_stdout="", stderr="process failed", returncode=1),
    )
    with pytest.raises(ModelError) as error:
        ClaudeCodeLLM().complete("claude-sonnet-5-5", "sys", [])
    assert error.value.kind == "cli_error"

    for message, kind in (("Not logged in. Please run /login.", "cli_auth"),
                          ("Usage limit reached. Try again later.", "cli_usage_limit"),
                          ("Your credit balance is too low.", "cli_usage_limit")):
        monkeypatch.setattr(
            "scio.llm.subprocess.run",
            _fake_run(_response(is_error=True, result=message)),
        )
        with pytest.raises(ClaudeCodeFatalError) as error:
            ClaudeCodeLLM().complete("claude-sonnet-5-5", "sys", [])
        assert error.value.kind == kind


def test_served_model_mismatch_uses_first_model_usage_entry(monkeypatch):
    response = _response(modelUsage={
        "another-model": {"canonicalModel": "claude-opus-5-5-20260101"},
    })
    monkeypatch.setattr("scio.llm.subprocess.run", _fake_run(response))
    with pytest.raises(ModelError) as error:
        ClaudeCodeLLM().complete("claude-sonnet-5-5", "sys", [])
    assert error.value.kind == "served_model_mismatch"
    assert error.value.usage.served_model == "claude-opus-5-5-20260101"


def test_backend_validation_and_mock_remains_unchanged(monkeypatch):
    assert DEFAULT_BACKEND == "api"
    assert BACKENDS == (DEFAULT_BACKEND, "subscription")
    assert backend_of({}) == DEFAULT_BACKEND
    assert isinstance(make_llm(True, DEFAULT_BACKEND), MockLLM)
    assert isinstance(make_llm(True, "subscription"), MockLLM)
    with pytest.raises(ValueError, match="Unknown backend"):
        make_llm(True, "unknown")
    with pytest.raises(SystemExit, match="Unknown backend"):
        cli._check_key(False, "unknown")

    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit, match="claude.*PATH"):
        cli._check_key(False, "subscription")


def test_api_fingerprints_keep_their_old_shape_and_subscription_is_separate():
    cfg = loaders.load_config()
    api_sim = fingerprint.record(cfg, ["bank_cio"], ["coached"])
    legacy_sim_settings = {
        "buyer_model": cfg["models"][cfg["roles"]["buyer"]],
        "buyer_effort": cfg["effort"]["buyer"],
        "max_seller_turns": cfg["run"]["max_seller_turns"],
        "max_tokens_per_turn": cfg["run"]["max_tokens_per_turn"],
    }
    assert api_sim["settings"] == legacy_sim_settings
    api_grade = fingerprint.grading("judge", "high", "buyer", {"rubric.yaml": "x"}, DEFAULT_BACKEND)
    legacy_grade_settings = {"judge_model": "judge", "judge_effort": "high", "debrief_model": "buyer"}
    assert api_grade["settings"] == legacy_grade_settings
    assert fingerprint.version(api_sim, api_grade) == fingerprint.short_hash({
        "simulation": {"files": api_sim["files"], "settings": legacy_sim_settings},
        "grading": {"files": api_grade["files"], "settings": legacy_grade_settings},
    })

    subscription_cfg = {**cfg, "backend": "subscription"}
    subscription_sim = fingerprint.record(subscription_cfg, ["bank_cio"], ["coached"])
    assert subscription_sim["settings"]["backend"] == "subscription"
    assert fingerprint.differences(api_sim, subscription_sim) == ["backend"]
    subscription_grade = fingerprint.grading("judge", "high", "buyer", {"rubric.yaml": "x"}, "subscription")
    assert subscription_grade["settings"]["backend"] == "subscription"
    assert fingerprint.version(api_sim, api_grade) != fingerprint.version(subscription_sim, subscription_grade)
    assert fingerprint.differences(subscription_sim, api_sim) == ["backend"]


def test_resume_refuses_a_backend_change(tmp_path):
    api_cfg = loaders.load_config()
    run_dir = tmp_path / "run"
    runner.write_run_meta(api_cfg, run_dir, mock=True)
    subscription_cfg = {**api_cfg, "backend": "subscription"}
    with pytest.raises(SystemExit, match="backend"):
        runner.write_run_meta(subscription_cfg, run_dir, mock=True)
