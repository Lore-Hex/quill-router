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
