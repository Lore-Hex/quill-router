"""A public status page may only advertise components it actually measures.

COMPONENT_DEFINITIONS is the full historical catalogue, and every deployment
used to publish all of it. On the AWS EU cloud — one Nitro gateway in
eu-west-3, no GCP regions at all — that put three permanently "unknown" rows
on https://aws.trustedrouter.com/status: US Central, US East, and an "EU
Regional API" that is GCP's europe-west4, not this cloud. "Unknown" on a
public page reads as "we are not sure our own service works".

The published list is therefore derived from configuration (the probe
targets the monitor builds from regions + synthetic_regional_probes_enabled),
never from a per-cloud hardcoded list. GCP, which legitimately has all eight,
must be completely unaffected.
"""

from __future__ import annotations

import datetime as dt

from trusted_router.config import Settings
from trusted_router.storage_models import SyntheticProbeSample, SyntheticRollup, utcnow
from trusted_router.synthetic.components import (
    COMPONENT_DEFINITIONS,
    COMPONENT_PROBE_TARGETS,
    applicable_component_definitions,
    rollup_slo_class_ids,
    sample_slo_class_ids,
)
from trusted_router.synthetic.status import status_snapshot

# The list GCP publishes today, in order. Written out rather than derived so
# a change to the catalogue cannot quietly redefine what "unchanged" means.
GCP_COMPONENT_IDS = (
    "canonical_api",
    "us_central1_regional_api",
    "us_east4_regional_api",
    "eu_regional_api",
    "sa_regional_api",
    "attestation",
    "billing_settlement",
    "provider_fallback",
    "image_generation",
)

REGIONAL_GCP_COMPONENT_IDS = (
    "us_central1_regional_api",
    "us_east4_regional_api",
    "eu_regional_api",
    "sa_regional_api",
)


def _gcp_settings() -> Settings:
    """Production GCP shape: four warm attested regions."""
    return Settings(
        environment="test",
        sentry_dsn=None,
        internal_gateway_token="test-gateway-token",  # noqa: S106 - test fixture.
    )


def _aws_eu_settings() -> Settings:
    """The AWS EU cloud, as scripts/deploy/aws_eu_control_plane.sh deploys it."""
    return Settings(
        environment="test",
        sentry_dsn=None,
        api_base_url="https://api-aws.trustedrouter.com/v1",
        primary_region="eu-west-3",
        regions="eu-west-3",
        synthetic_regional_probes_enabled=False,
        synthetic_image_probe_enabled=False,
        synthetic_canonical_attested=True,
        synthetic_control_plane_health_url="https://aws.trustedrouter.com",
    )


