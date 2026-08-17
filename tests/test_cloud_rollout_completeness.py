"""A cloud cannot be added without becoming checkable.

The AWS-EU cloud served production traffic for fifteen days with no analytics
pipeline: no drain, 470,897 rows stuck in the DSQL outbox, `activity_generations`
empty, and total silence, because the only backlog alarm is emitted BY the drain
that was missing. The bring-up script had ended by PRINTING next steps and
exiting 0.

Two things are pinned here.

1. **The binding.** `declared_clouds()` reads the deployment-declaring tables —
   `byok_v1_attestations.clouds_that_must_attest()` and
   `regions.MULTICLOUD_REGION_GEO` — so a cloud added to either one is a cloud
   the completeness check immediately expects to know about, and
   `registry_gaps()` fails until it does. `test_a_new_cloud_fails_ci_until_it_is_checkable`
   adds a fake cloud and asserts the failure, which is the whole mechanism: you
   cannot get a cloud into this codebase quietly.

   (The fleet registry module from the in-flight PR that publishes
   `drain_lag_seconds` in `/status.json` does not exist on `main` yet, so the
   binding reads the tables above plus `Settings.synthetic_fleet_peers`. When
   that module lands, `freshness_registry()` prefers it and this test is
   unchanged.)

2. **The stages.** Each one fails for the reason it exists, with a message
   naming the fix — and in particular stage (e) is not redundant with stage (d),
   because a drained outbox and a disabled outbox publish the same
   `drain_lag_seconds: 0.0`.
"""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path
from typing import Any

import pytest

from trusted_router import cloud_rollout_completeness as crc
from trusted_router import regions
from trusted_router.operational_analytics_freshness import (
    ANALYTICS_STATUS_KEY,
    DEFAULT_MAX_DRAIN_LAG_SECONDS,
    analytics_status_section,
    analytics_status_unavailable,
)

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts" / "deploy" / "verify_cloud_complete.sh"

