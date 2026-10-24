"""P1's run, held to what the nodes carry for it (the release workflow, the
startup script, the load mix)."""

from __future__ import annotations

import base64
import fcntl
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# The pinned-source checks may fetch into the same shallow checkout.
pytestmark = pytest.mark.xdist_group("fastpath-pinned-source")


def test_release_ships_the_load_generator_and_its_mix() -> None:
    workflow = (ROOT / ".github" / "workflows" / "fastpath-release.yml").read_text(encoding="utf-8")
    assert "-o ../release/loadgen ./cmd/loadgen" in workflow
    assert "-o ../release/fastpathwatch ./cmd/fastpathwatch" in workflow
    assert "cp testdata/load-mix.json ../release/load-mix.json" in workflow
    assert "sha256sum fastpath fastpathctl loadgen fastpathwatch load-mix.json > SHA256SUMS" in workflow
    assert "release/loadgen release/fastpathwatch release/load-mix.json release/SHA256SUMS" in workflow


def test_node_installs_the_load_generator_when_the_release_carries_it() -> None:
    script = (ROOT / "infra" / "fastpath_node_startup.sh.tftpl").read_text(encoding="utf-8")
    assert "for tool in loadgen fastpathwatch load-mix.json; do" in script
    # A release from before P1 has none of them; the node still runs.
    assert "carries no $tool; P1's tools are not installed" in script
    assert script.index("for tool in loadgen") < script.index('echo "$release" > /opt/fastpath/release')


def test_load_mix_is_a_histogram_the_generator_takes() -> None:
    mix = json.loads((ROOT / "fastpath" / "testdata" / "load-mix.json").read_text(encoding="utf-8"))
    assert 0 <= mix["stream_share"] <= 1 and 0 <= mix["refund_share"] <= 1
    for name in ("heartbeats", "estimates", "bill_permill"):
        weights = mix[name]
        assert weights and all(w["value"] >= 0 and w["weight"] > 0 for w in weights), name
        assert abs(sum(w["weight"] for w in weights) - 1) < 0.01, name
    assert all(w["value"] >= 1 for w in mix["estimates"])
    # The generator takes no field but the mix's, so where the numbers come
    # from, and which are assumed until measured, is the runbook's to say.
    assert set(mix) == {"stream_share", "heartbeats", "refund_share", "estimates", "bill_permill"}
    runbook = (ROOT / "docs" / "runbooks" / "fastpath-p1.md").read_text(encoding="utf-8")
    assert "tr_reservation_by_terminal" in runbook and "assumptions" in runbook


def _commands(runbook: str) -> list[str]:
    """Every command line of the runbook, in the document's order: the lines
    of its fenced blocks and the inline commands in backticks that start
    with a path or a tool."""
    commands: list[tuple[int, str]] = []
    for block in re.finditer(r"```bash\n(.*?)```", runbook, re.S):
        at = block.start(1)
        for line in block.group(1).splitlines(keepends=True):
            if line.strip():
                commands.append((at, line.strip()))
            at += len(line)
    for inline in re.finditer(r"`((?:/opt/fastpath/|fastpathctl |loadgen |fastpathwatch |gcloud |uv run )[^`]*)`", runbook):
        commands.append((inline.start(), inline.group(1)))
    commands.sort(key=lambda c: c[0])
    return [command for _, command in commands]


