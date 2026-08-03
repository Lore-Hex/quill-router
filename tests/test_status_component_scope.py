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
)
from trusted_router.synthetic.status import status_snapshot

# The list GCP publishes today, in order. Written out rather than derived so
# a change to the catalogue cannot quietly redefine what "unchanged" means.
GCP_COMPONENT_IDS = (
    "canonical_api",
    "us_central1_regional_api",
    "us_east4_regional_api",
    "eu_regional_api",
    "attestation",
    "billing_settlement",
    "provider_fallback",
    "image_generation",
)

REGIONAL_GCP_COMPONENT_IDS = (
    "us_central1_regional_api",
    "us_east4_regional_api",
    "eu_regional_api",
)


def _gcp_settings() -> Settings:
    """Production GCP shape: three warm attested regions."""
    return Settings(environment="test", sentry_dsn=None)


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


def test_gcp_publishes_the_full_catalogue_unchanged() -> None:
    assert applicable_component_definitions(_gcp_settings()) == COMPONENT_DEFINITIONS
    assert tuple(
        str(definition["id"]) for definition in applicable_component_definitions(_gcp_settings())
    ) == GCP_COMPONENT_IDS


def test_gcp_status_snapshot_components_are_byte_identical() -> None:
    now = utcnow()
    samples = [_tls_sample(created_at=_iso(now - dt.timedelta(seconds=10)))]

    snapshot = status_snapshot(samples, now=now, settings=_gcp_settings())

    assert _published_ids(snapshot) == list(GCP_COMPONENT_IDS)
    # Name and description text is public copy — assert it verbatim so a
    # scoping change cannot reword GCP's page as a side effect.
    published = {str(row["id"]): row for row in snapshot["components"]}
    for definition in COMPONENT_DEFINITIONS:
        row = published[str(definition["id"])]
        assert row["name"] == definition["name"]
        assert row["description"] == definition["description"]


def test_aws_eu_does_not_advertise_gcp_regional_gateways() -> None:
    ids = tuple(
        str(definition["id"]) for definition in applicable_component_definitions(_aws_eu_settings())
    )

    assert ids == (
        "canonical_api",
        "attestation",
        "billing_settlement",
        "provider_fallback",
    )
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


def test_a_probe_target_alone_does_not_make_a_component_measurable() -> None:
    """Capability gating is a separate axis from target topology.

    Turning the image job off on an otherwise complete GCP-shaped
    deployment must drop exactly that one component and nothing else.
    """
    from trusted_router.config import Settings as _Settings

    without_image_job = _Settings(
        environment="test", sentry_dsn=None, synthetic_image_probe_enabled=False
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
