"""Atomically expand a live typed workspace and one uncapped key, without a pause.

Dry-run uses the read-only ops credential. Apply requires a separately named
gcloud identity. Existing holds, usage, window counters and pause ownership are
never modified. Only free credit capacity moves; new counter rows start at zero.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import time
from typing import Any

from trusted_router.storage_gcp_counters import (
    CREDIT_BALANCE_TRUST_COLUMNS,
    MAX_CREDIT_SHARDS,
    distribute_credit_amount,
)
from trusted_router.trust_ownership import require_owner_trust_budget

READ_SECONDS = 5.0
APPLY_SECONDS = 20.0
MAX_OWNER_WORKSPACES = 32
CREDIT_COLUMNS = (
    "workspace_id", "shard", "total_credits", "total_usage", "reserved",
    *CREDIT_BALANCE_TRUST_COLUMNS,
)
KEY_COLUMNS = (
    "key_hash", "shard", "limit_micro", "usage", "byok_usage", "reserved",
    "include_byok", "day_limit_micro", "week_limit_micro", "month_limit_micro",
    "day_usage", "day_start", "week_usage", "week_start", "month_usage", "month_start",
)
CAP_FIELDS = (
    "limit_microdollars", "limit_daily_microdollars", "limit_weekly_microdollars",
    "limit_monthly_microdollars",
)


def expand_credit_rows(rows: list[dict[str, Any]], target: int) -> list[dict[str, Any]]:
    """Conserve money exactly while preserving every existing hold's shard."""
    if not rows or not len(rows) <= target <= MAX_CREDIT_SHARDS:
        raise ValueError("expansion only; expected 1..64 configured shards")
    if [int(row["shard"]) for row in rows] != list(range(len(rows))):
        raise ValueError("incomplete or unexpected credit shard set")
    trust = [rows[0].get(column) for column in CREDIT_BALANCE_TRUST_COLUMNS]
    for row in rows:
        credits, usage, reserved = (int(row[name]) for name in ("total_credits", "total_usage", "reserved"))
        if min(credits, usage, reserved) < 0 or usage + reserved > credits:
            raise ValueError("invalid credit counter or exhausted shard")
        if [row.get(column) for column in CREDIT_BALANCE_TRUST_COLUMNS] != trust:
            raise ValueError("divergent trust replication")
        if row.get("billing_pause_causes"):
            raise ValueError("workspace is paused")
    if len(rows) == target:
        return copy.deepcopy(rows)
    free = sum(int(row["total_credits"]) - int(row["total_usage"]) - int(row["reserved"]) for row in rows)
    parts = distribute_credit_amount(free, target)
    result = copy.deepcopy(rows)
    for shard in range(len(rows), target):
        result.append({
            "workspace_id": rows[0]["workspace_id"], "shard": shard,
            "total_usage": 0, "reserved": 0,
            **dict(zip(CREDIT_BALANCE_TRUST_COLUMNS, trust, strict=True)),
        })
    for shard, row in enumerate(result):
        row["total_credits"] = int(row["total_usage"]) + int(row["reserved"]) + parts[shard]
    return result


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"invalid {name}")
    result = int(value)
    if str(result) != str(value):
        raise ValueError(f"invalid {name}")
    return result


