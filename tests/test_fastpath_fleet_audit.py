"""The fleet audit (docs/design/fast-admission-widening.md section 7), held to
what watches it and what turns it on: the policy on its summary line's
absence (infra/fastpath_alerts.tf) against the line the Go code writes, the
node attribute that passes its flag (infra/fastpath.tf and the startup
script), and the flag the command takes."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from tests.test_fastpath_p1 import _command_flags, _source_at, _unit_flags

ROOT = Path(__file__).resolve().parents[1]
POLICIES = ROOT / "infra" / "fastpath_alerts.tf"
SCRIPT = ROOT / "infra" / "fastpath_node_startup.sh.tftpl"


def _read(*parts: str) -> str:
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


def _block(tf: str, kind: str, name: str) -> str:
    """One resource's block, through the next top-level closing brace."""
    start = tf.index(f'resource "{kind}" "{name}" {{')
    end = tf.find("\n}\n", start)
    assert end > start
    return tf[start:end]


def _summary_format() -> str:
    """FleetSummaryLine as the Go source spells it, its pieces joined."""
    fleet = _read("fastpath", "internal", "auditor", "fleet.go")
    match = re.search(r"^const FleetSummaryLine = ((?:\"[^\"]*\"(?: \+\n\t)?)+)$", fleet, re.M)
    assert match, "FleetSummaryLine"
    return "".join(re.findall(r'"([^"]*)"', match.group(1)))


def _fleet_local() -> str:
    tf = _read("infra", "fastpath.tf")
    found = re.findall(r"^\s*fastpath_fleet_audit\s*=\s*(\w+)$", tf, re.M)
    assert len(found) == 1, found
    return found[0]


def test_the_absence_policy_counts_the_line_the_audit_writes() -> None:
    """The metric's filter matches the summary line's beginning, up to its
    first count, in the nodes' journal, and nothing that a fault line could
    carry (a fault is a JSON alert, its text under jsonPayload.message)."""
    tf = POLICIES.read_text(encoding="utf-8")
    line = _summary_format()
    assert line.startswith("fastpath fleet audit: due=%d audited=%d leased_due=%d leased_audited=%d ")
    prefix = line[: line.index("%")]
    assert prefix == "fastpath fleet audit: due="
    filters = re.search(r"fastpath_fleet_audit_summary_filter = join\(\" AND \", \[\n(.*?)\n\s+\]\)", tf, re.S)
    assert filters
    parts = [p.strip().rstrip(",") for p in filters.group(1).splitlines()]
    assert parts == [
        '"logName=\\"projects/${local.gcp_project_id}/logs/fastpath_journal\\""',
        f'"jsonPayload.MESSAGE:\\"{prefix}\\""',
    ]
    metric = _block(tf, "google_logging_metric", "fastpath_fleet_audit_summary")
    assert "filter      = local.fastpath_fleet_audit_summary_filter" in metric
    assert 'metric_kind = "DELTA"' in metric and 'value_type  = "INT64"' in metric
    # The Go side writes the line through the logger the journal keeps,
    # once a pass, a pass with nothing due included.
    fleet = _read("fastpath", "internal", "auditor", "fleet.go")
    assert fleet.count("f.logf(FleetSummaryLine,") == 1


def test_the_absence_condition_fires_on_ninety_minutes_of_no_line_from_any_node() -> None:
    """No summary from any node for 90 minutes, the series summed over the
    nodes so the key moving between them is not an absence; past the pass's
    hour, so one late pass does not page."""
    tf = POLICIES.read_text(encoding="utf-8")
    policy = _block(tf, "google_monitoring_alert_policy", "fastpath_fleet_audit_absent")
    assert policy.count("conditions {") == 1 and policy.count("condition_absent {") == 1
    assert "condition_matched_log" not in policy and "condition_threshold" not in policy
    assert '"resource.type = \\"gce_instance\\""' in policy
    assert (
        '"metric.type = \\"logging.googleapis.com/user/${google_logging_metric.fastpath_fleet_audit_summary.name}\\""'
        in policy
    )
    (duration,) = re.findall(r'^\s+duration = "(\d+)s"$', policy, re.M)
    assert int(duration) == 90 * 60
    assert re.search(r'^\s+per_series_aligner\s+= "ALIGN_SUM"$', policy, re.M)
    assert re.search(r'^\s+cross_series_reducer\s+= "REDUCE_SUM"$', policy, re.M)
    assert "group_by_fields" not in policy
    (alignment,) = re.findall(r'^\s+alignment_period\s+= "(\d+)s"$', policy, re.M)
    assert int(alignment) < int(duration)
    service = _read("fastpath", "internal", "service", "service.go")
    defaults = re.search(r"Fleet: auditor\.FleetConfig\{Every: time\.Hour, Check: time\.Minute, Within: 26 \* time\.Hour, "
                         r"Rate: 5,\n\s+Deadline: 10 \* time\.Second, Stale: 3 \* time\.Hour\}", service)
    assert defaults, "the fleet audit's defaults"
    assert 60 * 60 < int(duration)
    assert 'display_name = "TR Fast path: fleet_audit_stale, no summary"' in policy
    assert 'severity     = "ERROR"' in policy
    assert "notification_channels = local.fastpath_alert_channels" in policy
    assert "docs/runbooks/fastpath-alerts.md" in policy
    runbook = _read("docs", "runbooks", "fastpath-alerts.md")
    assert "TR Fast path: fleet_audit_stale, no summary" in runbook and "## The fleet audit" in runbook


