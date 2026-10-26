"""Live-catalog properties of the dormant routing snapshot (docs/design/routing-snapshot-v1.md).

Each test compiles the session's real catalog and holds one part of the
snapshot to the Python function authorize calls today, through a small
reference reader written against the snapshot alone. Nothing is committed:
the golden files are tests/test_fastpath_routing_snapshot_vectors.py's.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import typing
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import BaseModel

from trusted_router import catalog_data, catalog_registry, provider_ranking, routing
from trusted_router.catalog import effective_endpoint
from trusted_router.catalog_privacy import (
    endpoint_meets_privacy_requirement,
    endpoint_stores_content,
)
from trusted_router.config import Settings
from trusted_router.enclave_regions import ENCLAVE_REGIONS
from trusted_router.pricing import (
    SIGNED_RECEIPT_TOTAL_FEE_BASIS_POINTS,
    STANDARD_PRICE_MARKUP_BASIS_POINTS,
    signed_receipt_price_microdollars,
)
from trusted_router.provider_compat import byok_storage_provider_candidates
from trusted_router.provider_contracts import (
    BEDROCK_GATEWAY_REGIONS,
    UNSUPPORTED_GATEWAY_REGIONS_BY_PROVIDER_MODEL,
    provider_model_available_from_gateway_region,
)
from trusted_router.provider_lifecycle import provider_model_retired
from trusted_router.provider_locations import inference_location_metadata
from trusted_router.regions import GCP_REGION_GEO, choose_region, region_payload
from trusted_router.routing_snapshot import (
    EPOCH_SECONDS,
    CatalogInputs,
    Plane,
    RoutingSnapshotV2,
    canonical_snapshot_bytes,
    compile_routing_snapshot,
    snapshot_settings_from,
)
from trusted_router.stage_d import endpoint_pricing_candidate

ROOT = Path(__file__).resolve().parents[1]
# Subprocess compiles pin both clocks here, so the window and the import agree.
FIXED_AT = int(datetime(2026, 10, 19, tzinfo=UTC).timestamp())
HORIZON = 6 * EPOCH_SECONDS
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# The session's registry was built at the pinned lifecycle instant; compiling
# at it is the window that instant's catalog is complete for.
LIVE_AT = (catalog_registry.CATALOG_RESOLVED_AT.astimezone(UTC) - EPOCH) // timedelta(seconds=1)


def _instant(seconds: int) -> datetime:
    return EPOCH + timedelta(seconds=seconds)


def _compile(settings: Settings, inputs: CatalogInputs) -> RoutingSnapshotV2:
    return compile_routing_snapshot(
        at=LIVE_AT, horizon=HORIZON, settings=snapshot_settings_from(settings),
        source_revision="test", plane=Plane(cloud="gcp", region="us-central1"), inputs=inputs,
    )


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings(environment="test")


@pytest.fixture(scope="module")
def catalog_clock() -> Iterator[None]:
    """Judge catalog freshness at LIVE_AT for this module, as production does at now.

    tests/catalog_freshness_freeze.py pins the catalog clock to 2000 for the
    session while the lifecycle clock is the session start (or, in CI's
    post-cutover job, past every cutover). The compiler rightly refuses a
    manifest deadline between the two, so this module puts both clocks at one
    instant; the routing functions the tests compare against see it too.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(catalog_data, "_utc_now", lambda: _instant(LIVE_AT))
        yield


@pytest.fixture(scope="module")
def live(settings: Settings, catalog_clock: None) -> tuple[RoutingSnapshotV2, CatalogInputs]:
    inputs = CatalogInputs.live(settings)
    assert inputs.catalog_resolved_at == LIVE_AT
    return _compile(settings, inputs), inputs


