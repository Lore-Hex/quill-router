"""A cloud cannot be added without becoming checkable.

The AWS-EU cloud served production traffic for fifteen days with no analytics
pipeline: no drain, 470,897 rows stuck in the DSQL outbox, `activity_generations`
empty, and total silence, because the only backlog alarm is emitted BY the drain
that was missing. The bring-up script had ended by PRINTING next steps and
exiting 0.

Four things are pinned here.

1. **The cloud binding.** `declared_clouds()` delegates to
   `operational_analytics_fleet.deployed_clouds()` — the union of every table in
   this repo that declares a deployment — so a cloud added to any one of them is
   a cloud the completeness check immediately expects to know about, and
   `registry_gaps()` fails until it does.
   `test_a_new_cloud_fails_ci_until_it_is_checkable` adds a fake cloud and
   asserts the failure, which is the whole mechanism: you cannot get a cloud
   into this codebase quietly.

2. **The script registry, and what it is FOR.** Which deploy scripts must end in
   the gate is data on `CloudRollout`, not a list in this file. What is checked
   here is structural — the file exists, an unbound script carries a reason,
   nothing unclaimed calls the verifier. Whether a script really runs the gate
   is proven by RUNNING it, in `tests/test_deploy_script_execution.py`; this
   file deliberately makes no claim it cannot support, because the previous
   version made exactly that mistake with a regex.

3. **The gate takes no input from anywhere.** Deploy scripts inherit their
   caller's environment, so an env-tunable bound or status URL is a remote
   control for the gate; a flag needs an outcome of its own to be safe, and an
   outcome that exists to make an override safe is more machinery to get wrong.
   There is neither. The tests below run the real shell with those variables
   exported and assert on what it FETCHED and what argv it PASSED, not on what
   it printed.

4. **The stages, and the one thing a passing run may claim.** Stage (e) is not
   redundant with stage (d), because a drained outbox and a disabled outbox
   publish the same `drain_lag_seconds: 0.0`. A run either VERIFIED the cloud or
   it did not: there is no waiver, no exemption and no verdict taxonomy, because
   two review rounds found bugs inside that machinery rather than around it. The
   green sentence is written so that it is true of every run that reaches it.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from trusted_router import cloud_rollout_completeness as crc
from trusted_router import operational_analytics_fleet as fleet
from trusted_router import regions
from trusted_router.operational_analytics_freshness import (
    ANALYTICS_STATUS_KEY,
    AVAILABLE_FIELD,
    DEFAULT_MAX_DRAIN_LAG_SECONDS,
    REASON_NOT_CONFIGURED,
    REASON_UNREACHABLE,
    analytics_status_section,
    analytics_status_unavailable,
)

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts" / "deploy" / "verify_cloud_complete.sh"

NOW = dt.datetime(2026, 8, 17, 12, 0, tzinfo=dt.UTC)


@dataclass(frozen=True)
class _ModuleRun:
    """What one CLI invocation exited with, and what it wrote to each stream."""

    returncode: int
    stdout: str
    stderr: str


def _verified(stderr: str) -> bool:
    """Did the run print the ONE green outcome?

    Spelled with the leading newline on purpose: "NOT VERIFIED" contains
    "VERIFIED", so the naive check passes on the failure banner and every
    assertion written with it is vacuous.
    """
    return "\nVERIFIED —" in stderr


def _run_module(*argv: str) -> _ModuleRun:
    """Call the module's CLI in process. The exit status IS the verdict.

    Worth stating once, since it is the whole output contract: nothing is parsed
    out of either stream to decide whether a stage held. stdout carries plain
    notes the shell reprints under the outcome; stderr carries the blockers.
    """
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = crc.main(list(argv))
    return _ModuleRun(code, out.getvalue(), err.getvalue())


def _payload(section: dict[str, Any] | None) -> dict[str, Any]:
    body: dict[str, Any] = {"overall_status": "up"}
    if section is not None:
        body[ANALYTICS_STATUS_KEY] = section
    return body


def _healthy_section() -> dict[str, Any]:
    return analytics_status_section(
        oldest_enqueued_at=NOW - dt.timedelta(seconds=30),
        now=NOW,
        outbox_depth=12,
    )


# ---------------------------------------------------------------------------
# The binding: adding a cloud is adding an obligation.
# ---------------------------------------------------------------------------


def test_registry_is_bound_to_the_declared_clouds() -> None:
    """Every declared cloud is reachable by the completeness check.

    No list of clouds is written here on purpose — a test that hard-codes
    {aws, azure, gcp} is another copy of the fleet, and the copies are what
    drift. The assertion is the relation: whatever the deployment tables
    declare, `verify_cloud_complete.sh` can check it.
    """
    gaps = crc.registry_gaps()
    assert gaps == [], "\n".join(gaps)
    registry = crc.freshness_registry()
    for cloud in crc.declared_clouds():
        assert registry[cloud].startswith("https://")
        assert cloud in crc.ROLLOUT_REGISTRY


def test_the_cloud_list_is_the_fleet_module_s_and_not_a_second_copy() -> None:
    """One union, in one place, named by the module that owns the question.

    An earlier revision re-derived the union here from three of the five tables
    `operational_analytics_fleet.deployment_sources()` reads. Two unions that
    disagree is the outage's shape with the halves swapped, so this asserts the
    delegation rather than the answer.
    """
    assert crc.declared_clouds() == fleet.deployed_clouds()


def test_the_gate_asks_the_front_end_that_holds_the_dsql_connection() -> None:
    """The AWS URL is the App Runner plane, not the vanity hostname.

    This is the concrete reason stage (a) reads the fleet registry instead of
    anything else. `aws.trustedrouter.com` fronts the Fargate control plane
    through Global Accelerator; the deployment that holds the Aurora DSQL
    connection — and whose drain was missing for fifteen days — is the tr-eu App
    Runner service. A gate pointed at the wrong AWS front end would have been
    green throughout the outage.
    """
    aws = crc.freshness_registry()["aws"]
    entry = fleet.fleet_endpoint("aws")
    assert entry is not None and entry.status_url == aws
    assert "aws.trustedrouter.com" not in aws
    assert "awsapprunner.com" in aws


def test_a_new_cloud_fails_ci_until_it_is_checkable(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE test. A cloud added to a deployment table with nothing else is a defect.

    This is the shape of the AWS-EU outage expressed as a unit test: a cloud
    exists, and no part of the system treats "it has no analytics pipeline" as
    an incomplete rollout. Adding the region below is all it takes to reproduce
    it, and `registry_gaps()` is what refuses.
    """
    monkeypatch.setitem(
        regions.MULTICLOUD_REGION_GEO,
        "oracle-eu-frankfurt-1",
        regions.RegionGeo("oracle-eu-frankfurt-1", "Frankfurt", 50.111, 8.682, cloud="oracle"),
    )

    assert "oracle" in crc.declared_clouds()
    gaps = crc.registry_gaps()
    assert gaps, "a cloud with no freshness endpoint and no rollout entry must fail"

    joined = "\n".join(gaps)
    # The message has to name the fix, not just the fact. A CI failure that says
    # "oracle: missing" teaches nobody what a complete cloud is.
    assert "oracle: declared as a deployment" in joined
    assert "operational_analytics_fleet.py" in joined
    assert "ROLLOUT_REGISTRY" in joined
    assert "verify_cloud_complete.sh" in joined


