"""Every provider's frozen price is its settle price, over the whole catalog.

Async settlement freezes prices at authorize (billing_snapshot.build_snapshot)
and charges what billing_snapshot.evaluate computes from the final usage. The
synchronous settle normalizes the same usage with normalized_prompt_accounting
and prices it with the endpoint cost function. This test holds the two equal
for every Credits endpoint in the catalog, of every provider, over a usage
grid built from each endpoint's own price tiers, through the settle handler's
own bindings of those two functions.

It reads the import-time catalog, so it follows the lifecycle clock: CI runs
it on the real clock and again with every scheduled cutover past.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from trusted_router import billing_snapshot as billing
from trusted_router.catalog import effective_endpoint
from trusted_router.catalog_data import ModelEndpoint
from trusted_router.catalog_registry import CATALOG_RESOLVED_AT, MODEL_ENDPOINTS
from trusted_router.pricing import PriceTier
from trusted_router.provider_contracts import (
    CACHE_EXCLUSIVE_INPUT_PROVIDERS,
    CACHE_EXCLUSIVE_INPUT_PUBLISHER_PREFIX,
    CACHE_EXCLUSIVE_INPUT_RESELLERS,
    PRIVATE_PRICE_TIER_BASIS_PROVIDER_MODELS,
    provider_input_excludes_cache,
    provider_model_has_private_price_tier_basis,
)
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewaySettleRequest
from trusted_router.stage_d import endpoint_pricing_candidate

# Output counts, and the prompt's cached share in tenths (read, written).
OUTPUTS = (0, 1, 777)
CACHE_SPLITS = ((0, 0), (10, 0), (0, 10), (3, 2), (5, 5))
ORDINARY = billing.Eligibility()


def _endpoint(model_id: str, provider: str, prompt: int, completion: int,
              tiers: tuple[PriceTier, ...] = ()) -> ModelEndpoint:
    return ModelEndpoint(
        id=f"{model_id}@{provider}/prepaid", model_id=model_id, provider=provider, usage_type="Credits",
        prompt_price_microdollars_per_million_tokens=prompt,
        completion_price_microdollars_per_million_tokens=completion, price_tiers=tiers,
    )


# One endpoint of each shape the catalog's own routes may lose to an hourly
# refresh, so each shape is compared on every run: a reseller of an anthropic/
# model with tiers and explicit cached rates, one without a cached rate (the
# provider default resolves it), a provider with a discounted cache read, a
# direct anthropic route, and a free route.
PINNED: tuple[ModelEndpoint, ...] = (
    _endpoint("anthropic/differential-a", "amazon-bedrock", 3_000_000, 15_000_000, (
        PriceTier(200_000, 3_000_000, 15_000_000, 300_000), PriceTier(None, 6_000_000, 22_500_000, 600_000))),
    _endpoint("anthropic/differential-b", "google-vertex", 3_000_000, 15_000_000),
    _endpoint("openai/differential-c", "azure", 1_250_000, 10_000_000, (
        PriceTier(272_000, 1_250_000, 10_000_000, 125_000), PriceTier(None, 2_500_000, 15_000_000, None))),
    _endpoint("qwen/differential-d", "alibaba", 400_000, 1_200_000),
    _endpoint("anthropic/differential-e", "anthropic", 3_000_000, 15_000_000, (
        PriceTier(200_000, 3_000_000, 15_000_000, None), PriceTier(None, 6_000_000, 22_500_000, None))),
    _endpoint("deepseek/differential-f", "venice", 0, 0),
    # The reseller of the first pinned endpoint serving another publisher's
    # model: cache-inclusive, where its Claude endpoint is exclusive.
    _endpoint("openai/differential-g", "amazon-bedrock", 1_250_000, 10_000_000, (
        PriceTier(None, 1_250_000, 10_000_000, 125_000),)),
)


def _credits() -> list[ModelEndpoint]:
    return [e for e in MODEL_ENDPOINTS.values() if str(e.usage_type) == "Credits"]


def _instants(endpoint: ModelEndpoint) -> list[datetime]:
    """The catalog instant, and for an endpoint with an effective-dated price
    each later hour of the next day at which its frozen price differs."""
    day = CATALOG_RESOLVED_AT + timedelta(hours=24)
    if effective_endpoint(endpoint, at=CATALOG_RESOLVED_AT) is endpoint and effective_endpoint(
            endpoint, at=day) is endpoint:
        return [CATALOG_RESOLVED_AT]
    seen: dict[str, datetime] = {}
    for hour in range(25):
        at = CATALOG_RESOLVED_AT + timedelta(hours=hour)
        seen.setdefault(repr(endpoint_pricing_candidate(effective_endpoint(endpoint, at=at))), at)
    return list(seen.values())


def _prompts(candidate: billing.Candidate) -> list[int]:
    """Total prompt sizes: small, large, and each side of each tier boundary."""
    points = {0, 1, 999, 2_000_001}
    for tier in candidate.tiers:
        if tier.max_prompt_tokens is not None:
            points.update((tier.max_prompt_tokens, tier.max_prompt_tokens + 1))
    return sorted(points)


def _reported(candidate: billing.Candidate, total: int, read: int, written: int) -> int:
    """The input count the provider reports for a prompt of ``total`` tokens."""
    return total - read - written if candidate.prompt_convention == "excludes_cache" else total


def _body(reported: int, read: int, written: int) -> SimpleNamespace:
    # The three counts normalized_prompt_accounting reads of a settle body.
    return SimpleNamespace(input_count=reported, cache_read_count=read, cache_creation_count=written)


def _settle_cost(endpoint: ModelEndpoint, at: datetime, reported: int, output: int, read: int,
                 written: int, *, tier_basis: int | None = None) -> tuple[int, int, int]:
    """The synchronous settle's arithmetic for an ordinary Credits request,
    through the handler's own names: (cost, uncached input, total prompt)."""
    uncached, total, cache_read, cache_creation = gateway.normalized_prompt_accounting(
        endpoint.provider, _body(reported, read, written), model_id=endpoint.model_id)
    cost = gateway._endpoint_cost_microdollars(
        endpoint, uncached, output, cache_read_tokens=cache_read, cache_creation_tokens=cache_creation,
        price_tier_input_tokens=gateway._provider_price_tier_input_tokens(endpoint, tier_basis),
        effective_at=at, service_tier=None)
    return cost, uncached, total


