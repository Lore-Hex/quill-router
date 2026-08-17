"""A cloud cannot be added without becoming checkable.

The AWS-EU cloud served production traffic for fifteen days with no analytics
pipeline: no drain, 470,897 rows stuck in the DSQL outbox, `activity_generations`
empty, and total silence, because the only backlog alarm is emitted BY the drain
that was missing. The bring-up script had ended by PRINTING next steps and
exiting 0.

Four things are pinned here.

1. **The cloud binding.** `declared_clouds()` reads the deployment-declaring
   tables — `byok_v1_attestations.clouds_that_must_attest()`,
   `regions.MULTICLOUD_REGION_GEO` and the fleet registry — so a cloud added to
   any one of them is a cloud the completeness check immediately expects to know
   about, and `registry_gaps()` fails until it does.
   `test_a_new_cloud_fails_ci_until_it_is_checkable` adds a fake cloud and
   asserts the failure, which is the whole mechanism: you cannot get a cloud
   into this codebase quietly.

   (The fleet registry module from the in-flight PR that publishes
   `drain_lag_seconds` in `/status.json` does not exist on `main` yet, so the
   binding reads the tables above plus `Settings.synthetic_fleet_peers`. When
   that module lands, `freshness_registry()` prefers it and this test is
   unchanged.)

2. **The script binding.** Which deploy scripts must END in the verifier is read
   from `CloudRollout.deploy_scripts`, not from a list in this file. It was such
   a list for one commit — five hand-written (script, cloud) rows, i.e. a fourth
   copy of the fleet in the suite whose subject is that copies drift — and a
   fourth cloud could ship a script that printed "Next: ..." and exited 0 with
   CI green. An unbound script now fails here, and the message names the file.

3. **The gate takes no input from the environment.** Deploy scripts inherit
   their caller's environment, so an env-tunable bound or status URL is a remote
   control for the gate. The tests below run the real shell with those variables
   exported and assert on what it FETCHED and what argv it PASSED, not on what
   it printed.

4. **The stages, and what the banner may claim.** Each stage fails for the
   reason it exists, with a message naming the fix — in particular stage (e) is
   not redundant with stage (d), because a drained outbox and a disabled outbox
   publish the same `drain_lag_seconds: 0.0` — and a stage that was exempted or
   caveated may not end in the flat green banner.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
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
    assert prescription in crc.outbox_enabled_blockers("azure")[0]

    script_dir = tmp_path / "scripts" / "deploy"
    script_dir.mkdir(parents=True)
    (script_dir / "azure_control_plane.sh").write_text(
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
    """No cloud CLI, no writes. It has to be runnable by anyone, from a laptop.

    The first version of this test looked for `"\\naws "` — a cloud CLI call at
    column zero and nowhere else — so an indented `  aws dsql ...` inside any
    `if` passed it, which is where such a call would actually be. It now scans
    every executable line for the CLI as a COMMAND word, comments excluded.
    """
    text = VERIFIER.read_text()
    assert "set -euo pipefail" in text

    forbidden = re.compile(r"(?:^|[;&|(]|\bthen\b|\bdo\b|\$\()\s*(aws|az|gcloud|gc)\s+\S")
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        match = forbidden.search(line)
        assert match is None, (
            f"verifier line {number} shells out to {match.group(1) if match else ''}: {line!r}"
        )

    # Exactly one network call, and it is a GET.
    assert text.count("curl ") == 1
    assert '-o "$BODY"' in text
    # And nothing writes anywhere but the one temp file it cleans up.
    assert text.count("mktemp") == 1
    assert len(re.findall(r">\s*\"?\$\{?REPO_ROOT", text)) == 0


# ---------------------------------------------------------------------------
# The gate cannot be turned off from the environment.
#
# It could, for one commit: the bound came from TR_MAX_DRAIN_LAG_SECONDS and the
# URL from TR_STATUS_URL, both `${VAR:-default}` at the top of the script. Every
# wired deploy script inherits its caller's environment, so a single `export` in
# a shell profile turned 470,897 undelivered rows into
# "COMPLETE: aws publishes a live analytics pipeline" — while the comment added
# to azure_control_plane.sh in the same commit told the reader the gate was "not
# suppressible from the environment".
#
# These tests run the REAL shell script, with a fake `curl` on PATH so no test
# touches the network, and a stub of the Python module that records the argv the
# shell passed it. Recorded argv is the strong form of the assertion: not "the
# output looked right" but "the shell never asked for the operator's number".
# ---------------------------------------------------------------------------

_STUB_MODULE = '''
"""Stand-in for the judgement module: replays a scripted plan, records argv."""
import json, os, sys

with open(os.environ["STUB_ARGV_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")

plan = json.loads(os.environ["STUB_PLAN"])
code, out = plan.get(sys.argv[1], [0, ""])
if out:
    print(out)
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
    talks to is replaced by something that records what it was asked. A
    ``.venv/bin/python`` symlink is what makes the run hermetic — it is the
    interpreter the verifier prefers, so no `uv` resolution and no network.
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

    def run(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
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


def _harness(
    root: Path, *, plan: dict[str, Any], body: dict[str, Any] | None = None
) -> _Harness:
    root.mkdir(parents=True, exist_ok=True)
    return _Harness(root, plan, body if body is not None else {"data": {}})


REGISTRY_URL = "https://registry.example/status.json"

_ALL_PASS = {
    "registry": [0, ""],
    "status-url": [0, REGISTRY_URL],
    "section": [0, ""],
    "available": [0, ""],
    "lag": [0, ""],
    "outbox": [0, "fact: TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED='true' in the working tree"],
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
    fifteen-day backlog pass stage (d). Now the shell asks for no bound at all
    unless someone typed the flag, and the number comes from the Python module.
    """
    harness = _harness(tmp_path, plan=_ALL_PASS)
    result = harness.run("aws", env={"TR_MAX_DRAIN_LAG_SECONDS": "99999999"})

    lag_argv = harness.argv_for("lag")
    assert "--max-lag-seconds" not in lag_argv, lag_argv
    assert "99999999" not in " ".join(lag_argv)
    assert "IGNORED: TR_MAX_DRAIN_LAG_SECONDS" in result.stderr
    assert result.returncode == 0


