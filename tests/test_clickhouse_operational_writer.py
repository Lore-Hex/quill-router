"""G7 in docs/design/clickhouse-high-availability.md: the operational writer
account existed on tr-clickhouse-1 only, so a direct sink through the load
balancer would fail authentication on two of every three connections. The
installer now covers every replica, one at a time, without restarting servers.

The script runs for real against a recording ``gcloud`` stub: describe calls
answer with no external IP (or with one, to prove the refusal), the secret
answers without a trailing newline, and every ``compute ssh`` is logged with
its stdin.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/deploy/clickhouse_operational_writer.sh"
NODES = [
    ("tr-clickhouse-1", "us-central1-a"),
    ("tr-clickhouse-2", "us-central1-b"),
    ("tr-clickhouse-3", "us-central1-c"),
]

FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
log="$FAKE_GCLOUD_LOG"
args=("$@")
stdin_text=""
if [ "${1:-}" = "compute" ] && [ "${2:-}" = "ssh" ]; then
  stdin_text="$(cat)"
fi
python3 - "$log" "$stdin_text" "${args[@]}" <<'PY'
import json, sys
log, stdin_text, *argv = sys.argv[1:]
with open(log, "a") as handle:
    handle.write(json.dumps({"argv": argv, "stdin": stdin_text}) + "\n")
PY
case "$*" in
  "compute instances describe "*)
    for arg in "$@"; do
      if [ "$arg" = "${FAKE_EXTERNAL_IP_NODE:-none}" ]; then printf '34.0.0.9'; fi
    done
    ;;
  "secrets versions access "*) printf 'fixture-writer-credential' ;;
esac
"""


