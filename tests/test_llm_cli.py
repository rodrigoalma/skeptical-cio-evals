import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import anthropic
import pytest

from scio import cli, fingerprint, loaders, runner
from scio.llm import (
    BACKENDS,
    DEFAULT_BACKEND,
    AccountError,
    AnthropicLLM,
    CREDIT_BALANCE_TOO_LOW_MARKER,
    ClaudeCodeLLM,
    ModelError,
    MockLLM,
    backend_of,
    cost_of,
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


def _fake_run(response=None, *, returncode=0, stderr="", raw_stdout=None, auth_status=None, seen=None):
    def run(command, **kwargs):
        is_auth_status = command[:3] == ["claude", "auth", "status"]
        if seen is not None:
            if is_auth_status:
                seen.setdefault("auth_calls", []).append((command, kwargs))
            else:
                seen["command"] = command
                seen["kwargs"] = kwargs
        assert Path(kwargs["cwd"]).is_dir()
        assert list(Path(kwargs["cwd"]).iterdir()) == []
        if is_auth_status:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(auth_status or {"loggedIn": True, "email": "cio@example.test"}),
                stderr="",
            )
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
    assert usage.input_tokens == 10
    assert usage.cache_read_tokens == 3
    assert usage.cache_write_5m_tokens == 0
    assert usage.cache_write_1h_tokens == 2
    assert usage.input_tokens + usage.cache_read_tokens + usage.cache_write_5m_tokens + usage.cache_write_1h_tokens == 15
    assert usage.output_tokens == 7
    prices = {"claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.1}}
    assert cost_of([usage.to_dict()], prices) == 0.0001
    assert usage.served_model == "claude-sonnet-5-5-20260101"
    assert usage.effort == "high"


def test_claude_cli_maps_cache_creation_breakdown_without_double_counting(monkeypatch):
    response = _response(usage={
        "input_tokens": 10,
        "cache_read_input_tokens": 20,
        "cache_creation_input_tokens": 30,
        "cache_creation": {"ephemeral_1h_input_tokens": 30, "ephemeral_5m_input_tokens": 0},
        "output_tokens": 7,
    })
    monkeypatch.setattr("scio.llm.subprocess.run", _fake_run(response))

    _, usage = ClaudeCodeLLM().complete("claude-sonnet-5-5", "system", [])

    assert usage.input_tokens == 10
    assert usage.cache_read_tokens == 20
    assert usage.cache_write_5m_tokens == 0
    assert usage.cache_write_1h_tokens == 30
    assert usage.input_tokens + usage.cache_read_tokens + usage.cache_write_5m_tokens + usage.cache_write_1h_tokens == 60
    prices = {"claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.1}}
    assert cost_of([usage.to_dict()], prices) == 0.00019


def test_claude_cli_maps_missing_cache_usage_to_zero(monkeypatch):
    response = _response(usage={
        "input_tokens": None,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
        "cache_creation": {"ephemeral_1h_input_tokens": None, "ephemeral_5m_input_tokens": None},
        "output_tokens": None,
    })
    monkeypatch.setattr("scio.llm.subprocess.run", _fake_run(response))

    _, usage = ClaudeCodeLLM().complete("claude-sonnet-5-5", "system", [])

    assert usage.input_tokens == 0
    assert usage.cache_read_tokens == 0
    assert usage.cache_write_5m_tokens == 0
    assert usage.cache_write_1h_tokens == 0
    assert usage.output_tokens == 0


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
    auth_command, auth_kwargs = seen["auth_calls"][0]
    assert auth_command == ["claude", "auth", "status", "--json"]
    assert "ANTHROPIC_API_KEY" not in auth_kwargs["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in auth_kwargs["env"]


def test_claude_cli_reads_account_email_once_for_concurrent_calls(monkeypatch):
    seen = {}
    monkeypatch.setattr("scio.llm.subprocess.run", _fake_run(seen=seen))
    llm = ClaudeCodeLLM()

    with ThreadPoolExecutor(max_workers=8) as pool:
        replies = list(pool.map(lambda _: llm.complete("claude-sonnet-5-5", "sys", [])[0], range(12)))

    assert replies == ["next line"] * 12
    assert len(seen["auth_calls"]) == 1


def test_claude_cli_requires_a_logged_in_account_with_email(monkeypatch):
    for auth_status in (
        {"loggedIn": False, "email": "cio@example.test"},
        {"loggedIn": True, "email": ""},
    ):
        seen = {}
        monkeypatch.setattr(
            "scio.llm.subprocess.run",
            _fake_run(auth_status=auth_status, seen=seen),
        )

        llm = ClaudeCodeLLM()

        def call():
            try:
                llm.complete("claude-sonnet-5-5", "sys", [])
            except AccountError as error:
                return error.kind

        with ThreadPoolExecutor(max_workers=4) as pool:
            assert list(pool.map(lambda _: call(), range(4))) == ["cli_auth"] * 4
        assert len(seen["auth_calls"]) == 1
        assert "command" not in seen


def test_claude_cli_fails_replies_that_include_account_email(monkeypatch):
    email = "cio@example.test"
    monkeypatch.setattr(
        "scio.llm.subprocess.run",
        _fake_run(
            _response(result="Contact CIO@EXAMPLE.TEST for details."),
            auth_status={"loggedIn": True, "email": email},
        ),
    )

    with pytest.raises(ModelError) as error:
        ClaudeCodeLLM().complete("claude-sonnet-5-5", "sys", [])

    assert error.value.kind == "context_leak"
    assert error.value.usage is not None
    assert email not in str(error.value).casefold()


def test_claude_cli_allows_a_reply_without_account_email(monkeypatch):
    monkeypatch.setattr(
        "scio.llm.subprocess.run",
        _fake_run(_response(result="A clean seller reply.")),
    )
    assert ClaudeCodeLLM().complete("claude-sonnet-5-5", "sys", [])[0] == "A clean seller reply."


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
        with pytest.raises(AccountError) as error:
            ClaudeCodeLLM().complete("claude-sonnet-5-5", "sys", [])
        assert error.value.kind == kind


def test_cli_refusal_in_error_response_is_labeled_refusal(monkeypatch):
    monkeypatch.setattr(
        "scio.llm.subprocess.run",
        _fake_run(_response(is_error=True, stop_reason="refusal", result="I cannot help with that.")),
    )
    with pytest.raises(ModelError) as error:
        ClaudeCodeLLM().complete("claude-sonnet-5-5", "sys", [])
    assert error.value.kind == "refusal"


@pytest.mark.parametrize(
    "message, maps_to_account_error",
    [
        ("Your credit balance is too low to access the Anthropic API.", True),
        ("The request is invalid.", False),
    ],
)
def test_api_bad_request_maps_only_credit_balance_error(message, maps_to_account_error):
    response = SimpleNamespace(status_code=400, request=object(), headers={})
    bad_request = anthropic.BadRequestError(message, response=response, body=None)

    class Messages:
        def create(self, **kwargs):
            raise bad_request

    llm = AnthropicLLM.__new__(AnthropicLLM)
    llm.client = SimpleNamespace(messages=Messages())

    if maps_to_account_error:
        with pytest.raises(AccountError) as error:
            llm.complete("claude-sonnet-5-5", "sys", [])
        assert error.value.kind == "api_credits"
        assert error.value.__cause__ is bad_request
        assert CREDIT_BALANCE_TOO_LOW_MARKER in error.value.args[0].lower()
    else:
        with pytest.raises(anthropic.BadRequestError) as error:
            llm.complete("claude-sonnet-5-5", "sys", [])
        assert error.value is bad_request


def test_anthropic_caches_complete_calls_and_maps_cache_usage():
    responses = [
        SimpleNamespace(
            model="claude-sonnet-5-5-20260101",
            stop_reason="end_turn",
            stop_details=None,
            content=[SimpleNamespace(type="text", text="seller reply")],
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=7,
                cache_read_input_tokens=3,
                cache_creation_input_tokens=10,
                cache_creation=SimpleNamespace(
                    ephemeral_5m_input_tokens=4,
                    ephemeral_1h_input_tokens=6,
                ),
            ),
        ),
        SimpleNamespace(
            model="claude-sonnet-5-5-20260101",
            stop_reason="end_turn",
            stop_details=None,
            content=[SimpleNamespace(type="text", text='{"score": 4}')],
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=7,
                cache_read_input_tokens=None,
                cache_creation_input_tokens=5,
                cache_creation=None,
            ),
        ),
        SimpleNamespace(
            model="claude-sonnet-5-5-20260101",
            stop_reason="end_turn",
            stop_details=None,
            content=[SimpleNamespace(type="text", text="uncached reply")],
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=7,
                cache_read_input_tokens=None,
                cache_creation_input_tokens=None,
                cache_creation=SimpleNamespace(
                    ephemeral_5m_input_tokens=None,
                    ephemeral_1h_input_tokens=None,
                ),
            ),
        ),
    ]
    requests = []

    class Messages:
        def create(self, **kwargs):
            requests.append(kwargs)
            return responses.pop(0)

    llm = AnthropicLLM.__new__(AnthropicLLM)
    llm.client = SimpleNamespace(messages=Messages())

    _, complete_usage = llm.complete("claude-sonnet-5-5", "sys", [])
    _, structured_usage = llm.structured("claude-sonnet-5-5", "sys", "grade", {})
    _, empty_cache_usage = llm.complete("claude-sonnet-5-5", "sys", [])

    assert requests[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in requests[1]
    assert requests[2]["cache_control"] == {"type": "ephemeral"}
    assert complete_usage.input_tokens == 10
    assert complete_usage.cache_read_tokens == 3
    assert complete_usage.cache_write_5m_tokens == 4
    assert complete_usage.cache_write_1h_tokens == 6
    provider_input_total = 10 + 3 + 10
    assert complete_usage.input_tokens + complete_usage.cache_read_tokens + complete_usage.cache_write_5m_tokens + complete_usage.cache_write_1h_tokens == provider_input_total
    assert structured_usage.cache_read_tokens == 0
    assert structured_usage.cache_write_5m_tokens == 5
    assert structured_usage.cache_write_1h_tokens == 0
    assert empty_cache_usage.cache_read_tokens == 0
    assert empty_cache_usage.cache_write_5m_tokens == 0
    assert empty_cache_usage.cache_write_1h_tokens == 0


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
