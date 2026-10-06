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
        meets(e, ZERO_RETENTION)        <=>  zero_data_retention(e) is True
        meets(e, CONFIDENTIAL)          <=>  confidential compute AND e2ee AND ZDR
                                             AND e is not the model vendor's own service
        meets(e, NO_STORE)               <=> not stores_content(e)

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
    _MODEL_PROVIDER_PRIVACY_OVERRIDES,
    PRIVACY_TIER_CONFIDENTIAL,
    PRIVACY_TIER_NO_STORE,
    PRIVACY_TIER_STANDARD,
    PRIVACY_TIER_ZERO_RETENTION,
    ModelEndpoint,
    ModelProviderPrivacyOverride,
)
from trusted_router.catalog_privacy import (
    endpoint_confidential_compute,
    endpoint_e2ee,
    endpoint_meets_privacy_requirement,
    endpoint_privacy_tier,
    endpoint_stores_content,
    endpoint_zero_data_retention,
    model_provider_privacy_tier,
    provider_privacy_tier,
    served_by_model_vendor,
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
        if endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_ZERO_RETENTION)
        != (endpoint_zero_data_retention(endpoint) is True)
    ]
    assert not disagreements, (
        "tier and the published ZDR claim disagree for "
        f"{len(disagreements)} route(s): {disagreements[:5]}"
    )


def test_confidential_tier_requires_all_three_flags() -> None:
    """CONFIDENTIAL is the strongest claim the catalog makes. It must never be
    reachable without all three underlying flags actually being set."""
    for endpoint in ALL_ENDPOINTS:
        expected = (
            endpoint_confidential_compute(endpoint) is True
            and endpoint_e2ee(endpoint) is True
            and endpoint_zero_data_retention(endpoint) is True
            and not served_by_model_vendor(endpoint.model_id, endpoint.provider)
        )
        assert endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_CONFIDENTIAL) is expected
        assert (endpoint_privacy_tier(endpoint) == PRIVACY_TIER_CONFIDENTIAL) is expected
        if not endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_CONFIDENTIAL):
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
        assert endpoint_stores_content(endpoint) != endpoint_meets_privacy_requirement(
            endpoint, PRIVACY_TIER_NO_STORE
        ), f"{_describe(endpoint)}: stores_content disagrees with no-store matching"


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
        assert justified, f"{_describe(endpoint)} sits at tier {tier} with no flag justifying it"


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
        provider, provider_confidential_compute=True, provider_e2ee=True,
        provider_zero_data_retention=True,
    )
    assert provider_privacy_tier(confidential) == PRIVACY_TIER_CONFIDENTIAL


@given(provider=synthetic_providers())
@settings(max_examples=500)
def test_confidential_requires_all_three_flags_by_construction(provider: object) -> None:
    """Unknown or false on any prerequisite disqualifies Confidential."""
    if provider_privacy_tier(provider) == PRIVACY_TIER_CONFIDENTIAL:
        assert provider.provider_confidential_compute and provider.provider_e2ee
        assert provider.provider_zero_data_retention is True


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
            {"provider_confidential_compute": True, "provider_e2ee": True,
             "provider_zero_data_retention": True},
            PRIVACY_TIER_CONFIDENTIAL,
        ),
        ({"provider_confidential_compute": True, "provider_e2ee": True}, PRIVACY_TIER_STANDARD),
        ({"provider_confidential_compute": True, "provider_e2ee": True,
          "provider_zero_data_retention": False}, PRIVACY_TIER_STANDARD),
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
# The incoherence this module found, and the guard that keeps it closed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("zdr", [None, False])
def test_tee_and_e2ee_without_zdr_remain_accurate_but_not_confidential(zdr: bool | None) -> None:
    template = next(iter(PROVIDERS.values()))
    provider = dataclasses.replace(
        template,
        stores_content=True,
        provider_zero_data_retention=zdr,
        provider_confidential_compute=True,
        provider_e2ee=True,
        prepaid_zero_data_retention=False,
    )
    assert provider_privacy_tier(provider) == PRIVACY_TIER_STANDARD
    assert provider.provider_zero_data_retention is zdr
    assert provider.provider_confidential_compute is True
    assert provider.provider_e2ee is True


def _with_confidential_flags(monkeypatch: pytest.MonkeyPatch, *slugs: str) -> None:
    for slug in slugs:
        monkeypatch.setitem(
            PROVIDERS,
            slug,
            dataclasses.replace(
                PROVIDERS[slug],
                provider_confidential_compute=True,
                provider_e2ee=True,
                provider_zero_data_retention=True,
            ),
        )


def _route(model_id: str, provider: str) -> ModelEndpoint:
    template = ALL_ENDPOINTS[0]
    return dataclasses.replace(
        template, id=f"{model_id}@{provider}/prepaid", model_id=model_id, provider=provider
    )


