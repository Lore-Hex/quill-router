"""G1 in docs/design/clickhouse-high-availability.md: one publisher at a time,
fenced durably by instance metadata, with a manual takeover in the order the
design requires. Every script here runs for real against stubs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROLE_DIR = ROOT / "scripts/deploy/clickhouse-worker-role"
WRAPPER = ROOT / "scripts/deploy/clickhouse_worker_role.sh"
BASH = shutil.which("bash") or "/bin/bash"
SERVICES = [
    "tr-clickhouse-ingest",
    "tr-clickhouse-operational-ingest",
    "tr-clickhouse-workspace-directory",
    "tr-clickhouse-overrun-rollup",
    "tr-clickhouse-archive",
    "tr-clickhouse-archive-restore",
    "tr-clickhouse-rollup-hourly",
    "tr-clickhouse-rollup-daily",
    "tr-clickhouse-synthetic-rollup",
    "tr-clickhouse-client-rollup",
    "tr-clickhouse-public-snapshots",
    "tr-clickhouse-spanner-delivery",
]

NODE_STUB = r"""#!/usr/bin/env bash
name="$(basename "$0")"
python3 - "$STUB_LOG" "$name" "$@" <<'PY'
import json, sys
log, name, *argv = sys.argv[1:]
with open(log, "a") as handle:
    handle.write(json.dumps([name, *argv]) + "\n")
PY
state="$STUB_STATE"
case "$name" in
  curl)
    # Next queued "body|code"; the last line repeats.
    line="$(head -1 "$state/metadata")"
    if [ "$(wc -l < "$state/metadata")" -gt 1 ]; then
      tail -n +2 "$state/metadata" > "$state/metadata.next"; mv "$state/metadata.next" "$state/metadata"
    fi
    printf '%s\n%s' "${line%%|*}" "${line##*|}"
    ;;
  systemctl)
    verb="$1"
    shift
    [ "${1:-}" = "--now" ] && shift
    case "$verb" in
      enable)
        [ ! -e "$state/enable-fails" ] || exit 1
        for unit in "$@"; do printf active > "$state/unit-$unit"; done
        ;;
      disable)
        [ ! -e "$state/disable-fails" ] || exit 1
        for unit in "$@"; do printf inactive > "$state/unit-$unit"; done
        ;;
      show)
        [ ! -e "$state/show-fails" ] || exit 1
        unit="${@: -1}"
        if [ -e "$state/force-$unit" ]; then cat "$state/force-$unit"
        elif [ -e "$state/unit-$unit" ]; then cat "$state/unit-$unit"
        else printf inactive
        fi
        printf '\n'
        ;;
    esac
    ;;
  clickhouse-client)
    query="${@: -1}"
    case "$query" in
      *"FROM system.processes"*) cat "$state/processes" 2>/dev/null || printf '0\n' ;;
      *"FROM system.tables"*) cat "$state/tables" 2>/dev/null || true ;;
      *"SYNC REPLICA"*)
        for table in $(cat "$state/unsynced" 2>/dev/null); do
          case "$query" in *"\`$table\`"*) exit 159 ;; esac
        done
        ;;
      *"FROM system.replication_queue"*) printf 'GET_PART\tall_1_1_0\ttr-clickhouse-1\tNo active replica has part\n' ;;
    esac
    ;;
  hostname) printf 'tr-clickhouse-test\n' ;;
