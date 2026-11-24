"""The decide path's read-through as the node is started with it: the
startup script passes -edge-decide-cache-wait from the instance attribute
fastpath-edge-decide-cache-wait alone, absent by default, only beside the
decide path, and refuses a value the release would not take before it writes
the unit."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_fastpath_p1 import NODE_CACHE_WAIT, _command_flags, _source_at

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra" / "fastpath_node_startup.sh.tftpl"
TF = ROOT / "infra" / "fastpath.tf"

# The first node created from the script that reads the attribute (#2903).
FIRST_READING_NODE = 17

# The pinned-source check may fetch into the same shallow checkout.
pytestmark = pytest.mark.xdist_group("fastpath-pinned-source")


def _run_wait_block(value: str | None, decide: str = "true") -> subprocess.CompletedProcess[str]:
    script = SCRIPT.read_text(encoding="utf-8")
    start = script.index('decide_cache_wait="$(attribute fastpath-edge-decide-cache-wait)"')
    block = script[start:script.index("\nfi\n", start) + len("\nfi\n")]
    # Rendered as it stands: the block has no template interpolation.
    assert "${" not in block and "%{" not in block
    stub = (
        'attribute() { printf "%s" "${A_CACHE_WAIT:-}"; }\nsystemd-cat() { cat >&2; }\n'
        f'decide="{decide}"\n'
    )
    env = {"PATH": "/usr/bin:/bin", **({"A_CACHE_WAIT": value} if value is not None else {})}
    bash = shutil.which("bash")
    assert bash
    return subprocess.run(  # noqa: S603 - bash on the repository's own script
        [bash, "-c", "set -euo pipefail\n" + stub + block + 'printf "%s" "$cache_wait_flags"'],
        capture_output=True, text=True, env=env, check=False,
    )


def _script_wait_rule(script: str) -> re.Pattern[str]:
    """The startup script's own pattern for the attribute, read from the
    script, so the per-node check is the rule the node applies."""
    rule = re.search(r'\[\[ "\$decide_cache_wait" =~ (\^\S+\$) \]\]', script)
    assert rule, "the startup script's check of fastpath-edge-decide-cache-wait"
    return re.compile(rule.group(1))


def _node_cache_waits(tf: str) -> dict[str, str]:
    """Each node's own cache_wait, from the nodes' for_each."""
    resource = tf[tf.index('resource "google_compute_instance" "fastpath_node"'):]
    for_each = re.search(r"^\s*for_each\s*=\s*\{\n(.*?)^\s*\}$", resource, re.M | re.S)
    assert for_each
    entries = re.findall(r'^\s*"(tr-fastpath-\d+)"\s*=\s*\{([^}\n]*)\}$', for_each.group(1), re.M)
    assert entries and len(entries) == len(re.findall(r'^\s*"tr-fastpath-\d+"\s*=', for_each.group(1), re.M))
    waits = {}
    for name, body in entries:
        wait = re.search(r'\bcache_wait\s*=\s*"([^"]*)"', body)
        assert wait, name
        waits[name] = wait.group(1)
    return waits


def _fleet_decide(tf: str) -> str:
    """fastpath-edge-decide as every node carries it (the snapshot's map,
    merged in for each node; no node has its own)."""
    start = tf.index("fastpath_edge_snapshot = tomap({")
    block = tf[start:tf.index("})", start)]
    decide = re.search(r'^\s*fastpath-edge-decide\s*=\s*"([^"]*)"$', block, re.M)
    assert decide
    return decide.group(1)


def _node_wait_problems(tf: str, script: str) -> list[str]:
    rule = _script_wait_rule(script)
    decide = _fleet_decide(tf)
    problems = []
    for name, wait in _node_cache_waits(tf).items():
        if wait == "":
            continue
        if not rule.fullmatch(wait):
            problems.append(f"{name}: {wait!r} is not 0 or 1ms to 250ms")
        if int(name.rsplit("-", 1)[1]) < FIRST_READING_NODE:
            problems.append(f"{name}: created before the script that reads the attribute")
        if decide != "true":
            problems.append(f"{name}: the decide path is off")
    return problems


def _with_wait(tf: str, name: str, value: str) -> str:
    """infra/fastpath.tf with one node's cache_wait set to value."""
    line = re.search(rf'^\s*"{name}"\s*=\s*\{{[^}}\n]*\}}$', tf, re.M)
    assert line, name
    return tf.replace(line.group(0), re.sub(r'cache_wait\s*=\s*"[^"]*"', f'cache_wait = "{value}"', line.group(0)))


