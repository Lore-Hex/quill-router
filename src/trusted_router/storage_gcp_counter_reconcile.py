"""Typed-ledger invariant audit and repair helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from trusted_router.storage_gcp_counters import credit_shard_count, key_usage_shard_count
from trusted_router.storage_models import ApiKey, CreditAccount, Workspace

# ── Standing typed-side invariant auditor ───────────────────────────────────
# This auditor is the standing typed-side tripwire: for every typed counter row,
# `reserved` MUST equal the sum of that scope's OPEN typed-origin request holds
# plus remaining regional quota escrow, and MUST be >= 0. A violation means a
# hold leaked or a release double-applied. Run it on a schedule + before each
# ramp batch; wire an alert on the "release row-count != 1" log line as the live
# signal between audits.

# Shard-aware (the typed counter PK is (scope, shard); reservations carry the
# per-scope shard), COALESCE so an empty SUM reads 0.
_OPEN_CREDIT_HOLDS = (
    "SELECT workspace_id, credit_shard, ws_shard, "
    "COALESCE(SUM(credit_reserved_micro), 0) "
    "FROM tr_reservation WHERE settled = false "
    "GROUP BY workspace_id, credit_shard, ws_shard"
)
_OPEN_KEY_HOLDS = (
    "SELECT key_hash, key_shard, COALESCE(SUM(key_reserved_micro), 0) "
    "FROM tr_reservation WHERE settled = false GROUP BY key_hash, key_shard"
)
_OPEN_REGIONAL_QUOTA_ESCROW = (
    "/* open_regional_quota_escrow */ "
    "SELECT open_index.id, open_index.body, lease_record.id, lease_record.body "
    "FROM tr_entities AS open_index "
    "LEFT JOIN tr_entities AS lease_record "
    "ON lease_record.kind='regional_quota_lease' "
    "AND lease_record.id=JSON_VALUE(open_index.body, '$.lease_entity_id') "
    "WHERE open_index.kind='regional_quota_lease_open' "
    "ORDER BY open_index.id"
)
_OPEN_REGIONAL_QUOTA_STATES = frozenset({"pending", "active", "draining", "quarantined"})
_NONCLOSED_REGIONAL_QUOTA_LEASES_FOR_REPAIR = (
    "/* nonclosed_regional_quota_leases_for_repair */ "
    "SELECT COUNT(*) FROM tr_entities "
    "WHERE kind='regional_quota_lease' "
    "AND JSON_VALUE(body, '$.workspace_id')=@ws "
    "AND (JSON_VALUE(body, '$.state') IS NULL "
    "OR JSON_VALUE(body, '$.state')!='closed')"
)


@dataclass
class InvariantReport:
    credit_rows: int = 0
    key_rows: int = 0
    regional_lease_rows: int = 0
    credit_violations: int = 0  # reserved != open-hold sum, or reserved < 0
    key_violations: int = 0
    regional_lease_violations: int = 0
    samples: dict[str, dict] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return (
            self.credit_violations == 0
            and self.key_violations == 0
            and self.regional_lease_violations == 0
        )

    def summary(self) -> str:
        return (
            f"credit: {self.credit_violations}/{self.credit_rows} | "
            f"key: {self.key_violations}/{self.key_rows} | "
            f"regional lease: {self.regional_lease_violations}/{self.regional_lease_rows} | "
            f"{'CLEAN' if self.clean else 'VIOLATIONS'}"
        )


def _entity_body(raw: Any) -> dict[str, Any]:
    body = json.loads(str(raw))
    if not isinstance(body, dict):
        raise ValueError("entity body is not an object")
    return body


def _required_int(body: dict[str, Any], field_name: str) -> int:
    value = body.get(field_name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field_name} is not an integer")
    return value


def _regional_quota_escrow(
    rows: list[list[Any]],
) -> tuple[dict[tuple[str, int], int], dict[str, str]]:
    """Return outstanding global lease escrow grouped by typed credit shard.

    Regional leases reserve credit directly instead of creating an ordinary
    ``tr_reservation`` row. The open index and canonical lease are both checked
    so a missing, duplicated, stale, or malformed index cannot silently alter
    the expected typed balance.
    """

    holds: dict[tuple[str, int], int] = {}
    errors: dict[str, str] = {}
    seen_leases: set[str] = set()
    for row in rows:
        index_id = str(row[0])
        try:
            open_index = _entity_body(row[1])
            lease_entity_id = open_index.get("lease_entity_id")
            if not isinstance(lease_entity_id, str) or not lease_entity_id:
                raise ValueError("open index has no lease_entity_id")
            if row[2] is None or str(row[2]) != lease_entity_id or row[3] is None:
                raise ValueError("indexed canonical lease is missing")
            if lease_entity_id in seen_leases:
                raise ValueError("canonical lease has more than one open index")
            seen_leases.add(lease_entity_id)

            lease = _entity_body(row[3])
            workspace_id = lease.get("workspace_id")
            region = lease.get("region")
            lease_id = lease.get("lease_id")
            expires_at = lease.get("expires_at")
            if not all(isinstance(value, str) and value for value in (
                workspace_id,
                region,
                lease_id,
                expires_at,
            )):
                raise ValueError("canonical lease identity is incomplete")
            if lease_entity_id != f"{workspace_id}#{region}#{lease_id}":
                raise ValueError("canonical lease id does not match its body")
            if index_id != f"{expires_at}#{workspace_id}#{region}#{lease_id}":
                raise ValueError("open index id does not match the canonical lease")
            for field_name in ("workspace_id", "region", "lease_id", "expires_at"):
                if open_index.get(field_name) != lease.get(field_name):
                    raise ValueError(f"open index {field_name} does not match canonical lease")

            state = lease.get("state")
            if state not in _OPEN_REGIONAL_QUOTA_STATES:
                raise ValueError(f"open index points to lease in {state!r} state")
            granted = _required_int(lease, "granted_microdollars")
            reconciled = _required_int(lease, "reconciled_spent_microdollars")
            shard = _required_int(lease, "credit_shard")
            if granted < 0 or reconciled < 0 or reconciled > granted or shard < 0:
                raise ValueError("canonical lease escrow values are invalid")
            outstanding = granted - reconciled
            scope = (str(workspace_id), shard)
            holds[scope] = holds.get(scope, 0) + outstanding
        except (TypeError, ValueError) as exc:
            errors[index_id] = str(exc)
    return holds, errors


def audit_typed_invariants(store: Any, *, max_samples: int = 20) -> InvariantReport:
    """Assert, in one consistent snapshot, that every typed `reserved` equals the
    sum of that (scope, shard)'s request holds and regional lease escrow.
    Checks BOTH directions: a typed row whose reserved != its open holds, AND an
    open hold group with no typed row (that leak is invisible if you only iterate
    typed rows). Read-only."""
    report = InvariantReport()

    with store._database.snapshot(multi_use=True) as snap:
        typed_credit = {
            (r[0], r[1]): int(r[2]) for r in snap.execute_sql(
                "SELECT workspace_id, shard, reserved FROM tr_credit_balance"
            )
        }
        typed_key = {
            (r[0], r[1]): int(r[2]) for r in snap.execute_sql(
                "SELECT key_hash, shard, reserved FROM tr_key_limit"
            )
        }
        credit_holds: dict[tuple[str, int], int] = {}
        for row in snap.execute_sql(_OPEN_CREDIT_HOLDS):
            shard = int(row[1] if row[1] is not None else (row[2] or 0))
            scope = (str(row[0]), shard)
            credit_holds[scope] = credit_holds.get(scope, 0) + int(row[3] or 0)
        key_holds = {(r[0], r[1]): int(r[2] or 0) for r in snap.execute_sql(_OPEN_KEY_HOLDS)}
        regional_rows = list(snap.execute_sql(_OPEN_REGIONAL_QUOTA_ESCROW))

    regional_holds, regional_errors = _regional_quota_escrow(regional_rows)
    for scope, held in regional_holds.items():
        credit_holds[scope] = credit_holds.get(scope, 0) + held

    def _sample(key: str, value: dict) -> None:
        if len(report.samples) < max_samples:
            report.samples[key] = value

    def _check(typed: dict, holds: dict, kind: str) -> tuple[int, int]:
        violations = 0
        # forward: every typed row's reserved must equal its open holds, and >= 0.
        for scope, reserved in typed.items():
            expected = holds.get(scope, 0)
            if reserved != expected or reserved < 0:
                violations += 1
                _sample(f"{kind}:{scope[0]}:{scope[1]}",
                        {"typed_reserved": reserved, "open_holds": expected})
        # reverse: an open hold group with NO typed row is a leak the forward pass
        # cannot see (typed row deleted/never created while holds are outstanding).
        for scope, held in holds.items():
            if held > 0 and scope not in typed:
                violations += 1
                _sample(f"{kind}-orphan-hold:{scope[0]}:{scope[1]}",
                        {"typed_reserved": None, "open_holds": held})
        return len(typed), violations

    report.credit_rows, report.credit_violations = _check(typed_credit, credit_holds, "credit")
    report.key_rows, report.key_violations = _check(typed_key, key_holds, "api_key")
    report.regional_lease_rows = len(regional_rows)
    report.regional_lease_violations = len(regional_errors)
    for index_id, error in regional_errors.items():
        _sample(f"regional-lease:{index_id}", {"error": error})
    return report


# ── Repair: clobbered typed `reserved` ──────────────────────────────────────
# The 2026-06-25 incident's accumulated damage: before the ownership split the
# mirror overwrote typed `reserved` with the stale JSON value, so already-typed
# workspaces have `reserved` frozen far from the truth (auditor flags them). Fix:
# set credit + each key `reserved` = SUM of that scope's OPEN typed holds. We do
# NOT touch total_usage — it is monotonic and verified ledger-consistent
# (JSON baseline + Σ settled actuals) for active workspaces; the lone usage-damaged
# case (ea7dd3d8) is handled separately. Fail-closed: requires billing_paused so
# the open-hold set is stable while we write.


@dataclass
class RepairResult:
    workspace_id: str
    ready: bool
    reasons: list[str] = field(default_factory=list)
    applied: bool = False
    credit_reserved_before: int | None = None
    credit_reserved_after: int | None = None
    keys_repaired: int = 0


def repair_typed_reserved(store: Any, workspace_id: str, *, apply: bool = False) -> RepairResult:
    """Set typed `reserved` = SUM(open typed holds) for an already-typed PAUSED
    workspace (credit + every key). Read-only when apply=False (reports the before/
    after). Fail-closed: refuses unless billing_paused."""
    pt = store._param_types
    res = RepairResult(workspace_id=workspace_id, ready=False)
    # SHARD-0 ONLY (prod has no other shards). Everything is filtered to shard 0;
    # a sharded workspace is refused (no shard-0 typed row, or a key row missing).
    open_credit_sql = (
        "SELECT COALESCE(SUM(credit_reserved_micro),0) FROM tr_reservation "
        "WHERE workspace_id=@ws AND ws_shard=0 AND settled=false"
    )
    open_key_sql = (
        "SELECT COALESCE(SUM(key_reserved_micro),0) FROM tr_reservation "
        "WHERE key_hash=@kh AND key_shard=0 AND settled=false"
    )
    credit_row_sql = "SELECT reserved FROM tr_credit_balance WHERE workspace_id=@pk AND shard=0"
    key_row_sql = "SELECT reserved FROM tr_key_limit WHERE key_hash=@pk AND shard=0"
    nonzero_key_shard_sql = (
        "SELECT COUNT(*) FROM tr_reservation "
        "WHERE key_hash=@kh AND settled=false AND key_shard!=0"
    )
    nonzero_shard_sql = (
        "SELECT COUNT(*) FROM tr_reservation "
        "WHERE workspace_id=@ws AND settled=false AND ws_shard!=0"
    )

    workspace = store.get_workspace(workspace_id)
    credit_account = store.get_credit_account(workspace_id)
    key_bodies = [
        b for b in store._list_entities("api_key", cls=dict)
        if b.get("workspace_id") == workspace_id
    ]
    key_hashes = [str(body["hash"]) for body in key_bodies]
    with store._database.snapshot(multi_use=True) as snap:
        cb = list(snap.execute_sql(
            credit_row_sql, params={"pk": workspace_id}, param_types={"pk": pt.STRING},
        ))
        open_credit = list(snap.execute_sql(
            open_credit_sql, params={"ws": workspace_id}, param_types={"ws": pt.STRING},
        ))[0][0]
        nonzero_shard = list(snap.execute_sql(
            nonzero_shard_sql, params={"ws": workspace_id}, param_types={"ws": pt.STRING},
        ))[0][0]
        open_regional_leases = list(snap.execute_sql(
            _NONCLOSED_REGIONAL_QUOTA_LEASES_FOR_REPAIR,
            params={"ws": workspace_id},
            param_types={"ws": pt.STRING},
        ))[0][0]

    if workspace is None or not getattr(workspace, "billing_paused", False):
        res.reasons.append("workspace not billing-paused — pause it before repair")
    if credit_account is not None and credit_shard_count(credit_account) != 1:
        res.reasons.append("credit ledger is sharded — consolidate before shard-zero repair")
    if any(key_usage_shard_count(body) != 1 for body in key_bodies):
        res.reasons.append("API-key usage is sharded — consolidate before shard-zero repair")
    if not cb:
        res.reasons.append("no typed credit row")
    if int(nonzero_shard) != 0:
        res.reasons.append(f"{nonzero_shard} open holds on a nonzero shard — sharded ws not handled")
    if int(open_regional_leases) != 0:
        res.reasons.append(
            f"{open_regional_leases} regional quota leases are open — drain them before repair"
        )
    res.ready = not res.reasons
    if cb:
        res.credit_reserved_before = int(cb[0][0])
        res.credit_reserved_after = int(open_credit)
    if not res.ready or not apply:
        return res

    cts = store._spanner.COMMIT_TIMESTAMP

    def _txn(transaction: Any) -> dict | None:
        # Re-read everything INSIDE the txn and validate the COMPLETE plan before
        # any write. A missing typed row (a key deleted mid-repair, or never
        # created) must ABORT — never be re-created as a partial (uncapped) row.
        ws = store._read_entity_tx(transaction, "workspace", workspace_id, Workspace)
        if ws is None or not ws.billing_paused:
            return None
        credit = store._read_entity_tx(transaction, "credit", workspace_id, CreditAccount)
        if credit is not None and credit_shard_count(credit) != 1:
            return None
        if int(list(transaction.execute_sql(
            nonzero_shard_sql, params={"ws": workspace_id}, param_types={"ws": pt.STRING},
        ))[0][0]) != 0:
            return None
        if int(list(transaction.execute_sql(
            _NONCLOSED_REGIONAL_QUOTA_LEASES_FOR_REPAIR,
            params={"ws": workspace_id},
            param_types={"ws": pt.STRING},
        ))[0][0]) != 0:
            return None
        if not list(transaction.execute_sql(
            credit_row_sql, params={"pk": workspace_id}, param_types={"pk": pt.STRING},
        )):
            return None  # no shard-0 credit row — abort
        oc = list(transaction.execute_sql(
            open_credit_sql, params={"ws": workspace_id}, param_types={"ws": pt.STRING},
        ))[0][0]
        plan: list[tuple[str, int]] = []
        for kh in key_hashes:
            key_obj = store._read_entity_tx(transaction, "api_key", kh, ApiKey)
            if key_obj is None or key_usage_shard_count(key_obj) != 1:
                return None
            if not list(transaction.execute_sql(
                key_row_sql, params={"pk": kh}, param_types={"pk": pt.STRING},
            )):
                return None  # typed key row missing — abort, never create a partial row
            if int(list(transaction.execute_sql(
                nonzero_key_shard_sql, params={"kh": kh}, param_types={"kh": pt.STRING},
            ))[0][0]) != 0:
                return None  # key hold on a nonzero shard — would write reserved low; abort
            ok = list(transaction.execute_sql(
                open_key_sql, params={"kh": kh}, param_types={"kh": pt.STRING},
            ))[0][0]
            plan.append((kh, int(ok)))
        # all rows exist + validated — now write (insert_or_update UPDATES them).
        transaction.insert_or_update(
            table="tr_credit_balance",
            columns=("workspace_id", "shard", "reserved", "updated_at"),
            values=[(workspace_id, 0, int(oc), cts)],
        )
        for kh, ok in plan:
            transaction.insert_or_update(
                table="tr_key_limit",
                columns=("key_hash", "shard", "reserved", "updated_at"),
                values=[(kh, 0, ok, cts)],
            )
        return {"keys": len(plan)}

    result = store._run_in_transaction(_txn)
    if result is None:
        res.ready = False
        res.reasons.append(
            "aborted: not paused / open regional lease / nonzero shard / "
            "a typed row was missing (key deleted?)"
        )
        return res
    res.applied = True
    res.keys_repaired = result["keys"]
    return res