def test_the_env_is_ignored_by_the_real_verifier_too(tmp_path: Path) -> None:
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
    assert "INCOMPLETE ROLLOUT" in result.stderr
    assert "IGNORED: TR_STATUS_URL" in result.stderr
    assert "IGNORED: TR_MAX_DRAIN_LAG_SECONDS" in result.stderr
    assert "COMPLETE:" not in result.stderr


def test_an_override_flag_suppresses_the_complete_banner(tmp_path: Path) -> None:
    """(ii) The escape hatch exists, and it cannot mint a green verdict.

    An override is legitimate for diagnosis ("what would a 4-hour bound say?").
    What it must never do is produce the sentence a deploy script and a reviewer
    read as done — so every stage passing under a flag ends in DIAGNOSTIC, and
    the exit status is its own code rather than 0.
    """
    harness = _harness(tmp_path, plan=_ALL_PASS)
    result = harness.run("--max-lag-seconds", "99999999", "aws")

    assert result.returncode == 4
    assert "DIAGNOSTIC RUN" in result.stderr
    assert "COMPLETE" not in result.stderr
    assert "--max-lag-seconds 99999999" in result.stderr
    assert harness.argv_for("lag")[-2:] == ["--max-lag-seconds", "99999999"]

    harness = _harness(tmp_path / "second", plan=_ALL_PASS)
    result = harness.run("--status-url", "https://elsewhere.example/status.json", "aws")
    assert result.returncode == 4
    assert "COMPLETE" not in result.stderr
    assert harness.fetched_urls == ["https://elsewhere.example/status.json"]


# ---------------------------------------------------------------------------
# The banner says what was measured, and nothing more.
# ---------------------------------------------------------------------------


def test_a_clean_run_is_the_only_one_that_prints_complete(tmp_path: Path) -> None:
    harness = _harness(tmp_path, plan={**_ALL_PASS, "outbox": [0, ""]})
    result = harness.run("aws")
    assert result.returncode == 0
    assert "COMPLETE: aws publishes a live analytics pipeline" in result.stderr
    assert "CAVEAT" not in result.stderr


