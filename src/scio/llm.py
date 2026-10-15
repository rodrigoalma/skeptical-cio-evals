"""Thin wrapper around the Anthropic Messages API, plus an offline mock.

Everything else in the project talks to an `LLM` object with two methods:

    complete(...)    -> plain text reply
    structured(...)  -> dict that matches a JSON schema (structured outputs)

Both return a `Usage` record so we can track tokens, cost, latency, which
model actually served the request, and why the response stopped.

Notes for current models (Opus 5.5, Sonnet 5.5, Haiku 5.5):
- Sampling parameters like temperature are not accepted, so we don't send them.
- Adaptive thinking is on by default and its tokens count toward max_tokens,
  so max_tokens is set with headroom and `effort` controls how much it thinks.
- Forced tool_choice is rejected on Opus 5.5 and Sonnet 5.5, so JSON comes
  from structured outputs (output_config.format) instead.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol


class ModelError(RuntimeError):
    """A call that came back but can't be used: refusal, wrong model, bad JSON."""

    def __init__(self, kind: str, message: str, usage: "Usage | None" = None):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.usage = usage


class AccountError(RuntimeError):
    """An account-level failure that makes every remaining call fail, so the run stops."""

    def __init__(self, kind: str, message: str):
        super().__init__(f"{kind}: {message}")
        self.kind = kind


DEFAULT_BACKEND = "api"
BACKENDS = (DEFAULT_BACKEND, "subscription")
CREDIT_BALANCE_TOO_LOW_MARKER = "credit balance is too low"


def backend_of(cfg: dict) -> str:
    return cfg.get("backend", DEFAULT_BACKEND)


@dataclass
class Usage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    stop_reason: str = ""
    served_model: str = ""
    effort: str = ""  # what we sent; empty means the model's API default
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0

    def cost(self, prices: dict[str, dict[str, float]]) -> float:
        model_prices = self._model_prices(prices)
        input_tokens = self.input_tokens + self.cache_read_tokens + self.cache_write_5m_tokens + self.cache_write_1h_tokens
        return (input_tokens * model_prices["input"] + self.output_tokens * model_prices["output"]) / 1_000_000

    def estimated_billed_cost(self, prices: dict[str, dict[str, float]], multipliers: dict[str, float]) -> float:
        model_prices = self._model_prices(prices)
        if self.cache_write_5m_tokens and "5m" not in multipliers:
            raise ValueError("Missing 5m cache write multiplier")
        if self.cache_write_1h_tokens and "1h" not in multipliers:
            raise ValueError("Missing 1h cache write multiplier")
        cache_read_price = model_prices.get("cache_read", 0.0)
        billed = self.input_tokens * model_prices["input"] + self.cache_read_tokens * cache_read_price
        billed += self.cache_write_5m_tokens * model_prices["input"] * multipliers.get("5m", 0.0)
        billed += self.cache_write_1h_tokens * model_prices["input"] * multipliers.get("1h", 0.0)
        billed += self.output_tokens * model_prices["output"]
        return billed / 1_000_000

    def _model_prices(self, prices: dict[str, dict[str, float]]) -> dict[str, float]:
        if self.model not in prices:
            raise ValueError(f"No pricing configured for model {self.model!r}")
        model_prices = prices[self.model]
        cache_tokens = self.cache_read_tokens + self.cache_write_5m_tokens + self.cache_write_1h_tokens
        if cache_tokens and "cache_read" not in model_prices:
            raise ValueError(f"Missing cache_read price for model {self.model!r} with cache tokens")
        return model_prices

    def to_dict(self) -> dict:
        return asdict(self)


