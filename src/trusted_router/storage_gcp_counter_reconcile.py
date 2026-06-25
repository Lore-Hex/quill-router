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

_CREDIT_TYPED_SCAN = (
    "SELECT workspace_id, total_credits, total_usage, reserved FROM tr_credit_balance"
)
_KEY_TYPED_SCAN = (
    "SELECT key_hash, limit_micro, usage, byok_usage, reserved, include_byok "
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
    "include_byok", "source_updated_at", "updated_at",
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
