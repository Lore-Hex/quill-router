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

WHAT COUNTS AS "A DEPLOYED CLOUD", AND WHY IT IS NOT ONE TABLE
    An earlier revision of this module derived the requirement from
    ``byok_v1_attestations.STANDALONE_CLOUDS`` alone. That table's own comment
    says it was TRANSCRIBED BY HAND out of another repository and re-reads
    nothing, so binding coverage to it means a fourth cloud is required to have
    a freshness endpoint only once somebody edits a BYOK module while thinking
    about BYOK. That is the outage's shape again: a check that is complete
    because of a list somebody remembered to update.

    So the requirement is the UNION of every table in this repository that
    declares a deployment exists, listed in :func:`deployment_sources`. They
    disagree in kind on purpose -- one is a hand-transcribed enclave topology,
    one is a map of dots, one is a config default -- and a union cannot be
    weakened by any single one of them going stale. A cloud appearing in ANY of
    them makes this registry incomplete, loudly, and the failure message names
    every source it consulted so the reader can see what the requirement is
    made of.

WHY A ``reason`` FIELD AND NOT AN OMISSION
    A cloud with no public control-plane status URL is a real possibility -- an
    internal-only plane, a deployment behind a private ALB. The registry makes
    that case *say so*, because the alternative is a cloud that is silently
    absent from the fleet list, which is the same "configured, healthy, and
    empty" shape as the outage above: everything reports success and nothing
    was measured. Such an entry is reported by the fleet check as EXPLICITLY
    UNCHECKED on every run -- printed, counted, never a silent skip -- and does
    not fail the job, because a check that fails forever about something nobody
    can fix is a check people learn to ignore.

WHY ``expects_outbox`` AND NOT A SECOND ``reason``
    Azure is reachable, publishes a status page, and has no operational-analytics
    outbox at all: ``scripts/deploy/azure_control_plane.sh`` never sets
    ``TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED``, so the flag defaults off and
    ``PostgresStore`` holds no outbox to read. Its section says
    ``not_configured`` and would say so forever. Retiring it behind ``reason=``
    would work, and would also throw away the ability to notice the day it
    changes: an outbox that gets enabled on Azure would go unwatched exactly as
    AWS-EU's was. ``expects_outbox=False`` instead keeps fetching the page and
    asserts the ABSENCE -- unchecked while it publishes ``not_configured``, a
    FAILURE the moment it publishes a real lag, which is the moment somebody
    needs to come back here and start watching it.

NOTHING HERE DOES IO. :mod:`clickhouse.check_fleet_analytics_freshness` fetches.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# Imported as MODULES, not as names. `from x import TABLE` binds the object
# once at import and then re-reads nothing, which is the same defect this
# module exists to close one level up: the binding would be a snapshot of what
# the deployment tables said the first time somebody imported them.
from trusted_router import byok_v1_attestations, regions
from trusted_router.config import Settings
from trusted_router.operational_analytics_freshness import (
    BACKEND_POSTGRES,
    BACKEND_SPANNER,
)

#: Backends a control plane can legitimately answer this question from. The
#: registry pins one per cloud so a status page belonging to a DIFFERENT cloud
#: cannot answer for this one; see :class:`FleetAnalyticsEndpoint`.
KNOWN_BACKENDS: frozenset[str] = frozenset({BACKEND_SPANNER, BACKEND_POSTGRES})


@dataclass(frozen=True)
class DeploymentSource:
    """One table in this repo that declares a deployment exists.

    ``name`` is quoted verbatim into the failure message. It is the dotted path
    an operator has to open, so it is written as one.
    """

    name: str
    clouds: tuple[str, ...]


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
    #: Storage backend this cloud's control plane MUST answer from. Checked
    #: against the published ``analytics.backend``, so two entries pointed at
    #: one plane cannot both look checked: the plane answers for its own cloud
    #: and the other entry fails. Required for every checkable entry.
    expected_backend: str | None = None
    #: Whether this deployment is supposed to run an operational-analytics
    #: outbox at all. ``False`` means "assert the absence": ``not_configured``
    #: is reported as explicitly unchecked, and a live lag is a FAILURE saying
    #: to come back here and start watching it.
    expects_outbox: bool = True
    note: str = ""

    @property
    def checkable(self) -> bool:
        return bool(self.status_url)