def test_a_nodes_own_wait_follows_the_scripts_rule() -> None:
    """A node's cache_wait is "", which adds no attribute, or a value the
    startup script takes: "0" or 1ms to 250ms. Any other value would stop the
    node's script before it writes the unit."""
    tf, script = TF.read_text(encoding="utf-8"), SCRIPT.read_text(encoding="utf-8")
    assert _node_wait_problems(tf, script) == []
    assert NODE_CACHE_WAIT in tf


def test_the_rule_read_from_the_script_is_the_rule_bash_applies() -> None:
    """The control for the per-node check: the pattern read from the script
    accepts and refuses what the script's own block does."""
    rule = _script_wait_rule(SCRIPT.read_text(encoding="utf-8"))
    for value in ("0", "1ms", "9ms", "10ms", "99ms", "100ms", "199ms", "200ms", "249ms", "250ms",
                  "100", "1s", "-1ms", "0ms", "00", "01ms", "251ms", "260ms", "300ms", "1000ms", " 100ms",
                  "100ms -roles auditor", "100ms\n-roles x", "x\n100ms", "off", "0.1s"):
        assert bool(rule.fullmatch(value)) == (_run_wait_block(value).returncode == 0), value


def test_the_node_check_refuses_a_bad_value_an_early_node_and_the_decide_path_off() -> None:
    """The negative controls: each rule, broken in a copy of infra/fastpath.tf,
    is reported, and with every cache_wait "" there is nothing to report."""
    tf, script = TF.read_text(encoding="utf-8"), SCRIPT.read_text(encoding="utf-8")
    waits = _node_cache_waits(tf)
    late = next(name for name, wait in waits.items() if wait)
    early = next(name for name in waits if int(name.rsplit("-", 1)[1]) < FIRST_READING_NODE)
    assert _node_wait_problems(_with_wait(tf, late, "300ms"), script) == [
        f"{late}: '300ms' is not 0 or 1ms to 250ms"]
    assert _node_wait_problems(_with_wait(tf, late, "100"), script) == [f"{late}: '100' is not 0 or 1ms to 250ms"]
    assert _node_wait_problems(_with_wait(tf, early, "0"), script) == [
        f"{early}: created before the script that reads the attribute"]
    decide_off = re.sub(r'(fastpath-edge-decide\s*=\s*)"true"', r'\1"false"', tf)
    assert decide_off != tf
    assert _node_wait_problems(decide_off, script) == [f"{late}: the decide path is off"]
    cleared = decide_off
    for name in waits:
        cleared = _with_wait(cleared, name, "")
    assert _node_wait_problems(cleared, script) == []


def test_a_nodes_own_wait_is_only_on_a_node_from_17_on() -> None:
    """A node keeps the startup script it was created with (ignore_changes =
    [metadata_startup_script]), and only 17 and later were created from the
    script that reads fastpath-edge-decide-cache-wait (#2903). 15 and 16
    would carry the attribute and pass no flag: the plan and the instance
    would show a wait their process does not run, so a canary there would
    measure nothing, and their metadata stays as it was. The node's number
    stands in for its script, which a test cannot read from the instance
    (docs/runbooks/fastpath-deploy.md, "The decide cache's wait" step 2,
    reads it with gcloud)."""
    tf = TF.read_text(encoding="utf-8")
    assert "ignore_changes = [metadata_startup_script]" in tf
    waits = _node_cache_waits(tf)
    assert [name for name, wait in waits.items() if wait and int(name.rsplit("-", 1)[1]) < FIRST_READING_NODE] == []
    # The canary: 17 alone, at the runbook's value; 18 stays as it was, to
    # be read beside it.
    assert {name: wait for name, wait in waits.items() if wait} == {"tr-fastpath-17": "100ms"}


def test_a_nodes_own_wait_needs_the_decide_path() -> None:
    """The script refuses the attribute, "0" included, without
    fastpath-edge-decide "true" on the same node. No node has its own decide
    attribute: each carries the snapshot map's, so that one must be "true"
    while any node carries a wait."""
    tf = TF.read_text(encoding="utf-8")
    resource = tf[tf.index('resource "google_compute_instance" "fastpath_node"'):]
    resource = resource[:resource.index("\n}\n")]
    assert not re.search(r"\bfastpath-edge-decide\s*=", resource)
    if any(_node_cache_waits(tf).values()):
        assert _fleet_decide(tf) == "true"


