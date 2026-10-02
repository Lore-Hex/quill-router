"""G2 (/tr_health) and G5 (system-log retention) in
docs/design/clickhouse-high-availability.md, as rolled out by
scripts/deploy/clickhouse_node_config.sh.

The XML was verified on ClickHouse 26.7.1.1315 (the cluster's version) in
Docker on 2026-10-02: the server starts with it, every listed log gets its TTL,
text_log drops to information, /tr_health answers 200 with 17 replicated tables
and 500 with 16 or with one detached, the built-in handlers keep working, and
tr_health is refused outside its networks and cannot read tr data. These tests
pin that content and execute both scripts against stubs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tarfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "scripts/deploy/clickhouse-node-config"
WRAPPER = ROOT / "scripts/deploy/clickhouse_node_config.sh"
DESIGN = ROOT / "docs/design/clickhouse-high-availability.md"
BASH = shutil.which("bash") or "/bin/bash"

# The system-log sections ClickHouse 26.7.1.1315's default config.xml enables
# with no TTL and no custom <engine> (derived by parsing that file from the
# clickhouse/clickhouse-server:26.7.1.1315 image). opentelemetry_span_log is the
# one TTL-less section with a custom engine, so it is deliberately absent.
TTL_LESS_DEFAULT_LOGS = {
    "query_log", "trace_log", "query_thread_log", "query_views_log", "part_log",
    "background_schedule_pool_log", "text_log", "metric_log", "error_log",
    "instrumentation_trace_log", "query_metric_log", "asynchronous_metric_log",
    "iceberg_metadata_log", "delta_lake_metadata_log", "crash_log", "backup_log",
    "s3queue_log",
}


def _xml(name: str) -> ET.Element:
    # The repository's own committed config files, not untrusted input.
    return ET.parse(CONFIG_DIR / name).getroot()  # noqa: S314


def test_every_ttl_less_default_log_gets_a_ttl_and_nothing_else_is_enabled() -> None:
    root = _xml("tr-system-logs.xml")
    sections = {child.tag: child for child in root}
    assert set(sections) == TTL_LESS_DEFAULT_LOGS
    for tag, section in sections.items():
        assert section.find("engine") is None
        days = 7 if tag == "text_log" else 30
        assert section.findtext("ttl") == f"event_date + INTERVAL {days} DAY DELETE", tag
    assert sections["text_log"].findtext("level") == "information"
    # The node script drops renamed copies of exactly these logs and no others.
    script = (CONFIG_DIR / "apply_node_config.sh").read_text()
    listed = re.search(r"MANAGED_LOGS=\(\n(.*?)\n\)", script, re.S)
    assert listed is not None
    assert set(listed[1].split()) == TTL_LESS_DEFAULT_LOGS


def test_health_handler_matches_the_design_and_keeps_the_built_in_handlers() -> None:
    handlers = _xml("tr-health.xml").find("http_handlers")
    assert handlers is not None
    rule = handlers.find("rule")
    assert rule is not None
    assert rule.findtext("url") == "/tr_health"
    assert rule.findtext("methods") == "GET,HEAD"
    assert rule.findtext("handler/type") == "predefined_query_handler"
    query = rule.findtext("handler/query")
    assert query == (
        "SELECT throwIf(countIf(is_readonly OR absolute_delay > 300) > 0 OR count() < 17, "
        "'replica unhealthy') FROM system.replicas WHERE database = 'tr'"
    )
    # Without <defaults/>, / and /ping and /replicas_status stop answering.
    assert handlers.find("defaults") is not None
    # The design document states the same rule and the same table count.
    design = DESIGN.read_text()
    assert "countIf(is_readonly OR absolute_delay > 300) > 0 OR count() < 17" in design


def test_health_user_is_passwordless_read_only_and_network_restricted() -> None:
    root = _xml("tr-health-user.xml")
    user = root.find("users/tr_health")
    assert user is not None
    assert user.find("no_password") is not None
    assert [ip.text for ip in user.findall("networks/ip")] == [
        "35.191.0.0/16",
        "130.211.0.0/22",
        "127.0.0.1",
        "::1",
    ]
    assert [grant.text for grant in user.findall("grants/query")] == [
        "GRANT SELECT ON system.replicas",
        "GRANT SHOW TABLES ON tr.*",
    ]
    profile = root.find(f"profiles/{user.findtext('profile')}")
    assert profile is not None
    assert profile.findtext("readonly") == "1"


# --- the node script, executed against a scratch root and stub commands ---

VOTERS = ("10.0.0.1", "10.0.0.2", "10.0.0.3")
CLUSTER_XML = (
    "<clickhouse>\n  <zookeeper>\n"
    + "".join(f"    <node><host>{ip}</host><port>9181</port></node>\n" for ip in VOTERS)
    + "  </zookeeper>\n</clickhouse>\n"
)


STUB = r"""#!/usr/bin/env bash
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
    # Answer the next queued status code; 200 once the queue is empty.
    code=200
    if [ -s "$state/curl-codes" ]; then
      code="$(head -1 "$state/curl-codes")"
      tail -n +2 "$state/curl-codes" > "$state/curl-codes.next"
      mv "$state/curl-codes.next" "$state/curl-codes"
    fi
    printf '%s' "$code"
    ;;
  clickhouse-client)
    host=local
    previous=""
    for arg in "$@"; do
      [ "$previous" = "--host" ] && host="$arg"
      previous="$arg"
    done
    query="${@: -1}"
    case "$query" in
      *"FROM system.replicas"*) cat "$state/replicas-$host" 2>/dev/null || printf '0\t17\n' ;;
      *"FROM system.tables"*)
        [ -e "$state/fail-system-tables" ] && { echo "Connection refused" >&2; exit 210; }
        cat "$state/system-tables" 2>/dev/null || true
        ;;
    esac
    ;;
  hostname) printf 'tr-clickhouse-test\n' ;;
  keeper-probe)
    host="$1"
    if [ -e "$state/keeper-$host" ]; then
      cat "$state/keeper-$host"
    elif [ "$host" = "10.0.0.1" ]; then
      printf 'zk_server_state\tleader\nzk_synced_followers\t2\n'
    else
      printf 'zk_server_state\tfollower\n'
    fi
    ;;