def test_a_caveated_stage_downgrades_the_banner(tmp_path: Path) -> None:
    """The empty-outbox caveat used to be printed one line ABOVE "COMPLETE"."""
    caveat = "outbox is empty: lag 0 proves nothing is STUCK, not that anything is moving"
    harness = _harness(tmp_path, plan={**_ALL_PASS, "lag": [0, f"caveat: {caveat}"]})
    result = harness.run("aws")

    assert result.returncode == 0
    assert "COMPLETE WITH CAVEATS" in result.stderr
    assert "COMPLETE: aws publishes a live analytics pipeline" not in result.stderr
    assert caveat in result.stderr


def test_an_exempted_stage_never_reads_as_complete(tmp_path: Path) -> None:
    """The worst line the old script could print, and the reason for this rule.

    On the exemption path every stage printed its unconditional green sentence
    and the run ended "COMPLETE: azure publishes a live analytics pipeline" —
    about a cloud whose analytics had, one line earlier, been formally excused
    for not existing.
    """
    waived = "ACCEPTED-ABSENT (azure): canary only — suppressing: never sets the outbox"
    harness = _harness(tmp_path, plan={**_ALL_PASS, "outbox": [0, f"waived: {waived}"]})
    result = harness.run("azure")

    assert result.returncode == 0
    assert "COMPLETE" not in result.stderr
    assert "NOT VERIFIED" in result.stderr
    assert "NOT MEASURED" in result.stderr
    assert waived in result.stderr


def test_an_unpublished_analytics_section_is_its_own_exit_code(tmp_path: Path) -> None:
    """Stage (b) failing means "nobody can see this cloud", not "you broke it".

    aws_eu_clickhouse_drain_install.sh depends on the distinction: the run that
    INSTALLS the drain hits this state by construction today, and reporting it
    as a flat failure is what teaches operators to ignore exit codes.
    """
    harness = _harness(
        tmp_path,
        plan={**_ALL_PASS, "section": [1, "aws: /status.json publishes no 'analytics' section"]},
    )
    result = harness.run("aws")

    assert result.returncode == 5
    assert "NOT YET OBSERVABLE" in result.stderr
    assert "COMPLETE" not in result.stderr
    assert "INCOMPLETE ROLLOUT" not in result.stderr


def test_the_drain_install_reports_the_pre_deploy_state_in_those_words() -> None:
    """And the caller that knows what exit 5 means says so, rather than failing."""
    text = (ROOT / "scripts" / "deploy" / "aws_eu_clickhouse_drain_install.sh").read_text()
    tail = text[text.index("verify_cloud_complete.sh") :]
    assert "-eq 5" in tail
    assert "DRAIN INSTALLED; NOT YET OBSERVABLE FROM OUTSIDE." in tail
    # Still not zero: a pipeline nobody outside can see is not a finished cloud.
    assert "exit 5" in tail
    assert re.search(r'exit "\$VERIFY_RC"', tail)


def test_bring_up_scripts_end_in_the_completeness_check() -> None:
    """ "The script finished" and "the cloud works" must be the same claim.

    THE OTHER binding test, and note what is NOT here: a list of scripts. Until
    2026-08-17 this assertion was a five-row `parametrize` of (script, cloud)
    pairs — a fourth copy of the fleet, in the file whose whole subject is that
    copies drift. A fourth cloud could ship a bring-up script that printed
    "Next: ..." and exited 0 and this suite stayed green, because nobody had
    typed its row.

    The pairs are now fields on the CloudRollout, so a cloud that ships a script
    without wiring it fails here, and the failure says which file to edit.
    """
    gaps = crc.script_binding_gaps()
    assert gaps == [], "\n".join(gaps)

    bound = {
        (script, cloud)
        for cloud, entry in crc.ROLLOUT_REGISTRY.items()
        for script in entry.deploy_scripts
    }
    assert bound, "no cloud binds any deploy script — the mechanism is not connected"
    for script, cloud in sorted(bound):
        text = (ROOT / script).read_text()
        assert f'verify_cloud_complete.sh" {cloud}' in text, (
            f"{script} must end by verifying the {cloud} cloud"
        )
        tail = text.rstrip().splitlines()[-crc.VERIFIER_TAIL_LINES :]
        assert any("verify_cloud_complete.sh" in line for line in tail), (
            f"{script} runs the check but not at the end"
        )


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


