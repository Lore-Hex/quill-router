#!/usr/bin/env python3
"""Apply Veriff decisions the webhook never delivered.

The webhook is the normal path and this changes nothing about it. It exists
for the window where the decision fires and the webhook cannot be delivered —
the URL is unset in the Veriff dashboard, or wrong, or the endpoint was down.
Veriff does not resend, so without this the person sits at ``pending`` forever
with a $5 attempt already charged, and support has no honest remedy short of
editing the database by hand.

Dry run by default. ``--apply`` writes only through
``Store.set_user_identity_status``, with exactly the mapping the webhook uses,
so a reconciled decision and a delivered one are indistinguishable afterwards.

  uv run python scripts/reconcile_veriff_decisions.py --email you@example.com
  uv run python scripts/reconcile_veriff_decisions.py --email you@example.com --apply

One person per run, named explicitly: there is no store index of pending
identity sessions, and inventing one to sweep them all is a bigger change than
this repair deserves. If stranded sessions ever stop being rare, that index is
the thing to add.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

os.environ.setdefault("TR_STORAGE_BACKEND", "spanner-bigtable")
os.environ.setdefault("TR_GCP_PROJECT_ID", "quill-cloud-proxy")
os.environ.setdefault("TR_SPANNER_INSTANCE_ID", "trusted-router-nam6")
os.environ.setdefault("TR_SPANNER_DATABASE_ID", "trusted-router")
os.environ.setdefault("TR_BIGTABLE_INSTANCE_ID", "trusted-router-logs")

from trusted_router.config import Settings
from trusted_router.routes.internal.veriff import (
    decision_reason,
    mapped_status,
    verified_name,
)
from trusted_router.services.veriff import VeriffError, fetch_veriff_decision
from trusted_router.storage import create_store
from trusted_router.storage_models import User


def _candidates(store: Any, args: argparse.Namespace) -> list[User]:
    if args.email:
        user = store.find_user_by_email(args.email)
        return [user] if user is not None else []
    user = store.get_user(args.user_id)
    return [user] if user is not None else []


def main(argv: list[str] | None = None, *, store: Any | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--email")
    target.add_argument("--user-id")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    settings = Settings()
    active = create_store(settings) if store is None else store
    users = _candidates(active, args)
    if not users:
        print("no such user")
        return 0

    changed = 0
    for user in users:
        session_id = getattr(user, "veriff_session_id", None)
        record: dict[str, Any] = {
            "user_id": user.id,
            "email": user.email,
            "session_id": session_id,
            "before": getattr(user, "identity_status", None),
        }
        if not session_id:
            record["outcome"] = "no_session"
            print(json.dumps(record, sort_keys=True))
            continue
        try:
            payload = fetch_veriff_decision(session_id, settings=settings)
        except VeriffError as exc:
            record["outcome"] = f"lookup_failed: {exc}"
            print(json.dumps(record, sort_keys=True))
            continue
        verification = payload.get("verification")
        if not isinstance(verification, dict):
            record["outcome"] = "no_decision_yet"
            print(json.dumps(record, sort_keys=True))
            continue
        code = verification.get("code")
        status = mapped_status(verification.get("status"), int(code) if code is not None else 0)
        reason, reason_code = decision_reason(verification)
        record["decision_code"] = code
        record["mapped"] = status
        # Operator-visible on purpose. The end user never sees this string —
        # see trusted_router.identity_guidance for why.
        record["reason"] = reason
        record["reason_code"] = reason_code
        if status is None:
            record["outcome"] = "unhandled_decision"
        elif status == record["before"]:
            record["outcome"] = "already_applied"
        elif not args.apply:
            record["outcome"] = "would_apply"
        else:
            active.set_user_identity_status(
                user.id,
                status=status,
                decision_code=int(code) if code is not None else None,
                decision_reason=reason,
                decision_reason_code=reason_code,
                verified_name=verified_name(verification) if status == "approved" else None,
            )
            changed += 1
            record["outcome"] = "applied"
        print(json.dumps(record, sort_keys=True))

    print(json.dumps({"mode": "apply" if args.apply else "dry_run", "applied": changed}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