def test_runbook_runs_the_load_under_the_watch_and_names_every_binary_by_its_path() -> None:
    runbook = (ROOT / "docs" / "runbooks" / "fastpath-p1.md").read_text(encoding="utf-8")
    commands = _commands(runbook)
    assert len(commands) >= 8, commands
    # No command starts with a bare binary: a fresh IAP login has no
    # /opt/fastpath on its PATH (curl is the image's, for the floor).
    for command in commands:
        assert not re.match(r"(fastpathctl|loadgen|fastpathwatch)\b", command), command
        assert re.match(r"(/opt/fastpath/|gcloud |uv run |curl |FRONT=)", command), command
    # The load runs only under the watch: one command, the watch's, with
    # every ceiling of P1's, and the generator after -run; no generator
    # runs on its own, in the background or by PID.
    watches = [c for c in commands if c.startswith("/opt/fastpath/fastpathwatch ")]
    loads = [c for c in commands if c.startswith("/opt/fastpath/loadgen ")]
    assert len(watches) == 1 and not loads and "-stop-pid" not in runbook and "$!" not in runbook
    watch = watches[0]
    for flag in ("-max-cpu 0.35", "-max-undelivered 1000", "-max-oldest 2m", "-overdue 5m", "-max-overdue 0",
                 "-max-spend $((FUNDED / 10))", "-subscriptions auditor,stager ",
                 "-workspace $WS", "-run /opt/fastpath/loadgen "):
        assert flag in watch, flag
    # The archives batch for up to 300 seconds, so their message age is not
    # the stage's: the watch must not name them.
    assert "archive" not in watch
    load = watch.split("-run ", 1)[1]
    for flag in ("-workspace $WS", "-database $DB", "-mix /opt/fastpath/load-mix.json", "-rate 1", "-front $FRONT"):
        assert flag in load, flag
    # The front doors are read from the nodes, never written down: a node
    # keeps its IP only until it is replaced.
    assert not re.search(r"10\.128\.\d+\.\d+", runbook)
    (front,) = [c for c in commands if c.startswith("FRONT=")]
    assert "FROM tr_fastpath_member WHERE state = 'serving' AND 'frontdoor' IN UNNEST(roles)" in front
    # Before the first run, in this order: the override that puts the
    # workspace at the tier, and the role the watch's Monitoring reads
    # need, both before the watch's command; before the rate rises, each
    # stop exercised.
    override = next(i for i, c in enumerate(commands)
                    if "scripts/provision_fastpath_p1_workspace.py" in c and "--apply" in c)
    # The audit's baseline is what the provisioner printed the rows hold,
    # an owned workspace's earlier funding included: never a figure the
    # runbook wrote down.
    assert re.search(r"`FUNDED` the `credits_total_microdollars` the\s+provisioner printed", runbook)
    assert "prints the workspace's ID and `credits_total_microdollars`" in runbook
    assert "FUNDED=100000000" not in runbook and not re.search(r"`100000000` micro-credits,\s+the `-funded`", runbook)
    role = next(i for i, c in enumerate(commands) if "roles/monitoring.viewer" in c and "tr-fastpath-node@" in c)
    assert max(override, role) < commands.index(watch)
    exercised = runbook.index("the stops P1 can produce are exercised once")
    raised = re.search(r"raises\s+`-rate` by a step", runbook)
    assert raised is not None and raised.start() > exercised
    # The exercises breach once the generator runs, each confirmed by the
    # generations sent and the watch naming the load; a lost view at the
    # start starts no load, exit 1, and no stop to confirm by status; the
    # CPU ceiling is not exercised in production.
    exercises = runbook[exercised:raised.start()]
    for phrase in ("`-max-spend 1`", "the generation log named by `-log` has a line before", '(`"load": "exited"`)',
                   "status $WS` at exit 0", "A lost view: the one `-run` gives, at the start", "exits 1 before any",
                   "not exercised live, a stated deviation", "The CPU ceiling", "The backlog and pending-work ceilings",
                   "stopping one node's `fastpath` unit leaves the other"):
        assert phrase in exercises, phrase
    # No exercise waits on the report, which comes only once the load has
    # ended, and none stops a node's unit as if that stopped a worker.
    for phrase in ("-max-cpu 0.01", "once the report shows generations", "`systemctl stop`"):
        assert phrase not in runbook, phrase


def test_commands_keep_their_order_within_a_block() -> None:
    doc = "```bash\nuv run b\ngcloud a\n/opt/fastpath/c\n```\n`/opt/fastpath/d`\n"
    assert _commands(doc) == ["uv run b", "gcloud a", "/opt/fastpath/c", "/opt/fastpath/d"]


