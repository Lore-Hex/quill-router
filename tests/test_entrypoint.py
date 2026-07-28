"""The container entrypoint is the only cross-cloud credential seam.

It runs on AWS and Azure test networks where there is no GCP metadata server,
so a mistake here is a deployment that either cannot reach Spanner at all or —
worse — reaches it with a long-lived key that the whole Workload Identity
Federation design exists to eliminate. Shell is untested by default in this
repo, so these tests execute the real script.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

ENTRYPOINT = Path(__file__).resolve().parents[1] / "scripts" / "entrypoint.sh"

# A realistic keyless config: it names the pool/provider/service account and
# points at the *local* cloud's metadata for proof. No key material.
AWS_WIF_CONFIG = {
    "type": "external_account",
    "audience": (
        "//iam.googleapis.com/projects/44325983244/locations/global/"
        "workloadIdentityPools/multicloud/providers/aws"
    ),
    "subject_token_type": "urn:ietf:params:aws:token-type:aws4_request",
    "token_url": "https://sts.googleapis.com/v1/token",
    "credential_source": {
        "environment_id": "aws1",
        "regional_cred_verification_url": (
            "https://sts.{region}.amazonaws.com?Action=GetCallerIdentity&Version=2011-06-15"
        ),
    },
}


def _run(env: dict[str, str], *argv: str) -> subprocess.CompletedProcess[str]:
    """Run the entrypoint with a harmless command in place of uvicorn."""
    child_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env}
    return subprocess.run(  # noqa: S603,S607 - test-owned argv, bash resolved from PATH
        ["bash", str(ENTRYPOINT), *argv],
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_exec_passes_through_without_credential_config() -> None:
    """On GCP nothing is set and the entrypoint must stay out of the way."""
    result = _run({}, "printenv")
    assert result.returncode == 0, result.stderr
    exported = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in exported


def test_materialises_keyless_config_and_points_adc_at_it(tmp_path: Path) -> None:
    target = tmp_path / "cred.json"
    result = _run(
        {
            "TR_GOOGLE_EXTERNAL_CREDENTIAL_CONFIG": json.dumps(AWS_WIF_CONFIG),
            "TR_GOOGLE_EXTERNAL_CREDENTIAL_CONFIG_PATH": str(target),
        },
        "printenv",
        "GOOGLE_APPLICATION_CREDENTIALS",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(target)
    # Round-trips exactly: a truncated or shell-mangled config fails at token
    # exchange time, in a deployed container, with an opaque error.
    assert json.loads(target.read_text()) == AWS_WIF_CONFIG


def test_config_file_is_not_world_readable(tmp_path: Path) -> None:
    """Not key material, but it names an impersonation target — don't leak it
    to every other process in the container."""
    target = tmp_path / "cred.json"
    result = _run(
        {
            "TR_GOOGLE_EXTERNAL_CREDENTIAL_CONFIG": json.dumps(AWS_WIF_CONFIG),
            "TR_GOOGLE_EXTERNAL_CREDENTIAL_CONFIG_PATH": str(target),
        },
        "true",
    )
    assert result.returncode == 0, result.stderr
    mode = stat.S_IMODE(target.stat().st_mode)
    assert not mode & (stat.S_IRGRP | stat.S_IROTH), oct(mode)


def test_refuses_a_service_account_key(tmp_path: Path) -> None:
    """The whole point of this seam is to remove long-lived keys. If someone
    pastes a key JSON in, fail loudly rather than silently accepting it."""
    target = tmp_path / "cred.json"
    key_json = json.dumps(
        {
            "type": "service_account",
            "project_id": "quill-cloud-proxy",
            "private_key": "-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----\n",
            "client_email": "tr@quill-cloud-proxy.iam.gserviceaccount.com",
        }
    )
    result = _run(
        {
            "TR_GOOGLE_EXTERNAL_CREDENTIAL_CONFIG": key_json,
            "TR_GOOGLE_EXTERNAL_CREDENTIAL_CONFIG_PATH": str(target),
        },
        "printenv",
    )
    assert result.returncode == 1
    assert "key material" in result.stderr
    # And it must not have been written to disk on the way to failing.
    assert not target.exists()
