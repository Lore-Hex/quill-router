"""P1's run, held to what the nodes carry for it (the release workflow, the
startup script, the load mix)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
    assert any(c.startswith("FRONT=$(gcloud compute instances list") for c in commands)
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
    return flags


def _command_flags(source: str) -> set[str]:
    """The flags the command's main.go declares."""
    return set(re.findall(r'flag\.\w+\((?:&[^,]+, )?"([a-z][a-z-]*)"', source))


def _source_at(commit: str, path: str) -> str:
    """The file at the commit, fetched when the checkout is shallow."""
    show = ["git", "-C", str(ROOT), "show", f"{commit}:{path}"]
    shown = subprocess.run(show, capture_output=True, text=True)  # noqa: S603 - git on the pinned SHA
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


def test_a_flag_the_pinned_command_lacks_is_found() -> None:
    """The control: the check reads the unit's flags and the command's."""
    unit = "[Service]\nExecStart=/opt/fastpath/fastpath \\\n  -roles admission \\\n  -hold-boot\nKillSignal=SIGTERM\n"
    command = 'r := flag.String("roles", "", "")\nflag.BoolVar(&b, "boot", false, "")\nflag.Parse()\n'
    assert _unit_flags(unit) == {"roles", "hold-boot"}
    assert _command_flags(command) == {"roles", "boot"}
    assert _unit_flags(unit) - _command_flags(command) == {"hold-boot"}
