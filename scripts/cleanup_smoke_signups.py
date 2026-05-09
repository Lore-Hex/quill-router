"""One-off cleanup: delete the synthetic-signup rows the GHA
prod-smoke workflow accumulated when it was using a timestamped email
(`smoke-<unix>@example.com`) per run. The smoke test now uses a single
stable `smoke@example.com` so this won't re-grow.

Walks the entity graph from each smoke `email_user` row out to its
`user`, `member`, `workspace`, `credit`, and `api_key` rows and
deletes them in one transaction per smoke user (so a partial failure
leaves no dangling pieces).

Run as a project Owner (needs Spanner DDL/DML perms):
    cd quill-router
    TR_GCP_PROJECT_ID=quill-cloud-proxy uv run python scripts/cleanup_smoke_signups.py

Refuses to touch the stable `smoke@example.com` row that the new
smoke test relies on.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import google.oauth2.credentials
from google.cloud import spanner

DRY_RUN = "--dry-run" in sys.argv


def _gcloud_credentials() -> google.oauth2.credentials.Credentials:
    """Use the active gcloud user's access token instead of ADC, so the
    operator just needs `gcloud auth login` (already required to run
    this), not a separate `gcloud auth application-default login`."""
    token = subprocess.check_output(
        ["gcloud", "auth", "print-access-token"], text=True
    ).strip()
    return google.oauth2.credentials.Credentials(token=token)

PROJECT_ID = os.environ.get("TR_GCP_PROJECT_ID", "quill-cloud-proxy")
INSTANCE_ID = os.environ.get("TR_SPANNER_INSTANCE_ID", "trusted-router")
DATABASE_ID = os.environ.get("TR_SPANNER_DATABASE_ID", "trusted-router")
STABLE_SMOKE_EMAIL = "smoke@example.com"


def main() -> int:
    client = spanner.Client(project=PROJECT_ID, credentials=_gcloud_credentials())
    instance = client.instance(INSTANCE_ID)
    database = instance.database(DATABASE_ID)

    # 1. Gather every row that will be touched, in a single
    #    multi-use snapshot. Doing all reads up-front keeps the read
    #    set small and consistent, and avoids 162 separate snapshots.
    plan: list[dict] = []  # [{email, user_id, member_ids, workspace_ids, api_key_ids}, ...]
    with database.snapshot(multi_use=True) as snap:
        rows = list(snap.execute_sql(
            "SELECT id, body FROM tr_entities "
            "WHERE kind='email_user' "
            "AND STARTS_WITH(id, 'smoke-') "
            "AND ENDS_WITH(id, '@example.com') "
            "AND id != @stable",
            params={"stable": STABLE_SMOKE_EMAIL},
            param_types={"stable": spanner.param_types.STRING},
        ))
        smoke_users: list[tuple[str, str]] = []
        for email, body in rows:
            try:
                user_id = json.loads(body)["user_id"]
            except (KeyError, ValueError):
                print(f"WARN: skipping malformed email_user row id={email!r} body={body!r}")
                continue
            smoke_users.append((email, user_id))

        print(f"Found {len(smoke_users)} smoke signups; gathering related rows…")

        for email, user_id in smoke_users:
            members = list(snap.execute_sql(
                "SELECT id, body FROM tr_entities "
                "WHERE kind='member' AND ENDS_WITH(id, @suffix)",
                params={"suffix": f"#{user_id}"},
                param_types={"suffix": spanner.param_types.STRING},
            ))
            workspace_ids = [json.loads(b)["workspace_id"] for _id, b in members]
            member_ids = [m_id for m_id, _b in members]

            api_key_ids: list[str] = []
            for wsid in workspace_ids:
                ak_rows = snap.execute_sql(
                    "SELECT id FROM tr_entities "
                    "WHERE kind='api_key' AND JSON_VALUE(body, '$.workspace_id') = @wsid",
                    params={"wsid": wsid},
                    param_types={"wsid": spanner.param_types.STRING},
                )
                api_key_ids.extend(row[0] for row in ak_rows)

            plan.append({
                "email": email,
                "user_id": user_id,
                "member_ids": member_ids,
                "workspace_ids": workspace_ids,
                "api_key_ids": api_key_ids,
            })

    if not plan:
        print("Nothing to delete.")
        return 0

    deleted = {"email_user": 0, "user": 0, "member": 0, "workspace": 0, "credit": 0, "api_key": 0}

    for entry in plan:
        email = entry["email"]
        user_id = entry["user_id"]
        member_ids = entry["member_ids"]
        workspace_ids = entry["workspace_ids"]
        api_keys = entry["api_key_ids"]

        def txn(transaction, _email=email, _user_id=user_id,
                _ws_ids=workspace_ids, _member_ids=member_ids,
                _ak_ids=api_keys) -> None:
            from google.cloud.spanner_v1 import KeySet
            for ak_id in _ak_ids:
                transaction.delete("tr_entities", KeySet(keys=[("api_key", ak_id)]))
            for wsid in _ws_ids:
                transaction.delete("tr_entities", KeySet(keys=[("credit", wsid)]))
                transaction.delete("tr_entities", KeySet(keys=[("workspace", wsid)]))
            for m_id in _member_ids:
                transaction.delete("tr_entities", KeySet(keys=[("member", m_id)]))
            transaction.delete("tr_entities", KeySet(keys=[("user", _user_id)]))
            transaction.delete("tr_entities", KeySet(keys=[("email_user", _email)]))

        if not DRY_RUN:
            database.run_in_transaction(txn)
        deleted["api_key"] += len(api_keys)
        deleted["credit"] += len(workspace_ids)
        deleted["workspace"] += len(workspace_ids)
        deleted["member"] += len(member_ids)
        deleted["user"] += 1
        deleted["email_user"] += 1
        verb = "would delete" if DRY_RUN else "deleted"
        print(f"  {verb} {email} (user={user_id[:8]}…, ws={len(workspace_ids)}, ak={len(api_keys)})")

    print()
    print(f"Totals ({'DRY-RUN, no rows touched' if DRY_RUN else 'DELETED'}):")
    for kind, n in deleted.items():
        print(f"  {kind}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