def make_plan(
    workspace: dict[str, Any], credit: dict[str, Any], key: dict[str, Any],
    credit_rows: list[dict[str, Any]], key_rows: list[dict[str, Any]],
    owner: dict[str, Any], owner_credits: dict[str, dict[str, Any]], target: int,
) -> dict[str, Any]:
    """Validate one coherent transactional snapshot before staging mutations."""
    ws = workspace["id"]
    if workspace.get("deleted") or workspace.get("federated_home"):
        raise ValueError("workspace is deleted or federated")
    if workspace.get("billing_paused") or workspace.get("billing_pause_causes"):
        raise ValueError("workspace is paused")
    if credit.get("workspace_id") != ws or key.get("workspace_id") != ws:
        raise ValueError("workspace mismatch")
    if key.get("disabled") or key.get("federated_home") or key.get("management"):
        raise ValueError("key is disabled, federated, or management")
    if any(key.get(name) is not None for name in CAP_FIELDS):
        raise ValueError("only uncapped keys may be expanded without a pause")
    current = _integer(credit.get("shard_count", 1), "credit shard count")
    key_current = _integer(key.get("usage_shard_count", 1), "key shard count")
    if not 1 <= current <= target <= MAX_CREDIT_SHARDS or not 1 <= key_current <= target:
        raise ValueError("invalid target or consolidation requested")
    if len(credit_rows) != current or len(key_rows) != key_current:
        raise ValueError("metadata and physical shard counts differ")
    if any(row["workspace_id"] != ws for row in credit_rows):
        raise ValueError("credit row workspace mismatch")
    if [int(row["shard"]) for row in key_rows] != list(range(key_current)):
        raise ValueError("incomplete or unexpected key shard set")
    for row in key_rows:
        if row["key_hash"] != key["hash"]:
            raise ValueError("key row identity mismatch")
        if any(row.get(name) is not None for name in ("limit_micro", "day_limit_micro", "week_limit_micro", "month_limit_micro")):
            raise ValueError("typed key caps disagree with uncapped metadata")
        if int(row["reserved"]) != 0:
            raise ValueError("uncapped key has a reserved hold")
        if any(int(row[name]) < 0 for name in ("usage", "byok_usage", "day_usage", "week_usage", "month_usage")):
            raise ValueError("negative key usage")
        if row["include_byok"] != key.get("include_byok_in_limit", True):
            raise ValueError("typed key flags disagree with metadata")
    if owner.get("id") != workspace["owner_user_id"] or ws not in owner_credits:
        raise ValueError("owner inventory missing target workspace")
    if not 1 <= len(owner_credits) <= MAX_OWNER_WORKSPACES:
        raise ValueError("owner inventory exceeds bounded repair scope")
    if _integer(owner.get("owner_workspace_count"), "owner workspace count") != len(owner_credits):
        raise ValueError("owner inventory count mismatch")
    counts = []
    for owned_id, account in owner_credits.items():
        if account.get("workspace_id") != owned_id:
            raise ValueError("owner inventory credit account mismatch")
        count = _integer(account.get("shard_count", 1), "owner shard count")
        if not 1 <= count <= MAX_CREDIT_SHARDS:
            raise ValueError("invalid owner shard count")
        counts.append(target if owned_id == ws else count)
    require_owner_trust_budget(counts)
    expanded = expand_credit_rows(credit_rows, target)
    return {
        "workspace_id": ws, "key_id": key["hash"], "target": target,
        "current_credit_shards": current, "current_key_shards": key_current,
        "credit": {**credit, "shard_count": target},
        "key": {**key, "usage_shard_count": target},
        "credit_rows": expanded, "key_rows": key_rows,
        "totals": {name: sum(int(row[name]) for row in credit_rows) for name in ("total_credits", "total_usage", "reserved")},
        "changed": current != target or key_current != target,
    }


