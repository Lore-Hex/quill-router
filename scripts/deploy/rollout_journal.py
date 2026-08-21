#!/usr/bin/env python3
"""Atomic, secret-free rollout journal, lease, authority, and bundle records."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _manifest_identity(manifest_path: Path) -> dict[str, Any]:
    manifest = _read(manifest_path)
    url_map = manifest.get("url_map") or {}
    return {
        "schema_version": 1,
        "manifest_sha256": _digest(manifest_path),
        "project_id": manifest.get("project_id"),
        "url_map_name": url_map.get("name"),
        "prior_url_map_sha256": url_map.get("prior_sha256"),
        "candidate_url_map_sha256": url_map.get("candidate_sha256"),
    }


def _state(path: Path, manifest_path: Path) -> dict[str, Any]:
    identity = _manifest_identity(manifest_path)
    try:
        state = _read(path)
    except FileNotFoundError:
        state = {**identity, "attempts": [], "lease": None}
    if not isinstance(state, dict):
        raise ValueError("promotion state must be an object")
    # Older local journals did not include the explicit lease member. Their
    # manifest identity is still checked below before they are upgraded.
    if "lease" not in state:
        state["lease"] = None
    if set(state) != {*identity, "attempts", "lease"}:
        raise ValueError("promotion state fields differ from schema v1")
    for key, expected in identity.items():
        if state.get(key) != expected:
            raise ValueError("promotion state belongs to a different rollout manifest")
    if not isinstance(state.get("attempts"), list):
        raise ValueError("promotion state attempts are invalid")
    for attempt in state["attempts"]:
        if not isinstance(attempt, dict) or set(attempt) != {
            "attempted_at",
            "operation",
            "surface",
            "service",
            "region",
            "target",
        }:
            raise ValueError("promotion state attempt fields differ")
        _parse_timestamp(attempt["attempted_at"])
        _validate_operation(attempt["operation"])
        for key in ("surface", "service", "region"):
            if attempt[key] is not None and not isinstance(attempt[key], str):
                raise ValueError("promotion state attempt identity is invalid")
        if not isinstance(attempt["target"], str):
            raise ValueError("promotion state attempt target is invalid")
    if state["lease"] is not None and not isinstance(state["lease"], dict):
        raise ValueError("promotion state lease is invalid")
    if state["lease"] is not None:
        lease = state["lease"]
        if "mutation" not in lease:
            lease["mutation"] = None
        if set(lease) != {
            "owner",
            "operation",
            "acquired_at",
            "expires_at",
            "mutation",
        }:
            raise ValueError("promotion state lease fields differ")
        _validate_owner(lease["owner"])
        _validate_operation(lease["operation"])
        _parse_timestamp(lease["acquired_at"])
        _parse_timestamp(lease["expires_at"])
        mutation = lease["mutation"]
        if mutation is not None:
            if not isinstance(mutation, dict) or set(mutation) != {
                "operation",
                "started_at",
            }:
                raise ValueError("promotion state in-flight mutation fields differ")
            _validate_operation(mutation["operation"])
            _parse_timestamp(mutation["started_at"])
    return state


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("lease expiry is absent")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("lease expiry must include a timezone")
    return parsed.astimezone(UTC)


def _validate_owner(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9._:-]{8,160}", value
    ):
        raise ValueError("rollout operation owner is invalid")
    return value


def _validate_operation(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-z][a-z0-9:-]{0,127}", value
    ):
        raise ValueError("rollout operation is invalid")
    return value


def _validate_gcs_uri(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("rollout bundle URI is invalid")
    match = re.fullmatch(
        r"gs://[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]/([^\x00-\x1f\x7f]+)",
        value,
    )
    if (
        not match
        or value.endswith("/")
        or "//" in match.group(1)
        or any(part in {"", ".", ".."} for part in match.group(1).split("/"))
    ):
        raise ValueError("rollout bundle URI is invalid")
    return value


def _acquire(args: argparse.Namespace) -> None:
    _validate_owner(args.owner)
    _validate_operation(args.operation)
    state = _state(args.state, args.manifest)
    current = state.get("lease")
    now = _now()
    if current:
        expired = _parse_timestamp(current.get("expires_at")) <= now
        if not expired:
            raise ValueError("another rollout operation holds the active lease")
        if not args.allow_expired_takeover:
            raise ValueError("expired rollout lease requires explicit reconciled takeover")
        if current.get("mutation") is not None:
            raise ValueError(
                "expired rollout lease has an unresolved provider mutation; "
                "cloud-owner reconciliation is required"
            )
    state["lease"] = {
        "owner": args.owner,
        "operation": args.operation,
        "acquired_at": _timestamp(now),
        "expires_at": _timestamp(now + timedelta(seconds=args.ttl_seconds)),
        "mutation": None,
    }
    _atomic_write(args.state, state)


def _refresh(args: argparse.Namespace) -> None:
    _validate_owner(args.owner)
    state = _state(args.state, args.manifest)
    lease = state.get("lease") or {}
    now = _now()
    if lease.get("owner") != args.owner:
        raise ValueError("rollout operation no longer owns the active lease")
    if _parse_timestamp(lease.get("expires_at")) <= now:
        raise ValueError("rollout operation lease expired before refresh")
    lease["expires_at"] = _timestamp(now + timedelta(seconds=args.ttl_seconds))
    state["lease"] = lease
    _atomic_write(args.state, state)


def _assert_lease(args: argparse.Namespace) -> None:
    _validate_owner(args.owner)
    state = _state(args.state, args.manifest)
    lease = state.get("lease") or {}
    if lease.get("owner") != args.owner:
        raise ValueError("rollout operation no longer owns the active lease")
    if _parse_timestamp(lease.get("expires_at")) <= _now():
        raise ValueError("rollout operation lease expired")


def _release(args: argparse.Namespace) -> None:
    _validate_owner(args.owner)
    state = _state(args.state, args.manifest)
    lease = state.get("lease") or {}
    if lease.get("owner") != args.owner:
        raise ValueError("rollout operation cannot release another owner's lease")
    if lease.get("mutation") is not None:
        raise ValueError("rollout operation cannot release an in-flight provider mutation")
    state["lease"] = None
    _atomic_write(args.state, state)


def _mutation_begin(args: argparse.Namespace) -> None:
    _validate_owner(args.owner)
    _validate_operation(args.operation)
    state = _state(args.state, args.manifest)
    lease = state.get("lease") or {}
    if lease.get("owner") != args.owner:
        raise ValueError("provider mutation is not owned by the active lease")
    if _parse_timestamp(lease.get("expires_at")) <= _now():
        raise ValueError("rollout operation lease expired before provider mutation")
    if lease.get("mutation") is not None:
        raise ValueError("another provider mutation is already in flight")
    lease["mutation"] = {
        "operation": args.operation,
        "started_at": _timestamp(),
    }
    state["lease"] = lease
    _atomic_write(args.state, state)


def _mutation_end(args: argparse.Namespace) -> None:
    _validate_owner(args.owner)
    _validate_operation(args.operation)
    state = _state(args.state, args.manifest)
    lease = state.get("lease") or {}
    if lease.get("owner") != args.owner:
        raise ValueError("provider mutation is not owned by the active lease")
    if _parse_timestamp(lease.get("expires_at")) <= _now():
        raise ValueError(
            "rollout lease expired with a provider mutation in flight; "
            "cloud-owner reconciliation is required"
        )
    mutation = lease.get("mutation")
    if not isinstance(mutation, dict) or mutation.get("operation") != args.operation:
        raise ValueError("provider mutation completion does not match the active fence")
    lease["mutation"] = None
    state["lease"] = lease
    _atomic_write(args.state, state)


def _append(args: argparse.Namespace) -> None:
    state = _state(args.state, args.manifest)
    lease = state.get("lease") or {}
    if lease and not args.owner:
        raise ValueError("rollout attempt append requires the active lease owner")
    if args.owner:
        _validate_owner(args.owner)
    if args.owner and lease.get("owner") != args.owner:
        raise ValueError("rollout attempt append is not owned by the active lease")
    _validate_operation(args.operation)
    state["attempts"].append(
        {
            "attempted_at": _timestamp(),
            "operation": args.operation,
            "surface": args.surface or None,
            "service": args.service or None,
            "region": args.region or None,
            "target": args.target,
        }
    )
    _atomic_write(args.state, state)


def _init(args: argparse.Namespace) -> None:
    if args.state.exists():
        _state(args.state, args.manifest)
        return
    _atomic_write(args.state, _state(args.state, args.manifest))


def _bundle_write(args: argparse.Namespace) -> None:
    manifest = _read(args.manifest)
    manifest_dir = args.manifest.parent.resolve()
    names = [
        args.manifest.name,
        manifest["url_map"]["prior_snapshot"],
        manifest["url_map"]["candidate_snapshot"],
    ]
    promotion_state = manifest.get("promotion_state")
    if (
        not isinstance(promotion_state, str)
        or Path(promotion_state).name != promotion_state
        or promotion_state in {*names, "bundle.json", "authority.json"}
    ):
        raise ValueError("recovery bundle promotion-state filename is unsafe")
    if len(names) != len(set(names)):
        raise ValueError("recovery bundle filenames must be distinct")
    files = []
    for name in names:
        path = manifest_dir / name
        if path.resolve().parent != manifest_dir or not path.is_file():
            raise ValueError(f"recovery bundle file is unsafe or absent: {name}")
        files.append({"name": name, "sha256": _digest(path)})
    _atomic_write(
        args.output,
        {
            "schema_version": 1,
            "kind": "trusted-router-rollout-recovery-bundle",
            "project_id": manifest["project_id"],
            "release": manifest["release"],
            "manifest": args.manifest.name,
            "manifest_sha256": _digest(args.manifest),
            "promotion_state": promotion_state,
            "files": files,
        },
    )


def _bundle_validate(args: argparse.Namespace) -> None:
    descriptor = _read(args.descriptor)
    required = {
        "schema_version",
        "kind",
        "project_id",
        "release",
        "manifest",
        "manifest_sha256",
        "promotion_state",
        "files",
    }
    if not isinstance(descriptor, dict) or set(descriptor) != required:
        raise ValueError("recovery bundle descriptor fields differ")
    if descriptor["schema_version"] != 1 or descriptor["kind"] != (
        "trusted-router-rollout-recovery-bundle"
    ):
        raise ValueError("recovery bundle descriptor schema differs")
    names: set[str] = set()
    for item in descriptor["files"]:
        if not isinstance(item, dict) or set(item) != {"name", "sha256"}:
            raise ValueError("recovery bundle file record differs")
        name = item["name"]
        path = args.directory / name
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or name in names
            or not path.is_file()
            or _digest(path) != item["sha256"]
        ):
            raise ValueError(f"recovery bundle file validation failed: {name!r}")
        names.add(name)
    if descriptor["manifest"] not in names:
        raise ValueError("recovery bundle manifest record is absent")
    promotion_state = descriptor["promotion_state"]
    if (
        not isinstance(promotion_state, str)
        or Path(promotion_state).name != promotion_state
        or promotion_state in names
    ):
        raise ValueError("recovery bundle promotion-state record is invalid")
    manifest_path = args.directory / descriptor["manifest"]
    if _digest(manifest_path) != descriptor["manifest_sha256"]:
        raise ValueError("recovery bundle manifest digest differs")
    print(manifest_path)


def _authority_write(args: argparse.Namespace) -> None:
    manifest = _read(args.manifest)
    _validate_gcs_uri(args.bundle_uri)
    promotion_state = manifest.get("promotion_state")
    if not isinstance(promotion_state, str) or Path(promotion_state).name != promotion_state:
        raise ValueError("rollout authority promotion-state filename is invalid")
    _atomic_write(
        args.output,
        {
            "schema_version": 1,
            "kind": "trusted-router-rollout-authority",
            "project_id": manifest["project_id"],
            "url_map_name": manifest["url_map"]["name"],
            "manifest_sha256": _digest(args.manifest),
            "release": manifest["release"],
            "bundle_uri": args.bundle_uri,
            "promotion_state": promotion_state,
            "state": args.state_value,
            "updated_at": _timestamp(),
        },
    )


def _authority_validate(args: argparse.Namespace) -> None:
    value = _read(args.authority)
    manifest = _read(args.manifest)
    _validate_gcs_uri(args.bundle_uri)
    promotion_state = manifest.get("promotion_state")
    if not isinstance(promotion_state, str) or Path(promotion_state).name != promotion_state:
        raise ValueError("rollout authority promotion-state filename is invalid")
    expected = {
        "schema_version": 1,
        "kind": "trusted-router-rollout-authority",
        "project_id": manifest["project_id"],
        "url_map_name": manifest["url_map"]["name"],
        "manifest_sha256": _digest(args.manifest),
        "release": manifest["release"],
        "bundle_uri": args.bundle_uri,
        "promotion_state": promotion_state,
    }
    if not isinstance(value, dict) or set(value) != {
        *expected,
        "state",
        "updated_at",
    } or any(value.get(key) != item for key, item in expected.items()):
        raise ValueError("rollout authority belongs to another manifest")
    if value.get("state") != args.expected_state:
        raise ValueError(f"rollout authority is {value.get('state')!r}, not {args.expected_state!r}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate")
    validate.add_argument("state", type=Path)
    validate.add_argument("manifest", type=Path)

    initialize = sub.add_parser("init")
    initialize.add_argument("state", type=Path)
    initialize.add_argument("manifest", type=Path)

    acquire = sub.add_parser("lease-acquire")
    acquire.add_argument("state", type=Path)
    acquire.add_argument("manifest", type=Path)
    acquire.add_argument("owner")
    acquire.add_argument("operation")
    acquire.add_argument("--ttl-seconds", type=int, default=900)
    acquire.add_argument("--allow-expired-takeover", action="store_true")

    refresh = sub.add_parser("lease-refresh")
    refresh.add_argument("state", type=Path)
    refresh.add_argument("manifest", type=Path)
    refresh.add_argument("owner")
    refresh.add_argument("--ttl-seconds", type=int, default=900)

    lease_assert = sub.add_parser("lease-assert")
    lease_assert.add_argument("state", type=Path)
    lease_assert.add_argument("manifest", type=Path)
    lease_assert.add_argument("owner")

    mutation_begin = sub.add_parser("lease-mutation-begin")
    mutation_begin.add_argument("state", type=Path)
    mutation_begin.add_argument("manifest", type=Path)
    mutation_begin.add_argument("owner")
    mutation_begin.add_argument("operation")

    mutation_end = sub.add_parser("lease-mutation-end")
    mutation_end.add_argument("state", type=Path)
    mutation_end.add_argument("manifest", type=Path)
    mutation_end.add_argument("owner")
    mutation_end.add_argument("operation")

    release = sub.add_parser("lease-release")
    release.add_argument("state", type=Path)
    release.add_argument("manifest", type=Path)
    release.add_argument("owner")

    append = sub.add_parser("append")
    append.add_argument("state", type=Path)
    append.add_argument("manifest", type=Path)
    append.add_argument("operation")
    append.add_argument("surface")
    append.add_argument("service")
    append.add_argument("region")
    append.add_argument("target")
    append.add_argument("--owner", default="")

    bundle_write = sub.add_parser("bundle-write")
    bundle_write.add_argument("manifest", type=Path)
    bundle_write.add_argument("output", type=Path)

    bundle_validate = sub.add_parser("bundle-validate")
    bundle_validate.add_argument("descriptor", type=Path)
    bundle_validate.add_argument("directory", type=Path)

    authority_write = sub.add_parser("authority-write")
    authority_write.add_argument("manifest", type=Path)
    authority_write.add_argument("output", type=Path)
    authority_write.add_argument("state_value", choices=("active", "closed"))
    authority_write.add_argument("bundle_uri")

    authority_validate = sub.add_parser("authority-validate")
    authority_validate.add_argument("authority", type=Path)
    authority_validate.add_argument("manifest", type=Path)
    authority_validate.add_argument("expected_state", choices=("active", "closed"))
    authority_validate.add_argument("bundle_uri")
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        if args.command == "validate":
            _state(args.state, args.manifest)
        elif args.command == "init":
            _init(args)
        elif args.command == "lease-acquire":
            if args.ttl_seconds < 60 or args.ttl_seconds > 3600:
                raise ValueError("lease TTL must be from 60 through 3600 seconds")
            _acquire(args)
        elif args.command == "lease-refresh":
            if args.ttl_seconds < 60 or args.ttl_seconds > 3600:
                raise ValueError("lease TTL must be from 60 through 3600 seconds")
            _refresh(args)
        elif args.command == "lease-assert":
            _assert_lease(args)
        elif args.command == "lease-mutation-begin":
            _mutation_begin(args)
        elif args.command == "lease-mutation-end":
            _mutation_end(args)
        elif args.command == "lease-release":
            _release(args)
        elif args.command == "append":
            _append(args)
        elif args.command == "bundle-write":
            _bundle_write(args)
        elif args.command == "bundle-validate":
            _bundle_validate(args)
        elif args.command == "authority-write":
            _authority_write(args)
        elif args.command == "authority-validate":
            _authority_validate(args)
        else:  # pragma: no cover
            raise AssertionError(args.command)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