def test_a_bound_script_that_loses_the_call_fails_ci(tmp_path: Path) -> None:
    """Wiring that is deleted later must fail the same way as wiring never added."""
    for cloud, entry in crc.ROLLOUT_REGISTRY.items():
        for script in (entry.control_plane_script, *entry.deploy_scripts):
            path = tmp_path / script
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f'bash "${{SCRIPT_DIR}}/verify_cloud_complete.sh" {cloud}\n')
    for item in crc.ROLLOUT_REGISTRY["gcp"].exempt_deploy_scripts:
        (tmp_path / item.script).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / item.script).write_text("# exempt, no verifier call\n")
    assert crc.script_binding_gaps(root=tmp_path) == []

    victim = crc.ROLLOUT_REGISTRY["azure"].deploy_scripts[0]
    (tmp_path / victim).write_text('echo "Next: build the pipeline"\n')
    gaps = crc.script_binding_gaps(root=tmp_path)
    assert any(victim in gap and "does not END in" in gap for gap in gaps), gaps


def test_a_script_that_runs_the_verifier_unclaimed_fails_ci(tmp_path: Path) -> None:
    """Wiring nobody claims is wiring nobody would miss.

    A script that verifies a cloud, with no registry entry naming it, is one
    delete away from silence — and the deletion looks like a cleanup.
    """
    for cloud, entry in crc.ROLLOUT_REGISTRY.items():
        for script in (entry.control_plane_script, *entry.deploy_scripts):
            path = tmp_path / script
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f'bash "${{SCRIPT_DIR}}/verify_cloud_complete.sh" {cloud}\n')
    for item in crc.ROLLOUT_REGISTRY["gcp"].exempt_deploy_scripts:
        (tmp_path / item.script).write_text("# exempt\n")
    (tmp_path / "scripts" / "deploy" / "oracle_bring_up.sh").write_text(
        'bash "${SCRIPT_DIR}/verify_cloud_complete.sh" oracle\n'
    )
    gaps = crc.script_binding_gaps(root=tmp_path)
    assert any("oracle_bring_up.sh" in gap and "no CloudRollout claims it" in gap for gap in gaps)


def test_an_unbound_script_must_carry_a_named_reason() -> None:
    """GCP is exempt, and the exemption is a sentence somebody signed.

    `rollout.sh` runs inside the deploy workflow, so ending it in a public fetch
    of the page that same cloud serves would make an outage un-deployable
    through. That is a real reason — and the requirement is that it be stated in
    code, next to the exemption, rather than being a row absent from a list.
    """
    exemptions = [
        (cloud, item)
        for cloud, entry in crc.ROLLOUT_REGISTRY.items()
        for item in entry.exempt_deploy_scripts
    ]
    assert exemptions, "if nothing is exempt, this test is stale — delete it"
    for cloud, item in exemptions:
        assert (ROOT / item.script).is_file(), f"{cloud}: {item.script} does not exist"
        assert len(item.reason) > 80, f"{cloud}: {item.script} exemption reason is a shrug"

    gcp = crc.ROLLOUT_REGISTRY["gcp"]
    assert [item.script for item in gcp.exempt_deploy_scripts] == ["scripts/deploy/rollout.sh"]
    assert "deploy.yml" in gcp.exempt_deploy_scripts[0].reason


def test_no_bring_up_script_prints_next_steps_and_returns_zero() -> None:
    """The regression guard for the actual outage mechanism.

    `aws_eu_clickhouse.sh` ended with `echo "Next: ..."` and exited 0 on
    2026-08-02. A script whose final word is an instruction has to make that
    instruction load-bearing, so the assertion is positional and not just
    "the file contains the string `exit 1` somewhere": the verifier must be
    invoked, and a failing exit path must come AFTER it. An `exit 1` a hundred
    lines earlier, in some unrelated precondition, is not this guarantee.
    """
    checked = 0
    for cloud, entry in crc.ROLLOUT_REGISTRY.items():
        for script in entry.deploy_scripts:
            lines = (ROOT / script).read_text().splitlines()
            invocations = [
                i for i, line in enumerate(lines) if "verify_cloud_complete.sh" in line
            ]
            assert invocations, f"{script} never runs the check"
            after = "\n".join(lines[invocations[0] :])
            assert re.search(r"^\s*exit\s+(?!0\b)\S+", after, re.MULTILINE), (
                f"{script} runs the check for {cloud} but has no non-zero exit after it, "
                "so its result cannot change the script's exit status"
            )
            checked += 1
    assert checked >= 5
