"""Dormant E2 routing snapshot v1; no gateway imports or activation side effects.

Compiles the catalog facts Go needs to reproduce Python's authorize candidates
and prices (docs/design/fast-admission-enclave-direct.md, E2 and §4.1) into one
versioned document with canonical bytes. Nothing at runtime imports this
module: no route, service, settings or rollout reads it yet, and the compiler
only reads module globals when a caller asks for ``CatalogInputs.live``.
The schema contract is docs/design/routing-snapshot-v1.md.

Every instant is an integer: Unix seconds, or microseconds where Python
compares sub-second values (the ranking evidence). Floats are refused; sets are
sorted; lists that Python iterates in order keep catalog order. No secret, BYOK
material, broadcast destination, prompt or custom model enters the document.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, Self, cast

from pydantic import Field, model_validator

from trusted_router.billing_snapshot import (
    MAX_INT,
    Frozen,
    Identity,
    Rates,
    Tier,
    UInt,
    canonical_bytes,
)
from trusted_router.catalog import effective_endpoint
from trusted_router.catalog_data import (
    AUTO_MODEL_ID,
    META_MODEL_IDS,
    PRIVACY_TIER_CONFIDENTIAL,
    PRIVACY_TIER_NO_STORE,
    PRIVACY_TIER_STANDARD,
    PRIVACY_TIER_ZERO_RETENTION,
    PRIVATE_PROXY_MODEL_TARGETS,
    ROUTING_MODEL_ALIAS_TARGETS,
    ROUTING_MODEL_MIN_PRIVACY_TIERS,
    US_PROVIDER_ONLY_MODEL_IDS,
    Model,
    ModelEndpoint,
    Provider,
)
from trusted_router.catalog_energy import GREEN_MODEL_ID, renewable_provider_slugs
from trusted_router.catalog_privacy import (
    endpoint_meets_privacy_requirement,
    endpoint_stores_content,
    endpoint_zero_data_retention,
)
from trusted_router.pricing import (
    SIGNED_RECEIPT_TOTAL_FEE_BASIS_POINTS,
    STANDARD_PRICE_MARKUP_BASIS_POINTS,
)
from trusted_router.provider_compat import byok_storage_provider_candidates
from trusted_router.provider_contracts import (
    BEDROCK_GATEWAY_REGIONS,
    CLAIRIC_GATEWAY_REGIONS,
    UNSUPPORTED_GATEWAY_REGIONS_BY_PROVIDER_MODEL,
)
from trusted_router.provider_lifecycle import provider_retirements
from trusted_router.provider_locations import inference_location_metadata
from trusted_router.provider_ranking import MAX_AGE, UNKNOWN_RANK, validate_snapshot
from trusted_router.stage_d import PRICING_ROUNDING, endpoint_pricing_candidate

if TYPE_CHECKING:
    from trusted_router.config import Settings

SNAPSHOT_VERSION: Final = 1
SNAPSHOT_KIND: Final = "tr-routing-snapshot"
EPOCH_SECONDS: Final = 3_600
MAX_HORIZON_SECONDS = 24 * 3_600
# The privacy requirement vocabulary (catalog_data.PRIVACY_TIER_*). Values
# outside it never match an endpoint (catalog_privacy.endpoint_meets_*).
PRIVACY_REQUIREMENTS = (
    PRIVACY_TIER_STANDARD, PRIVACY_TIER_NO_STORE,
    PRIVACY_TIER_ZERO_RETENTION, PRIVACY_TIER_CONFIDENTIAL,
)
# Literals inside model_aliases.canonical_router_model_id and
# routing.resolve_model_alias; tests hold these to those functions.
ROUTER_PREFIX_REWRITES = (("nyte/", "trustedrouter/"),)
ROUTER_BASE_REWRITES = {"trustedrouter/auto-routing": AUTO_MODEL_ID}
BARE_VENDOR_PREFIX = "openai/"
DATED_SPELLING_SUFFIX = "-2000-01-01"
BEDROCK_PROVIDER = "amazon-bedrock"
WAFER_PROVIDER = "wafer"

Int64 = Annotated[int, Field(strict=True, ge=-(1 << 63), le=MAX_INT)]
Flag = Annotated[bool, Field(strict=True)]
Text = Annotated[str, Field(max_length=4_096)]
Identities = tuple[Identity, ...]
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class SnapshotRefused(ValueError):
    """The catalog cannot be frozen faithfully for this window; compile nothing."""


def _sorted_unique(values: Sequence[Any], name: str) -> None:
    if list(values) != sorted(set(values)):
        raise ValueError(f"{name} must be sorted and unique")


class Plane(Frozen):
    cloud: Identity
    region: Identity


class RegionEntry(Frozen):
    """One row of regions.region_payload: the authorize response's `regions`."""

    id: Identity
    name: Identity
    primary: Flag
    enabled: Flag
    api_base_url: Text
    control_plane_url: None


