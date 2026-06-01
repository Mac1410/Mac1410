"""
analyzer.py — Claude model wrapper.

Exposes four functions the dashboard uses:
    morning_brief()            -> str
    market_scan()              -> str
    chat(message, history=...) -> generator of text chunks (streaming)
    telegram_morning_update()  -> (ok, detail)

Model: claude-opus-4-8 with adaptive thinking. The stable system block
(persona + philosophy) is cached; the volatile block (live prices/portfolio)
is sent as a second, uncached system block so the cached prefix stays stable.
"""

from __future__ import annotations

from typing import Iterator

import anthropic

import memory
import prompts
from prices import format_prices_for_prompt, price_holdings
from telegram_alert import send_telegram

MODEL = "claude-opus-4-8"
_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        # Resolves ANTHROPIC_API_KEY from the environment.
        _client = anthropic.Anthropic()
    return _client


def _build_system(extra_context: str | None = None) -> list[dict]:
    """Two system blocks: cached persona+philosophy, then live context."""
    valuation = price_holdings(memory.get_holdings())
    prices_md = format_prices_for_prompt(valuation)
    return [
        {
            "type": "text",
            "text": prompts.stable_system_block(),
            "cache_control": {"type": "ephemeral"},  # cache breakpoint
        },
        {
            "type": "text",
            "text": prompts.context_block(prices_md, extra=extra_context),
        },
    ]


def _one_shot(task: str, *, max_tokens: int = 4000) -> str:
    """Non-streaming generation for briefs/scans (streamed under the hood)."""
    client = _get_client()
    text_parts: list[str] = []
    with client.messages.stream(
        model=MODEL,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system=_build_system(),
        messages=[{"role": "user", "content": task}],
    ) as stream:
        msg = stream.get_final_message()
    for block in msg.content:
        if block.type == "text":
            text_parts.append(block.text)
    return "".join(text_parts).strip()


def morning_brief() -> str:
    return _one_shot(prompts.MORNING_BRIEF_TASK)


def market_scan() -> str:
    return _one_shot(prompts.MARKET_SCAN_TASK, max_tokens=5000)


def chat(message: str, history: list[dict] | None = None) -> Iterator[str]:
    """
    Stream a chat reply. Persists both turns to memory.

    Yields text chunks; the caller is responsible for accumulating them.
    """
    history = history if history is not None else memory.get_recent_messages(limit=20)
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    messages.append({"role": "user", "content": message})

    memory.add_message("user", message)

    client = _get_client()
    collected: list[str] = []
    with client.messages.stream(
        model=MODEL,
        max_tokens=4000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system=_build_system(),
        messages=messages,
    ) as stream:
        for text in stream.text_stream:
            collected.append(text)
            yield text

    memory.add_message("assistant", "".join(collected).strip())


def telegram_morning_update() -> tuple[bool, str]:
    """Generate a concise brief and push it to Telegram."""
    brief = _one_shot(prompts.TELEGRAM_BRIEF_TASK, max_tokens=1500)
    if not brief:
        return False, "Model returned no content."
    return send_telegram(brief)