def test_the_audit_and_its_absence_policy_turn_on_together() -> None:
    """One literal turns the nodes' attribute and the absence policy on, so
    the policy never pages for an audit no node runs; dark by default."""
    tf = POLICIES.read_text(encoding="utf-8")
    policy = _block(tf, "google_monitoring_alert_policy", "fastpath_fleet_audit_absent")
    assert re.search(r"^\s+enabled\s+= local\.fastpath_fleet_audit$", policy, re.M)
    assert _fleet_local() in {"true", "false"}
    fastpath = _read("infra", "fastpath.tf")
    assert 'fastpath-fleet-audit       = local.fastpath_fleet_audit ? "true" : "false"' in fastpath
    # The command's own default is off: a node given no flag audits nothing.
    main = _read("fastpath", "cmd", "fastpath", "main.go")
    assert 'flag.BoolVar(&cfg.FleetAudit, "fleet-audit", false,' in main
    assert "if cfg.FleetAudit {" in _read("fastpath", "internal", "service", "service.go")


def _run_fleet_block(**attributes: str) -> subprocess.CompletedProcess[str]:
    """The startup script's fleet audit block, run by bash with the
    instance's attributes given."""
    script = SCRIPT.read_text(encoding="utf-8")
    start = script.index('fleet_audit="$(attribute fastpath-fleet-audit)"')
    block = script[start:script.index("esac\n", start) + len("esac\n")]
    assert "${" not in block and "%{" not in block
    stub = (
        'attribute() { local name="A_${1//-/_}"; printf "%s" "${!name:-}"; }\n'
        "systemd-cat() { cat >&2; }\n"
    )
    env = {"PATH": "/usr/bin:/bin", **{"A_" + name.replace("-", "_"): value for name, value in attributes.items()}}
    bash = shutil.which("bash")
    assert bash
    return subprocess.run(  # noqa: S603 - bash on the repository's own script
        [bash, "-c", "set -euo pipefail\n" + stub + block + 'printf "%s" "$fleet_flags"'],
        capture_output=True, text=True, env=env, check=False,
    )


def test_the_script_passes_the_flag_only_for_true() -> None:
    """true passes -fleet-audit; false or absent pass nothing; anything else
    stops the script, as the edge's switches do."""
    on = _run_fleet_block(**{"fastpath-fleet-audit": "true"})
    assert on.returncode == 0 and on.stdout == "-fleet-audit", on.stderr
    for off in ({"fastpath-fleet-audit": "false"}, {}):
        ran = _run_fleet_block(**off)
        assert ran.returncode == 0 and ran.stdout == "", (off, ran.stderr)
    refused = _run_fleet_block(**{"fastpath-fleet-audit": "yes"})
    assert refused.returncode == 1 and "fastpath-fleet-audit is true, false or absent" in refused.stderr
    script = SCRIPT.read_text(encoding="utf-8")
    unit_line = re.search(r"^  -subscriptions auditor,stager \$edge_flags \$snapshot_flags( \$\w+)*$",
                          script, re.M)
    assert unit_line and "$fleet_flags" in unit_line.group(0).split(), unit_line
    # Not among the unit's unconditional flags, which the pinned release
    # must take whatever the attribute says.
    assert "fleet-audit" not in _unit_flags(script)


def test_the_flag_waits_for_a_release_that_takes_it() -> None:
    """HEAD's command takes the flag; once infra/fastpath.tf turns the audit
    on, the pinned release must take it too, or every node would stop at
    its start (docs/runbooks/fastpath-deploy.md, "A flag the unit passes")."""
    assert "fleet-audit" in _command_flags(_read("fastpath", "cmd", "fastpath", "main.go"))
    if _fleet_local() == "true":
        pinned = re.search(r'fastpath_release\s*=\s*"([0-9a-f]{40})"', _read("infra", "fastpath.tf"))
        assert pinned
        assert "fleet-audit" in _command_flags(_source_at(pinned.group(1), "fastpath/cmd/fastpath/main.go"))


def test_the_pilot_retires_the_hourly_script() -> None:
    """The pilot's checks name the fleet audit in the hourly script's place,
    and the script only until the audit's first complete summary."""
    pilot = _read("docs", "design", "fast-admission-pilot.md")
    assert "The fleet audit (`docs/design/fast-admission-widening.md` §7" in pilot
    assert "which retires the\n  hourly script" in pilot
    assert "the script is stopped and started on no node again" in pilot
