"""Jurisdiction directories behind /us-ai-models, /eu-ai-models, /china-ai-models.

Search traffic conflates two questions. The catalog stores them as two separate
tables, so these pages answer them separately and never derive one from the
other:

  * Who built the weights — catalog_data.MODEL_ORIGINS, keyed by the vendor
    prefix of a model id, each row carrying a lab name and a primary source.
  * Whose endpoint a request is routed to — Provider.provider_headquarters_country,
    the legal home of the entity operating that endpoint.

The two disagree often: Chinese-origin open weights are served by US, Singapore,
and Dutch operators, and US-origin weights are served by non-US operators.

Neither field is a data-residency claim. A US-registered operator can serve a
request on hardware outside the US, and an EU-registered one can serve a request
outside the EU; the catalog records the operator's legal home, not the machine's
location. Providers whose operator TrustedRouter checked and could not establish
carry no country (catalog_data.PROVIDER_JURISDICTION_UNVERIFIED records what was
checked), and this module reports them as not established rather than folding
them into a region.

Every row is computed from the live catalog at render time, so a route, model, or
provider added to the catalog appears here without a second edit.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from trusted_router.catalog import (
    META_MODEL_IDS,
    MODELS,
    PROVIDERS,
    Model,
    Provider,
    endpoints_for_model,
    meta_candidate_models,
    model_us_provider_available,
)
from trusted_router.catalog_data import (
    LIBERTY_1_0_1M_MODEL_ID,
    LIBERTY_1_0_MODEL_ID,
    LIBERTY_2_0_MODEL_ID,
    LIBERTY_3_0_MODEL_ID,
    PARASAIL_LIBERTY_2_0_MODEL_ID,
    PRIVACY_TIER_LABELS,
    PROVIDER_JURISDICTION_CN,
    PROVIDER_JURISDICTION_US,
    model_origin_for_model_id,
)
from trusted_router.catalog_privacy import provider_privacy_tier

# The 27 EU member states, as ISO 3166-1 alpha-2 codes, from the EU's own list
# of member countries (https://european-union.europa.eu/principles-countries-history/eu-countries_en).
# Membership is the test for the EU page: a provider or lab is on it because its
# recorded country is a member state, not because a human decided it belonged.
# The EEA and Switzerland are deliberately not included; they are not EU members,
# and the page says which countries it covers.
EU_MEMBER_COUNTRIES: frozenset[str] = frozenset(
    {
        "AT",
        "BE",
        "BG",
        "CY",
        "CZ",
        "DE",
        "DK",
        "EE",
        "ES",
        "FI",
        "FR",
        "GR",
        "HR",
        "HU",
        "IE",
        "IT",
        "LT",
        "LU",
        "LV",
        "MT",
        "NL",
        "PL",
        "PT",
        "RO",
        "SE",
        "SI",
        "SK",
    }
)

# Country names for the codes the catalog can hold today. An unrecognised code
# renders as the code itself rather than a guessed name.
COUNTRY_NAMES: dict[str, str] = {
    "AT": "Austria",
    "BE": "Belgium",
    "BG": "Bulgaria",
    "CA": "Canada",
    "CN": "China",
    "CY": "Cyprus",
    "CZ": "Czechia",
    "DE": "Germany",
    "DK": "Denmark",
    "EE": "Estonia",
    "ES": "Spain",
    "FI": "Finland",
    "FR": "France",
    "GR": "Greece",
    "HR": "Croatia",
    "HU": "Hungary",
    "IE": "Ireland",
    "IL": "Israel",
    "IT": "Italy",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "LV": "Latvia",
    "MT": "Malta",
    "NL": "Netherlands",
    "PL": "Poland",
    "PT": "Portugal",
    "RO": "Romania",
    "SE": "Sweden",
    "SG": "Singapore",
    "SI": "Slovenia",
    "SK": "Slovakia",
    "US": "United States",
}

# What a missing provider country means on these pages. Never "unknown, assume
# the convenient answer": provider.jurisdiction filtering treats a missing
# country as a non-match, and each one is documented in
# catalog_data.PROVIDER_JURISDICTION_UNVERIFIED.
OPERATOR_NOT_ESTABLISHED_LABEL = "operator not established"

# Models shown per originating lab before the page defers to /models. Six keeps
# each lab's current generation visible without turning a directory into a dump
# of every historical checkpoint.
MODELS_PER_LAB = 6

# Operators listed per model before the row defers to the model page.
OPERATORS_PER_MODEL = 6

# The Liberty routes, newest first. Featured on the US page because every
# component model under them resolves to a US lab in MODEL_ORIGINS — which the
# page states as a computed count, not as a slogan.
LIBERTY_MODEL_IDS: tuple[str, ...] = (
    LIBERTY_3_0_MODEL_ID,
    LIBERTY_2_0_MODEL_ID,
    PARASAIL_LIBERTY_2_0_MODEL_ID,
    LIBERTY_1_0_1M_MODEL_ID,
    LIBERTY_1_0_MODEL_ID,
)


@dataclass(frozen=True)
class ModelRegion:
    """One jurisdiction page.

    origin_countries selects labs by MODEL_ORIGINS country; provider_countries
    selects operators by provider_headquarters_country. They are separate fields
    even where they hold the same code, because the two tables are separate.

    highlight_countries picks the serving jurisdiction each model row calls out
    by name. On the China page that is the US, because "which US host serves this
    Chinese model" is the question the page exists to answer.
    """

    slug: str
    country_label: str
    origin_countries: frozenset[str]
    provider_countries: frozenset[str]
    highlight_countries: frozenset[str]
    highlight_label: str
    origin_heading: str
    provider_heading: str


MODEL_REGIONS: dict[str, ModelRegion] = {
    "us-ai-models": ModelRegion(
        slug="us-ai-models",
        country_label="the United States",
        origin_countries=frozenset({PROVIDER_JURISDICTION_US}),
        provider_countries=frozenset({PROVIDER_JURISDICTION_US}),
        highlight_countries=frozenset({PROVIDER_JURISDICTION_US}),
        highlight_label="US-operated routes",
        origin_heading="Models built by US labs",
        provider_heading="Providers operated from the United States",
    ),
    "eu-ai-models": ModelRegion(
        slug="eu-ai-models",
        country_label="the European Union",
        origin_countries=EU_MEMBER_COUNTRIES,
        provider_countries=EU_MEMBER_COUNTRIES,
        highlight_countries=EU_MEMBER_COUNTRIES,
        highlight_label="EU-operated routes",
        origin_heading="Models built by EU labs",
        provider_heading="Providers operated from an EU member state",
    ),
    "china-ai-models": ModelRegion(
        slug="china-ai-models",
        country_label="China",
        origin_countries=frozenset({PROVIDER_JURISDICTION_CN}),
        provider_countries=frozenset({PROVIDER_JURISDICTION_CN}),
        highlight_countries=frozenset({PROVIDER_JURISDICTION_US}),
        highlight_label="US-operated routes",
        origin_heading="Models built by Chinese labs",
        provider_heading="Providers operated from China",
    ),
}

MODEL_REGION_SLUGS: tuple[str, ...] = tuple(MODEL_REGIONS)


def country_label(code: str | None) -> str:
    if code is None:
        return OPERATOR_NOT_ESTABLISHED_LABEL
    return COUNTRY_NAMES.get(code, code)


def _directory_models() -> list[Model]:
    """Catalog models a reader can actually call and inspect.

    Meta routes (aliases and orchestration presets) are excluded because their
    serving jurisdiction is the union of their component models' routes, which
    the Liberty section reports explicitly instead. Hidden-metadata models are
    excluded because their configuration is not public. A model with no
    configured endpoint is excluded because there is no route to describe.
    """
    return [
        model
        for model in MODELS.values()
        if model.id not in META_MODEL_IDS
        and not model.hidden_public_metadata
        and endpoints_for_model(model.id)
    ]


def _served_model_counts() -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for model in _directory_models():
        for slug in {endpoint.provider for endpoint in endpoints_for_model(model.id)}:
            counts[slug] += 1
    return dict(counts)


def _provider_row(provider: Provider, *, model_count: int) -> dict[str, object]:
    return {
        "slug": provider.slug,
        "name": provider.name,
        "detail_href": f"/providers/{provider.slug}",
        "country_code": provider.provider_headquarters_country,
        "country_label": country_label(provider.provider_headquarters_country),
        "privacy_tier_label": PRIVACY_TIER_LABELS[provider_privacy_tier(provider)],
        "provider_zero_data_retention": provider.provider_zero_data_retention,
        "prepaid_zero_data_retention": provider.prepaid_zero_data_retention,
        "provider_confidential_compute": provider.provider_confidential_compute,
        "provider_e2ee": provider.provider_e2ee,
        "policy_url": provider.provider_policy_url,
        "model_count": model_count,
    }


def _operator_rows(model: Model) -> list[dict[str, object]]:
    """Distinct operators of this model's configured endpoints, with the country
    recorded for each. Ordered by country name so a reader scanning the row sees
    the jurisdictions grouped, with unestablished operators last."""
    slugs = sorted({endpoint.provider for endpoint in endpoints_for_model(model.id)})
    rows: list[dict[str, object]] = [
        {
            "slug": slug,
            "name": PROVIDERS[slug].name,
            "detail_href": f"/providers/{slug}",
            "country_code": PROVIDERS[slug].provider_headquarters_country,
            "country_label": country_label(PROVIDERS[slug].provider_headquarters_country),
        }
        for slug in slugs
        if slug in PROVIDERS
    ]
    rows.sort(
        key=lambda row: (
            row["country_code"] is None,
            str(row["country_label"]),
            str(row["name"]),
        )
    )
    return rows


def _jurisdiction_chips(operators: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    """One chip per serving jurisdiction, counting operators rather than routes:
    two endpoints on the same provider are one operator in one jurisdiction."""
    counts: dict[str | None, int] = defaultdict(int)
    for operator in operators:
        code = operator["country_code"]
        counts[code if code is None else str(code)] += 1
    chips: list[dict[str, object]] = [
        {
            "country_code": code,
            "country_label": country_label(code),
            "operator_count": count,
        }
        for code, count in counts.items()
    ]
    chips.sort(
        key=lambda chip: (
            chip["country_code"] is None,
            -int(str(chip["operator_count"])),
            str(chip["country_label"]),
        )
    )
    return chips


def _model_row(model: Model, *, region: ModelRegion) -> dict[str, object]:
    endpoints = endpoints_for_model(model.id)
    operators = _operator_rows(model)
    highlighted = [
        operator
        for operator in operators
        if operator["country_code"] in region.highlight_countries
    ]
    in_region = [
        operator
        for operator in operators
        if operator["country_code"] in region.provider_countries
    ]
    return {
        "id": model.id,
        "name": model.name,
        "detail_href": f"/models/{model.id}",
        "context_length": f"{model.context_length:,}",
        "route_count": len(endpoints),
        "operators": operators[:OPERATORS_PER_MODEL],
        "operator_count": len(operators),
        "jurisdiction_chips": _jurisdiction_chips(operators),
        "highlight_operators": highlighted[:OPERATORS_PER_MODEL],
        "highlight_operator_count": len(highlighted),
        "in_region_operator_count": len(in_region),
    }


def _lab_rows(region: ModelRegion) -> list[dict[str, object]]:
    """Models from this region's labs, grouped by lab.

    Grouping key is the MODEL_ORIGINS row, so two vendor prefixes for one lab
    (Qwen and qwen, minimax and MiniMaxAI) land in one section.
    """
    grouped: dict[str, list[Model]] = defaultdict(list)
    sources: dict[str, tuple[str | None, str]] = {}
    for model in _directory_models():
        origin = model_origin_for_model_id(model.id)
        if origin is None or origin.country not in region.origin_countries:
            continue
        grouped[origin.lab_name].append(model)
        sources[origin.lab_name] = (origin.source_url, origin.note)
    rows: list[dict[str, object]] = []
    for lab_name, models in grouped.items():
        models.sort(key=lambda model: (-len(endpoints_for_model(model.id)), model.id))
        source_url, note = sources[lab_name]
        rows.append(
            {
                "lab_name": lab_name,
                "source_url": source_url,
                "note": note,
                "model_count": len(models),
                "models": [_model_row(model, region=region) for model in models[:MODELS_PER_LAB]],
                "hidden_model_count": max(0, len(models) - MODELS_PER_LAB),
            }
        )
    rows.sort(key=lambda row: (-int(str(row["model_count"])), str(row["lab_name"])))
    return rows


def liberty_component_origin_counts(model_id: str) -> dict[str | None, int]:
    """Origin countries of every component model under a route, counted.

    Recurses through meta routes because Liberty 2.0 and 3.0 list earlier
    Liberty presets among their components. A component whose vendor prefix has
    no MODEL_ORIGINS row counts under None, so a page that says "every component
    comes from a US lab" can only say it while that is arithmetically true.
    """
    counts: dict[str | None, int] = defaultdict(int)
    _accumulate_origin_counts(model_id, counts, frozenset())
    return dict(counts)


def _accumulate_origin_counts(
    model_id: str,
    counts: dict[str | None, int],
    seen: frozenset[str],
) -> None:
    if model_id in seen:
        return
    model = MODELS.get(model_id)
    if model is None:
        return
    candidates = meta_candidate_models(model_id) if model_id in META_MODEL_IDS else []
    if candidates:
        for candidate in candidates:
            _accumulate_origin_counts(candidate.id, counts, seen | {model_id})
        return
    origin = model_origin_for_model_id(model_id)
    counts[origin.country if origin is not None else None] += 1


def _liberty_component_row(component: Model) -> dict[str, object]:
    origin = model_origin_for_model_id(component.id)
    return {
        "id": component.id,
        "name": component.name,
        "detail_href": f"/models/{component.id}",
        "lab_name": origin.lab_name if origin is not None else None,
        "country_label": country_label(origin.country if origin is not None else None),
    }


def _liberty_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for model_id in LIBERTY_MODEL_IDS:
        model = MODELS.get(model_id)
        if model is None:
            continue
        counts = liberty_component_origin_counts(model_id)
        component_count = sum(counts.values())
        us_component_count = counts.get(PROVIDER_JURISDICTION_US, 0)
        components = meta_candidate_models(model_id)
        rows.append(
            {
                "id": model_id,
                "name": model.name,
                "detail_href": f"/models/{model_id}",
                "context_length": f"{model.context_length:,}",
                "component_count": component_count,
                "us_component_count": us_component_count,
                "all_components_us": component_count > 0
                and us_component_count == component_count,
                "components": [_liberty_component_row(component) for component in components],
                # True when a component is itself a Liberty preset, so the
                # component count is larger than the list of ids shown.
                "has_nested_components": component_count != len(components),
                "us_provider_available": model_us_provider_available(model),
                "prepaid": model.prepaid_available,
                "byok": model.byok_available,
            }
        )
    return rows


def model_region_evidence(slug: str) -> dict[str, object]:
    """Everything the /us-ai-models, /eu-ai-models, and /china-ai-models pages
    show, computed from the live catalog on each render."""
    region = MODEL_REGIONS[slug]
    served_counts = _served_model_counts()
    provider_rows = [
        _provider_row(provider, model_count=served_counts.get(provider.slug, 0))
        for provider in PROVIDERS.values()
        if provider.provider_headquarters_country in region.provider_countries
    ]
    provider_rows.sort(
        key=lambda row: (-int(str(row["model_count"])), str(row["name"])),
    )
    lab_rows = _lab_rows(region)
    directory_models = _directory_models()
    highlight_provider_rows = [
        _provider_row(provider, model_count=served_counts.get(provider.slug, 0))
        for provider in PROVIDERS.values()
        if provider.provider_headquarters_country in region.highlight_countries
    ]
    highlight_provider_rows.sort(
        key=lambda row: (-int(str(row["model_count"])), str(row["name"])),
    )
    return {
        "slug": region.slug,
        "country_label": region.country_label,
        "origin_heading": region.origin_heading,
        "provider_heading": region.provider_heading,
        "highlight_label": region.highlight_label,
        "as_of_label": datetime.now(UTC).strftime("%B %Y"),
        "provider_rows": provider_rows,
        "provider_count": len(provider_rows),
        "highlight_provider_count": len(highlight_provider_rows),
        "labs": lab_rows,
        "lab_count": len(lab_rows),
        "origin_model_count": sum(int(str(row["model_count"])) for row in lab_rows),
        "directory_model_count": len(directory_models),
        "catalog_provider_count": len(PROVIDERS),
        "unestablished_operator_count": sum(
            provider.provider_headquarters_country is None for provider in PROVIDERS.values()
        ),
        "models_without_recorded_origin": sum(
            model_origin_for_model_id(model.id) is None for model in directory_models
        ),
        "liberty_models": _liberty_rows(),
        "other_regions": [
            {
                "slug": other.slug,
                "href": f"/{other.slug}",
                "country_label": other.country_label,
            }
            for other in MODEL_REGIONS.values()
            if other.slug != region.slug
        ],
    }