class StageDSettings(Frozen):
    eligibility_enabled: Flag
    heartbeat_enabled: Flag
    require_heartbeat_declaration: Flag
    pilot_workspaces: Identities

    @model_validator(mode="after")
    def sorted_pilots(self) -> Self:
        _sorted_unique(self.pilot_workspaces, "pilot_workspaces")
        return self


class SnapshotSettings(Frozen):
    """The allowlisted Settings values authorize reads; nothing else."""

    regions: Identities = Field(min_length=1)
    primary_region: Identity
    region_payload: tuple[RegionEntry, ...]
    stage_d: StageDSettings
    request_record_write_mode: Literal["legacy", "typed"]

    @model_validator(mode="after")
    def primary_is_configured(self) -> Self:
        if self.primary_region not in self.regions or len(set(self.regions)) != len(self.regions):
            raise ValueError("regions must be unique and include primary_region")
        return self


class ProviderEntry(Frozen):
    slug: Identity
    name: Text
    headquarters_country: Identity | None
    byok_storage_slugs: Identities = Field(min_length=1)


class ModelEntry(Frozen):
    id: Identity
    provider: Identity
    supports_chat: Flag
    supports_embeddings: Flag
    supports_decide: Flag


class Privacy(Frozen):
    stores_content: Flag
    meets: tuple[UInt, ...]

    @model_validator(mode="after")
    def sorted_meets(self) -> Self:
        _sorted_unique(self.meets, "meets")
        return self


class GatewayRegions(Frozen):
    only: Identities | None
    deny: Identities
    requires_explicit_origin: Flag


class Headline(Frozen):
    prompt: UInt
    completion: UInt


class Price(Frozen):
    """Exactly stage_d.endpoint_pricing_candidate: the frozen Stage D candidate."""

    endpoint_id: Identity
    price_history_version: Literal[1]
    rates: Rates
    tiers: tuple[Tier, ...] = Field(max_length=64)
    request_fee_micro: UInt
    rounding: Literal["half_up_per_million"]


class Epoch(Frozen):
    starts_at: UInt
    headline: Headline
    price: Price


class InferenceLocation(Frozen):
    """Exactly provider_locations.inference_location_metadata(provider, model)."""

    advertised_regions: tuple[Text, ...]
    advertised_region_scope: Text
    catalog_updated_at: Text | None
    provider_declared_locations: tuple[Text, ...]
    provider_declaration_reviewed_on: Text | None
    serving_region: None
    serving_region_status: Text
    region_pinning_enforced: Flag
    documentation_url: Text | None


class EndpointResponse(Frozen):
    inference_location: InferenceLocation
    wafer_zdr_required: Flag


class EndpointEntry(Frozen):
    id: Identity
    model_id: Identity
    provider: Identity
    usage_type: Literal["Credits", "BYOK"]
    upstream_model: Identity
    supported_parameters: Identities
    input_modalities: Identities = Field(min_length=1)
    privacy: Privacy
    gateway_regions: GatewayRegions
    catalog_valid_until: Int64 | None
    retired_at: Int64 | None
    epochs: tuple[Epoch, ...] = Field(min_length=1)
    response: EndpointResponse

    @model_validator(mode="after")
    def consistent(self) -> Self:
        _sorted_unique(self.supported_parameters, "supported_parameters")
        _sorted_unique(self.input_modalities, "input_modalities")
        starts = [epoch.starts_at for epoch in self.epochs]
        if starts != sorted(set(starts)) or any(
            epoch.price.endpoint_id != self.id for epoch in self.epochs
        ):
            raise ValueError("epochs must increase and price this endpoint")
        if any(a.headline == b.headline and a.price == b.price
               for a, b in zip(self.epochs, self.epochs[1:], strict=False)):
            raise ValueError("adjacent epochs must differ")
        return self


