"""scripts/deploy/ensure_allyrouter_alias.sh on a proxy with and without a certificate map.

The script runs with PATH holding only a recording fake ``gcloud`` and
``python3``: any other external command fails, and no real cloud CLI is
reachable. The fake answers from a JSON state file.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "deploy" / "ensure_allyrouter_alias.sh"
HOSTS = ["allyrouter.com", "www.allyrouter.com", "status.allyrouter.com", "trust.allyrouter.com"]
MAP = "//certificatemanager.googleapis.com/projects/p/locations/global/certificateMaps/control"
CERT = "projects/p/locations/global/certificates/control-allyrouter-com"
OTHER = "projects/p/locations/global/certificates/www-allyrouter-com"
READS = [
    [
        "compute", "target-https-proxies", "describe", "trusted-router-control-https-proxy",
        "--global", "--project=quill-cloud-proxy", "--format=value(certificateMap)",
    ],
    [
        "certificate-manager", "maps", "entries", "list", "--map=control",
        "--location=global", "--project=quill-cloud-proxy", "--format=json",
    ],
    ["certificate-manager", "certificates", "list", "--location=global", "--project=quill-cloud-proxy", "--format=json"],
]

FAKE_GCLOUD = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
state = json.loads(open(os.environ["FAKE_STATE"]).read())
joined = " ".join(args)
if joined.startswith("compute target-https-proxies describe"):
    if "--format=value(certificateMap)" in args:
        print(state.get("certificate_map", ""))
    else:
        print(";".join(state.get("attached", [])))
elif joined.startswith("certificate-manager maps entries list"):
    print(json.dumps(state["entries"]))
elif joined.startswith("certificate-manager certificates list"):
    print(json.dumps(state["certificates"]))
elif joined.startswith("compute ssl-certificates describe"):
    sys.exit(0 if state.get("classic_exists") else 1)
"""


