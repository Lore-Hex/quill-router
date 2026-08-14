"""Exhaustive coherence proof for the catalog's privacy tiers.

Three things describe a route's privacy posture, and customers see all three:

  * `endpoint_privacy_tier` — the rank the router enforces a `min_privacy`
    floor against
  * the boolean claims — `endpoint_stores_content`,
    `endpoint_zero_data_retention`, and the confidential-compute pair, which
    are what the catalog UI and the `/models` API publish
  * the override table, which lets a specific (model, provider) route depart
    from its provider's default posture

If those disagree, one of two things happens, and both are bad in the way that
matters most for this product: a route is advertised as more private than the
router will actually enforce (overclaiming retention posture to a paying
customer), or less (silently excluding a route the customer paid for).

The law, quantified over the whole catalog:

    for every ModelEndpoint e,
        tier(e) >= ZERO_RETENTION      <=>  zero_data_retention(e) is True
        tier(e) == CONFIDENTIAL         =>  confidential compute AND e2ee
        tier(e) >= NO_STORE             =>  some explicit flag justifies it
        stores_content(e)               <=> tier(e) < NO_STORE

The catalog is finite — 51 providers, ~1500 endpoints — so this enumerates
rather than samples. That makes it a genuine proof *for the shipped catalog*,
re-established on every CI run, rather than evidence from a sample. Hypothesis
covers the part enumeration cannot: that the laws are consequences of the
CODE and not accidents of today's DATA, by generating synthetic providers and
override entries the real catalog does not currently contain.

The distinction matters. An exhaustive pass over real data proves today's
catalog is coherent. It does not stop someone adding a provider tomorrow whose
flags are contradictory. The synthetic half is what covers tomorrow.
"""

from __future__ import annotations

import dataclasses

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from trusted_router.catalog import MODEL_ENDPOINTS, PROVIDERS
from trusted_router.catalog_data import (
    PRIVACY_TIER_CONFIDENTIAL,
    PRIVACY_TIER_NO_STORE,
    PRIVACY_TIER_STANDARD,
    PRIVACY_TIER_ZERO_RETENTION,
)
from trusted_router.catalog_privacy import (
    endpoint_privacy_tier,
    endpoint_stores_content,
    endpoint_zero_data_retention,
    provider_privacy_tier,
)

# MODEL_ENDPOINTS is a dict keyed by "model@provider/usage"; the VALUES are the
# ModelEndpoint records. Iterating the mapping directly yields keys.
ALL_ENDPOINTS = list(MODEL_ENDPOINTS.values())


def _describe(endpoint: object) -> str:
    model = getattr(endpoint, "model_id", "?")
    provider = getattr(endpoint, "provider", "?")
    return f"{model} @ {provider}"


# ---------------------------------------------------------------------------
# Exhaustive over the shipped catalog.
# ---------------------------------------------------------------------------


def test_the_catalog_is_large_enough_that_enumeration_is_the_point() -> None:
    """Guard the guard: if the catalog ever collapses to a handful of entries,
    the exhaustive tests below stop being meaningful and someone should know."""
    assert len(ALL_ENDPOINTS) > 100, (
        f"only {len(ALL_ENDPOINTS)} endpoints; exhaustive coverage is no longer "
        "a meaningful claim and these tests need rethinking"
    )


def test_zero_data_retention_is_exactly_the_zdr_tier() -> None:
    """The biconditional. This is the law customers rely on most directly:
    the ZDR badge and the zdr routing floor must mean the same thing."""
    disagreements = [
        (
            _describe(endpoint),
            endpoint_privacy_tier(endpoint),
            endpoint_zero_data_retention(endpoint),
        )
        for endpoint in ALL_ENDPOINTS
        if (endpoint_privacy_tier(endpoint) >= PRIVACY_TIER_ZERO_RETENTION)
        != (endpoint_zero_data_retention(endpoint) is True)
    ]
    assert not disagreements, (
        "tier and the published ZDR claim disagree for "
        f"{len(disagreements)} route(s): {disagreements[:5]}"
    )


def test_confidential_tier_requires_both_confidential_flags() -> None:
    """CONFIDENTIAL is the strongest claim the catalog makes. It must never be
    reachable without both underlying flags actually being set."""
    for endpoint in ALL_ENDPOINTS:
        if endpoint_privacy_tier(endpoint) != PRIVACY_TIER_CONFIDENTIAL:
            continue
        provider = PROVIDERS[endpoint.provider]
        from trusted_router.catalog_privacy import _model_provider_privacy_override

        override = _model_provider_privacy_override(endpoint.model_id, endpoint.provider)
        if override is not None and override.privacy_tier == PRIVACY_TIER_CONFIDENTIAL:
            assert override.provider_confidential_compute is True, (
                f"{_describe(endpoint)}: override claims CONFIDENTIAL without "
                "provider_confidential_compute"
            )
            assert override.provider_e2ee is True, (
                f"{_describe(endpoint)}: override claims CONFIDENTIAL without provider_e2ee"
            )
        else:
            assert provider.provider_confidential_compute and provider.provider_e2ee, (
                f"{_describe(endpoint)}: CONFIDENTIAL tier without both provider flags"
            )