esac
exit 0
"""


class Node:
    def __init__(self, tmp_path: Path, metadata: str = "publisher|200") -> None:
        self.root = tmp_path / "node"
        (self.root / "etc/systemd/system").mkdir(parents=True)
        (self.root / "etc/tr-clickhouse-ingest.env").write_text("CH_PASSWORD=fixture-node-credential\n")
        self.bundle = tmp_path / "bundle"
        shutil.copytree(ROLE_DIR, self.bundle)
        self.state = tmp_path / "state"
        self.state.mkdir()
        (self.state / "metadata").write_text(metadata + "\n")
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name in ("curl", "systemctl", "clickhouse-client", "hostname", "sleep"):
            (self.bin / name).write_text(NODE_STUB)
            (self.bin / name).chmod(0o755)
        self.log = tmp_path / "calls.jsonl"

    def run(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        self.log.write_text("")
        return subprocess.run(  # noqa: S603 - fixed scripts under test
            [BASH, str(self.bundle / script), *args],
            env={
                "PATH": f"{self.bin}:{os.environ['PATH']}",
                "HOME": str(self.root),
                "STUB_LOG": str(self.log),
                "STUB_STATE": str(self.state),
                "TR_NODE_ROOT": str(self.root),
                "TR_ROLE_ATTEMPTS": "3",
            },
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def calls(self, name: str) -> list[list[str]]:
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [row[1:] for row in rows if row[0] == name]


@pytest.mark.parametrize(
    ("metadata", "code"),
    [
        ("publisher|200", 0),
        ("standby|200", 1),
        ("Publisher|200", 1),
        ("|404", 1),
        ("|000", 255),
        ("oops|500", 255),
        ("|000\n|503\npublisher|200", 0),
    ],
    ids=["publisher", "standby", "case-sensitive", "absent", "unreachable", "server-error", "retries"],
)
def test_role_check_follows_execcondition_semantics(tmp_path: Path, metadata: str, code: int) -> None:
    node = Node(tmp_path, metadata)
    (node.bundle / "role-check").chmod(0o755)
    result = subprocess.run(  # noqa: S603 - fixed script under test
        ["/bin/sh", str(node.bundle / "role-check")],
        env={
            "PATH": f"{node.bin}:{os.environ['PATH']}",
            "STUB_LOG": str(node.log),
            "STUB_STATE": str(node.state),
            "TR_ROLE_ATTEMPTS": "3",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == code, result.stderr


def test_the_fence_installs_a_role_check_on_every_worker(tmp_path: Path) -> None:
    node = Node(tmp_path, "publisher|200")

    result = node.run("install_fence.sh", "publisher")

    assert result.returncode == 0, result.stderr
    check = node.root / "usr/local/libexec/tr-clickhouse-role-check"
    assert check.read_text() == (ROLE_DIR / "role-check").read_text()
    for unit in SERVICES:
        drop_in = node.root / f"etc/systemd/system/{unit}.service.d/tr-clickhouse-role.conf"
        assert drop_in.read_text() == (
            "[Service]\nExecCondition=/usr/local/libexec/tr-clickhouse-role-check\n"
        )
    assert node.calls("systemctl") == [["daemon-reload"]]


@pytest.mark.parametrize(
    ("expected", "metadata"),
    [("publisher", "standby|200"), ("publisher", "|404"), ("standby", "publisher|200"), ("publisher", "|000")],
    ids=["publisher-without-role", "publisher-unset", "standby-is-publisher", "unreadable"],
)
def test_the_fence_refuses_a_node_whose_role_does_not_match(
    tmp_path: Path, expected: str, metadata: str
) -> None:
    # Installing the fence on the live publisher before its metadata says so
    # would stop every drain and job at their next start.
    node = Node(tmp_path, metadata)

    result = node.run("install_fence.sh", expected)

    assert result.returncode != 0
    assert "set its tr-clickhouse-role metadata first" in result.stderr
    assert not (node.root / "usr/local/libexec/tr-clickhouse-role-check").exists()
    assert node.calls("systemctl") == []


def test_stop_workers_disables_every_timer_and_service(tmp_path: Path) -> None:
    node = Node(tmp_path)

    result = node.run("node_takeover.sh", "stop-workers")

    assert result.returncode == 0, result.stderr
    disable = next(call for call in node.calls("systemctl") if call[:2] == ["disable", "--now"])
    assert {unit for unit in disable[2:] if unit.endswith(".service")} == {f"{u}.service" for u in SERVICES}
    assert len([unit for unit in disable[2:] if unit.endswith(".timer")]) == 10


@pytest.mark.parametrize("state", ["active", "activating", "deactivating", "reloading"])
def test_stop_workers_refuses_while_a_worker_is_still_running(tmp_path: Path, state: str) -> None:
    node = Node(tmp_path)
    # A oneshot job still running reports "activating", not "active".
    (node.state / "force-tr-clickhouse-public-snapshots.service").write_text(state)

    result = node.run("node_takeover.sh", "stop-workers")

    assert result.returncode != 0
    assert f"tr-clickhouse-public-snapshots.service {state}" in result.stderr


@pytest.mark.parametrize("failure", ["show-fails", "disable-fails"])
def test_stop_workers_fails_closed_when_systemd_cannot_answer(tmp_path: Path, failure: str) -> None:
    node = Node(tmp_path)
    (node.state / failure).write_text("")

    result = node.run("node_takeover.sh", "stop-workers")

    assert result.returncode != 0
    assert "every worker timer and service is stopped" not in result.stdout


def test_start_workers_refuses_a_worker_that_did_not_start(tmp_path: Path) -> None:
    node = Node(tmp_path, "publisher|200")
    (node.state / "force-tr-clickhouse-ingest.service").write_text("failed")

    result = node.run("node_takeover.sh", "start-workers")

    assert result.returncode != 0
    assert "tr-clickhouse-ingest.service failed" in result.stderr


@pytest.mark.parametrize(("running", "ok"), [("0", True), ("2", False)])
def test_drain_server_work_kills_worker_queries_but_not_itself(
    tmp_path: Path, running: str, ok: bool
) -> None:
    node = Node(tmp_path)
    (node.state / "processes").write_text(running + "\n")

    result = node.run("node_takeover.sh", "drain-server-work")

    assert (result.returncode == 0) is ok, result.stderr
    queries = [call[-1] for call in node.calls("clickhouse-client")]
    assert queries[0] == "KILL QUERY WHERE user = 'tr' AND query_id != queryID() SYNC"


def test_the_sync_barrier_covers_every_replicated_table(tmp_path: Path) -> None:
    node = Node(tmp_path)
    (node.state / "tables").write_text("activity_generations\nprovider_analytics_hourly\n")

    result = node.run("node_takeover.sh", "sync-barrier")

    assert result.returncode == 0, result.stderr
    syncs = [call[-1] for call in node.calls("clickhouse-client") if "SYNC REPLICA" in call[-1]]
    assert syncs == [
        "SYSTEM SYNC REPLICA tr.`activity_generations` LIGHTWEIGHT",
        "SYSTEM SYNC REPLICA tr.`provider_analytics_hourly` LIGHTWEIGHT",
    ]


def test_the_sync_barrier_stops_when_a_table_cannot_catch_up(tmp_path: Path) -> None:
    # A part held only by the lost publisher must not be published over:
    # recomputing from incomplete raw tables shrinks aggregates.
    node = Node(tmp_path)
    (node.state / "tables").write_text("activity_generations\nprovider_analytics_hourly\n")
    (node.state / "unsynced").write_text("activity_generations\n")

    result = node.run("node_takeover.sh", "sync-barrier")

    assert result.returncode != 0
    assert "tr.activity_generations did not finish" in result.stderr
    assert "No active replica has part" in result.stderr
    assert "recover it (or its disk)" in result.stderr


def test_the_sync_barrier_refuses_a_node_with_no_replicated_tables(tmp_path: Path) -> None:
    node = Node(tmp_path)

    result = node.run("node_takeover.sh", "sync-barrier")

    assert result.returncode != 0
    assert "no replicated tables" in result.stderr


def test_start_workers_requires_the_publisher_role(tmp_path: Path) -> None:
    node = Node(tmp_path, "standby|200")

    result = node.run("node_takeover.sh", "start-workers")

    assert result.returncode != 0
    assert "set its tr-clickhouse-role to publisher first" in result.stderr
    assert node.calls("systemctl") == []


def test_start_workers_starts_the_drains_and_timers(tmp_path: Path) -> None:
    node = Node(tmp_path, "publisher|200")

    result = node.run("node_takeover.sh", "start-workers")

    assert result.returncode == 0, result.stderr
    enable = next(call for call in node.calls("systemctl") if call[:2] == ["enable", "--now"])
    assert "tr-clickhouse-ingest.service" in enable and "tr-clickhouse-operational-ingest.service" in enable
    assert len([unit for unit in enable if unit.endswith(".timer")]) == 10


# --- the operator wrapper, with a stateful fake gcloud ---

FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
# The archive arrives on gcloud's stdin; python3 below reads its program from
# the heredoc, so capture the archive first.
stdin_file="$FAKE_DIR/stdin.$$"
: > "$stdin_file"
if [ "${1:-}" = "compute" ] && [ "${2:-}" = "ssh" ]; then
  cat > "$stdin_file"
fi
python3 - "$FAKE_DIR" "$stdin_file" "$@" <<'PY'
import json, sys, pathlib
root = pathlib.Path(sys.argv[1]); stdin_path = pathlib.Path(sys.argv[2]); argv = sys.argv[3:]
state_file = root / "instances.json"
state = json.loads(state_file.read_text())
log = root / "gcloud.jsonl"
stdin_text = stdin_path.read_bytes().hex() if argv[:2] == ["compute", "ssh"] else ""
with log.open("a") as handle:
    handle.write(json.dumps({"argv": argv, "stdin": stdin_text}) + "\n")
name = argv[3] if len(argv) > 3 else ""
if argv[:3] == ["compute", "instances", "describe"]:
    node = state[name]
    fmt = next(arg for arg in argv if arg.startswith("--format="))
    if fmt == "--format=json" and node.get("describe_fails"):
        print("ERROR: (gcloud.compute.instances.describe) transient failure", file=sys.stderr)
        sys.exit(1)
    if "natIP" in fmt:
        print(node.get("nat", ""))
    elif "status" in fmt:
        print(node["status"])
    else:
        items = [{"key": "tr-clickhouse-role", "value": node["role"]}] if node.get("role") else []
        print(json.dumps({"metadata": {"items": items}, "status": node["status"]}))
elif argv[:3] == ["compute", "instances", "add-metadata"]:
    value = next(arg for arg in argv if arg.startswith("--metadata=")).split("=", 2)[2]
    if state.get("slow_add_metadata"):
        (root / "add-metadata-started").write_text(name)
        import time
        if state["slow_add_metadata"] == "until_released":
            # Block until the test releases the write, so a signal it sends
            # meanwhile always lands while the write is in flight. A release
            # that never comes fails the write; it never completes it.
            release = root / "add-metadata-release"
            deadline = time.monotonic() + 600
            while not release.exists():
                if time.monotonic() > deadline:
                    sys.exit("fake gcloud: add-metadata was never released")
                time.sleep(0.02)
        else:
            time.sleep(float(state["slow_add_metadata"]))
        state = json.loads(state_file.read_text())
    state[name]["role"] = value
    state_file.write_text(json.dumps(state))
elif argv[:3] == ["compute", "instances", "stop"]:
    if not state[name].get("stop_fails"):
        state[name]["status"] = "TERMINATED"
        state_file.write_text(json.dumps(state))
elif argv[:2] == ["storage", "cp"]:
    if "--if-generation-match=0" in argv and state.get("lock"):
        print("412 precondition failed: the lock object exists", file=sys.stderr)
        sys.exit(1)
    generation = state.get("lock_generation", 0) + 1
    state["lock"] = {"content": pathlib.Path(argv[2]).read_text(), "generation": generation}
    state["lock_generation"] = generation
    state_file.write_text(json.dumps(state))
elif argv[:3] == ["storage", "objects", "describe"]:
    if state.get("lock_describe_fails"):
        print("ERROR: transient", file=sys.stderr)
        sys.exit(1)
    print(state["lock"]["generation"])
elif argv[:2] == ["storage", "cat"]:
    print(state["lock"]["content"] if state.get("lock") else "")
elif argv[:2] == ["storage", "rm"]:
    match = next((arg for arg in argv if arg.startswith("--if-generation-match=")), "=").split("=", 1)[1]
    if not state.get("lock") or (match and str(state["lock"]["generation"]) != match):
        sys.exit(1)
    state["lock"] = None
    state_file.write_text(json.dumps(state))
elif argv[:2] == ["compute", "ssh"]:
    name = argv[2]
    command = next(arg for arg in argv if arg.startswith("--command="))
    fail = state.get("fail_step")
    if fail and fail in command:
        sys.exit(1)
PY
"""