NOW = dt.datetime(2026, 8, 17, 12, 0, tzinfo=dt.UTC)


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
    {aws, azure, gcp} is a fourth copy of the fleet, and the copies are what
    drift. The assertion is the relation: whatever the deployment tables
    declare, `verify_cloud_complete.sh` can check it.
    """
    gaps = crc.registry_gaps()
    assert gaps == [], "\n".join(gaps)
    for cloud in crc.declared_clouds():
        assert crc.status_url_for(cloud).startswith("https://")
        assert cloud in crc.ROLLOUT_REGISTRY


def test_declared_clouds_reads_both_deployment_tables() -> None:
    """Neither table alone can weaken the requirement by omission.

    The union is the safe direction: a cloud named in only one place is still a
    cloud that must be finishable.
    """
    from trusted_router.byok_v1_attestations import clouds_that_must_attest

    declared = set(crc.declared_clouds())
    assert set(clouds_that_must_attest()) <= declared
    assert {geo.cloud for geo in regions.MULTICLOUD_REGION_GEO.values()} <= declared


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
    assert "Settings.synthetic_fleet_peers" in joined
    assert "ROLLOUT_REGISTRY" in joined
    assert "verify_cloud_complete.sh" in joined


def test_registry_gap_survives_a_half_finished_addition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wiring the status URL but not the rollout entry is still a gap.

    The likelier real mistake: someone adds the peer so the fleet page looks
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


def test_unavailable_is_not_the_same_as_empty() -> None:
    """ "I could not look" must never collapse into "there was nothing to see"."""
    blockers = crc.available_blockers("aws", _payload(analytics_status_unavailable()))
    assert blockers
    assert "available is false" in blockers[0]
    # Names the actual command, from the registry.
    assert "aws_eu_clickhouse_drain_install.sh" in blockers[0]


def test_healthy_section_passes_c_and_d() -> None:
    payload = _payload(_healthy_section())
    assert crc.available_blockers("aws", payload) == []
    assert crc.drain_lag_blockers("aws", payload, now=NOW) == []


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


def test_empty_outbox_passes_but_says_so() -> None:
    """The caveat that keeps green from meaning more than it measured."""
    payload = _payload(analytics_status_section(oldest_enqueued_at=None, now=NOW))
    assert crc.drain_lag_blockers("aws", payload, now=NOW) == []
    caveat = crc.drain_lag_caveat(payload)
    assert caveat is not None
    assert "not that anything is moving" in caveat


# ---------------------------------------------------------------------------
# (e): the producer side, which (b)-(d) cannot see.
# ---------------------------------------------------------------------------


def test_azure_fails_because_it_has_no_outbox_at_all() -> None:
    """Pins the state found on 2026-08-17, and the reason it is not benign.

    Azure's control-plane deploy script sets no outbox variable, so settle
    enqueues nothing. Every published freshness signal for such a cloud is
    vacuously green. This test flips to the other branch when the outbox is
    added, which is the point at which it should.
    """
    assert crc.declared_outbox_value("azure") is None
    blockers = crc.outbox_enabled_blockers("azure")
    assert blockers
    assert "never sets TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED" in blockers[0]
    assert "looks perfectly healthy" in blockers[0]


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


# ---------------------------------------------------------------------------
# The exemption, and its narrowness.
# ---------------------------------------------------------------------------


def test_no_cloud_is_currently_exempt() -> None:
    """Silence is not an exemption. Nobody has signed for one."""
    assert [c for c in crc.ROLLOUT_REGISTRY if crc.exemption(c)] == []


def test_an_exemption_waives_but_still_prints_what_it_waives() -> None:
    blockers = crc.outbox_enabled_blockers("azure")
    waived, note = crc.apply_exemption("azure", blockers)
    assert waived == blockers and note is None  # no reason recorded -> no waiver

    patched = crc.CloudRollout(
        cloud="azure",
        control_plane_script="scripts/deploy/azure_control_plane.sh",
        drain_install_command="build it",
        analytics_absent_reason="canary only, tracked in #644",
    )
    original = crc.ROLLOUT_REGISTRY["azure"]
    crc.ROLLOUT_REGISTRY["azure"] = patched
    try:
        waived, note = crc.apply_exemption("azure", blockers)
        assert waived == []
        assert note is not None
        assert "ACCEPTED-ABSENT" in note
        assert "canary only, tracked in #644" in note
        # The suppressed blocker is still in the output: an exemption may not
        # hide what it exempts.
        assert "never sets TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED" in note
    finally:
        crc.ROLLOUT_REGISTRY["azure"] = original


# ---------------------------------------------------------------------------
# The shell entry point, and the scripts that must end in it.
# ---------------------------------------------------------------------------


def test_verifier_refuses_an_unknown_cloud_without_touching_the_network() -> None:
    result = subprocess.run(  # noqa: S603,S607 - fixed argv, no shell, repo-local script
        ["bash", str(VERIFIER), "oracle"],  # noqa: S607
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 1
    assert "INCOMPLETE ROLLOUT" in result.stderr
    assert "no ROLLOUT_REGISTRY entry" in result.stderr


def test_verifier_is_read_only() -> None:
    """No cloud CLI, no writes. It has to be runnable by anyone, from a laptop."""
    text = VERIFIER.read_text()
    assert "set -euo pipefail" in text
    for forbidden in ("aws ", "az ", "gcloud ", "gc "):
        assert f"\n{forbidden}" not in text, f"verifier must not shell out to {forbidden.strip()}"
    # Exactly one network call, and it is a GET.
    assert text.count("curl ") == 1
    assert '-o "$BODY"' in text


@pytest.mark.parametrize(
    ("script", "cloud"),
    [
        ("aws_eu_clickhouse.sh", "aws"),
        ("aws_eu_north_clickhouse.sh", "aws"),
        ("aws_eu_control_plane.sh", "aws"),
        ("aws_eu_clickhouse_drain_install.sh", "aws"),
        ("azure_control_plane.sh", "azure"),
    ],
)
def test_bring_up_scripts_end_in_the_completeness_check(script: str, cloud: str) -> None:
    """ "The script finished" and "the cloud works" must be the same claim.

    Each of these previously ended by printing next steps and exiting 0. The
    assertion is not that they print less — the instructions are useful — but
    that the last thing they do is a check whose failure is an exit code.
    """
    text = (ROOT / "scripts" / "deploy" / script).read_text()
    assert f'verify_cloud_complete.sh" {cloud}' in text, (
        f"{script} must end by verifying the {cloud} cloud"
    )
    tail = text.rstrip().splitlines()[-40:]
    assert any("verify_cloud_complete.sh" in line for line in tail), (
        f"{script} runs the check but not at the end"
    )


def test_no_bring_up_script_prints_next_steps_and_returns_zero() -> None:
    """The regression guard for the actual outage mechanism.

    `aws_eu_clickhouse.sh` ended with `echo "Next: ..."` and exited 0 on
    2026-08-02. A script whose final word is an instruction has to make that
    instruction load-bearing — an explicit non-zero exit — or the instruction is
    advice, and advice was what failed.
    """
    for script, cloud in (
        ("aws_eu_clickhouse.sh", "aws"),
        ("aws_eu_north_clickhouse.sh", "aws"),
        ("aws_eu_control_plane.sh", "aws"),
        ("azure_control_plane.sh", "azure"),
    ):
        text = (ROOT / "scripts" / "deploy" / script).read_text()
        assert "exit 1" in text or "exit 3" in text, f"{script} has no failing exit path"
        assert f'verify_cloud_complete.sh" {cloud}' in text