def _as_json(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _epoch_at(entry: Any, t: int) -> Any:
    return [epoch for epoch in entry.epochs if epoch.starts_at <= t][-1]


def test_the_snapshot_lists_the_catalog_in_catalog_order(live) -> None:
    snapshot, inputs = live
    assert [e.id for e in snapshot.endpoints] == [e.id for e in inputs.endpoints]
    assert [m.id for m in snapshot.models] == [m.id for m in inputs.models]
    assert len(snapshot.endpoints) > 1_000 and len(snapshot.models) > 300
    assert [p.slug for p in snapshot.providers] == sorted(catalog_data.PROVIDERS)


def test_every_epoch_price_is_the_effective_endpoint_price_at_that_instant(live) -> None:
    snapshot, inputs = live
    raw = {e.id: e for e in inputs.endpoints}
    hours = range(snapshot.compiled_at, snapshot.not_after, EPOCH_SECONDS)
    coarse = sorted({*hours, *(h + 1_800 for h in hours), snapshot.not_after - 1})
    fine = range(snapshot.compiled_at, snapshot.not_after, 900)
    scheduled = changing = 0
    for entry in snapshot.endpoints:
        endpoint = raw[entry.id]
        scheduled += len(entry.epochs) > 1
        start = effective_endpoint(endpoint, at=_instant(snapshot.compiled_at))
        changing += any(effective_endpoint(endpoint, at=_instant(t)) != start for t in fine)
        for t in fine if len(entry.epochs) > 1 else coarse:
            epoch = _epoch_at(entry, t)
            effective = effective_endpoint(endpoint, at=_instant(t))
            assert epoch.price.model_dump(mode="json") == _as_json(
                endpoint_pricing_candidate(effective)
            ), (entry.id, t)
            assert (epoch.headline.prompt, epoch.headline.completion) == (
                effective.prompt_price_microdollars_per_million_tokens,
                effective.completion_price_microdollars_per_million_tokens,
            ), (entry.id, t)
    # An endpoint lists several epochs exactly when its price moves in the window.
    # (tests/test_fastpath_routing_snapshot_vectors.py holds a window with real peaks.)
    assert scheduled == changing


def test_availability_is_the_catalog_deadline_and_the_retirement(live) -> None:
    snapshot, inputs = live
    raw = {e.id: e for e in inputs.endpoints}
    edged = 0
    for entry in snapshot.endpoints:
        endpoint = raw[entry.id]
        edges = [v for v in (entry.catalog_valid_until, entry.retired_at) if v is not None]
        edged += bool(edges)
        times = {snapshot.compiled_at, snapshot.not_after - 1,
                 *(edge + d for edge in edges for d in (-1, 0, 1))}
        for t in sorted(t for t in times if t > 0):
            instant = _instant(t)
            expected = endpoint.catalog_is_current(at=instant) and not provider_model_retired(
                endpoint.provider, endpoint.model_id, endpoint.upstream_id, at=instant,
            )
            available = (
                (entry.catalog_valid_until is None or t < entry.catalog_valid_until)
                and (entry.retired_at is None or t < entry.retired_at)
            )
            assert available == expected, (entry.id, t)
    # Not vacuous: deadline edges were compared. (Whether any retirement is
    # still ahead depends on the lifecycle clock; CI's post-cutover job pins it
    # past every one. The subprocess retirement test covers retired_at.)
    assert edged > 0


def test_privacy_is_what_the_routing_filters_ask(live) -> None:
    snapshot, inputs = live
    raw = {e.id: e for e in inputs.endpoints}
    for entry in snapshot.endpoints:
        endpoint = raw[entry.id]
        assert entry.privacy.stores_content == endpoint_stores_content(endpoint)
        for requirement in range(-1, 6):
            assert (requirement in entry.privacy.meets) == endpoint_meets_privacy_requirement(
                endpoint, requirement
            ), (entry.id, requirement)
    assert any(3 in e.privacy.meets for e in snapshot.endpoints)


def test_gateway_regions_agree_with_the_provider_contract(live, settings) -> None:
    snapshot, inputs = live
    raw = {e.id: e for e in inputs.endpoints}
    regions = {
        "", "unknown-region", *snapshot.settings.regions, *ENCLAVE_REGIONS, *GCP_REGION_GEO,
        *BEDROCK_GATEWAY_REGIONS, *(r for rs in UNSUPPORTED_GATEWAY_REGIONS_BY_PROVIDER_MODEL.values() for r in rs),
    }
    for entry in snapshot.endpoints:
        endpoint = raw[entry.id]
        rule = entry.gateway_regions
        assert rule.requires_explicit_origin == (
            endpoint.provider == "amazon-bedrock"
            or (endpoint.provider, endpoint.model_id) in {
                ("cloudflare-workers-ai", "cloudflare/clairic"),
                ("regolo", "cloudflare/clef-flash"),
            }
        )
        for region in sorted(regions):
            available = (rule.only is None or region in rule.only) and region not in rule.deny
            assert available == provider_model_available_from_gateway_region(
                endpoint.provider, endpoint.model_id, region,
            ), (entry.id, region)


def _gateway_region(settings: Any, rule: Any, body_region: str | None) -> str:
    """The region authorize checks: the request's own, even when unconfigured."""
    chosen = body_region if body_region in settings.regions else settings.primary_region
    return body_region or ("" if rule.requires_explicit_origin else chosen)


def test_the_request_region_is_checked_even_when_it_is_not_configured(live) -> None:
    import inspect

    from trusted_router import authorize_candidates
    from trusted_router.catalog_data import ModelEndpoint
    from trusted_router.provider_contracts import SAKANA_FUGU_MODEL_ID
    from trusted_router.routes.internal import gateway
    from trusted_router.routing_snapshot import _gateway_regions

    # The rule this test models, as authorize writes it today: the gateway
    # chooses the region, and the candidate pipeline checks the request's own.
    source = inspect.getsource(gateway._authorize_gateway_sync_impl)
    assert "region = choose_region(settings, body.region or None)" in source
    assert "body_region=body.region," in source
    pipeline = inspect.getsource(authorize_candidates.resolve_candidates)
    assert " ".join('''request.body_region or (
        "" if candidate_endpoint.provider == "amazon-bedrock"
        or (candidate_endpoint.provider, candidate_endpoint.model_id)
        in EXPLICIT_GATEWAY_REGIONS_BY_PROVIDER_MODEL else region
    )'''.split()) in " ".join(pipeline.split())
    us_only = Settings(environment="test", regions="us-central1,us-east4",
                       primary_region="us-central1")
    settings = snapshot_settings_from(us_only)
    assert "europe-west4" not in settings.regions
    assert choose_region(us_only, "europe-west4") == "us-central1"
    # Sakana's europe-west4 exclusion holds for a request naming that region,
    # though this plane would choose us-central1 for it.
    fugu = ModelEndpoint(id=f"{SAKANA_FUGU_MODEL_ID}@sakana/prepaid",
                         model_id=SAKANA_FUGU_MODEL_ID, provider="sakana", usage_type="Credits")
    rule = _gateway_regions(fugu)
    region = _gateway_region(settings, rule, "europe-west4")
    assert region == "europe-west4" and region in rule.deny
    assert not provider_model_available_from_gateway_region("sakana", SAKANA_FUGU_MODEL_ID, region)
    assert provider_model_available_from_gateway_region("sakana", SAKANA_FUGU_MODEL_ID, "us-central1")
    snapshot, inputs = live
    raw = {e.id: e for e in inputs.endpoints}
    for entry in snapshot.endpoints:
        endpoint = raw[entry.id]
        rule = entry.gateway_regions
        for body_region in (None, "", "europe-west4", "us-east4", "unknown-region"):
            checked = _gateway_region(settings, rule, body_region)
            explicit_origin = endpoint.provider == "amazon-bedrock" or (
                endpoint.provider, endpoint.model_id
            ) in {
                ("cloudflare-workers-ai", "cloudflare/clairic"),
                ("regolo", "cloudflare/clef-flash"),
            }
            python = body_region or ("" if explicit_origin else choose_region(us_only, body_region or None))
            assert checked == python
            available = (rule.only is None or checked in rule.only) and checked not in rule.deny
            assert available == provider_model_available_from_gateway_region(
                endpoint.provider, endpoint.model_id, python,
            ), (entry.id, body_region)


def test_endpoint_routing_and_response_fields(live) -> None:
    from trusted_router.routes.internal.gateway import _gateway_provider_route_payload

    snapshot, inputs = live
    raw = {e.id: e for e in inputs.endpoints}
    models = {m.id: m for m in inputs.models}
    for entry in snapshot.endpoints:
        endpoint = raw[entry.id]
        assert (entry.model_id, entry.provider, entry.usage_type) == (
            endpoint.model_id, endpoint.provider, endpoint.usage_type,
        )
        assert entry.upstream_model == (endpoint.upstream_id or models[endpoint.model_id].id)
        assert set(entry.supported_parameters) == set(endpoint.supported_parameters)
        for wanted in ({"text"}, {"image"}, {"audio"}, {"video"}, {"text", "image"}, set()):
            assert (wanted <= set(entry.input_modalities)) == (
                routing.endpoint_supports_input_modalities(endpoint, frozenset(wanted))
            ), (entry.id, wanted)
        assert entry.response.inference_location.model_dump(mode="json") == _as_json(
            inference_location_metadata(endpoint.provider, endpoint.model_id)
        )
        flag = _gateway_provider_route_payload(endpoint).get("wafer_zdr_required", False)
        assert entry.response.wafer_zdr_required is flag
    assert any(e.response.wafer_zdr_required for e in snapshot.endpoints)


def test_providers_and_models(live) -> None:
    snapshot, inputs = live
    for provider in snapshot.providers:
        source = catalog_data.PROVIDERS[provider.slug]
        assert (provider.name, provider.headquarters_country) == (
            source.name, source.provider_headquarters_country,
        )
        assert provider.byok_storage_slugs == byok_storage_provider_candidates(provider.slug)
    for model, source in zip(snapshot.models, inputs.models, strict=True):
        assert (model.id, model.provider, model.supports_chat, model.supports_embeddings,
                model.supports_decide, model.context_length) == (
            source.id, source.provider, source.supports_chat, source.supports_embeddings,
            source.supports_decide, source.context_length,
        )


# --- model and provider spellings -------------------------------------------------------------


def _strip_variant(snapshot: RoutingSnapshotV2, raw: str) -> tuple[str, dict[str, str]]:
    for variant in snapshot.aliases.variant_suffixes:
        if raw.endswith(variant.suffix):
            return raw[: -len(variant.suffix)], {variant.preference: variant.value}
    return raw, {}


def _resolve(snapshot: RoutingSnapshotV2, model_id: str) -> str:
    aliases = snapshot.aliases
    for rewrite in aliases.router_prefix_rewrites:
        if model_id.startswith(rewrite.prefix):
            model_id = rewrite.replacement + model_id[len(rewrite.prefix):]
    base, separator, variant = model_id.partition(":")
    if base in aliases.router_base_rewrites:
        model_id = aliases.router_base_rewrites[base] + separator + variant
    if model_id in aliases.alias_targets:
        return aliases.alias_targets[model_id]
    models = {m.id for m in snapshot.models}
    if model_id in models:
        return model_id
    for candidate in (model_id, re.sub(aliases.dated_suffix_pattern, "", model_id)):
        if candidate in models:
            return candidate
        if aliases.bare_vendor_prefix + candidate in models:
            return aliases.bare_vendor_prefix + candidate
    return model_id


def _spellings(snapshot: RoutingSnapshotV2) -> list[str]:
    ids = [m.id for m in snapshot.models]
    out = {
        "", "nope/model", "gpt-unknown-2025-01-01", "trustedrouter/auto-routing",
        "nyte/auto-routing", "trustedrouter/auto-routing:nitro", "nyte/auto-routing:floor",
        *snapshot.aliases.alias_targets, *ids,
    }
    for model_id in ids:
        out.add(model_id + ":nitro")
        out.add(model_id + ":floor")
        out.add(model_id + "-2025-04-14")
        if model_id.startswith("trustedrouter/"):
            out.add("nyte/" + model_id.removeprefix("trustedrouter/"))
        if model_id.startswith("openai/"):
            bare = model_id.removeprefix("openai/")
            out.update({bare, bare + "-2025-04-14", bare + ":nitro"})
    return sorted(out)


def test_model_spellings_resolve_as_routing_resolves_them(live) -> None:
    snapshot, _inputs = live
    rewritten = 0
    for spelling in _spellings(snapshot):
        stripped, _ = _strip_variant(snapshot, spelling)
        assert _resolve(snapshot, stripped) == routing.resolve_model_alias(stripped), spelling
        assert _resolve(snapshot, stripped) == routing.canonical_model_id(spelling), spelling
        rewritten += routing.canonical_model_id(spelling) != spelling
    assert rewritten > 100


def _provider_filter(snapshot: RoutingSnapshotV2, items: list[str]) -> list[str] | int:
    aliases, providers = snapshot.aliases, {p.slug for p in snapshot.providers}
    out: list[str] = []
    for item in items:
        normalized = item.strip().lower().replace("_", "-").replace(" ", "-")
        slugs = aliases.provider_group_aliases.get(
            normalized, (aliases.provider_aliases.get(normalized, normalized),)
        )
        if any(slug in aliases.router_provider_slugs for slug in slugs):
            return 400
        for slug in slugs:
            if slug not in providers:
                return 400
            if slug not in out:
                out.append(slug)
    return out


def _python_provider_filter(items: list[str]) -> list[str] | int:
    try:
        return routing._provider_filter_list("only", items)
    except HTTPException as exc:
        return exc.status_code


def test_provider_spellings_normalize_as_routing_normalizes_them(live) -> None:
    snapshot, _inputs = live
    aliases = snapshot.aliases
    spellings = sorted({
        *aliases.provider_aliases, *aliases.provider_group_aliases, *aliases.router_provider_slugs,
        *(p.slug for p in snapshot.providers), "nope", " Google_AI ", "Together AI", "OpenRouter",
    })
    for spelling in spellings:
        assert _provider_filter(snapshot, [spelling]) == _python_provider_filter([spelling]), spelling
    pairs = [spellings[i: i + 2] for i in range(0, len(spellings) - 1)]
    for pair in pairs:
        assert _provider_filter(snapshot, pair) == _python_provider_filter(pair), pair


# --- requested models --------------------------------------------------------------------------


def _python_requested(body: dict[str, Any], settings: Settings) -> Any:
    try:
        ids, overrides = routing._requested_model_ids(body, settings)
    except HTTPException as exc:
        return exc.status_code
    normalized: dict[str, Any] = {}
    for key, value in overrides.items():
        if key in {"order", "only"}:
            value = tuple(item.strip() for item in value.split(",") if item.strip())
        elif key in {"privacy_requirements", "pool_model_ids"}:
            value = tuple(sorted(value))
        normalized[key] = value
    return ids, normalized


def _requested(snapshot: RoutingSnapshotV2, raws: list[str]) -> Any:
    """The composition rule docs/design/routing-snapshot-v1.md gives Go."""
    entries = {r.id: r for r in snapshot.requested_models}
    ids: list[str] = []
    overrides: dict[str, Any] = {}
    pool_ids: set[str] = set()
    named_ids: set[str] = set()
    for raw in raws:
        stripped, variant = _strip_variant(snapshot, raw)
        canonical = _resolve(snapshot, stripped)
        entry = entries.get(canonical)
        if entry is not None and entry.refused_status is not None:
            return entry.refused_status
        overrides.update(variant)
        if entry is None:
            ids.append(canonical)
            named_ids.add(canonical)
            continue
        o = entry.overrides
        if o.usage is not None:
            overrides["usage"] = o.usage
        if o.renewable_only is not None:
            overrides["renewable_energy_only"] = True
        if o.provider_jurisdiction is not None:
            overrides["provider_jurisdiction"] = o.provider_jurisdiction
        if o.privacy_requirements:
            overrides["privacy_requirements"] = tuple(sorted(
                {*overrides.get("privacy_requirements", ()), *o.privacy_requirements}
            ))
        if o.order is not None:
            overrides["order"] = o.order
        if o.only is not None:
            overrides["only"] = o.only
        ids.extend(entry.expands_to)
        (pool_ids if list(entry.expands_to) != [canonical] else named_ids).update(entry.expands_to)
    if pool_ids - named_ids:
        overrides["pool_model_ids"] = tuple(sorted(pool_ids - named_ids))
    return (ids, overrides) if ids else 400


def test_each_requested_model_means_what_routing_says(live, settings) -> None:
    snapshot, _inputs = live
    from trusted_router.catalog_energy import renewable_provider_slugs

    for entry in snapshot.requested_models:
        spelling = entry.id
        if _resolve(snapshot, spelling) != entry.id:
            spelling = entry.id + "-2000-01-01"
        assert _resolve(snapshot, spelling) == entry.id
        assert _requested(snapshot, [spelling]) == _python_requested({"model": spelling}, settings)
        if entry.overrides.renewable_only is not None:
            assert set(entry.overrides.renewable_only) == renewable_provider_slugs()
    refused = {r.id for r in snapshot.requested_models if r.refused_status is not None}
    assert "trustedrouter/synth" in refused and "trustedrouter/auto" not in refused


def test_every_other_spelling_means_itself_with_no_overrides(live, settings) -> None:
    snapshot, _inputs = live
    for spelling in _spellings(snapshot):
        if not spelling:
            continue
        assert _requested(snapshot, [spelling]) == _python_requested({"model": spelling}, settings), spelling


def test_model_lists_compose_as_routing_composes_them(live, settings) -> None:
    snapshot, _inputs = live
    special = [r.id for r in snapshot.requested_models]
    plain = [m.id for m in snapshot.models[:5]]
    lists = [[a, b] for a in special for b in (special[::7] + plain[:2])]
    lists += [[a, "trustedrouter/zdr:nitro", b] for a, b in zip(plain, special[::11], strict=False)]
    for models in lists:
        assert _requested(snapshot, models) == _python_requested({"models": models}, settings), models


# --- ranking, chains, settings, fees ------------------------------------------------------------


def test_ranking_reproduces_measured_provider_rank(live) -> None:
    snapshot, inputs = live
    ranking = snapshot.ranking
    generated_at, ranks = provider_ranking.validate_snapshot(inputs.ranking_evidence)
    assert (generated_at, ranks) == (provider_ranking._GENERATED_AT, provider_ranking._RANKS)
    assert ranking.prepaid_min_rank == routing._PREPAID_PROVIDER_MIN_RANK
    assert ranking.model_provider_preference == routing._MODEL_PROVIDER_PREFERENCE
    tables = {None: ranking.default, "latency": ranking.latency, "throughput": ranking.throughput}
    providers = sorted({*ranking.default, *ranking.latency, "nope", *catalog_data.PROVIDERS})
    instants = [ranking.measured_from_us + d for d in (-1, 0, 1)]
    instants += [ranking.measured_through_us + d for d in (-1, 0, 1)]
    for micros in instants:
        now = EPOCH + timedelta(microseconds=micros)
        fresh = ranking.measured_from_us <= micros <= ranking.measured_through_us
        for sort in (None, "latency", "throughput", "price"):
            for provider in providers:
                expected = tables.get(sort, {}).get(provider, ranking.unknown_rank) if fresh else ranking.unknown_rank
                assert expected == provider_ranking.measured_provider_rank(provider, sort, now=now)


def test_decision_chains_and_fees(live) -> None:
    snapshot, _inputs = live
    assert snapshot.decision_chains == {
        model: tuple(chain) for model, chain in catalog_data.NAMED_DECISION_MODEL_PROVIDERS.items()
    }
    assert snapshot.fees.standard_markup_bps == STANDARD_PRICE_MARKUP_BASIS_POINTS
    assert snapshot.fees.signed_receipt_total_fee_bps == SIGNED_RECEIPT_TOTAL_FEE_BASIS_POINTS
    numerator = 10_000 + snapshot.fees.signed_receipt_total_fee_bps
    denominator = 10_000 + snapshot.fees.standard_markup_bps
    for price in (0, 1, 2, 999, 1_055, 10**9 + 7):
        ceil_ratio = (price * numerator + denominator - 1) // denominator
        assert ceil_ratio == signed_receipt_price_microdollars(price)


def test_settings_reproduce_region_choice_and_payload() -> None:
    for kwargs in ({}, {"regions": "us-east4,europe-west4", "primary_region": "us-west1"},
                   {"multi_region_enabled": False}):
        settings = Settings(environment="test", **kwargs)
        snapshot = snapshot_settings_from(settings)
        assert [r.model_dump(mode="json") for r in snapshot.region_payload] == region_payload(settings)
        for requested in (None, "", "bogus", *snapshot.regions):
            expected = requested if requested in snapshot.regions else snapshot.primary_region
            assert choose_region(settings, requested) == expected
        assert snapshot.stage_d.pilot_workspaces == tuple(sorted(settings.stage_d_pilot_workspaces))
        assert snapshot.request_record_write_mode == settings.request_record_write_mode


# --- determinism, bounds, secrets, dormancy -----------------------------------------------------


def test_compiling_twice_gives_identical_bytes(live, settings) -> None:
    snapshot, inputs = live
    assert canonical_snapshot_bytes(_compile(settings, inputs)) == canonical_snapshot_bytes(snapshot)


# Compiles the live catalog with the registry built at argv[3] and the catalog
# clock at argv[3]; prints the digest and the retirement instants, or the refusal.
_SUBPROCESS = """
import hashlib, json, sys
from datetime import UTC, datetime
resolved = datetime.fromtimestamp(int(sys.argv[3]), UTC)
from trusted_router import catalog_data
catalog_data._utc_now = lambda: resolved
from trusted_router.config import Settings
from trusted_router import routing_snapshot as rs
settings = Settings(environment="test")
try:
    snapshot = rs.compile_routing_snapshot(
        at=int(sys.argv[1]), horizon=int(sys.argv[2]), settings=rs.snapshot_settings_from(settings),
        source_revision="test", plane=rs.Plane(cloud="gcp", region="us-central1"),
        inputs=rs.CatalogInputs.live(settings))
except rs.SnapshotRefused as exc:
    print(json.dumps({"refused": str(exc)}))
else:
    print(json.dumps({
        "digest": hashlib.sha256(rs.canonical_snapshot_bytes(snapshot)).hexdigest(),
        "not_after": snapshot.not_after,
        "retired_at": sorted({e.retired_at for e in snapshot.endpoints if e.retired_at}),
    }))
"""


def _compile_in_subprocess(at: int, horizon: int, resolved: int, seed: str = "0") -> dict[str, Any]:
    env = {
        **os.environ, "PYTHONHASHSEED": seed, "TR_ENVIRONMENT": "test",
        "TR_LIFECYCLE_CLOCK_OVERRIDE": _instant(resolved).isoformat().replace("+00:00", "Z"),
        "PYTHONPATH": str(ROOT / "src"),
    }
    result = subprocess.run(  # noqa: S603 - fixed Python script, no shell or caller input
        [sys.executable, "-c", _SUBPROCESS, str(at), str(horizon), str(resolved)], env=env,
        cwd=ROOT, capture_output=True, text=True, timeout=300, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_identical_bytes_under_two_hash_seeds() -> None:
    runs = [_compile_in_subprocess(FIXED_AT, HORIZON, FIXED_AT, seed) for seed in ("0", "4242")]
    assert re.fullmatch(r"[0-9a-f]{64}", runs[0]["digest"]) and runs[0] == runs[1]


def test_a_catalog_imported_after_a_retirement_cannot_compile_before_it() -> None:
    # DeepInfra retires its GPT-OSS-120B Turbo routes at 2026-10-20 00:00 UTC.
    day = 24 * EPOCH_SECONDS
    retirement = FIXED_AT + day
    # Positive control: imported at noon, no retirement since compiled_at; the
    # routes retiring at the window's end are present, with that instant.
    noon = _compile_in_subprocess(FIXED_AT, day, FIXED_AT + 12 * EPOCH_SECONDS)
    assert noon["not_after"] == retirement and retirement in noon["retired_at"]
    # Imported after the retirement, the registry has dropped those routes.
    late = _compile_in_subprocess(FIXED_AT, day, retirement + EPOCH_SECONDS)
    assert "retirement" in late["refused"]
    # The same late import compiles a window starting at the retirement.
    assert "digest" in _compile_in_subprocess(retirement, HORIZON, retirement + EPOCH_SECONDS)


def test_size_and_compile_time_bounds(settings, catalog_clock) -> None:
    start = time.monotonic()
    snapshot = _compile(settings, CatalogInputs.live(settings))
    elapsed = time.monotonic() - start
    body = canonical_snapshot_bytes(snapshot)
    # Today about 3 MB and 1-2 s; the bounds catch a runaway, not a slow host.
    assert len(body) < 8 * 1024 * 1024
    assert elapsed < 120


# Every field the snapshot may carry. A new field fails here until it is added,
# with the reviewer's judgment that it is not secret, key-derived or a prompt.
ALLOWED_FIELDS = frozenset({
    "v", "kind", "compiled_at", "not_after", "epoch_seconds", "source_revision",
    "plane", "plane.cloud", "plane.region",
    "settings", "settings.regions", "settings.primary_region", "settings.region_payload",
    "settings.region_payload.id", "settings.region_payload.name",
    "settings.region_payload.primary", "settings.region_payload.enabled",
    "settings.region_payload.api_base_url", "settings.region_payload.control_plane_url",
    "settings.stage_d", "settings.stage_d.eligibility_enabled",
    "settings.stage_d.heartbeat_enabled", "settings.stage_d.require_heartbeat_declaration",
    "settings.stage_d.pilot_workspaces", "settings.request_record_write_mode",
    "providers", "providers.slug", "providers.name", "providers.headquarters_country",
    "providers.byok_storage_slugs",
    "models", "models.id", "models.provider", "models.supports_chat",
    "models.supports_embeddings", "models.supports_decide", "models.context_length",
    "endpoints", "endpoints.id", "endpoints.model_id", "endpoints.provider",
    "endpoints.usage_type", "endpoints.upstream_model", "endpoints.supported_parameters",
    "endpoints.input_modalities", "endpoints.context_length", "endpoints.privacy", "endpoints.privacy.stores_content",
    "endpoints.privacy.meets", "endpoints.gateway_regions", "endpoints.gateway_regions.only",
    "endpoints.gateway_regions.deny", "endpoints.gateway_regions.requires_explicit_origin",
    "endpoints.catalog_valid_until", "endpoints.retired_at", "endpoints.epochs",
    "endpoints.epochs.starts_at", "endpoints.epochs.headline",
    "endpoints.epochs.headline.prompt", "endpoints.epochs.headline.completion",
    "endpoints.epochs.price", "endpoints.epochs.price.endpoint_id",
    "endpoints.epochs.price.price_history_version", "endpoints.epochs.price.rates",
    "endpoints.epochs.price.rates.input_micro_per_million",
    "endpoints.epochs.price.rates.cached_input_micro_per_million",
    "endpoints.epochs.price.rates.cache_creation_micro_per_million",
    "endpoints.epochs.price.rates.output_micro_per_million",
    "endpoints.epochs.price.tiers", "endpoints.epochs.price.tiers.max_prompt_tokens",
    "endpoints.epochs.price.tiers.rates",
    "endpoints.epochs.price.tiers.rates.input_micro_per_million",
    "endpoints.epochs.price.tiers.rates.cached_input_micro_per_million",
    "endpoints.epochs.price.tiers.rates.cache_creation_micro_per_million",
    "endpoints.epochs.price.tiers.rates.output_micro_per_million",
    "endpoints.epochs.price.request_fee_micro", "endpoints.epochs.price.rounding",
    "endpoints.response", "endpoints.response.inference_location",
    "endpoints.response.inference_location.advertised_regions",
    "endpoints.response.inference_location.advertised_region_scope",
    "endpoints.response.inference_location.catalog_updated_at",
    "endpoints.response.inference_location.provider_declared_locations",
    "endpoints.response.inference_location.provider_declaration_reviewed_on",
    "endpoints.response.inference_location.serving_region",
    "endpoints.response.inference_location.serving_region_status",
    "endpoints.response.inference_location.region_pinning_enforced",
    "endpoints.response.inference_location.documentation_url",
    "endpoints.response.wafer_zdr_required",
    "aliases", "aliases.router_prefix_rewrites", "aliases.router_prefix_rewrites.prefix",
    "aliases.router_prefix_rewrites.replacement", "aliases.router_base_rewrites",
    "aliases.alias_targets", "aliases.dated_suffix_pattern", "aliases.bare_vendor_prefix",
    "aliases.variant_suffixes", "aliases.variant_suffixes.suffix",
    "aliases.variant_suffixes.preference", "aliases.variant_suffixes.value",
    "aliases.provider_aliases", "aliases.provider_group_aliases",
    "aliases.router_provider_slugs",
    "requested_models", "requested_models.id", "requested_models.expands_to",
    "requested_models.refused_status", "requested_models.private_proxy",
    "requested_models.overrides", "requested_models.overrides.usage",
    "requested_models.overrides.renewable_only",
    "requested_models.overrides.provider_jurisdiction",
    "requested_models.overrides.privacy_requirements", "requested_models.overrides.order",
    "requested_models.overrides.only",
    "decision_chains",
    "ranking", "ranking.measured_from_us", "ranking.measured_through_us",
    "ranking.unknown_rank", "ranking.default", "ranking.latency", "ranking.throughput",
    "ranking.prepaid_min_rank", "ranking.model_provider_preference",
    "fees", "fees.standard_markup_bps", "fees.signed_receipt_total_fee_bps",
    "fees.receipt_rounding", "fees.token_rounding",
})
# Maps whose keys are catalog data (model ids, provider slugs), not field names.
DATA_KEYED = frozenset({
    "aliases.router_base_rewrites", "aliases.alias_targets", "aliases.provider_aliases",
    "aliases.provider_group_aliases", "decision_chains", "ranking.default",
    "ranking.latency", "ranking.throughput", "ranking.prepaid_min_rank",
    "ranking.model_provider_preference",
})
SECRET_SHAPED = re.compile(
    r"secret|encrypt|password|credential|hidden|destination|webhook|api_key|key_hint|"
    r"cache_key|private_key|bearer|signature|hash|authorization|workspace_id|prompt_text|byok_ref",
    re.IGNORECASE,
)


def _schema_paths(model: type[BaseModel], prefix: str = "") -> set[str]:
    paths: set[str] = set()
    for name, info in model.model_fields.items():
        path = prefix + name
        paths.add(path)
        pending = [info.annotation]
        while pending:
            annotation = pending.pop()
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                paths |= _schema_paths(annotation, path + ".")
            pending.extend(typing.get_args(annotation))
    return paths


def test_the_schema_carries_only_allowlisted_fields() -> None:
    paths = _schema_paths(RoutingSnapshotV2)
    assert paths == ALLOWED_FIELDS, (
        f"new fields {sorted(paths - ALLOWED_FIELDS)}; gone {sorted(ALLOWED_FIELDS - paths)}"
    )
    for path in paths:
        assert not SECRET_SHAPED.search(path), path


def _walk(value: Any, path: str, out: list[tuple[str, Any]]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if path in DATA_KEYED:
                assert re.fullmatch(r"[A-Za-z0-9_./:@+\-]+", key), (path, key)
                _walk(item, path, out)
            else:
                child = f"{path}.{key}" if path else key
                assert child in ALLOWED_FIELDS, child
                _walk(item, child, out)
    elif isinstance(value, list):
        for item in value:
            _walk(item, path, out)
    else:
        out.append((path, value))


def test_the_live_document_holds_nothing_secret_shaped(live, settings) -> None:
    snapshot, _inputs = live
    leaves: list[tuple[str, Any]] = []
    _walk(json.loads(canonical_snapshot_bytes(snapshot)), "", leaves)
    region_urls = {row["api_base_url"] for row in region_payload(settings)}
    key_shaped = re.compile(r"sk-[A-Za-z0-9]|AKIA[0-9A-Z]{8}|AIza[0-9A-Za-z_\-]{10}|-----BEGIN|"
                            r"ghp_[A-Za-z0-9]|xox[abp]-|[A-Za-z0-9+_]{32,}")
    for path, value in leaves:
        assert not isinstance(value, float), path
        if not isinstance(value, str):
            continue
        assert not key_shaped.search(value), (path, value)
        if "://" in value:
            assert value in region_urls or (
                path == "endpoints.response.inference_location.documentation_url"
                and value.startswith("https://trustedrouter.com/providers/")
            ), (path, value)


def test_importing_the_app_loads_neither_snapshot_module() -> None:
    # Publishing is off by default (Settings and rollout.sh): off, the app
    # imports neither module; only the publisher, which main imports when
    # publishing is on, reads them (tests/test_routing_snapshot_publish.py).
    script = (
        "import sys; import trusted_router.main; "
        "loaded = [m for m in ('trusted_router.routing_snapshot', "
        "'trusted_router.routing_snapshot_signing', 'trusted_router.routing_snapshot_worker', "
        "'trusted_router.services.routing_snapshot_publisher') if m in sys.modules]; "
        "assert not loaded, loaded"
    )
    result = subprocess.run(  # noqa: S603 - fixed Python script, no shell or caller input
        [sys.executable, "-c", script],
        env={**os.environ, "TR_ENVIRONMENT": "test", "PYTHONPATH": str(ROOT / "src")},
        # A cold import of trusted_router.main takes about 13 s on an idle
        # machine and far longer on a loaded one; the bound only catches a hang.
        capture_output=True, text=True, timeout=300, cwd=ROOT, check=False,
    )
    assert result.returncode == 0, result.stderr
    importers = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src").rglob("*.py")
        if re.search(r"^\s*(from|import)\s+trusted_router\.routing_snapshot\b|"
                     r"from trusted_router import .*\brouting_snapshot\b",
                     path.read_text(), re.MULTILINE)
    )
    assert importers == [
        "src/trusted_router/routing_snapshot_signing.py",
        "src/trusted_router/routing_snapshot_worker.py",
        "src/trusted_router/services/routing_snapshot_publisher.py",
    ]