def _grid(candidate: billing.Candidate) -> Iterator[tuple[int, int, int, int]]:
    for total in _prompts(candidate):
        for read_tenths, written_tenths in CACHE_SPLITS:
            read, written = total * read_tenths // 10, total * written_tenths // 10
            for output in OUTPUTS:
                yield _reported(candidate, total, read, written), output, read, written


def test_the_settle_body_counts_are_the_ones_compared() -> None:
    """The stand-in body above carries what a real settle body's properties do."""
    real = GatewaySettleRequest(authorization_id="a", actual_input_tokens=10, actual_output_tokens=3,
                                cache_read_input_tokens=4, cache_creation_input_tokens=2)
    stand_in = _body(10, 4, 2)
    for provider in ("anthropic", "amazon-bedrock", "openai"):
        for model_id in ("anthropic/claude-x", "openai/gpt-x"):
            assert gateway.normalized_prompt_accounting(provider, real, model_id=model_id) == (
                gateway.normalized_prompt_accounting(provider, stand_in, model_id=model_id))
    assert gateway.normalized_prompt_accounting("anthropic", real, model_id="anthropic/claude-x") == (10, 16, 4, 2)
    assert gateway.normalized_prompt_accounting("amazon-bedrock", real, model_id="anthropic/claude-x") == (10, 16, 4, 2)
    assert gateway.normalized_prompt_accounting("amazon-bedrock", real, model_id="openai/gpt-x") == (4, 10, 4, 2)


