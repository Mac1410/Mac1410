"""
prompts.py — System prompt construction.

Design for prompt caching: the *stable* block (advisor persona + your
philosophy.md) goes first with a cache breakpoint; the *volatile* block
(live portfolio, prices, date) goes in a second system block after the
breakpoint so the cached prefix stays byte-identical across calls.
"""

from __future__ import annotations

import os
from datetime import date

import skills

PHILOSOPHY_PATH = os.path.join(os.path.dirname(__file__), "philosophy.md")

# ----------------------------------------------------------------------------
# Stable persona — McKinsey-grade investment analyst.
# ----------------------------------------------------------------------------
_PERSONA = """\
You are a senior investment strategist — the kind of analyst a top-tier firm \
(think McKinsey, Goldman, Bridgewater) would put in front of a serious private \
client. You are rigorous, structured, and intellectually honest.

How you operate:
- Lead with the answer. State your view in the first sentence, then support it.
- Think in frameworks: thesis → evidence → risks → what would change your mind.
- Quantify. Use the live numbers you are given; never invent prices or figures.
- Separate signal from noise. Call out what actually matters for this portfolio.
- Be candid about uncertainty and downside. Name the bear case explicitly.
- Respect the client's stated philosophy below — it is the lens for every call.

Hard boundaries (do not cross):
- You are an ANALYSIS and ADVISORY assistant. You do NOT place trades, move \
money, or execute orders. When the client should act, tell them what to consider \
and let THEM execute.
- Nothing you say is a guarantee. Frame recommendations as reasoned views, with \
risk. Remind the client to size positions to their own risk tolerance.
- No hype, no FOMO, no "to the moon." If something looks like a bad idea, say so.

Formatting: tight markdown. Short paragraphs, decisive headers, tables when \
comparing. No filler, no preamble like "Certainly" — open with the substance.
"""

_DEFAULT_PHILOSOPHY = (
    "(No philosophy.md content provided yet. Apply prudent, risk-aware, "
    "long-horizon principles until the client specifies their own.)"
)


def load_philosophy() -> str:
    try:
        with open(PHILOSOPHY_PATH, "r", encoding="utf-8") as f:
            text = f.read().strip()
        return text or _DEFAULT_PHILOSOPHY
    except FileNotFoundError:
        return _DEFAULT_PHILOSOPHY


def stable_system_block() -> str:
    """The cacheable prefix: persona + investment philosophy + skill methodology."""
    block = (
        f"{_PERSONA}\n\n"
        "=== CLIENT INVESTMENT PHILOSOPHY (authoritative) ===\n"
        f"{load_philosophy()}\n"
        "=== END PHILOSOPHY ==="
    )
    method = skills.methodology()
    if method:
        block += "\n\n" + method
    return block


def context_block(prices_markdown: str, extra: str | None = None) -> str:
    """The volatile suffix: today's date + live portfolio snapshot."""
    parts = [
        f"=== LIVE CONTEXT (as of {date.today().isoformat()}) ===",
        "Current portfolio and market prices:",
        "",
        prices_markdown,
    ]
    if extra:
        parts += ["", extra]
    parts.append("=== END LIVE CONTEXT ===")
    return "\n".join(parts)


# ----------------------------------------------------------------------------
# Task instructions appended to the user turn for one-shot generations.
# ----------------------------------------------------------------------------

MORNING_BRIEF_TASK = """\
Write today's MORNING BRIEF for the client. Structure:

1. **Bottom line** — one or two sentences: what matters most for this portfolio today.
2. **Portfolio pulse** — notable moves in held positions and what's driving them.
3. **What to watch** — 2-4 catalysts/levels relevant to these holdings.
4. **Action items** — concrete things the client should consider (not execute for them).

Keep it punchy and skimmable — this is read with morning coffee. Ground every claim \
in the live numbers above and the client's philosophy."""

MARKET_SCAN_TASK = """\
Run a MARKET SCAN focused on this portfolio and its adjacencies. Structure:

1. **Regime read** — risk-on / risk-off / mixed, and why, in 2-3 sentences.
2. **Position-by-position** — for each meaningful holding: thesis status, key risk, \
and whether the philosophy says add / hold / trim.
3. **Opportunities & threats** — sectors/assets aligned with the philosophy worth a look, \
and concentration or correlation risks in the current book.
4. **One question** — the single most important question the client should be asking now.

Be decisive and quantify against the live prices above."""

TELEGRAM_BRIEF_TASK = """\
Write an ULTRA-CONCISE morning update for delivery over Telegram (plain text, no \
markdown tables, under ~1200 characters). Cover: overall portfolio stance in one line, \
the 1-2 most important moves/risks today, and one thing to watch. Use short lines. \
No greetings, no sign-off."""