def test_registry_gap_survives_a_half_finished_addition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wiring the status URL but not the rollout entry is still a gap.

    The likelier real mistake: someone adds the endpoint so the fleet page looks
    complete, and never adds what "done" means for that cloud.
    """
    monkeypatch.setitem(
        regions.MULTICLOUD_REGION_GEO,
        "oracle-eu-frankfurt-1",
        regions.RegionGeo("oracle-eu-frankfurt-1", "Frankfurt", 50.111, 8.682, cloud="oracle"),
    )
    monkeypatch.setattr(
        crc,
        "freshness_registry",
        lambda: {
            **{c: "https://x/status.json" for c in ("aws", "azure", "gcp")},
            "oracle": "https://oracle.trustedrouter.com/status.json",
        },
    )
    gaps = crc.registry_gaps()
    assert len(gaps) == 1
    assert "absent from ROLLOUT_REGISTRY" in gaps[0]


def test_a_cloud_the_fleet_declares_uncheckable_is_still_a_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`reason=` in the fleet registry is not a pass here.

    The fleet checker treats a cloud with no public status URL as EXPLICITLY
    UNCHECKED and does not fail — reasonably, since a daily job that fails
    forever about something nobody can fix is a job people mute. A ROLLOUT is a
    different question: a cloud nobody outside can measure cannot be declared
    done, whatever the reason, so this reports it with the reason attached.
    """
    monkeypatch.setattr(
        fleet,
        "ANALYTICS_FRESHNESS_FLEET",
        (
            *[e for e in fleet.ANALYTICS_FRESHNESS_FLEET if e.cloud != "azure"],
            fleet.FleetAnalyticsEndpoint(cloud="azure", reason="internal-only ALB, no public page"),
        ),
    )
    monkeypatch.setattr(
        crc, "ANALYTICS_FRESHNESS_FLEET", fleet.ANALYTICS_FRESHNESS_FLEET
    )
    gaps = crc.registry_gaps()
    assert any("UNCHECKABLE over HTTP" in gap and "internal-only ALB" in gap for gap in gaps), gaps


def test_every_registered_cloud_names_a_control_plane_script_that_exists() -> None:
    """A fix instruction pointing at a file that is not there is not a fix."""
    for cloud, entry in crc.ROLLOUT_REGISTRY.items():
        assert (ROOT / entry.control_plane_script).is_file(), (
            f"{cloud}: {entry.control_plane_script} does not exist"
        )


# ---------------------------------------------------------------------------
# (a)-(d): what the cloud publishes about itself.
# ---------------------------------------------------------------------------


def test_unknown_cloud_is_a_failure_not_a_skip() -> None:
    blockers = crc.registry_blockers("oracle")
    assert blockers
    assert any("no ROLLOUT_REGISTRY entry" in b for b in blockers)


def test_missing_analytics_section_fails() -> None:
    blockers = crc.section_blockers("aws", _payload(None))
    assert blockers
    assert "publishes no 'analytics' section" in blockers[0]


def test_the_absent_section_and_the_unreadable_body_get_different_exits(
    tmp_path: Path,
) -> None:
    """The two states stage (b) can be in, and why only one of them keeps a code.

    "This cloud publishes no analytics section" is the state every cloud is in
    until a control plane that publishes it is deployed, and the run installing
    a drain hits it by construction — so it keeps its own exit, 5, and its own
    words. A body that is not the status document at all (a CDN interstitial, a
    captive portal, a truncated response) is a plain failure: deploying a newer
    control plane does nothing for it, and the message says so instead of
    borrowing the other one's advice.
    """
    absent = tmp_path / "absent.json"
    absent.write_text(json.dumps({"data": {"overall_status": "up"}}))
    run = _run_module("section", "--cloud", "aws", "--status-file", str(absent))
    assert run.returncode == crc.EXIT_NOT_OBSERVABLE
    assert "publishes no 'analytics' section" in run.stderr

    interstitial = tmp_path / "interstitial.html"
    interstitial.write_text("<!DOCTYPE html><title>Just a moment...</title>")
    run = _run_module("section", "--cloud", "aws", "--status-file", str(interstitial))
    assert run.returncode == 1
    assert "Just a moment" in run.stderr
    assert "redeploying will not change it" in run.stderr


def test_a_body_that_is_not_the_status_document_is_its_own_finding() -> None:
    """"No analytics section" and "not the status page at all" are different.

    Both used to end in NOT YET OBSERVABLE, whose fix instruction is "deploy a
    control plane built from a newer commit". That does nothing about a
    Cloudflare challenge page, a captive portal, or a body cut off mid-stream.
    """
    with pytest.raises(crc.UnreadableStatusPage):
        crc.unwrap_status_payload(["not", "an", "object"])


def test_unavailable_is_not_the_same_as_empty() -> None:
    """"I could not look" must never collapse into "there was nothing to see"."""
    blockers = crc.available_blockers(
        "aws", _payload(analytics_status_unavailable(REASON_UNREACHABLE))
    )
    assert blockers
    assert "available is false" in blockers[0]
    # Names the actual command, from the registry.
    assert "aws_eu_clickhouse_drain_install.sh" in blockers[0]


def test_not_configured_is_not_the_same_as_unreachable() -> None:
    """A cloud saying "I run no outbox" is a configuration, not a fault.

    Both fail, and neither is excusable — there is no exemption any more — but
    they send the reader to different places, so they get different sentences.
    """
    configured_off = crc.available_blockers(
        "azure", _payload(analytics_status_unavailable(REASON_NOT_CONFIGURED))
    )
    broken = crc.available_blockers(
        "azure", _payload(analytics_status_unavailable(REASON_UNREACHABLE))
    )
    assert "runs NO operational-analytics outbox" in configured_off[0]
    assert "could not read its own" in broken[0]
    assert configured_off != broken


def test_healthy_section_passes_c_and_d() -> None:
    payload = _payload(_healthy_section())
    assert crc.available_blockers("aws", payload) == []
    assert crc.drain_lag_blockers("aws", payload, now=NOW) == []