class PrefixRewrite(Frozen):
    prefix: Identity
    replacement: Identity


class VariantSuffix(Frozen):
    suffix: Identity
    preference: Literal["sort"]
    value: Identity


class Aliases(Frozen):
    router_prefix_rewrites: tuple[PrefixRewrite, ...]
    router_base_rewrites: dict[Identity, Identity]
    alias_targets: dict[Identity, Identity]
    dated_suffix_pattern: Text
    bare_vendor_prefix: Identity
    variant_suffixes: tuple[VariantSuffix, ...]
    provider_aliases: dict[Identity, Identity]
    provider_group_aliases: dict[Identity, Identities]
    router_provider_slugs: Identities

    @model_validator(mode="after")
    def sorted_router_slugs(self) -> Self:
        _sorted_unique(self.router_provider_slugs, "router_provider_slugs")
        return self


class RequestedOverrides(Frozen):
    """Assignments one requested model makes to the route preferences.

    None means "not assigned". privacy_requirements accumulate across a
    request's models; every other field is assigned in model order.
    """

    usage: Literal["Credits"] | None
    renewable_only: Identities | None
    provider_jurisdiction: Identity | None
    privacy_requirements: tuple[UInt, ...]
    order: Identities | None
    only: Identities | None

    @model_validator(mode="after")
    def sorted_sets(self) -> Self:
        _sorted_unique(self.privacy_requirements, "privacy_requirements")
        if self.renewable_only is not None:
            _sorted_unique(self.renewable_only, "renewable_only")
        return self


NO_OVERRIDES = RequestedOverrides(
    usage=None, renewable_only=None, provider_jurisdiction=None,
    privacy_requirements=(), order=None, only=None,
)


class RequestedModel(Frozen):
    """A canonical requested id whose meaning is not "itself, no overrides"."""

    id: Identity
    expands_to: Identities
    refused_status: UInt | None
    private_proxy: Flag
    overrides: RequestedOverrides

    @model_validator(mode="after")
    def refusal_is_empty(self) -> Self:
        if self.refused_status is not None and (self.expands_to or self.overrides != NO_OVERRIDES):
            raise ValueError("a refused model expands to nothing")
        return self


class Ranking(Frozen):
    measured_from_us: Int64
    measured_through_us: Int64
    unknown_rank: UInt
    default: dict[Identity, UInt]
    latency: dict[Identity, UInt]
    throughput: dict[Identity, UInt]
    prepaid_min_rank: dict[Identity, UInt]
    model_provider_preference: dict[Identity, dict[Identity, Int64]]


class Fees(Frozen):
    standard_markup_bps: UInt
    signed_receipt_total_fee_bps: UInt
    receipt_rounding: Literal["ceil_ratio"]
    token_rounding: Literal["half_up_per_million"]


class RoutingSnapshotV1(Frozen):
    v: Literal[1]
    kind: Literal["tr-routing-snapshot"]
    compiled_at: UInt
    not_after: UInt
    epoch_seconds: Literal[3600]
    source_revision: Identity
    plane: Plane
    settings: SnapshotSettings
    providers: tuple[ProviderEntry, ...]
    models: tuple[ModelEntry, ...]
    endpoints: tuple[EndpointEntry, ...]
    aliases: Aliases
    requested_models: tuple[RequestedModel, ...]
    decision_chains: dict[Identity, Identities]
    ranking: Ranking
    fees: Fees

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if not self.compiled_at < self.not_after <= self.compiled_at + MAX_HORIZON_SECONDS:
            raise ValueError("not_after must follow compiled_at within the horizon")
        _sorted_unique([p.slug for p in self.providers], "providers")
        _sorted_unique([r.id for r in self.requested_models], "requested_models")
        providers = {p.slug for p in self.providers}
        model_ids = [m.id for m in self.models]
        endpoint_ids = [e.id for e in self.endpoints]
        if len(set(model_ids)) != len(model_ids) or len(set(endpoint_ids)) != len(endpoint_ids):
            raise ValueError("model and endpoint ids must be unique")
        models = set(model_ids)
        for model in self.models:
            if model.provider not in providers:
                raise ValueError("model provider is not in providers")
        for endpoint in self.endpoints:
            if endpoint.model_id not in models or endpoint.provider not in providers:
                raise ValueError("endpoint references an unknown model or provider")
            if endpoint.epochs[0].starts_at != self.compiled_at or (
                endpoint.epochs[-1].starts_at >= self.not_after
            ):
                raise ValueError("epochs must cover compiled_at and end before not_after")
        for requested in self.requested_models:
            if not set(requested.expands_to) <= models:
                raise ValueError("requested model expands to an unknown model")
        for chain in self.decision_chains.values():
            if not chain or not set(chain) <= providers:
                raise ValueError("decision chain names an unknown provider")
        return self


