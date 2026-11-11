"""G2 (/tr_health) and G5 (system-log retention) in
docs/design/clickhouse-high-availability.md, as rolled out by
scripts/deploy/clickhouse_node_config.sh.

The XML was verified on ClickHouse 26.7.1.1315 (the cluster's version) in
Docker on 2026-10-02: the server starts with it, every listed log gets its TTL,
text_log drops to information, /tr_health answers 200 with 17 replicated tables
and 500 with 16 or with one detached, the built-in handlers keep working, and
tr_health is refused outside its networks and cannot read tr data. These tests
pin that content and execute both scripts against stubs.

The lockdown (no `default` user, no outbound table functions, no metadata
server for the server process) was verified on ClickHouse 26.8.36.3 in Docker on
2026-11-11 by running apply_node_config.sh --apply on a node installed the way
clickhouse_startup.sh installs it. Before: a query with no credentials answers
200 and the clickhouse user reaches the metadata address. After: system.users
lists only named users, that query answers 401, url() and remote() fail with
UNACCEPTABLE_URL, ON CLUSTER statements and replication still work, and the
clickhouse user's connection to the metadata address is reset while root's is
not. A second run changes nothing and leaves one firewall rule. With each
protection undone in turn the script refused. One finding is pinned below:
SELECT count() FROM url(...) answers 1 without fetching anything, even when the
connection is refused, so the probe has to read the body.
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
REAL_INSTALL = shutil.which("install") or "/usr/bin/install"

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


def test_the_default_user_is_removed_and_no_other_user_is_touched() -> None:
    users = _xml("tr-default-user.xml").find("users")
    assert users is not None
    assert [(child.tag, child.attrib) for child in users] == [("default", {"remove": "remove"})]


def test_no_host_is_allowed_for_outbound_table_functions() -> None:
    root = _xml("tr-egress.xml")
    assert [child.tag for child in root] == ["remote_url_allow_hosts"]
    # An empty element refuses every host. A missing one allows them all.
    allowed = root.find("remote_url_allow_hosts")
    assert allowed is not None
    assert list(allowed) == [] and not (allowed.text or "").strip()


def _code(name: str) -> str:
    lines = (CONFIG_DIR / name).read_text().splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def test_the_metadata_block_rejects_the_server_user_and_spares_the_resolver() -> None:
    helper = CONFIG_DIR / "tr-clickhouse-metadata-block.sh"
    # git archive ships the mode, and systemd will not run a file without it.
    assert os.access(helper, os.X_OK)
    code = " ".join(_code(helper.name).split())
    assert (
        "RULE=( -d 169.254.169.254/32 -p tcp ! --dport 53 -m owner --uid-owner clickhouse "
        "-j REJECT --reject-with tcp-reset )"
    ) in code
    # Asked for before it is added, so a restart does not stack copies.
    assert 'if ! iptables -w -C OUTPUT "${RULE[@]}" 2>/dev/null; then iptables -w -I OUTPUT "${RULE[@]}" fi' in code
    assert "set -euo pipefail" in code


def test_the_server_cannot_start_without_the_metadata_block() -> None:
    assert _code("tr-metadata-block.conf").split() == [
        "[Service]",
        "ExecStartPre=+/usr/local/sbin/tr-clickhouse-metadata-block",
    ]
    # The path systemd runs is the path the node script installs.
    script = (CONFIG_DIR / "apply_node_config.sh").read_text()
    assert 'METADATA_BLOCK="${ROOT}/usr/local/sbin/tr-clickhouse-metadata-block"' in script
    assert '"tr-clickhouse-metadata-block.sh|${METADATA_BLOCK}|run"' in script
    assert 'DROP_IN_D="${ROOT}/etc/systemd/system/clickhouse-server.service.d"' in script


def test_the_url_probe_reads_the_body_and_tries_once() -> None:
    # Verified on 26.8.36.3: count() over url() answers 1 with the connection
    # refused, and a refused connection is retried for about 98 s by default.
    script = (CONFIG_DIR / "apply_node_config.sh").read_text()
    probes = [line for line in script.splitlines() if "FROM url(" in line and not line.startswith("#")]
    assert len(probes) == 1
    assert "SELECT length(raw_blob) FROM url('${METADATA_URL}', 'RawBLOB')" in probes[0]
    assert "SETTINGS http_max_tries = 1" in probes[0]
    assert "count()" not in probes[0]


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
    url="${@: -1}"
    case "$url" in
      *169.254.169.254*)
        # Root asking the metadata server: curl's exit status is the answer.
        exit "$(cat "$state/metadata-root-rc" 2>/dev/null || echo 0)"
        ;;
      *query=SELECT*)
        # A query with no credentials.
        printf '%s' "$(cat "$state/anonymous-code" 2>/dev/null || echo 401)"
        ;;
      *)
        # /tr_health: the next queued status code; 200 once the queue is empty.
        code=200
        if [ -s "$state/curl-codes" ]; then
          code="$(head -1 "$state/curl-codes")"
          tail -n +2 "$state/curl-codes" > "$state/curl-codes.next"
          mv "$state/curl-codes.next" "$state/curl-codes"
        fi
        printf '%s' "$code"
        ;;
    esac
    ;;
  runuser)
    # The same request as the clickhouse user: 7 is "connection refused".
    exit "$(cat "$state/metadata-clickhouse-rc" 2>/dev/null || echo 7)"
    ;;
  iptables)
    [ -e "$state/fail-iptables" ] && { echo "iptables: Permission denied" >&2; exit 4; }
    case " $* " in
      *" -C "*) [ -e "$state/rule-present" ] || exit 1 ;;
      *" -I "*) : > "$state/rule-present" ;;
    esac
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
      *"FROM url("*)
        if [ -e "$state/url-open" ]; then
          printf '1042\n'
        else
          cat "$state/url-error" 2>/dev/null >&2 \
            || echo "Code: 491. DB::Exception: URL \"http://169.254.169.254/\" is not allowed in configuration file, see <remote_url_allow_hosts>. (UNACCEPTABLE_URL)" >&2
          exit 235
        fi
        ;;
      *"FROM system.replicas"*) cat "$state/replicas-$host" 2>/dev/null || printf '0\t17\n' ;;
      *"FROM system.tables"*)
        [ -e "$state/fail-system-tables" ] && { echo "Connection refused" >&2; exit 210; }
        cat "$state/system-tables" 2>/dev/null || true
        ;;
    esac
    ;;
  hostname) printf 'tr-clickhouse-test\n' ;;
  # Recorded, then done for real: the tests read the files it leaves.
  install) exec "$STUB_REAL_INSTALL" "$@" ;;
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


# Where each shipped file lands on a node.
INSTALLED = {
    "etc/clickhouse-server/users.d/tr-health.xml": "tr-health-user.xml",
    "etc/clickhouse-server/users.d/tr-default-user.xml": "tr-default-user.xml",
    "etc/clickhouse-server/config.d/tr-system-logs.xml": "tr-system-logs.xml",
    "etc/clickhouse-server/config.d/tr-health.xml": "tr-health.xml",
    "etc/clickhouse-server/config.d/tr-egress.xml": "tr-egress.xml",
    "usr/local/sbin/tr-clickhouse-metadata-block": "tr-clickhouse-metadata-block.sh",
    "etc/systemd/system/clickhouse-server.service.d/tr-metadata-block.conf": "tr-metadata-block.conf",
}
LOCKED_DOWN = "no query without credentials, no url(), and no metadata server for the ClickHouse user"
RULE = [
    "-d", "169.254.169.254/32", "-p", "tcp", "!", "--dport", "53",
    "-m", "owner", "--uid-owner", "clickhouse",
    "-j", "REJECT", "--reject-with", "tcp-reset",
]


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
        for name in (
            "systemctl", "curl", "clickhouse-client", "hostname", "sleep", "keeper-probe",
            "runuser", "iptables", "install",
        ):
            stub = self.bin / name
            stub.write_text(STUB)
            stub.chmod(0o755)
        self.log = tmp_path / "calls.jsonl"
        self.log.write_text("")
        self.env: dict[str, str] = {}

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        self.log.write_text("")
        env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.root),
            "STUB_LOG": str(self.log),
            "STUB_STATE": str(self.state),
            "STUB_REAL_INSTALL": REAL_INSTALL,
            "TR_NODE_ROOT": str(self.root),
            "TR_FILE_OWNER": subprocess.check_output(["id", "-un"], text=True).strip(),  # noqa: S607
            "TR_FILE_GROUP": subprocess.check_output(["id", "-gn"], text=True).strip(),  # noqa: S607
            "TR_KEEPER_PROBE": str(self.bin / "keeper-probe"),
            "TR_WAIT_TRIES": "4",
            "TR_WAIT_SECONDS": "0",
            **self.env,
        }
        return subprocess.run(  # noqa: S603 - fixed script under test
            [BASH, str(self.bundle / "apply_node_config.sh"), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def sequence(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def calls(self, name: str) -> list[list[str]]:
        return [row[1:] for row in self.sequence() if row[0] == name]

    def health_polls(self) -> list[list[str]]:
        return [call for call in self.calls("curl") if "tr_health" in call[-1]]

    def installed(self) -> dict[str, str]:
        found = {}
        for relative in INSTALLED:
            path = self.root / relative
            if path.exists():
                found[relative] = path.read_text()
        return found

    def install_current(self, *, users: bool = True, configs: bool = True) -> None:
        for relative, source in INSTALLED.items():
            if "/users.d/" in relative and not users:
                continue
            if "/config.d/" in relative and not configs:
                continue
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(CONFIG_DIR / source, destination)


@pytest.fixture
def node(tmp_path: Path) -> Node:
    return Node(tmp_path)


def test_report_only_changes_nothing(node: Node) -> None:
    result = node.run()

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("differs") == len(INSTALLED) == 7
    assert "--apply would restart clickhouse-server" in result.stdout
    assert node.installed() == {}
    assert node.calls("systemctl") == [] and node.calls("clickhouse-client") == []
    assert node.calls("iptables") == [] and node.calls("runuser") == []


def test_apply_installs_restarts_once_and_waits_for_health(node: Node) -> None:
    (node.state / "curl-codes").write_text("000\n503\n200\n")

    result = node.run("--apply")

    assert result.returncode == 0, result.stderr
    assert node.installed() == {
        relative: (CONFIG_DIR / source).read_text() for relative, source in INSTALLED.items()
    }
    # systemd rereads its units before the one restart, so that restart
    # already runs the metadata block as a start step.
    assert node.calls("systemctl") == [["daemon-reload"], ["restart", "clickhouse-server"]]
    # It kept polling /tr_health until the 200.
    assert len(node.health_polls()) == 3
    assert "/tr_health answers 200 and the cluster is whole" in result.stdout
    assert LOCKED_DOWN in result.stdout
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
    # The lockdown is still tried on a node with nothing to change.
    assert LOCKED_DOWN in result.stdout


def test_the_metadata_block_is_root_owned_runs_before_the_restart_and_is_not_stacked(
    node: Node,
) -> None:
    result = node.run("--apply")

    assert result.returncode == 0, result.stderr
    helper = node.root / "usr/local/sbin/tr-clickhouse-metadata-block"
    drop_in = node.root / "etc/systemd/system/clickhouse-server.service.d/tr-metadata-block.conf"
    # Not writable by the server's group or anyone else, unlike the 0640
    # clickhouse-owned XML beside it.
    assert helper.stat().st_mode & 0o777 == 0o755
    assert drop_in.stat().st_mode & 0o777 == 0o644
    # Mode alone would pass with the server's user as owner, who could then
    # rewrite both. The script runs as root, so naming no owner is what makes
    # them root's; the XML is handed to the server's user by name.
    owned = {call[-1]: call for call in node.calls("install")}
    assert set(owned) == {str(node.root / relative) for relative in INSTALLED}
    for destination, call in owned.items():
        handed_over = "-o" in call or "-g" in call
        assert handed_over == destination.endswith(".xml"), call
    # Asked for, absent, so inserted: all before the server restarts.
    assert node.calls("iptables") == [["-w", "-C", "OUTPUT", *RULE], ["-w", "-I", "OUTPUT", *RULE]]
    names = [row[:2] for row in node.sequence()]
    assert names.index(["iptables", "-w"]) < names.index(["systemctl", "restart"])
    # The clickhouse user, not root, is who must be refused.
    assert node.calls("runuser")[0][:4] == ["-u", "clickhouse", "--", "curl"]
    assert node.calls("runuser")[0][-1] == "http://169.254.169.254/"

    again = node.run("--apply")

    assert again.returncode == 0, again.stderr
    assert node.calls("systemctl") == []
    assert node.calls("iptables") == [["-w", "-C", "OUTPUT", *RULE]]


def test_a_node_without_iptables_is_refused_before_anything_changes(node: Node) -> None:
    # The helper refuses without iptables, and the drop-in makes the helper a
    # step of every server start. Installing either first would leave a server
    # that keeps running and then cannot start again.
    node.env["TR_IPTABLES_COMMAND"] = "iptables-that-is-not-installed"

    report = node.run()

    assert report.returncode == 0, report.stderr
    assert "iptables is not installed; --apply would refuse" in report.stdout

    result = node.run("--apply")

    assert result.returncode == 1
    assert "refusing to change tr-clickhouse-test: iptables is not installed" in result.stderr
    assert "apt-get install -y iptables" in result.stderr
    assert node.installed() == {}
    assert node.calls("install") == [] and node.calls("systemctl") == []
    assert node.calls("iptables") == [] and node.calls("keeper-probe") == []
    assert LOCKED_DOWN not in result.stdout


def test_a_current_node_without_iptables_is_still_refused(node: Node) -> None:
    # Nothing to install is not the same as nothing to check: the rule is
    # applied on every run, and this node could not apply it.
    node.install_current()
    node.env["TR_IPTABLES_COMMAND"] = "iptables-that-is-not-installed"

    result = node.run("--apply")

    assert result.returncode == 1
    assert "iptables is not installed" in result.stderr
    assert node.calls("systemctl") == [] and node.calls("iptables") == []


def test_report_says_nothing_about_iptables_when_it_is_installed(node: Node) -> None:
    assert "iptables" not in node.run().stdout


# What the node still does with one protection undone, and what the script says.
# Each row was reproduced on a real 26.8.36.3 node on 2026-11-11.
UNDONE = [
    (("anonymous-code", "200"), "a query with no credentials was not refused with 401"),
    (("url-open", ""), "ClickHouse on tr-clickhouse-test fetched a URL with url()"),
    (
        # remote_url_allow_hosts open, and only the firewall rule stopped it.
        ("url-error", "Code: 1000. DB::Exception: Connection refused. (POCO_EXCEPTION)\n"),
        "url() on tr-clickhouse-test failed, but not because the host is not allowed",
    ),
    (
        ("metadata-root-rc", "7"),
        "root on tr-clickhouse-test cannot reach the metadata server, so the block cannot be checked",
    ),
    (
        ("metadata-clickhouse-rc", "0"),
        "the ClickHouse user on tr-clickhouse-test was not refused by the metadata server (curl exit 0)",
    ),
    (
        # A timeout is not the rule's answer either: something else is in the way.
        ("metadata-clickhouse-rc", "28"),
        "the ClickHouse user on tr-clickhouse-test was not refused by the metadata server (curl exit 28)",
    ),
    (
        ("fail-iptables", ""),
        "could not block the metadata server for the ClickHouse server on tr-clickhouse-test",
    ),
]
UNDONE_IDS = [
    "default-user-still-answers",
    "url-fetches",
    "url-stopped-only-by-the-firewall",
    "metadata-server-down",
    "server-user-reaches-metadata",
    "server-user-times-out",
    "rule-cannot-be-installed",
]


@pytest.mark.parametrize("current", [False, True], ids=["first-apply", "nothing-to-change"])
@pytest.mark.parametrize(("setup", "message"), UNDONE, ids=UNDONE_IDS)
def test_success_is_not_reported_while_a_protection_is_missing(
    node: Node, setup: tuple[str, str], message: str, current: bool
) -> None:
    # The files being in place proves nothing: another users.d file can bring
    # `default` back, and a rule ahead of ours can let the server through.
    if current:
        node.install_current()
    (node.state / setup[0]).write_text(setup[1])

    result = node.run("--apply")

    assert result.returncode != 0
    assert message in result.stderr
    assert LOCKED_DOWN not in result.stdout


def test_a_users_only_change_reloads_without_a_restart(node: Node) -> None:
    node.install_current(users=False)

    result = node.run("--apply")

    assert result.returncode == 0, result.stderr
    assert node.calls("systemctl") == []
    assert "etc/clickhouse-server/users.d/tr-health.xml" in node.installed()
    assert "etc/clickhouse-server/users.d/tr-default-user.xml" in node.installed()
    assert len(node.health_polls()) >= 1


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
    assert node.calls("systemctl") == [["daemon-reload"], ["restart", "clickhouse-server"]]
    assert LOCKED_DOWN not in result.stdout


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
            *(f"scripts/deploy/clickhouse-node-config/{source}" for source in INSTALLED.values()),
        } <= names
        # The mode travels with the archive: the node runs the helper as shipped.
        with tarfile.open(call["stdin_file"]) as bundle:
            helper = bundle.getmember(
                "scripts/deploy/clickhouse-node-config/tr-clickhouse-metadata-block.sh"
            )
        assert helper.mode & 0o111
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
