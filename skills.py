"""
skills.py — Loader for the repo's analysis skills (skills/<name>/SKILL.md).

The skills describe TradingView analysis methodology (which indicators to read,
how to form a bias, how to report). We inject their *reasoning* into the model's
system prompt so every analysis — morning brief, market scan, BTC bot — follows
the same disciplined workflow.
"""

from __future__ import annotations

import os

SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")


def _strip_frontmatter(text: str) -> str:
    text = text.lstrip()
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2].strip()
    return text.strip()


def load_skill(name: str) -> str:
    """Return a skill's markdown body (frontmatter stripped), or '' if missing."""
    path = os.path.join(SKILLS_DIR, name, "SKILL.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _strip_frontmatter(f.read())
    except FileNotFoundError:
        return ""


def list_skills() -> list[str]:
    if not os.path.isdir(SKILLS_DIR):
        return []
    return sorted(
        d for d in os.listdir(SKILLS_DIR)
        if os.path.exists(os.path.join(SKILLS_DIR, d, "SKILL.md"))
    )


def methodology(names: list[str] | None = None) -> str:
    """
    Concatenate the requested skills into a single methodology block for the
    system prompt. Defaults to the two most relevant for ongoing analysis.
    """
    names = names or ["chart-analysis", "multi-symbol-scan"]
    blocks = []
    for n in names:
        body = load_skill(n)
        if body:
            blocks.append(f"### Skill: {n}\n{body}")
    if not blocks:
        return ""
    return (
        "=== ANALYSIS METHODOLOGY (from project skills) ===\n"
        "Apply the *analytical reasoning* below to every analysis — which "
        "indicators to read (RSI, EMA, MACD, Bollinger Bands, Volume, VWAP), how "
        "to identify support/resistance, and how to state a bias with reasoning. "
        "The tool names in the skills (chart_set_symbol, data_get_ohlcv, etc.) are "
        "for reference only — you are given the live readings directly; do not try "
        "to call them.\n\n"
        + "\n\n".join(blocks)
        + "\n=== END METHODOLOGY ==="
    )