@dataclass(frozen=True)
class CatalogInputs:
    """Everything the compiler reads. ``live`` gathers it from module globals.

    ``catalog_resolved_at`` is the lifecycle instant the registry was built
    at (``catalog_registry.CATALOG_RESOLVED_AT``): the import drops endpoints
    and models already retired then. ``expansions_clock`` is the catalog clock
    (``catalog_data._utc_now``) before and after ``requested_models`` were
    computed: the privacy pools behind trustedrouter/zdr, eu, e2e and green
    read catalog currency on that clock, not on any ``at``.

    ``providers`` must be the global ``catalog_data.PROVIDERS`` entries: the
    privacy, BYOK-slug and inference-location rules read the globals, so a
    different provider would make ``providers[]`` disagree with ``endpoints[]``.
    """

    providers: Mapping[str, Provider]
    models: Sequence[Model]
    endpoints: Sequence[ModelEndpoint]
    requested_models: Sequence[RequestedModel]
    catalog_resolved_at: int
    expansions_clock: tuple[int, int]
    aliases: Aliases
    decision_chains: Mapping[str, Sequence[str]]
    ranking_evidence: Mapping[str, Any]
    prepaid_min_rank: Mapping[str, int]
    model_provider_preference: Mapping[str, Mapping[str, int]]

    @classmethod
    def live(cls, settings: Settings) -> CatalogInputs:
        from trusted_router import catalog_data, provider_ranking, routing
        from trusted_router.catalog_registry import CATALOG_RESOLVED_AT, MODEL_ENDPOINTS, MODELS

        before = _floor_seconds(catalog_data._utc_now())
        requested = live_requested_models(settings)
        after = _floor_seconds(catalog_data._utc_now())
        evidence_path = Path(provider_ranking.__file__).with_name("data") / (
            "provider_routing_evidence.json"
        )
        return cls(
            providers=dict(catalog_data.PROVIDERS),
            models=tuple(MODELS.values()),
            endpoints=tuple(MODEL_ENDPOINTS.values()),
            requested_models=requested,
            catalog_resolved_at=_floor_seconds(CATALOG_RESOLVED_AT),
            expansions_clock=(before, after),
            aliases=live_aliases(),
            decision_chains=dict(catalog_data.NAMED_DECISION_MODEL_PROVIDERS),
            ranking_evidence=json.loads(evidence_path.read_text()),
            prepaid_min_rank=dict(routing._PREPAID_PROVIDER_MIN_RANK),
            model_provider_preference={
                model: dict(preference)
                for model, preference in routing._MODEL_PROVIDER_PREFERENCE.items()
            },
        )


def live_aliases() -> Aliases:
    from trusted_router import routing

    return Aliases(
        router_prefix_rewrites=tuple(
            PrefixRewrite(prefix=prefix, replacement=replacement)
            for prefix, replacement in ROUTER_PREFIX_REWRITES
        ),
        router_base_rewrites=dict(ROUTER_BASE_REWRITES),
        alias_targets=dict(ROUTING_MODEL_ALIAS_TARGETS),
        dated_suffix_pattern=routing._DATED_SNAPSHOT_RE.pattern,
        bare_vendor_prefix=BARE_VENDOR_PREFIX,
        variant_suffixes=tuple(
            _variant_suffix(suffix, field, value)
            for suffix, (field, value) in routing._VARIANT_SUFFIXES.items()
        ),
        provider_aliases=dict(routing._PROVIDER_ALIASES),
        provider_group_aliases={
            alias: tuple(slugs) for alias, slugs in routing._PROVIDER_GROUP_ALIASES.items()
        },
        router_provider_slugs=tuple(sorted(routing._ROUTER_PROVIDER_SLUGS)),
    )