def run(tmp_path: Path, state: dict) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "gcloud"
    fake.write_text(FAKE_GCLOUD)
    fake.chmod(0o755)
    (bin_dir / "python3").symlink_to(sys.executable)
    (tmp_path / "state.json").write_text(json.dumps(state))
    log = tmp_path / "gcloud.log"
    log.write_text("")
    (tmp_path / "cloudsdk").mkdir()
    result = subprocess.run(  # noqa: S603 - fixed script path from this checkout
        ["/bin/bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": str(bin_dir),
            "HOME": str(tmp_path),
            "CLOUDSDK_CONFIG": str(tmp_path / "cloudsdk"),
            "FAKE_LOG": str(log),
            "FAKE_STATE": str(tmp_path / "state.json"),
        },
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return result, calls


def map_state(
    entries: dict[str, str],
    states: dict[str, str] | None = None,
    pem: str = "",
    entry_states: dict[str, str] | None = None,
) -> dict:
    states = states or {CERT: "ACTIVE"}
    return {
        "certificate_map": MAP,
        "entries": [{"matcher": "PRIMARY", "certificates": ["primary"], "state": "ACTIVE"}]
        + [
            {"hostname": host, "certificates": [cert], "state": (entry_states or {}).get(host, "ACTIVE")}
            for host, cert in entries.items()
        ],
        "certificates": [
            {"name": name, "managed": {"state": state}, "pemCertificate": pem}
            for name, state in states.items()
        ],
    }


def classic_writes(calls: list[list[str]]) -> list[list[str]]:
    return [
        c
        for c in calls
        if c[:3] == ["compute", "ssl-certificates", "create"]
        or c[:3] == ["compute", "target-https-proxies", "update"]
    ]


def test_with_a_covering_map_it_succeeds_and_writes_no_classic_certificate(tmp_path: Path) -> None:
    result, calls = run(tmp_path, map_state({"allyrouter.com": CERT, "*.allyrouter.com": CERT}))

    assert result.returncode == 0, result.stderr
    assert "every hostname has an ACTIVE entry and certificate there" in result.stdout
    assert calls == READS


def test_with_a_map_missing_the_wildcard_it_fails_naming_the_hosts(tmp_path: Path) -> None:
    result, calls = run(tmp_path, map_state({"allyrouter.com": CERT}))

    assert result.returncode == 1
    assert "no ACTIVE entry with an ACTIVE certificate for: www.allyrouter.com, status.allyrouter.com, trust.allyrouter.com" in result.stderr
    assert classic_writes(calls) == []


def test_with_a_map_whose_certificate_is_not_active_it_fails(tmp_path: Path) -> None:
    result, calls = run(
        tmp_path, map_state({"allyrouter.com": CERT, "*.allyrouter.com": CERT}, {CERT: "PROVISIONING"})
    )

    assert result.returncode == 1
    assert "no ACTIVE entry with an ACTIVE certificate for: " + ", ".join(HOSTS) in result.stderr
    assert classic_writes(calls) == []


def test_an_exact_entry_is_used_before_the_wildcard(tmp_path: Path) -> None:
    # www.allyrouter.com's own entry has a certificate that is not ACTIVE; the
    # ACTIVE wildcard does not serve it, because Certificate Manager uses the
    # exact entry first.
    result, calls = run(
        tmp_path,
        map_state(
            {"allyrouter.com": CERT, "www.allyrouter.com": OTHER, "*.allyrouter.com": CERT},
            {CERT: "ACTIVE", OTHER: "PROVISIONING"},
        ),
    )

    assert result.returncode == 1
    assert "no ACTIVE entry with an ACTIVE certificate for: www.allyrouter.com\n" in result.stderr
    assert calls == READS


def test_a_pending_exact_entry_is_used_before_the_wildcard(tmp_path: Path) -> None:
    # The exact entry is PENDING and both certificates are ACTIVE: the exact
    # entry is still the one selected, so the host fails until it is ACTIVE.
    result, calls = run(
        tmp_path,
        map_state(
            {"allyrouter.com": CERT, "www.allyrouter.com": OTHER, "*.allyrouter.com": CERT},
            {CERT: "ACTIVE", OTHER: "ACTIVE"},
            entry_states={"www.allyrouter.com": "PENDING"},
        ),
    )

    assert result.returncode == 1
    assert "no ACTIVE entry with an ACTIVE certificate for: www.allyrouter.com\n" in result.stderr
    assert calls == READS


def test_a_pending_map_entry_fails_even_with_an_active_certificate(tmp_path: Path) -> None:
    # A PENDING entry has not reached every frontend yet.
    result, calls = run(
        tmp_path,
        map_state({"allyrouter.com": CERT, "*.allyrouter.com": CERT}, entry_states={"*.allyrouter.com": "PENDING"}),
    )

    assert result.returncode == 1
    assert (
        "no ACTIVE entry with an ACTIVE certificate for: www.allyrouter.com, status.allyrouter.com, "
        "trust.allyrouter.com\n" in result.stderr
    )
    assert calls == READS


def test_a_certificate_list_larger_than_an_environment_string_is_read(tmp_path: Path) -> None:
    # Linux limits one environment string to 128 KiB; the list reaches python3
    # on stdin, so its size does not matter.
    result, calls = run(
        tmp_path,
        map_state({"allyrouter.com": CERT, "*.allyrouter.com": CERT}, pem="x" * 300_000),
    )

    assert result.returncode == 0, result.stderr
    assert calls == READS


def test_without_a_map_it_keeps_the_classic_certificate_path(tmp_path: Path) -> None:
    result, calls = run(
        tmp_path, {"classic_exists": True, "attached": ["trusted-router-apex-cert-v2", "allyrouter-control-cert-v1"]}
    )

    assert result.returncode == 0, result.stderr
    assert ["compute", "ssl-certificates", "describe"] in [c[:3] for c in calls]
    assert not [c for c in calls if c[0] == "certificate-manager"]
    assert classic_writes(calls) == []