def test_frozen_price_is_the_settle_price_for_every_credits_endpoint() -> None:
    credits = _credits()
    compared = Counter()
    providers: set[str] = set()
    refused: list[str] = []
    shapes = Counter()
    for endpoint in (*credits, *PINNED):
        for at in _instants(endpoint):
            effective = effective_endpoint(endpoint, at=at)
            try:
                snapshot = billing.build_snapshot([effective], ORDINARY)
            except ValidationError as exc:
                # The one refusal: a request fee, which the snapshot does not carry.
                assert [error["msg"] for error in exc.errors()] == ["Value error, request_fee"], endpoint.id
                assert effective.request_price_microdollars != 0, endpoint.id
                refused.append(endpoint.id)
                continue
            assert effective.request_price_microdollars == 0, endpoint.id
            candidate = snapshot.candidates[0]
            # One rule decides the convention, for the snapshot and the settle
            # path: the anthropic slug, and the two resellers relaying
            # Anthropic-native usage for an anthropic/ model.
            excludes = provider_input_excludes_cache(endpoint.provider, endpoint.model_id)
            assert candidate.prompt_convention == ("excludes_cache" if excludes else "includes_cache")
            assert excludes == (endpoint.provider in CACHE_EXCLUSIVE_INPUT_PROVIDERS or (
                endpoint.provider in CACHE_EXCLUSIVE_INPUT_RESELLERS
                and endpoint.model_id.startswith(CACHE_EXCLUSIVE_INPUT_PUBLISHER_PREFIX)))
            for reported, output, read, written in _grid(candidate):
                cost, uncached, total = _settle_cost(endpoint, at, reported, output, read, written)
                frozen = billing.evaluate(snapshot, endpoint.id, billing.RawUsage(
                    input_tokens=reported, output_tokens=output, cache_read_tokens=read,
                    cache_creation_tokens=written), ORDINARY)
                assert (frozen.charge_micro, frozen.usage.uncached_input_tokens,
                        frozen.usage.total_prompt_tokens) == (cost, uncached, total), (
                    endpoint.id, at, reported, output, read, written)
                compared[endpoint.id] += 1
            providers.add(endpoint.provider)
            shapes["tiered"] += bool(candidate.tiers)
            shapes["resold"] += not endpoint.model_id.startswith(endpoint.provider + "/")
            # Claude resold by amazon-bedrock or google-vertex: exclusive, as
            # the enclave relays Anthropic-native usage for it.
            shapes["resold_claude"] += endpoint.model_id.startswith("anthropic/") and endpoint.provider != "anthropic"
            shapes["excludes_cache"] += excludes
            shapes["free"] += candidate.rates.input_micro_per_million == 0
    # It cannot pass empty: nearly every Credits endpoint, of nearly every
    # provider, was compared, and each shape above among them.
    listed = {endpoint.provider for endpoint in credits}
    assert len(credits) >= 500 and len(listed) >= 40
    assert len(compared) + len(set(refused)) == len(credits) + len(PINNED)
    assert len(set(refused)) <= 10 and not set(refused) & {endpoint.id for endpoint in PINNED}
    assert providers >= listed - {MODEL_ENDPOINTS[endpoint_id].provider for endpoint_id in refused}
    assert min(compared.values()) >= len(OUTPUTS) * len(CACHE_SPLITS) * 4
    assert sum(compared.values()) >= 30_000
    for shape in ("tiered", "resold", "resold_claude", "excludes_cache", "free"):
        assert shapes[shape] >= 1, shape
    assert shapes["tiered"] >= 100 and shapes["resold"] >= 100


def test_usage_the_settle_path_clamps_is_refused_not_repriced() -> None:
    """A cached count above a cache-inclusive input is clamped by the
    synchronous settle and refused by the frozen evaluation (the enclave then
    settles synchronously), and reasoning above output likewise: the frozen
    path never answers such usage with a price of its own."""
    checked = 0
    for endpoint in (*_credits(), *PINNED):
        effective = effective_endpoint(endpoint, at=CATALOG_RESOLVED_AT)
        if effective.request_price_microdollars != 0:
            continue
        snapshot = billing.build_snapshot([effective], ORDINARY)
        if snapshot.candidates[0].prompt_convention == "includes_cache":
            cost, uncached, total = _settle_cost(endpoint, CATALOG_RESOLVED_AT, 10, 3, 8, 4)
            assert (uncached, total) == (0, 10)  # the synchronous clamp
            with pytest.raises(ValueError, match="^malformed_usage$"):
                billing.evaluate(snapshot, endpoint.id, billing.RawUsage(
                    input_tokens=10, output_tokens=3, cache_read_tokens=8, cache_creation_tokens=4), ORDINARY)
        with pytest.raises(ValueError, match="malformed_usage"):
            billing.evaluate(snapshot, endpoint.id, billing.RawUsage(
                input_tokens=10, output_tokens=3, reasoning_tokens=4), ORDINARY)
        # Reasoning within output is informational in both paths.
        plain = billing.evaluate(snapshot, endpoint.id, billing.RawUsage(input_tokens=10, output_tokens=3), ORDINARY)
        reasoned = billing.evaluate(snapshot, endpoint.id, billing.RawUsage(
            input_tokens=10, output_tokens=3, reasoning_tokens=3), ORDINARY)
        assert plain.charge_micro == reasoned.charge_micro == _settle_cost(
            endpoint, CATALOG_RESOLVED_AT, 10, 3, 0, 0)[0]
        checked += 1
    assert checked >= 500


