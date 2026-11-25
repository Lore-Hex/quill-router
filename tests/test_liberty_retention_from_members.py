"""Liberty 4.x's advertised retention comes from its members' routes.

Every Liberty 4.x request reaches every panel member, so the combo is only as
private as its weakest member's best eligible route. A hard-coded or best-member
label could drift, as Liberty 4.1's did while Inkling 2 Small's only US route
(Cloudflare Workers AI) was outside strict ZDR.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.lifecycle_clock import catalog_predates
from trusted_router.catalog import (
    MODELS,
    PROVIDERS,
    endpoints_for_model,
    meta_candidate_models,
    model_max_privacy_tier,
)
from trusted_router.catalog_data import (
    LIBERTY_4_0_MODEL_ID,
    LIBERTY_4_1_MODEL_ID,
    PRIVACY_TIER_STANDARD,
    PRIVACY_TIER_ZERO_RETENTION,
    PROVIDER_JURISDICTION_US,
    US_PROVIDER_ONLY_MODEL_IDS,
)
from trusted_router.catalog_privacy import endpoint_privacy_tier, endpoint_zero_data_retention
from trusted_router.provider_lifecycle import BASETEN_DECEMBER_2026_RETIREMENT_AT

INKLING_2_SMALL = "thinkingmachines/inkling-2-small"
NEMOTRON_3_ULTRA = "nvidia/nemotron-3-ultra-550b-a55b"
LIBERTY_WINDOW = 1_048_576


def _inkling_endpoints() -> dict[str, object]:
    if INKLING_2_SMALL not in MODELS:
        pytest.skip("Inkling 2 Small is not cataloged under this clock")
    return {
        endpoint.provider: endpoint
        for endpoint in endpoints_for_model(INKLING_2_SMALL)
        if endpoint.usage_type == "Credits"
    }


def test_cloudflare_workers_ai_is_zero_retention_via_trs_account_setting() -> None:
    provider = PROVIDERS["cloudflare-workers-ai"]

    assert provider.provider_zero_data_retention is True
    assert provider.stores_content is False
    assert provider.supports_byok is False  # every request uses TR's account
    assert "TrustedRouter's arrangement" in provider.provider_policy
    assert "zero-data-retention setting" in provider.provider_policy
    assert "outside strict ZDR" not in provider.provider_policy
    cloudflare = _inkling_endpoints().get("cloudflare-workers-ai")
    if cloudflare is not None:
        assert endpoint_zero_data_retention(cloudflare) is True


def test_cloudflare_is_liberty_41s_only_eligible_zdr_inkling_route() -> None:
    """Eligible: ZDR, US-headquartered, Liberty's full window, not DeepInfra
    (barred from every combo). Nebius is ZDR but headquartered in the
    Netherlands, so Liberty 4.x's US-only rule keeps it out."""
    endpoints = _inkling_endpoints()
    if "cloudflare-workers-ai" not in endpoints:
        pytest.skip("Cloudflare's Inkling 2 Small route is not cataloged under this clock")
    eligible = {
        slug
        for slug, endpoint in endpoints.items()
        if endpoint_zero_data_retention(endpoint) is True
        and PROVIDERS[slug].provider_headquarters_country == PROVIDER_JURISDICTION_US
        and (endpoint.context_length or 0) >= LIBERTY_WINDOW
        and slug != "deepinfra"
    }

    assert eligible == {"cloudflare-workers-ai"}
    if "nebius" in endpoints:
        assert endpoint_zero_data_retention(endpoints["nebius"]) is True
        assert PROVIDERS["nebius"].provider_headquarters_country != PROVIDER_JURISDICTION_US


@pytest.mark.parametrize("model_id", [LIBERTY_4_0_MODEL_ID, LIBERTY_4_1_MODEL_ID])
def test_liberty_tier_is_its_weakest_members_best_us_route(model_id: str) -> None:
    assert model_id in US_PROVIDER_ONLY_MODEL_IDS
    members = meta_candidate_models(model_id)
    assert members
    weakest = min(
        max(
            (
                endpoint_privacy_tier(endpoint)
                for endpoint in endpoints_for_model(member.id)
                if PROVIDERS[endpoint.provider].provider_headquarters_country
                == PROVIDER_JURISDICTION_US
            ),
            default=PRIVACY_TIER_STANDARD,
        )
        for member in members
    )

    assert model_max_privacy_tier(MODELS[model_id]) == weakest


def test_liberty_41_advertises_zero_retention() -> None:
    if "cloudflare-workers-ai" not in _inkling_endpoints():
        pytest.skip("Cloudflare's Inkling 2 Small route is not cataloged under this clock")
    if catalog_predates(BASETEN_DECEMBER_2026_RETIREMENT_AT):
        assert model_max_privacy_tier(MODELS[LIBERTY_4_1_MODEL_ID]) >= PRIVACY_TIER_ZERO_RETENTION
        return
    # Baseten's 2026-12-24 retirement removes Nemotron 3 Ultra's only ZDR US
    # route. Its remaining US hosts are not ZDR, so Liberty 4.0 and 4.1 stop
    # advertising zero retention: the label follows the members, never a claim.
    ultra_best = max(
        endpoint_privacy_tier(endpoint)
        for endpoint in endpoints_for_model(NEMOTRON_3_ULTRA)
        if PROVIDERS[endpoint.provider].provider_headquarters_country == PROVIDER_JURISDICTION_US
    )
    assert ultra_best < PRIVACY_TIER_ZERO_RETENTION
    for model_id in (LIBERTY_4_0_MODEL_ID, LIBERTY_4_1_MODEL_ID):
        assert model_max_privacy_tier(MODELS[model_id]) == ultra_best


def test_one_non_zdr_member_route_demotes_liberty_41_not_40(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nebius's ZDR route cannot rescue Inkling: Liberty 4.x is US-only."""
    if "cloudflare-workers-ai" not in _inkling_endpoints():
        pytest.skip("Cloudflare's Inkling 2 Small route is not cataloged under this clock")
    liberty_40_before = model_max_privacy_tier(MODELS[LIBERTY_4_0_MODEL_ID])
    monkeypatch.setitem(
        PROVIDERS,
        "cloudflare-workers-ai",
        replace(
            PROVIDERS["cloudflare-workers-ai"],
            stores_content=True,
            provider_zero_data_retention=False,
        ),
    )

    assert model_max_privacy_tier(MODELS[LIBERTY_4_1_MODEL_ID]) < PRIVACY_TIER_ZERO_RETENTION
    assert model_max_privacy_tier(MODELS[LIBERTY_4_0_MODEL_ID]) == liberty_40_before
