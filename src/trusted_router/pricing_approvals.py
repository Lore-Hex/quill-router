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
    )