def test_the_string_false_does_not_read_as_available() -> None:
    """Stage (c) tested this field with Python truthiness, and `"false"` is true.

    The value comes off somebody else's HTTP response. A control plane that
    serialised the flag as text — or anything in the path that stringified the
    body — would publish `available: "false"` and be read here as AVAILABLE,
    which passes the stage whose entire job is to notice that the control plane
    could not read its own outbox. Only a JSON boolean counts now, and a
    non-boolean is its own finding rather than a silent pass.
    """
    for value in ("false", "0", "no"):
        section = {**_healthy_section(), AVAILABLE_FIELD: value}
        blockers = crc.available_blockers("aws", _payload(section))
        assert blockers, f"{value!r} read as available"
        assert "rather than the JSON boolean" in blockers[0]
        # ...and stage (d) does not then read a lag off a section it cannot trust.
        assert crc.drain_lag_blockers("aws", _payload(section), now=NOW)

    assert crc.available_blockers("aws", _payload(_healthy_section())) == []


def test_a_generated_at_in_the_future_fails_instead_of_disabling_the_check() -> None:
    """A negative age passes every "is it too old?" test there is.

    The staleness half of stage (d) exists because a frozen control plane
    republishes a healthy lag forever. A publisher whose clock or timezone is
    wrong stamps the section ahead of now, the computed age goes negative, and
    the comparison can never fire again however stale the snapshot really is —
    the check switches itself off and the run stays green.
    """
    stamped = NOW + dt.timedelta(hours=6)
    section = analytics_status_section(
        oldest_enqueued_at=stamped - dt.timedelta(seconds=5), now=stamped
    )
    blockers = crc.drain_lag_blockers("aws", _payload(section), now=NOW)
    # Non-emptiness FIRST. `blockers == [b for b in blockers if ...]` is
    # trivially true for the empty list, so the previous form passed with the
    # guard deleted -- a test green for the reason it was written to catch.
    assert blockers, "a generated_at 6h in the future produced no blocker at all"
    assert all("in the FUTURE" in blocker for blocker in blockers), blockers

    # Ordinary clock skew between two machines is not a finding.
    skew = dt.timedelta(seconds=crc.MAX_SECTION_CLOCK_SKEW_SECONDS - 1)
    skewed = analytics_status_section(
        oldest_enqueued_at=NOW + skew - dt.timedelta(seconds=5), now=NOW + skew
    )
    assert crc.drain_lag_blockers("aws", _payload(skewed), now=NOW) == []


def test_stage_d_defers_to_stage_c_rather_than_inventing_a_second_failure() -> None:
    """An unavailable section has no lag to read, and must not pretend otherwise.

    Reporting "drain_lag_seconds is missing" about a section that says
    `available: false` is the same condition twice, in words that point at the
    wrong fix.
    """
    blockers = crc.drain_lag_blockers(
        "azure", _payload(analytics_status_unavailable(REASON_NOT_CONFIGURED)), now=NOW
    )
    assert len(blockers) == 1
    assert "Stage (c) is the one to fix" in blockers[0]


def test_the_aws_eu_backlog_would_have_failed_stage_d() -> None:
    """The outage, replayed: fifteen days of undrained rows.

    2026-08-02 -> 2026-08-17 is the real interval; 1,248,668s is the lag the
    drain reported on the day it was finally installed.
    """
    section = analytics_status_section(
        oldest_enqueued_at=dt.datetime(2026, 8, 2, 4, 15, tzinfo=dt.UTC),
        now=NOW,
        outbox_depth=470_897,
    )
    blockers = crc.drain_lag_blockers("aws", _payload(section), now=NOW)
    assert blockers
    assert "oldest undelivered outbox row is" in blockers[0]
    assert "aws_eu_clickhouse_drain_install.sh" in blockers[0]


def test_stale_section_fails_even_when_the_lag_reads_healthy() -> None:
    """A frozen control plane republishes a healthy number forever."""
    section = analytics_status_section(
        oldest_enqueued_at=NOW - dt.timedelta(seconds=5),
        now=NOW - dt.timedelta(hours=9),
    )
    blockers = crc.drain_lag_blockers("aws", _payload(section), now=NOW)
    assert blockers
    assert "section is" in blockers[0] and "old" in blockers[0]


def test_lag_bound_matches_the_alarm_the_drain_itself_fires() -> None:
    """A gate more tolerant than the process it gates is decorative."""
    assert crc.DEFAULT_MAX_SECTION_AGE_SECONDS == 3_600.0
    section = analytics_status_section(
        oldest_enqueued_at=NOW - dt.timedelta(seconds=DEFAULT_MAX_DRAIN_LAG_SECONDS + 1),
        now=NOW,
    )
    assert crc.drain_lag_blockers("aws", _payload(section), now=NOW)


def test_the_limit_of_a_passing_lag_is_printed_on_every_passing_run(tmp_path: Path) -> None:
    """What green means, said on the runs that are green — all of them.

    This used to be conditional: the sentence was printed only when the section
    reported `outbox_depth == 0`. No storage backend in this repository ever
    populates that field (both build `OutboxFreshness` without it), so against a
    real cloud the condition was never true and the sentence was never printed —
    while the banner it was supposed to weaken printed every time. The
    limitation does not depend on the depth, so neither does the sentence.
    """
    payload = _payload(
        analytics_status_section(oldest_enqueued_at=None, now=NOW, outbox_depth=None)
    )
    assert crc.drain_lag_blockers("aws", payload, now=NOW) == []
    assert "does not prove rows are moving" in crc.DRAIN_LAG_LIMIT_NOTE

    # ...and it reaches the operator, on stdout, where the shell reprints it.
    # Generated at the real clock: the CLI compares the section's age to now.
    live = dt.datetime.now(dt.UTC)
    status = tmp_path / "status.json"
    status.write_text(
        json.dumps(
            _payload(analytics_status_section(oldest_enqueued_at=None, now=live))
        )
    )
    captured = _run_module("lag", "--cloud", "aws", "--status-file", str(status))
    assert captured.returncode == 0
    assert crc.DRAIN_LAG_LIMIT_NOTE.split(";")[0] in captured.stdout


# ---------------------------------------------------------------------------
# (e): the producer side, which (b)-(d) cannot see.
# ---------------------------------------------------------------------------


def test_azure_declares_the_runtime_computed_outbox() -> None:
    assert crc.declared_outbox_value("azure") == "${OUTBOX_ENABLED}"
    assert crc.outbox_enabled_blockers("azure") == []
    note = crc.outbox_note("azure")
    assert note is not None and "computed at deploy time" in note


