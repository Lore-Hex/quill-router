#!/usr/bin/env python3
"""Provision the isolated production workspace used by synthetic monitoring.

Dry-run is the default. The apply path uses only public Store operations and
the typed credit ledger; it never issues ad hoc production DML.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

os.environ.setdefault("TR_STORAGE_BACKEND", "spanner-bigtable")
os.environ.setdefault("TR_GCP_PROJECT_ID", "quill-cloud-proxy")
os.environ.setdefault("TR_SPANNER_INSTANCE_ID", "trusted-router-nam6")
os.environ.setdefault("TR_SPANNER_DATABASE_ID", "trusted-router")
os.environ.setdefault("TR_BIGTABLE_INSTANCE_ID", "trusted-router-logs")
os.environ.setdefault("TR_BIGTABLE_GENERATION_TABLE", "trustedrouter-generations")

from trusted_router.config import Settings
from trusted_router.money import dollars_to_microdollars
from trusted_router.security import new_api_key
from trusted_router.storage import create_store

DEFAULT_EMAIL = "synthetic-monitor@trustedrouter.internal"
DEFAULT_WORKSPACE_NAME = "TrustedRouter Synthetic Monitoring"
DEFAULT_KEY_NAME = "Synthetic monitor"
DEFAULT_FUNDING_EVENT = "synthetic_monitor_workspace_funding_v1"
DEFAULT_TARGET_SHARDS = 16


def provision(
    store: Any,
    *,
    email: str,
    workspace_name: str,
    key_name: str,
    funding_microdollars: int,
    funding_event_id: str,
    target_shards: int,
    apply: bool,
    key_output_file: Path | None,
) -> dict[str, Any]:
    user = store.find_user_by_email(email)
    created_user = False
    created_workspace = False
    created_key = False
    credited = False

    if user is None and not apply:
        return {
            "apply": False,
            "email": email,
            "workspace_name": workspace_name,
            "would_create_user": True,
            "would_create_workspace": True,
            "would_create_key": True,
            "funding_microdollars": funding_microdollars,
        }
    if user is None:
        user = store.ensure_user(
            email,
            email=email,
            trial_credit_microdollars=0,
        )
        created_user = True

    workspaces = store.list_workspaces_for_user(user.id)
    matching = [workspace for workspace in workspaces if workspace.name == workspace_name]
    if len(matching) > 1:
        raise ValueError("multiple synthetic monitoring workspaces exist")
    workspace = matching[0] if matching else None
    if workspace is None:
        personal = [
            candidate
            for candidate in workspaces
            if candidate.name == "Personal Workspace"
            and candidate.owner_user_id == user.id
        ]
        if len(personal) == 1 and len(workspaces) == 1:
            workspace = personal[0]
            if apply:
                workspace = store.update_workspace(workspace.id, name=workspace_name)
                if workspace is None:
                    raise RuntimeError("failed to rename synthetic monitoring workspace")
            created_workspace = created_user
        elif apply:
            workspace = store.create_workspace(
                user.id,
                workspace_name,
                trial_credit_microdollars=0,
            )
            created_workspace = True

    if workspace is None:
        raise ValueError("synthetic monitoring workspace is missing")

    active_keys = [key for key in store.list_keys(workspace.id) if not key.disabled]
    existing_keys = [key for key in active_keys if key.name == key_name]
    if len(active_keys) != len(existing_keys):
        raise ValueError("synthetic monitoring workspace has unexpected active keys")
    if len(existing_keys) > 1:
        raise ValueError("multiple active synthetic monitor keys exist")
    if existing_keys:
        existing_key = existing_keys[0]
        if (
            existing_key.management
            or existing_key.limit_microdollars is not None
            or existing_key.limit_daily_microdollars is not None
            or existing_key.limit_weekly_microdollars is not None
            or existing_key.limit_monthly_microdollars is not None
            or existing_key.expires_at is not None
            or existing_key.tags.get("purpose") != "synthetic_monitoring"
            or existing_key.tags.get("analytics") != "excluded"
            or existing_key.tags.get("spend_control") != "workspace_funding_only"
        ):
            raise ValueError("existing synthetic monitor key has unsafe configuration")

    account = store.get_credit_account(workspace.id)
    if account is None:
        raise ValueError("synthetic monitoring credit account is missing")
    if account.auto_refill_enabled:
        raise ValueError("synthetic monitoring workspace must not use auto-refill")
    current_shards = account.shard_count
    current_key_shards = existing_keys[0].usage_shard_count if existing_keys else 1

    result: dict[str, Any] = {
        "apply": apply,
        "email": email,
        "user_id": user.id,
        "workspace_id": workspace.id,
        "workspace_name": workspace.name,
        "key_id": existing_keys[0].hash if existing_keys else None,
        "created_user": created_user,
        "created_workspace": created_workspace,
        "created_key": False,
        "credited": False,
        "funding_microdollars": funding_microdollars,
        "current_shards": current_shards,
        "current_key_shards": current_key_shards,
        "target_shards": target_shards,
        "requires_reshard": (
            current_shards != target_shards or current_key_shards != target_shards
        ),
    }
    if not apply:
        result["would_create_key"] = not existing_keys
        return result

    if not existing_keys:
        if key_output_file is None:
            raise ValueError("--key-output-file is required when creating the monitor key")
        if key_output_file.exists():
            raise ValueError("refusing to overwrite the key output file")

    credited = store.credit_workspace_typed_direct(
        workspace.id,
        funding_microdollars,
        funding_event_id,
    )

    if not existing_keys:
        assert key_output_file is not None
        raw_key = new_api_key()
        # A lifetime key cap requires one exact usage row and therefore cannot
        # be sharded. This isolated workspace has no auto-refill; its explicitly
        # funded balance is the hard total spend cap while both credit and
        # uncapped-key counters can safely use 16 shards.
        raw_key, api_key = store.create_api_key(
            workspace_id=workspace.id,
            name=key_name,
            creator_user_id=user.id,
            management=False,
            raw_key=raw_key,
            tags={
                "purpose": "synthetic_monitoring",
                "analytics": "excluded",
                "spend_control": "workspace_funding_only",
            },
        )
        created_output_file = False
        try:
            fd = os.open(
                key_output_file,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            created_output_file = True
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                # Secret Manager preserves every byte from --data-file. Keep
                # this file byte-identical to the key so env injection cannot
                # change its lookup hash with a trailing newline.
                output.write(raw_key)
        except Exception:
            store.delete_key(api_key.hash)
            if created_output_file:
                key_output_file.unlink(missing_ok=True)
            raise
        result["key_id"] = api_key.hash
        created_key = True

    result["created_key"] = created_key
    result["credited"] = credited
    return result


def main(argv: list[str] | None = None, *, store: Any | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--workspace-name", default=DEFAULT_WORKSPACE_NAME)
    parser.add_argument("--key-name", default=DEFAULT_KEY_NAME)
    parser.add_argument("--fund-usd", default="1000")
    parser.add_argument("--funding-event-id", default=DEFAULT_FUNDING_EVENT)
    parser.add_argument("--target-shards", type=int, default=DEFAULT_TARGET_SHARDS)
    parser.add_argument("--key-output-file", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    if args.apply and os.environ.get("TR_STORAGE_BACKEND") != "spanner-bigtable":
        print("ERROR: --apply requires TR_STORAGE_BACKEND=spanner-bigtable", file=sys.stderr)
        return 2
    if args.target_shards < 1 or args.target_shards > 64:
        print("ERROR: --target-shards must be between 1 and 64", file=sys.stderr)
        return 2
    try:
        active_store = create_store(Settings()) if store is None else store
        result = provision(
            active_store,
            email=args.email.strip().lower(),
            workspace_name=args.workspace_name.strip(),
            key_name=args.key_name.strip(),
            funding_microdollars=dollars_to_microdollars(args.fund_usd),
            funding_event_id=args.funding_event_id.strip(),
            target_shards=args.target_shards,
            apply=args.apply,
            key_output_file=args.key_output_file,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    if not args.apply:
        print("DRY-RUN: no production state changed; pass --apply after review.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