def _run(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess[str], list[dict]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(FAKE_GCLOUD)
    gcloud.chmod(0o755)
    log = tmp_path / "gcloud.jsonl"
    log.write_text("")
    run_env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "FAKE_GCLOUD_LOG": str(log),
        **env,
    }
    result = subprocess.run(  # noqa: S603 - fixed script under test
        [shutil.which("bash") or "/bin/bash", str(SCRIPT)],
        env=run_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return result, calls


def _ssh_targets(calls: list[dict]) -> list[tuple[str, str, str]]:
    targets = []
    for call in calls:
        argv = call["argv"]
        if argv[:2] != ["compute", "ssh"]:
            continue
        zone = next(arg.split("=", 1)[1] for arg in argv if arg.startswith("--zone="))
        command = next(arg for arg in argv if arg.startswith("--command="))
        step = "install" if "users.d/tr-ops-ingest.xml" in command else "grant-check"
        targets.append((argv[2], zone, step))
    return targets


def test_every_replica_gets_the_account_one_node_at_a_time(tmp_path: Path) -> None:
    result, calls = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert _ssh_targets(calls) == [
        (name, zone, step) for name, zone in NODES for step in ("install", "grant-check")
    ]
    ssh_calls = [call for call in calls if call["argv"][:2] == ["compute", "ssh"]]
    for call in ssh_calls:
        command = next(arg for arg in call["argv"] if arg.startswith("--command="))
        # users.d reloads on the fly; restarting each node in a row would
        # break the one-zone-at-a-time rule.
        assert "systemctl restart" not in command
    installs = [call for call in ssh_calls if "users.d/tr-ops-ingest.xml" in str(call["argv"])]
    # The same rendered users.d file, carrying the credential's hash and never
    # the credential, goes to every node.
    assert len({call["stdin"] for call in installs}) == 1
    assert "<tr_ops_ingest>" in installs[0]["stdin"]
    assert "fixture-writer-credential" not in installs[0]["stdin"]
    checks = [call for call in ssh_calls if call not in installs]
    assert all(
        call["stdin"] == "CH_OPS_INGEST_PASSWORD=fixture-writer-credential" for call in checks
    )
    # Every node is checked for an external IP before any node is changed.
    first_ssh = calls.index(ssh_calls[0])
    describes = [
        call["argv"][3]
        for call in calls[:first_ssh]
        if call["argv"][:3] == ["compute", "instances", "describe"]
    ]
    assert describes == [name for name, _ in NODES]


def test_name_and_zone_target_one_node(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, NAME="tr-clickhouse-2", ZONE="us-central1-b")

    assert result.returncode == 0, result.stderr
    assert _ssh_targets(calls) == [
        ("tr-clickhouse-2", "us-central1-b", "install"),
        ("tr-clickhouse-2", "us-central1-b", "grant-check"),
    ]


@pytest.mark.parametrize("env", [{"NAME": "tr-clickhouse-2"}, {"ZONE": "us-central1-b"}])
def test_name_or_zone_alone_is_refused(tmp_path: Path, env: dict[str, str]) -> None:
    result, calls = _run(tmp_path, **env)

    assert result.returncode != 0
    assert "set NAME and ZONE together" in result.stderr
    assert calls == []


def test_an_external_ip_on_any_node_refuses_before_any_change(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, FAKE_EXTERNAL_IP_NODE="tr-clickhouse-3")

    assert result.returncode != 0
    assert "tr-clickhouse-3 has external IP 34.0.0.9" in result.stderr
    assert _ssh_targets(calls) == []


# The grant check runs on the node. Execute the exact remote command captured
# above, with sudo, sleep and clickhouse-client stubbed, to pin its polling.

CLICKHOUSE_STUB = r"""#!/usr/bin/env bash
# users.d reloads on the fly but not instantly: INSERT is refused for the first
# $INSERT_REFUSED attempts and SELECT still allowed for the first $SELECT_ALLOWED.
state="$STUB_STATE"
query="${@: -1}"
case "$query" in
  INSERT*)
    cat > /dev/null
    n=$(( $(cat "$state/inserts" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$state/inserts"
    [ "$n" -gt "${INSERT_REFUSED:-0}" ]
    ;;
  SELECT*)
    n=$(( $(cat "$state/selects" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$state/selects"
    [ "$n" -le "${SELECT_ALLOWED:-0}" ]
    ;;
  *) exit 2 ;;
esac
"""


def _grant_check(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess[str], int, int]:
    result, calls = _run(tmp_path, NAME="tr-clickhouse-2", ZONE="us-central1-b")
    assert result.returncode == 0, result.stderr
    [check] = [
        call
        for call in calls
        if call["argv"][:2] == ["compute", "ssh"]
        and "users.d/tr-ops-ingest.xml" not in str(call["argv"])
    ]
    command = next(arg for arg in check["argv"] if arg.startswith("--command="))
    command = command.removeprefix("--command=")
    remote = tmp_path / "remote"
    remote.mkdir()
    for name, body in {
        "sudo": '#!/usr/bin/env bash\nexec "$@"\n',
        "sleep": "#!/usr/bin/env bash\nexit 0\n",
        "clickhouse-client": CLICKHOUSE_STUB,
    }.items():
        (remote / name).write_text(body)
        (remote / name).chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    command = command.replace("/usr/bin/clickhouse-client", str(remote / "clickhouse-client"))
    # The node-side path, redirected into this test's directory.
    command = command.replace("/tmp/tr-ops-ingest.env", str(tmp_path / "ops-ingest.env"))  # noqa: S108
    run = subprocess.run(  # noqa: S603 - the script's own remote command, stubbed
        ["/bin/sh", "-c", command],
        input=check["stdin"] + "\n",
        env={"PATH": f"{remote}:{os.environ['PATH']}", "STUB_STATE": str(state), **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    inserts = int((state / "inserts").read_text()) if (state / "inserts").exists() else 0
    selects = int((state / "selects").read_text()) if (state / "selects").exists() else 0
    # The credential file is removed once read.
    assert not (tmp_path / "ops-ingest.env").exists()
    return run, inserts, selects


def test_grant_check_passes_at_once_when_grants_are_already_right(tmp_path: Path) -> None:
    run, inserts, selects = _grant_check(tmp_path)

    assert run.returncode == 0, run.stderr
    assert (inserts, selects) == (1, 1)


@pytest.mark.parametrize(
    ("env", "attempts"),
    [({"SELECT_ALLOWED": "3"}, 4), ({"INSERT_REFUSED": "2"}, 3)],
    ids=["old-select-grant-still-loaded", "insert-grant-not-yet-loaded"],
)
def test_grant_check_waits_for_the_reload_instead_of_failing(
    tmp_path: Path, env: dict[str, str], attempts: int
) -> None:
    # Review finding: authenticating proved only that the account existed, so a
    # check right after install could see the old grants and abort the rollout.
    run, inserts, selects = _grant_check(tmp_path, **env)

    assert run.returncode == 0, run.stderr
    assert inserts == selects == attempts


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"SELECT_ALLOWED": "1000"}, "tr_ops_ingest can still SELECT after 60 s; grants too broad"),
        ({"INSERT_REFUSED": "1000"}, "tr_ops_ingest cannot INSERT into tr.operational_outbox_quarantine after 60 s"),
    ],
    ids=["select-never-revoked", "insert-never-granted"],
)
def test_grant_check_fails_when_the_grants_never_converge(
    tmp_path: Path, env: dict[str, str], message: str
) -> None:
    run, inserts, selects = _grant_check(tmp_path, **env)

    assert run.returncode != 0
    assert message in run.stderr
    assert inserts == selects == 30