class Reader:
    def __init__(self, client: Any, session: str, transaction: dict[str, Any], deadline: float, *, priority: str = "PRIORITY_LOW"):
        self.client, self.session, self.transaction, self.deadline = client, session, transaction, deadline
        self.priority = priority

    def timeout(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("bounded billing expansion deadline expired")
        return min(READ_SECONDS, remaining)

    def read(self, table: str, columns: tuple[str, ...], keys: list[list[Any]]) -> list[dict[str, Any]]:
        from google.protobuf.json_format import MessageToDict

        result = self.client.read(request={
            "session": self.session, "transaction": self.transaction,
            "table": table, "columns": columns,
            "key_set": {"keys": keys}, "limit": len(keys),
            "request_options": {"priority": self.priority, "request_tag": "tr_ops_expand_billing"},
        }, timeout=self.timeout(), retry=None)
        return [dict(zip(columns, row, strict=True)) for row in MessageToDict(result._pb).get("rows", [])]

    def entities(self, keys: list[list[str]]) -> dict[tuple[str, str], dict[str, Any]]:
        result = {}
        for row in self.read("tr_entities", ("kind", "id", "body"), keys):
            body = json.loads(row["body"])
            if not isinstance(body, dict):
                raise ValueError("invalid entity JSON")
            result[(row["kind"], row["id"])] = body
        if len(result) != len(keys):
            raise ValueError("missing required entity")
        return result

    def plan(self, workspace_id: str, key_id: str, target: int) -> dict[str, Any]:
        from google.protobuf.json_format import MessageToDict

        records = self.entities([["workspace", workspace_id], ["credit", workspace_id], ["api_key", key_id]])
        workspace = records[("workspace", workspace_id)]
        owner_id = workspace["owner_user_id"]
        result = self.client.execute_sql(request={
            "session": self.session, "transaction": self.transaction,
            "sql": "SELECT workspace_id FROM tr_owner_workspace WHERE owner_user_id=@owner ORDER BY workspace_id LIMIT 33",
            "params": {"owner": owner_id}, "param_types": {"owner": {"code": "STRING"}},
            "request_options": {"priority": self.priority, "request_tag": "tr_ops_expand_billing"},
        }, timeout=self.timeout(), retry=None)
        inventory = [row[0] for row in MessageToDict(result._pb).get("rows", [])]
        if not 1 <= len(inventory) <= MAX_OWNER_WORKSPACES:
            raise ValueError("missing or oversized owner inventory")
        owned = self.entities([["user", owner_id], *[["credit", ws] for ws in inventory]])
        # Match finalization's key -> credit lock order. The inverse produces
        # S-credit/X-key versus X-key/X-credit cycles on a busy account.
        key_rows = self.read("tr_key_limit", KEY_COLUMNS, [[key_id, str(shard)] for shard in range(MAX_CREDIT_SHARDS)])
        credits = self.read("tr_credit_balance", CREDIT_COLUMNS, [[workspace_id, str(shard)] for shard in range(MAX_CREDIT_SHARDS)])
        credits.sort(key=lambda row: int(row["shard"]))
        key_rows.sort(key=lambda row: int(row["shard"]))
        return make_plan(workspace, records[("credit", workspace_id)], records[("api_key", key_id)], credits, key_rows, owned[("user", owner_id)], {ws: owned[("credit", ws)] for ws in inventory}, target)


def mutations(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Update only capacity on old credit rows; never rewrite existing counters."""
    if not plan["changed"]:
        return []
    ws, key_id, target = plan["workspace_id"], plan["key_id"], plan["target"]
    old = plan["current_credit_shards"]
    changes = []
    if old != target:
        changes.append({"update": {
            "table": "tr_credit_balance", "columns": ["workspace_id", "shard", "total_credits", "updated_at"],
            "values": [[ws, str(row["shard"]), str(row["total_credits"]), "spanner.commit_timestamp()"] for row in plan["credit_rows"][:old]],
        }})
        changes.append({"insert": {
            "table": "tr_credit_balance", "columns": [*CREDIT_COLUMNS, "source_updated_at", "updated_at"],
            "values": [[_wire(row[column]) for column in CREDIT_COLUMNS] + ["spanner.commit_timestamp()"] * 2 for row in plan["credit_rows"][old:]],
        }})
    if plan["current_key_shards"] != target:
        changes.append({"insert": {
            "table": "tr_key_limit", "columns": ["key_hash", "shard", "include_byok", "source_updated_at", "updated_at"],
            "values": [[key_id, str(shard), plan["key"].get("include_byok_in_limit", True), "spanner.commit_timestamp()", "spanner.commit_timestamp()"] for shard in range(plan["current_key_shards"], target)],
        }})
    changes.append({"update": {
        "table": "tr_entities", "columns": ["kind", "id", "body", "updated_at"],
        "values": [[kind, identifier, json.dumps(plan[field], separators=(",", ":")), "spanner.commit_timestamp()"] for kind, identifier, field in (("credit", ws, "credit"), ("api_key", key_id, "key"))],
    }})
    return changes


def _wire(value: Any) -> Any:
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


def execute(client: Any, session: str, ws: str, key: str, target: int, *, apply: bool) -> dict[str, Any]:
    from google.api_core.exceptions import Aborted

    deadline = time.monotonic() + (APPLY_SECONDS if apply else 30)
    for attempt in range(6):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("bounded billing expansion deadline expired")
        mode = {"read_write": {}} if apply else {"read_only": {"strong": True}}
        tx = client.begin_transaction(request={"session": session, "options": mode}, timeout=min(READ_SECONDS, remaining), retry=None)
        reader = Reader(client, session, {"id": tx.id}, deadline, priority="PRIORITY_MEDIUM" if apply else "PRIORITY_LOW")
        committed = False
        aborted = False
        try:
            plan = reader.plan(ws, key, target)
            changes = mutations(plan) if apply else []
            if changes:
                client.commit(request={"session": session, "transaction_id": tx.id, "mutations": changes,
                    "request_options": {"transaction_tag": "tr_ops_expand_billing"}}, timeout=reader.timeout(), retry=None)
                committed = True
            return {name: plan[name] for name in ("workspace_id", "key_id", "target", "current_credit_shards", "current_key_shards", "totals", "changed")} | {"applied": bool(changes)}
        except Aborted:
            aborted = True
            if attempt == 5 or time.monotonic() >= deadline:
                raise
            time.sleep(min(0.1 * (attempt + 1), max(0, deadline - time.monotonic())))
        finally:
            if apply and not committed and not aborted:
                try:
                    client.rollback(request={"session": session, "transaction_id": tx.id}, timeout=2, retry=None)
                except Exception as exc:
                    print(json.dumps({"event": "expansion_rollback_error", "error_type": type(exc).__name__}), flush=True)
    raise RuntimeError("expansion retry exhausted")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--shards", type=int, default=16)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--account", help="Separate gcloud deployment identity, required for apply")
    args = parser.parse_args()
    from google.cloud.spanner_v1.services.spanner import SpannerClient
    from google.oauth2 import credentials as oauth_credentials
    from google.oauth2 import service_account

    if args.apply and (not args.account or "tr-ops-local" in args.account or not re.fullmatch(r"[A-Za-z0-9_.+@-]+@[A-Za-z0-9.-]+", args.account)):
        parser.error("apply requires a separate deployment identity")
    database = "projects/quill-cloud-proxy/instances/trusted-router-nam6/databases/trusted-router"

    def run(credentials: Any, *, apply: bool) -> dict[str, Any]:
        with SpannerClient(credentials=credentials) as client:
            session = client.create_session(request={"database": database}, timeout=READ_SECONDS, retry=None)
            try:
                return execute(client, session.name, args.workspace, args.key_id, args.shards, apply=apply)
            finally:
                client.delete_session(request={"name": session.name}, timeout=READ_SECONDS, retry=None)

    try:
        ops_credentials = service_account.Credentials.from_service_account_file(os.environ["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"], scopes=["https://www.googleapis.com/auth/spanner.data"])
        print(json.dumps({"preflight": run(ops_credentials, apply=False)}), flush=True)
        if args.apply:
            gcloud = shutil.which("gcloud")
            if not gcloud:
                raise ValueError("gcloud is required")
            env = {name: value for name, value in os.environ.items() if name != "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"}
            token = subprocess.run(  # noqa: S603 - validated operator identity; no shell; resolved executable.
                [gcloud, "auth", "print-access-token", "--account", args.account],
                check=True, capture_output=True, text=True, timeout=30, env=env,
            ).stdout.strip()
            print(json.dumps({"operator": args.account, "result": run(oauth_credentials.Credentials(token), apply=True)}), flush=True)
            verified = run(ops_credentials, apply=False)
            if verified["changed"]:
                raise RuntimeError("post-commit shard verification failed")
            print(json.dumps({"verification": verified}), flush=True)
    except Exception as exc:
        # Entity JSON includes credential hashes. Never dump plans or RPC payloads.
        print(json.dumps({"error_type": type(exc).__name__, "message": str(exc) if isinstance(exc, ValueError) else "repair did not verify; inspect exact-key state before retry"}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
