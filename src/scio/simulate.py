"""Run one simulated discovery call between a seller model and a buyer model."""

from __future__ import annotations

import re
import time

from . import loaders
from .llm import LLM, Usage

END_TOKEN = "[END_CALL]"


def _strip_end(text: str) -> tuple[str, bool]:
    ended = END_TOKEN in text
    return re.sub(re.escape(END_TOKEN), "", text).strip(), ended


def _view(turns: list[dict], me: str) -> list[dict]:
    """Turn the shared transcript into a message list from one side's view.

    The Messages API needs alternating user/assistant turns that start with a
    user turn. For each side, its own lines are "assistant" and the other
    side's lines are "user".
    """
    messages = []
    for t in turns:
        role = "assistant" if t["speaker"] == me else "user"
        messages.append({"role": role, "content": t["text"]})
    if messages and messages[0]["role"] == "assistant":
        messages.insert(0, {"role": "user", "content": "(The call connects. You speak first.)"})
    return messages


def run_call(
    llm: LLM,
    persona: dict,
    seller_prompt: str,
    seller_model: str,
    buyer_model: str,
    max_seller_turns: int = 8,
    max_tokens: int = 600,
    seller_temperature: float = 1.0,
    buyer_temperature: float = 1.0,
) -> dict:
    seller_system = loaders.render(
        f"seller_{seller_prompt}",
        fact_sheet=loaders.fact_sheet(),
        account_brief=loaders.account_brief(persona),
    )
    buyer_system = loaders.render(
        "buyer",
        persona_public=loaders.as_yaml(persona["public"]),
        persona_hidden=loaders.as_yaml(persona["hidden"]),
    )

    # The buyer always opens with their scripted line, so every seller gets
    # the same starting point for a given persona.
    turns = [{"speaker": "buyer", "text": persona["hidden"]["opening_line"].strip()}]
    usage: list[Usage] = []
    ended_by = "max_turns"
    started = time.time()

    for _ in range(max_seller_turns):
        text, u = llm.complete(
            seller_model, seller_system, _view(turns, "seller"), max_tokens, seller_temperature
        )
        usage.append(u)
        turns.append({"speaker": "seller", "text": text, "seconds": round(u.seconds, 2)})

        text, u = llm.complete(
            buyer_model, buyer_system, _view(turns, "buyer"), max_tokens, buyer_temperature
        )
        usage.append(u)
        text, ended = _strip_end(text)
        if text:
            turns.append({"speaker": "buyer", "text": text})
        if ended:
            ended_by = "buyer"
            break

    return {
        "persona": persona["id"],
        "difficulty": persona.get("difficulty"),
        "seller_prompt": seller_prompt,
        "seller_model": seller_model,
        "buyer_model": buyer_model,
        "turns": turns,
        "ended_by": ended_by,
        "wall_seconds": round(time.time() - started, 1),
        "usage": {
            "seller": [u.to_dict() for u, t in zip(usage, _roles(len(usage))) if t == "seller"],
            "buyer": [u.to_dict() for u, t in zip(usage, _roles(len(usage))) if t == "buyer"],
        },
    }


def _roles(n: int) -> list[str]:
    return ["seller" if i % 2 == 0 else "buyer" for i in range(n)]


def format_transcript(turns: list[dict]) -> str:
    """Plain text transcript for the judge and the debrief."""
    names = {"seller": "SELLER", "buyer": "BUYER"}
    return "\n\n".join(f"{names[t['speaker']]}: {t['text']}" for t in turns)
