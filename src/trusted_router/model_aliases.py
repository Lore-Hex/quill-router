"""Explicit router-brand and upstream catalog aliases; no fuzzy model matching."""

_OPENROUTER_MODEL_ID_ALIASES = {
    # Published from Mistral's native catalog before OpenRouter listed it.
    "mistralai/mistral-large-4-0": "mistralai/mistral-large-4",
}


def canonical_openrouter_model_id(model_id: str) -> str:
    """Keep established public IDs when OpenRouter uses a different spelling.

    Apply only to catalog model IDs, never endpoint-native request IDs.
    """
    return _OPENROUTER_MODEL_ID_ALIASES.get(model_id, model_id)


def canonical_router_model_id(model_id: str) -> str:
    if model_id.startswith("nyte/"):
        model_id = "trustedrouter/" + model_id.removeprefix("nyte/")
    base, separator, variant = model_id.partition(":")
    if base == "trustedrouter/auto-routing":
        return "trustedrouter/auto" + separator + variant
    return model_id


def router_model_aliases(model_id: str) -> list[str]:
    if not model_id.startswith("trustedrouter/"):
        return []
    aliases = ["nyte/" + model_id.removeprefix("trustedrouter/")]
    if model_id == "trustedrouter/auto":
        aliases.extend(("trustedrouter/auto-routing", "nyte/auto-routing"))
    return aliases