def test_the_comparison_sees_a_wrong_convention_and_a_private_tier_basis() -> None:
    """Two controls on the comparison itself. Read by the other convention,
    cached usage prices differently; and a provider-private tier basis, which
    the snapshot's total-prompt basis cannot hold, prices differently too,
    which is why authorize leaves such an endpoint to the synchronous settle."""
    bedrock = PINNED[0]  # Claude resold by amazon-bedrock: cache-exclusive
    assert billing.build_snapshot([bedrock], ORDINARY).candidates[0].prompt_convention == "excludes_cache"
    inclusive_endpoint = PINNED[6]  # the same reseller serving another publisher's model
    snapshot = billing.build_snapshot([inclusive_endpoint], ORDINARY)
    assert snapshot.candidates[0].prompt_convention == "includes_cache"
    cost, _uncached, _total = _settle_cost(inclusive_endpoint, CATALOG_RESOLVED_AT, 100_000, 10, 60_000, 0)
    as_reported = billing.RawUsage(input_tokens=100_000, output_tokens=10, cache_read_tokens=60_000)
    assert billing.evaluate(snapshot, inclusive_endpoint.id, as_reported, ORDINARY).charge_micro == cost
    # The same counts read as cache-exclusive input: 100,000 uncached, not 40,000.
    exclusive = billing.BillingSnapshot.model_validate({**snapshot.model_dump(), "candidates": [{
        **snapshot.candidates[0].model_dump(), "provider": "anthropic", "prompt_convention": "excludes_cache"}]})
    assert billing.evaluate(exclusive, inclusive_endpoint.id, as_reported, ORDINARY).charge_micro > cost
    # And the reseller's Claude endpoint: the settle path reads its usage as
    # cache-exclusive, as the frozen evaluation does; read by the slug alone
    # (what it did before the rule took the model id) the same counts clamp
    # to no uncached input and a 40,000-token prompt, and price below it.
    cost, uncached, total = _settle_cost(bedrock, CATALOG_RESOLVED_AT, 40_000, 10, 60_000, 0)
    assert (uncached, total) == (40_000, 100_000)
    assert billing.evaluate(billing.build_snapshot([bedrock], ORDINARY), bedrock.id, billing.RawUsage(
        input_tokens=40_000, output_tokens=10, cache_read_tokens=60_000), ORDINARY).charge_micro == cost
    by_slug = gateway.normalized_prompt_accounting(bedrock.provider, _body(40_000, 60_000, 0))
    assert by_slug == (0, 40_000, 60_000, 0)
    assert gateway._endpoint_cost_microdollars(
        bedrock, by_slug[0], 10, cache_read_tokens=60_000, cache_creation_tokens=0, price_tier_input_tokens=None,
        effective_at=CATALOG_RESOLVED_AT, service_tier=None) < cost

    assert PRIVATE_PRICE_TIER_BASIS_PROVIDER_MODELS
    for provider, model_id in PRIVATE_PRICE_TIER_BASIS_PROVIDER_MODELS:
        private = replace(bedrock, id=f"{model_id}@{provider}/prepaid", model_id=model_id, provider=provider)
        assert provider_model_has_private_price_tier_basis(private.provider, private.model_id)
        frozen = billing.evaluate(billing.build_snapshot([private], ORDINARY), private.id,
                                  billing.RawUsage(input_tokens=200_001, output_tokens=10), ORDINARY).charge_micro
        # Without a reported basis the settle path selects the tier by the total prompt, as the snapshot does.
        assert _settle_cost(private, CATALOG_RESOLVED_AT, 200_001, 10, 0, 0)[0] == frozen
        # With one, it selects by the provider's own count: here the first tier, not the second.
        assert _settle_cost(private, CATALOG_RESOLVED_AT, 200_001, 10, 0, 0, tier_basis=150_000)[0] < frozen
    # Every other endpoint's reported basis is ignored by the settle path.
    for endpoint in (*_credits(), *PINNED):
        private_basis = provider_model_has_private_price_tier_basis(endpoint.provider, endpoint.model_id)
        assert (gateway._provider_price_tier_input_tokens(endpoint, 150_000) is not None) is private_basis
