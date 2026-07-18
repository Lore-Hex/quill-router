from __future__ import annotations


def canonical_byok_provider(provider: str) -> str:
    """Return the public provider slug used for new BYOK configuration."""

    slug = provider.strip().lower().replace("_", "-").replace(" ", "-")
    if slug in {"gemini", "google", "google-ai", "ai-studio"}:
        return "google-ai-studio"
    return slug


def byok_storage_provider_candidates(provider: str) -> tuple[str, ...]:
    """Storage keys to try, newest first, for a public provider slug.

    Gemini BYOK envelopes created before the Google provider split are bound to
    `gemini` in their KMS AAD. They cannot be renamed in place, so reads retain a
    narrow fallback until each workspace rotates its key.
    """

    canonical = canonical_byok_provider(provider)
    if canonical == "google-ai-studio":
        return (canonical, "gemini")
    return (canonical,)
