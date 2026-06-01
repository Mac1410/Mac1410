"""
telegram_alert.py — Minimal Telegram sender.

Reads credentials from the environment:
    TELEGRAM_BOT_TOKEN   — from @BotFather
    TELEGRAM_CHAT_ID     — your chat/channel id

send_telegram() returns (ok: bool, detail: str) so callers can surface failures
in the UI instead of swallowing them.
"""

from __future__ import annotations

import os

import requests

API_TEMPLATE = "https://api.telegram.org/bot{token}/sendMessage"
_TG_LIMIT = 4096  # Telegram hard cap per message


def is_configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def send_telegram(text: str, *, disable_preview: bool = True) -> tuple[bool, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set."

    text = text.strip()[:_TG_LIMIT]
    try:
        resp = requests.post(
            API_TEMPLATE.format(token=token),
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": disable_preview,
            },
            timeout=15,
        )
        if resp.status_code == 200 and resp.json().get("ok"):
            return True, "sent"
        return False, f"Telegram API error {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:  # network, timeout, etc.
        return False, f"Telegram request failed: {exc}"
