"""The binding: a deployed cloud cannot exist without a drain-freshness signal.

The AWS-EU drain went fifteen days (2026-08-02..17) delivering nothing while
470,370 rows piled up in the outbox, because the only backlog alarm in
existence is emitted by the drain process that was never installed. GCP was
healthy the whole time, so the fleet looked healthy.

Publishing `analytics.drain_lag_seconds` fixes the signal. These tests fix the
COVERAGE: they fail if `operational_analytics_fleet.ANALYTICS_FRESHNESS_FLEET`
and the repo's deployed-cloud list disagree in either direction, so adding a
fourth cloud is impossible without either giving it an endpoint or writing down
why it has none.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from clickhouse.check_fleet_analytics_freshness import evaluate_fleet
from trusted_router.byok_v1_attestations import (
    ENCLAVE_CONTROL_PLANE_SOURCES,
    STANDALONE_CLOUDS,
)
from trusted_router.operational_analytics_fleet import (
    ANALYTICS_FRESHNESS_FLEET,
    FleetAnalyticsEndpoint,
    checkable_endpoints,
    deployed_clouds,
    fleet_endpoint,
    registry_defects,
)
from trusted_router.operational_analytics_freshness import (
    ANALYTICS_STATUS_KEY,
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

    `deployed_clouds()` takes a union of two tables, and a union is exactly the
    kind of derivation that can be quietly widened until it proves nothing.
    """
    covered = {entry.cloud for entry in ANALYTICS_FRESHNESS_FLEET}
    missing = sorted(set(STANDALONE_CLOUDS) - covered)

    assert not missing, (
        f"{missing} are standalone deployments with no drain-freshness endpoint. "
        "Add them to ANALYTICS_FRESHNESS_FLEET in "
        "src/trusted_router/operational_analytics_fleet.py."
    )


def test_deployed_clouds_takes_both_tables() -> None:
    assert set(deployed_clouds()) >= set(STANDALONE_CLOUDS)
    assert set(deployed_clouds()) >= set(ENCLAVE_CONTROL_PLANE_SOURCES)


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
    assert "470,370" in message


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
            )
        ],
    )

    assert any("both a status_url and a reason" in defect for defect in defects)


def test_a_cloud_with_a_reason_is_allowed_but_still_reported_as_unchecked() -> None:
    """An honest "cannot be checked" must not read as "checked and healthy"."""
    registry = (
        FleetAnalyticsEndpoint(cloud="aws", reason="control plane is not public"),
    )

    assert registry_defects(["aws"], registry=registry) == []

    problems = evaluate_fleet({}, now=NOW, registry=registry)
    assert len(problems) == 1
    assert "not checkable over HTTP" in problems[0]


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


def _healthy(**overrides: object) -> dict[str, object]:
    section: dict[str, object] = dict(
        analytics_status_section(
            oldest_enqueued_at=NOW - dt.timedelta(seconds=30),
            now=NOW,
            outbox_depth=12,
        )
    )
    section.update(overrides)
    return {ANALYTICS_STATUS_KEY: section}


def _all_healthy() -> dict[str, dict[str, object] | None]:
    return {entry.cloud: _healthy() for entry in checkable_endpoints()}


def test_whole_fleet_healthy_reports_nothing() -> None:
    assert evaluate_fleet(_all_healthy(), now=NOW) == []


@pytest.mark.parametrize("cloud", [entry.cloud for entry in checkable_endpoints()])
def test_any_single_cloud_going_stale_fails_the_fleet(cloud: str) -> None:
    """One broken cloud out of three must fail. Two healthy peers are not a quorum."""
    payloads = _all_healthy()
    payloads[cloud] = _healthy(drain_lag_seconds=7_200.0)

    problems = evaluate_fleet(payloads, now=NOW)

    assert [problem for problem in problems if problem.startswith(f"{cloud}:")]
    assert len(problems) == 1


