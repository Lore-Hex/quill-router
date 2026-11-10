"""Classify explicit AI source signals; these are referrals, never citation counts."""
from __future__ import annotations

AI_SOURCES = {
    "chatgpt": "chatgpt", "chatgpt.com": "chatgpt", "chat.openai.com": "chatgpt",
    "openai": "openai", "claude": "claude", "claude.ai": "claude", "claude.com": "claude",
    "perplexity": "perplexity", "perplexity.ai": "perplexity",
    "gemini": "gemini", "gemini.google.com": "gemini",
    "copilot": "copilot", "copilot.microsoft.com": "copilot", "copilot.com": "copilot",
}


def ai_referral_platform(source: object) -> str | None:
    if not isinstance(source, str):
        return None
    value = source.strip().lower().removeprefix("www.")
    return AI_SOURCES.get(value)
