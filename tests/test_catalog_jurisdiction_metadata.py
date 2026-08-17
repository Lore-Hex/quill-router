"""Contracts for provider jurisdiction and model-origin metadata.

Two separate facts live in catalog_data.py and must stay separately true:
Provider.provider_headquarters_country (where the operator of the routed API
endpoint is legally based) and MODEL_ORIGINS (where the weights were built).
These tests pin the coverage and the shape of both, and they pin that a missing
country is a documented decision rather than an oversight.
"""

from __future__ import annotations

import re
from collections import Counter

from trusted_router.catalog import MODELS, PROVIDERS
from trusted_router.catalog_data import (
    EU_FOCUSED_PROVIDER_ORDER,
    MODEL_ORIGIN_REQUIRED_MODEL_COUNT,
    MODEL_ORIGINS,
    PROVIDER_JURISDICTION_CN,
    PROVIDER_JURISDICTION_UNVERIFIED,
    PROVIDER_JURISDICTION_US,
    US_PROVIDER_ONLY_MODEL_IDS,
    model_origin_for_model_id,
)

_ISO_ALPHA2 = re.compile(r"^[A-Z]{2}$")


def _vendor_prefix_counts() -> Counter[str]:
    return Counter(model_id.split("/")[0] for model_id in MODELS if "/" in model_id)


def test_every_provider_has_a_country_or_a_documented_reason_it_has_none() -> None:
    undocumented = sorted(
        slug
        for slug, provider in PROVIDERS.items()
        if provider.provider_headquarters_country is None
        and slug not in PROVIDER_JURISDICTION_UNVERIFIED
    )

    assert undocumented == [], (
        "these providers have no headquarters country and no entry in "
        f"PROVIDER_JURISDICTION_UNVERIFIED saying what was checked: {undocumented}"
    )


def test_unverified_jurisdiction_entries_are_real_providers_without_a_country() -> None:
    for slug, reason in PROVIDER_JURISDICTION_UNVERIFIED.items():
        assert slug in PROVIDERS, f"{slug} is not a provider slug"
        assert PROVIDERS[slug].provider_headquarters_country is None, (
            f"{slug} has a headquarters country, so it must not also be listed as unverified"
        )
        # A reason has to say what was read, not merely that nothing was found.
        assert len(reason) >= 80, f"{slug} needs a fuller record of what was checked"
        assert "heck" in reason or "read" in reason, (
            f"{slug} must record what was checked, e.g. the pages read"
        )


def test_provider_countries_are_iso_alpha2_codes() -> None:
    for slug, provider in PROVIDERS.items():
        country = provider.provider_headquarters_country
        if country is None:
            continue
        assert _ISO_ALPHA2.match(country), f"{slug} has a non-ISO-alpha-2 country: {country!r}"


def test_named_provider_jurisdictions_match_their_researched_values() -> None:
    # Spot-check the entities this change researched. Moving one of these needs
    # a new source, so the change has to touch this list to land.
    expected = {
        "deepseek": PROVIDER_JURISDICTION_CN,
        "mistral": "FR",
        "kimi": PROVIDER_JURISDICTION_CN,
        "zai": "SG",
        "siliconflow": "SG",
        "friendli": PROVIDER_JURISDICTION_US,
        "zero-g": PROVIDER_JURISDICTION_US,
        "deepinfra": PROVIDER_JURISDICTION_US,
        "nebius": "NL",
        "minimax": PROVIDER_JURISDICTION_CN,
        "xiaomi": PROVIDER_JURISDICTION_CN,
        "alibaba": PROVIDER_JURISDICTION_CN,
        "ltx": "IL",
        "cohere": "CA",
    }

    actual = {slug: PROVIDERS[slug].provider_headquarters_country for slug in sorted(expected)}

    assert actual == expected


