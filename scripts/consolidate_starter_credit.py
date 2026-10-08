"""Consolidate one drained, <=$1 starter ledger without changing API keys.

Dry-run is read-only. Apply rereads every precondition in one transaction and
preserves exact credit/usage totals and trust columns. Only workspaces created
on/after October 4 are eligible: all serving revisions were verified to contain
the July C1 removal of legacy reserve/settle/refund. Older accounts must use
the reviewed pause/drain tooling. Active holds and oversized scans fail closed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from typing import Any

from scripts.expand_billing_shards import CREDIT_COLUMNS, READ_SECONDS, Reader, _wire
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TRUST_COLUMNS, MAX_CREDIT_SHARDS

MAX_STARTER_CREDITS = 1_000_000
MAX_OPEN_RESERVATIONS = 2000
TYPED_ONLY_COHORT_START = datetime(2026, 10, 4, tzinfo=UTC)


def make_plan(workspace: dict[str, Any], credit: dict[str, Any], rows: list[dict[str, Any]], *, open_reservations: int) -> dict[str, Any]:
    ws = workspace["id"]
    created = datetime.fromisoformat(str(workspace.get("created_at", "")).replace("Z", "+00:00"))
    if created.tzinfo is None or not TYPED_ONLY_COHORT_START <= created <= datetime.now(UTC):
        raise ValueError("workspace is outside the verified typed-only cohort")
    if workspace.get("deleted") or workspace.get("federated_home") or workspace.get("billing_paused") or workspace.get("billing_pause_causes"):
        raise ValueError("workspace is deleted, federated, or paused")
    if open_reservations:
        raise ValueError("open reservations block consolidation")
    if credit.get("workspace_id") != ws:
        raise ValueError("credit workspace mismatch")
    count = int(credit.get("shard_count", 1))
    if not 1 <= count <= MAX_CREDIT_SHARDS or len(rows) != count:
        raise ValueError("configured and physical shard counts differ")
    if [int(row["shard"]) for row in rows] != list(range(count)):
        raise ValueError("incomplete shard set")
    trust = [rows[0].get(name) for name in CREDIT_BALANCE_TRUST_COLUMNS]
    for row in rows:
        if row["workspace_id"] != ws or [row.get(name) for name in CREDIT_BALANCE_TRUST_COLUMNS] != trust:
            raise ValueError("workspace or replicated trust columns differ")
        total, usage, reserved = (int(row[name]) for name in ("total_credits", "total_usage", "reserved"))
        if min(total, usage, reserved) < 0 or usage > total or reserved != 0:
            raise ValueError("invalid counters or outstanding credit hold")
        if row.get("billing_pause_causes"):
            raise ValueError("typed credit is paused")
        if row.get("in_debt"):
            raise ValueError("typed credit is marked in debt")
    totals = {name: sum(int(row[name]) for row in rows) for name in ("total_credits", "total_usage", "reserved")}
    if totals["total_credits"] > MAX_STARTER_CREDITS:
        raise ValueError("not a starter account; use reviewed pause/drain reshard tooling")
    return {
        "workspace_id": ws, "current_shards": count, "totals": totals,
        "credit": {**credit, "shard_count": 1},
        # A marked workspace was refused above, so the one row is not in debt.
        "row": {**rows[0], **totals, "shard": 0, "in_debt": False},
    }


def read_plan(reader: Reader, ws: str) -> dict[str, Any]:
    from google.protobuf.json_format import MessageToDict

    records = reader.entities([["workspace", ws], ["credit", ws]])
    rows = reader.read("tr_credit_balance", CREDIT_COLUMNS, [[ws, str(i)] for i in range(MAX_CREDIT_SHARDS)])
    rows.sort(key=lambda row: int(row["shard"]))
    # Scan only the live (settled=false) index prefix, with an explicit hard cap.
    # Reading the range in the commit transaction also conflicts with a new hold.
    common = {"session": reader.session, "transaction": reader.transaction,
              "request_options": {"priority": reader.priority, "request_tag": "tr_ops_starter_credit"}}
    result = reader.client.execute_sql(request={
        **common,
        "sql": "SELECT workspace_id FROM tr_reservation@{FORCE_INDEX=tr_reservation_by_expiry} WHERE settled=false LIMIT 2001",
    }, timeout=reader.timeout(), retry=None)
    active = MessageToDict(result._pb).get("rows", [])
    if len(active) > MAX_OPEN_RESERVATIONS:
        raise ValueError("active reservation scan bound exceeded")
    return make_plan(records[("workspace", ws)], records[("credit", ws)], rows,
                     open_reservations=sum(row[0] == ws for row in active))


def mutations(plan: dict[str, Any]) -> list[dict[str, Any]]:
    count = plan["current_shards"]
    if count == 1:
        return []
    ws = plan["workspace_id"]
    return [
        {"update": {"table": "tr_credit_balance", "columns": [*CREDIT_COLUMNS, "updated_at"],
                    "values": [[*[_wire(plan["row"][name]) for name in CREDIT_COLUMNS], "spanner.commit_timestamp()"]]}},
        {"delete": {"table": "tr_credit_balance", "key_set": {"keys": [[ws, str(i)] for i in range(1, count)]}}},
        {"update": {"table": "tr_entities", "columns": ["kind", "id", "body", "updated_at"],
                    "values": [["credit", ws, json.dumps(plan["credit"], separators=(",", ":")), "spanner.commit_timestamp()"]]}},
    ]


def execute(client: Any, session: str, ws: str, *, apply: bool) -> dict[str, Any]:
    mode = {"read_write": {}} if apply else {"read_only": {"strong": True}}
    tx = client.begin_transaction(request={"session": session, "options": mode}, timeout=READ_SECONDS, retry=None)
    reader = Reader(client, session, {"id": tx.id}, time.monotonic() + 20)
    committed = False
    try:
        plan = read_plan(reader, ws)
        changes = mutations(plan) if apply else []
        if changes:
            client.commit(request={"session": session, "transaction_id": tx.id, "mutations": changes,
                                   "request_options": {"transaction_tag": "tr_ops_starter_credit"}}, timeout=reader.timeout(), retry=None)
            committed = True
        return {name: plan[name] for name in ("workspace_id", "current_shards", "totals")} | {"applied": bool(changes)}
    finally:
        if apply and not committed:
            try:
                client.rollback(request={"session": session, "transaction_id": tx.id}, timeout=2, retry=None)
            except Exception:
                print('{"event":"starter_credit_rollback_failed"}', flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--account", help="Separate deployment-capable identity, required for apply")
    args = parser.parse_args()
    if args.apply and (not args.account or "tr-ops-local" in args.account or not re.fullmatch(r"[A-Za-z0-9_.+@-]+@[A-Za-z0-9.-]+", args.account)):
        parser.error("apply requires a separate deployment identity")
    from google.cloud.spanner_v1.services.spanner import SpannerClient
    from google.oauth2 import credentials as oauth_credentials
    from google.oauth2 import service_account

    def run(credentials: Any, *, apply: bool) -> dict[str, Any]:
        with SpannerClient(credentials=credentials) as client:
            session = client.create_session(request={"database": "projects/quill-cloud-proxy/instances/trusted-router-nam6/databases/trusted-router"}, timeout=READ_SECONDS, retry=None)
            try:
                return execute(client, session.name, args.workspace, apply=apply)
            finally:
                client.delete_session(request={"name": session.name}, timeout=READ_SECONDS, retry=None)

    try:
        ops = service_account.Credentials.from_service_account_file(os.environ["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"], scopes=["https://www.googleapis.com/auth/spanner.data"])
        before = run(ops, apply=False)
        print(json.dumps({"preflight": before}), flush=True)
        if args.apply:
            gcloud = shutil.which("gcloud")
            if not gcloud:
                raise ValueError("gcloud is required")
            env = {k: v for k, v in os.environ.items() if k != "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"}
            token = subprocess.run(  # noqa: S603 - validated operator account; no shell.
                [gcloud, "auth", "print-access-token", "--account", args.account],
                env=env, check=True, capture_output=True, text=True, timeout=30,
            ).stdout.strip()
            result = run(oauth_credentials.Credentials(token), apply=True)
            print(json.dumps({"result": result}), flush=True)
            after = run(ops, apply=False)
            if after["current_shards"] != 1:
                raise RuntimeError("post-commit consolidation did not verify")
            print(json.dumps({"verification": after}), flush=True)
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__, "message": str(exc) if isinstance(exc, ValueError) else "repair did not verify; inspect exact-key state before retry"}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