def _iso(moment: dt.datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _tls_sample(*, created_at: str, target: str = "canonical") -> SyntheticProbeSample:
    return SyntheticProbeSample(
        id=f"syn-{target}-{created_at}",
        probe_type="tls_health",
        target=target,
        target_url="https://api.example/health",
        monitor_region="eu-west-3",
        target_region=target if target != "canonical" else None,
        status="up",
        latency_milliseconds=21,
        created_at=created_at,
    )


def _published_ids(snapshot: dict[str, object]) -> list[str]:
    components = snapshot["components"]
    assert isinstance(components, list)
    return [str(row["id"]) for row in components]


def test_gcp_publishes_exactly_its_own_components_unchanged() -> None:
    """GCP's page is defined by GCP's probe targets, never by the catalogue.

    The catalogue also carries per-enclave AWS components, which GCP has no
    targets for; the published list must therefore stay exactly the nine
    rows above, in that order.
    """
    published = applicable_component_definitions(_gcp_settings())

    assert tuple(str(definition["id"]) for definition in published) == GCP_COMPONENT_IDS
    # Each published row is the catalogue entry verbatim, not a copy that
    # scoping could have reworded.
    catalogue = {str(definition["id"]): definition for definition in COMPONENT_DEFINITIONS}
    assert published == tuple(catalogue[component_id] for component_id in GCP_COMPONENT_IDS)


def test_gcp_status_snapshot_components_are_byte_identical() -> None:
    now = utcnow()
    samples = [_tls_sample(created_at=_iso(now - dt.timedelta(seconds=10)))]

    snapshot = status_snapshot(samples, now=now, settings=_gcp_settings())

    assert _published_ids(snapshot) == list(GCP_COMPONENT_IDS)
    # Name and description text is public copy — assert it verbatim so a
    # scoping change cannot reword GCP's page as a side effect.
    published = {str(row["id"]): row for row in snapshot["components"]}
    for definition in COMPONENT_DEFINITIONS:
        component_id = str(definition["id"])
        if component_id not in GCP_COMPONENT_IDS:
            # Catalogue entries GCP cannot sample (the per-enclave AWS rows)
            # must not appear on GCP's page at all.
            assert component_id not in published
            continue
        row = published[component_id]
        assert row["name"] == definition["name"]
        assert row["description"] == definition["description"]


def test_gcp_current_checks_expose_regions_without_blending_router_core_slo() -> None:
    """Deploy gates see every configured region; public SLO remains canonical."""
    now = utcnow()
    samples = [
        _tls_sample(created_at=_iso(now - dt.timedelta(seconds=10)), target=target)
        for target in (
            "canonical",
            "us-central1",
            "us-east4",
            "europe-west4",
            "southamerica-east1",
        )
    ]

    snapshot = status_snapshot(samples, now=now, settings=_gcp_settings())

    current = snapshot["current"]
    assert isinstance(current, dict)
    checks = current["checks"]
    assert isinstance(checks, list)
    assert {row["target"] for row in checks} == {
        "canonical",
        "us-central1",
        "us-east4",
        "europe-west4",
        "southamerica-east1",
    }
    assert {row["target_region"] for row in checks if row["target"] != "canonical"} == {
        "us-central1",
        "us-east4",
        "europe-west4",
        "southamerica-east1",
    }

    # Direct regional diagnostics must not inflate uptime denominators or
    # burn-rate calculations for the canonical router-core service.
    windows = snapshot["windows"]
    assert isinstance(windows, dict)
    assert windows["5m"]["sample_count"] == 1
    slo_classes = snapshot["slo_classes"]
    assert isinstance(slo_classes, dict)
    assert slo_classes["router_core"]["windows"]["5m"]["sample_count"] == 1


def test_legacy_regional_control_plane_probes_do_not_burn_global_slo() -> None:
    """Private run.app probe artifacts remain visible but never count as downtime."""
    now = utcnow()
    created_at = _iso(now - dt.timedelta(seconds=10))
    canonical = SyntheticProbeSample(
        id="syn-control-plane-global",
        probe_type="control_plane_health",
        target="control-plane",
        target_url="https://trustedrouter.com/health",
        monitor_region="us-central1",
        status="up",
        created_at=created_at,
    )
    legacy_regional = SyntheticProbeSample(
        id="syn-control-plane-legacy-region",
        probe_type="control_plane_health",
        target="us-central1",
        target_region="us-central1",
        target_url="https://trusted-router.example.run.app/health",
        monitor_region="us-central1",
        status="down",
        http_status=404,
        created_at=created_at,
    )
    legacy_rollup = SyntheticRollup(
        id="rollup-control-plane-legacy-region",
        period="hour",
        period_start=_iso(now.replace(minute=0, second=0, microsecond=0)),
        component="uncategorized",
        target="us-central1",
        probe_type="control_plane_health",
        monitor_region="us-central1",
        target_region="us-central1",
        sample_count=31,
        down_count=31,
        error_counts={"bad_health_response": 31},
        last_checked_at=created_at,
    )

    assert sample_slo_class_ids(canonical) == ["control_plane"]
    assert sample_slo_class_ids(legacy_regional) == []
    assert rollup_slo_class_ids(legacy_rollup) == []

    snapshot = status_snapshot(
        [canonical, legacy_regional],
        rollups=[legacy_rollup],
        now=now,
        settings=_gcp_settings(),
    )
    control_plane = snapshot["slo_classes"]["control_plane"]
    assert control_plane["status"] == "up"
    assert control_plane["windows"]["5m"]["sample_count"] == 1
    assert control_plane["windows"]["5m"]["uptime_percent"] == 100.0
    assert set(control_plane["current_by_region"]) == {"global"}


def test_control_plane_slo_backfills_each_monitor_dimension_from_rollups() -> None:
    """One live monitor row must not hide its peer monitor's hourly rollup."""
    now = dt.datetime(2026, 8, 31, 2, 30, tzinfo=dt.UTC)
    period_start = _iso(now.replace(minute=0, second=0, microsecond=0))
    live = SyntheticProbeSample(
        id="syn-control-plane-eu-live",
        probe_type="control_plane_health",
        target="control-plane",
        target_url="https://trustedrouter.com/health",
        monitor_region="europe-west4",
        status="up",
        created_at=_iso(now - dt.timedelta(seconds=30)),
    )
    eu_rollup = SyntheticRollup(
        id="rollup-control-plane-eu",
        period="hour",
        period_start=period_start,
        component="uncategorized",
        target="control-plane",
        probe_type="control_plane_health",
        monitor_region="europe-west4",
        sample_count=20,
        up_count=20,
        last_checked_at=live.created_at,
    )
    us_rollup = SyntheticRollup(
        id="rollup-control-plane-us",
        period="hour",
        period_start=period_start,
        component="uncategorized",
        target="control-plane",
        probe_type="control_plane_health",
        monitor_region="us-central1",
        sample_count=20,
        up_count=20,
        last_checked_at=_iso(now - dt.timedelta(minutes=1)),
    )

    snapshot = status_snapshot(
        [live],
        rollups=[eu_rollup, us_rollup],
        now=now,
        settings=_gcp_settings(),
    )

    control_plane = snapshot["slo_classes"]["control_plane"]
    one_hour = control_plane["windows"]["1h"]
    # The live EU row supersedes its same-hour aggregate. The independent US
    # aggregate remains valid evidence and must not be discarded with it.
    assert one_hour["sample_count"] == 21
    assert one_hour["up_count"] == 21
    assert one_hour["uptime_percent"] == 100.0


def test_gcp_current_checks_keep_complete_regional_deploy_canaries() -> None:
    """A bounded public sample tail cannot crowd deploy-gate PONGs out."""
    now = utcnow()
    created_at = _iso(now - dt.timedelta(seconds=10))
    samples = [
        _tls_sample(created_at=created_at),
        _tls_sample(created_at=created_at, target="us-central1"),
        SyntheticProbeSample(
            id="syn-region-attestation",
            probe_type="attestation_nonce",
            target="us-central1",
            target_region="us-central1",
            target_url="https://api-us-central1.quillrouter.com/attestation",
            monitor_region="europe-west4",
            status="up",
            created_at=created_at,
        ),
        SyntheticProbeSample(
            id="syn-region-chat",
            probe_type="openai_sdk_pong",
            target="us-central1",
            target_region="us-central1",
            target_url="https://api-us-central1.quillrouter.com/v1/chat/completions",
            monitor_region="europe-west4",
            status="up",
            created_at=created_at,
        ),
        SyntheticProbeSample(
            id="syn-region-responses",
            probe_type="responses_pong",
            target="us-central1",
            target_region="us-central1",
            target_url="https://api-us-central1.quillrouter.com/v1/responses",
            monitor_region="europe-west4",
            status="down",
            created_at=created_at,
        ),
    ]
    samples.extend(
        SyntheticProbeSample(
            id=f"syn-newer-control-plane-{index}",
            probe_type="gateway_authorize",
            target="control-plane",
            target_url="https://trustedrouter.com/internal/gateway/authorize",
            monitor_region="europe-west4",
            status="up",
            created_at=_iso(now - dt.timedelta(seconds=1)),
        )
        for index in range(120)
    )

    snapshot = status_snapshot(samples, now=now, settings=_gcp_settings())
    checks = snapshot["current"]["checks"]

    assert {
        row["probe_type"] for row in checks if row["target"] == "us-central1"
    } == {
        "attestation_nonce",
        "openai_sdk_pong",
        "responses_pong",
        "tls_health",
    }
    assert not any(
        row["target"] == "us-central1"
        and row["probe_type"] in {"openai_sdk_pong", "responses_pong"}
        for row in snapshot["samples"]
    )
    responses = next(
        row
        for row in checks
        if row["target"] == "us-central1"
        and row["probe_type"] == "responses_pong"
    )
    assert responses["effective_status"] == "down"
    assert snapshot["slo_classes"]["router_core"]["status"] == "up"


def test_aws_eu_does_not_advertise_gcp_regional_gateways() -> None:
    ids = tuple(
        str(definition["id"]) for definition in applicable_component_definitions(_aws_eu_settings())
    )

    assert ids == ("canonical_api", "attestation")
    for component_id in REGIONAL_GCP_COMPONENT_IDS:
        assert component_id not in ids


def test_aws_eu_status_snapshot_publishes_no_unmeasurable_components() -> None:
    now = utcnow()
    samples = [_tls_sample(created_at=_iso(now - dt.timedelta(seconds=10)))]

    snapshot = status_snapshot(samples, now=now, settings=_aws_eu_settings())

    published = _published_ids(snapshot)
    for component_id in REGIONAL_GCP_COMPONENT_IDS:
        assert component_id not in published
    # The components it DOES publish are the ones it probes, and none of
    # them is a row that can never resolve.
    assert "canonical_api" in published
    assert "attestation" in published
    assert "billing_settlement" not in published
    assert "provider_fallback" not in published


def test_scope_follows_configuration_not_a_hardcoded_cloud_list() -> None:
    """Drop a region from settings and its component stops being published.

    This is the property that makes the fix general: nothing anywhere names
    AWS, and a GCP deployment that retires europe-west4 gets the same
    treatment as the AWS cloud that never had it.
    """
    two_region_gcp = Settings(
        environment="test",
        sentry_dsn=None,
        regions="us-central1,us-east4",
        primary_region="us-central1",
    )

    ids = tuple(
        str(definition["id"]) for definition in applicable_component_definitions(two_region_gcp)
    )

    assert "us_central1_regional_api" in ids
    assert "us_east4_regional_api" in ids
    assert "eu_regional_api" not in ids


def test_historical_rollup_for_an_inapplicable_component_still_renders() -> None:
    """24-month history outlives the component list.

    A rollup naming a component this deployment no longer publishes must
    still resolve to its real display name, not a slug — that is why the
    module-level catalogue stays complete.
    """
    now = utcnow()
    rollup = SyntheticRollup(
        id="rollup-eu-legacy",
        period="hour",
        period_start=_iso((now - dt.timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)),
        component="eu_regional_api",
        target="europe-west4",
        probe_type="tls_health",
        monitor_region="europe-west4",
        target_region="europe-west4",
        sample_count=6,
        up_count=4,
        down_count=2,
        error_counts={"bad_health_response": 2},
        last_checked_at=_iso(now - dt.timedelta(hours=2)),
    )

    snapshot = status_snapshot(
        [_tls_sample(created_at=_iso(now - dt.timedelta(seconds=10)))],
        rollups=[rollup],
        now=now,
        settings=_aws_eu_settings(),
    )

    assert "eu_regional_api" not in _published_ids(snapshot)
    events = snapshot["recent_events"]
    assert isinstance(events, list)
    assert "EU Regional API" in [event["component"] for event in events]


def test_image_generation_is_dropped_where_nothing_schedules_its_job() -> None:
    """The fourth permanently-"unknown" row on the live AWS EU page.

    Image samples come from a SEPARATE scheduled job that only
    scripts/deploy/synthetic.sh creates. Its probe target is "canonical",
    which every deployment has, so target presence alone kept publishing a
    component the AWS cloud can never sample — leaving "Image Generation —
    unknown" on the public page exactly like the three regional rows.
    """
    aws_ids = tuple(
        str(definition["id"]) for definition in applicable_component_definitions(_aws_eu_settings())
    )
    assert "image_generation" not in aws_ids
    # GCP, which does schedule the job, keeps it.
    assert "image_generation" in tuple(
        str(definition["id"]) for definition in applicable_component_definitions(_gcp_settings())
    )


def test_aws_eu_status_snapshot_has_no_unknown_component_rows() -> None:
    """The user-visible assertion: no row on the AWS page says "unknown".

    Every published component must resolve to a real measurement. This is
    what the original report was about — four `unknown` rows on a public
    status page.
    """
    now = utcnow()
    samples = [
        _tls_sample(created_at=_iso(now - dt.timedelta(seconds=10))),
        SyntheticProbeSample(
            id="syn-att",
            probe_type="attestation_nonce",
            target="canonical",
            target_url="https://api-aws.trustedrouter.com/attestation",
            monitor_region="eu-west-3",
            status="up",
            created_at=_iso(now - dt.timedelta(seconds=10)),
        ),
        SyntheticProbeSample(
            id="syn-bill",
            probe_type="gateway_authorize_settle",
            target="control-plane",
            target_url="https://aws.trustedrouter.com",
            monitor_region="eu-west-3",
            status="up",
            created_at=_iso(now - dt.timedelta(seconds=10)),
        ),
        SyntheticProbeSample(
            id="syn-fallback",
            probe_type="provider_fallback",
            target="control-plane",
            target_url="https://aws.trustedrouter.com",
            monitor_region="eu-west-3",
            status="up",
            created_at=_iso(now - dt.timedelta(seconds=10)),
        ),
    ]

    snapshot = status_snapshot(samples, now=now, settings=_aws_eu_settings())

    components = snapshot["components"]
    assert isinstance(components, list)
    unknown = [str(row["id"]) for row in components if row["status"] == "unknown"]
    assert unknown == []
    assert "billing_settlement" not in _published_ids(snapshot)
    assert "provider_fallback" not in _published_ids(snapshot)
    current = snapshot["current"]
    assert isinstance(current, dict)
    checks = current["checks"]
    assert isinstance(checks, list)
    assert {row["probe_type"] for row in checks} == {"tls_health", "attestation_nonce"}


def test_retired_gateway_samples_cannot_poison_standalone_current_status() -> None:
    """A secret-removal deploy must not leave its status page red forever."""
    now = utcnow()
    samples = [
        _tls_sample(created_at=_iso(now - dt.timedelta(seconds=10))),
        SyntheticProbeSample(
            id="syn-attestation-current",
            probe_type="attestation_nonce",
            target="canonical",
            target_url="https://api-aws.trustedrouter.com/attestation",
            monitor_region="eu-west-3",
            status="up",
            created_at=_iso(now - dt.timedelta(seconds=10)),
        ),
        SyntheticProbeSample(
            id="syn-billing-retired",
            probe_type="gateway_authorize_settle",
            target="control-plane",
            target_url="https://aws.trustedrouter.com",
            monitor_region="eu-west-3",
            status="up",
            created_at=_iso(now - dt.timedelta(hours=1)),
        ),
        SyntheticProbeSample(
            id="syn-fallback-retired",
            probe_type="provider_fallback",
            target="control-plane",
            target_url="https://aws.trustedrouter.com",
            monitor_region="eu-west-3",
            status="up",
            created_at=_iso(now - dt.timedelta(hours=1)),
        ),
    ]

    snapshot = status_snapshot(samples, now=now, settings=_aws_eu_settings())

    assert snapshot["current"]["overall_status"] == "up"
    assert snapshot["slo_classes"]["router_core"]["status"] == "up"
    assert {row["probe_type"] for row in snapshot["current"]["checks"]} == {
        "tls_health",
        "attestation_nonce",
    }


def test_a_probe_target_alone_does_not_make_a_component_measurable() -> None:
    """Capability gating is a separate axis from target topology.

    Turning the image job off on an otherwise complete GCP-shaped
    deployment must drop exactly that one component and nothing else.
    """
    from trusted_router.config import Settings as _Settings

    without_image_job = _Settings(
        environment="test",
        sentry_dsn=None,
        synthetic_image_probe_enabled=False,
        internal_gateway_token="test-gateway-token",  # noqa: S106 - test fixture.
    )

    ids = tuple(
        str(definition["id"])
        for definition in applicable_component_definitions(without_image_job)
    )

    assert ids == tuple(
        component_id for component_id in GCP_COMPONENT_IDS if component_id != "image_generation"
    )


def test_component_probe_targets_cover_the_catalogue() -> None:
    """Every catalogue component declares which probe target produces it.

    Without this, a newly added component would fall through the unmapped
    branch in applicable_component_definitions and get published on clouds
    that cannot measure it — the exact bug this module fences.
    """
    assert {str(definition["id"]) for definition in COMPONENT_DEFINITIONS} == set(
        COMPONENT_PROBE_TARGETS
    )
