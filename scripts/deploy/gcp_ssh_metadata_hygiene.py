#!/usr/bin/env python3
"""Keep CI SSH keys out of Compute Engine project and instance metadata."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_INSTANCES = (
    "tr-clickhouse-1:us-central1-a",
    "tr-clickhouse-2:us-central1-b",
    "tr-clickhouse-3:us-central1-c",
)
DEFAULT_CI_USERS = frozenset({"runner", "github-actions"})
MAX_PROJECT_SSH_METADATA_BYTES = 64 * 1024
MAX_PROJECT_SSH_KEY_LINES = 64
GCLOUD = shutil.which("gcloud")


def metadata_true(value: str | None) -> bool:
    return value is not None and value.strip().upper() == "TRUE"


def metadata_map(payload: dict[str, Any], *, project: bool) -> dict[str, str]:
    container_name = "commonInstanceMetadata" if project else "metadata"
    container = payload.get(container_name) or {}
    items = container.get("items") or []
    return {
        str(item["key"]): str(item.get("value", ""))
        for item in items
        if isinstance(item, dict) and "key" in item
    }


def prune_ci_ssh_keys(
    value: str,
    *,
    ci_users: frozenset[str] = DEFAULT_CI_USERS,
) -> tuple[str, int]:
    """Remove only recognized CI-owned metadata keys, preserving all others."""
    kept: list[str] = []
    removed = 0
    for line in value.splitlines():
        stripped = line.strip()
        username, separator, _remainder = stripped.partition(":")
        lower = stripped.lower()
        is_ci_key = separator == ":" and username.lower() in ci_users
        is_ci_key = is_ci_key or "runner@runnervm" in lower
        is_ci_key = is_ci_key or '"username":"runner"' in lower.replace(" ", "")
        if is_ci_key:
            removed += 1
        else:
            kept.append(line)
    cleaned = "\n".join(kept)
    if value.endswith("\n") and cleaned:
        cleaned += "\n"
    return cleaned, removed


def validate_instance_metadata(metadata: dict[str, str]) -> None:
    if not metadata_true(metadata.get("enable-oslogin")):
        raise ValueError("enable-oslogin must be TRUE")
    if not metadata_true(metadata.get("block-project-ssh-keys")):
        raise ValueError("block-project-ssh-keys must be TRUE")


def validate_project_ssh_metadata(value: str) -> None:
    key_lines = len(value.splitlines())
    key_bytes = len(value.encode("utf-8"))
    if (
        key_lines > MAX_PROJECT_SSH_KEY_LINES
        or key_bytes > MAX_PROJECT_SSH_METADATA_BYTES
    ):
        raise ValueError(
            "project ssh-keys metadata remains too large after CI-key pruning: "
            f"{key_lines} entries, {key_bytes} bytes"
        )


def _gcloud_json(project: str, args: list[str]) -> dict[str, Any]:
    if GCLOUD is None:
        raise RuntimeError("gcloud is not installed")
    # Every argument is passed directly to gcloud without a shell. Project and
    # instance values cannot become executable syntax.
    result = subprocess.run(  # noqa: S603
        [GCLOUD, "--project", project, *args, "--format=json"],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise ValueError("gcloud returned a non-object response")
    return payload


def _gcloud(project: str, args: list[str]) -> None:
    if GCLOUD is None:
        raise RuntimeError("gcloud is not installed")
    subprocess.run(  # noqa: S603
        [GCLOUD, "--project", project, *args, "--quiet"],
        check=True,
    )


def _write_metadata_value(
    project: str,
    *,
    value: str,
    instance: str | None = None,
    zone: str | None = None,
) -> None:
    target = ["compute", "project-info"]
    if instance is not None:
        if zone is None:
            raise ValueError("zone is required for instance metadata")
        target = ["compute", "instances", "add-metadata", instance, "--zone", zone]

    if not value:
        if instance is None:
            _gcloud(
                project,
                ["compute", "project-info", "remove-metadata", "--keys=ssh-keys"],
            )
        else:
            _gcloud(
                project,
                [
                    "compute",
                    "instances",
                    "remove-metadata",
                    instance,
                    "--zone",
                    zone,
                    "--keys=ssh-keys",
                ],
            )
        return

    with tempfile.TemporaryDirectory(prefix="tr-ssh-metadata-") as temp_dir:
        value_path = Path(temp_dir) / "ssh-keys"
        value_path.write_text(value, encoding="utf-8")
        if instance is None:
            _gcloud(
                project,
                [
                    *target,
                    "add-metadata",
                    f"--metadata-from-file=ssh-keys={value_path}",
                ],
            )
        else:
            _gcloud(
                project,
                [*target, f"--metadata-from-file=ssh-keys={value_path}"],
            )


def _parse_instance(spec: str) -> tuple[str, str]:
    name, separator, zone = spec.partition(":")
    if separator != ":" or not name or not zone:
        raise ValueError(f"invalid instance specification: {spec!r}; use NAME:ZONE")
    return name, zone


def reconcile(
    *,
    project: str,
    instance_specs: list[str],
    apply: bool,
    require_os_login: bool = False,
) -> int:
    project_payload = _gcloud_json(
        project, ["compute", "project-info", "describe"]
    )
    project_metadata = metadata_map(project_payload, project=True)

    instances: list[tuple[str, str, dict[str, str]]] = []
    errors: list[str] = []
    for spec in instance_specs:
        name, zone = _parse_instance(spec)
        payload = _gcloud_json(
            project,
            ["compute", "instances", "describe", name, "--zone", zone],
        )
        metadata = metadata_map(payload, project=False)
        if require_os_login:
            try:
                validate_instance_metadata(metadata)
            except ValueError as exc:
                errors.append(f"{name} ({zone}): {exc}")
        instances.append((name, zone, metadata))

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print(
            "Refusing to touch SSH metadata until every target uses OS Login "
            "and blocks project SSH keys.",
            file=sys.stderr,
        )
        return 1

    project_value = project_metadata.get("ssh-keys", "")
    clean_project_value, project_removed = prune_ci_ssh_keys(project_value)
    resource_changes: list[tuple[str, str | None, str | None, str, int]] = [
        ("project", None, None, clean_project_value, project_removed)
    ]
    for name, zone, metadata in instances:
        value = metadata.get("ssh-keys", "")
        cleaned, removed = prune_ci_ssh_keys(value)
        resource_changes.append((name, name, zone, cleaned, removed))

    total_removed = sum(item[4] for item in resource_changes)
    if not apply and total_removed:
        for label, _name, _zone, _value, removed in resource_changes:
            if removed:
                print(
                    f"ERROR: {label} contains {removed} CI-owned SSH metadata key(s)",
                    file=sys.stderr,
                )
        return 1

    if apply:
        for _label, name, zone, cleaned, removed in resource_changes:
            if not removed:
                continue
            _write_metadata_value(
                project,
                value=cleaned,
                instance=name,
                zone=zone,
            )

    validate_project_ssh_metadata(clean_project_value)
    action = "removed" if apply else "found"
    print(
        f"SSH metadata hygiene passed; {action} {total_removed} CI key(s); "
        f"project metadata has {len(clean_project_value.splitlines())} key(s) "
        f"and {len(clean_project_value.encode('utf-8'))} bytes"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--project", default="quill-cloud-proxy")
    parser.add_argument(
        "--require-os-login",
        action="store_true",
        help="also fail unless every target explicitly uses OS Login",
    )
    parser.add_argument(
        "--instance",
        action="append",
        dest="instances",
        help="ClickHouse VM as NAME:ZONE; repeat for multiple VMs",
    )
    args = parser.parse_args()
    return reconcile(
        project=args.project,
        instance_specs=args.instances or list(DEFAULT_INSTANCES),
        apply=args.apply,
        require_os_login=args.require_os_login,
    )


if __name__ == "__main__":
    raise SystemExit(main())