def _variant_suffix(suffix: str, preference: str, value: str) -> VariantSuffix:
    if preference != "sort":
        raise SnapshotRefused(f"variant suffix {suffix} sets an unknown preference")
    return VariantSuffix(suffix=suffix, preference=cast(Literal["sort"], preference), value=value)


_OVERRIDE_KEYS = frozenset({
    "usage", "renewable_energy_only", "provider_jurisdiction",
    "privacy_requirements", "order", "only", "pool_model_ids",
})


def _provider_list(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def live_requested_models(settings: Settings) -> tuple[RequestedModel, ...]:
    """Ask routing what each special requested id means, under these settings.

    Every catalog id outside the result means itself with no overrides; the
    property tests hold the result to routing._requested_model_ids.
    """
    from fastapi import HTTPException

    from trusted_router import routing
    from trusted_router.catalog_registry import MODELS

    candidates = (
        set(META_MODEL_IDS) | set(ROUTING_MODEL_MIN_PRIVACY_TIERS)
        | set(PRIVATE_PROXY_MODEL_TARGETS) | set(US_PROVIDER_ONLY_MODEL_IDS)
        | set(ROUTING_MODEL_ALIAS_TARGETS.values()) | {GREEN_MODEL_ID, AUTO_MODEL_ID}
        # An alias key is a canonical id of its own only when it is a model.
        | (set(ROUTING_MODEL_ALIAS_TARGETS) & set(MODELS))
    )
    candidates |= {m for m in MODELS if routing._expand_model_id(m, settings) != [m]}
    # Ask about each id beside a plain one, as in a `models` list: alone, an id
    # whose pool is empty would read as the request-level "model is required".
    companion = next((
        m for m in sorted(MODELS) if m not in candidates and routing.resolve_model_alias(m) == m
        and routing._requested_model_ids({"model": m}, settings) == ([m], {})
    ), None)
    if companion is None:
        raise SnapshotRefused("no plain catalog model to expand beside")
    out: list[RequestedModel] = []
    for model_id in sorted(candidates | {routing.resolve_model_alias(c) for c in candidates}):
        spelling = model_id
        if routing.resolve_model_alias(model_id) != model_id:
            # An alias key that is also a catalog id is still reached by its
            # dated spelling, which resolve_model_alias does not follow.
            spelling = model_id + DATED_SPELLING_SUFFIX
            if routing.resolve_model_alias(spelling) != model_id:
                raise SnapshotRefused(f"no spelling reaches {model_id}")
        try:
            ids, overrides = routing._requested_model_ids(
                {"models": [spelling, companion]}, settings,
            )
        except HTTPException as exc:
            out.append(RequestedModel(
                id=model_id, expands_to=(), refused_status=exc.status_code,
                private_proxy=model_id in PRIVATE_PROXY_MODEL_TARGETS, overrides=NO_OVERRIDES,
            ))
            continue
        unknown = set(overrides) - _OVERRIDE_KEYS
        if unknown or ids[-1:] != [companion]:
            raise SnapshotRefused(f"unexpected expansion of {model_id}: {sorted(unknown)}")
        # Pool membership is derived from expands_to, not an independent
        # schema field. Refuse an override that cannot be reconstructed.
        expected_pool = set(ids[:-1]) - {companion} if ids[:-1] != [model_id] else set()
        if set(overrides.get("pool_model_ids", ())) != expected_pool:
            raise SnapshotRefused(f"unexpected pool membership for {model_id}")
        out.append(RequestedModel(
            id=model_id,
            expands_to=tuple(ids[:-1]),
            refused_status=None,
            private_proxy=model_id in PRIVATE_PROXY_MODEL_TARGETS,
            overrides=RequestedOverrides(
                usage=overrides.get("usage"),
                renewable_only=(
                    tuple(sorted(renewable_provider_slugs()))
                    if overrides.get("renewable_energy_only") else None
                ),
                provider_jurisdiction=overrides.get("provider_jurisdiction"),
                privacy_requirements=tuple(sorted(overrides.get("privacy_requirements", ()))),
                order=_provider_list(overrides["order"]) if "order" in overrides else None,
                only=_provider_list(overrides["only"]) if "only" in overrides else None,
            ),
        ))
    return tuple(out)


def snapshot_settings_from(settings: Settings) -> SnapshotSettings:
    from trusted_router.regions import configured_regions, region_payload

    return SnapshotSettings(
        regions=tuple(configured_regions(settings)),
        primary_region=settings.primary_region,
        region_payload=tuple(RegionEntry(**row) for row in region_payload(settings)),
        stage_d=StageDSettings(
            eligibility_enabled=settings.stage_d_eligibility_enabled,
            heartbeat_enabled=settings.stage_d_heartbeat_enabled,
            require_heartbeat_declaration=settings.stage_d_require_heartbeat_declaration,
            pilot_workspaces=tuple(sorted(settings.stage_d_pilot_workspaces)),
        ),
        request_record_write_mode=settings.request_record_write_mode,  # type: ignore[arg-type]
    )


def _floor_seconds(instant: datetime) -> int:
    return (instant.astimezone(UTC) - _EPOCH) // timedelta(seconds=1)


def _exact_seconds(instant: datetime) -> int:
    """Exact Unix seconds; a sub-second instant cannot be compared in seconds."""
    if instant.tzinfo is None or instant.microsecond:
        raise SnapshotRefused("catalog instants must be timezone-aware whole seconds")
    return _floor_seconds(instant)


def _seconds(instant: datetime | None) -> int | None:
    return None if instant is None else _exact_seconds(instant)


def _micros(instant: datetime) -> int:
    return (instant.astimezone(UTC) - _EPOCH) // timedelta(microseconds=1)


def _instant(seconds: int) -> datetime:
    return _EPOCH + timedelta(seconds=seconds)


def _retired_at(endpoint: ModelEndpoint) -> int | None:
    """provider_model_retired(…, at=t) is exactly t >= this instant."""
    matches = [
        _exact_seconds(retirement.effective_at)
        for retirement in provider_retirements()
        if retirement.provider == endpoint.provider and (
            endpoint.model_id in retirement.model_ids
            or (endpoint.upstream_id is not None and endpoint.upstream_id in retirement.upstream_ids)
        )
    ]
    return min(matches) if matches else None


def _gateway_regions(endpoint: ModelEndpoint) -> GatewayRegions:
    if endpoint.provider == BEDROCK_PROVIDER:
        return GatewayRegions(
            only=tuple(sorted(BEDROCK_GATEWAY_REGIONS)), deny=(), requires_explicit_origin=True,
        )
    if (endpoint.provider, endpoint.model_id) == ("cloudflare-workers-ai", "cloudflare/clairic"):
        return GatewayRegions(
            only=tuple(sorted(CLAIRIC_GATEWAY_REGIONS)), deny=(), requires_explicit_origin=True,
        )
    return GatewayRegions(
        only=None,
        deny=tuple(sorted(UNSUPPORTED_GATEWAY_REGIONS_BY_PROVIDER_MODEL.get(
            (endpoint.provider, endpoint.model_id), frozenset(),
        ))),
        requires_explicit_origin=False,
    )


def _epoch_starts(at: int, not_after: int) -> list[int]:
    starts = [at]
    boundary = (at // EPOCH_SECONDS + 1) * EPOCH_SECONDS
    while boundary < not_after:
        starts.append(boundary)
        boundary += EPOCH_SECONDS
    return starts


def _refuse_off_hour_price_changes(
    endpoints: Iterable[ModelEndpoint], starts: list[int], not_after: int,
) -> None:
    """Fail closed if any price moves between two epoch boundaries.

    effective_endpoint depends on time only through the provider and model, so
    one endpoint per pair stands for all of them. Minute resolution: a guard on
    the hourly assumption, not a proof that no price moves within a minute.
    """
    bounds = [*starts, not_after]
    windows = [
        (_instant(start), [_instant(minute) for minute in range(
            (start // 60 + 1) * 60, end, 60)])
        for start, end in zip(bounds, bounds[1:], strict=False)
    ]
    representatives: dict[tuple[str, str], ModelEndpoint] = {}
    for endpoint in endpoints:
        representatives.setdefault((endpoint.provider, endpoint.model_id), endpoint)
    for endpoint in representatives.values():
        for start, minutes in windows:
            base = effective_endpoint(endpoint, at=start)
            for minute in minutes:
                if effective_endpoint(endpoint, at=minute) != base:
                    raise SnapshotRefused(
                        f"price of {endpoint.provider}:{endpoint.model_id} changes "
                        f"off the hour at {minute.isoformat()}"
                    )


def _epochs(endpoint: ModelEndpoint, starts: list[int]) -> tuple[Epoch, ...]:
    epochs: list[Epoch] = []
    for start in starts:
        effective = effective_endpoint(endpoint, at=_instant(start))
        price_dict = endpoint_pricing_candidate(effective)
        price = Price.model_validate(price_dict)
        if price.model_dump(mode="json") != json.loads(json.dumps(price_dict)):
            raise SnapshotRefused("pricing candidate is not representable exactly")
        headline = Headline(
            prompt=effective.prompt_price_microdollars_per_million_tokens,
            completion=effective.completion_price_microdollars_per_million_tokens,
        )
        if epochs and epochs[-1].headline == headline and epochs[-1].price == price:
            continue
        epochs.append(Epoch(starts_at=start, headline=headline, price=price))
    return tuple(epochs)


def _endpoint_entry(endpoint: ModelEndpoint, starts: list[int]) -> EndpointEntry:
    return EndpointEntry(
        id=endpoint.id,
        model_id=endpoint.model_id,
        provider=endpoint.provider,
        usage_type=endpoint.usage_type,  # type: ignore[arg-type]
        upstream_model=endpoint.upstream_id or endpoint.model_id,
        supported_parameters=tuple(sorted(set(endpoint.supported_parameters))),
        input_modalities=tuple(sorted(set(endpoint.input_modalities or ("text",)))),
        privacy=Privacy(
            stores_content=endpoint_stores_content(endpoint),
            meets=tuple(
                requirement for requirement in PRIVACY_REQUIREMENTS
                if endpoint_meets_privacy_requirement(endpoint, requirement)
            ),
        ),
        gateway_regions=_gateway_regions(endpoint),
        catalog_valid_until=_seconds(endpoint.catalog_valid_until),
        retired_at=_retired_at(endpoint),
        epochs=_epochs(endpoint, starts),
        response=EndpointResponse(
            inference_location=InferenceLocation.model_validate(
                inference_location_metadata(endpoint.provider, endpoint.model_id)
            ),
            wafer_zdr_required=(
                endpoint.provider == WAFER_PROVIDER
                and endpoint_zero_data_retention(endpoint) is True
            ),
        ),
    )


def _ranking(inputs: CatalogInputs) -> Ranking:
    generated_at, ranks = validate_snapshot(inputs.ranking_evidence)
    return Ranking(
        measured_from_us=_micros(generated_at),
        measured_through_us=_micros(generated_at + MAX_AGE),
        unknown_rank=UNKNOWN_RANK,
        default=dict(ranks["default"]),
        latency=dict(ranks["latency"]),
        throughput=dict(ranks["throughput"]),
        prepaid_min_rank=dict(inputs.prepaid_min_rank),
        model_provider_preference={
            model: dict(preference) for model, preference in inputs.model_provider_preference.items()
        },
    )


def _clamp(at: int, horizon: int, events: set[int], low: int, high: int, what: str) -> int:
    """Refuse an event in (low, high]; end the window at the first event after high."""
    if any(low < event <= high for event in events):
        raise SnapshotRefused(f"a {what} falls between {low} and {high}")
    return min([at + horizon, *(event for event in events if event > high)])


def _not_after(at: int, horizon: int, inputs: CatalogInputs) -> int:
    """Clamp the window so the imported catalog and the expansions hold on it.

    The registry drops what is retired at its import instant, so a retirement
    in (at, catalog_resolved_at] would remove endpoints still available at
    `at`. Retirements before the import stay in the registry, carried with
    their exact retired_at. The privacy pools read each endpoint's catalog
    currency on the catalog clock, so they are constant on the window only if
    no catalog deadline falls between that clock and any of its instants.
    """
    retirements = {_exact_seconds(r.effective_at) for r in provider_retirements()}
    resolved = inputs.catalog_resolved_at
    not_after = _clamp(at, horizon, retirements, at, max(at, resolved), "retirement")
    deadlines = {
        deadline for endpoint in inputs.endpoints
        if (deadline := _seconds(endpoint.catalog_valid_until)) is not None
    }
    low = min(inputs.expansions_clock[0], at)
    high = max(inputs.expansions_clock[1], at)
    return min(not_after, _clamp(at, horizon, deadlines, low, high, "catalog deadline"))


def _require_global_providers(inputs: CatalogInputs) -> None:
    from trusted_router.catalog_data import PROVIDERS

    for slug, provider in inputs.providers.items():
        if PROVIDERS.get(slug) != provider:
            raise SnapshotRefused(f"provider {slug} is not the catalog's provider")


def compile_routing_snapshot(
    *,
    at: int,
    horizon: int,
    settings: SnapshotSettings,
    source_revision: str,
    plane: Plane,
    inputs: CatalogInputs,
) -> RoutingSnapshotV1:
    """Freeze the catalog for [at, not_after); refuse rather than approximate."""
    if type(at) is not int or type(horizon) is not int or at < 0:
        raise SnapshotRefused("at and horizon must be integer seconds")
    if not 0 < horizon <= MAX_HORIZON_SECONDS:
        raise SnapshotRefused("horizon is out of range")
    _require_global_providers(inputs)
    not_after = _not_after(at, horizon, inputs)
    starts = _epoch_starts(at, not_after)
    _refuse_off_hour_price_changes(inputs.endpoints, starts, not_after)
    return RoutingSnapshotV1(
        v=SNAPSHOT_VERSION,
        kind=SNAPSHOT_KIND,
        compiled_at=at,
        not_after=not_after,
        epoch_seconds=EPOCH_SECONDS,
        source_revision=source_revision,
        plane=plane,
        settings=settings,
        providers=tuple(
            ProviderEntry(
                slug=slug,
                name=provider.name,
                headquarters_country=provider.provider_headquarters_country,
                byok_storage_slugs=byok_storage_provider_candidates(slug),
            )
            for slug, provider in sorted(inputs.providers.items())
        ),
        models=tuple(
            ModelEntry(
                id=model.id, provider=model.provider, supports_chat=model.supports_chat,
                supports_embeddings=model.supports_embeddings,
                supports_decide=model.supports_decide,
            )
            for model in inputs.models
        ),
        endpoints=tuple(_endpoint_entry(endpoint, starts) for endpoint in inputs.endpoints),
        aliases=inputs.aliases,
        requested_models=tuple(sorted(inputs.requested_models, key=lambda r: r.id)),
        decision_chains={model: tuple(chain) for model, chain in inputs.decision_chains.items()},
        ranking=_ranking(inputs),
        fees=Fees(
            standard_markup_bps=STANDARD_PRICE_MARKUP_BASIS_POINTS,
            signed_receipt_total_fee_bps=SIGNED_RECEIPT_TOTAL_FEE_BASIS_POINTS,
            receipt_rounding="ceil_ratio",
            token_rounding=PRICING_ROUNDING,  # type: ignore[arg-type]
        ),
    )


def canonical_snapshot_bytes(snapshot: RoutingSnapshotV1) -> bytes:
    """ASCII JSON, lexical keys, compact separators, no trailing newline."""
    return canonical_bytes(snapshot)


def snapshot_digest(snapshot: RoutingSnapshotV1) -> str:
    return hashlib.sha256(canonical_snapshot_bytes(snapshot)).hexdigest()


def parse_routing_snapshot(raw: bytes | str) -> RoutingSnapshotV1:
    return RoutingSnapshotV1.model_validate_json(raw)