def test_node_ships_its_journal_to_cloud_logging() -> None:
    """The unit writes its alerts as structured JSON to journald; W7's
    alerting reads them in Cloud Logging, so the node runs the Ops Agent
    with a journald receiver whose parser reads the journal's MESSAGE and
    whose fields processor names the unit (P1's second run
    found the alerts stopping in journald). The agent is set up before the
    release is looked up, so a node with no release still ships its journal,
    and restarted on its configuration before the unit starts."""
    import yaml

    script = (ROOT / "infra" / "fastpath_node_startup.sh.tftpl").read_text(encoding="utf-8")
    assert "add-google-cloud-ops-agent-repo.sh" in script and "--also-install" in script
    config = yaml.safe_load(re.search(r"<<AGENT\n(.*?)\nAGENT\n", script, re.S).group(1))
    logging = config["logging"]
    receiver = next(iter(logging["receivers"].values()))
    assert receiver == {"type": "systemd_journald"}
    parser = logging["processors"]["fastpath_json"]
    assert parser == {"type": "parse_json", "field": "MESSAGE"}
    fields = logging["processors"]["fastpath_fields"]
    assert fields["type"] == "modify_fields"
    # The unit is always in a journal record, and the entry's severity is
    # the journal's priority (a severity in the JSON, the agent's special
    # field included, is outranked by it), so the service writes its alerts
    # with the err priority prefix systemd reads, and the agent moves no
    # severity.
    assert set(fields["fields"]) == {'labels."unit"'}
    assert fields["fields"]['labels."unit"'] == {"move_from": "jsonPayload._SYSTEMD_UNIT"}
    service = (ROOT / "fastpath" / "internal" / "service" / "service.go").read_text(encoding="utf-8")
    assert '"<3>"+string(line)' in service and '"logging.googleapis.com/severity"' not in service
    pipeline = logging["service"]["pipelines"]["fastpath"]
    assert pipeline == {"receivers": list(logging["receivers"]), "processors": ["fastpath_json", "fastpath_fields"]}
    # Order: install, then the configuration written, then the agent
    # restarted on it, then the release looked up (and its early exit), then
    # the unit started.
    order = [script.index(step) for step in (
        "--also-install", "/etc/google-cloud-ops-agent/config.yaml <<AGENT", "systemctl restart google-cloud-ops-agent",
        'release="$(', 'if [ -z "$release" ]', "systemctl enable --now fastpath")]
    assert order == sorted(order), order


def _unit_flags(script: str) -> set[str]:
    """The flags the unit's ExecStart passes the command."""
    lines = script.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("ExecStart=/opt/fastpath/fastpath")]
    assert len(starts) == 1, starts
    flags: set[str] = set()
    i = starts[0]
    while lines[i].endswith("\\"):
        i += 1
        found = re.match(r"^\s*-([a-z][a-z-]*)", lines[i])
        assert found, lines[i]
        flags.add(found.group(1))
    # Flags the script adds from the instance's metadata (the edge's) are
    # the unit's too, on a node given them.
    for line in lines:
        if re.match(r"^\s*edge_flags=\"-", line):
            flags.update(re.findall(r"(?:^|\s)-([a-z][a-z-]*)", line.split("=", 1)[1].strip().strip('"')))
    return flags


def _command_flags(source: str) -> set[str]:
    """The flags the command's main.go declares."""
    return set(re.findall(r'flag\.\w+\((?:&[^,]+, )?"([a-z][a-z-]*)"', source))


