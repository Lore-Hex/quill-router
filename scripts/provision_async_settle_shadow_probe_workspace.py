#!/usr/bin/env python3
"""Provision or read-only validate an isolated async-settle shadow cohort."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    from scripts.provision_synthetic_monitor import provision, validate_synthetic_monitor_key
except ModuleNotFoundError:  # direct script execution
    from provision_synthetic_monitor import provision, validate_synthetic_monitor_key
from trusted_router.config import Settings
from trusted_router.money import dollars_to_microdollars
from trusted_router.storage import create_store, typed_billing_store

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
        else:
            user = active.find_user_by_email(names["email"])
            if args.apply and user is None and args.target_shards != 1:
                raise ValueError("new cohorts have one shard; target only validates")
            if user is not None:
                workspaces = active.list_workspaces_for_user(user.id)
                if len(workspaces) != 1 or workspaces[0].name != names["workspace_name"]:
                    raise ValueError("existing probe user has unexpected workspaces")
                workspace = workspaces[0]
                if workspace.owner_user_id != user.id or any(
                    key.creator_user_id != user.id for key in active.list_keys(workspace.id)
                    if not key.disabled
                ):
                    raise ValueError("existing probe workspace or key has the wrong owner")
                _validate_workspace(active, workspace.id, args.target_shards)
            result = provision(
                active, email=names["email"], workspace_name=names["workspace_name"],
                key_name=names["key_name"], funding_microdollars=funding,
                funding_event_id=names["funding_event_id"], target_shards=args.target_shards,
                apply=args.apply, key_output_file=args.key_output_file,
            )
        workspace_id = result.get("workspace_id")
        result.update(secret_name=names["secret_name"], pin_line=(
            f'"TR_ASYNC_SETTLE_SHADOW_WORKSPACES={workspace_id}"' if workspace_id else None
        ))
    except Exception:  # Do not echo backend errors: they may contain credentials.
        print("ERROR: shadow probe provisioning/validation failed; check cohort configuration", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