#: One entry per deployed cloud. Verified reachable and returning HTTP 200
#: with a JSON body on 2026-08-17.
ANALYTICS_FRESHNESS_FLEET: tuple[FleetAnalyticsEndpoint, ...] = (
    FleetAnalyticsEndpoint(
        cloud="aws",
        status_url="https://gchircrcif.eu-west-3.awsapprunner.com/status.json",
        expected_backend=BACKEND_POSTGRES,
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
        expected_backend=BACKEND_POSTGRES,
        expects_outbox=False,
        note=(
            "The Azure control plane's own hostname, set as STATUS_HOST by "
            "scripts/deploy/azure_control_plane.sh and pointed at the container app "
            "by that script's Cloud DNS step. It runs PostgresStore, so it COULD "
            "hold the same outbox shape as AWS -- but that deploy script sets no "
            "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED at all, the setting defaults "
            "to False (config.py), and PostgresStore therefore builds no outbox. "
            "Verified 2026-08-17: no cloud published an `analytics` section yet, "
            "so this is read off the deploy script and the default, not off the "
            "wire. There is no drain here to be missing; expects_outbox=False "
            "asserts that absence and fails the day it stops being true."
        ),
    ),
    FleetAnalyticsEndpoint(
        cloud="gcp",
        status_url="https://trustedrouter.com/status.json",
        expected_backend=BACKEND_SPANNER,
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


def _setting(settings: Settings | None, field: str) -> str:
    """One comma-separated settings field, from a live Settings or the default.

    The repo's declared DEFAULT is the right value offline: CI has no TR_ env,
    and what the binding is about is what this repository claims to deploy. A
    live ``Settings`` is accepted so a running control plane can ask the same
    question about its own configuration.
    """
    if settings is not None:
        value = getattr(settings, field)
        return value if isinstance(value, str) else ""
    default = Settings.model_fields[field].default
    return default if isinstance(default, str) else ""


def _region_clouds(settings: Settings | None, field: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                regions.cloud_for_region(item.strip())
                for item in _setting(settings, field).split(",")
                if item.strip()
            }
        )
    )


def deployment_sources(settings: Settings | None = None) -> tuple[DeploymentSource, ...]:
    """Every table in this repo that declares a deployment, and its clouds.

    Read at CALL time, never captured at import, so a test can add a fake cloud
    to any one of them and watch the binding bite. Each entry's ``name`` is
    printed in the failure message: the point of a union is lost if the reader
    cannot see what it is a union OF.
    """
    return (
        DeploymentSource(
            name="trusted_router.byok_v1_attestations.clouds_that_must_attest()"
            " (STANDALONE_CLOUDS + ENCLAVE_CONTROL_PLANE_SOURCES)",
            clouds=byok_v1_attestations.clouds_that_must_attest(),
        ),
        DeploymentSource(
            name="trusted_router.regions.MULTICLOUD_REGION_GEO",
            clouds=tuple(sorted({geo.cloud for geo in regions.MULTICLOUD_REGION_GEO.values()})),
        ),
        DeploymentSource(
            name="trusted_router.config.Settings.external_live_regions",
            clouds=_region_clouds(settings, "external_live_regions"),
        ),
        DeploymentSource(
            name="trusted_router.config.Settings.marketing_regions",
            clouds=_region_clouds(settings, "marketing_regions"),
        ),
    )


