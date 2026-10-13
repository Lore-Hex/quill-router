"""Provision rollout P1's synthetic workspace in one step: a workspace of the
operator's own, named for it, with no API key (nothing but leases can spend
from it), funded by an admin grant through the typed ledger with an event ID
that makes a retry idempotent, and put at the trust tier leases need by the
override the internal admin route sets (a grant is not a payment, so the
rows would stay at tier 0). docs/runbooks/fastpath-p1.md.

Dry-run is the default; ``--apply`` changes production, and is idempotent:
a second run finds the workspace, the grant's event already applied, and
sets the same override again.

Example:
  uv run python scripts/provision_fastpath_p1_workspace.py --email joseph@jperla.com --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from typing import Any

os.environ.setdefault("TR_STORAGE_BACKEND", "spanner-clickhouse")
os.environ.setdefault("TR_GCP_PROJECT_ID", "quill-cloud-proxy")
os.environ.setdefault("TR_SPANNER_INSTANCE_ID", "trusted-router-nam6")
os.environ.setdefault("TR_SPANNER_DATABASE_ID", "trusted-router")

from trusted_router.config import Settings
from trusted_router.money import dollars_to_microdollars
from trusted_router.storage import create_store
from trusted_router.storage_models import CreditProvenance

DEFAULT_WORKSPACE_NAME = "fastpath-synthetic"
DEFAULT_FUNDING_EVENT = "fastpath_p1_funding_2026_10"


def _shard_tiers(store: Any, workspace_id: str) -> list[list[int | None]]:
    with store._database.snapshot() as snap:
        rows = snap.execute_sql(
            "SELECT shard, trust_tier, trust_override_tier, total_credits FROM tr_credit_balance "
            "WHERE workspace_id=@pk ORDER BY shard",
            params={"pk": workspace_id},
            param_types={"pk": store._param_types.STRING},
        )
        return [[None if v is None else int(v) for v in row] for row in rows]


def _credits(store: Any, workspace_id: str) -> int:
    return sum(row[3] or 0 for row in _shard_tiers(store, workspace_id))


def _grant_credited(store: Any, workspace_id: str, event_id: str) -> list[int]:
    """What this workspace's grant events for the funding event credited:
    a grant's trust event (kind "grant"), by its credited amount; a
    payment's fact of the same ID proves nothing of the funding."""
    with store._database.snapshot() as snap:
        rows = snap.execute_sql(
            "SELECT event_id, kind, credited_micro FROM tr_trust_event WHERE workspace_id=@pk",
            params={"pk": workspace_id},
            param_types={"pk": store._param_types.STRING},
        )
        return [int(row[2] or 0) for row in rows if str(row[0]) == event_id and str(row[1]) == "grant"]


def _open_reservations(store: Any, workspace_id: str) -> int:
    with store._database.snapshot() as snap:
        rows = snap.execute_sql(
            "SELECT COUNT(*) FROM tr_reservation WHERE workspace_id=@ws AND settled=false",
            params={"ws": workspace_id},
            param_types={"ws": store._param_types.STRING},
        )
        return int(list(rows)[0][0])