def test_confidential_never_admits_the_model_vendor(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Confidential request never reaches the model vendor, even when the
    vendor's own API carries every Confidential flag."""
    _with_confidential_flags(monkeypatch, "minimax", "novita", "trustedrouter")

    vendor = _route("minimax/minimax-m3", "minimax")
    assert not endpoint_meets_privacy_requirement(vendor, PRIVACY_TIER_CONFIDENTIAL)
    assert endpoint_privacy_tier(vendor) == PRIVACY_TIER_ZERO_RETENTION
    assert model_provider_privacy_tier("minimax/minimax-m3", "minimax") == PRIVACY_TIER_ZERO_RETENTION
    # Spelling the author another way names the same vendor.
    assert served_by_model_vendor("MiniMaxAI/MiniMax-M3", "minimax")

    # Positive controls: the same flags on a third-party host, and on
    # TrustedRouter's own models, whose gateway a Confidential request trusts.
    host = _route("minimax/minimax-m3", "novita")
    assert endpoint_meets_privacy_requirement(host, PRIVACY_TIER_CONFIDENTIAL)
    assert endpoint_privacy_tier(host) == PRIVACY_TIER_CONFIDENTIAL
    assert model_provider_privacy_tier("minimax/minimax-m3", "novita") == PRIVACY_TIER_CONFIDENTIAL
    own = _route("trustedrouter/prometheus-1.0-1m", "trustedrouter")
    assert endpoint_meets_privacy_requirement(own, PRIVACY_TIER_CONFIDENTIAL)


@pytest.mark.parametrize(
    "model_id,vendor_host",
    [
        # The maker's own API, under the maker map and under an alias author.
        ("deepseek/deepseek-v4-pro", "deepseek"),
        ("moonshotai/kimi-k3", "kimi"),
        ("qwen/qwen3.7-max", "alibaba"),
        # Hosts the maker's company runs besides its own API.
        ("google/gemini-3-pro", "google-vertex"),
        ("nvidia/nemotron-3-ultra-550b-a55b", "nvidia-nim"),
        ("microsoft/phi-5", "azure"),
        ("meta-llama/llama-4-maverick", "meta"),
        # A provider named like the model's author.
        ("system1models/s1-pro", "system1models"),
    ],
)
def test_every_host_the_vendor_runs_is_the_vendor(
    monkeypatch: pytest.MonkeyPatch, model_id: str, vendor_host: str
) -> None:
    _with_confidential_flags(monkeypatch, vendor_host, "tinfoil")
    assert served_by_model_vendor(model_id, vendor_host)
    assert not endpoint_meets_privacy_requirement(
        _route(model_id, vendor_host), PRIVACY_TIER_CONFIDENTIAL
    )
    # Positive control: a third-party host with the same flags.
    assert endpoint_meets_privacy_requirement(_route(model_id, "tinfoil"), PRIVACY_TIER_CONFIDENTIAL)


def test_no_shipped_provider_has_confidential_tier_without_zdr() -> None:
    offenders = [
        slug
        for slug, provider in PROVIDERS.items()
        if provider_privacy_tier(provider) == PRIVACY_TIER_CONFIDENTIAL
        and provider.provider_zero_data_retention is not True
    ]
    assert not offenders, (
        f"providers {offenders} have the Confidential label without explicit ZDR"
    )


@pytest.mark.parametrize("zdr", [None, False, True])
@pytest.mark.parametrize("usage", ["Credits", "BYOK"])
def test_confidential_prepaid_zdr_is_credential_scoped(
    monkeypatch: pytest.MonkeyPatch, zdr: bool | None, usage: str,
) -> None:
    monkeypatch.setitem(PROVIDERS, "tinfoil", dataclasses.replace(
        PROVIDERS["tinfoil"], provider_zero_data_retention=zdr,
        prepaid_zero_data_retention=True, stores_content=True,
    ))
    endpoint = ModelEndpoint(id="test", model_id="test/model", provider="tinfoil", usage_type=usage)
    expected = zdr is True or usage == "Credits"
    assert endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_CONFIDENTIAL) is expected
    assert (endpoint_privacy_tier(endpoint) == PRIVACY_TIER_CONFIDENTIAL) is expected


@pytest.mark.parametrize("zdr", [False, True])
def test_confidential_override_cannot_bypass_retention_gate(
    monkeypatch: pytest.MonkeyPatch, zdr: bool,
) -> None:
    monkeypatch.setitem(_MODEL_PROVIDER_PRIVACY_OVERRIDES, ("test/model", "tinfoil"),
        ModelProviderPrivacyOverride(
            privacy_tier=PRIVACY_TIER_CONFIDENTIAL, provider_zero_data_retention=zdr,
            provider_confidential_compute=True, provider_e2ee=True, stores_content=not zdr,
        ))
    endpoint = ModelEndpoint(id="test", model_id="test/model", provider="tinfoil", usage_type="Credits")
    assert endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_CONFIDENTIAL) is zdr
    assert (endpoint_privacy_tier(endpoint) == PRIVACY_TIER_CONFIDENTIAL) is zdr
    assert (model_provider_privacy_tier("test/model", "tinfoil") == PRIVACY_TIER_CONFIDENTIAL) is zdr