def test_gcp_and_aws_declare_the_outbox() -> None:
    assert crc.declared_outbox_value("gcp") == "true"
    assert crc.outbox_enabled_blockers("gcp") == []
    # AWS computes it at deploy time; the stage passes and says what it did not prove.
    assert crc.declared_outbox_value("aws") == "${OUTBOX_ENABLED}"
    assert crc.outbox_enabled_blockers("aws") == []
    note = crc.outbox_note("aws")
    assert note is not None and "computed at deploy time" in note


def test_instructions_to_set_the_variable_are_not_the_variable(tmp_path: Path) -> None:
    """The check must not read its own advice back as compliance.

    Found while writing this: `azure_control_plane.sh` now ends by printing the
    exact line an operator should add — `TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED
    =true` — and a name-anywhere match counted that as having set it. The one
    cloud this stage exists to fail passed, because the fix instruction looked
    like the fix.
    """
    script_dir = tmp_path / "scripts" / "deploy"
    script_dir.mkdir(parents=True)
    (script_dir / "azure_control_plane.sh").write_text(
        'az containerapp update --set-env-vars "TR_ENVIRONMENT=canary"\n'
        "cat >&2 <<'NEXT'\n"
        "  2. add to the ENV_VARS block in this file:\n"
        "       TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED=true\n"
        "NEXT\n"
        "# and see TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED in the runbook\n"
    )
    assert crc.declared_outbox_value("azure", root=tmp_path) is None
    assert crc.outbox_enabled_blockers("azure", root=tmp_path)


def test_a_here_string_is_not_a_heredoc(tmp_path: Path) -> None:
    """The false claim in `_executable_text`, made checkable.

    Its docstring said `<<<` here-strings "are left alone". They were not: the
    opener pattern could start one character in, so `<<<WORD` matched as a
    heredoc named WORD and everything after it was swallowed until a line
    reading WORD turned up, which never happens. A here-string sitting above the
    assignment therefore hid the assignment, and the cloud that PASSES this
    stage would have failed it.
    """
    script_dir = tmp_path / "scripts" / "deploy"
    script_dir.mkdir(parents=True)
    (script_dir / "azure_control_plane.sh").write_text(
        "#!/usr/bin/env bash\n"
        "python3 -c 'import sys; print(sys.stdin.read())' <<<PAYLOAD\n"
        f"{crc.OUTBOX_ENABLED_ENV}=true\n"
    )
    assert crc.declared_outbox_value("azure", root=tmp_path) == "true"

    # ...and a real heredoc still hides its body, which is the behaviour the
    # here-string case was wrongly getting.
    (script_dir / "azure_control_plane.sh").write_text(
        "#!/usr/bin/env bash\n"
        "cat <<PAYLOAD\n"
        f"{crc.OUTBOX_ENABLED_ENV}=true\n"
        "PAYLOAD\n"
    )
    assert crc.declared_outbox_value("azure", root=tmp_path) is None


def test_the_three_declaration_shapes_are_all_recognised(tmp_path: Path) -> None:
    """App Runner JSON, a quoted array element, and an export — one per cloud."""
    script_dir = tmp_path / "scripts" / "deploy"
    script_dir.mkdir(parents=True)
    (script_dir / "aws_eu_control_plane.sh").write_text(
        '        "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED": "${OUTBOX_ENABLED}",\n'
    )
    (script_dir / "rollout.sh").write_text('  "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED=true"\n')
    (script_dir / "azure_control_plane.sh").write_text(
        "  export TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED=true\n"
    )
    assert crc.declared_outbox_value("aws", root=tmp_path) == "${OUTBOX_ENABLED}"
    assert crc.declared_outbox_value("gcp", root=tmp_path) == "true"
    assert crc.declared_outbox_value("azure", root=tmp_path) == "true"


def test_the_form_the_failure_message_prescribes_is_accepted(tmp_path: Path) -> None:
    """A gate that rejects the fix it prints is a gate people learn to route around.

    Stage (e) failing says, in as many words, `set
    TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED=true in <script>`. An operator who
    does exactly that — a plain assignment, not a JSON map, not a quoted array
    element, not an export — used to get the same failure back, which reads as
    the check being broken rather than as the cloud being incomplete.

    The heredoc case still does not count (see the test above): the difference
    is between a line of shell and a line of advice, and both contain the same
    characters.
    """
    prescription = f"{crc.OUTBOX_ENABLED_ENV}=true"
    script_dir = tmp_path / "scripts" / "deploy"
    script_dir.mkdir(parents=True)
    target = script_dir / "azure_control_plane.sh"
    target.write_text(
        "#!/usr/bin/env bash\n"
        'az containerapp update --set-env-vars "TR_ENVIRONMENT=canary"\n'
    )
    assert prescription in crc.outbox_enabled_blockers("azure", root=tmp_path)[0]

    target.write_text(
        "#!/usr/bin/env bash\n"
        f"# {prescription}   <- a comment is still not a setting\n"
        f"{prescription}\n"
        'az containerapp update --set-env-vars "TR_ENVIRONMENT=canary"\n'
    )
    assert crc.declared_outbox_value("azure", root=tmp_path) == "true"
    assert crc.outbox_enabled_blockers("azure", root=tmp_path) == []


def test_only_the_assignment_counts_not_the_comment_that_quotes_it(tmp_path: Path) -> None:
    """The commented-out line above the fix is the likeliest near-miss of all."""
    script_dir = tmp_path / "scripts" / "deploy"
    script_dir.mkdir(parents=True)
    (script_dir / "azure_control_plane.sh").write_text(
        "#!/usr/bin/env bash\n"
        f"#   {crc.OUTBOX_ENABLED_ENV}=true    # TODO once the ClickHouse exists\n"
    )
    assert crc.declared_outbox_value("azure", root=tmp_path) is None
    assert crc.outbox_enabled_blockers("azure", root=tmp_path)


def test_a_hard_disabled_outbox_fails(tmp_path: Path) -> None:
    script_dir = tmp_path / "scripts" / "deploy"
    script_dir.mkdir(parents=True)
    (script_dir / "rollout.sh").write_text(
        'ENV=(\n  "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED=false"\n)\n'
    )
    blockers = crc.outbox_enabled_blockers("gcp", root=tmp_path)
    assert blockers
    assert "switched OFF" in blockers[0]
    assert "vacuously green" in blockers[0]


def test_a_control_plane_script_that_is_not_there_says_so(tmp_path: Path) -> None:
    """A missing FILE is not "the file does not set the variable".

    `declared_outbox_value` answers None for both, and stage (e) used to print
    the same sentence either way: "<script> — the source of truth for this
    cloud's control-plane environment — never sets TR_...", about a path that
    does not exist. That sends the reader to open a file that is not there and
    teaches them the gate is confused, which is how a gate stops being read.
    """
    (tmp_path / "scripts" / "deploy").mkdir(parents=True)
    blockers = crc.outbox_enabled_blockers("azure", root=tmp_path)
    assert blockers
    assert "DOES NOT EXIST in this checkout" in blockers[0]
    assert "never sets" not in blockers[0]
    assert "control_plane_script" in blockers[0]


