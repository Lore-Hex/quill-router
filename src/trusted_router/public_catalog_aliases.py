"""Public-page aliases shared by redirects and links in historical reports."""

from collections.abc import Mapping

LEGACY_MODEL_ID_ALIASES: dict[str, str] = {
    "meta-llama/llama-4-scout": "meta-llama/llama-4-scout-17b-16e-instruct",
    "mistralai/mistral-small-3.2-24b-instruct": "mistralai/mistral-small-3.2-24b-instruct-2506",
    "lightning-ai/nemotron-3-nano-omni-30b-a3b-reasoning": (
        "nvidia/nemotron-3-nano-omni-reasoning-30b-a3b"
    ),
    "nvidia/nemotron-120b-a12b": "nvidia/nemotron-3-120b-a12b",
    "nvidia/nvidia-nemotron-3-ultra-550b-a55b": "nvidia/nemotron-3-ultra-550b-a55b",
    "xiaomi/mimo-v2-flash": "xiaomimimo/mimo-v2-flash",
    "zai-org/glm-4.5": "z-ai/glm-4.5",
}
LEGACY_PROVIDER_PAGE_ALIASES = {"gemini": "google-ai-studio"}


def canonical_public_provider_slug(provider_slug: str, providers: Mapping[str, object]) -> str:
    if provider_slug in providers:
        return provider_slug
    target = LEGACY_PROVIDER_PAGE_ALIASES.get(provider_slug.casefold())
    return target if target is not None and target in providers else provider_slug


def canonical_public_model_id(model_id: str, models: Mapping[str, object]) -> str:
    """Resolve known aliases and unambiguous casing to existing catalog IDs."""
    # Exact native IDs remain authoritative, including mixed-case provider IDs.
    if model_id in models:
        return model_id
    alias = LEGACY_MODEL_ID_ALIASES.get(model_id.casefold())
    if alias is not None and alias in models:
        return alias
    matches = [candidate for candidate in models if candidate.casefold() == model_id.casefold()]
    return matches[0] if len(matches) == 1 else model_id