@pytest.mark.parametrize("cloud", [entry.cloud for entry in checkable_endpoints()])
def test_a_cloud_that_publishes_no_analytics_section_fails(cloud: str) -> None:
    payloads = _all_healthy()
    payloads[cloud] = {}

    problems = evaluate_fleet(payloads, now=NOW)

    assert any(problem.startswith(f"{cloud}:") for problem in problems)
    assert any("does not publish drain lag" in problem for problem in problems)


@pytest.mark.parametrize("cloud", [entry.cloud for entry in checkable_endpoints()])
def test_a_cloud_reporting_unavailable_fails(cloud: str) -> None:
    payloads = _all_healthy()
    payloads[cloud] = {ANALYTICS_STATUS_KEY: analytics_status_unavailable()}

    problems = evaluate_fleet(payloads, now=NOW)

    assert any(problem.startswith(f"{cloud}:") for problem in problems)


@pytest.mark.parametrize("cloud", [entry.cloud for entry in checkable_endpoints()])
def test_a_cloud_that_could_not_be_fetched_fails(cloud: str) -> None:
    """Unreachable is a failure, not a skip: this job cannot tell down from lazy."""
    payloads = _all_healthy()
    payloads[cloud] = None

    problems = evaluate_fleet(payloads, now=NOW)

    assert any("could not read" in problem for problem in problems)


def test_a_cloud_nobody_fetched_is_reported_rather_than_skipped() -> None:
    """The fleet-scale version of the original bug: absence rendering as health."""
    payloads = _all_healthy()
    payloads.pop("azure")

    problems = evaluate_fleet(payloads, now=NOW)

    assert any(problem.startswith("azure: never fetched") for problem in problems)


def test_registry_defects_surface_inside_the_fleet_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CI is the gate, but a broken registry must also fail the live job.

    Otherwise the only thing standing between a fourth cloud and no coverage is
    a test somebody could mark xfail, and the scheduled job would go on
    reporting OK about the three clouds it still knew about.
    """
    monkeypatch.setattr(
        "trusted_router.operational_analytics_fleet.ANALYTICS_FRESHNESS_FLEET",
        (fleet_endpoint("aws"),),
    )

    problems = evaluate_fleet({"aws": _healthy()}, now=NOW, registry=())

    registry_problems = [problem for problem in problems if problem.startswith("registry:")]
    assert {"azure", "gcp"} <= {problem.split()[1].rstrip(":") for problem in registry_problems}


# ---------------------------------------------------------------------------
# The workflow now covers the fleet, on a schedule.
# ---------------------------------------------------------------------------


def test_workflow_has_a_schedule_now_that_the_field_is_published() -> None:
    """The predecessor shipped without one because nothing published the field.

    Matched at the two-space indent a real trigger sits at under `on:`, so a
    commented-out example cannot satisfy it.
    """
    workflow = WORKFLOW.read_text()

    assert "\n  schedule:\n" in workflow
    assert "cron:" in workflow
    assert "\n  workflow_dispatch:" in workflow


def test_workflow_checks_every_registry_entry() -> None:
    """It must run the FLEET module, unrestricted by default.

    The single-cloud alias exists for incidents; a scheduled job that used it
    would be green for whichever cloud it named, which is the outage's own
    shape.
    """
    workflow = WORKFLOW.read_text()

    assert "clickhouse.check_fleet_analytics_freshness" in workflow
    # --cloud is only ever added from a workflow_dispatch input, never on the
    # scheduled path.
    assert 'if [ -n "${CLOUD}" ]; then' in workflow
    assert "python3 -m clickhouse.check_aws_analytics_freshness" not in workflow


def test_workflow_needs_no_cloud_credentials() -> None:
    """The whole reason for publishing the field: the check is a plain GET."""
    workflow = WORKFLOW.read_text()

    assert "configure-aws-credentials" not in workflow
    assert "role-to-assume" not in workflow
    assert "google-github-actions/auth" not in workflow
    assert "secrets." not in workflow