# ---------------------------------------------------------------------------
# The script registry: structure only. Behaviour is proven by execution in
# tests/test_deploy_script_execution.py.
# ---------------------------------------------------------------------------


def test_the_script_registry_is_well_formed() -> None:
    gaps = crc.script_binding_gaps()
    assert gaps == [], "\n".join(gaps)
    assert crc.scripts_proven_by_execution(), "nothing is proven by execution"


def test_a_new_cloud_with_no_wired_script_fails_ci_naming_the_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The BLOCKING case: a fourth cloud ships a script nothing binds.

    Registering the cloud is not enough — that only makes it checkABLE. This is
    the step that makes it checkED, and it has to fail loudly enough to be
    actionable: the message names the registry file, the field, and the line to
    put at the end of the script.
    """
    monkeypatch.setitem(
        crc.ROLLOUT_REGISTRY,
        "oracle",
        crc.CloudRollout(
            cloud="oracle",
            control_plane_script="scripts/deploy/rollout.sh",
            drain_install_command="build it",
        ),
    )
    gaps = crc.script_binding_gaps()
    joined = "\n".join(gaps)
    assert gaps
    assert "src/trusted_router/cloud_rollout_completeness.py" in joined
    assert "deploy_scripts" in joined
    assert "verify_cloud_complete.sh" in joined
    assert "oracle" in joined


def test_a_script_that_runs_the_verifier_unclaimed_fails_ci(tmp_path: Path) -> None:
    """Wiring nobody claims is wiring nobody would miss.

    A script that verifies a cloud, with no registry entry naming it, is one
    delete away from silence — and the deletion looks like a cleanup.
    """
    for cloud, entry in crc.ROLLOUT_REGISTRY.items():
        for path in (entry.control_plane_script, *(s.path for s in entry.deploy_scripts)):
            target = tmp_path / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"require_cloud_complete {cloud}\n")
    for item in crc.ROLLOUT_REGISTRY["gcp"].exempt_deploy_scripts:
        (tmp_path / item.script).write_text("# exempt\n")
    (tmp_path / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".github" / "workflows" / "deploy.yml").write_text("jobs: {}\n")
    assert crc.script_binding_gaps(root=tmp_path) == []

    (tmp_path / "scripts" / "deploy" / "oracle_bring_up.sh").write_text(
        'bash "${SCRIPT_DIR}/verify_cloud_complete.sh" oracle\n'
    )
    gaps = crc.script_binding_gaps(root=tmp_path)
    assert any("oracle_bring_up.sh" in gap and "no CloudRollout claims it" in gap for gap in gaps)


def test_a_script_in_a_subdirectory_does_not_escape_the_scan(tmp_path: Path) -> None:
    """The glob was `scripts/deploy/*.sh`, and one directory down was invisible.

    A bring-up script at `scripts/deploy/aws/bring_up.sh` could call the
    verifier with nothing claiming it — the "wiring nobody would miss" case,
    hidden by a non-recursive pattern.
    """
    for cloud, entry in crc.ROLLOUT_REGISTRY.items():
        for path in (entry.control_plane_script, *(s.path for s in entry.deploy_scripts)):
            target = tmp_path / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"require_cloud_complete {cloud}\n")
    for item in crc.ROLLOUT_REGISTRY["gcp"].exempt_deploy_scripts:
        (tmp_path / item.script).write_text("# exempt\n")
    (tmp_path / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".github" / "workflows" / "deploy.yml").write_text("jobs: {}\n")

    nested = tmp_path / "scripts" / "deploy" / "oracle" / "bring_up.sh"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text('bash "${SCRIPT_DIR}/../verify_cloud_complete.sh" oracle\n')
    gaps = crc.script_binding_gaps(root=tmp_path)
    assert any("oracle/bring_up.sh" in gap for gap in gaps), gaps


def test_an_unbound_script_must_carry_a_named_reason_and_a_real_control() -> None:
    """GCP is exempt, and the exemption is a sentence somebody signed.

    `rollout.sh` runs inside the deploy workflow, so ending IT in a public fetch
    of the page that same cloud serves would abort the deploy that repairs an
    outage, partway through. That is a real reason.

    What round 2 caught is the other half: the exemption cited "the scheduled
    analytics freshness workflow" as the compensating control, and that workflow
    ships with no `schedule:` trigger by design — so the primary cloud had no
    automated completeness check at all, behind a sentence saying it did. A
    claimed control is now a structured reference, and this resolves it.

    What round 5 caught is the half after that, and it is the reason this test
    reads the way it does. The resolution used to be a SUBSTRING: concatenate
    the job's `run:` blocks and look for "verify_cloud_complete.sh gcp".
    Replacing the whole job body with

        echo "Next: bash scripts/deploy/verify_cloud_complete.sh gcp"
        exit 0

    satisfied it, and so did a commented-out line and a `|| true` — the three
    saboteur shapes this change exists to kill, all of them passing the only
    binding that covered the primary cloud. So: the job must contain a step
    whose `run` IS an invocation of a script that this cloud's registry entry
    proves BY EXECUTION. An echo of the command is not an invocation, and the
    script on the other end is in the behavioural harness.
    """
    exemptions = [
        (cloud, item)
        for cloud, entry in crc.ROLLOUT_REGISTRY.items()
        for item in entry.exempt_deploy_scripts
    ]
    assert exemptions, "if nothing is exempt, this test is stale — delete it"
    for cloud, item in exemptions:
        assert (ROOT / item.script).is_file(), f"{cloud}: {item.script} does not exist"
        assert item.reason.strip(), f"{cloud}: {item.script} exemption has no reason"

        control = item.compensating_control
        if control is None:
            continue
        workflow = yaml.safe_load((ROOT / control.workflow).read_text())
        assert control.job in workflow["jobs"], (
            f"{cloud}: {item.script} cites {control.workflow} job {control.job!r}, which "
            "does not exist in that workflow"
        )
        job = workflow["jobs"][control.job]
        # Only scripts THIS cloud proves by execution count. A control pointing
        # at an unproven script is a control whose behaviour nothing checks.
        proven = {
            script
            for script, script_cloud in crc.scripts_proven_by_execution()
            if script_cloud == cloud
        }
        assert proven, f"{cloud}: nothing is proven by execution, so no control can cite it"
        invocations = {f"bash {script}" for script in proven}
        # The invocation must be the LAST executable line of some step, not
        # merely present in one. Presence alone still accepted two of the three
        # saboteur shapes: the line followed by `exit 0`, and the line sealed
        # inside `if false; then ... fi` (whose last line is `fi`). A workflow
        # cannot be executed from a test, so this checks the SHAPE of the
        # declaration -- said plainly in the docs' limits list.
        run_lines = set()
        for step in job.get("steps", []):
            executable = [
                line.strip()
                for line in str(step.get("run", "")).splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            if executable:
                run_lines.add(executable[-1])
        assert run_lines & invocations, (
            f"{cloud}: {control.workflow} job {control.job!r} has no step that RUNS a "
            f"script proven by execution for this cloud. Expected one of {sorted(invocations)} "
            f"as a step's whole `run` line; found {sorted(line for line in run_lines if line)}. "
            "A printed instruction, a commented-out call, a `|| true`, a trailing "
            "`exit 0` and a call sealed in `if false` all used to pass here, which is "
            "the defect this shape closes."
        )
        # The prose in multi-cloud-separation.md and the runbook says this check
        # reads the job's `needs` and its `if:`. It did not, until now.
        assert "deploy" in str(job.get("needs", "")), (
            f"{cloud}: {control.workflow} job {control.job!r} does not depend on `deploy`, "
            "so it can run before the thing it is supposed to check"
        )
        assert "always()" in str(job.get("if", "")), (
            f"{cloud}: {control.workflow} job {control.job!r} has no `if: always()`, so it "
            "is skipped exactly when the deploy it follows failed"
        )

    gcp = crc.ROLLOUT_REGISTRY["gcp"]
    assert [item.script for item in gcp.exempt_deploy_scripts] == ["scripts/deploy/rollout.sh"]
    assert gcp.exempt_deploy_scripts[0].compensating_control is not None
    # ...and the exempt file is not the cloud: GCP is bound like everyone else.
    assert [script.path for script in gcp.deploy_scripts] == [
        "scripts/deploy/verify_gcp_complete.sh"
    ]
    assert all(script.proof == crc.PROVEN_BY_EXECUTION for script in gcp.deploy_scripts)


def test_an_exemption_citing_a_workflow_that_is_not_there_fails_ci(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The generalisation of the GCP finding: a phantom control is a defect."""
    monkeypatch.setitem(
        crc.ROLLOUT_REGISTRY,
        "gcp",
        crc.CloudRollout(
            cloud="gcp",
            control_plane_script="scripts/deploy/rollout.sh",
            drain_install_command="build it",
            exempt_deploy_scripts=(
                crc.ScriptExemption(
                    script="scripts/deploy/rollout.sh",
                    reason="x" * 100,
                    compensating_control=crc.CompensatingControl(
                        workflow=".github/workflows/no-such-workflow.yml",
                        job="imaginary",
                        description="a control that does not exist",
                    ),
                ),
            ),
        ),
    )
    (tmp_path / "scripts" / "deploy").mkdir(parents=True)
    (tmp_path / "scripts" / "deploy" / "rollout.sh").write_text("# exempt\n")
    gaps = crc.script_binding_gaps(root=tmp_path)
    assert any("no-such-workflow.yml" in gap and "does not exist" in gap for gap in gaps), gaps