esac
exit 0
"""


class Node:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "node"
        (self.root / "etc/clickhouse-server/config.d").mkdir(parents=True)
        (self.root / "etc/clickhouse-server/users.d").mkdir(parents=True)
        (self.root / "etc/clickhouse-server/config.d/tr-cluster.xml").write_text(CLUSTER_XML)
        (self.root / "etc/tr-clickhouse-ingest.env").write_text("CH_PASSWORD=fixture-node-credential\n")
        self.bundle = tmp_path / "bundle"
        shutil.copytree(CONFIG_DIR, self.bundle)
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name in ("systemctl", "curl", "clickhouse-client", "hostname", "sleep", "keeper-probe"):
            stub = self.bin / name
            stub.write_text(STUB)
            stub.chmod(0o755)
        self.log = tmp_path / "calls.jsonl"
        self.log.write_text("")

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        self.log.write_text("")
        env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.root),
            "STUB_LOG": str(self.log),
            "STUB_STATE": str(self.state),
            "TR_NODE_ROOT": str(self.root),
            "TR_FILE_OWNER": subprocess.check_output(["id", "-un"], text=True).strip(),  # noqa: S607
            "TR_FILE_GROUP": subprocess.check_output(["id", "-gn"], text=True).strip(),  # noqa: S607
            "TR_KEEPER_PROBE": str(self.bin / "keeper-probe"),
            "TR_WAIT_TRIES": "4",
            "TR_WAIT_SECONDS": "0",
        }
        return subprocess.run(  # noqa: S603 - fixed script under test
            [BASH, str(self.bundle / "apply_node_config.sh"), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def calls(self, name: str) -> list[list[str]]:
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [row[1:] for row in rows if row[0] == name]

    def installed(self) -> dict[str, str]:
        found = {}
        for relative in (
            "etc/clickhouse-server/config.d/tr-system-logs.xml",
            "etc/clickhouse-server/config.d/tr-health.xml",
            "etc/clickhouse-server/users.d/tr-health.xml",
        ):
            path = self.root / relative
            if path.exists():
                found[relative] = path.read_text()
        return found

    def install_current(self, *, users: bool = True, configs: bool = True) -> None:
        if configs:
            for name in ("tr-system-logs.xml", "tr-health.xml"):
                shutil.copy(CONFIG_DIR / name, self.root / "etc/clickhouse-server/config.d" / name)
        if users:
            shutil.copy(
                CONFIG_DIR / "tr-health-user.xml",
                self.root / "etc/clickhouse-server/users.d/tr-health.xml",
            )


@pytest.fixture
def node(tmp_path: Path) -> Node:
    return Node(tmp_path)


def test_report_only_changes_nothing(node: Node) -> None:
    result = node.run()

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("differs") == 3
    assert "--apply would restart clickhouse-server" in result.stdout
    assert node.installed() == {}
    assert node.calls("systemctl") == [] and node.calls("clickhouse-client") == []


def test_apply_installs_restarts_once_and_waits_for_health(node: Node) -> None:
    (node.state / "curl-codes").write_text("000\n503\n200\n")

    result = node.run("--apply")

    assert result.returncode == 0, result.stderr
    assert node.installed() == {
        "etc/clickhouse-server/config.d/tr-system-logs.xml": (CONFIG_DIR / "tr-system-logs.xml").read_text(),
        "etc/clickhouse-server/config.d/tr-health.xml": (CONFIG_DIR / "tr-health.xml").read_text(),
        "etc/clickhouse-server/users.d/tr-health.xml": (CONFIG_DIR / "tr-health-user.xml").read_text(),
    }
    assert node.calls("systemctl") == [["restart", "clickhouse-server"]]
    # It kept polling /tr_health until the 200.
    assert len(node.calls("curl")) == 3
    assert "/tr_health answers 200 and the cluster is whole" in result.stdout
    # Every voter was asked before the restart, not just this node's.
    assert {call[0] for call in node.calls("keeper-probe")} == set(VOTERS)
    # The users.d identity is installed before the restart that needs it.
    installs = [line for line in result.stdout.splitlines() if "installed" in line]
    assert installs[0].endswith("users.d/tr-health.xml")


def test_a_current_node_is_left_alone(node: Node) -> None:
    node.install_current()

    result = node.run("--apply")

    assert result.returncode == 0, result.stderr
    assert "configuration already current" in result.stdout
    assert node.calls("systemctl") == []


def test_a_users_only_change_reloads_without_a_restart(node: Node) -> None:
    node.install_current(users=False)

    result = node.run("--apply")

    assert result.returncode == 0, result.stderr
    assert node.calls("systemctl") == []
    assert "etc/clickhouse-server/users.d/tr-health.xml" in node.installed()
    assert len(node.calls("curl")) >= 1


DEGRADED = [
    (
        ("keeper-10.0.0.3", ""),
        "Keeper voter 10.0.0.3 is not leader or follower (no answer)",
    ),
    (
        ("keeper-10.0.0.1", "zk_server_state\tobserver\n"),
        "Keeper voter 10.0.0.1 is not leader or follower (observer)",
    ),
    (
        ("keeper-10.0.0.1", "zk_server_state\tleader\nzk_synced_followers\t1\n"),
        "Keeper has 1 leader(s) and 1 synced follower(s) of 2",
    ),
    (
        ("replicas-10.0.0.2", "1\t17\n"),
        "a replica on 10.0.0.2 is read-only, more than 300 s behind, or unreachable",
    ),
    (
        # Review finding: countIf() is 0 when tables are missing or detached.
        ("replicas-10.0.0.3", "0\t16\n"),
        "10.0.0.3 sees 16 replicated tables in tr, fewer than 17",
    ),
    (
        ("replicas-10.0.0.2", ""),
        "a replica on 10.0.0.2 is read-only, more than 300 s behind, or unreachable",
    ),
]
DEGRADED_IDS = [
    "another-voter-down",
    "this-voter-observer",
    "leader-missing-a-follower",
    "replica-behind-elsewhere",
    "tables-missing-elsewhere",
    "replicas-unreachable",
]


@pytest.mark.parametrize(("setup", "message"), DEGRADED, ids=DEGRADED_IDS)
def test_a_degraded_cluster_is_never_restarted(
    node: Node, setup: tuple[str, str], message: str
) -> None:
    # Review finding: checking only this node let a survivor restart while
    # another voter was already down, losing quorum.
    (node.state / setup[0]).write_text(setup[1])

    result = node.run("--apply")

    assert result.returncode != 0
    assert message in result.stderr
    assert "the cluster is not whole" in result.stderr
    assert node.calls("systemctl") == []
    assert node.installed() == {}


@pytest.mark.parametrize(("setup", "message"), DEGRADED, ids=DEGRADED_IDS)
def test_an_unchanged_node_still_fails_when_the_cluster_is_not_whole(
    node: Node, setup: tuple[str, str], message: str
) -> None:
    # Review finding: a retry after a failed restart found matching files and
    # reported success, so the wrapper moved on to the next node.
    node.install_current()
    (node.state / setup[0]).write_text(setup[1])

    result = node.run("--apply")

    assert result.returncode != 0
    assert message in result.stderr
    assert node.calls("systemctl") == []


def test_installed_but_not_live_configuration_is_restarted(node: Node) -> None:
    # An earlier run installed the files and stopped before its restart.
    node.install_current()
    (node.state / "curl-codes").write_text("404\n200\n")

    result = node.run("--apply")

    assert result.returncode == 0, result.stderr
    assert "installed but /tr_health does not answer" in result.stdout
    assert node.calls("systemctl") == [["restart", "clickhouse-server"]]


def test_health_that_never_recovers_fails_the_node(node: Node) -> None:
    (node.state / "curl-codes").write_text("500\n500\n500\n500\n500\n")

    result = node.run("--apply")

    assert result.returncode != 0
    assert "/tr_health did not answer 200 or the cluster is not whole" in result.stderr
    assert node.calls("systemctl") == [["restart", "clickhouse-server"]]


def test_a_failed_system_tables_query_fails_the_apply(node: Node) -> None:
    node.install_current()
    (node.state / "fail-system-tables").write_text("")

    result = node.run("--apply")

    assert result.returncode != 0
    assert "could not list system tables" in result.stderr


def test_apply_without_credentials_refuses(node: Node) -> None:
    (node.root / "etc/tr-clickhouse-ingest.env").unlink()

    result = node.run("--apply")

    assert result.returncode != 0
    assert "tr-clickhouse-ingest.env is missing" in result.stderr
    assert node.installed() == {}


SYSTEM_TABLES = "\n".join(
    [
        "query_log",
        "query_log_0",
        "text_log_12",
        "query_log_backup",
        "custom_log_0",
        "opentelemetry_span_log_0",
        "zz_query_log_0",
    ]
)


@pytest.mark.parametrize("drop", [False, True], ids=["report", "drop"])
def test_only_renamed_copies_of_managed_logs_are_dropped(node: Node, drop: bool) -> None:
    node.install_current()
    (node.state / "system-tables").write_text(SYSTEM_TABLES + "\n")

    result = node.run("--apply", *(["--drop-renamed-logs"] if drop else []))

    assert result.returncode == 0, result.stderr
    drops = [call[-1] for call in node.calls("clickhouse-client") if call[-1].startswith("DROP")]
    if drop:
        assert drops == [
            "DROP TABLE system.query_log_0 SYNC",
            "DROP TABLE system.text_log_12 SYNC",
        ]
    else:
        assert drops == []
        assert "system.query_log_0 holds old log data" in result.stdout
        assert "system.text_log_12 holds old log data" in result.stdout
    for kept in ("query_log_backup", "custom_log_0", "opentelemetry_span_log_0", "zz_query_log_0"):
        assert f"system.{kept} " not in result.stdout


# --- the operator wrapper, run from a scratch git checkout ---


FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
stdin_file=""
if [ "${1:-}" = "compute" ] && [ "${2:-}" = "ssh" ]; then
  stdin_file="$(mktemp "$FAKE_DIR/stdin.XXXXXX")"
  cat > "$stdin_file"
fi
python3 - "$FAKE_DIR/gcloud.jsonl" "$stdin_file" "$@" <<'PY'
import json, sys
log, stdin_file, *argv = sys.argv[1:]
with open(log, "a") as handle:
    handle.write(json.dumps({"argv": argv, "stdin_file": stdin_file}) + "\n")
PY
case "$*" in
  "compute instances describe "*)
    for arg in "$@"; do
      if [ "$arg" = "${FAKE_EXTERNAL_IP_NODE:-none}" ]; then printf '34.0.0.9'; fi
    done
    ;;
  "compute ssh "*)
    if [ "$3" = "${FAKE_FAILING_NODE:-none}" ]; then echo "node failed" >&2; exit 1; fi
    ;;
esac
"""


