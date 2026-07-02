"""Step 2 of the billing typed-column migration: backfill + drift comparator.

See docs/design/billing-typed-counters.md.

- ``compare`` is the INDEPENDENT full-row comparator (red-team P2): it scans both
  the authoritative JSON rows and the typed mirror and reports any row whose
  counters diverge (or whose typed mirror is missing). The per-write mirror is
  atomic so it cannot tear, but this comparator is the defense-in-depth that the
  Step 3 enforcement flip is gated on — flip only when it reports zero drift.

- ``backfill`` writes the typed mirror for pre-flag JSON rows (rows that existed
  before TR_TYPED_COUNTER_MIRROR was turned on). Each row is mirrored inside a
  transaction that re-reads the authoritative JSON row, so it writes a
  json-consistent typed row atomically and cannot clobber a concurrent
  dual-write with a stale read. Idempotent and re-runnable to convergence.

Both operate on a live SpannerBigtableStore (``store``), using its existing JSON
scan plus a direct typed-table scan.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from trusted_router.storage_gcp_counters import (
    credit_drift,
    key_drift,
    mirror_write,
)
from trusted_router.storage_models import ApiKey, CreditAccount, Workspace

_CREDIT_TYPED_SCAN = (
    "SELECT workspace_id, total_credits, total_usage, reserved FROM tr_credit_balance"
)
_KEY_TYPED_SCAN = (
    "SELECT key_hash, limit_micro, usage, byok_usage, reserved, include_byok, "
    "day_limit_micro, week_limit_micro, month_limit_micro "
    "FROM tr_key_limit"
)


@dataclass
class DriftReport:
    credit_rows: int = 0
    key_rows: int = 0
    credit_drift: int = 0
    key_drift: int = 0
    credit_orphans: int = 0  # typed rows with no JSON authority
    key_orphans: int = 0
    # up to a few examples for triage: {id: {col: (json, typed)}}
    samples: dict[str, dict] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return (
            self.credit_drift == 0
            and self.key_drift == 0
            and self.credit_orphans == 0
            and self.key_orphans == 0
        )

    def summary(self) -> str:
        return (
            f"credit: {self.credit_drift}/{self.credit_rows} drift, "
            f"{self.credit_orphans} orphan | "
            f"key: {self.key_drift}/{self.key_rows} drift, {self.key_orphans} orphan | "
            f"{'CLEAN' if self.clean else 'DRIFT'}"
        )


def _scan_json(snapshot: Any, kind: str, id_field: str, pt: Any) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in snapshot.execute_sql(
        "SELECT body FROM tr_entities WHERE kind=@kind",
        params={"kind": kind},
        param_types={"kind": pt.STRING},
    ):
        body = json.loads(row[0])
        out[body[id_field]] = body
    return out


def compare(store: Any, *, max_samples: int = 20) -> DriftReport:
    """Scan JSON vs typed in ONE consistent snapshot and report drift + orphans.

    Read-only. A single multi-use snapshot reads JSON and typed at the same
    timestamp, so a live dual-write in flight cannot produce a transient false
    positive (codex Step-2 #3). Reports value drift, missing mirrors, AND orphan
    typed rows that have no JSON authority (codex Step-2 #1).
    """
    report = DriftReport()
    pt = store._param_types

    with store._database.snapshot(multi_use=True) as snapshot:
        json_credit = _scan_json(snapshot, "credit", "workspace_id", pt)
        json_key = _scan_json(snapshot, "api_key", "hash", pt)
        typed_credit = {
            r[0]: {"total_credits": r[1], "total_usage": r[2], "reserved": r[3]}
            for r in snapshot.execute_sql(_CREDIT_TYPED_SCAN)
        }
        typed_key = {
            r[0]: {
                "limit_micro": r[1], "usage": r[2], "byok_usage": r[3],
                "reserved": r[4], "include_byok": r[5],
                "day_limit_micro": r[6], "week_limit_micro": r[7],
                "month_limit_micro": r[8],
            }
            for r in snapshot.execute_sql(_KEY_TYPED_SCAN)
        }

    def _sample(key: str, value: dict) -> None:
        if len(report.samples) < max_samples:
            report.samples[key] = value

    report.credit_rows = len(json_credit)
    for ws_id, body in json_credit.items():
        drift = credit_drift(body, typed_credit.get(ws_id))
        if drift:
            report.credit_drift += 1
            _sample(f"credit:{ws_id}", drift)
    for ws_id in typed_credit.keys() - json_credit.keys():
        report.credit_orphans += 1
        _sample(f"credit-orphan:{ws_id}", {"orphan_typed_row": True})

    report.key_rows = len(json_key)
    for key_hash, body in json_key.items():
        drift = key_drift(body, typed_key.get(key_hash))
        if drift:
            report.key_drift += 1
            _sample(f"api_key:{key_hash}", drift)
    for key_hash in typed_key.keys() - json_key.keys():
        report.key_orphans += 1
        _sample(f"api_key-orphan:{key_hash}", {"orphan_typed_row": True})

    return report


def backfill(store: Any, *, dry_run: bool = False) -> dict[str, int]:
    """Mirror the JSON-owned columns of the typed row for every JSON credit/
    api_key row. Idempotent; safe to run repeatedly.

    OWNERSHIP SPLIT (2026-06-25 incident) — READ BEFORE USING AS A FLIP GATE:
    this delegates to ``mirror_write``, which now writes ONLY JSON-owned columns
    (total_credits; key limit_micro/include_byok). It therefore does NOT seed the
    typed-DML-owned reserved / total_usage / usage / byok_usage, and a clean
    ``compare`` (which now audits only those JSON-owned columns) does NOT mean a
    workspace is safe to flip to typed enforcement. Flipping requires a SEPARATE
    ledger-derived reconciliation that computes reserved from open holds (legacy +
    typed) and seeds total_usage, atomic with the gate flip — otherwise the typed
    reserve gate over-admits by the sum of open/historical holds (silent
    overspend). Do NOT "fix" this by re-adding reserved/usage to the mirror: that
    full-row copy WAS the clobber.
    """
    counts = {"credit": 0, "api_key": 0}
    spanner_module = store._spanner

    plan: list[tuple[str, str]] = []
    plan += [("credit", b["workspace_id"]) for b in store._list_entities("credit", cls=dict)]
    plan += [("api_key", b["hash"]) for b in store._list_entities("api_key", cls=dict)]

    for kind, entity_id in plan:
        if dry_run:
            counts[kind] += 1
            continue

        def _txn(transaction: Any, _kind: str = kind, _id: str = entity_id) -> bool:
            body = store._read_entity_tx(transaction, _kind, _id, dict)
            if body is None:
                return False
            mirror_write(
                transaction, _kind, _id, body, spanner_module.COMMIT_TIMESTAMP
            )
            return True

        if store._run_in_transaction(_txn):
            counts[kind] += 1

    return counts


# ── Step 6: ledger-derived flip reconciliation ──────────────────────────────
# Seeds the typed-DML-owned counters for a workspace at the moment it is flipped
# to typed enforcement. This is the FULL-ROW seed that backfill()/mirror_write no
# longer do after the 2026-06-25 ownership split — and the ONLY sanctioned writer
# of typed reserved/total_usage outside the typed authorize/finalize DML.
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
# compare() audits JSON-vs-typed and was correctly NARROWED to JSON-owned columns
# after the ownership split — so it can no longer see a typed `reserved` leak (the
# exact incident class). This auditor is the typed-side tripwire that replaces it:
# for every typed counter row, `reserved` MUST equal the sum of that scope's OPEN
# typed-origin holds (tr_reservation, settled=false), and MUST be >= 0. A drift
# means a hold leaked or a release double-applied. Run it on a schedule + before
# each ramp batch; wire an alert on the "release row-count != 1" log line as the
# live signal between audits.

# Shard-aware (the typed counter PK is (scope, shard); reservations carry the
# per-scope shard), COALESCE so an empty SUM reads 0.
_OPEN_CREDIT_HOLDS = (
    "SELECT workspace_id, ws_shard, COALESCE(SUM(credit_reserved_micro), 0) "
    "FROM tr_reservation WHERE settled = false GROUP BY workspace_id, ws_shard"
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
        credit_holds = {
            (r[0], r[1]): int(r[2] or 0) for r in snap.execute_sql(_OPEN_CREDIT_HOLDS)
        }
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


# ── Rollback: typed → JSON backsync ─────────────────────────────────────────
# The INVERSE of reconcile_for_flip. Denylisting a workspace back to legacy is NOT
# rollback-correct on its own: once typed usage exists, the JSON counters are
# stale-low, so the legacy path would over-admit. Before rolling back, copy the
# typed-DML-owned gross counters back into JSON so the legacy path is authoritative
# and correct. Fail-closed: the workspace must have NO open typed holds (pause +
# drain first); writing JSON re-fires the ownership-split mirror, which only writes
# total_credits/config, so it does NOT re-clobber the typed counters mid-backsync.


@dataclass
class BacksyncResult:
    workspace_id: str
    ready: bool
    reasons: list[str] = field(default_factory=list)
    applied: bool = False
    credit: dict | None = None
    keys: int = 0


def backsync_typed_to_json(store: Any, workspace_id: str, *, apply: bool = False) -> BacksyncResult:
    """Copy the typed gross counters back into JSON for a drained workspace so a
    rollback to legacy is correct. Fail-closed: refuses unless the workspace is
    billing-PAUSED and has NO open typed holds (settled=false). With apply=True
    it re-reads everything INSIDE one transaction (the paused gate, the open-hold
    count, the FRESH typed credit + key counters) — because typed usage can still
    advance via an in-flight settle between an outer snapshot and the txn — builds
    a COMPLETE plan (failing if any active JSON key has no typed row, rather than
    silently skipping it), and only then writes JSON credit total_usage/reserved +
    key usage/byok_usage/reserved. Read-only when apply=False."""
    pt = store._param_types
    res = BacksyncResult(workspace_id=workspace_id, ready=False)
    open_holds_sql = (
        "SELECT COUNT(*) FROM tr_reservation WHERE workspace_id=@ws AND settled = false"
    )
    credit_sql = "SELECT total_usage, reserved FROM tr_credit_balance WHERE workspace_id=@pk AND shard=0"
    key_sql = "SELECT usage, byok_usage, reserved FROM tr_key_limit WHERE key_hash=@pk AND shard=0"

    key_hashes = [
        b["hash"] for b in store._list_entities("api_key", cls=dict)
        if b.get("workspace_id") == workspace_id
    ]
    workspace = store.get_workspace(workspace_id)
    # multi_use: two reads on one snapshot (a single-use snapshot raises on the
    # second read on real Spanner — the fa9f5d4 class the fake now models).
    with store._database.snapshot(multi_use=True) as snap:
        open_holds = list(snap.execute_sql(
            open_holds_sql, params={"ws": workspace_id}, param_types={"ws": pt.STRING},
        ))[0][0]
        credit_rows = list(snap.execute_sql(
            credit_sql, params={"pk": workspace_id}, param_types={"pk": pt.STRING},
        ))

    if workspace is None or not getattr(workspace, "billing_paused", False):
        res.reasons.append("workspace not billing-paused — pause (quiesce) it before rollback")
    if int(open_holds) != 0:
        res.reasons.append(f"{open_holds} open typed holds — drain before rollback backsync")
    if not credit_rows:
        res.reasons.append("no typed credit row to backsync")
    res.ready = not res.reasons
    if credit_rows:
        res.credit = {"total_usage": int(credit_rows[0][0]), "reserved": int(credit_rows[0][1])}
    if not res.ready or not apply:
        return res

    def _txn(transaction: Any) -> dict | None:
        # Re-read ALL preconditions + values INSIDE the txn (no stale outer reads).
        # Issue ZERO mutations until the complete plan is validated.
        ws = store._read_entity_tx(transaction, "workspace", workspace_id, Workspace)
        if ws is None or not ws.billing_paused:
            return None
        oh = list(transaction.execute_sql(
            open_holds_sql, params={"ws": workspace_id}, param_types={"ws": pt.STRING},
        ))[0][0]
        if int(oh) != 0:
            return None
        tc = list(transaction.execute_sql(
            credit_sql, params={"pk": workspace_id}, param_types={"pk": pt.STRING},
        ))
        credit = store._read_entity_tx(transaction, "credit", workspace_id, CreditAccount)
        if not tc or credit is None:
            return None
        plan: list[tuple] = []
        for kh in key_hashes:
            key_obj = store._read_entity_tx(transaction, "api_key", kh, ApiKey)
            if key_obj is None:
                continue  # key deleted since assess (pause blocks creation, not deletion)
            kt = list(transaction.execute_sql(
                key_sql, params={"pk": kh}, param_types={"pk": pt.STRING},
            ))
            if not kt:
                return None  # active JSON key with NO typed row — unsafe to backsync, abort
            plan.append((key_obj, int(kt[0][0]), int(kt[0][1]), int(kt[0][2])))
        # All checks passed — write credit + every key (no mutation was issued above).
        typed_total_usage, typed_reserved = int(tc[0][0]), int(tc[0][1])
        credit.total_usage_microdollars = typed_total_usage
        credit.reserved_microdollars = typed_reserved
        store._write_entity_tx(transaction, "credit", workspace_id, credit)
        for key_obj, usage, byok, reserved in plan:
            key_obj.usage_microdollars = usage
            key_obj.byok_usage_microdollars = byok
            key_obj.reserved_microdollars = reserved
            store._write_entity_tx(transaction, "api_key", key_obj.hash, key_obj)
        return {"total_usage": typed_total_usage, "reserved": typed_reserved, "keys": len(plan)}

    result = store._run_in_transaction(_txn)
    if result is None:
        res.ready = False
        res.reasons.append(
            "aborted: not paused / a hold appeared / an active key lacked a typed row (re-drain and retry)"
        )
        return res
    res.applied = True
    res.keys = result["keys"]
    res.credit = {"total_usage": result["total_usage"], "reserved": result["reserved"]}
    return res


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
    key_hashes = [
        b["hash"] for b in store._list_entities("api_key", cls=dict)
        if b.get("workspace_id") == workspace_id
    ]
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