def _source_at(commit: str, path: str) -> str:
    """The file at the commit, fetched when the checkout is shallow."""
    show = ["git", "-C", str(ROOT), "show", f"{commit}:{path}"]
    shown = subprocess.run(show, capture_output=True, text=True)  # noqa: S603 - git on the pinned SHA
    if shown.returncode != 0:
        # pytest-xdist workers share the checkout, and two fetches at once
        # collide on .git/shallow.lock (exit 128 on CI). One fetch at a
        # time; a worker that waited finds the commit already fetched.
        with (Path(tempfile.gettempdir()) / "tr-p1-fetch.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            shown = subprocess.run(show, capture_output=True, text=True)  # noqa: S603 - as above
            if shown.returncode != 0:
                fetch = ["git", "-C", str(ROOT), "fetch", "--depth=1", "origin", commit]
                subprocess.run(fetch, check=True)  # noqa: S603 - git on the pinned SHA
                shown = subprocess.run(show, capture_output=True, text=True, check=True)  # noqa: S603 - as above
    return shown.stdout


def test_the_unit_passes_only_flags_the_pinned_release_takes() -> None:
    """The node starts the pinned release's command with the unit's flags:
    one that command does not take stops it before either role starts, on
    both nodes, restarted into the same refusal. So the unit's flags are
    held to the pinned command's, not HEAD's (docs/runbooks/fastpath-deploy.md)."""
    script = (ROOT / "infra" / "fastpath_node_startup.sh.tftpl").read_text(encoding="utf-8")
    pinned = re.search(r'fastpath_release\s*=\s*"([0-9a-f]{40})"', (ROOT / "infra" / "fastpath.tf").read_text())
    assert pinned
    passed = _unit_flags(script)
    assert {"roles", "address", "database"} <= passed
    taken = _command_flags(_source_at(pinned.group(1), "fastpath/cmd/fastpath/main.go"))
    assert passed <= taken, sorted(passed - taken)
    deploy = (ROOT / "docs" / "runbooks" / "fastpath-deploy.md").read_text(encoding="utf-8")
    assert "## A flag the unit passes" in deploy and "two releases, and no flag" in deploy


_SNAPSHOT_ATTRIBUTES = (
    "fastpath-edge-snapshot-url",
    "fastpath-edge-snapshot-public-key",
    "fastpath-edge-snapshot-kid",
    "fastpath-edge-snapshot-plane",
    "fastpath-edge-shadow",
)


def _snapshot_flags(script: str) -> set[str]:
    """The flags the script adds for the edge's routing snapshot and shadow
    (E2), from the instance's metadata alone."""
    flags: set[str] = set()
    for line in script.splitlines():
        if "snapshot_flags=" in line:
            flags.update(re.findall(r"(?:^|[\s\"])-([a-z][a-z-]*)", line.split("snapshot_flags=", 1)[1]))
    return flags


def test_the_snapshot_flags_wait_for_metadata_and_a_release_that_takes_them() -> None:
    """The routing snapshot's flags reach the unit only from instance
    metadata, which infra/fastpath.tf sets on the nodes it names alone, so
    any other node polls and compares nothing. Once Terraform sets any of
    those attributes, the pinned release must take every flag they add, as
    for the edge's own."""
    script = (ROOT / "infra" / "fastpath_node_startup.sh.tftpl").read_text(encoding="utf-8")
    flags = _snapshot_flags(script)
    assert flags == {
        "edge-snapshot-url", "edge-snapshot-public-key", "edge-snapshot-kid", "edge-snapshot-plane", "edge-shadow",
    }
    for attribute in _SNAPSHOT_ATTRIBUTES:
        assert f"$(attribute {attribute})" in script, attribute
    assert "-subscriptions auditor,stager $edge_flags $snapshot_flags" in script
    # Not among the unit's unconditional flags.
    assert not flags & _unit_flags(script)
    head = _command_flags((ROOT / "fastpath" / "cmd" / "fastpath" / "main.go").read_text(encoding="utf-8"))
    assert flags <= head, sorted(flags - head)
    tf = (ROOT / "infra" / "fastpath.tf").read_text(encoding="utf-8")
    if any(attribute in tf for attribute in _SNAPSHOT_ATTRIBUTES):
        pinned = re.search(r'fastpath_release\s*=\s*"([0-9a-f]{40})"', tf)
        assert pinned
        taken = _command_flags(_source_at(pinned.group(1), "fastpath/cmd/fastpath/main.go"))
        assert flags <= taken, sorted(flags - taken)


def _run_snapshot_block(
    script: str, upstream: str = "https://trusted-router-x.a.run.app", **attributes: str,
) -> subprocess.CompletedProcess[str]:
    """The startup script's routing snapshot block, run by bash with the
    instance's attributes given, the edge's upstream and flags set."""
    start = script.index('snapshot_url="$(attribute fastpath-edge-snapshot-url)"')
    block = script[start:script.index("install -d /opt/fastpath", start)]
    # Rendered as it stands: the block has no template interpolation.
    assert "${" not in block and "%{" not in block
    stub = (
        'attribute() { local name="A_${1//-/_}"; printf "%s" "${!name:-}"; }\n'
        "systemd-cat() { cat >&2; }\n"
        f'edge_upstream="{upstream}"\nedge_flags="-edge-address 10.0.0.1:8081"\n'
    )
    env = {"PATH": "/usr/bin:/bin", **{"A_" + name.replace("-", "_"): value for name, value in attributes.items()}}
    bash = shutil.which("bash")
    assert bash
    return subprocess.run(  # noqa: S603 - bash on the repository's own script
        [bash, "-c", "set -euo pipefail\n" + stub + block + 'printf "%s" "$snapshot_flags"'],
        capture_output=True, text=True, env=env, check=False,
    )


def test_the_script_refuses_a_snapshot_url_off_the_edges_upstream() -> None:
    """edge.New refuses a snapshot URL on another scheme or host than the
    upstream, which would end the whole process, admission included; the
    startup script refuses it first, and passes one on the upstream."""
    script = (ROOT / "infra" / "fastpath_node_startup.sh.tftpl").read_text(encoding="utf-8")
    key = "A" * 43
    given = {
        "fastpath-edge-snapshot-public-key": key,
        "fastpath-edge-snapshot-kid": "routing-snapshot-1",
        "fastpath-edge-snapshot-plane": "gcp/us-central1",
    }
    ok = _run_snapshot_block(
        script, **given, **{"fastpath-edge-snapshot-url": "https://trusted-router-x.a.run.app/internal/fastpath/routing-snapshot"},
        **{"fastpath-edge-shadow": "true"},
    )
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout == (
        "-edge-snapshot-url https://trusted-router-x.a.run.app/internal/fastpath/routing-snapshot "
        f"-edge-snapshot-public-key {key} -edge-snapshot-kid routing-snapshot-1 -edge-snapshot-plane gcp/us-central1 "
        "-edge-shadow"
    )
    for url in (
        "https://elsewhere.example/internal/fastpath/routing-snapshot",
        "http://trusted-router-x.a.run.app/internal/fastpath/routing-snapshot",
        "https://trusted-router-x.a.run.app:8443/internal/fastpath/routing-snapshot",
        "https://trusted-router-x.a.run.app.elsewhere.example/internal/fastpath/routing-snapshot",
        "https:/trusted-router-x.a.run.app/x",
    ):
        refused = _run_snapshot_block(script, **given, **{"fastpath-edge-snapshot-url": url})
        assert refused.returncode == 1, url
        assert "not on the edge's upstream" in refused.stderr, (url, refused.stderr)
    # An upstream with userinfo, which the edge refuses beside a snapshot.
    userinfo = _run_snapshot_block(
        script, upstream="https://u@trusted-router-x.a.run.app", **given,
        **{"fastpath-edge-snapshot-url": "https://trusted-router-x.a.run.app/internal/fastpath/routing-snapshot"},
    )
    assert userinfo.returncode == 1 and "carries userinfo" in userinfo.stderr, userinfo.stderr
    # None set: nothing passed.
    none = _run_snapshot_block(script)
    assert none.returncode == 0 and none.stdout == "", none.stderr


def test_the_snapshot_attributes_terraform_sets_pass_the_startup_script() -> None:
    """The attributes infra/fastpath.tf gives the snapshot's nodes, rendered
    as Terraform renders them, pass the startup script's own checks (all
    four, on the edge's upstream, characters the unit can carry) and turn
    the shadow on; a value the script refused would leave a new node
    stopped. Every node Terraform declares is given them."""
    tf = (ROOT / "infra" / "fastpath.tf").read_text(encoding="utf-8")
    script = (ROOT / "infra" / "fastpath_node_startup.sh.tftpl").read_text(encoding="utf-8")
    upstream = re.search(r'^\s*fastpath_edge_upstream\s*=\s*"([^"]+)"', tf, re.M)
    region = re.search(r'^\s*fastpath_region\s*=\s*"([^"]+)"', tf, re.M)
    assert upstream and region
    start = tf.index("fastpath_edge_snapshot = tomap({")
    block = tf[start:tf.index("})", start)]
    rendered = {
        name: value.replace("${local.fastpath_edge_upstream}", upstream.group(1))
        .replace("${local.fastpath_region}", region.group(1))
        for name, value in re.findall(r'^\s*(fastpath-edge-[a-z-]+)\s*=\s*"([^"]*)"', block, re.M)
    }
    assert set(rendered) == set(_SNAPSHOT_ATTRIBUTES), sorted(rendered)
    assert not any("${" in value for value in rendered.values()), rendered
    assert rendered["fastpath-edge-snapshot-plane"] == "gcp/us-central1"
    # The key and kid are the ones the control plane signs with (rollout.sh's
    # pin): a different valid key fails every verification, and a key that is
    # not 32 bytes stops the binary at start, admission and all.
    rollout = (ROOT / "scripts" / "deploy" / "rollout.sh").read_text(encoding="utf-8")
    for attribute, literal in (
        ("fastpath-edge-snapshot-public-key", "ROUTING_SNAPSHOT_PUBLIC_KEY"),
        ("fastpath-edge-snapshot-kid", "ROUTING_SNAPSHOT_KID"),
    ):
        pinned = re.search(rf'^{literal}="([^"]+)"$', rollout, re.M)
        assert pinned and rendered[attribute] == pinned.group(1), (attribute, pinned)
    key = rendered["fastpath-edge-snapshot-public-key"]
    assert len(base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))) == 32, key
    ran = _run_snapshot_block(script, upstream=upstream.group(1), **rendered)
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout == (
        f"-edge-snapshot-url {upstream.group(1)}/internal/fastpath/routing-snapshot "
        f"-edge-snapshot-public-key {rendered['fastpath-edge-snapshot-public-key']} "
        f"-edge-snapshot-kid {rendered['fastpath-edge-snapshot-kid']} -edge-snapshot-plane gcp/us-central1 -edge-shadow"
    )
    # Every node: the for_each declares at least one, and the metadata merges
    # the snapshot's attributes in with no condition on the node's name.
    resource = tf[tf.index('resource "google_compute_instance" "fastpath_node"'):]
    resource = resource[:resource.index("\n}\n")]
    for_each = re.search(r"^\s*for_each\s*=\s*\{([^}]*)\}", resource, re.M)
    assert for_each
    assert re.findall(r'^\s*"tr-fastpath-\d+"\s*=\s*"us-central1-[a-z]"$', for_each.group(1), re.M)
    metadata = re.search(r"^\s*metadata\s*=\s*merge\(\{[^}]*\},\s*([^\n]*)\)$", resource, re.M)
    assert metadata and metadata.group(1) == "local.fastpath_edge_snapshot", metadata