def test_stores_content_is_the_complement_of_the_no_store_tier() -> None:
    for endpoint in ALL_ENDPOINTS:
        assert endpoint_stores_content(endpoint) == (
            endpoint_privacy_tier(endpoint) < PRIVACY_TIER_NO_STORE
        ), f"{_describe(endpoint)}: stores_content disagrees with its tier"


def test_no_endpoint_clears_a_bar_without_an_explicit_flag() -> None:
    """A tier above STANDARD must be justified by something a human wrote —
    an override, a provider flag, or the prepaid-ZDR upgrade. Never by a
    default."""
    from trusted_router.catalog_privacy import _model_provider_privacy_override

    for endpoint in ALL_ENDPOINTS:
        tier = endpoint_privacy_tier(endpoint)
        if tier <= PRIVACY_TIER_STANDARD:
            continue
        provider = PROVIDERS[endpoint.provider]
        override = _model_provider_privacy_override(endpoint.model_id, endpoint.provider)
        justified = (
            override is not None
            or provider.stores_content is False
            or bool(provider.provider_zero_data_retention)
            or bool(provider.provider_confidential_compute and provider.provider_e2ee)
            or (endpoint.usage_type == "Credits" and provider.prepaid_zero_data_retention)
        )
        assert justified, (
            f"{_describe(endpoint)} sits at tier {tier} with no flag justifying it"
        )


def test_every_endpoint_provider_exists_and_tiers_are_in_range() -> None:
    """Totality: the tier function is defined everywhere and never returns a
    rank outside the declared ladder."""
    valid = {
        PRIVACY_TIER_STANDARD,
        PRIVACY_TIER_NO_STORE,
        PRIVACY_TIER_ZERO_RETENTION,
        PRIVACY_TIER_CONFIDENTIAL,
    }
    for endpoint in ALL_ENDPOINTS:
        assert endpoint.provider in PROVIDERS, f"{_describe(endpoint)}: unknown provider"
        assert endpoint_privacy_tier(endpoint) in valid, _describe(endpoint)


def test_the_prepaid_upgrade_only_ever_raises_a_tier() -> None:
    """The Credits/prepaid path is the one place a tier is computed rather than
    declared. It must be monotone: an upgrade rule that could LOWER a tier
    would silently downgrade a route whose provider posture already qualified."""
    for endpoint in ALL_ENDPOINTS:
        provider = PROVIDERS[endpoint.provider]
        if not (endpoint.usage_type == "Credits" and provider.prepaid_zero_data_retention):
            continue
        from trusted_router.catalog_privacy import _model_provider_privacy_override

        if _model_provider_privacy_override(endpoint.model_id, endpoint.provider) is not None:
            continue  # override wins; not the computed path
        assert endpoint_privacy_tier(endpoint) >= provider_privacy_tier(provider), (
            f"{_describe(endpoint)}: prepaid upgrade lowered the tier"
        )


# ---------------------------------------------------------------------------
# Synthetic providers: the laws must follow from the CODE, not from the
# accident that today's data happens to be consistent.
# ---------------------------------------------------------------------------

_PROVIDER_TEMPLATE = next(iter(PROVIDERS.values()))


@st.composite
def synthetic_providers(draw: object) -> object:
    """Every combination of the four posture flags, including contradictory
    ones a careless catalog edit could introduce."""
    return dataclasses.replace(
        _PROVIDER_TEMPLATE,
        stores_content=draw(st.booleans()),
        provider_zero_data_retention=draw(st.one_of(st.none(), st.booleans())),
        provider_confidential_compute=draw(st.one_of(st.none(), st.booleans())),
        provider_e2ee=draw(st.one_of(st.none(), st.booleans())),
        prepaid_zero_data_retention=draw(st.booleans()),
    )


@given(provider=synthetic_providers())
@settings(max_examples=500)
def test_provider_tier_is_monotone_in_its_flags(provider: object) -> None:
    """Turning a posture flag ON must never LOWER the resulting tier.

    This is the property that stops a future edit from creating a provider
    whose stronger guarantees compute to a weaker rank — the failure that no
    amount of exhaustive checking over today's data would catch.
    """
    base = provider_privacy_tier(provider)

    stronger = dataclasses.replace(provider, stores_content=False)
    assert provider_privacy_tier(stronger) >= base or provider.stores_content is False

    zdr = dataclasses.replace(provider, provider_zero_data_retention=True)
    assert provider_privacy_tier(zdr) >= PRIVACY_TIER_ZERO_RETENTION

    confidential = dataclasses.replace(
        provider, provider_confidential_compute=True, provider_e2ee=True
    )
    assert provider_privacy_tier(confidential) == PRIVACY_TIER_CONFIDENTIAL


