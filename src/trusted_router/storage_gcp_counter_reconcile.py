"""Typed counter operator helpers that remain after JSON money retirement.

The surviving tools either prepare a drained never-typed workspace for
activation or audit/repair the typed-side reserved invariant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trusted_router.storage_gcp_counters import credit_shard_count, key_usage_shard_count
from trusted_router.storage_models import ApiKey, CreditAccount, Workspace

# ── Step 6: ledger-derived flip reconciliation ──────────────────────────────
# Seeds the typed-DML-owned counters for a workspace at the moment it is
# activated. This is the full-row seed for drained never-typed workspaces and
# the only sanctioned writer of typed reserved/total_usage outside the typed
# authorize/finalize DML.
#
# Safe ONLY for a NEVER-TYPED, DRAINED workspace (fail-closed). A workspace with
# prior typed-origin history (the cohort, or a rolled-back ws like the incident's
# ea7dd3d8) already has typed gross counters that JSON does not reflect; blindly
# seeding from JSON would lose typed-era usage, so this tool REFUSES it (needs a
# ledger reconcile instead). The caller MUST first QUIESCE the workspace (disable
# its keys) and DRAIN in-flight requests — incl. no-hold (uncapped / BYOK-excluded)
# paths that settle usage without a reserved hold — before apply=True
# (codex Step-6 design: enforced quiesce, not observed idle, is the safety).

# Full typed-row column sets — the seed writes reserved/usage too (unlike the
# narrowed mirror constants, which deliberately omit the typed-owned columns).
_CREDIT_SEED_COLUMNS = (
    "workspace_id", "shard", "total_credits", "total_usage", "reserved",
    "source_updated_at", "updated_at",
)
_KEY_SEED_COLUMNS = (
    "key_hash", "shard", "limit_micro", "usage", "byok_usage", "reserved",
    "include_byok", "day_limit_micro", "week_limit_micro", "month_limit_micro",
    "source_updated_at", "updated_at",
)


@dataclass
class FlipReadiness:
    workspace_id: str
    ready: bool
    reasons: list[str] = field(default_factory=list)  # why NOT ready (empty == ready)
    applied: bool = False
    credit_seeded: dict | None = None
    keys_seeded: int = 0


def reconcile_for_flip(store: Any, workspace_id: str, *, apply: bool = False) -> FlipReadiness:
    """Assess (and optionally apply) the typed-counter SEED for flipping ONE
    never-typed, drained workspace to typed enforcement.

    Fail-closed: refuses unless the workspace has a credit account, has NO
    tr_reservation history (never typed), and is fully drained (json credit
    reserved == 0 and every json api_key reserved == 0). With apply=True it
    re-reads the JSON rows INSIDE one Spanner transaction, re-checks the
    reserved==0 predicates (a legacy hold racing the seed aborts it), and upserts
    the full typed rows: total_credits/total_usage (credit) and
    limit/usage/byok_usage/include_byok (key) from JSON, with reserved = 0 (the
    workspace is drained and never-typed, so there are no open holds to carry).
    Read-only when apply=False.

    CAUTION — reserved==0 does NOT by itself prove drained: a no-hold (uncapped /
    BYOK-excluded) request can be mid-flight with reserved==0 and settle usage
    AFTER the seed, leaving typed usage stale-low. The caller MUST enforce quiesce
    (disable the workspace's keys) and drain in-flight requests first; this
    function only checks DB predicates. The key set is captured at assess time, so
    quiesce must also prevent a new key appearing mid-flip. Run before the cohort
    gate flip, never after.
    """
    pt = store._param_types
    res = FlipReadiness(workspace_id=workspace_id, ready=False)

    credit = next(
        (b for b in store._list_entities("credit", cls=dict) if b.get("workspace_id") == workspace_id),
        None,
    )
    if credit is None:
        res.reasons.append("no credit account")
        return res
    keys = [b for b in store._list_entities("api_key", cls=dict) if b.get("workspace_id") == workspace_id]

    workspace = store.get_workspace(workspace_id)
    if workspace is None or not getattr(workspace, "billing_paused", False):
        res.reasons.append("workspace not billing-paused — pause (quiesce) it before flipping")

    with store._database.snapshot() as snap:
        typed_history = list(snap.execute_sql(
            "SELECT COUNT(*) FROM tr_reservation WHERE workspace_id=@ws",
            params={"ws": workspace_id}, param_types={"ws": pt.STRING},
        ))[0][0]
    if int(typed_history) != 0:
        res.reasons.append(
            f"{typed_history} tr_reservation rows (typed history) — needs a ledger reconcile, not a JSON seed"
        )
    if int(credit.get("reserved_microdollars", 0)) != 0:
        res.reasons.append(
            f"credit.reserved={credit['reserved_microdollars']} (open legacy holds — quiesce + drain first)"
        )
    for k in keys:
        if int(k.get("reserved_microdollars", 0)) != 0:
            res.reasons.append(f"key {k['hash'][:12]} reserved={k['reserved_microdollars']} (drain first)")

    res.ready = not res.reasons
    if not res.ready or not apply:
        return res

    cts = store._spanner.COMMIT_TIMESTAMP

    def _txn(transaction: Any) -> dict | None:
        # Returning None from a run_in_transaction callback COMMITS the buffered
        # mutations (only a RAISE aborts), so we must issue ZERO mutations until
        # EVERY predicate has passed — read + re-check all rows first, then write
        # all of them. Otherwise a hold on a later key would partial-seed the
        # earlier ones.
        # Hard precondition: the workspace MUST be billing-paused (quiesced) so no
        # new work races the seed. Re-checked in-txn, not just trusted from assess.
        ws = store._read_entity_tx(transaction, "workspace", workspace_id, Workspace)
        if ws is None or not ws.billing_paused:
            return None
        # Defense-in-depth: re-check no typed history INSIDE the txn — a ws that
        # got a typed reservation concurrently is already typed; never JSON-seed it.
        hist = list(transaction.execute_sql(
            "SELECT COUNT(*) FROM tr_reservation WHERE workspace_id=@ws",
            params={"ws": workspace_id}, param_types={"ws": pt.STRING},
        ))
        if hist and int(hist[0][0]) != 0:
            return None
        c = store._read_entity_tx(transaction, "credit", workspace_id, dict)
        if c is None or int(c.get("reserved_microdollars", 0)) != 0:
            return None  # account vanished or a legacy hold appeared mid-seed — no writes issued
        fresh_keys = []
        for k in keys:
            kb = store._read_entity_tx(transaction, "api_key", k["hash"], dict)
            if kb is None:
                continue
            if int(kb.get("reserved_microdollars", 0)) != 0:
                return None  # a key hold appeared mid-seed — no writes issued yet
            fresh_keys.append(kb)
        # All predicates hold — now issue every upsert (no partial seed possible).
        for kb in fresh_keys:
            limit = kb.get("limit_microdollars")
            day = kb.get("limit_daily_microdollars")
            week = kb.get("limit_weekly_microdollars")
            month = kb.get("limit_monthly_microdollars")
            transaction.insert_or_update(
                table="tr_key_limit",
                columns=_KEY_SEED_COLUMNS,
                values=[(
                    kb["hash"], 0,
                    None if limit is None else int(limit),
                    int(kb.get("usage_microdollars", 0)),
                    int(kb.get("byok_usage_microdollars", 0)),
                    0,
                    bool(kb.get("include_byok_in_limit", True)),
                    None if day is None else int(day),
                    None if week is None else int(week),
                    None if month is None else int(month),
                    cts, cts,
                )],
            )
        transaction.insert_or_update(
            table="tr_credit_balance",
            columns=_CREDIT_SEED_COLUMNS,
            values=[(
                workspace_id, 0,
                int(c.get("total_credits_microdollars", 0)),
                int(c.get("total_usage_microdollars", 0)),
                0,
                cts, cts,
            )],
        )
        return {
            "total_credits": int(c.get("total_credits_microdollars", 0)),
            "total_usage": int(c.get("total_usage_microdollars", 0)),
            "reserved": 0,
            "keys": len(fresh_keys),
        }

    seeded = store._run_in_transaction(_txn)
    if seeded is None:
        res.ready = False
        res.reasons.append("aborted: a hold appeared during seed (re-drain and retry)")
        return res
    res.applied = True
    res.keys_seeded = seeded.pop("keys")
    res.credit_seeded = seeded
    return res


# ── Standing typed-side invariant auditor ───────────────────────────────────
# This auditor is the standing typed-side tripwire: for every typed counter row,
# `reserved` MUST equal the sum of that scope's OPEN typed-origin holds
# (tr_reservation, settled=false), and MUST be >= 0. A violation means a hold
# leaked or a release double-applied. Run it on a schedule + before each ramp
# batch; wire an alert on the "release row-count != 1" log line as the live
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


@dataclass
class InvariantReport:
    credit_rows: int = 0
    key_rows: int = 0
    credit_violations: int = 0  # reserved != open-hold sum, or reserved < 0
    key_violations: int = 0
    samples: dict[str, dict] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return self.credit_violations == 0 and self.key_violations == 0

    def summary(self) -> str:
        return (
            f"credit: {self.credit_violations}/{self.credit_rows} | "
            f"key: {self.key_violations}/{self.key_rows} | "
            f"{'CLEAN' if self.clean else 'VIOLATIONS'}"
        )


def audit_typed_invariants(store: Any, *, max_samples: int = 20) -> InvariantReport:
    """Assert, in one consistent snapshot, that every typed `reserved` equals the
    sum of that (scope, shard)'s OPEN typed-origin holds and is non-negative.
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
    with store._database.snapshot(multi_use=True) as snap:  # 3 reads below — must be multi-use
        cb = list(snap.execute_sql(
            credit_row_sql, params={"pk": workspace_id}, param_types={"pk": pt.STRING},
        ))
        open_credit = list(snap.execute_sql(
            open_credit_sql, params={"ws": workspace_id}, param_types={"ws": pt.STRING},
        ))[0][0]
        nonzero_shard = list(snap.execute_sql(
            nonzero_shard_sql, params={"ws": workspace_id}, param_types={"ws": pt.STRING},
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
        res.reasons.append("aborted: not paused / nonzero shard / a typed row was missing (key deleted?)")
        return res
    res.applied = True
    res.keys_repaired = result["keys"]
    return res
