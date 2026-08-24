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
    one is a map of dots, two are region settings, and one is the peer list
    every cloud already polls -- and a union cannot be weakened by any single
    one of them going stale. A cloud appearing in ANY of them makes this
    registry incomplete, loudly, and the failure message names every source it
    consulted so the reader can see what the requirement is made of. Each
    source must also be NON-EMPTY: a union over an empty source is satisfied by
    anything, so a source that silently degraded to zero clouds would leave
    this check passing about a requirement it had stopped stating.

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
from trusted_router.synthetic import fleet as synthetic_fleet

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
    #: Region ids this source declares that NO table in
    #: :mod:`trusted_router.regions` can attribute to a cloud. Reported as
    #: defects rather than guessed at: guessing a prefix mints phantom clouds
    #: out of ordinary GCP region ids, and shrugging them off as GCP hides a
    #: real fourth cloud, which is the failure this whole module is about.
    unattributable: tuple[str, ...] = ()
    #: A bound source that is empty proves nothing -- the union it feeds is
    #: vacuously satisfied, so the day it silently degrades to zero clouds is
    #: a day this check quietly stops being a check. Sources are therefore
    #: required to be non-empty unless they say here that empty is legitimate,
    #: which is a claim a reader can argue with.
    allow_empty: bool = False


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
        note=(
            "The Azure control plane's own hostname, set as STATUS_HOST by "
            "scripts/deploy/azure_control_plane.sh and pointed at the container app "
            "by that script's Cloud DNS step. Production now publishes a live "
            "Postgres operational-analytics outbox lag from this endpoint. Keep "
            "expects_outbox=True so a stalled or removed Azure drain fails the "
            "same fleet-wide freshness gate as AWS and GCP."
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

    Raises rather than returning ``""`` when the field is not a string. The
    empty string is the one value that cannot fail: it parses to no clouds, and
    a source with no clouds satisfies the union vacuously. So a field that
    changes type -- to a list, or to ``None`` -- would silently delete a
    deployment source instead of failing, which is the exact way this check
    would stop being a check without anybody noticing.
    """
    value = getattr(settings, field) if settings is not None else Settings.model_fields[field].default
    if not isinstance(value, str):
        raise TypeError(
            f"Settings.{field} is bound as a deployment source in "
            "trusted_router.operational_analytics_fleet.deployment_sources(), but its "
            f"value is {type(value).__name__}, not str. Parse it into cloud names "
            "explicitly there -- an unparseable source reads as zero clouds, and zero "
            "clouds is a coverage requirement that is satisfied by anything."
        )
    return value


def _region_clouds(settings: Settings | None, field: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(clouds, unattributable region ids)`` for one comma-separated setting.

    Split in two because "we do not know which cloud this is" must not render
    as either answer: not as a cloud (a phantom named after a GCP geography)
    and not as silence (a real fourth cloud, covered by nothing).
    """
    clouds: set[str] = set()
    unknown: set[str] = set()
    for item in _setting(settings, field).split(","):
        region = item.strip()
        if not region:
            continue
        cloud = regions.cloud_for_region(region)
        if cloud is None:
            unknown.add(region)
        else:
            clouds.add(cloud)
    return tuple(sorted(clouds)), tuple(sorted(unknown))


def _peer_clouds(settings: Settings | None) -> tuple[str, ...]:
    """Cloud names in ``synthetic_fleet_peers`` ("cloud=base_url,...").

    Parsed through :func:`trusted_router.synthetic.fleet.parse_fleet_peers`,
    the same function the probes use, so this binding cannot disagree with the
    list that is actually fetched.
    """
    raw = _setting(settings, "synthetic_fleet_peers")
    return tuple(sorted({name for name, _url in synthetic_fleet.parse_fleet_peers(raw)}))


def deployment_sources(settings: Settings | None = None) -> tuple[DeploymentSource, ...]:
    """Every table in this repo that declares a deployment, and its clouds.

    Read at CALL time, never captured at import, so a test can add a fake cloud
    to any one of them and watch the binding bite. Each entry's ``name`` is
    printed in the failure message: the point of a union is lost if the reader
    cannot see what it is a union OF.

    WHAT IS DELIBERATELY NOT HERE, and why -- because "I did not think of it"
    and "it does not declare a deployment" are indistinguishable from outside:

    * ``catalog_data``'s provider list and ``bedrock_group_buy``'s spend
      sources both contain the string ``"azure"``. They name a VENDOR we route
      to or a box a customer ticks on a form; neither says TrustedRouter is
      deployed on that cloud, and binding them would demand a status endpoint
      for every provider in the catalogue.
    * ``Settings.regions`` / ``primary_region`` are the attested-gateway
      regions, which are GCP-only by construction (they template
      ``api-{region}.quillrouter.com`` and Cloud Run URLs). They can add a
      REGION, never a cloud, and ``marketing_regions`` is a superset of them.
    * ``Settings.synthetic_status_us_url`` / ``_eu_url`` are two URLs of the
      GCP deployment's own status service, keyed by geography rather than by
      cloud.
    """
    external_clouds, external_unknown = _region_clouds(settings, "external_live_regions")
    marketing_clouds, marketing_unknown = _region_clouds(settings, "marketing_regions")
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
            clouds=external_clouds,
            unattributable=external_unknown,
        ),
        DeploymentSource(
            name="trusted_router.config.Settings.marketing_regions",
            clouds=marketing_clouds,
            unattributable=marketing_unknown,
        ),
        DeploymentSource(
            # The closest relative of ANALYTICS_FRESHNESS_FLEET in the repo:
            # cloud-name-keyed, config-as-code, and holding a public status URL
            # per deployment. It is how every cloud watches every other cloud's
            # /status.json, so a fourth cloud added here is a fourth cloud whose
            # status page this repo already reads -- while nothing read its
            # drain lag. Bound for exactly that reason: a peer list that knows
            # about a cloud the freshness registry does not is the outage's
            # shape with the two halves swapped.
            name="trusted_router.config.Settings.synthetic_fleet_peers",
            clouds=_peer_clouds(settings),
        ),
    )


