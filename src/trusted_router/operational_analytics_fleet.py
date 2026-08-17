"""Where to read every cloud's drain-freshness signal, for every cloud.

THE FAILURE THIS EXISTS TO MAKE IMPOSSIBLE
    Between 2026-08-02 and 2026-08-17 the AWS-EU operational-analytics drain
    was never installed: no systemd unit, no environment file, stale code at
    /opt/drain, and a node role holding no ``dsql`` permission at all. The
    outbox grew to 470,370 rows and nothing reported it, because the one
    backlog alarm in existence -- ``operational_analytics_outbox.backlog_alarm``
    -- is emitted BY the drain process that was missing. GCP was fine, so the
    fleet looked fine.

    :mod:`trusted_router.operational_analytics_freshness` fixed the signal: the
    control plane publishes the age of the oldest undelivered outbox row, which
    is observable from outside the VPC and is observable precisely when the
    drain does not exist to complain. This module fixes the *coverage*. A
    freshness check pointed at one hardcoded URL answers the question for the
    cloud whose URL somebody happened to type.

THE IDIOM
    Same shape, and the same reason, as
    ``trusted_router.byok_v1_attestations.STANDALONE_CLOUDS``: "checking the
    one cloud you happened to have a URL for is exactly the mistake this tuple
    exists to make impossible." ``tests/test_analytics_freshness_registry.py``
    binds the two together in both directions, so a fourth deployment cannot be
    added to the fleet without either giving it a drain-freshness endpoint or
    writing down, here, why it has none.

WHY A ``reason`` FIELD AND NOT AN OMISSION
    A cloud with no public control-plane status URL is a real possibility -- an
    internal-only plane, a deployment behind a private ALB. The registry makes
    that case *say so*, because the alternative is a cloud that is silently
    absent from the fleet list, which is the same "configured, healthy, and
    empty" shape as the outage above: everything reports success and nothing
    was measured. An entry with a ``reason`` still fails the CI binding's
    membership check with a loud, specific message; it just cannot be checked
    over HTTP.

NOTHING HERE DOES IO. :mod:`clickhouse.check_fleet_analytics_freshness` fetches.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from trusted_router.byok_v1_attestations import (
    ENCLAVE_CONTROL_PLANE_SOURCES,
    STANDALONE_CLOUDS,
)


@dataclass(frozen=True)
class FleetAnalyticsEndpoint:
    """One standalone cloud's public, credential-free drain-freshness source.

    Exactly one of ``status_url`` / ``reason`` is set. Both set, or neither,
    is a registry that is lying about its own coverage, and
    :func:`registry_defects` refuses it.
    """

    cloud: str
    #: Public ``/status.json`` of that cloud's CONTROL PLANE. Not the inference
    #: plane: ``api-aws``/``api-azure.trustedrouter.com`` are the enclaves and
    #: serve no status page.
    status_url: str | None = None
    #: Why this cloud cannot be checked over HTTP. Required iff no URL.
    reason: str | None = None
    note: str = ""

    @property
    def checkable(self) -> bool:
        return bool(self.status_url)


#: One entry per standalone cloud. Verified reachable and returning HTTP 200
#: with a JSON body on 2026-08-17.
ANALYTICS_FRESHNESS_FLEET: tuple[FleetAnalyticsEndpoint, ...] = (
    FleetAnalyticsEndpoint(
        cloud="aws",
        status_url="https://gchircrcif.eu-west-3.awsapprunner.com/status.json",
        note=(
            "The tr-eu App Runner control plane -- the deployment that holds the "
            "Aurora DSQL connection this signal is read through, and the one whose "
            "drain was missing for fifteen days. Deliberately NOT "
            "aws.trustedrouter.com: that vanity name fronts the Fargate control "
            "plane through Global Accelerator, and pointing the check at the wrong "
            "AWS front end would answer a question nobody asked."
        ),
    ),
    FleetAnalyticsEndpoint(
        cloud="azure",
        status_url="https://azure.trustedrouter.com/status.json",
        note=(
            "The Azure control plane's own hostname, set as STATUS_HOST by "
            "scripts/deploy/azure_control_plane.sh and pointed at the container app "
            "by that script's Cloud DNS step. Its storage backend is PostgresStore, "
            "so it holds the same outbox shape as AWS and the same drain can stop "
            "on it the same way."
        ),
    ),
    FleetAnalyticsEndpoint(
        cloud="gcp",
        status_url="https://trustedrouter.com/status.json",
        note=(
            "The home plane, on Spanner rather than Postgres. It was the healthy "
            "cloud during the AWS-EU outage, which is exactly why it belongs here: "
            "one green cloud is what made the fleet look fine."
        ),
    ),
)


def fleet_endpoint(cloud: str) -> FleetAnalyticsEndpoint | None:
    for entry in ANALYTICS_FRESHNESS_FLEET:
        if entry.cloud == cloud:
            return entry
    return None


def checkable_endpoints() -> tuple[FleetAnalyticsEndpoint, ...]:
    """Entries the fleet check can actually fetch."""
    return tuple(entry for entry in ANALYTICS_FRESHNESS_FLEET if entry.checkable)


def deployed_clouds() -> tuple[str, ...]:
    """Every cloud the repo already claims to deploy.

    The union of ``STANDALONE_CLOUDS`` and both sides of
    ``ENCLAVE_CONTROL_PLANE_SOURCES``, for the same reason
    ``byok_v1_attestations.clouds_that_must_attest`` takes the union: neither
    table can weaken the requirement by omission. Today all three tables agree;
    a fourth deployment landing in any one of them makes this registry
    incomplete, loudly.
    """
    required = set(STANDALONE_CLOUDS)
    required.update(ENCLAVE_CONTROL_PLANE_SOURCES)
    required.update(
        cloud for sources in ENCLAVE_CONTROL_PLANE_SOURCES.values() for cloud in sources
    )
    return tuple(cloud for cloud in STANDALONE_CLOUDS if cloud in required) + tuple(
        sorted(cloud for cloud in required if cloud not in STANDALONE_CLOUDS)
    )


def registry_defects(
    clouds: Iterable[str] | None = None,
    *,
    registry: Sequence[FleetAnalyticsEndpoint] | None = None,
) -> list[str]:
    """Every way the registry and the deployment list can disagree.

    Returns operator-actionable sentences, not booleans: the caller is a CI
    test whose failure message has to be enough to fix the problem without
    reading this module.
    """
    entries = tuple(ANALYTICS_FRESHNESS_FLEET if registry is None else registry)
    expected = tuple(deployed_clouds() if clouds is None else clouds)
    defects: list[str] = []

    seen: set[str] = set()
    for entry in entries:
        if entry.cloud in seen:
            defects.append(
                f"{entry.cloud}: listed twice in ANALYTICS_FRESHNESS_FLEET; "
                "one entry per cloud, so there is one place to fix the URL"
            )
        seen.add(entry.cloud)
        if entry.status_url and entry.reason:
            defects.append(
                f"{entry.cloud}: has both a status_url and a reason. A reason "
                "means 'this cloud cannot be checked'; delete one of them."
            )
        elif not entry.status_url and not entry.reason:
            defects.append(
                f"{entry.cloud}: has no status_url and no reason. Give it the "
                "public /status.json of its CONTROL plane, or set reason= to "
                "state why it has none -- silence is what let AWS-EU go "
                "fifteen days unmeasured."
            )
        if entry.status_url and not entry.status_url.startswith("https://"):
            defects.append(
                f"{entry.cloud}: status_url must be https:// (got {entry.status_url!r}); "
                "this check carries no credentials and must still be authenticated "
                "about who answered it"
            )
        if entry.status_url and not entry.status_url.endswith("/status.json"):
            defects.append(
                f"{entry.cloud}: status_url must point at /status.json "
                f"(got {entry.status_url!r})"
            )

    for cloud in expected:
        if cloud not in seen:
            defects.append(
                f"{cloud}: deployed (it is in byok_v1_attestations.STANDALONE_CLOUDS "
                "or ENCLAVE_CONTROL_PLANE_SOURCES) but missing from "
                "ANALYTICS_FRESHNESS_FLEET in "
                "src/trusted_router/operational_analytics_fleet.py. Add "
                f'FleetAnalyticsEndpoint(cloud=\"{cloud}\", status_url=\"https://.../status.json\") '
                "pointing at that cloud's CONTROL-plane status page, or set "
                "reason= to record why it has no public one. A cloud with no "
                "drain-freshness endpoint has no way to report a drain that was "
                "never installed: its outbox just grows, exactly as AWS-EU's grew "
                "to 470,370 rows between 2026-08-02 and 2026-08-17."
            )

    for cloud in sorted(seen - set(expected)):
        defects.append(
            f"{cloud}: in ANALYTICS_FRESHNESS_FLEET but not a deployed cloud. "
            "Either it was retired -- then remove the entry, because a check "
            "against a decommissioned URL fails forever and teaches people to "
            "ignore this job -- or it belongs in "
            "byok_v1_attestations.STANDALONE_CLOUDS."
        )

    return defects
