"""Public projections of existing customer token tariffs; no pricing math."""

from typing import Any

from trusted_router.catalog_data import Model, ModelEndpoint
from trusted_router.money import microdollars_per_million_tokens_to_token_decimal
from trusted_router.pricing import PriceTier
from trusted_router.provider_contracts import provider_model_has_private_price_tier_basis


def public_token_price_tiers(route: Model | ModelEndpoint, model: Model) -> tuple[PriceTier, ...]:
    # Settlement uses price_tiers. Do not advertise a divergent publication
    # tariff, or add a new field to the common, single-tier representation.
    tiers = route.price_tiers
    if len(tiers) < 2 or tiers != route.published_price_tiers:
        return ()
    from trusted_router.speech import SPEECH_MODELS
    from trusted_router.transcription import TRANSCRIPTION_MODELS
    from trusted_router.web_fetch import FETCH_MODEL_ID

    if (
        model.supports_video or model.supports_rerank
        or "image" in model.output_modalities
        or model.id in SPEECH_MODELS or model.id in TRANSCRIPTION_MODELS
        or model.id == FETCH_MODEL_ID
    ):
        return ()
    # A provider-private context basis cannot be described as total prompt
    # tokens. Keep that route out of this disclosure rather than mislabel it.
    if provider_model_has_private_price_tier_basis(route.provider, model.id):
        return ()
    return tiers


def token_price_tiers_payload(tiers: tuple[PriceTier, ...]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for tier in tiers:
        row: dict[str, Any] = {
            "max_prompt_tokens": tier.max_prompt_tokens,
            "prompt": microdollars_per_million_tokens_to_token_decimal(
                tier.prompt_price_microdollars_per_million_tokens
            ),
            "completion": microdollars_per_million_tokens_to_token_decimal(
                tier.completion_price_microdollars_per_million_tokens
            ),
        }
        cached = tier.prompt_cached_price_microdollars_per_million_tokens
        if cached is not None:
            row["input_cache_read"] = microdollars_per_million_tokens_to_token_decimal(cached)
        result.append(row)
    return result
