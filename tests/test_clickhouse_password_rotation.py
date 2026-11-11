"""Rotation of the `tr` ClickHouse password on the GCP cluster:
scripts/deploy/clickhouse_rotate_password.sh and the node-side
scripts/deploy/clickhouse-node-config/rotate_tr_password.sh.

The node script was run on ClickHouse 26.8.36.3 in Docker on 2026-11-11, on a
node whose users.d/tr-user.xml still held the password in plain text. Run with
the same password it replaced the file with the hashed one and the server kept
accepting it. Run with a new one, the server accepted the new password and
answered AUTHENTICATION_FAILED to the old one with no restart (same server
process before and after), the staged copies in users.d were never read as
configuration, /etc/tr-clickhouse-ingest.env held the new value at mode 0600,
and only the running service that reads that file was restarted. With a hash
the server could not match, the previous file went back and the old password
worked again. A second run changed nothing. These tests execute both scripts
against stand-ins that model those behaviours.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tarfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "scripts/deploy/clickhouse-node-config"
NODE_SCRIPT = CONFIG_DIR / "rotate_tr_password.sh"
WRAPPER = ROOT / "scripts/deploy/clickhouse_rotate_password.sh"
STARTUP = ROOT / "scripts/deploy/clickhouse_startup.sh"
UNITS = ROOT / "clickhouse"
BASH = shutil.which("bash") or "/bin/bash"

OLD = "old-fixture-credential-0123456789"
NEW = "new-fixture-credential-abcdefghij"
ENV_LINE = "EnvironmentFile=/etc/tr-clickhouse-ingest.env"

# One program under several names. The ClickHouse client answers the way the
# server would for the user file that is on disk right now, which is what
# rereading users.d without a restart amounts to.
STUB = r'''#!/usr/bin/env python3
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
argv = sys.argv[1:]
state = Path(os.environ["STUB_STATE"])
root = Path(os.environ["TR_NODE_ROOT"])


def listed(file, item):
    path = state / file
    return path.exists() and item in path.read_text().split()


entry = {"name": name, "argv": argv}
if name == "clickhouse-client":
    entry["password"] = os.environ.get("CLICKHOUSE_PASSWORD")
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps(entry) + "\n")

if name == "hostname":
    print("tr-clickhouse-test")
elif name == "sleep":
    if (state / "real-sleep").exists():
        import time
        time.sleep(0.2)
elif name == "clickhouse-client":
    outage = "Code: 210. DB::NetException: Connection refused (localhost:9000). (NETWORK_ERROR)"
    if (state / "server-down").exists():
        sys.exit(outage)
    blip = state / "down-after-accepting"
    if blip.exists() and (state / "accepted-once").exists():
        sys.exit(outage)
    frozen = state / "frozen-user.xml"
    live = root / "etc/clickhouse-server/users.d/tr-user.xml"
    text = (frozen if frozen.exists() else live).read_text()
    password = entry["password"] or ""
    plain = re.search(r"<password>(.*?)</password>", text)
    hashed = re.search(r"<password_sha256_hex>(.*?)</password_sha256_hex>", text)
    accepted = (
        (plain is not None and plain.group(1) == password)
        or (hashed is not None
            and hashed.group(1) == hashlib.sha256(password.encode()).hexdigest())
        or listed("also-accepts", password)
    )
    if not accepted:
        if (state / "wrong-password-looks-like-an-outage").exists():
            sys.exit(outage)
        print("Code: 516. DB::Exception: tr: Authentication failed: password is incorrect, "
              "or there is no user with such name. (AUTHENTICATION_FAILED)", file=sys.stderr)
        sys.exit(4)
    if blip.exists() and listed("down-after-accepting", password) and argv[-1] == "SELECT 1":
        (state / "accepted-once").touch()
    if argv[-1] == "SELECT 1":
        print(1)
elif name == "systemctl":
    unit = argv[-1]
    if argv[0] == "is-active":
        sys.exit(0 if listed("active", unit) else 3)
    if argv[0] == "restart":
        shutil.copy(root / "etc/tr-clickhouse-ingest.env", state / ("env-at-restart-" + unit))
        if listed("restart-fails", unit) or listed("dies-on-restart", unit):
            active = state / "active"
            active.write_text("\n".join(u for u in active.read_text().split() if u != unit))
        if listed("restart-fails", unit):
            sys.exit(1)
'''


def _plain_user_file(password: str) -> str:
    """What clickhouse_startup.sh wrote before it hashed the password."""
    return (
        "<clickhouse>\n    <users>\n        <tr>\n"
        f"            <password>{password}</password>\n"
        "            <networks>\n                <ip>10.0.0.0/8</ip>\n"
        "                <ip>127.0.0.1</ip>\n                <ip>::1</ip>\n"
        "            </networks>\n            <profile>default</profile>\n"
        "            <quota>default</quota>\n"
        "            <access_management>1</access_management>\n"
        "        </tr>\n    </users>\n</clickhouse>\n"
    )


def _hashed_user_file(password: str) -> str:
    """The file a new node gets, taken from the startup script itself."""
    match = re.search(
        r"cat > /etc/clickhouse-server/users\.d/tr-user\.xml <<XML\n(.*?\n)XML\n",
        STARTUP.read_text(),
        re.DOTALL,
    )
    assert match is not None
    digest = hashlib.sha256(password.encode()).hexdigest()
    rendered = match.group(1).replace("${PASSWORD_SHA256}", digest)
    assert "$" not in rendered
    return rendered


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class Node:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "node"
        self.users_d = self.root / "etc/clickhouse-server/users.d"
        self.user_file = self.users_d / "tr-user.xml"
        self.env_file = self.root / "etc/tr-clickhouse-ingest.env"
        self.unit_d = self.root / "etc/systemd/system"
        self.state = tmp_path / "state"
        self.bin = tmp_path / "bin"
        self.log = tmp_path / "calls.jsonl"
        for directory in (self.users_d, self.unit_d, self.state, self.bin):
            directory.mkdir(parents=True)
        self.user_file.write_text(_plain_user_file(OLD))
        self.env_file.write_text(f"CH_PASSWORD={OLD}\n")
        self.env_file.chmod(0o600)
        self.unit("tr-clickhouse-ingest.service", ENV_LINE, active=True)
        self.unit("tr-clickhouse-rollup-hourly.service", ENV_LINE, active=False)
        self.unit("optional-file.service", ENV_LINE.replace("=/", "=-/"), active=True)
        self.unit("commented-out.service", "# " + ENV_LINE, active=True)
        self.unit("unrelated.service", "Environment=A=b", active=True)
        (self.bin / "stub").write_text(STUB)
        (self.bin / "stub").chmod(0o755)
        for name in ("clickhouse-client", "systemctl", "sleep", "hostname"):
            (self.bin / name).symlink_to("stub")

    def unit(self, name: str, line: str, *, active: bool) -> None:
        (self.unit_d / name).write_text(f"[Service]\n{line}\nExecStart=/bin/true\n")
        if active:
            with (self.state / "active").open("a") as handle:
                handle.write(name + "\n")

    def mark(self, file: str, *items: str) -> None:
        (self.state / file).write_text("\n".join(items))

    def _env(self, tries: str) -> dict[str, str]:
        return {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.root),
            "STUB_LOG": str(self.log),
            "STUB_STATE": str(self.state),
            "TR_NODE_ROOT": str(self.root),
            "TR_FILE_OWNER": subprocess.check_output(["id", "-un"], text=True).strip(),  # noqa: S607
            "TR_FILE_GROUP": subprocess.check_output(["id", "-gn"], text=True).strip(),  # noqa: S607
            "TR_WAIT_TRIES": tries,
            "TR_WAIT_SECONDS": "0",
        }

    def run(self, password: str, *args: str) -> subprocess.CompletedProcess[str]:
        self.log.write_text("")
        env = self._env("3")
        return subprocess.run(  # noqa: S603 - fixed script under test
            [BASH, str(NODE_SCRIPT), *args],
            input=password + "\n", env=env, capture_output=True, text=True, timeout=60,
            check=False,
        )

    def run_with_output_gone(self, password: str, *args: str) -> int:
        """Run as if the SSH session had dropped: every write fails."""
        self.log.write_text("")
        read_end, write_end = os.pipe()
        os.close(read_end)
        try:
            return subprocess.run(  # noqa: S603 - fixed script under test
                [BASH, str(NODE_SCRIPT), *args], env=self._env("3"),
                input=(password + "\n").encode(), stdout=write_end, stderr=write_end,
                timeout=60, check=False,
            ).returncode
        finally:
            os.close(write_end)

    @property
    def unfinished(self) -> Path:
        return self.root / "var/lib/tr-clickhouse-rotate/unfinished"

    def start(self, password: str, *args: str, tries: str) -> subprocess.Popen[str]:
        self.log.write_text("")
        process = subprocess.Popen(  # noqa: S603 - fixed script under test
            [BASH, str(NODE_SCRIPT), *args], env=self._env(tries), text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert process.stdin is not None
        process.stdin.write(password + "\n")
        process.stdin.close()
        return process

    def calls(self, name: str | None = None) -> list[dict]:
        entries = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [e for e in entries if name is None or e["name"] == name]

    def restarted(self) -> list[str]:
        return [c["argv"][-1] for c in self.calls("systemctl") if c["argv"][0] == "restart"]

    def signs_in(self, password: str) -> bool:
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
               "STUB_LOG": os.devnull, "STUB_STATE": str(self.state),
               "TR_NODE_ROOT": str(self.root), "CLICKHOUSE_PASSWORD": password}
        return subprocess.run(  # noqa: S603 - the stand-in above
            [str(self.bin / "clickhouse-client"), "--user", "tr", "--query", "SELECT 1"],
            env=env, capture_output=True, check=False,
        ).returncode == 0

    def files(self) -> dict[str, str]:
        return {
            str(p.relative_to(self.root)): p.read_text()
            for p in sorted(self.root.rglob("*")) if p.is_file()
        }


@pytest.fixture
def node(tmp_path: Path) -> Node:
    return Node(tmp_path)


def test_report_only_changes_nothing(node: Node) -> None:
    before = node.files()
    result = node.run(NEW)
    assert result.returncode == 0, result.stderr
    assert "--apply would change the tr password" in result.stdout
    assert node.files() == before
    assert node.calls("systemctl") == []
    assert all(c["argv"][-1] == "SELECT 1" for c in node.calls("clickhouse-client"))


def test_apply_changes_the_password_everywhere_it_lives(node: Node) -> None:
    result = node.run(NEW, "--apply")
    assert result.returncode == 0, result.stderr

    assert node.signs_in(NEW)
    assert not node.signs_in(OLD)
    # Byte for byte the user a new node gets, so the two cannot drift apart.
    assert node.user_file.read_text() == _hashed_user_file(NEW)
    assert _mode(node.user_file) == 0o640
    # Only the hash is on disk on the server's side.
    user = ET.fromstring(node.user_file.read_text()).find("users/tr")  # noqa: S314
    assert user is not None
    assert user.find("password") is None
    assert user.findtext("password_sha256_hex") == hashlib.sha256(NEW.encode()).hexdigest()
    assert NEW not in node.user_file.read_text()

    assert node.env_file.read_text() == f"CH_PASSWORD={NEW}\n"
    assert _mode(node.env_file) == 0o600
    # Nothing staged is left behind, in particular no copy of the old file.
    assert [p.name for p in node.users_d.iterdir()] == ["tr-user.xml"]
    assert [p.name for p in node.env_file.parent.iterdir() if p.is_file()] == [
        "tr-clickhouse-ingest.env"
    ]
    assert all(OLD not in text for text in node.files().values())


def test_only_running_services_that_read_the_env_file_are_restarted(node: Node) -> None:
    result = node.run(NEW, "--apply")
    assert result.returncode == 0, result.stderr
    assert sorted(node.restarted()) == ["optional-file.service", "tr-clickhouse-ingest.service"]
    # Each was restarted after the file it reads held the new value.
    for unit in node.restarted():
        assert (node.state / f"env-at-restart-{unit}").read_text() == f"CH_PASSWORD={NEW}\n"
    # ClickHouse itself is never restarted: the user file is reread in place.
    assert not any("clickhouse-server" in " ".join(c["argv"]) for c in node.calls("systemctl"))


def test_every_unit_in_the_repository_that_reads_the_env_file_is_found(node: Node) -> None:
    expected = []
    for unit in sorted(UNITS.glob("*.service")):
        shutil.copy(unit, node.unit_d / unit.name)
        if ENV_LINE in unit.read_text().splitlines():
            expected.append(unit.name)
    node.mark("active", *(p.name for p in node.unit_d.iterdir()))
    assert {"tr-clickhouse-ingest.service", "tr-clickhouse-operational-ingest.service"} <= set(
        expected
    )

    result = node.run(NEW, "--apply")
    assert result.returncode == 0, result.stderr
    assert sorted(node.restarted()) == sorted([*expected, "optional-file.service"])


def test_the_password_is_never_a_command_argument(node: Node) -> None:
    result = node.run(NEW, "--apply")
    assert result.returncode == 0, result.stderr
    calls = node.calls()
    assert calls
    for call in calls:
        assert not any(OLD in arg or NEW in arg for arg in call["argv"]), call
        assert "--password" not in call["argv"]
    assert {c["password"] for c in node.calls("clickhouse-client")} == {OLD, NEW}
    assert OLD not in result.stdout + result.stderr
    assert NEW not in result.stdout + result.stderr


def test_the_same_password_replaces_a_plain_text_file_and_nothing_else(node: Node) -> None:
    report = node.run(OLD)
    assert "would rewrite" in report.stdout
    assert "hold only its hash" in report.stdout
    assert node.user_file.read_text() == _plain_user_file(OLD)

    result = node.run(OLD, "--apply")
    assert result.returncode == 0, result.stderr
    assert node.user_file.read_text() == _hashed_user_file(OLD)
    assert node.signs_in(OLD)
    assert node.env_file.read_text() == f"CH_PASSWORD={OLD}\n"
    assert node.restarted() == []


def test_a_second_run_changes_nothing(node: Node) -> None:
    assert node.run(NEW, "--apply").returncode == 0
    before = node.files()
    report = node.run(NEW)
    assert "already uses this password" in report.stdout
    result = node.run(NEW, "--apply")
    assert result.returncode == 0, result.stderr
    assert node.files() == before
    assert node.restarted() == []
    assert "installed" not in result.stdout
    assert "wrote" not in result.stdout


def test_the_previous_user_file_returns_when_the_server_never_accepts_the_new_one(
    node: Node,
) -> None:
    # The server keeps answering from the file it loaded at start.
    shutil.copy(node.user_file, node.state / "frozen-user.xml")
    before = node.files()
    result = node.run(NEW, "--apply")
    assert result.returncode == 1
    assert "did not accept the new password" in result.stderr
    assert "the previous user file is back" in result.stderr
    assert node.files() == before
    assert _mode(node.user_file) == 0o640
    assert node.restarted() == []


def test_success_is_not_reported_while_the_old_password_still_works(node: Node) -> None:
    node.mark("also-accepts", OLD)
    result = node.run(NEW, "--apply")
    assert result.returncode == 1
    assert "was not seen to refuse the previous password" in result.stderr
    # The server accepts the new value, so the services already have it: they
    # are never left holding a value the server may stop accepting.
    assert node.env_file.read_text() == f"CH_PASSWORD={NEW}\n"
    assert sorted(node.restarted()) == ["optional-file.service", "tr-clickhouse-ingest.service"]


def test_an_outage_is_not_mistaken_for_the_old_password_being_refused(node: Node) -> None:
    node.mark("wrong-password-looks-like-an-outage")
    result = node.run(NEW, "--apply")
    assert result.returncode == 1
    assert "was not seen to refuse the previous password" in result.stderr
    assert node.env_file.read_text() == f"CH_PASSWORD={NEW}\n"


def test_services_get_the_new_value_even_if_the_server_blips_after_accepting_it(
    node: Node,
) -> None:
    # Accepted, then down for the refused-check: the old order left the env
    # file on the old value, and the services broke once the server came back.
    node.mark("down-after-accepting", NEW)
    result = node.run(NEW, "--apply")
    assert result.returncode == 1
    assert node.env_file.read_text() == f"CH_PASSWORD={NEW}\n"
    assert node.user_file.read_text() == _hashed_user_file(NEW)


def test_a_dropped_session_after_acceptance_still_writes_and_restarts(node: Node) -> None:
    # The server already takes the new value (an earlier run installed it);
    # the services still hold the old one. Over SSH a dropped session is no
    # signal: the script learns of it when a write fails.
    node.user_file.write_text(_hashed_user_file(NEW))
    node.run_with_output_gone(NEW, "--apply")
    assert node.env_file.read_text() == f"CH_PASSWORD={NEW}\n"
    assert sorted(node.restarted()) == ["optional-file.service", "tr-clickhouse-ingest.service"]
    assert not node.unfinished.exists()


def test_a_run_that_stopped_after_the_env_file_is_finished_by_the_next(node: Node) -> None:
    # Where a dropped session used to leave a node: env file already new, the
    # services never restarted. The env file alone would make it look done.
    node.user_file.write_text(_hashed_user_file(NEW))
    node.env_file.write_text(f"CH_PASSWORD={NEW}\n")
    node.unfinished.parent.mkdir(parents=True)
    node.unfinished.write_text(OLD)
    report = node.run(NEW)
    assert "did not finish" in report.stdout
    result = node.run(NEW, "--apply")
    assert result.returncode == 0, result.stderr
    assert sorted(node.restarted()) == ["optional-file.service", "tr-clickhouse-ingest.service"]
    assert OLD in {c["password"] for c in node.calls("clickhouse-client")}
    assert not node.unfinished.exists()


def test_a_rerun_checks_again_that_the_old_password_is_refused(node: Node) -> None:
    node.mark("also-accepts", OLD)
    first = node.run(NEW, "--apply")
    assert first.returncode == 1
    assert node.env_file.read_text() == f"CH_PASSWORD={NEW}\n"
    assert _mode(node.unfinished) == 0o600
    second = node.run(NEW, "--apply")
    assert second.returncode == 1
    assert "was not seen to refuse the previous password" in second.stderr
    node.mark("also-accepts")
    third = node.run(NEW, "--apply")
    assert third.returncode == 0, third.stderr
    assert not node.unfinished.exists()
    assert all(OLD not in text for text in node.files().values())


def test_the_wait_for_the_old_password_to_be_refused_can_be_stopped(node: Node) -> None:
    node.mark("also-accepts", OLD)
    node.mark("real-sleep")
    process = node.start(NEW, "--apply", tries="200")
    restarted = node.state / "env-at-restart-tr-clickhouse-ingest.service"
    deadline = time.monotonic() + 20
    while not (restarted.exists() and (node.state / "env-at-restart-optional-file.service").exists()):
        assert time.monotonic() < deadline, "the services were never restarted"
        time.sleep(0.05)
    time.sleep(0.3)
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=10)
    assert process.returncode != 0
    # The services keep the new value and the user file stays; the next run
    # finishes the check.
    assert node.env_file.read_text() == f"CH_PASSWORD={NEW}\n"
    assert node.user_file.read_text() == _hashed_user_file(NEW)
    assert node.unfinished.exists()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_an_interrupted_wait_puts_the_previous_user_file_back(node: Node, sig: int) -> None:
    shutil.copy(node.user_file, node.state / "frozen-user.xml")
    node.mark("real-sleep")
    before = node.files()
    process = node.start(NEW, "--apply", tries="200")
    deadline = time.monotonic() + 20
    while node.user_file.read_text() == before["etc/clickhouse-server/users.d/tr-user.xml"]:
        assert time.monotonic() < deadline, "the new user file was never installed"
        time.sleep(0.05)
    time.sleep(0.3)
    process.send_signal(sig)
    process.wait(timeout=20)
    assert process.stderr is not None
    stderr = process.stderr.read()
    assert process.returncode != 0
    assert "the previous user file is back" in stderr
    assert node.files() == before
    assert node.restarted() == []


def test_a_server_that_accepts_neither_password_is_left_alone(node: Node) -> None:
    node.mark("server-down")
    before = node.files()
    result = node.run(NEW, "--apply")
    assert result.returncode == 1
    assert "accepts neither" in result.stderr
    assert node.files() == before
    assert node.calls("systemctl") == []


def test_a_run_that_stopped_halfway_is_finished(node: Node) -> None:
    # The server already has the new password; the services do not.
    node.user_file.write_text(_hashed_user_file(NEW))
    node.env_file.write_text(f"OTHER=kept\nCH_PASSWORD={OLD}\n")
    report = node.run(NEW)
    assert "holds another; --apply would rewrite it" in report.stdout

    result = node.run(NEW, "--apply")
    assert result.returncode == 0, result.stderr
    assert node.env_file.read_text() == f"OTHER=kept\nCH_PASSWORD={NEW}\n"
    assert "tr-clickhouse-ingest.service" in node.restarted()
    assert "installed" not in result.stdout


@pytest.mark.parametrize("how", ["dies-on-restart", "restart-fails"])
def test_a_service_that_does_not_come_back_fails_the_node(node: Node, how: str) -> None:
    node.mark(how, "tr-clickhouse-ingest.service")
    result = node.run(NEW, "--apply")
    assert result.returncode == 1
    assert "tr-clickhouse-ingest.service on tr-clickhouse-test is not running" in result.stderr
    assert "tr signs in with this password" not in result.stdout


@pytest.mark.parametrize(
    "password",
    [
        "",
        "short-0123456789",
        "has a space in it 0123456789abcdef",
        "dollar-$(id)-0123456789abcdefgh",
        "quote-'-0123456789abcdefghijkl",
        "hash-#-0123456789abcdefghijklmn",
        "-leading-dash-0123456789abcdefgh",
    ],
)
def test_a_password_the_env_file_cannot_carry_is_refused(node: Node, password: str) -> None:
    before = node.files()
    result = node.run(password, "--apply")
    assert result.returncode == 1
    assert "must be 24 or more characters" in result.stderr
    assert node.files() == before
    assert node.calls("clickhouse-client") == []


def test_only_the_first_line_of_standard_input_is_the_password(node: Node) -> None:
    result = node.run(f"{NEW}\nsecond-line-is-ignored-0123456789", "--apply")
    assert result.returncode == 0, result.stderr
    assert node.env_file.read_text() == f"CH_PASSWORD={NEW}\n"


def test_a_node_without_the_env_file_is_refused(node: Node) -> None:
    node.env_file.unlink()
    result = node.run(NEW, "--apply")
    assert result.returncode == 1
    assert "is missing" in result.stderr
    assert node.user_file.read_text() == _plain_user_file(OLD)


def test_an_unknown_flag_is_refused(node: Node) -> None:
    before = node.files()
    result = node.run(NEW, "--force")
    assert result.returncode == 2
    assert node.files() == before
    assert node.calls() == []


# The wrapper, against a gcloud that records what it was asked and what it was
# given on standard input.
FAKE_GCLOUD = r'''#!/usr/bin/env python3
import json
import os
import sys
import tempfile
from pathlib import Path

argv = sys.argv[1:]
directory = Path(os.environ["FAKE_DIR"])
journal = directory / "gcloud.jsonl"
entry = {"argv": argv}
code = 0
if argv[:2] == ["compute", "ssh"]:
    earlier = 0
    if journal.exists():
        for line in journal.read_text().splitlines():
            if json.loads(line)["argv"][:3] == argv[:3]:
                earlier += 1
    handle, path = tempfile.mkstemp(dir=directory, prefix="stdin.")
    os.write(handle, sys.stdin.buffer.read())
    os.close(handle)
    entry["stdin_file"] = path
    if os.environ.get("FAKE_FAILING_SSH") == "%s:%d" % (argv[2], earlier + 1):
        code = 1
elif argv[:3] == ["compute", "instances", "describe"]:
    if argv[3] == os.environ.get("FAKE_EXTERNAL_IP_NODE"):
        print("34.0.0.9")
elif argv[:3] == ["secrets", "versions", "access"]:
    if os.environ.get("FAKE_SECRET_UNREADABLE"):
        code = 1
    else:
        sys.stdout.write(os.environ["FAKE_SECRET"])
with open(journal, "a") as log:
    log.write(json.dumps(entry) + "\n")
sys.exit(code)
'''

REMOTE_SCRIPT = "scripts/deploy/clickhouse-node-config/rotate_tr_password.sh"
STAGING = "/run/tr-clickhouse-rotate"
ALL_NODES = ["tr-clickhouse-3", "tr-clickhouse-2", "tr-clickhouse-1"]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",  # noqa: S607
         *args],
        check=True, capture_output=True,
    )


def _checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts/deploy").mkdir(parents=True)
    shutil.copy(WRAPPER, repo / "scripts/deploy" / WRAPPER.name)
    shutil.copytree(CONFIG_DIR, repo / "scripts/deploy/clickhouse-node-config")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


def _wrapper(
    tmp_path: Path, *args: str, repo: Path | None = None, **env: str
) -> tuple[subprocess.CompletedProcess[str], list[dict]]:
    repo = repo or _checkout(tmp_path)
    fake = tmp_path / "fake"
    (fake / "bin").mkdir(parents=True)
    (fake / "bin/gcloud").write_text(FAKE_GCLOUD)
    (fake / "bin/gcloud").chmod(0o755)
    (tmp_path / "tmp").mkdir()
    result = subprocess.run(  # noqa: S603 - fixed script under test
        [BASH, str(repo / "scripts/deploy" / WRAPPER.name), *args],
        env={
            "PATH": f"{fake / 'bin'}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path / "tmp"),
            "FAKE_DIR": str(fake),
            "FAKE_SECRET": NEW,
            **env,
        },
        capture_output=True, text=True, timeout=60, check=False,
    )
    journal = fake / "gcloud.jsonl"
    calls = [json.loads(line) for line in journal.read_text().splitlines()] if journal.exists() else []
    return result, calls


def _ssh(calls: list[dict]) -> list[dict]:
    return [c for c in calls if c["argv"][:2] == ["compute", "ssh"]]


def _command(call: dict) -> str:
    (command,) = [a for a in call["argv"] if a.startswith("--command=")]
    return command.removeprefix("--command=")


def test_wrapper_ships_the_script_then_pipes_the_password_to_each_node(tmp_path: Path) -> None:
    result, calls = _wrapper(tmp_path, "--apply")
    assert result.returncode == 0, result.stderr
    ssh = _ssh(calls)
    assert [c["argv"][2] for c in ssh] == [n for n in ALL_NODES for _ in range(2)]
    assert "The tr password is current on: tr-clickhouse-3 tr-clickhouse-2 tr-clickhouse-1" in (
        result.stdout
    )
    for ship, rotate in zip(ssh[0::2], ssh[1::2], strict=True):
        for call in (ship, rotate):
            assert "--tunnel-through-iap" in call["argv"]
            assert "--project=quill-cloud-proxy" in call["argv"]
            # What the node is asked to run has to parse.
            subprocess.run([BASH, "-n", "-c", _command(call)], check=True)  # noqa: S603
        with tarfile.open(ship["stdin_file"]) as archive:
            member = archive.getmember(REMOTE_SCRIPT)
            assert member.mode & 0o111
            shipped = archive.extractfile(member)
            assert shipped is not None
            assert shipped.read() == NODE_SCRIPT.read_bytes()
        # Root-only, emptied first, and gone when the run ends.
        assert f"rm -rf {STAGING}; install -d -m 0700 {STAGING}; tar -xzf - -C {STAGING}" in (
            _command(ship)
        )
        assert "rotate_tr_password.sh" not in _command(ship)
        assert _command(rotate).startswith("sudo bash -c '")
        assert f'trap "rm -rf {STAGING}" EXIT' in _command(rotate)
        assert _command(rotate).endswith(f"bash {STAGING}/{REMOTE_SCRIPT} --apply'")
        assert Path(rotate["stdin_file"]).read_bytes() == f"{NEW}\n".encode()


def test_wrapper_never_puts_the_password_in_an_argument_or_its_output(tmp_path: Path) -> None:
    result, calls = _wrapper(tmp_path, "--apply")
    assert result.returncode == 0, result.stderr
    assert calls
    assert not any(NEW in arg for call in calls for arg in call["argv"])
    assert NEW not in result.stdout + result.stderr
    # Nothing of it is left in the operator's temporary directory either.
    assert list((tmp_path / "tmp").iterdir()) == []


def test_wrapper_reads_the_secret_once_after_the_checks_and_before_any_node(
    tmp_path: Path,
) -> None:
    result, calls = _wrapper(tmp_path, "--apply")
    assert result.returncode == 0, result.stderr
    kinds = [" ".join(c["argv"][:3]) for c in calls]
    assert kinds == [
        *["compute instances describe"] * 3,
        "secrets versions access",
        *[f"compute ssh {node}" for node in ALL_NODES for _ in range(2)],
    ]
    secret = calls[3]["argv"]
    assert secret[3] == "latest"
    assert "--secret=trustedrouter-clickhouse-password" in secret
    assert "--project=quill-cloud-proxy" in secret


def test_wrapper_without_apply_only_asks_each_node_to_report(tmp_path: Path) -> None:
    result, calls = _wrapper(tmp_path)
    assert result.returncode == 0, result.stderr
    rotations = _ssh(calls)[1::2]
    assert len(rotations) == 3
    for call in rotations:
        assert "--apply" not in _command(call)
        assert _command(call).endswith(f"bash {STAGING}/{REMOTE_SCRIPT} '")
    assert "is current on" not in result.stdout


@pytest.mark.parametrize("step", [1, 2])
def test_wrapper_stops_at_the_first_failing_node(tmp_path: Path, step: int) -> None:
    result, calls = _wrapper(tmp_path, "--apply", FAKE_FAILING_SSH=f"tr-clickhouse-2:{step}")
    assert result.returncode != 0
    assert [c["argv"][2] for c in _ssh(calls)] == [
        "tr-clickhouse-3", "tr-clickhouse-3", *["tr-clickhouse-2"] * step,
    ]
    assert "is current on" not in result.stdout


@pytest.mark.parametrize(
    ("args", "env", "message", "allowed"),
    [
        (["--force"], {}, "usage:", []),
        (["--apply", "extra"], {}, "usage:", []),
        (["--apply"], {"NAME": "tr-clickhouse-2"}, "set NAME and ZONE together", []),
        (["--apply"], {"ZONE": "us-central1-b"}, "set NAME and ZONE together", []),
        (
            ["--apply"],
            {"FAKE_EXTERNAL_IP_NODE": "tr-clickhouse-1"},
            "tr-clickhouse-1 has external IP 34.0.0.9",
            ["compute instances describe"],
        ),
        (
            ["--apply"],
            {"FAKE_SECRET": "too-short"},
            "must be 24 or more characters",
            ["compute instances describe", "secrets versions access"],
        ),
        (
            ["--apply"],
            {"FAKE_SECRET": "two-lines-0123456789abcdef\nsecond-line-0123456789abcdef"},
            "must be 24 or more characters",
            ["compute instances describe", "secrets versions access"],
        ),
        (
            ["--apply"],
            {"FAKE_SECRET": NEW + "\n"},
            "must not end in a newline",
            ["compute instances describe", "secrets versions access"],
        ),
        (
            ["--apply"],
            {"FAKE_SECRET": "-" + NEW},
            "must not start with -",
            ["compute instances describe", "secrets versions access"],
        ),
        (
            ["--apply"],
            {"FAKE_SECRET_UNREADABLE": "1"},
            "",
            ["compute instances describe", "secrets versions access"],
        ),
    ],
    ids=["unknown-flag", "extra-argument", "name-alone", "zone-alone", "external-ip",
         "weak-secret", "two-line-secret", "trailing-newline-secret", "leading-dash-secret",
         "unreadable-secret"],
)
def test_wrapper_refuses_before_touching_any_node(
    tmp_path: Path, args: list[str], env: dict[str, str], message: str, allowed: list[str]
) -> None:
    result, calls = _wrapper(tmp_path, *args, **env)
    assert result.returncode != 0
    assert message in result.stderr
    assert _ssh(calls) == []
    assert {" ".join(c["argv"][:3]) for c in calls} == set(allowed)
    assert env.get("FAKE_SECRET", NEW) not in result.stdout + result.stderr


def test_wrapper_ships_committed_files_only(tmp_path: Path) -> None:
    repo = _checkout(tmp_path)
    with (repo / REMOTE_SCRIPT).open("a") as handle:
        handle.write("# local edit\n")
    result, calls = _wrapper(tmp_path, "--apply", repo=repo)
    assert result.returncode == 1
    assert "has uncommitted changes" in result.stderr
    assert calls == []


def test_wrapper_targets_one_node_with_name_and_zone(tmp_path: Path) -> None:
    result, calls = _wrapper(tmp_path, "--apply", NAME="tr-clickhouse-2", ZONE="us-central1-b")
    assert result.returncode == 0, result.stderr
    assert [c["argv"][2] for c in _ssh(calls)] == ["tr-clickhouse-2", "tr-clickhouse-2"]
    assert all("--zone=us-central1-b" in c["argv"] for c in _ssh(calls))
    assert [c["argv"][3] for c in calls if c["argv"][:3] == ["compute", "instances", "describe"]] == [
        "tr-clickhouse-2"
    ]


def test_both_scripts_are_executable_and_the_wrapper_and_node_agree_on_the_rule() -> None:
    rule = r'=~ ^[A-Za-z0-9_][A-Za-z0-9_-]{23,}$ ]]'
    for script in (WRAPPER, NODE_SCRIPT):
        assert _mode(script) & 0o111, script
        assert rule in script.read_text(), script