def provision(
    store: Any,
    *,
    email: str,
    workspace_name: str,
    funding_microdollars: int,
    funding_event_id: str,
    tier: int,
    operator: str,
    reason: str,
    apply: bool,
) -> dict[str, Any]:
    user = store.find_user_by_email(email)
    if user is None:
        raise ValueError(f"no user for {email}; the workspace is the operator's own, so the user must exist")
    # The operator's OWN workspace: a membership in another owner's
    # workspace of the name is not it, and is never funded.
    workspaces = [
        w for w in store.list_workspaces_for_user(user.id)
        if w.name == workspace_name and w.owner_user_id == user.id
    ]
    if len(workspaces) > 1:
        raise ValueError(f"{len(workspaces)} workspaces named {workspace_name!r} owned by {email}")
    workspace = workspaces[0] if workspaces else None
    result: dict[str, Any] = {
        "apply": apply,
        "email": email,
        "user_id": user.id,
        "workspace_name": workspace_name,
        "workspace_id": None if workspace is None else workspace.id,
        "would_create_workspace": workspace is None,
        "funding_microdollars": funding_microdollars,
        "funding_event_id": funding_event_id,
        "tier": tier,
    }
    if workspace is not None:
        # Nothing but leases may spend from it: no key, disabled or not (a
        # disabled key can be enabled again), and no synchronous request
        # still open, whose settle would consume the funding.
        keys = store.list_keys(workspace.id)
        if keys:
            raise ValueError(f"workspace {workspace.id} has {len(keys)} API keys; nothing but leases may spend from it")
        open_holds = _open_reservations(store, workspace.id)
        if open_holds:
            raise ValueError(f"workspace {workspace.id} has {open_holds} unsettled reservations; wait for them to settle")
        result["shards_before"] = _shard_tiers(store, workspace.id)
        result["credits_total_microdollars"] = _credits(store, workspace.id)
    if not apply:
        return result
    if workspace is None:
        workspace = store.create_workspace(user.id, workspace_name, trial_credit_microdollars=0)
        result["workspace_id"] = workspace.id
        result["created_workspace"] = True
    # The grant must show: a new one raises the rows by exactly the funding,
    # from what they held just before; one the store reports already
    # applied must have its event on this workspace, with this amount, or
    # it was applied elsewhere (the store deduplicates the event globally).
    before = _credits(store, workspace.id)
    granted = store.credit_workspace_typed_direct(
        workspace.id,
        funding_microdollars,
        funding_event_id,
        provenance=CreditProvenance(source="grant", provider="operator", external_ref=None, occurred_at=datetime.now(UTC)),
    )
    if granted:
        after = _credits(store, workspace.id)
        if after != before + funding_microdollars:
            raise RuntimeError(f"the credit rows rose by {after - before}, not by the funding {funding_microdollars}")
    elif funding_microdollars not in _grant_credited(store, workspace.id, funding_event_id):
        raise RuntimeError(f"the funding event {funding_event_id} was applied elsewhere, or with another amount; "
                           "this workspace holds no such grant")
    result["funded"] = "applied" if granted else "already applied"
    # The audit's baseline: what the credit rows hold now, pre-existing
    # funding of the operator's own workspace included.
    result["credits_total_microdollars"] = _credits(store, workspace.id)
    record = store.set_workspace_trust_override(
        workspace.id, tier=tier, identity_bypass=False, operator_identity=operator, reason=reason
    )
    result["override"] = {"tier": record.tier, "set_at": record.set_at.isoformat()}
    result["shards_after"] = _shard_tiers(store, workspace.id)
    if any(row[1] is None or row[1] < tier for row in result["shards_after"]):
        raise RuntimeError("a credit shard's effective tier is still below the override")
    return result


def main(argv: list[str] | None = None, *, store: Any | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True, help="the operator's TrustedRouter user")
    parser.add_argument("--workspace-name", default=DEFAULT_WORKSPACE_NAME)
    parser.add_argument("--fund-usd", default="100")
    parser.add_argument("--funding-event-id", default=DEFAULT_FUNDING_EVENT)
    parser.add_argument("--tier", type=int, default=1)
    parser.add_argument("--operator", default=None, help="recorded with the override; the email by default")
    parser.add_argument("--reason", default="fastpath P1 synthetic workspace")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.apply and os.environ.get("TR_STORAGE_BACKEND") != "spanner-clickhouse":
        print("ERROR: --apply requires TR_STORAGE_BACKEND=spanner-clickhouse", file=sys.stderr)
        return 2
    try:
        funding = dollars_to_microdollars(args.fund_usd)
        if funding <= 0 or not 0 <= args.tier <= 3:
            raise ValueError("funding must be positive and the tier 0 through 3")
        active_store = create_store(Settings()) if store is None else store
        result = provision(
            active_store,
            email=args.email,
            workspace_name=args.workspace_name,
            funding_microdollars=funding,
            funding_event_id=args.funding_event_id,
            tier=args.tier,
            operator=args.operator or args.email,
            reason=args.reason,
            apply=args.apply,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    if not args.apply:
        print("DRY-RUN: no production state changed; pass --apply after reviewing the plan")
    return 0


if __name__ == "__main__":
    sys.exit(main())
