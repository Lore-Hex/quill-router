#!/usr/bin/env python3
"""Provision or read-only validate an isolated async-settle shadow cohort."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from scripts.provision_synthetic_monitor import provision, validate_synthetic_monitor_key
except ModuleNotFoundError:  # direct script execution
    from provision_synthetic_monitor import provision, validate_synthetic_monitor_key
from trusted_router.config import Settings
from trusted_router.money import dollars_to_microdollars
from trusted_router.security import new_api_key
from trusted_router.storage import create_store, typed_billing_store
from trusted_router.storage_models import CreditProvenance

SECRET_NAME = "trustedrouter-async-settle-shadow-probe-api-key"  # noqa: S105 - secret resource name


def cohort_names(label: str = "") -> dict[str, str]:
    if label and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", label) is None:
        raise ValueError("label must contain 1 to 32 lowercase letters, digits or hyphens")
    suffix = f"-{label}" if label else ""
    title = f" {label}" if label else ""
    return {
        "email": f"async-settle-shadow-probe{suffix}@trustedrouter.internal",
        "workspace_name": f"TrustedRouter Async Settle Shadow Probe{title}",
        "key_name": f"Async settle shadow probe{title}",
        "funding_event_id": f"async_settle_shadow_probe_workspace_funding_v1{suffix}",
        "secret_name": SECRET_NAME + suffix,
    }


def _validate_workspace(store: Any, workspace_id: str, target_shards: int = 1) -> None:
    workspace = store.get_workspace(workspace_id)
    if workspace is None or workspace.deleted or workspace.billing_paused or workspace.federated_home:
        raise ValueError("probe workspace must be active and local")
    if store.list_byok_providers(workspace_id):
        raise ValueError("probe workspace must not have BYOK credentials")
    account = store.get_credit_account(workspace_id)
    if account is None or account.auto_refill_enabled or account.shard_count != target_shards:
        raise ValueError("probe workspace has an unexpected shard count or auto-refill")
    keys = [key for key in store.list_keys(workspace_id) if not key.disabled]
    if len(keys) > 1:
        raise ValueError("probe workspace must have only one active key")
    for key in keys:
        if (key.usage_shard_count != target_shards or key.app_id or key.scopes or key.federated_home
                or key.federated_app_markup_basis_points or key.federated_app_suspended
                or key.federated_app_owner_user_id or key.budget_alert_only or key.budget_strict):
            raise ValueError("probe key must be ordinary local inference without app attribution")


def validate(store: Any, raw_key: str, *, label: str = "", target_shards: int = 1) -> dict[str, Any]:
    try:
        names = cohort_names(label)
        if not raw_key or any(char.isspace() for char in raw_key):
            raise ValueError("invalid credential")
        result = validate_synthetic_monitor_key(
            store, raw_key, email=names["email"], workspace_name=names["workspace_name"],
            key_name=names["key_name"], target_shards=target_shards,
        )
        user = store.find_user_by_email(names["email"])
        workspaces = store.list_workspaces_for_user(user.id)
        if len(workspaces) != 1 or workspaces[0].id != result["workspace_id"]:
            raise ValueError("unexpected cohort workspaces")
        _validate_workspace(store, result["workspace_id"], target_shards)
        typed = typed_billing_store(store)
        if typed is None or getattr(typed, "request_record_write_mode", "legacy") != "typed":
            raise ValueError("probe requires typed local settlement storage")
        return result
    except Exception:
        raise ValueError("shadow probe key validation failed") from None


class MissingCredential(ValueError):
    """An existing active key has no recoverable local credential."""


def apply_cohort(store: Any, names: dict[str, str], funding: int, path: Path | None) -> dict[str, Any]:
    user = store.find_user_by_email(names["email"])
    workspace = None
    keys = []
    if user is not None:
        workspaces = store.list_workspaces_for_user(user.id)
        if len(workspaces) != 1:
            raise ValueError("unexpected workspace inventory")
        workspace = workspaces[0]
        if workspace.owner_user_id != user.id:
            raise ValueError("wrong owner")
        _validate_workspace(store, workspace.id)
        keys = [key for key in store.list_keys(workspace.id) if not key.disabled]
        if workspace.name == "Personal Workspace":
            # This is exactly ensure_user(trial_credit=0)'s intermediate state.
            members = store.list_members(workspace.id)
            if (len(members) != 1 or members[0].user_id != user.id or members[0].role != "owner"
                    or store.list_keys(workspace.id) or any(store.credit_money_snapshot(workspace.id))):
                raise ValueError("personal workspace is not pristine")
        elif workspace.name != names["workspace_name"]:
            raise ValueError("unexpected workspace")
    if path is None or (keys and not path.exists()):
        raise MissingCredential()
    if path.exists():
        if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError("credential file must be private")
        raw = path.read_text()
        if re.fullmatch(r"sk-tr-v1-[A-Za-z0-9_-]{43}", raw) is None:
            if keys:
                raise MissingCredential()
            # A process may die during the exclusive write, before any DB step.
            # No active key exists: discard only the incomplete local credential.
            path.unlink()
    if not path.exists():
        raw = new_api_key()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except Exception:
            path.unlink(missing_ok=True)
            raise
    raw = path.read_text()
    registered = store.get_key_by_raw(raw)
    if keys:
        if registered is None or registered.hash != keys[0].hash:
            raise MissingCredential()
        validate(store, raw, label=names["email"].removeprefix("async-settle-shadow-probe").split("@")[0].removeprefix("-"))
    elif registered is not None:
        raise ValueError("credential already belongs to a key")
    # From here on keep the file on EVERY failure, including uncertain commits.
    if user is None:
        user = store.ensure_user(names["email"], email=names["email"], trial_credit_microdollars=0)
        workspaces = store.list_workspaces_for_user(user.id)
        if len(workspaces) != 1:
            raise ValueError("unexpected workspace inventory")
        workspace = workspaces[0]
    assert workspace is not None
    if workspace.name == "Personal Workspace":
        workspace = store.update_workspace(workspace.id, name=names["workspace_name"])
    credited = store.credit_workspace_typed_direct(
        workspace.id, funding, names["funding_event_id"],
        provenance=CreditProvenance(source="provisioning", provider="system",
                                    external_ref=None, occurred_at=datetime.now(UTC)),
    )
    if not keys:
        _, key = store.create_api_key(workspace_id=workspace.id, name=names["key_name"],
            creator_user_id=user.id, management=False, raw_key=raw,
            tags={"purpose": "synthetic_monitoring", "analytics": "excluded",
                  "spend_control": "workspace_funding_only"})
    else:
        key = keys[0]
    return dict(apply=True, workspace_id=workspace.id, key_id=key.hash, credited=credited,
                created_key=not keys, funding_microdollars=funding)


def main(argv: list[str] | None = None, *, store: Any | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="")
    parser.add_argument("--fund-usd", default="100")
    parser.add_argument("--target-shards", type=int, default=1,
                        help="expected existing shard count (validation only; never reshards)")
    parser.add_argument("--key-output-file", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--validate", action="store_true", help="read the key from stdin; no writes")
    args = parser.parse_args(argv)
    try:
        names = cohort_names(args.label)
        if not 1 <= args.target_shards <= 64:
            raise ValueError("invalid target shards")
        if args.apply and os.environ.get("TR_STORAGE_BACKEND") != "spanner-clickhouse":
            raise ValueError("apply requires spanner-clickhouse")
        funding = dollars_to_microdollars(args.fund_usd)
        if funding <= 0:
            raise ValueError("funding must be positive")
        active = create_store(Settings(), initialize_schema=False) if store is None else store
        if args.apply:
            typed = typed_billing_store(active)
            if typed is None or getattr(typed, "request_record_write_mode", "legacy") != "typed":
                raise ValueError("apply requires typed local settlement storage")
        if args.validate:
            result = validate(active, sys.stdin.read(), label=args.label, target_shards=args.target_shards)
        elif args.apply:
            if args.target_shards != 1:
                raise ValueError("apply requires one shard")
            result = apply_cohort(active, names, funding, args.key_output_file)
        else:
            result = provision(
                active, email=names["email"], workspace_name=names["workspace_name"],
                key_name=names["key_name"], funding_microdollars=funding,
                funding_event_id=names["funding_event_id"], target_shards=args.target_shards,
                apply=False, key_output_file=None,
            )
        workspace_id = result.get("workspace_id")
        result.update(secret_name=names["secret_name"], pin_line=(
            f'"TR_ASYNC_SETTLE_SHADOW_WORKSPACES={workspace_id}"' if workspace_id else None
        ))
    except MissingCredential:
        print("ERROR: supply the original --key-output-file; if lost, disable the active key through the existing key management path and rerun with a new file", file=sys.stderr)
        return 1
    except Exception:  # Do not echo backend errors: they may contain credentials.
        print("ERROR: shadow probe provisioning/validation failed; check cohort configuration", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