def _checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts/deploy").mkdir(parents=True)
    shutil.copy(WRAPPER, repo / "scripts/deploy/clickhouse_node_config.sh")
    shutil.copytree(CONFIG_DIR, repo / "scripts/deploy/clickhouse-node-config")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q"], check=True)  # noqa: S603
    subprocess.run([*git, "add", "-A"], check=True)  # noqa: S603
    subprocess.run([*git, "commit", "-q", "-m", "fixture"], check=True)  # noqa: S603
    return repo


def _wrapper(tmp_path: Path, *args: str, **env: str) -> tuple[subprocess.CompletedProcess[str], list[dict]]:
    repo = _checkout(tmp_path)
    fake = tmp_path / "fake"
    fake.mkdir()
    gcloud = fake / "gcloud"
    gcloud.write_text(FAKE_GCLOUD)
    gcloud.chmod(0o755)
    (fake / "gcloud.jsonl").write_text("")
    result = subprocess.run(  # noqa: S603 - fixed script under test
        [BASH, str(repo / "scripts/deploy/clickhouse_node_config.sh"), *args],
        env={
            "PATH": f"{fake}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "FAKE_DIR": str(fake),
            **env,
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    calls = [json.loads(line) for line in (fake / "gcloud.jsonl").read_text().splitlines()]
    return result, calls


def _ssh(calls: list[dict]) -> list[dict]:
    return [call for call in calls if call["argv"][:2] == ["compute", "ssh"]]


def test_wrapper_rolls_nodes_three_two_one_and_ships_the_committed_bundle(tmp_path: Path) -> None:
    result, calls = _wrapper(tmp_path)

    assert result.returncode == 0, result.stderr
    ssh = _ssh(calls)
    assert [call["argv"][2] for call in ssh] == ["tr-clickhouse-3", "tr-clickhouse-2", "tr-clickhouse-1"]
    for call in ssh:
        command = next(arg for arg in call["argv"] if arg.startswith("--command="))
        assert command.endswith("apply_node_config.sh\" '")  # report-only: no flags
        with tarfile.open(call["stdin_file"]) as bundle:
            names = set(bundle.getnames())
        assert {
            "scripts/deploy/clickhouse-node-config/apply_node_config.sh",
            "scripts/deploy/clickhouse-node-config/tr-system-logs.xml",
            "scripts/deploy/clickhouse-node-config/tr-health.xml",
            "scripts/deploy/clickhouse-node-config/tr-health-user.xml",
        } <= names
    # Every node is checked for an external IP before any node is touched.
    first_ssh = calls.index(ssh[0])
    assert [call["argv"][3] for call in calls[:first_ssh]] == [
        "tr-clickhouse-3",
        "tr-clickhouse-2",
        "tr-clickhouse-1",
    ]


def test_wrapper_passes_the_flags_through(tmp_path: Path) -> None:
    result, calls = _wrapper(tmp_path, "--apply", "--drop-renamed-logs")

    assert result.returncode == 0, result.stderr
    for call in _ssh(calls):
        command = next(arg for arg in call["argv"] if arg.startswith("--command="))
        assert command.endswith('apply_node_config.sh" --apply --drop-renamed-logs\'')


def test_wrapper_stops_at_the_first_failing_node(tmp_path: Path) -> None:
    result, calls = _wrapper(tmp_path, "--apply", FAKE_FAILING_NODE="tr-clickhouse-2")

    assert result.returncode != 0
    assert [call["argv"][2] for call in _ssh(calls)] == ["tr-clickhouse-3", "tr-clickhouse-2"]


@pytest.mark.parametrize(
    ("args", "env", "message"),
    [
        ((), {"NAME": "tr-clickhouse-2"}, "set NAME and ZONE together"),
        ((), {"FAKE_EXTERNAL_IP_NODE": "tr-clickhouse-1"}, "tr-clickhouse-1 has external IP"),
        (("--restart-everything",), {}, "usage:"),
    ],
    ids=["name-alone", "external-ip", "unknown-flag"],
)
def test_wrapper_refuses_before_touching_any_node(
    tmp_path: Path, args: tuple[str, ...], env: dict[str, str], message: str
) -> None:
    result, calls = _wrapper(tmp_path, *args, **env)

    assert result.returncode != 0
    assert message in result.stderr
    assert _ssh(calls) == []


def test_wrapper_targets_one_node_with_name_and_zone(tmp_path: Path) -> None:
    result, calls = _wrapper(tmp_path, NAME="tr-clickhouse-2", ZONE="us-central1-b")

    assert result.returncode == 0, result.stderr
    assert [call["argv"][2] for call in _ssh(calls)] == ["tr-clickhouse-2"]