def test_absent_the_unit_passes_no_wait_flag() -> None:
    for value in (None, ""):
        for decide in ("true", "false", ""):
            ran = _run_wait_block(value, decide)
            assert ran.returncode == 0 and ran.stdout == "", (value, decide, ran.stderr)


def test_zero_or_milliseconds_up_to_the_bound_pass_as_the_flag() -> None:
    for value in ("0", "1ms", "9ms", "10ms", "99ms", "100ms", "199ms", "200ms", "249ms", "250ms"):
        ran = _run_wait_block(value)
        assert ran.returncode == 0, (value, ran.stderr)
        assert ran.stdout.split() == ["-edge-decide-cache-wait", value]


def test_a_value_the_release_would_refuse_stops_the_script() -> None:
    for value in ("100", "1s", "-1ms", "0ms", "00", "01ms", "251ms", "260ms", "300ms", "1000ms", "100ms -roles auditor",
                  " 100ms", "100ms\n-roles x", "x\n100ms", "off", "0.1s"):
        ran = _run_wait_block(value)
        assert ran.returncode == 1, (value, ran.stdout)
        assert "fastpath-edge-decide-cache-wait is 0, 1ms to 250ms, or absent" in ran.stderr, (value, ran.stderr)


def test_the_wait_needs_the_decide_path() -> None:
    for decide in ("false", ""):
        ran = _run_wait_block("100ms", decide)
        assert ran.returncode == 1, (decide, ran.stdout)
        assert "needs fastpath-edge-decide true" in ran.stderr, (decide, ran.stderr)


def test_the_bound_is_the_releases() -> None:
    """The script's bound is the edge's MaxCacheWait, which service.go and
    edge.New hold the flag to."""
    decide_path = (ROOT / "fastpath" / "internal" / "edge" / "decide_path.go").read_text(encoding="utf-8")
    assert re.search(r"^\tMaxCacheWait = 250 \* time\.Millisecond$", decide_path, re.M)


def test_the_flag_waits_for_the_attribute_and_a_release_that_takes_it() -> None:
    """The unit passes the wait's flag only from instance metadata; HEAD's
    command takes it, and once Terraform sets the attribute, for the fleet
    or for one node, the pinned release must take it too: a flag it does
    not take stops the node before either role starts."""
    script = SCRIPT.read_text(encoding="utf-8")
    unit_line = re.search(r"^  -subscriptions auditor,stager \$edge_flags \$snapshot_flags( \$\w+)*$", script, re.M)
    assert unit_line and "$cache_wait_flags" in unit_line.group(0).split(), unit_line
    head = _command_flags((ROOT / "fastpath" / "cmd" / "fastpath" / "main.go").read_text(encoding="utf-8"))
    assert "edge-decide-cache-wait" in head
    tf = (ROOT / "infra" / "fastpath.tf").read_text(encoding="utf-8")
    local = re.findall(r'^\s*fastpath_edge_decide_cache_wait\s*=\s*"([^"]*)"$', tf, re.M)
    assert len(local) == 1, local
    assert "local.fastpath_edge_decide_cache_wait_metadata," in tf
    assert NODE_CACHE_WAIT in tf
    values = [value for value in (local[0], *_node_cache_waits(tf).values()) if value]
    for value in values:
        assert re.fullmatch(r"0|[1-9]ms|[1-9][0-9]ms|1[0-9][0-9]ms|2[0-4][0-9]ms|250ms", value), value
    if values:
        pinned = re.search(r'fastpath_release\s*=\s*"([0-9a-f]{40})"', tf)
        assert pinned
        taken = _command_flags(_source_at(pinned.group(1), "fastpath/cmd/fastpath/main.go"))
        assert "edge-decide-cache-wait" in taken