def _is_price(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def normalize_pricing(pricing: dict, models=None, require_cache_read: bool = False) -> dict[str, dict[str, float]]:
    """Validate prices and convert legacy [input, output] pairs to named rates."""
    if not isinstance(pricing, dict):
        raise ValueError("Pricing must be a mapping from model IDs to prices")

    normalized = {}
    for model, rates in pricing.items():
        if not isinstance(model, str) or not model:
            raise ValueError(f"Invalid pricing model ID: {model!r}")
        if isinstance(rates, (list, tuple)):
            if len(rates) != 2:
                raise ValueError(
                    f"Invalid pricing for model {model!r}: expected input and output prices"
                )
            rates = {"input": rates[0], "output": rates[1]}
        if not isinstance(rates, dict):
            raise ValueError(
                f"Invalid pricing for model {model!r}: expected a mapping with input and output prices"
            )
        required_rates = {"input", "output"}
        if require_cache_read:
            required_rates.add("cache_read")
        missing = required_rates - rates.keys()
        if missing:
            names = " and ".join(sorted(missing))
            raise ValueError(f"Invalid pricing for model {model!r}: missing {names} price")
        for name, price in rates.items():
            if not _is_price(price):
                raise ValueError(
                    f"Invalid pricing for model {model!r}: {name} price must be a non-negative number"
                )
        normalized[model] = dict(rates)

    if models is not None:
        model_ids = models.values() if isinstance(models, dict) else models
        for model in model_ids:
            if model not in normalized:
                raise ValueError(f"Missing pricing for configured model {model!r}")
    return normalized


def normalize_cache_write_multipliers(multipliers: dict | None, required: bool = False) -> dict[str, float]:
    """Validate the global cache write multipliers, allowing old run configs without them."""
    if multipliers is None:
        if required:
            raise ValueError("Missing cache_write_multipliers")
        return {}
    if not isinstance(multipliers, dict):
        raise ValueError("cache_write_multipliers must be a mapping with 5m and 1h rates")
    if any(not isinstance(ttl, str) for ttl in multipliers):
        raise ValueError("cache_write_multipliers keys must be 5m and 1h")
    expected = {"5m", "1h"}
    missing = expected - multipliers.keys()
    if missing:
        names = " and ".join(sorted(missing))
        raise ValueError(f"cache_write_multipliers is missing {names}")
    extra = multipliers.keys() - expected
    if extra:
        raise ValueError(f"Unknown cache_write_multipliers key(s): {', '.join(sorted(extra))}")
    for ttl, multiplier in multipliers.items():
        if not _is_price(multiplier):
            raise ValueError(f"cache_write_multipliers.{ttl} must be a non-negative number")
    return dict(multipliers)


def cost_of(usages: list[dict], pricing: dict) -> float:
    """Return list-price cost, counting cached input at the uncached input rate."""
    normalized = normalize_pricing(pricing)
    return sum(Usage(**usage).cost(normalized) for usage in usages)


def cost_totals(
    usages_by_category: dict[str, list[dict]], pricing: dict, cache_write_multipliers: dict | None = None
) -> dict:
    """Return list-price totals by category and the estimated billed total."""
    normalized = normalize_pricing(pricing)
    multipliers = normalize_cache_write_multipliers(cache_write_multipliers)
    usages = {category: [Usage(**usage) for usage in records] for category, records in usages_by_category.items()}
    list_price = {
        category: sum(usage.cost(normalized) for usage in records)
        for category, records in usages.items()
    }
    return {
        "list_price": list_price,
        "list_price_total": sum(list_price.values()),
        "estimated_billed_total": sum(
            usage.estimated_billed_cost(normalized, multipliers)
            for records in usages.values()
            for usage in records
        ),
    }


class LLM(Protocol):
    def complete(
        self, model: str, system: str, messages: list[dict], max_tokens: int = 4000, effort: str | None = None
    ) -> tuple[str, Usage]: ...

    def structured(
        self, model: str, system: str, prompt: str, schema: dict, max_tokens: int = 16000, effort: str | None = None
    ) -> tuple[dict, Usage]: ...


def _check_model(requested: str, served: str, usage: Usage) -> None:
    # Aliases can resolve to dated snapshots, so a prefix match is fine.
    if served and not served.startswith(requested):
        raise ModelError("served_model_mismatch", f"asked for {requested}, got {served}", usage)


class AnthropicLLM:
    """Real API calls. Reads ANTHROPIC_API_KEY from the environment."""

    def __init__(self, max_retries: int = 4, timeout_s: float = 300.0):
        import anthropic  # imported here so the mock works without the SDK

        # The SDK retries 429/5xx with backoff. The timeout is a hard ceiling
        # per request so a hung connection can't stall a worker forever.
        self.client = anthropic.Anthropic(max_retries=max_retries, timeout=timeout_s)

    def _call(self, model, system, messages, max_tokens, effort, fmt=None, cache_control=None):
        import anthropic

        output_config: dict = {}
        if effort:
            output_config["effort"] = effort
        if fmt:
            output_config["format"] = fmt
        start = time.perf_counter()
        try:
            resp = self.client.messages.create(
                model=model,
                system=system,
                messages=messages,
                max_tokens=max_tokens,
                **({"output_config": output_config} if output_config else {}),
                **({"cache_control": cache_control} if cache_control is not None else {}),
            )
        except anthropic.BadRequestError as e:
            if CREDIT_BALANCE_TOO_LOW_MARKER in e.message.lower():
                raise AccountError("api_credits", e.message) from e
            raise
        cache_creation = getattr(resp.usage, "cache_creation", None)
        if cache_creation is None:
            cache_write_5m_tokens = getattr(resp.usage, "cache_creation_input_tokens", None) or 0
            cache_write_1h_tokens = 0
            # Older usage responses expose only total creation tokens; these requests use a 5m TTL.
        else:
            cache_write_5m_tokens = getattr(cache_creation, "ephemeral_5m_input_tokens", None) or 0
            cache_write_1h_tokens = getattr(cache_creation, "ephemeral_1h_input_tokens", None) or 0
        usage = Usage(
            model=model,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            seconds=round(time.perf_counter() - start, 2),
            stop_reason=resp.stop_reason or "",
            served_model=resp.model,
            effort=effort or "",
            cache_read_tokens=getattr(resp.usage, "cache_read_input_tokens", None) or 0,
            cache_write_5m_tokens=cache_write_5m_tokens,
            cache_write_1h_tokens=cache_write_1h_tokens,
        )
        _check_model(model, resp.model, usage)
        if resp.stop_reason == "refusal":
            d = resp.stop_details
            why = f" (category: {d.category or 'none'}{f', {d.explanation}' if d.explanation else ''})" if d else ""
            raise ModelError("refusal", f"the model declined this request{why}", usage)
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        return text, usage

    def complete(self, model, system, messages, max_tokens=4000, effort=None):
        return self._call(model, system, messages, max_tokens, effort, cache_control={"type": "ephemeral"})

    def structured(self, model, system, prompt, schema, max_tokens=16000, effort=None):
        fmt = {"type": "json_schema", "schema": schema}
        # Single-shot grading calls do not reuse a prefix, so cache writes would add cost without a later read.
        text, usage = self._call(model, system, [{"role": "user", "content": prompt}], max_tokens, effort, fmt)
        if usage.stop_reason == "max_tokens":
            raise ModelError("truncated", "structured output hit max_tokens", usage)
        try:
            return json.loads(text), usage
        except json.JSONDecodeError as e:
            raise ModelError("bad_json", str(e), usage) from e


def _render_transcript(messages: list[dict]) -> str:
    lines = [
        "This is the conversation so far. Reply with only your next turn as the assistant side.",
        "Do not repeat the transcript or add a speaker label.",
        "",
    ]
    for message in messages:
        speaker = "Assistant" if message["role"] == "assistant" else "Other party"
        lines.append(f"{speaker}: {message['content']}")
    lines.extend(("", "Assistant:"))
    return "\n".join(lines)


def _error_excerpt(text: str) -> str:
    return " ".join(text.split())[:500] or "Claude CLI returned no error details"


def _fatal_cli_error(text: str) -> str | None:
    lowered = text.lower()
    if any(term in lowered for term in (
        "not logged in", "login required", "please log in", "authentication required",
        "authentication failed", "not authenticated", "please authenticate",
        "failed to authenticate", "unauthorized", "invalid api key",
    )):
        return "cli_auth"
    if any(term in lowered for term in (
        "usage limit", "rate limit", "rate_limit", "out of extra usage", "limit reached",
        CREDIT_BALANCE_TOO_LOW_MARKER,
    )):
        return "cli_usage_limit"
    return None


class ClaudeCodeLLM:
    """Run prompts through a logged-in Claude Code CLI subscription."""

    def __init__(self, timeout_s: float = 300.0):
        self.timeout_s = timeout_s

    def _call(self, model, system, prompt, effort, schema=None):
        command = ["claude", "-p", "--model", model]
        if effort is not None:
            command.extend(("--effort", effort))
        command.extend((
            "--system-prompt", system,
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--no-session-persistence",
            "--output-format", "json",
        ))
        if schema is not None:
            command.extend(("--json-schema", json.dumps(schema, ensure_ascii=False, separators=(",", ":"))))
        command.append(prompt)

        start = time.perf_counter()
        # Keep API credentials from overriding Claude Code's subscription login.
        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        try:
            with tempfile.TemporaryDirectory(prefix="scio-claude-") as cwd:
                completed = subprocess.run(
                    command,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_s,
                    check=False,
                    env=env,
                )
        except subprocess.TimeoutExpired as e:
            raise ModelError("timeout", f"Claude CLI timed out after {self.timeout_s:g}s") from e
        elapsed = round(time.perf_counter() - start, 2)
        output = completed.stdout.strip()
        detail = "\n".join(part for part in (completed.stderr.strip(), output) if part)
        if completed.returncode != 0:
            fatal_kind = _fatal_cli_error(detail)
            if fatal_kind:
                raise AccountError(fatal_kind, _error_excerpt(detail))
            raise ModelError("cli_error", _error_excerpt(detail))
        try:
            response = json.loads(output)
        except json.JSONDecodeError as e:
            fatal_kind = _fatal_cli_error(detail)
            if fatal_kind:
                raise AccountError(fatal_kind, _error_excerpt(detail)) from e
            raise ModelError("cli_error", f"Claude CLI returned invalid JSON: {_error_excerpt(detail)}") from e
        if not isinstance(response, dict):
            fatal_kind = _fatal_cli_error(detail)
            if fatal_kind:
                raise AccountError(fatal_kind, _error_excerpt(detail))
            raise ModelError("cli_error", "Claude CLI JSON response was not an object")
        if response.get("is_error"):
            status = response.get("api_error_status")
            error_text = "\n".join(filter(None, (
                str(response.get("subtype", "")), str(status or ""), str(response.get("result", "")), detail,
            )))
            fatal_kind = _fatal_cli_error(error_text)
            if not fatal_kind and status in (401, 403, "401", "403"):
                fatal_kind = "cli_auth"
            if not fatal_kind and status in (429, "429"):
                fatal_kind = "cli_usage_limit"
            if fatal_kind:
                raise AccountError(fatal_kind, _error_excerpt(error_text))
            raise ModelError("cli_error", _error_excerpt(error_text))

        usage_data = response.get("usage") or {}
        model_usage = response.get("modelUsage") or {}
        model_entry = model_usage.get(model)
        if model_entry is None and model_usage:
            model_entry = next(iter(model_usage.values()))
        served_model = (model_entry or {}).get("canonicalModel", "")
        usage = Usage(
            model=model,
            input_tokens=(usage_data.get("input_tokens", 0)
                          + usage_data.get("cache_creation_input_tokens", 0)
                          + usage_data.get("cache_read_input_tokens", 0)),
            output_tokens=usage_data.get("output_tokens", 0),
            seconds=elapsed,
            stop_reason=response.get("stop_reason") or "",
            served_model=served_model,
            effort=effort or "",
        )
        _check_model(model, served_model, usage)
        if usage.stop_reason == "refusal":
            raise ModelError("refusal", "the model declined this request", usage)
        return response, usage

    # Claude Code does not expose a max_tokens option for either call type.
    def complete(self, model, system, messages, max_tokens=4000, effort=None):
        response, usage = self._call(model, system, _render_transcript(messages), effort)
        return str(response.get("result", "")).strip(), usage

    def structured(self, model, system, prompt, schema, max_tokens=16000, effort=None):
        response, usage = self._call(model, system, prompt, effort, schema)
        if usage.stop_reason == "max_tokens":
            raise ModelError("truncated", "structured output hit max_tokens", usage)
        usage.stop_reason = "end_turn" if usage.stop_reason == "tool_use" else usage.stop_reason
        result = response.get("structured_output")
        if result is None:
            try:
                result = json.loads(response.get("result", ""))
            except (json.JSONDecodeError, TypeError) as e:
                raise ModelError("bad_json", str(e), usage) from e
        if not isinstance(result, dict):
            raise ModelError("bad_json", "structured output was not a JSON object", usage)
        return result, usage


class MockLLM:
    """Offline stand-in for testing the pipeline without spending credits.

    The output is deterministic for a given input, so tests are stable. It is
    obviously not a real conversation: use it to check plumbing, not results.
    """

    BUYER_LINES = [
        "Okay. Go on, but keep it short.",
        "That's a fair question. Honestly, what worries me is governance.",
        "I'd need to see that working on our own data before I believe it.",
        "Alright, send me something and we'll look at a pilot.",
    ]
    SELLER_LINES = [
        "Thanks for the time. Before I talk about us, what made you take this call?",
        "That makes sense. What has gotten in the way of AI projects so far?",
        "I don't want to guess on that, I'll confirm with our team and follow up.",
        "Would a two-week pilot on twenty of your real examples be a useful next step?",
    ]

    def _seed(self, *parts: Any) -> random.Random:
        digest = hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()
        return random.Random(int(digest[:16], 16))

    def complete(self, model, system, messages, max_tokens=4000, effort=None):
        is_buyer = "role-playing a buyer" in system
        turn = sum(1 for m in messages if m["role"] == "assistant")
        lines = self.BUYER_LINES if is_buyer else self.SELLER_LINES
        text = lines[min(turn, len(lines) - 1)]
        if is_buyer and turn >= len(self.BUYER_LINES) - 1:
            text += "\n[END_CALL]"
        in_tokens = (len(system) + sum(len(str(m["content"])) for m in messages)) // 4
        return text, Usage(model, in_tokens, len(text) // 4, 0.01, "end_turn", model, effort or "")

    def structured(self, model, system, prompt, schema, max_tokens=16000, effort=None):
        rng = self._seed(model, prompt)
        data = _fake_from_schema(schema, rng)
        return data, Usage(model, len(prompt) // 4, 200, 0.01, "end_turn", model, effort or "")


def _fake_from_schema(schema: dict, rng: random.Random) -> Any:
    if "enum" in schema:
        return rng.choice(schema["enum"])
    kind = schema.get("type")
    if kind == "object":
        return {k: _fake_from_schema(v, rng) for k, v in schema.get("properties", {}).items()}
    if kind == "array":
        return []
    if kind == "boolean":
        return rng.random() < 0.6
    if kind == "integer":
        return rng.randint(1, 5)
    return "mock"


def make_llm(mock: bool, backend: str) -> LLM:
    if backend not in BACKENDS:
        raise ValueError(f"Unknown backend {backend!r}. Use one of: {', '.join(BACKENDS)}")
    if mock:
        return MockLLM()
    return AnthropicLLM() if backend == DEFAULT_BACKEND else ClaudeCodeLLM()
