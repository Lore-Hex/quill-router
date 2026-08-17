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

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/check-analytics-freshness.yml"

NOW = dt.datetime(2026, 8, 17, 12, 0, tzinfo=dt.UTC)


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
    }


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


def test_a_fake_cloud_in_external_live_regions_fails_the_binding() -> None:
    """A settings default is a deployment claim too: it lights the map dot up."""
    settings = Settings(
        environment="local",
        external_live_regions="aws-eu-west-1,azure-australiaeast,oracle-eu-frankfurt-1",
    )

    defects = registry_defects(settings=settings)

    assert len(defects) == 1
    assert defects[0].startswith("oracle:")
    assert "trusted_router.config.Settings.external_live_regions" in defects[0]


def test_a_cloud_id_with_no_namespace_does_not_invent_a_cloud() -> None:
    """GCP regions are bare (`us-central1`); they must not read as cloud "us"."""
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
    payloads = _all_healthy()
    payloads["azure"] = {ANALYTICS_STATUS_KEY: analytics_status_unavailable(REASON_NOT_CONFIGURED)}
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
    registry = (FleetAnalyticsEndpoint(cloud="aws", reason="control plane is not public"),)

    assert registry_defects(["aws"], registry=registry) == []

    result = evaluate_fleet({}, now=NOW, registry=registry, deployed=["aws"])

    assert result.problems == []
    assert len(result.unchecked) == 1
    assert result.unchecked[0].startswith("aws: NOT CHECKED")


def test_a_cloud_declared_outbox_free_is_unchecked_rather_than_failing() -> None:
    """Azure today: no TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED, so no outbox.

    It would publish `not_configured` forever. Failing on it daily is the
    cry-wolf shape the repo already fixed once, in the client-telemetry
    freshness check's `CANARY_COUNT_GATE_FROM` ramp-up guard.
    """
    payloads = _all_healthy()
    payloads["azure"] = {ANALYTICS_STATUS_KEY: analytics_status_unavailable(REASON_NOT_CONFIGURED)}

    result = evaluate_fleet(payloads, now=NOW)

    assert result.problems == []
    assert any(note.startswith("azure: NOT CHECKED") for note in result.unchecked)
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


def test_a_cloud_declared_outbox_free_still_fails_when_its_database_is_broken() -> None:
    """`expects_outbox=False` excuses `not_configured`, and nothing else."""
    payloads = _all_healthy()
    payloads["azure"] = {ANALYTICS_STATUS_KEY: analytics_status_unavailable(REASON_UNREACHABLE)}

    result = evaluate_fleet(payloads, now=NOW)

    assert [problem for problem in result.problems if problem.startswith("azure:")]


def test_unavailable_explanations_differ_per_reason() -> None:
    """One sentence for every reason told the operator the opposite of the truth.

    `not_configured` is not "could not read the outbox": there is no outbox, the
    database is fine, and an operator sent to check it wastes the incident.
    """
    payloads = _all_healthy()
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


def test_whole_fleet_healthy_reports_only_the_declared_absence() -> None:
    """Azure is unchecked by declaration; the other two must be clean."""
    payloads = _all_healthy()
    payloads["azure"] = {ANALYTICS_STATUS_KEY: analytics_status_unavailable(REASON_NOT_CONFIGURED)}

    result = evaluate_fleet(payloads, now=NOW)

    assert result.problems == []
    assert result.ok


@pytest.mark.parametrize("cloud", ["aws", "gcp"])
def test_any_single_cloud_going_stale_fails_the_fleet(cloud: str) -> None:
    """One broken cloud out of three must fail. Two healthy peers are not a quorum."""
    payloads = _all_healthy()
    payloads["azure"] = {ANALYTICS_STATUS_KEY: analytics_status_unavailable(REASON_NOT_CONFIGURED)}
    payloads[cloud] = _healthy(cloud, drain_lag_seconds=7_200.0)

    result = evaluate_fleet(payloads, now=NOW)

    assert [problem for problem in result.problems if problem.startswith(f"{cloud}:")]
    assert len(result.problems) == 1


@pytest.mark.parametrize("cloud", ["aws", "azure", "gcp"])
def test_a_cloud_that_publishes_no_analytics_section_fails(cloud: str) -> None:
    """Including the outbox-free one: no section means code too old to publish."""
    payloads = _all_healthy()
    payloads["azure"] = {ANALYTICS_STATUS_KEY: analytics_status_unavailable(REASON_NOT_CONFIGURED)}
    payloads[cloud] = {}

    result = evaluate_fleet(payloads, now=NOW)

    assert any(problem.startswith(f"{cloud}:") for problem in result.problems)
    assert any("does not publish drain lag" in problem for problem in result.problems)


@pytest.mark.parametrize("cloud", ["aws", "gcp"])
def test_a_cloud_reporting_unavailable_fails(cloud: str) -> None:
    payloads = _all_healthy()
    payloads["azure"] = {ANALYTICS_STATUS_KEY: analytics_status_unavailable(REASON_NOT_CONFIGURED)}
    payloads[cloud] = {ANALYTICS_STATUS_KEY: analytics_status_unavailable()}

    result = evaluate_fleet(payloads, now=NOW)

    assert any(problem.startswith(f"{cloud}:") for problem in result.problems)


@pytest.mark.parametrize("cloud", ["aws", "azure", "gcp"])
def test_a_cloud_that_could_not_be_fetched_fails(cloud: str) -> None:
    """Unreachable is a failure, not a skip: this job cannot tell down from lazy."""
    payloads = _all_healthy()
    payloads[cloud] = None

    result = evaluate_fleet(payloads, now=NOW)

    assert any("could not read" in problem for problem in result.problems)


def test_a_cloud_nobody_fetched_is_reported_rather_than_skipped() -> None:
    """The fleet-scale version of the original bug: absence rendering as health."""
    payloads = _all_healthy()
    payloads.pop("azure")

    result = evaluate_fleet(payloads, now=NOW)

    assert any(problem.startswith("azure: never fetched") for problem in result.problems)


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


def test_workflow_ships_without_a_schedule_until_every_cloud_publishes() -> None:
    """A green-by-construction cron is worse than no cron at all.

    Merging main auto-deploys the GCP control plane ONLY (deploy.yml); AWS-EU
    and Azure are hand-run scripts and are already behind. A `schedule:` landed
    in the same commit as the publisher would file an issue every morning about
    two clouds nobody has redeployed -- and the daily issue teaches people to
    close this job unread, at which point it is not watching the clouds it CAN
    read either. That is the same failure the client-telemetry check's
    `CANARY_COUNT_GATE_FROM` ramp-up guard exists to avoid, and the same reason
    this job's single-cloud predecessor shipped scheduleless.

    The precondition and the one-line follow-up are in the workflow header, and
    this test is what stops the cron arriving before the deploys do.
    """
    on_block = _on_block(_workflow())

    assert "schedule" not in on_block
    assert "workflow_dispatch" in on_block
    assert "push" in on_block


def test_workflow_header_states_the_precondition_and_the_follow_up() -> None:
    """A withheld trigger is only honest if the note says how to un-withhold it."""
    workflow = WORKFLOW.read_text()

    assert "OPERATOR STEPS BEFORE THE SCHEDULE IS ENABLED" in workflow
    assert "PRECONDITION" in workflow
    assert 'schedule: [{cron: "20 7 * * *"}]' in workflow
    assert "cries wolf" in workflow


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
