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
import random
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol


class ModelError(RuntimeError):
    """A call that came back but can't be used: refusal, wrong model, bad JSON."""

    def __init__(self, kind: str, message: str, usage: "Usage | None" = None):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.usage = usage


@dataclass
class Usage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    stop_reason: str = ""
    served_model: str = ""

    def cost(self, pricing: dict[str, list[float]]) -> float:
        rate_in, rate_out = pricing.get(self.model, [0.0, 0.0])
        return (self.input_tokens * rate_in + self.output_tokens * rate_out) / 1_000_000

    def to_dict(self) -> dict:
        return asdict(self)


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

    def _call(self, model, system, messages, max_tokens, effort, fmt=None):
        output_config: dict = {}
        if effort:
            output_config["effort"] = effort
        if fmt:
            output_config["format"] = fmt
        start = time.perf_counter()
        resp = self.client.messages.create(
            model=model,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            **({"output_config": output_config} if output_config else {}),
        )
        usage = Usage(
            model=model,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            seconds=round(time.perf_counter() - start, 2),
            stop_reason=resp.stop_reason or "",
            served_model=resp.model,
        )
        _check_model(model, resp.model, usage)
        if resp.stop_reason == "refusal":
            raise ModelError("refusal", "the model declined this request", usage)
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        return text, usage

    def complete(self, model, system, messages, max_tokens=4000, effort=None):
        return self._call(model, system, messages, max_tokens, effort)

    def structured(self, model, system, prompt, schema, max_tokens=16000, effort=None):
        fmt = {"type": "json_schema", "schema": schema}
        text, usage = self._call(model, system, [{"role": "user", "content": prompt}], max_tokens, effort, fmt)
        if usage.stop_reason == "max_tokens":
            raise ModelError("truncated", "structured output hit max_tokens", usage)
        try:
            return json.loads(text), usage
        except json.JSONDecodeError as e:
            raise ModelError("bad_json", str(e), usage) from e


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
        return text, Usage(model, in_tokens, len(text) // 4, 0.01, "end_turn", model)

    def structured(self, model, system, prompt, schema, max_tokens=16000, effort=None):
        rng = self._seed(model, prompt)
        data = _fake_from_schema(schema, rng)
        return data, Usage(model, len(prompt) // 4, 200, 0.01, "end_turn", model)


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


def make_llm(mock: bool) -> LLM:
    return MockLLM() if mock else AnthropicLLM()
