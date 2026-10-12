"""Reviewed, dated price transitions shared by every pricing spike gate."""

from datetime import UTC, date, datetime
from decimal import Decimal

# Google's Standard TTS tariff: https://ai.google.dev/gemini-api/docs/pricing
# Rates are microdollars per million tokens, ordered (input, output). Match
# the complete route and both complete pairs, never individual dimensions.
_GEMINI_TTS_EFFECTIVE_ON = date(2027, 1, 1)
_GEMINI_TTS_TRANSITIONS = {
    "google/gemini-3.8-flash-tts [google-ai-studio:google-ai-studio:gemini-3.8-flash-tts]": (
        (500_000, 9_000_000), (1_000_000, 18_000_000),
    ),
    "google/gemini-3.8-flash-lite-tts [google-ai-studio:google-ai-studio:gemini-3.8-flash-lite-tts]": (
        (500_000, 6_000_000), (1_000_000, 12_000_000),
    ),
}

# https://decisionmodels.io/pricing, verified 2026-10-12: existing accounts
# retain the old Pro rate until November 12. Scope approval to the full pair,
# both regional identities, and the native alias/version actually tested.
_DECISION_PRO_EFFECTIVE_ON = date(2026, 11, 12)
_DECISION_PRO_TRANSITIONS = {
    f"{slug}/{model} [{slug}:{slug}:{model}]": ((before, 0), (after, 0))
    for slug, before, after in (
        ("system1models", 38_000, 80_000),
        ("system1models-eu", 40_000, 84_000),
    )
    for model in ("s1-pro", "s1-pro-2")
}


def approved_dated_price_transition(
    route: str,
    before: tuple[int | Decimal, int | Decimal],
    after: tuple[int | Decimal, int | Decimal],
    *,
    today: date | None = None,
) -> bool:
    """Approve only the reviewed full tariff from its UTC cutover onward.

    There is no upper date bound: a refresh that missed the cutover must still
    catch up. A subsequent doubling or partial transition remains unapproved.
    """
    today = today or datetime.now(UTC).date()
    return (
        today >= _GEMINI_TTS_EFFECTIVE_ON
        and (before, after) == _GEMINI_TTS_TRANSITIONS.get(route)
    ) or (
        today >= _DECISION_PRO_EFFECTIVE_ON
        and (before, after) == _DECISION_PRO_TRANSITIONS.get(route)
    )