def _wrapper(
    tmp_path: Path, *args: str, roles: dict[str, str] | None = None, **extra: object
) -> tuple[subprocess.CompletedProcess[str], list[dict], dict]:
    repo = tmp_path / "repo"
    (repo / "scripts/deploy").mkdir(parents=True)
    shutil.copy(WRAPPER, repo / "scripts/deploy/clickhouse_worker_role.sh")
    for helper in ("_clickhouse_bundle.sh", "_clickhouse_publisher.sh"):
        shutil.copy(ROOT / "scripts/deploy" / helper, repo / "scripts/deploy" / helper)
    shutil.copytree(ROLE_DIR, repo / "scripts/deploy/clickhouse-worker-role")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q"], check=True)  # noqa: S603
    subprocess.run([*git, "add", "-A"], check=True)  # noqa: S603
    subprocess.run([*git, "commit", "-q", "-m", "fixture"], check=True)  # noqa: S603
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "gcloud").write_text(FAKE_GCLOUD)
    (fake / "gcloud").chmod(0o755)
    state = {
        name: {"role": (roles or {}).get(name, ""), "status": "RUNNING"}
        for name in ("tr-clickhouse-1", "tr-clickhouse-2", "tr-clickhouse-3")
    }
    for key, value in extra.items():
        if key == "fail_step":
            state["fail_step"] = value  # type: ignore[assignment]
        elif key == "stop_fails":
            state["tr-clickhouse-1"]["stop_fails"] = value
        elif key == "describe_fails":
            state[str(value)]["describe_fails"] = True
        elif key == "lock_describe_fails":
            state["lock_describe_fails"] = True  # type: ignore[assignment]
        elif key == "slow_add_metadata":
            state["slow_add_metadata"] = value  # type: ignore[assignment]
        elif key == "lock_held":
            state["lock"] = {"content": '{"owner":"someone@else"}', "generation": 7}  # type: ignore[assignment]
    (fake / "instances.json").write_text(json.dumps(state))
    (fake / "gcloud.jsonl").write_text("")
    result = subprocess.run(  # noqa: S603 - fixed script under test
        [BASH, str(repo / "scripts/deploy/clickhouse_worker_role.sh"), *args],
        env={"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(tmp_path), "TMPDIR": str(tmp_path), "FAKE_DIR": str(fake)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    calls = [json.loads(line) for line in (fake / "gcloud.jsonl").read_text().splitlines()]
    return result, calls, json.loads((fake / "instances.json").read_text())


def _steps(calls: list[dict]) -> list[str]:
    steps = []
    for call in calls:
        argv = call["argv"]
        if argv[:3] == ["compute", "instances", "add-metadata"]:
            steps.append(f"{argv[3]}={argv[-1].split('=', 2)[2]}")
        elif argv[:3] == ["compute", "instances", "stop"]:
            steps.append(f"stop {argv[3]}")
        elif argv[:2] == ["compute", "ssh"]:
            command = next(arg for arg in argv if arg.startswith("--command="))
            script = command.split("clickhouse-worker-role/", 1)[1].split("'", 1)[0]
            steps.append(f"{argv[2]}: {script.strip()}")
    return steps


def test_first_fence_makes_node_one_the_publisher_and_fences_it_last(tmp_path: Path) -> None:
    result, calls, state = _wrapper(tmp_path, "fence", "--apply")

    assert result.returncode == 0, result.stderr
    assert _steps(calls) == [
        "tr-clickhouse-1=publisher",
        "tr-clickhouse-2=standby",
        "tr-clickhouse-3=standby",
        'tr-clickhouse-3: install_fence.sh" standby',
        'tr-clickhouse-2: install_fence.sh" standby',
        'tr-clickhouse-1: install_fence.sh" publisher',
    ]
    assert {name: node["role"] for name, node in state.items() if name.startswith("tr-clickhouse-")} == {
        "tr-clickhouse-1": "publisher",
        "tr-clickhouse-2": "standby",
        "tr-clickhouse-3": "standby",
    }
    ssh = [call for call in calls if call["argv"][:2] == ["compute", "ssh"]]
    with tarfile.open(fileobj=__import__("io").BytesIO(bytes.fromhex(ssh[0]["stdin"]))) as bundle:
        assert "scripts/deploy/clickhouse-worker-role/role-check" in bundle.getnames()


def test_takeover_runs_the_four_fenced_steps_in_order(tmp_path: Path) -> None:
    roles = {"tr-clickhouse-1": "publisher", "tr-clickhouse-2": "standby", "tr-clickhouse-3": "standby"}

    result, calls, state = _wrapper(tmp_path, "takeover", "--to", "tr-clickhouse-2", "--apply", roles=roles)

    assert result.returncode == 0, result.stderr
    assert _steps(calls) == [
        "tr-clickhouse-1=standby",
        'tr-clickhouse-1: node_takeover.sh" stop-workers',
        'tr-clickhouse-1: node_takeover.sh" drain-server-work',
        'tr-clickhouse-2: node_takeover.sh" drain-server-work',
        'tr-clickhouse-3: node_takeover.sh" drain-server-work',
        'tr-clickhouse-2: node_takeover.sh" sync-barrier',
        "tr-clickhouse-2=publisher",
        'tr-clickhouse-2: node_takeover.sh" start-workers',
    ]
    assert state["tr-clickhouse-2"]["role"] == "publisher"
    assert state["tr-clickhouse-1"]["role"] == "standby"


def test_takeover_from_an_unreachable_node_power_fences_it(tmp_path: Path) -> None:
    roles = {"tr-clickhouse-1": "publisher", "tr-clickhouse-2": "standby", "tr-clickhouse-3": "standby"}

    result, calls, state = _wrapper(
        tmp_path, "takeover", "--to", "tr-clickhouse-2", "--from-unreachable", "--apply", roles=roles
    )

    assert result.returncode == 0, result.stderr
    assert _steps(calls) == [
        "tr-clickhouse-1=standby",
        "stop tr-clickhouse-1",
        'tr-clickhouse-2: node_takeover.sh" drain-server-work',
        'tr-clickhouse-3: node_takeover.sh" drain-server-work',
        'tr-clickhouse-2: node_takeover.sh" sync-barrier',
        "tr-clickhouse-2=publisher",
        'tr-clickhouse-2: node_takeover.sh" start-workers',
    ]
    assert state["tr-clickhouse-1"]["status"] == "TERMINATED"


def test_takeover_stops_if_the_old_node_does_not_terminate(tmp_path: Path) -> None:
    roles = {"tr-clickhouse-1": "publisher", "tr-clickhouse-2": "standby", "tr-clickhouse-3": "standby"}

    result, calls, state = _wrapper(
        tmp_path, "takeover", "--to", "tr-clickhouse-2", "--from-unreachable", "--apply",
        roles=roles, stop_fails=True,
    )

    assert result.returncode != 0
    assert "not TERMINATED" in result.stderr
    assert state["tr-clickhouse-2"]["role"] == "standby"


def test_a_failed_barrier_never_makes_the_new_node_publish(tmp_path: Path) -> None:
    roles = {"tr-clickhouse-1": "publisher", "tr-clickhouse-2": "standby", "tr-clickhouse-3": "standby"}

    result, calls, state = _wrapper(
        tmp_path, "takeover", "--to", "tr-clickhouse-2", "--apply", roles=roles, fail_step="sync-barrier"
    )

    assert result.returncode != 0
    assert state["tr-clickhouse-2"]["role"] == "standby"
    assert not any("start-workers" in step for step in _steps(calls))


def test_without_apply_nothing_changes(tmp_path: Path) -> None:
    roles = {"tr-clickhouse-1": "publisher", "tr-clickhouse-2": "standby", "tr-clickhouse-3": "standby"}

    result, calls, state = _wrapper(tmp_path, "takeover", "--to", "tr-clickhouse-2", roles=roles)

    assert result.returncode == 0, result.stderr
    assert _steps(calls) == []
    assert "[dry-run] set tr-clickhouse-1 tr-clickhouse-role=standby" in result.stdout
    assert state["tr-clickhouse-1"]["role"] == "publisher"


def test_two_publishers_are_refused(tmp_path: Path) -> None:
    roles = {"tr-clickhouse-1": "publisher", "tr-clickhouse-2": "publisher"}

    result, calls, _ = _wrapper(tmp_path, "fence", "--apply", roles=roles)

    assert result.returncode != 0
    assert "more than one node is the publisher" in result.stderr
    assert _steps(calls) == []


# --- the standby install (G1 item 1) ---


def _standby_node(tmp_path: Path, metadata: str = "standby|200", fenced: bool = True) -> Node:
    node = Node(tmp_path, metadata)
    app = node.root / "opt/tr-clickhouse"
    (app / "clickhouse").mkdir(parents=True)
    for unit in SERVICES:
        (app / f"clickhouse/{unit}.service").write_text(f"[Unit]\nDescription={unit}\n")
    for unit in SERVICES[2:]:
        (app / f"clickhouse/{unit}.timer").write_text(f"[Timer]\nUnit={unit}.service\n")
    (app / "clickhouse/requirements-live.txt").write_text("")
    (app / "clickhouse/002_provider_analytics_rollups.sql").write_text("SELECT 1;\n")
    (app / "venv/bin").mkdir(parents=True)
    for tool in ("python", "pip"):
        script = app / f"venv/bin/{tool}"
        script.write_text(
            '#!/usr/bin/env bash\npython3 - "$STUB_LOG" "' + tool + '" "$@" <<\'PY\'\n'
            "import json, sys\nlog, name, *argv = sys.argv[1:]\n"
            "open(log, 'a').write(json.dumps([name, *argv]) + '\\n')\nPY\n"
        )
        script.chmod(0o755)
    for name in ("useradd", "apt-get", "id"):
        (node.bin / name).write_text(NODE_STUB)
        (node.bin / name).chmod(0o755)
    if fenced:
        drop_in = node.root / "etc/systemd/system/tr-clickhouse-ingest.service.d"
        drop_in.mkdir(parents=True)
        (drop_in / "tr-clickhouse-role.conf").write_text("[Service]\n")
    return node


def test_standby_install_puts_every_unit_in_place_disabled(tmp_path: Path) -> None:
    node = _standby_node(tmp_path)

    result = node.run("install_standby.sh")

    assert result.returncode == 0, result.stderr
    units = node.root / "etc/systemd/system"
    assert {path.name for path in units.glob("*.service")} == {f"{unit}.service" for unit in SERVICES}
    assert len(list(units.glob("*.timer"))) == 10
    systemctl = node.calls("systemctl")
    assert ["daemon-reload"] in systemctl
    assert not any(call[:1] in (["enable"], ["start"], ["restart"]) for call in systemctl)
    # The staging tables are created behind the replication guard.
    python = node.calls("python")
    assert python and python[0][:2] == ["-m", "clickhouse.require_replicated_tables"]
    assert "workers installed, disabled and fenced" in result.stdout


# An absent key is not refused here: the fence already skips every unit on such
# a node. The wrapper is what requires an explicit standby role (below).
@pytest.mark.parametrize(
    ("metadata", "fenced", "message"),
    [
        ("publisher|200", True, "not 1 (standby)"),
        ("standby|200", False, "has no role fence drop-ins"),
    ],
    ids=["publisher", "unfenced"],
)
def test_standby_install_refuses_an_unfenced_or_publishing_node(
    tmp_path: Path, metadata: str, fenced: bool, message: str
) -> None:
    node = _standby_node(tmp_path, metadata, fenced=fenced)

    result = node.run("install_standby.sh")

    assert result.returncode != 0
    assert message in result.stderr
    assert not list((node.root / "etc/systemd/system").glob("*.service"))


def test_standby_command_requires_a_standby_role(tmp_path: Path) -> None:
    # tr-clickhouse-3 has no role yet: the fence has not run.
    result, calls, _ = _wrapper(
        tmp_path, "standby", "--node", "tr-clickhouse-3", "--apply", roles={"tr-clickhouse-1": "publisher"}
    )

    assert result.returncode != 0
    assert "not standby" in result.stderr
    assert _steps(calls) == []


def test_standby_command_ships_the_bundle_then_installs(tmp_path: Path) -> None:
    roles = {"tr-clickhouse-1": "publisher", "tr-clickhouse-2": "standby", "tr-clickhouse-3": "standby"}

    result, calls, _ = _wrapper(tmp_path, "standby", "--node", "tr-clickhouse-2", roles=roles)

    # Report-only: nothing is shipped or run.
    assert result.returncode == 0, result.stderr
    assert "[dry-run] ship the worker bundle to tr-clickhouse-2:/opt/tr-clickhouse" in result.stdout
    assert "[dry-run] on tr-clickhouse-2: install_standby.sh" in result.stdout
    assert _steps(calls) == []


# --- the installers start workers on the publisher only ---


def test_live_ingestion_refuses_a_node_that_is_not_the_publisher(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts/deploy").mkdir(parents=True)
    for name in ("clickhouse_live_ingestion.sh", "_clickhouse_bundle.sh", "_clickhouse_publisher.sh"):
        shutil.copy(ROOT / "scripts/deploy" / name, repo / "scripts/deploy" / name)
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "gcloud").write_text(FAKE_GCLOUD)
    (fake / "gcloud").chmod(0o755)
    state = {
        "tr-clickhouse-1": {"role": "publisher", "status": "RUNNING"},
        "tr-clickhouse-2": {"role": "standby", "status": "RUNNING"},
        "tr-clickhouse-3": {"role": "standby", "status": "RUNNING"},
    }
    (fake / "instances.json").write_text(json.dumps(state))
    (fake / "gcloud.jsonl").write_text("")

    result = subprocess.run(  # noqa: S603 - fixed script under test
        [BASH, str(repo / "scripts/deploy/clickhouse_live_ingestion.sh")],
        env={
            "PATH": f"{fake}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "FAKE_DIR": str(fake),
            "NAME": "tr-clickhouse-2",
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode != 0
    assert "tr-clickhouse-2 is not the ClickHouse publisher (tr-clickhouse-1)" in result.stderr
    assert "clickhouse_worker_role.sh standby --node tr-clickhouse-2" in result.stderr
    calls = [json.loads(line) for line in (fake / "gcloud.jsonl").read_text().splitlines()]
    assert not any(call["argv"][:2] == ["compute", "ssh"] for call in calls)


def test_the_operational_installer_targets_the_publisher_not_node_one() -> None:
    script = (ROOT / "scripts/deploy/clickhouse_operational_analytics.sh").read_text()
    assert "node_ssh 0 " not in script
    assert 'publisher="$(clickhouse_publisher)"' in script
    assert script.count('node_ssh "$WORKER"') >= 10


# --- review round 1: locking, unreadable roles, resuming, reconciling ---


def _metadata_writes(calls: list[dict]) -> list[list[str]]:
    return [call["argv"] for call in calls if call["argv"][:3] == ["compute", "instances", "add-metadata"]]


def test_a_held_lock_refuses_every_change(tmp_path: Path) -> None:
    roles = {"tr-clickhouse-1": "publisher", "tr-clickhouse-2": "standby", "tr-clickhouse-3": "standby"}

    result, calls, state = _wrapper(
        tmp_path, "takeover", "--to", "tr-clickhouse-2", "--apply", roles=roles, lock_held=True
    )

    assert result.returncode != 0
    assert "another role change holds" in result.stderr
    assert "someone@else" in result.stderr
    assert _metadata_writes(calls) == [] and _steps(calls) == []
    assert state["lock"]["generation"] == 7  # someone else's lock is left alone


def test_apply_holds_the_lock_for_the_whole_run(tmp_path: Path) -> None:
    roles = {"tr-clickhouse-1": "publisher", "tr-clickhouse-2": "standby", "tr-clickhouse-3": "standby"}

    result, calls, state = _wrapper(tmp_path, "takeover", "--to", "tr-clickhouse-2", "--apply", roles=roles)

    assert result.returncode == 0, result.stderr
    argvs = [call["argv"] for call in calls]
    take = next(i for i, argv in enumerate(argvs) if argv[:2] == ["storage", "cp"])
    first_write = next(i for i, argv in enumerate(argvs) if argv[:3] == ["compute", "instances", "add-metadata"])
    release = next(i for i, argv in enumerate(argvs) if argv[:2] == ["storage", "rm"])
    assert "--if-generation-match=0" in argvs[take]
    assert take < first_write < release
    assert state["lock"] is None


def test_a_dry_run_takes_no_lock(tmp_path: Path) -> None:
    roles = {"tr-clickhouse-1": "publisher", "tr-clickhouse-2": "standby", "tr-clickhouse-3": "standby"}

    result, calls, _ = _wrapper(tmp_path, "takeover", "--to", "tr-clickhouse-2", roles=roles)

    assert result.returncode == 0, result.stderr
    assert not any(call["argv"][0] == "storage" for call in calls)


@pytest.mark.parametrize("command", [("fence",), ("takeover", "--to", "tr-clickhouse-3")])
def test_an_unreadable_role_stops_before_any_change(tmp_path: Path, command: tuple[str, ...]) -> None:
    # Node 2 publishes; a failed read of its metadata must not look like "no
    # publisher" and promote node 1 beside it.
    roles = {"tr-clickhouse-2": "publisher", "tr-clickhouse-3": "standby", "tr-clickhouse-1": "standby"}

    result, calls, state = _wrapper(
        tmp_path, *command, "--apply", roles=roles, describe_fails="tr-clickhouse-2"
    )

    assert result.returncode != 0
    assert "cannot read tr-clickhouse-2's metadata" in result.stderr
    assert _metadata_writes(calls) == [] and _steps(calls) == []
    assert state["tr-clickhouse-2"]["role"] == "publisher"


def test_fence_refuses_a_takeover_that_stopped_part_way(tmp_path: Path) -> None:
    roles = {"tr-clickhouse-1": "standby", "tr-clickhouse-2": "standby"}

    result, calls, _ = _wrapper(tmp_path, "fence", "--apply", roles=roles)

    assert result.returncode != 0
    assert "a takeover stopped part-way" in result.stderr
    assert "--from OLD_PUBLISHER" in result.stderr
    assert _metadata_writes(calls) == []


def test_a_takeover_with_no_publisher_needs_from(tmp_path: Path) -> None:
    roles = {name: "standby" for name in ("tr-clickhouse-1", "tr-clickhouse-2", "tr-clickhouse-3")}

    result, calls, _ = _wrapper(tmp_path, "takeover", "--to", "tr-clickhouse-2", "--apply", roles=roles)

    assert result.returncode != 0
    assert "takeover --to tr-clickhouse-2 --from OLD_PUBLISHER --apply" in result.stderr
    assert _metadata_writes(calls) == []


def test_a_stopped_takeover_resumes_with_from(tmp_path: Path) -> None:
    # The first attempt fenced node 1, then its barrier failed.
    roles = {name: "standby" for name in ("tr-clickhouse-1", "tr-clickhouse-2", "tr-clickhouse-3")}

    result, calls, state = _wrapper(
        tmp_path, "takeover", "--to", "tr-clickhouse-2", "--from", "tr-clickhouse-1", "--apply", roles=roles
    )

    assert result.returncode == 0, result.stderr
    steps = _steps(calls)
    assert 'tr-clickhouse-1: node_takeover.sh" stop-workers' in steps
    assert steps.index('tr-clickhouse-2: node_takeover.sh" sync-barrier') < steps.index("tr-clickhouse-2=publisher")
    assert steps[-1] == 'tr-clickhouse-2: node_takeover.sh" start-workers'
    assert state["tr-clickhouse-2"]["role"] == "publisher"
    assert state["tr-clickhouse-1"]["role"] == "standby"


def test_from_must_name_a_fenced_node(tmp_path: Path) -> None:
    roles = {"tr-clickhouse-1": "standby", "tr-clickhouse-2": "standby"}  # node 3: no role

    result, calls, _ = _wrapper(
        tmp_path, "takeover", "--to", "tr-clickhouse-2", "--from", "tr-clickhouse-3", "--apply", roles=roles
    )

    assert result.returncode != 0
    assert "tr-clickhouse-3's role is '', not standby" in result.stderr
    assert _metadata_writes(calls) == []


def test_from_must_match_a_live_publisher(tmp_path: Path) -> None:
    roles = {"tr-clickhouse-1": "publisher", "tr-clickhouse-2": "standby", "tr-clickhouse-3": "standby"}

    result, calls, _ = _wrapper(
        tmp_path, "takeover", "--to", "tr-clickhouse-2", "--from", "tr-clickhouse-3", "--apply", roles=roles
    )

    assert result.returncode != 0
    assert "the publisher is tr-clickhouse-1, not tr-clickhouse-3" in result.stderr
    assert _metadata_writes(calls) == []


def test_a_retry_after_promotion_starts_and_verifies_the_workers(tmp_path: Path) -> None:
    # The first attempt set node 2's role, then its start-workers call failed.
    roles = {"tr-clickhouse-1": "standby", "tr-clickhouse-2": "publisher", "tr-clickhouse-3": "standby"}

    result, calls, _ = _wrapper(tmp_path, "takeover", "--to", "tr-clickhouse-2", "--apply", roles=roles)

    assert result.returncode == 0, result.stderr
    assert _steps(calls) == ['tr-clickhouse-2: node_takeover.sh" start-workers']
    assert "its workers are running" in result.stdout


def test_a_failed_start_after_promotion_is_not_reported_as_success(tmp_path: Path) -> None:
    roles = {"tr-clickhouse-1": "standby", "tr-clickhouse-2": "publisher", "tr-clickhouse-3": "standby"}

    result, _, _ = _wrapper(
        tmp_path, "takeover", "--to", "tr-clickhouse-2", "--apply", roles=roles, fail_step="start-workers"
    )

    assert result.returncode != 0
    assert "its workers are running" not in result.stdout


def _publisher_lookup(tmp_path: Path, roles: dict[str, str], **failures: str) -> subprocess.CompletedProcess[str]:
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "gcloud").write_text(FAKE_GCLOUD)
    (fake / "gcloud").chmod(0o755)
    state = {
        name: {"role": roles.get(name, ""), "status": "RUNNING"}
        for name in ("tr-clickhouse-1", "tr-clickhouse-2", "tr-clickhouse-3")
    }
    if failures.get("describe_fails"):
        state[failures["describe_fails"]]["describe_fails"] = True
    (fake / "instances.json").write_text(json.dumps(state))
    (fake / "gcloud.jsonl").write_text("")
    helper = ROOT / "scripts/deploy/_clickhouse_publisher.sh"
    return subprocess.run(  # noqa: S603 - fixed helper under test
        [BASH, "-c", f'set -euo pipefail; source "{helper}"; clickhouse_publisher'],
        env={"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(tmp_path), "FAKE_DIR": str(fake)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize(
    ("roles", "failure", "answer", "message"),
    [
        ({}, "", "tr-clickhouse-1", ""),
        ({"tr-clickhouse-3": "publisher", "tr-clickhouse-1": "standby"}, "", "tr-clickhouse-3", ""),
        ({"tr-clickhouse-1": "standby", "tr-clickhouse-2": "standby"}, "", "", "finish the takeover"),
        ({"tr-clickhouse-1": "publisher", "tr-clickhouse-2": "publisher"}, "", "", "more than one"),
        ({"tr-clickhouse-2": "publisher"}, "tr-clickhouse-2", "", "cannot read tr-clickhouse-2's metadata"),
    ],
    ids=["legacy", "publisher", "part-way", "two", "unreadable"],
)
def test_the_installers_publisher_lookup_fails_closed(
    tmp_path: Path, roles: dict[str, str], failure: str, answer: str, message: str
) -> None:
    result = _publisher_lookup(tmp_path, roles, describe_fails=failure)

    if answer:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == answer
    else:
        assert result.returncode != 0
        assert message in result.stderr


# --- review round 2: interruption, installers under the lock, resumed retries ---


@pytest.mark.parametrize("signal_name", ["SIGTERM", "SIGHUP"])
def test_an_interrupted_run_finishes_its_write_and_keeps_the_lock(tmp_path: Path, signal_name: str) -> None:
    import contextlib
    import signal
    import time

    roles = {"tr-clickhouse-1": "publisher", "tr-clickhouse-2": "standby", "tr-clickhouse-3": "standby"}
    repo = tmp_path / "repo"
    (repo / "scripts/deploy").mkdir(parents=True)
    for name in ("clickhouse_worker_role.sh", "_clickhouse_bundle.sh", "_clickhouse_publisher.sh"):
        shutil.copy(ROOT / "scripts/deploy" / name, repo / "scripts/deploy" / name)
    shutil.copytree(ROLE_DIR, repo / "scripts/deploy/clickhouse-worker-role")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q"], check=True)  # noqa: S603
    subprocess.run([*git, "add", "-A"], check=True)  # noqa: S603
    subprocess.run([*git, "commit", "-q", "-m", "fixture"], check=True)  # noqa: S603
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "gcloud").write_text(FAKE_GCLOUD)
    (fake / "gcloud").chmod(0o755)
    state = {name: {"role": roles[name], "status": "RUNNING"} for name in roles}
    # Held until released below: a fixed sleep let a loaded machine finish the
    # whole takeover before the signal was sent.
    state["slow_add_metadata"] = "until_released"  # type: ignore[assignment]
    (fake / "instances.json").write_text(json.dumps(state))
    (fake / "gcloud.jsonl").write_text("")
    process = subprocess.Popen(  # noqa: S603 - fixed script under test
        [BASH, str(repo / "scripts/deploy/clickhouse_worker_role.sh"), "takeover", "--to", "tr-clickhouse-2", "--apply"],
        env={"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(tmp_path), "TMPDIR": str(tmp_path), "FAKE_DIR": str(fake)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,  # its own process group, so cleanup reaches the children
    )
    release = fake / "add-metadata-release"
    try:
        started = fake / "add-metadata-started"
        deadline = time.monotonic() + 60
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert started.exists()
        process.send_signal(getattr(signal, signal_name))  # while the first role write is in flight
        release.write_text("")
        _, stderr = process.communicate(timeout=60)
    finally:
        # On any failure above, release the held write and end the run with
        # its children, so nothing outlives the test.
        release.write_text("")
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()

    final = json.loads((fake / "instances.json").read_text())
    assert process.returncode != 0
    # The write that was running finished, and nothing after it ran.
    assert final["tr-clickhouse-1"]["role"] == "standby"
    assert final["tr-clickhouse-2"]["role"] == "standby"
    calls = [json.loads(line) for line in (fake / "gcloud.jsonl").read_text().splitlines()]
    assert not any(call["argv"][:2] == ["compute", "ssh"] for call in calls)
    # The lock stays for an operator to check, with the command to remove it.
    assert final["lock"] is not None
    assert "stays held" in stderr


def _installer_run(tmp_path: Path, roles: dict[str, str], *, lock_held: bool) -> tuple[subprocess.CompletedProcess[str], dict, list[dict]]:
    repo = tmp_path / "repo"
    (repo / "scripts/deploy").mkdir(parents=True)
    for name in ("clickhouse_live_ingestion.sh", "_clickhouse_bundle.sh", "_clickhouse_publisher.sh"):
        shutil.copy(ROOT / "scripts/deploy" / name, repo / "scripts/deploy" / name)
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "gcloud").write_text(FAKE_GCLOUD)
    (fake / "gcloud").chmod(0o755)
    state: dict = {name: {"role": roles.get(name, ""), "status": "RUNNING"} for name in ("tr-clickhouse-1", "tr-clickhouse-2", "tr-clickhouse-3")}
    if lock_held:
        state["lock"] = {"content": '{"owner":"someone@else"}', "generation": 7}
    (fake / "instances.json").write_text(json.dumps(state))
    (fake / "gcloud.jsonl").write_text("")
    result = subprocess.run(  # noqa: S603 - fixed script under test
        [BASH, str(repo / "scripts/deploy/clickhouse_live_ingestion.sh")],
        env={"PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(tmp_path), "TMPDIR": str(tmp_path), "FAKE_DIR": str(fake), "NAME": "tr-clickhouse-2"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    calls = [json.loads(line) for line in (fake / "gcloud.jsonl").read_text().splitlines()]
    return result, json.loads((fake / "instances.json").read_text()), calls


def test_an_installer_waits_for_no_one_while_a_role_change_holds_the_lock(tmp_path: Path) -> None:
    roles = {"tr-clickhouse-2": "publisher", "tr-clickhouse-1": "standby", "tr-clickhouse-3": "standby"}

    result, state, calls = _installer_run(tmp_path, roles, lock_held=True)

    assert result.returncode != 0
    assert "another role change holds" in result.stderr
    assert not any(call["argv"][:2] == ["compute", "ssh"] for call in calls)
    assert not any(call["argv"][:3] == ["compute", "instances", "describe"] for call in calls)
    assert state["lock"]["generation"] == 7


def test_an_installer_takes_and_releases_the_lock(tmp_path: Path) -> None:
    roles = {"tr-clickhouse-1": "publisher", "tr-clickhouse-2": "standby", "tr-clickhouse-3": "standby"}

    # NAME=tr-clickhouse-2 is not the publisher, so the installer refuses
    # after taking the lock; its EXIT trap must release it.
    result, state, calls = _installer_run(tmp_path, roles, lock_held=False)

    assert result.returncode != 0
    assert "is not the ClickHouse publisher" in result.stderr
    argvs = [call["argv"] for call in calls]
    take = next(i for i, argv in enumerate(argvs) if argv[:2] == ["storage", "cp"])
    first_role_read = next(i for i, argv in enumerate(argvs) if argv[:3] == ["compute", "instances", "describe"])
    assert take < first_role_read
    assert state["lock"] is None


def test_a_resumed_takeover_retried_after_promotion_starts_the_workers(tmp_path: Path) -> None:
    # takeover --to 2 --from 1 promoted node 2, then start-workers failed.
    roles = {"tr-clickhouse-1": "standby", "tr-clickhouse-2": "publisher", "tr-clickhouse-3": "standby"}

    result, calls, _ = _wrapper(
        tmp_path, "takeover", "--to", "tr-clickhouse-2", "--from", "tr-clickhouse-1", "--apply", roles=roles
    )

    assert result.returncode == 0, result.stderr
    assert _steps(calls) == ['tr-clickhouse-2: node_takeover.sh" start-workers']



# --- review round 3: the installer's rollup under systemd, armed restart, lock lookup ---


def test_the_installer_runs_its_initial_rollup_through_the_fenced_unit() -> None:
    script = (ROOT / "scripts/deploy/clickhouse_operational_analytics.sh").read_text()
    # A direct python run outlives the SSH session and the role fence; the
    # oneshot unit is fenced and is stopped by a takeover's stop-workers.
    assert "python -m clickhouse.rollup_synthetic" not in script
    assert 'sudo systemctl start tr-clickhouse-synthetic-rollup.service' in script


def test_the_installer_arms_the_ingest_restart_before_stopping_ingest() -> None:
    script = (ROOT / "scripts/deploy/clickhouse_operational_analytics.sh").read_text()
    arm = script.index("ingester_stopped=1")
    stop = script.index("systemctl stop tr-clickhouse-operational-ingest.service")
    assert arm < stop


def test_a_failed_lock_lookup_removes_the_lock_and_refuses(tmp_path: Path) -> None:
    roles = {"tr-clickhouse-1": "publisher", "tr-clickhouse-2": "standby", "tr-clickhouse-3": "standby"}

    result, calls, state = _wrapper(
        tmp_path, "takeover", "--to", "tr-clickhouse-2", "--apply", roles=roles, lock_describe_fails=True
    )

    assert result.returncode != 0
    assert "cannot read the generation of the lock just taken" in result.stderr
    assert state["lock"] is None
    assert _metadata_writes(calls) == [] and _steps(calls) == []
