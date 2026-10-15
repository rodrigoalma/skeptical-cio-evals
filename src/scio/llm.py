"""Thin wrapper around the Anthropic Messages API, plus an offline mock.

Everything else in the project talks to an `LLM` object with two methods:

    complete(...)    -> plain text reply
    structured(...)  -> dict that matches a JSON schema (via forced tool use)

Both return a `Usage` record so we can track tokens, cost and latency.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol


@dataclass
class Usage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0

    def cost(self, pricing: dict[str, list[float]]) -> float:
        rate_in, rate_out = pricing.get(self.model, [0.0, 0.0])
        return (self.input_tokens * rate_in + self.output_tokens * rate_out) / 1_000_000

    def to_dict(self) -> dict:
        return asdict(self)


class LLM(Protocol):
    def complete(
        self,
        model: str,
        system: str,
        messages: list[dict],
        max_tokens: int = 600,
        temperature: float = 1.0,
    ) -> tuple[str, Usage]: ...

    def structured(
        self,
        model: str,
        system: str,
        prompt: str,
        tool_name: str,
        schema: dict,
        max_tokens: int = 2000,
    ) -> tuple[dict, Usage]: ...


class AnthropicLLM:
    """Real API calls. Reads ANTHROPIC_API_KEY from the environment."""

    def __init__(self, max_retries: int = 4):
        import anthropic  # imported here so the mock works without the SDK

        self.client = anthropic.Anthropic(max_retries=max_retries)

    def complete(self, model, system, messages, max_tokens=600, temperature=1.0):
        start = time.perf_counter()
        resp = self.client.messages.create(
            model=model,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        elapsed = time.perf_counter() - start
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        usage = Usage(model, resp.usage.input_tokens, resp.usage.output_tokens, elapsed)
        return text, usage

    def structured(self, model, system, prompt, tool_name, schema, max_tokens=2000):
        # Forcing a tool call is the simplest reliable way to get JSON that
        # matches a schema out of the Messages API.
        start = time.perf_counter()
        resp = self.client.messages.create(
            model=model,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.0,
            tools=[{"name": tool_name, "description": f"Record the {tool_name} result.", "input_schema": schema}],
            tool_choice={"type": "tool", "name": tool_name},
        )
        elapsed = time.perf_counter() - start
        block = next(b for b in resp.content if b.type == "tool_use")
        usage = Usage(model, resp.usage.input_tokens, resp.usage.output_tokens, elapsed)
        return dict(block.input), usage


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

    def complete(self, model, system, messages, max_tokens=600, temperature=1.0):
        is_buyer = "role-playing a buyer" in system
        turn = sum(1 for m in messages if m["role"] == "assistant")
        lines = self.BUYER_LINES if is_buyer else self.SELLER_LINES
        text = lines[min(turn, len(lines) - 1)]
        if is_buyer and turn >= len(self.BUYER_LINES) - 1:
            text += "\n[END_CALL]"
        in_tokens = (len(system) + sum(len(str(m["content"])) for m in messages)) // 4
        return text, Usage(model, in_tokens, len(text) // 4, 0.01)

    def structured(self, model, system, prompt, tool_name, schema, max_tokens=2000):
        rng = self._seed(model, prompt, tool_name)
        data = _fake_from_schema(schema, rng)
        return data, Usage(model, len(prompt) // 4, 200, 0.01)


def _fake_from_schema(schema: dict, rng: random.Random) -> Any:
    kind = schema.get("type")
    if kind == "object":
        return {k: _fake_from_schema(v, rng) for k, v in schema.get("properties", {}).items()}
    if kind == "array":
        return []
    if kind == "boolean":
        return rng.random() < 0.6
    if kind == "integer":
        return rng.randint(schema.get("minimum", 0), schema.get("maximum", 5))
    if kind == "number":
        return round(rng.uniform(schema.get("minimum", 0), schema.get("maximum", 1)), 2)
    if "enum" in schema:
        return rng.choice(schema["enum"])
    return "mock"


def make_llm(mock: bool) -> LLM:
    return MockLLM() if mock else AnthropicLLM()