def test_the_snapshot_flags_are_found() -> None:
    """The control: the reader finds the flags in each line that adds them."""
    script = (
        'snapshot_flags="-edge-snapshot-url $u -edge-snapshot-kid $k"\n'
        '    true) snapshot_flags="$snapshot_flags -edge-shadow" ;;\n'
        'edge_flags="-edge-address $x"\n'
    )
    assert _snapshot_flags(script) == {"edge-snapshot-url", "edge-snapshot-kid", "edge-shadow"}


def test_a_flag_the_pinned_command_lacks_is_found() -> None:
    """The control: the check reads the unit's flags and the command's."""
    unit = "[Service]\nExecStart=/opt/fastpath/fastpath \\\n  -roles admission \\\n  -hold-boot\nKillSignal=SIGTERM\n"
    command = 'r := flag.String("roles", "", "")\nflag.BoolVar(&b, "boot", false, "")\nflag.Parse()\n'
    assert _unit_flags(unit) == {"roles", "hold-boot"}
    assert _command_flags(command) == {"roles", "boot"}
    assert _unit_flags(unit) - _command_flags(command) == {"hold-boot"}
    given = 'edge_flags="-edge-address $x:8081 -edge-upstream $u"\n' + unit
    assert _unit_flags(given) == {"roles", "hold-boot", "edge-address", "edge-upstream"}
