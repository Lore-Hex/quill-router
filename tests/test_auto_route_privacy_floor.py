"""`trustedrouter/auto` must not be quietly worse than the aliases it replaces.

`auto` is the route most traffic takes, including every caller who never chose
a model. These tests pin the two properties that make that default defensible,
so a future edit to the ladder cannot silently drop them.
"""

from __future__ import annotations

from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, endpoint_privacy_tier
from trusted_router.catalog_data import (
    DEFAULT_AUTO_MODEL_ORDER,
    PRIVACY_TIER_ZERO_RETENTION,
    US_FOCUSED_PROVIDER_ORDER,
)
from trusted_router.routing_candidates import auto_candidate_models


def _us_zdr_providers(model_id: str) -> set[str]:
    """Providers serving `model_id` that are US-hosted AND clear ZDR."""
    return {
        endpoint.provider
        for endpoint in MODEL_ENDPOINTS.values()
        if endpoint.model_id == model_id
        and endpoint.provider in US_FOCUSED_PROVIDER_ORDER
        and endpoint_privacy_tier(endpoint) >= PRIVACY_TIER_ZERO_RETENTION
    }


def test_every_auto_candidate_has_a_us_zero_retention_endpoint() -> None:
    offenders = {
        model_id: sorted(
            {
                (endpoint.provider, endpoint_privacy_tier(endpoint))
                for endpoint in MODEL_ENDPOINTS.values()
                if endpoint.model_id == model_id
            }
        )
        for model_id in DEFAULT_AUTO_MODEL_ORDER
        if not _us_zdr_providers(model_id)
    }
    assert not offenders, (
        "every default auto candidate must have a US-hosted endpoint at or above "
        f"zero-retention; these do not: {offenders}"
    )


def test_auto_ladder_spans_more_than_one_provider() -> None:
    """A single-provider ladder makes one provider outage an `auto` outage."""
    providers: set[str] = set()
    for model_id in DEFAULT_AUTO_MODEL_ORDER:
        providers |= _us_zdr_providers(model_id)
    assert len(providers) > 1, f"auto depends on a single provider: {providers}"


def test_cheapest_qualifying_model_leads_the_ladder() -> None:
    """The default route should take the cheap option first, not the pricey one."""
    assert DEFAULT_AUTO_MODEL_ORDER[0] == "deepseek/deepseek-v4-flash-0731"


def test_every_auto_candidate_is_a_real_resolvable_model() -> None:
    """A typo'd id is silently dropped by auto_candidate_models, which would
    shrink the ladder without failing anything."""
    missing = [model_id for model_id in DEFAULT_AUTO_MODEL_ORDER if model_id not in MODELS]
    assert not missing, f"auto references models absent from the catalog: {missing}"

    resolved = {model.id for model in auto_candidate_models()}
    dropped = [model_id for model_id in DEFAULT_AUTO_MODEL_ORDER if model_id not in resolved]
    assert not dropped, f"auto candidates silently dropped during resolution: {dropped}"