def test_the_script_that_reads_the_wait_reaches_the_fleet_on_new_nodes() -> None:
    """A node keeps the script it was created with, so the script that reads
    fastpath-edge-decide-cache-wait, fastpath-starved-leases and
    fastpath-settle-hedge reaches the fleet only on nodes added under new
    names: every node is 17 or later, 15 and 16 (from before #2671, #2906
    and #2903) having been removed once 17 and 18 served."""
    script = SCRIPT.read_text(encoding="utf-8")
    for attribute in ("fastpath-edge-decide-cache-wait", "fastpath-starved-leases", "fastpath-settle-hedge"):
        assert f"$(attribute {attribute})" in script, attribute
    tf = (ROOT / "infra" / "fastpath.tf").read_text(encoding="utf-8")
    assert "ignore_changes = [metadata_startup_script]" in tf
    nodes = re.findall(r'^\s*"tr-fastpath-(\d+)"\s*=\s*{ zone = "us-central1-[ab]"', tf, re.M)
    assert nodes and all(int(node) >= 17 for node in nodes), nodes


def test_the_runbook_names_the_switch_and_its_counts() -> None:
    deploy = (ROOT / "docs" / "runbooks" / "fastpath-deploy.md").read_text(encoding="utf-8")
    section = deploy[deploy.index("## The decide cache's wait"):]
    section = section[:section.index("\n## ", 1)] if "\n## " in section[1:] else section
    for needed in ('fastpath_edge_decide_cache_wait = "100ms"', 'fastpath_edge_decide_cache_wait = "0"',
                   "-edge-decide-cache-wait", "cache_miss", "cache_wait", "stale.idle", "cold.evicted",
                   "decide-cache-wait"):
        assert needed in section, needed


def _wait_section() -> str:
    deploy = (ROOT / "docs" / "runbooks" / "fastpath-deploy.md").read_text(encoding="utf-8")
    section = deploy[deploy.index("## The decide cache's wait"):]
    return section[:section.index("\n## ", 1)]


# Step 2's unit rewrite, in its order: the node's value, then leaving, and
# all three waits (its leases, the balancer turning away, the authorize
# route's timeout) before the stop, then the script run again and the flag
# read back. Matched on the step with its whitespace collapsed.
_STEP_TWO = (
    ("the node's value", 'cache_wait = "100ms"'),
    ("leaving", "sudo systemctl kill -s USR1 fastpath"),
    ("the leases", "`fastpathctl node` exits 0"),
    ("the balancer", "`UNHEALTHY` for it in `gcloud compute backend-services get-health tr-fastpath-edge-backend --global`"),
    ("the route's 24 s", "the authorize route's 24 s"),
    ("the stop", "sudo systemctl stop fastpath"),
    ("the script", "sudo google_metadata_script_runner startup"),
    ("the read-back", "-edge-decide-cache-wait 100ms"),
)


def _step_two() -> str:
    section = _wait_section()
    return " ".join(section[section.index("\n2. On a node"):section.index("\n3. Over an hour")].split())


def _step_two_problems(step: str) -> list[str]:
    problems = [f"missing {label}" for label, needle in _STEP_TWO if needle not in step]
    if not problems:
        at = {label: step.index(needle) for label, needle in _STEP_TWO}
        before = ("the node's value", "leaving", "the leases", "the balancer", "the route's 24 s")
        problems = [f"{label} after the stop" for label in before if at[label] > at["the stop"]]
        problems += [f"{label} before the stop" for label in ("the script", "the read-back") if at[label] < at["the stop"]]
        problems += [f"{label} before leaving" for label in before[2:] if at[label] < at["leaving"]]
        if at["leaving"] < at["the node's value"]:
            problems.append("leaving before the node's value")
        if at["the read-back"] < at["the script"]:
            problems.append("the read-back before the script")
    if "A reset is not enough" not in step:
        problems.append("missing the reset warning")
    return problems


def test_the_runbook_takes_one_node_by_its_own_wait() -> None:
    """Step 2 sets the wait for one node by its for_each entry and takes the
    node by the unit rewrite, in order: leaving; its leases drained
    (`fastpathctl node` exiting 0), the balancer's UNHEALTHY and the
    authorize route's 24 s, all before the stop; the script run again; the
    flag read back."""
    assert _step_two_problems(_step_two()) == []