def test_providers_with_unverified_jurisdiction_cannot_satisfy_a_us_only_request() -> None:
    # None is the conservative state: US-only routing compares against the
    # US code, so an unverified provider is excluded rather than assumed.
    for slug in PROVIDER_JURISDICTION_UNVERIFIED:
        assert PROVIDERS[slug].provider_headquarters_country != PROVIDER_JURISDICTION_US


def test_model_origins_cover_every_vendor_prefix_with_enough_models() -> None:
    counts = _vendor_prefix_counts()
    missing = sorted(
        prefix
        for prefix, count in counts.items()
        if count >= MODEL_ORIGIN_REQUIRED_MODEL_COUNT and prefix not in MODEL_ORIGINS
    )

    assert missing == [], (
        "these vendor prefixes carry "
        f"{MODEL_ORIGIN_REQUIRED_MODEL_COUNT}+ catalog models and need a MODEL_ORIGINS "
        f"row: {missing}"
    )


def test_model_origin_rows_are_sourced_or_explain_their_absent_country() -> None:
    assert MODEL_ORIGINS

    for prefix, origin in MODEL_ORIGINS.items():
        assert origin.lab_name, f"{prefix} needs a lab name"
        if origin.country is None:
            assert origin.note, f"{prefix} records no country and must explain why"
            continue
        assert _ISO_ALPHA2.match(origin.country), (
            f"{prefix} has a non-ISO-alpha-2 country: {origin.country!r}"
        )
        assert origin.source_url and origin.source_url.startswith("https://"), (
            f"{prefix} claims country {origin.country} and needs an https source"
        )


def test_model_origins_only_describe_prefixes_the_catalog_uses() -> None:
    counts = _vendor_prefix_counts()
    unused = sorted(prefix for prefix in MODEL_ORIGINS if prefix not in counts)

    assert unused == [], f"MODEL_ORIGINS rows for prefixes no catalog model uses: {unused}"


def test_model_origin_lookup_reads_the_vendor_prefix() -> None:
    assert model_origin_for_model_id("z-ai/glm-5.2") is MODEL_ORIGINS["z-ai"]
    assert model_origin_for_model_id("Qwen/Qwen3-32B") is MODEL_ORIGINS["Qwen"]
    # Model ids without a vendor prefix, and prefixes with no row, stay None
    # instead of borrowing a neighbour's origin.
    assert model_origin_for_model_id("gpt-4o-mini") is None
    assert model_origin_for_model_id("no-such-vendor/some-model") is None


def test_chinese_origin_weights_can_be_served_by_a_non_chinese_operator() -> None:
    # The two tables answer different questions, and this is the case that
    # proves it: GLM weights are Chinese, the routed Z.ai API operator is not.
    glm_origin = MODEL_ORIGINS["z-ai"]

    assert glm_origin.country == PROVIDER_JURISDICTION_CN
    assert PROVIDERS["zai"].provider_headquarters_country == "SG"


def test_trustedrouter_families_are_recorded_as_us_built() -> None:
    origin = MODEL_ORIGINS["trustedrouter"]

    assert origin.country == PROVIDER_JURISDICTION_US
    assert all(model_id.startswith("trustedrouter/") for model_id in US_PROVIDER_ONLY_MODEL_IDS)
    # US_PROVIDER_ONLY_MODEL_IDS is the existing request-time control and stays
    # the single source of that rule; MODEL_ORIGINS only records who built them.
    for model_id in US_PROVIDER_ONLY_MODEL_IDS:
        assert model_origin_for_model_id(model_id) is origin


def test_eu_focused_route_has_no_provider_with_an_unverified_jurisdiction_left_unlabelled() -> None:
    # The trustedrouter/eu provider order is jurisdiction-adjacent copy, so each
    # provider in it must be either labelled or explicitly documented as not.
    for slug in EU_FOCUSED_PROVIDER_ORDER:
        provider = PROVIDERS[slug]
        assert (
            provider.provider_headquarters_country is not None
            or slug in PROVIDER_JURISDICTION_UNVERIFIED
        )
