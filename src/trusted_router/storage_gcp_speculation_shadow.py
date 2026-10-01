"""Native Spanner-only shadow namespace. Mutation targets are closed and isolated.

All source reads are out of band, strongly consistent and bounded by identity.
No mutation here can name a credit, authorization, risk, or customer table.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

from google.cloud.spanner_v1 import COMMIT_TIMESTAMP, KeySet

from trusted_router.services.speculation_shadow import TABLES, ShadowMiss, canonical
from trusted_router.services.speculation_shadow import identity as shadow_identity
from trusted_router.storage_gcp_counters import credit_shard_count
from trusted_router.storage_models import ApiKey, CreditAccount, Workspace, workspace_billing_paused


def table_name(table: str) -> str:
    if table not in TABLES:
        raise ShadowMiss("shadow-namespace")
    return "tr_speculation_shadow_" + table


class Transaction:
    def __init__(self, transaction: Any, plane: str) -> None:
        self.transaction, self.plane = transaction, plane
        self.pending: dict[tuple[str, str], dict[str, Any]] = {}

    def get(self, table: str, identity: str) -> dict[str, Any] | None:
        if (table, identity) in self.pending:
            return json.loads(canonical(self.pending[table, identity]))
        key: list[Any] = [self.plane, *identity.rsplit(":", 1)] if table == "event" else [self.plane, identity]
        if table == "event":
            key[-1] = int(key[-1])
        rows = list(self.transaction.read(table_name(table), ["body"], KeySet(keys=[key])))
        return json.loads(rows[0][0]) if rows else None

    def put(self, table: str, identity: str, value: dict[str, Any]) -> None:
        table_name(table)
        self.pending[table, identity] = json.loads(canonical(value))

    def flush(self) -> None:
        for (table, identity), value in self.pending.items():
            columns = ["plane", "identity", "body", "updated_at"]
            values: list[Any] = [self.plane, identity, canonical(value).decode("ascii"), COMMIT_TIMESTAMP]
            if table == "event":
                incarnation, sequence = identity.rsplit(":", 1)
                columns = ["plane", "producer_incarnation", "sequence", "body", "updated_at"]
                values = [self.plane, incarnation, int(sequence), canonical(value).decode("ascii"), COMMIT_TIMESTAMP]
            self.transaction.insert_or_update(table_name(table), columns, [values])


class SpannerSpeculationShadow:
    def __init__(self, backend: Any, settings: Any) -> None:
        self.backend, self.settings = backend, settings
        self.database, self.plane = backend._database, settings.speculation_shadow_plane

    def ready(self) -> None:
        with self.database.snapshot(multi_use=True) as snapshot:
            for table in sorted(TABLES):
                list(snapshot.read(table_name(table), ["body"], KeySet(keys=[[self.plane, "readiness", 0] if table == "event" else [self.plane, "readiness"]])))

    def transaction(self, operation: Any) -> Any:
        def apply(transaction: Any) -> Any:
            tx = Transaction(transaction, self.plane)
            result = operation(tx)
            tx.flush()
            return result
        return self.database.run_in_transaction(apply, timeout_secs=2)

    def read(self, table: str, identity: str) -> dict[str, Any] | None:
        with self.database.snapshot() as snapshot:
            return Transaction(snapshot, self.plane).get(table, identity)

    def boot(self, kid: str) -> Any:
        return self.backend.get_gateway_boot(kid)

    def resolve(self, lookup: str, now: int) -> dict[str, Any]:
        pt = self.backend._param_types
        with self.database.snapshot(multi_use=True) as snapshot:
            # Lookup binding, key, workspace, ledger, trust and paid coverage
            # share one strong snapshot. A remapped lookup cannot reuse an old key.
            reference = self.backend._read_entity_from(snapshot, "api_key_lookup", lookup, dict)
            if not reference or not isinstance(reference.get("key_id"), str):
                raise ShadowMiss("key-unresolved")
            key = self.backend._read_entity_from(snapshot, "api_key", reference["key_id"], ApiKey)
            if key is None or key.lookup_hash != lookup:
                raise ShadowMiss("key-unresolved")
            ws = key.workspace_id
            account = self.backend._read_entity_from(snapshot, "credit", ws, CreditAccount)
            workspace = self.backend._read_entity_from(snapshot, "workspace", ws, Workspace)
            if account is None or workspace is None or getattr(workspace, "deleted", False):
                raise ShadowMiss("workspace-unresolved")
            count = credit_shard_count(account)
            rows = list(snapshot.execute_sql(
                "SELECT shard, total_credits, total_usage, reserved, trust_tier, trust_latched_at, "
                "billing_pause_causes, pause_epoch, trust_reconciled_through FROM tr_credit_balance "
                "WHERE workspace_id=@workspace_id AND shard>=0 AND shard<@shard_count ORDER BY shard",
                params={"workspace_id": ws, "shard_count": count},
                param_types={"workspace_id": pt.STRING, "shard_count": pt.INT64}))
            limits = list(snapshot.execute_sql(
                "SELECT shard, limit_micro, day_limit_micro, week_limit_micro, month_limit_micro "
                "FROM tr_key_limit WHERE key_hash=@key_id AND shard>=0 AND shard<@shard_count ORDER BY shard",
                params={"key_id": key.hash, "shard_count": key.usage_shard_count},
                param_types={"key_id": pt.STRING, "shard_count": pt.INT64}))
            events = list(snapshot.execute_sql(
                "SELECT kind, provider, credited_micro, recovered_micro, lifecycle_status, "
                "unrecovered_micro, recovery_target FROM tr_trust_event "
                "WHERE workspace_id=@workspace_id ORDER BY event_id LIMIT 1001",
                params={"workspace_id": ws}, param_types={"workspace_id": pt.STRING}))
            anchor = Transaction(snapshot, self.plane).get("paid", ws) or {}
        if [int(r[0]) for r in rows] != list(range(count)):
            raise ShadowMiss("missing-shards")
        trust = {(r[4], r[5], tuple(r[6] or []), r[7], r[8]) for r in rows}
        if len(trust) != 1:
            raise ShadowMiss("divergent-trust")
        tier, latch, pause, _, reconciled = trust.pop()
        reconciled_at = int(reconciled.timestamp()) if reconciled else 0
        credits = sum(int(r[1]) for r in rows)
        # Conservative initial subset: any recovery/adverse row is unknown.
        # A bounded complete ledger with exact inflow conservation is required.
        # Equality of net credits alone cannot exclude offsetting unknown
        # transfers. Require independently reconciled source coverage; this PR
        # does not fabricate or backfill that missing provenance.
        ledger_digest = hashlib.sha256(canonical(events)).hexdigest()
        source_covered = (bool(anchor.get("coverage_version"))
                          and anchor.get("source_ledger_digest") == ledger_digest
                          and anchor.get("source_credits_micro") == credits
                          and 0 <= anchor.get("source_as_of", -1) <= now < anchor.get("source_expires_at", 0))
        complete = source_covered and bool(events) and len(events) < 1001 and all(
            r[0] in {"payment", "grant"} and r[4] == "succeeded" and not any(r[i] for i in (3, 5, 6))
            and r[2] is not None and int(r[2]) >= 0 for r in events)
        accounted = sum(int(r[2] or 0) for r in events)
        nonpaid = sum(int(r[2] or 0) for r in events if r[0] != "payment" or r[1] not in set(self.settings.trust_qualifying_providers.split(",")))
        paid: dict[str, Any] = {**{k: anchor[k] for k in ("coverage_version", "source_ledger_digest", "source_credits_micro", "source_as_of", "source_expires_at") if k in anchor}, "version": 1, "complete": complete, "conserved": accounted == credits,
                "accounted_credits_micro": accounted, "nonqualifying_upper_micro": nonpaid,
                "as_of": now, "expires_at": now + 10, "unresolved_recovery": not complete}
        policy_fields = ("hash", "lookup_hash", "workspace_id", "disabled", "management", "scopes", "app_id",
                         "federated_home", "budget_strict", "limit_microdollars", "limit_daily_microdollars",
                         "limit_weekly_microdollars", "limit_monthly_microdollars", "expires_at", "updated_at")
        policy_digest = hashlib.sha256(canonical({k: getattr(key, k, None) for k in policy_fields})).hexdigest()
        def project_sources(tx: Any) -> None:
            tx.put("paid", ws, paid)
            scope_id = shadow_identity("key", ws, key.hash)
            scope = tx.get("scope", scope_id)
            if scope is not None:
                if scope.get("policy_digest") != policy_digest:
                    scope["epoch"] += 1
                    scope["clean_since"] = now
                scope.update(policy_digest=policy_digest, policy_as_of=now, policy_expires_at=now + 10)
                tx.put("scope", scope_id, scope)
        self.transaction(project_sources)
        expiry = int(dt.datetime.fromisoformat(key.expires_at.replace("Z", "+00:00")).timestamp()) if key.expires_at else now + 30
        eligible = (not any((key.disabled, key.management, key.scopes, key.app_id, key.federated_home, key.budget_strict))
                    and all(getattr(key, name) is None for name in ("limit_microdollars", "limit_daily_microdollars", "limit_weekly_microdollars", "limit_monthly_microdollars"))
                    and [r[0] for r in limits] == list(range(key.usage_shard_count))
                    and all(v is None for r in limits for v in r[1:]) and expiry > now)
        return {"workspace_id": ws, "key_id": key.hash, "lookup_digest": lookup, "key_eligible": eligible,
                "key_expires_at": expiry, "shards_complete": True, "tier": int(tier or 0), "latched": bool(latch),
                "paused": bool(pause) or workspace_billing_paused(workspace), "reconciled_through": reconciled_at,
                "trust_fresh_until": reconciled_at + self.settings.trust_reconcile_max_age_seconds,
                "credits": credits, "usage": sum(int(r[2]) for r in rows), "reserved": sum(int(r[3]) for r in rows)}