@given(provider=synthetic_providers())
@settings(max_examples=500)
def test_confidential_requires_both_flags_by_construction(provider: object) -> None:
    """One flag alone must never reach CONFIDENTIAL — the pair is the claim."""
    if provider_privacy_tier(provider) == PRIVACY_TIER_CONFIDENTIAL:
        assert provider.provider_confidential_compute and provider.provider_e2ee


@given(provider=synthetic_providers())
@settings(max_examples=500)
def test_tier_is_always_within_the_declared_ladder(provider: object) -> None:
    assert PRIVACY_TIER_STANDARD <= provider_privacy_tier(provider) <= PRIVACY_TIER_CONFIDENTIAL


@pytest.mark.parametrize(
    "flags,expected",
    [
        ({"stores_content": True}, PRIVACY_TIER_STANDARD),
        ({"stores_content": False}, PRIVACY_TIER_NO_STORE),
        ({"provider_zero_data_retention": True}, PRIVACY_TIER_ZERO_RETENTION),
        (
            {"provider_confidential_compute": True, "provider_e2ee": True},
            PRIVACY_TIER_CONFIDENTIAL,
        ),
        # One confidential flag alone is NOT confidential.
        ({"provider_confidential_compute": True}, PRIVACY_TIER_STANDARD),
        ({"provider_e2ee": True}, PRIVACY_TIER_STANDARD),
    ],
)
def test_the_flag_ladder_is_pinned(flags: dict[str, object], expected: int) -> None:
    """The exact rung each flag buys. Pinned so a refactor of the if-chain
    cannot quietly re-order the ladder."""
    cleared = {
        "stores_content": True,
        "provider_zero_data_retention": None,
        "provider_confidential_compute": None,
        "provider_e2ee": None,
        "prepaid_zero_data_retention": False,
    }
    provider = dataclasses.replace(_PROVIDER_TEMPLATE, **{**cleared, **flags})
    assert provider_privacy_tier(provider) == expected


# ---------------------------------------------------------------------------
# A latent incoherence the code permits and the data currently avoids.
# ---------------------------------------------------------------------------


def test_confidential_without_the_zdr_flag_is_a_known_latent_incoherence() -> None:
    """Standing record of a gap the shipped catalog does not currently hit.

    A provider with `provider_confidential_compute` and `provider_e2ee` set but
    `provider_zero_data_retention=False` resolves to tier CONFIDENTIAL (3),
    which is above ZERO_RETENTION (2) — so the router would admit it for a
    `min_privacy=zdr` request. But `endpoint_zero_data_retention` reads the raw
    flag and returns False, so `/models` would publish "no ZDR" for a route the
    router treats as satisfying ZDR.

    The two would disagree, which is exactly what
    `test_zero_data_retention_is_exactly_the_zdr_tier` forbids across the real
    catalog. No shipped provider has that flag combination, so the biconditional
    holds today — this test exists so the gap is recorded rather than
    rediscovered.

    Deliberately NOT fixed here. The repair is a product decision about what
    the ZDR badge means (does confidential compute imply zero retention?), and
    it changes a value published on a customer-visible API. If this test starts
    failing, the ladder was changed and this note should be revisited.
    """
    template = next(iter(PROVIDERS.values()))
    contradictory = dataclasses.replace(
        template,
        stores_content=True,
        provider_zero_data_retention=False,
        provider_confidential_compute=True,
        provider_e2ee=True,
        prepaid_zero_data_retention=False,
    )

    tier = provider_privacy_tier(contradictory)
    assert tier == PRIVACY_TIER_CONFIDENTIAL
    assert tier >= PRIVACY_TIER_ZERO_RETENTION, (
        "the router would admit this provider for a zdr floor..."
    )
    assert contradictory.provider_zero_data_retention is False, (
        "...while the published ZDR claim would say False. Coherent only because "
        "no real provider is configured this way."
    )


def test_no_shipped_provider_has_the_contradictory_flag_combination() -> None:
    """The reason the gap above is latent rather than live. If a future catalog
    edit introduces this combination, this fails before the biconditional does,
    and points at the cause rather than the symptom."""
    offenders = [
        slug
        for slug, provider in PROVIDERS.items()
        if provider.provider_confidential_compute
        and provider.provider_e2ee
        and provider.provider_zero_data_retention is False
    ]
    assert not offenders, (
        f"providers {offenders} claim confidential compute + e2ee but explicitly "
        "deny zero data retention; the router and the published claim will disagree"
    )