def deployed_clouds(settings: Settings | None = None) -> tuple[str, ...]:
    """Every cloud the repo already claims to deploy, from every source.

    A union, for the same reason
    ``byok_v1_attestations.clouds_that_must_attest`` takes one: no single table
    can weaken the requirement by omission. Today all five sources agree on
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

    for source in sources:
        if not source.clouds and not source.allow_empty:
            defects.append(
                f"{source.name}: declares NO clouds at all. It is bound as a deployment "
                "source, and a union over an empty source is satisfied by anything -- "
                "so this check would go on passing while one of the tables it is made "
                "of had quietly stopped saying anything. Either it really is empty now "
                "(pass allow_empty=True in deployment_sources() and say why), or it was "
                "renamed, retyped, or reparsed wrongly."
            )
        for region in source.unattributable:
            defects.append(
                f"{source.name}: region id {region!r} belongs to no cloud this repo "
                "knows. Add it to trusted_router.regions.GCP_REGION_GEO (a new GCP "
                "region) or to trusted_router.regions.MULTICLOUD_REGION_GEO (a "
                "deployment on another cloud -- that row is also what makes its "
                "'<cloud>-' namespace recognisable everywhere else). It is not guessed "
                "at in either direction on purpose: reading the prefix as a cloud "
                "invents one out of an ordinary GCP geography and fails CI with a "
                "nonsense name, and defaulting it to GCP would let a FOURTH CLOUD "
                "arrive with no drain-freshness endpoint and nothing to say so. The "
                "marketing map already drops this id silently (regions.py: `if geo is "
                "None: continue`), so it is unrendered as well as unattributed."
            )

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
