"""P1's run, held to what the nodes carry for it (the release workflow, the
startup script, the load mix)."""

from __future__ import annotations

import json
import re
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
    # /opt/fastpath on its PATH.
    for command in commands:
        assert not re.match(r"(fastpathctl|loadgen|fastpathwatch)\b", command), command
        assert re.match(r"(/opt/fastpath/|gcloud |uv run |LOADGEN=)", command), command
    # The load runs only under the watch: one command, the watch's, with
    # every ceiling of P1's, and the generator after -run; no generator
    # runs on its own, in the background or by PID.
    watches = [c for c in commands if c.startswith("/opt/fastpath/fastpathwatch ")]
    loads = [c for c in commands if c.startswith("/opt/fastpath/loadgen ")]
    assert len(watches) == 1 and not loads and "-stop-pid" not in runbook and "$!" not in runbook
    watch = watches[0]
    for flag in ("-max-cpu 0.35", "-max-undelivered 1000", "-max-oldest 2m", "-overdue 5m", "-max-overdue 0",
                 "-max-spend $((FUNDED / 10))", "-subscriptions auditor,stager,settle-log-archive,records-archive",
                 "-workspace $WS", "-run /opt/fastpath/loadgen "):
        assert flag in watch, flag
    load = watch.split("-run ", 1)[1]
    for flag in ("-workspace $WS", "-database $DB", "-mix /opt/fastpath/load-mix.json", "-rate 1"):
        assert flag in load, flag
    # Before the first run, in this order: the override that puts the
    # workspace at the tier, and the role the watch's Monitoring reads
    # need, both before the watch's command; before the rate rises, each
    # stop exercised.
    override = next(i for i, c in enumerate(commands)
                    if "scripts/set_trust_override.py" in c and "--tier 1" in c and "--apply" in c)
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
