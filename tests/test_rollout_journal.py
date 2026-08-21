"""Focused tests for the atomic rollout journal and recovery records."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts/deploy/rollout_journal.py"


def _run(*arguments: str | Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(TOOL), *(str(value) for value in arguments)],
        check=check,
        capture_output=True,
        text=True,
    )


def _manifest(tmp_path: Path) -> Path:
    prior = tmp_path / "map.prior.json"
    candidate = tmp_path / "map.candidate.json"
    prior.write_text('{"name":"prior"}\n', encoding="utf-8")
    candidate.write_text('{"name":"candidate"}\n', encoding="utf-8")
    prior.chmod(0o600)
    candidate.chmod(0o600)
    manifest = tmp_path / "rollout.json"
    manifest.write_text(
        json.dumps(
            {
                "project_id": "quill-cloud-proxy",
                "release": "release-1",
                "promotion_state": "promotion-state.json",
                "url_map": {
                    "name": "trusted-router-map",
                    "prior_sha256": "1" * 64,
                    "candidate_sha256": "2" * 64,
                    "prior_snapshot": prior.name,
                    "candidate_snapshot": candidate.name,
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest.chmod(0o600)
    return manifest


def test_active_lease_excludes_every_second_operation(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    state = tmp_path / "promotion-state.json"

    _run("lease-acquire", state, manifest, "operation-one", "promote")
    assert state.stat().st_mode & 0o777 == 0o600

    for owner in ("operation-two", "operation-one"):
        conflict = _run(
            "lease-acquire", state, manifest, owner, "rollback", check=False
        )
        assert conflict.returncode != 0
        assert "active lease" in conflict.stderr

    ownerless = _run(
        "append", state, manifest, "traffic", "public", "service", "region", "10",
        check=False,
    )
    assert ownerless.returncode != 0
    assert "active lease owner" in ownerless.stderr

    _run(
        "append", state, manifest, "traffic", "public", "service", "region", "10",
        "--owner", "operation-one",
    )
    _run("lease-refresh", state, manifest, "operation-one", "--ttl-seconds", "60")
    _run("lease-release", state, manifest, "operation-one")
    value = json.loads(state.read_text(encoding="utf-8"))
    assert value["lease"] is None
    assert value["attempts"][0]["target"] == "10"


def test_expired_lease_requires_explicit_takeover(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    state = tmp_path / "promotion-state.json"
    _run("lease-acquire", state, manifest, "operation-one", "promote")
    value = json.loads(state.read_text(encoding="utf-8"))
    value["lease"]["expires_at"] = "2000-01-01T00:00:00+00:00"
    state.write_text(json.dumps(value), encoding="utf-8")
    state.chmod(0o600)

    refused = _run(
        "lease-acquire", state, manifest, "operation-two", "rollback", check=False
    )
    assert refused.returncode != 0
    assert "explicit reconciled takeover" in refused.stderr

    _run(
        "lease-acquire", state, manifest, "operation-two", "rollback",
        "--allow-expired-takeover",
    )
    assert json.loads(state.read_text(encoding="utf-8"))["lease"]["owner"] == (
        "operation-two"
    )


def test_inflight_provider_mutation_permanently_fences_expired_takeover(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    state = tmp_path / "promotion-state.json"
    _run("lease-acquire", state, manifest, "operation-one", "promote")
    _run(
        "lease-mutation-begin",
        state,
        manifest,
        "operation-one",
        "url-map",
    )

    release = _run(
        "lease-release", state, manifest, "operation-one", check=False
    )
    assert release.returncode != 0
    assert "in-flight provider mutation" in release.stderr

    value = json.loads(state.read_text(encoding="utf-8"))
    value["lease"]["expires_at"] = "2000-01-01T00:00:00+00:00"
    state.write_text(json.dumps(value), encoding="utf-8")
    state.chmod(0o600)
    takeover = _run(
        "lease-acquire",
        state,
        manifest,
        "operation-two",
        "rollback",
        "--allow-expired-takeover",
        check=False,
    )
    assert takeover.returncode != 0
    assert "unresolved provider mutation" in takeover.stderr


def test_state_schema_rejects_unknown_fields(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    state = tmp_path / "promotion-state.json"
    _run("lease-acquire", state, manifest, "operation-one", "promote")
    value = json.loads(state.read_text(encoding="utf-8"))
    value["unbound"] = True
    state.write_text(json.dumps(value), encoding="utf-8")

    result = _run("validate", state, manifest, check=False)
    assert result.returncode != 0
    assert "fields differ" in result.stderr


def test_bundle_and_authority_are_atomic_exact_records(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    descriptor = tmp_path / "bundle.json"
    authority = tmp_path / "authority.json"

    _run("bundle-write", manifest, descriptor)
    assert descriptor.stat().st_mode & 0o777 == 0o600
    validated = _run("bundle-validate", descriptor, tmp_path)
    assert Path(validated.stdout.strip()) == manifest

    bundle_uri = "gs://trusted-router-rollout/releases/release-0001"
    _run("authority-write", manifest, authority, "active", bundle_uri)
    assert authority.stat().st_mode & 0o777 == 0o600
    _run("authority-validate", authority, manifest, "active", bundle_uri)
    closed_mismatch = _run(
        "authority-validate", authority, manifest, "closed", bundle_uri, check=False
    )
    assert closed_mismatch.returncode != 0

    (tmp_path / "map.candidate.json").write_text(
        '{"name":"tampered"}\n', encoding="utf-8"
    )
    tampered = _run("bundle-validate", descriptor, tmp_path, check=False)
    assert tampered.returncode != 0
    assert "validation failed" in tampered.stderr