def test_a_not_proven_script_must_say_why(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """"The harness cannot run this one" is allowed. Silence is not."""
    monkeypatch.setitem(
        crc.ROLLOUT_REGISTRY,
        "azure",
        crc.CloudRollout(
            cloud="azure",
            control_plane_script="scripts/deploy/azure_control_plane.sh",
            drain_install_command="build it",
            deploy_scripts=(
                crc.DeployScript("scripts/deploy/azure_control_plane.sh", crc.NOT_PROVEN),
            ),
        ),
    )
    (tmp_path / "scripts" / "deploy").mkdir(parents=True)
    (tmp_path / "scripts" / "deploy" / "azure_control_plane.sh").write_text("# nothing\n")
    gaps = crc.script_binding_gaps(root=tmp_path)
    assert any("NOT_PROVEN with no reason" in gap for gap in gaps), gaps


# ---------------------------------------------------------------------------
# The shell entry point.
# ---------------------------------------------------------------------------


def test_verifier_refuses_an_unknown_cloud_without_touching_the_network() -> None:
    result = subprocess.run(  # noqa: S603,S607 - fixed argv, no shell, repo-local script
        ["bash", str(VERIFIER), "oracle"],  # noqa: S607
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 1
    assert "NOT VERIFIED" in result.stderr
    assert "no ROLLOUT_REGISTRY entry" in result.stderr


def test_verifier_is_read_only() -> None:
    """No cloud CLI, no writes. It has to be runnable by anyone, from a laptop.

    The first version of this test looked for `"\\naws "` — a cloud CLI call at
    column zero and nowhere else — so an indented `  aws dsql ...` inside any
    `if` passed it, which is where such a call would actually be. It now scans
    every executable line for the CLI as a COMMAND word, comments excluded.
    """
    text = VERIFIER.read_text()
    assert "set -euo pipefail" in text

    forbidden = re.compile(r"(?:^|[;&|(]|\bthen\b|\bdo\b|\$\()\s*(aws|az|gcloud|gc)\s+\S")
    executable = []
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        executable.append(line)
        match = forbidden.search(line)
        assert match is None, (
            f"verifier line {number} shells out to {match.group(1) if match else ''}: {line!r}"
        )

    # Counted over CODE, not over the file: the header explains what the one
    # curl and the one mktemp are for, and prose that describes a rule is not
    # an instance of breaking it.
    code = "\n".join(executable)
    assert code.count("curl ") == 1
    assert '-o "$BODY"' in code
    # And nothing writes anywhere but the one temp file it cleans up.
    assert code.count("mktemp") == 1
    assert len(re.findall(r">\s*\"?\$\{?REPO_ROOT", code)) == 0


# ---------------------------------------------------------------------------
# The gate cannot be turned off from the environment, or from anywhere else.
#
# It could, for one commit: the bound came from TR_MAX_DRAIN_LAG_SECONDS and the
# URL from TR_STATUS_URL, both `${VAR:-default}` at the top of the script. Every
# wired deploy script inherits its caller's environment, so a single `export` in
# a shell profile turned 470,897 undelivered rows into a green verdict. The
# replacement was a pair of `--max-lag-seconds` / `--status-url` FLAGS plus a
# fourth outcome ("this run is only a diagnosis") to keep them safe; both are
# gone too, because an outcome that exists to make an override safe is one more
# thing that can be got wrong, and this file has had two rounds of exactly that.
#
# These tests run the REAL shell script, with a fake `curl` on PATH so no test
# touches the network, and a stub of the Python module that records the argv the
# shell passed it. Recorded argv is the strong form of the assertion: not "the
# output looked right" but "the shell never asked for the operator's number".
# ---------------------------------------------------------------------------

_STUB_MODULE = '''
"""Stand-in for the judgement module: replays a scripted plan, records argv.

Speaks the contract the real module speaks, which is now just the exit status
plus plain lines: stderr is the operator's explanation, stdout is notes for the
shell to reprint. STUB_STDERR_NOISE and STUB_STDOUT_NOISE exist so a test can
fire the interpreter-warning shape that used to be able to rewrite a verdict.
"""
import json, os, sys

with open(os.environ["STUB_ARGV_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")

plan = json.loads(os.environ["STUB_PLAN"])
code, lines = plan.get(sys.argv[1], [0, []])
noise = os.environ.get("STUB_STDERR_NOISE")
if noise:
    print(noise, file=sys.stderr)
extra = os.environ.get("STUB_STDOUT_NOISE")
if extra:
    print(extra)
for line in lines:
    print(line)
sys.exit(code)
'''

_FAKE_CURL = """#!/usr/bin/env bash
# Records the URL it was asked for; serves a fixture; never leaves the machine.
out=""
url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -w|--max-time) shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
printf '%s\\n' "$url" >> "$CURL_URL_LOG"
[ -z "$out" ] || cat "$CURL_BODY" > "$out"
printf '%s' "${CURL_HTTP_CODE:-200}"
"""


class _Harness:
    """A throwaway checkout: the REAL verifier, a stub module, a fake curl.

    The shell is the thing under test, so it is copied verbatim; everything it
    talks to is replaced by something that records what it was asked. The
    ``.venv/bin/python`` symlink is what keeps the run off the network — it is
    the interpreter the verifier prefers, so no `uv` resolution happens. Like
    the deploy-script harness, the isolation here is by NAME (`curl` is a stub
    on PATH), not a sandbox.
    """

    def __init__(self, root: Path, plan: dict[str, Any], body: dict[str, Any]) -> None:
        self.root = root
        self.plan = plan
        self.verifier = root / "scripts" / "deploy" / "verify_cloud_complete.sh"
        self.curl_log = root / "curl-urls.txt"
        self.argv_log = root / "stub-argv.txt"
        self.body = root / "status.json"
        self.curl_dir = root / "bin"

        self.verifier.parent.mkdir(parents=True)
        self.verifier.write_text(VERIFIER.read_text())
        package = root / "src" / "trusted_router"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "cloud_rollout_completeness.py").write_text(_STUB_MODULE)
        venv_bin = root / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").symlink_to(sys.executable)
        self.curl_dir.mkdir()
        (self.curl_dir / "curl").write_text(_FAKE_CURL)
        (self.curl_dir / "curl").chmod(0o755)
        self.body.write_text(json.dumps(body))

    @property
    def fetched_urls(self) -> list[str]:
        """Every URL that actually went to `curl`, in order."""
        return self.curl_log.read_text().split() if self.curl_log.exists() else []

    def argv_for(self, command: str) -> list[str]:
        """The argv the shell passed to a subcommand — what it ASKED, not what it printed."""
        for line in self.argv_log.read_text().splitlines():
            argv: list[str] = json.loads(line)
            if argv and argv[0] == command:
                return argv
        raise AssertionError(f"the shell never ran the {command!r} subcommand")

    def run(
        self, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment.update(
            {
                "PATH": f"{self.curl_dir}{os.pathsep}{os.environ['PATH']}",
                "STUB_PLAN": json.dumps(self.plan),
                "STUB_ARGV_LOG": str(self.argv_log),
                "CURL_URL_LOG": str(self.curl_log),
                "CURL_BODY": str(self.body),
            }
        )
        environment.update(env or {})
        return subprocess.run(  # noqa: S603
            ["bash", str(self.verifier), *args],  # noqa: S607
            capture_output=True,
            text=True,
            env=environment,
            cwd=self.root,
        )


def _harness(root: Path, *, plan: dict[str, Any], body: dict[str, Any] | None = None) -> _Harness:
    root.mkdir(parents=True, exist_ok=True)
    return _Harness(root, plan, body if body is not None else {"data": {}})


REGISTRY_URL = "https://registry.example/status.json"

#: ``subcommand -> [exit status, stdout lines]``. The exit status is the entire
#: verdict; the lines are notes the shell reprints under the outcome.
_ALL_PASS: dict[str, list[Any]] = {
    "registry": [0, []],
    "status-url": [0, [REGISTRY_URL]],
    "section": [0, []],
    "available": [0, []],
    "lag": [0, ["a lag under the bound proves nothing is STUCK"]],
    "outbox": [0, ["TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED='true' in the working tree"]],
}


def test_exported_status_url_cannot_redirect_the_check(tmp_path: Path) -> None:
    """(i) TR_STATUS_URL, invoked exactly as a deploy script invokes this.

    The fake curl records what was actually fetched, so this is not a test of
    what the script printed: with the variable exported to a page that would
    answer perfectly, the URL that goes over the wire is still the registry's.
    """
    harness = _harness(tmp_path, plan=_ALL_PASS)
    result = harness.run("aws", env={"TR_STATUS_URL": "https://attacker.example/status.json"})

    assert harness.fetched_urls == [REGISTRY_URL]
    assert "attacker.example" not in "".join(harness.fetched_urls)
    assert "IGNORED: TR_STATUS_URL" in result.stderr
    assert result.returncode == 0


def test_exported_lag_bound_cannot_widen_the_gate(tmp_path: Path) -> None:
    """(i) TR_MAX_DRAIN_LAG_SECONDS, asserted on the argv the shell passed.

    The old script read the variable into `MAX_LAG_SECONDS` and passed it
    through on every run, so `export TR_MAX_DRAIN_LAG_SECONDS=99999999` made the
    fifteen-day backlog pass stage (d). Now the shell asks for no bound at all —
    there is no flag to ask with — and the number comes from the Python module.
    """
    harness = _harness(tmp_path, plan=_ALL_PASS)
    result = harness.run("aws", env={"TR_MAX_DRAIN_LAG_SECONDS": "99999999"})

    lag_argv = harness.argv_for("lag")
    assert "--max-lag-seconds" not in lag_argv, lag_argv
    assert "99999999" not in " ".join(lag_argv)
    assert "IGNORED: TR_MAX_DRAIN_LAG_SECONDS" in result.stderr
    assert result.returncode == 0


def test_the_env_is_ignored_by_the_real_verifier_too() -> None:
    """The same, against the real module: no fixture, no network, no verdict change.

    `oracle` fails stage (a) before anything is fetched, so this exercises the
    real script and the real registry — and the exported variables change
    neither the outcome nor the fact that they are called out as ignored.
    """
    result = subprocess.run(  # noqa: S603
        ["bash", str(VERIFIER), "oracle"],  # noqa: S607
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={
            **os.environ,
            "TR_STATUS_URL": "https://attacker.example/status.json",
            "TR_MAX_DRAIN_LAG_SECONDS": "99999999",
        },
    )
    assert result.returncode == 1
    assert "NOT VERIFIED" in result.stderr
    assert "IGNORED: TR_STATUS_URL" in result.stderr
    assert "IGNORED: TR_MAX_DRAIN_LAG_SECONDS" in result.stderr
    assert not _verified(result.stderr)


def test_there_is_no_override_flag_left_to_pass(tmp_path: Path) -> None:
    """The flags are gone, and gone means REJECTED rather than quietly accepted.

    `--max-lag-seconds` and `--status-url` were the sanctioned way to ask the
    gate a different question, and they needed a whole fourth outcome
    (DIAGNOSTIC, exit 4) to keep them from minting a green verdict. Deleting
    them deletes that outcome; this pins that an unknown flag is a failure
    rather than something the script quietly treats as a cloud id.
    """
    harness = _harness(tmp_path, plan=_ALL_PASS)
    for flag in ("--max-lag-seconds", "--status-url"):
        result = harness.run(flag, "99999999", "aws")
        assert result.returncode == 1
        assert "unknown option" in result.stderr
        assert not _verified(result.stderr)
        assert harness.fetched_urls == []


# ---------------------------------------------------------------------------
# The outcome says what was measured, and nothing more.
# ---------------------------------------------------------------------------


def test_a_passing_run_says_verified_and_says_what_it_did_not_show(tmp_path: Path) -> None:
    """The one green sentence, and the limit printed next to it every time.

    The banner used to read "COMPLETE: <cloud> publishes a live analytics
    pipeline", downgraded to "COMPLETE WITH CAVEATS" when a stage came back
    flagged. The only flag that could have downgraded it was raised off a field
    (`outbox_depth`) that no storage backend in this repository populates — so
    the strong sentence was what every passing run printed, including over an
    outbox nothing had ever been enqueued into. There is one outcome now and it
    is true of every run that reaches it.
    """
    harness = _harness(tmp_path, plan=_ALL_PASS)
    result = harness.run("aws")

    assert result.returncode == 0
    assert _verified(result.stderr) and "aws passed every stage" in result.stderr
    assert "publishes a live analytics pipeline" not in result.stderr
    assert "It does not mean rows were seen moving" in result.stderr


def test_stage_notes_are_reprinted_verbatim_under_the_outcome(tmp_path: Path) -> None:
    """Notes are text, not a taxonomy. They inform; they never re-rank a run."""
    note = "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED is computed at deploy time"
    harness = _harness(tmp_path, plan={**_ALL_PASS, "outbox": [0, [note]]})
    result = harness.run("aws")

    assert result.returncode == 0
    assert note in result.stderr
    assert "Notes from the stages, verbatim:" in result.stderr


def test_an_unpublished_analytics_section_is_its_own_exit_code(tmp_path: Path) -> None:
    """Stage (b) failing means "nobody can see this cloud", not "you broke it".

    Every bound script depends on the distinction through
    scripts/deploy/cloud_complete_gate.sh: the run that INSTALLS a drain hits
    this state by construction today, and reporting it as a flat failure is what
    teaches operators to ignore exit codes. It is the ONE code kept beside 0 and
    1, for that reason.
    """
    harness = _harness(tmp_path, plan={**_ALL_PASS, "section": [5, []]})
    result = harness.run("aws")

    assert result.returncode == 5
    assert "NOT YET OBSERVABLE" in result.stderr
    assert not _verified(result.stderr)


def test_a_failing_stage_ends_the_run_non_zero(tmp_path: Path) -> None:
    """Everything that is not stage (b)'s absence is one code, and it says why."""
    harness = _harness(tmp_path, plan={**_ALL_PASS, "available": [1, []]})
    result = harness.run("aws")

    assert result.returncode == 1
    assert "NOT VERIFIED" in result.stderr
    assert "c: analytics available" in result.stderr
    assert not _verified(result.stderr)


def test_noise_on_either_stream_cannot_change_an_outcome(tmp_path: Path) -> None:
    """The bug the old verdict contract existed to survive, now impossible.

    The shell used to capture the module with `2>&1` and classify by the first
    word; a DeprecationWarning at import time could therefore turn one outcome
    into another, and the fall-through was the green banner. The successor
    contract read a tab-separated sentinel out of stdout, which had its own ways
    to go wrong. Nothing is read out of a stream now: a failing stage that also
    prints noise still fails, and a passing run's stray stdout line is a note.
    """
    noise = {
        "STUB_STDERR_NOISE": "DeprecationWarning: pkg_resources is deprecated as an API",
        "STUB_STDOUT_NOISE": "warning: a library printed this to stdout",
    }
    failing = _harness(tmp_path, plan={**_ALL_PASS, "outbox": [1, []]})
    result = failing.run("azure", env=noise)
    assert result.returncode == 1
    assert "NOT VERIFIED" in result.stderr
    assert not _verified(result.stderr)

    passing = _harness(tmp_path / "second", plan=_ALL_PASS)
    result = passing.run("aws", env=noise)
    assert result.returncode == 0
    assert _verified(result.stderr) and "aws passed every stage" in result.stderr
    assert "a library printed this to stdout" in result.stderr


def test_a_status_url_that_is_not_a_url_stops_the_run(tmp_path: Path) -> None:
    """The one stdout line the shell CONSUMES, and its guard.

    Stage (a) answers with a URL, which is the only place the shell takes a
    value rather than an exit status. A stray line there can only make the run
    fail — never pass, and never fetch something that is not an https:// page.
    """
    harness = _harness(tmp_path, plan={**_ALL_PASS, "status-url": [0, ["not-a-url"]]})
    result = harness.run("aws")

    assert result.returncode == 1
    assert "not an https:// URL" in result.stderr
    assert harness.fetched_urls == []


def test_every_gcp_surface_that_serves_status_builds_the_outbox() -> None:
    """The surface that answers /status.json must be able to SEE the outbox.

    When the T1 public website became its own service (#742) it took over
    trustedrouter.com — including /status.json — with an env list that never
    set TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED. Its store was built with no
    outbox object, the page published `analytics.reason=not_configured`, and
    stage (c) of verify-cloud-complete failed on every subsequent deploy
    while the actual pipeline (enqueued by the API service) drained green.

    The rollout script for any surface that serves the public status page
    must therefore carry the flag. The API service's rollout.sh already
    does; this pins the public one.
    """
    public = (ROOT / "scripts/deploy/public_surface.sh").read_text(encoding="utf-8")
    assert '"TR_SERVICE_SURFACE=public"' in public
    assert '"TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED=true"' in public

    api = (ROOT / "scripts/deploy/rollout.sh").read_text(encoding="utf-8")
    assert "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED" in api