def test_the_step_check_refuses_a_missing_or_late_wait() -> None:
    """The negative controls: each wait before the stop, removed, is
    reported, as is each moved after the stop."""
    step = _step_two()
    for label, needle in _STEP_TWO[2:5]:
        assert _step_two_problems(step.replace(needle, "")) == [f"missing {label}"], label
    stop = "sudo systemctl stop fastpath"
    for label, needle in _STEP_TWO[2:5]:
        late = step.replace(needle, "").replace(stop, f"{stop} {needle}")
        assert _step_two_problems(late) == [f"{label} after the stop"], label


def _merged_cache_wait(tf: str, node: str) -> str | None:
    """fastpath-edge-decide-cache-wait in the node's metadata as Terraform's
    merge renders it: the arguments in the resource's order, a later one's
    key over an earlier one's; None when no argument carries it."""
    assert len(re.findall(r"fastpath-edge-decide-cache-wait\s*=", tf)) == 2
    assert ("fastpath_edge_decide_cache_wait_metadata = {\n    for name, value in { fastpath-edge-decide-cache-wait = "
            'local.fastpath_edge_decide_cache_wait } : name => value if value != ""\n  }') in tf
    resource = tf[tf.index('resource "google_compute_instance" "fastpath_node"'):]
    resource = resource[:resource.index("\n}\n")].replace(NODE_CACHE_WAIT, "NODE_CACHE_WAIT")
    line = re.search(r"^\s*metadata\s*=\s*merge\(\{[^}]*\},\s*([^\n]*)\)$", resource, re.M)
    assert line
    fleet = re.findall(r'^\s*fastpath_edge_decide_cache_wait\s*=\s*"([^"]*)"$', tf, re.M)
    assert len(fleet) == 1
    carried = {"local.fastpath_edge_decide_cache_wait_metadata": fleet[0], "NODE_CACHE_WAIT": _node_cache_waits(tf)[node]}
    value = None
    for argument in line.group(1).split(", "):
        if carried.get(argument, "") != "":
            value = carried[argument]
    return value


def test_a_nodes_own_wait_wins_over_the_fleets() -> None:
    """What the off switch rests on: a node's cache_wait merges after the
    fleet value, so the fleet at "0" leaves a node at "100ms" carrying
    100ms; only every node's "" with the fleet's "" leaves the attribute
    absent. The control: with the node's map first, the fleet's would win."""
    tf = TF.read_text(encoding="utf-8")
    waits = _node_cache_waits(tf)
    assert _merged_cache_wait(tf, "tr-fastpath-17") == "100ms"
    assert [name for name in waits if name != "tr-fastpath-17" and _merged_cache_wait(tf, name) is not None] == []
    fleet_zero = re.sub(r'^(\s*fastpath_edge_decide_cache_wait\s*=\s*)""$', r'\1"0"', tf, flags=re.M)
    assert fleet_zero != tf
    assert _merged_cache_wait(fleet_zero, "tr-fastpath-17") == "100ms"
    assert _merged_cache_wait(fleet_zero, "tr-fastpath-18") == "0"
    cleared = fleet_zero
    for name in waits:
        cleared = _with_wait(cleared, name, "")
    assert _merged_cache_wait(cleared, "tr-fastpath-17") == "0"
    cleared = re.sub(r'^(\s*fastpath_edge_decide_cache_wait\s*=\s*)"0"$', r'\1""', cleared, flags=re.M)
    assert [_merged_cache_wait(cleared, name) for name in waits] == [None] * len(waits)
    fleet_merge = "local.fastpath_edge_decide_cache_wait_metadata"
    swapped = fleet_zero.replace(f"{fleet_merge}, {NODE_CACHE_WAIT}", f"{NODE_CACHE_WAIT}, {fleet_merge}")
    assert swapped != fleet_zero
    assert _merged_cache_wait(swapped, "tr-fastpath-17") == "0"


def test_the_runbook_turns_the_wait_off_on_every_node() -> None:
    """The off switch names the per-node values, since the fleet value alone
    leaves a node's own in place, and the release before the flag only
    with the attribute absent everywhere, since "0" still passes the flag."""
    section = _wait_section()
    off = " ".join(section[section.index("Turning it off"):].split())
    for needed in ("alone turns nothing off where a node carries one", "still carries `100ms`",
                   "`cache_wait = \"\"` (or `cache_wait = \"0\"`) in every node's `for_each` entry",
                   "take each node that carried a wait by the unit rewrite", "absent on every node",
                   'never `"0"`', '`"0"` still passes `-edge-decide-cache-wait 0`'):
        assert needed in off, needed