def deployed_clouds(settings: Settings | None = None) -> tuple[str, ...]:
    """Every cloud the repo already claims to deploy, from every source.

    A union, for the same reason
    ``byok_v1_attestations.clouds_that_must_attest`` takes one: no single table
    can weaken the requirement by omission. Today all four sources agree on
    ``aws``/``azure``/``gcp``; a fourth deployment landing in any ONE of them
    makes this registry incomplete.
    """
    required: set[str] = set()
    for source in deployment_sources(settings):
        required.update(source.clouds)
    return tuple(sorted(required))


def _sources_naming(cloud: str, sources: Sequence[DeploymentSource]) -> str:
    named = [source.name for source in sources if cloud in source.clouds]
    return ", ".join(named) if named else "the caller's explicit cloud list"


def registry_defects(
    clouds: Iterable[str] | None = None,
    *,
    registry: Sequence[FleetAnalyticsEndpoint] | None = None,
    settings: Settings | None = None,
) -> list[str]:
    """Every way the registry and the deployment list can disagree.

    Returns operator-actionable sentences, not booleans: the caller is a CI
    test whose failure message has to be enough to fix the problem without
    reading this module.
    """
    entries = tuple(ANALYTICS_FRESHNESS_FLEET if registry is None else registry)
    sources = deployment_sources(settings)
    expected = tuple(deployed_clouds(settings) if clouds is None else clouds)
    source_list = "; ".join(source.name for source in sources)
    defects: list[str] = []

    seen: set[str] = set()
    urls: dict[str, str] = {}
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
        if entry.status_url:
            # Two entries on one URL is the outage's shape in miniature: the
            # run reports two clouds checked, one control plane answered, and
            # the cloud nobody read looks exactly as healthy as the one
            # somebody did. Offline, because a runtime check cannot catch the
            # aws/azure case -- both are Postgres, so the backend they publish
            # is identical and only the URL tells them apart.
            twin = urls.get(entry.status_url)
            if twin is not None:
                defects.append(
                    f"{entry.cloud}: shares status_url {entry.status_url!r} with "
                    f"{twin}. Two clouds cannot have one control plane: whichever "
                    "one answers, the run would report BOTH as checked and only "
                    "one of them was. Point this entry at its own cloud's "
                    "/status.json."
                )
            else:
                urls[entry.status_url] = entry.cloud
            if entry.expected_backend is None:
                defects.append(
                    f"{entry.cloud}: checkable but has no expected_backend. Set it to "
                    f"one of {sorted(KNOWN_BACKENDS)} so a status page belonging to a "
                    "DIFFERENT cloud cannot answer this cloud's question."
                )
            elif entry.expected_backend not in KNOWN_BACKENDS:
                defects.append(
                    f"{entry.cloud}: expected_backend={entry.expected_backend!r} is not "
                    f"one of {sorted(KNOWN_BACKENDS)}"
                )

    for cloud in expected:
        if cloud not in seen:
            defects.append(
                f"{cloud}: deployed -- declared by {_sources_naming(cloud, sources)} "
                "-- but missing from ANALYTICS_FRESHNESS_FLEET in "
                "src/trusted_router/operational_analytics_fleet.py. Add "
                f'FleetAnalyticsEndpoint(cloud="{cloud}", '
                'status_url="https://.../status.json", expected_backend=...) '
                "pointing at that cloud's CONTROL-plane status page; or set "
                "reason= to record why it has no public one; or set "
                "expects_outbox=False if it runs no analytics pipeline at all. "
                "A cloud with no drain-freshness endpoint has no way to report a "
                "drain that was never installed: its outbox just grows, exactly "
                "as AWS-EU's grew to 470,370 rows between 2026-08-02 and "
                f"2026-08-17. Deployment sources consulted: {source_list}."
            )

    for cloud in sorted(seen - set(expected)):
        defects.append(
            f"{cloud}: in ANALYTICS_FRESHNESS_FLEET but not a deployed cloud. "
            "Either it was retired -- then remove the entry, because a check "
            "against a decommissioned URL fails forever and teaches people to "
            "ignore this job -- or it is missing from every deployment source: "
            f"{source_list}."
        )

    return defects
