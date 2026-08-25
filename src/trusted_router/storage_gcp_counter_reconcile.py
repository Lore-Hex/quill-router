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
# ── Typed `total_usage` ledger reconstruction ───────────────────────────────
# `total_usage` has exactly TWO writers on this store, and both leave a durable
# row, so the counter is reconstructible rather than merely trusted:
#
#   1. the typed settle (storage_gcp_counter_dml.release_credit) adds `actual`,
#      and only for Credits traffic -- storage_gcp_authorize passes
#      `book_actual if settled_usage_type == "Credits" else 0`, so a non-Credits
#      settle bumps nothing. Its row is the settled tr_reservation.
#   2. deferred federated settlement (storage_gcp_federated_settlement) adds a
#      peer plane's debt straight to `total_usage`, with NO reservation in this
#      plane -- the authorize happened elsewhere. Its row is the insert-once
#      federated_settlement_claim.
#
# Summing only (1) -- which is what the retired PR #89 did -- reads every
# federated microdollar as drift. Both arms are required.
#: Shard-0 only, unlike the fleet-wide form above, because its only caller --
#: repair_typed_usage -- refuses a sharded workspace outright. Keeping the
#: filter also makes it fail SAFE if one ever slipped through: booked would come
#: out low, the computed target would fall below the current counter, and the
#: monotonic guard would refuse the write rather than lower it.
_SETTLED_CREDIT_ACTUALS = (
    "SELECT COALESCE(SUM(actual_micro), 0) FROM tr_reservation "
    "WHERE workspace_id=@ws AND ws_shard=0 AND settled=true "
    "AND settled_usage_type='Credits'"
)
_FEDERATED_SETTLEMENT_APPLIED = (
    "/* federated_settlement_applied */ "
    "SELECT COALESCE(SUM(CAST(JSON_VALUE(body, '$.cost_microdollars') AS INT64)), 0) "
    "FROM tr_entities WHERE kind='federated_settlement_claim' "
    "AND JSON_VALUE(body, '$.workspace_id')=@ws"
)
#: The pre-flip baseline typed `total_usage` was seeded from. Rows created after
#: the flip omit `total_usage` at insert (storage_gcp_counters.CREDIT_BALANCE_COLUMNS)
#: so they start at the Spanner default 0 and need no baseline at all.
#:
#: This field is REMOVED by the reviewed cleanup migration
#: (storage_gcp_credit_json_cleanup.LEGACY_CREDIT_MONEY_FIELDS) and its value is
#: not archived anywhere. So absent means one of two things we cannot tell apart:
#: a post-flip workspace whose baseline is genuinely 0, or a pre-flip workspace
#: whose baseline was deleted. The audit reports those as UNAUDITABLE rather than
#: guessing 0, because guessing 0 would report every pre-flip workspace's entire
#: historical spend as drift.
_JSON_USAGE_BASELINE = (
    "/* json_usage_baseline */ "
    "SELECT id, JSON_VALUE(body, '$.total_usage_microdollars') "
    "FROM tr_entities WHERE kind='credit'"
)
_TYPED_USAGE_ROWS = "SELECT workspace_id, shard, total_usage FROM tr_credit_balance"
#: Entity kind holding a RECORDED pre-ledger baseline, for the workspaces whose
#: JSON one was deleted before anyone needed it. Its own kind rather than a key
#: back in the credit body, because the credit body is precisely what
#: storage_gcp_credit_json_cleanup empties -- putting it back there would set up
#: the same loss again.
USAGE_BASELINE_KIND = "typed_usage_baseline"
_RECORDED_USAGE_BASELINES = (
    "/* recorded_usage_baselines */ "
    "SELECT JSON_VALUE(body, '$.workspace_id'), "
    "JSON_VALUE(body, '$.baseline_microdollars') "
    "FROM tr_entities WHERE kind='typed_usage_baseline'"
)
#: Spelled literally above so the query is a constant rather than an
#: interpolation; this keeps the two from drifting apart silently.
assert f"kind='{USAGE_BASELINE_KIND}'" in _RECORDED_USAGE_BASELINES
#: Fleet-wide forms of the two ledger arms, grouped so the audit stays ONE
#: snapshot rather than two queries per workspace.
#: NOT filtered to ws_shard=0, unlike the repair path. `typed_usage` sums
#: total_usage over every shard of a workspace, so the ledger has to as well.
#: Filtering one side and not the other charged sharded workspaces for their own
#: non-zero-shard settles: production has 638,484 settled reservations off
#: shard 0 across 30 workspaces, and the mismatch reported 42 workspaces as
#: needing a recorded baseline when 13 do. Recording those would have written
#: fictitious history for 29 of them.
_SETTLED_CREDIT_ACTUALS_BY_WS = (
    "SELECT workspace_id, COALESCE(SUM(actual_micro), 0) FROM tr_reservation "
    "WHERE settled=true AND settled_usage_type='Credits' "
    "GROUP BY workspace_id"
)
_FEDERATED_SETTLEMENT_APPLIED_BY_WS = (
    "/* federated_settlement_applied_by_ws */ "
    "SELECT JSON_VALUE(body, '$.workspace_id'), "
    "COALESCE(SUM(CAST(JSON_VALUE(body, '$.cost_microdollars') AS INT64)), 0) "
    "FROM tr_entities WHERE kind='federated_settlement_claim' "
    "GROUP BY JSON_VALUE(body, '$.workspace_id')"
)


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
    usage_rows: int = 0
    usage_violations: int = 0  # total_usage != baseline + settled actuals + federated
    #: Rows whose baseline is unrecoverable, so usage can be neither confirmed nor
    #: faulted. Counted apart from violations: calling them clean would overstate
    #: coverage, and calling them violations would cry wolf on every workspace the
    #: JSON cleanup has already run against.
    usage_unauditable: int = 0
    #: Counter below its ledger. Reported, NOT faulted, and deliberately so.
    #: A settle credits the balance and marks its reservation in separate
    #: transactions, so a continuously-loaded workspace is ALWAYS a little
    #: behind -- production reads it at -3780, -1537, -856, -173 on consecutive
    #: snapshots of one monitoring workspace while another sits at exactly -9
    #: in every one. Two reads mostly separate those, but not reliably: two
    #: snapshots can coincide. Failing the nightly audit on a fluctuating
    #: fraction of a cent is the cry-wolf shape this repo has already paid for
    #: once, so the number is surfaced and a human decides.
    usage_behind_ledger: int = 0
    samples: dict[str, dict] = field(default_factory=dict)
    #: Kept OUT of `samples` because callers label everything in there as a
    #: violation -- scripts/audit_typed_counters.py passes a single
    #: `sample_label` for the whole dict. Sharing it printed 643 lines reading
    #: "VIOLATION usage-unauditable:..." underneath a summary that said CLEAN,
    #: which is a worse read than either fact alone.
    unauditable: dict[str, dict] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        """No violated invariant. Says nothing about `usage_unauditable` -- see
        `fully_audited` for whether the sweep actually covered everything."""
        return (
            self.credit_violations == 0
            and self.key_violations == 0
            and self.regional_lease_violations == 0
            and self.usage_violations == 0
        )

    @property
    def behind_ledger_clean(self) -> bool:
        """No counter sits below its ledger. Separate from `clean` because a
        busy workspace is transiently behind by construction."""
        return self.usage_behind_ledger == 0

    @property
    def fully_audited(self) -> bool:
        """Every row was checkable. CLEAN with unauditable rows is a narrower
        claim than it looks, and an operator reading only `clean` would not see
        the difference."""
        return self.usage_unauditable == 0

    def summary(self) -> str:
        usage = f"usage: {self.usage_violations}/{self.usage_rows}"
        if self.usage_unauditable:
            usage += f" ({self.usage_unauditable} unauditable)"
        if self.usage_behind_ledger:
            usage += f" ({self.usage_behind_ledger} behind ledger)"
        return (
            f"credit: {self.credit_violations}/{self.credit_rows} | "
            f"key: {self.key_violations}/{self.key_rows} | "
            f"regional lease: {self.regional_lease_violations}/{self.regional_lease_rows} | "
            f"{usage} | "
            f"{'CLEAN' if self.clean else 'VIOLATIONS'}"
            f"{'' if self.fully_audited else ' (PARTIAL)'}"
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
        # Usage arm, read in the SAME snapshot: a settle that lands between two
        # reads would otherwise show as drift equal to its own actual.
        # Summed ACROSS shards, unlike the reserved arm. `reserved` is compared
        # per (scope, shard) because each shard holds its own reservations, but
        # both usage ledgers are per-WORKSPACE -- a settled reservation and a
        # federated claim name a workspace, not a shard. Comparing shard 0 alone
        # against a whole-workspace ledger would report every sharded workspace's
        # other shards as missing usage.
        typed_usage: dict[str, int] = {}
        for row in snap.execute_sql(_TYPED_USAGE_ROWS):
            workspace = str(row[0])
            typed_usage[workspace] = typed_usage.get(workspace, 0) + int(row[2] or 0)
        settled_actuals = {
            str(r[0]): int(r[1] or 0)
            for r in snap.execute_sql(_SETTLED_CREDIT_ACTUALS_BY_WS)
        }
        federated_applied = {
            str(r[0]): int(r[1] or 0)
            for r in snap.execute_sql(_FEDERATED_SETTLEMENT_APPLIED_BY_WS)
            if r[0] is not None
        }
        usage_baselines = {
            str(r[0]): (None if r[1] is None else int(r[1]))
            for r in snap.execute_sql(_JSON_USAGE_BASELINE)
        }
        recorded_baselines = {
            str(r[0]): int(r[1])
            for r in snap.execute_sql(_RECORDED_USAGE_BASELINES)
            if r[0] is not None and r[1] is not None
        }

    regional_holds, regional_errors = _regional_quota_escrow(regional_rows)
    for scope, held in regional_holds.items():
        credit_holds[scope] = credit_holds.get(scope, 0) + held

    def _sample(key: str, value: dict) -> None:
        if len(report.samples) < max_samples:
            report.samples[key] = value

    def _unauditable(key: str, value: dict) -> None:
        if len(report.unauditable) < max_samples:
            report.unauditable[key] = value

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
    # ── usage: total_usage must equal everything the ledger says was booked ──
    # Both directions again, in the same spirit as _check: a federated claim or a
    # settled actual for a workspace with NO typed row is a booking that landed
    # nowhere, and iterating typed rows alone cannot see it.
    behind_candidates: dict[str, tuple[int, int]] = {}
    for workspace_id, actual_usage in typed_usage.items():
        report.usage_rows += 1
        booked = settled_actuals.get(workspace_id, 0) + federated_applied.get(workspace_id, 0)
        baseline = recorded_baselines.get(workspace_id)
        if baseline is None:
            baseline = usage_baselines.get(workspace_id)
        if baseline is None:
            # No baseline anywhere. That is not automatically unknowable: a
            # baseline is usage booked BEFORE the ledger began, so it can never
            # be negative, and the two decidable cases follow from that alone.
            if actual_usage < booked:
                # Candidate only. A settle marks its reservation and credits the
                # balance in SEPARATE transactions, so at any instant a busy
                # workspace has settles counted in the ledger and not yet in the
                # counter. Measured on production: one workspace read -3780 and
                # then -1537 microdollars seconds apart under load, while
                # another sat at exactly -9 across both reads. The transient
                # kind resolves in milliseconds; the real kind does not, so
                # these are confirmed against a second snapshot below rather
                # than reported now.
                behind_candidates[workspace_id] = (actual_usage, booked)
                continue
            if actual_usage == booked:
                # Baseline is zero, and reconciled rather than assumed: the
                # ledger accounts for the counter exactly. Fleet-wide this is
                # 604 of 643 workspaces, which used to report as uncheckable.
                continue
            # Counter exceeds the ledger by an amount only pre-ledger history
            # explains. Genuinely uncheckable until that number is recorded --
            # and the remainder below IS the number to record.
            report.usage_unauditable += 1
            _unauditable(
                f"usage-unauditable:{workspace_id}",
                {
                    "typed_total_usage": actual_usage,
                    "ledger_booked": booked,
                    "unexplained": actual_usage - booked,
                    "why": "usage predates the ledger and no baseline is "
                    "recorded. `unexplained` is the value to record as "
                    f"kind={USAGE_BASELINE_KIND!r} to make this workspace "
                    "auditable.",
                },
            )
            continue
        expected = baseline + booked
        if actual_usage != expected or actual_usage < 0:
            report.usage_violations += 1
            _sample(
                f"usage:{workspace_id}",
                {
                    "typed_total_usage": actual_usage,
                    "expected": expected,
                    "baseline": baseline,
                    "settled_actuals": settled_actuals.get(workspace_id, 0),
                    "federated_applied": federated_applied.get(workspace_id, 0),
                    "delta": actual_usage - expected,
                },
            )
    for workspace_id in set(settled_actuals) | set(federated_applied):
        if workspace_id in typed_usage:
            continue
        booked = settled_actuals.get(workspace_id, 0) + federated_applied.get(workspace_id, 0)
        if booked > 0:
            report.usage_violations += 1
            _sample(
                f"usage-orphan-booking:{workspace_id}",
                {
                    "typed_total_usage": None,
                    "settled_actuals": settled_actuals.get(workspace_id, 0),
                    "federated_applied": federated_applied.get(workspace_id, 0),
                },
            )

    if behind_candidates:
        for workspace_id, still_behind in _confirm_behind_ledger(
            store, behind_candidates
        ).items():
            if not still_behind:
                continue
            actual_usage, booked = still_behind
            report.usage_behind_ledger += 1
            _unauditable(
                f"usage-behind-ledger:{workspace_id}",
                {
                    "typed_total_usage": actual_usage,
                    "ledger_booked": booked,
                    "shortfall": booked - actual_usage,
                    "why": "counter is BELOW booked spend by the SAME amount in "
                    "two consecutive snapshots. An in-flight settle moves; this "
                    "did not. No baseline can be negative, so this is drift "
                    "rather than missing history",
                },
            )

    report.regional_lease_rows = len(regional_rows)
    report.regional_lease_violations = len(regional_errors)
    for index_id, error in regional_errors.items():
        _sample(f"regional-lease:{index_id}", {"error": error})
    return report


def _confirm_behind_ledger(
    store: Any, candidates: dict[str, tuple[int, int]]
) -> dict[str, tuple[int, int] | None]:
    """Re-read the candidates once. ``None`` means the shortfall MOVED.

    The discriminator is stability, not disappearance. A continuously-loaded
    workspace always has settles between their two transactions, so its
    shortfall never reaches zero -- it just changes. Production shows both
    shapes plainly: one workspace read -3780, then -1537, then -856 across
    consecutive snapshots while another sat at exactly -9 in every one. A gap
    that holds the SAME value while the fleet keeps settling is not in flight.
    """
    if not candidates:
        return {}
    with store._database.snapshot(multi_use=True) as snap:
        typed: dict[str, int] = {}
        for row in snap.execute_sql(_TYPED_USAGE_ROWS):
            key = str(row[0])
            if key in candidates:
                typed[key] = typed.get(key, 0) + int(row[2] or 0)
        settled = {
            str(r[0]): int(r[1] or 0)
            for r in snap.execute_sql(_SETTLED_CREDIT_ACTUALS_BY_WS)
            if str(r[0]) in candidates
        }
        federated = {
            str(r[0]): int(r[1] or 0)
            for r in snap.execute_sql(_FEDERATED_SETTLEMENT_APPLIED_BY_WS)
            if r[0] is not None and str(r[0]) in candidates
        }
    confirmed: dict[str, tuple[int, int] | None] = {}
    for workspace_id, (first_usage, first_booked) in candidates.items():
        usage = typed.get(workspace_id, 0)
        booked = settled.get(workspace_id, 0) + federated.get(workspace_id, 0)
        if usage >= booked:
            confirmed[workspace_id] = None
            continue
        if booked - usage != first_booked - first_usage:
            # Moved between snapshots: settles in flight, not drift.
            confirmed[workspace_id] = None
            continue
        confirmed[workspace_id] = (usage, booked)
    return confirmed


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


# ── Repair: typed `total_usage` ─────────────────────────────────────────────
# Companion to repair_typed_reserved, and deliberately stricter. `reserved` is
# derived from state that is still open, so recomputing it is safe whenever the
# workspace is quiesced. `total_usage` is a MONOTONIC lifetime total: writing it
# rewrites history, and writing it DOWN destroys the record of spend that was
# really booked. So this refuses to lower it unless the caller says so in as
# many words, and refuses entirely unless every hold is drained -- an open hold
# is a settle that has not yet added its actual, which would read as an
# over-count and "repair" the counter backwards.


@dataclass
class UsageRepairResult:
    workspace_id: str
    ready: bool
    reasons: list[str] = field(default_factory=list)
    applied: bool = False
    total_usage_before: int | None = None
    total_usage_after: int | None = None
    baseline: int | None = None
    settled_actuals: int = 0
    federated_applied: int = 0

    @property
    def delta(self) -> int | None:
        if self.total_usage_before is None or self.total_usage_after is None:
            return None
        return self.total_usage_after - self.total_usage_before


def repair_typed_usage(
    store: Any,
    workspace_id: str,
    *,
    apply: bool = False,
    allow_decrease: bool = False,
) -> UsageRepairResult:
    """Set typed `total_usage` = JSON baseline + settled Credits actuals +
    federated settlement claims, for a fully drained PAUSED workspace.

    Read-only when apply=False (reports before/after). Fail-closed: refuses
    unless billing_paused, unsharded, fully drained, and the JSON baseline still
    exists. Refuses to LOWER the counter unless allow_decrease=True."""
    pt = store._param_types
    res = UsageRepairResult(workspace_id=workspace_id, ready=False)
    usage_row_sql = (
        "SELECT total_usage FROM tr_credit_balance WHERE workspace_id=@pk AND shard=0"
    )
    open_holds_sql = (
        "SELECT COUNT(*) FROM tr_reservation WHERE workspace_id=@ws AND settled=false"
    )
    baseline_sql = (
        "/* json_usage_baseline_one */ "
        "SELECT JSON_VALUE(body, '$.total_usage_microdollars') FROM tr_entities "
        "WHERE kind='credit' AND id=@ws"
    )

    workspace = store.get_workspace(workspace_id)
    credit_account = store.get_credit_account(workspace_id)
    ws_param = {"ws": workspace_id}
    ws_type = {"ws": pt.STRING}
    with store._database.snapshot(multi_use=True) as snap:
        usage_row = list(snap.execute_sql(
            usage_row_sql, params={"pk": workspace_id}, param_types={"pk": pt.STRING},
        ))
        open_holds = list(snap.execute_sql(
            open_holds_sql, params=ws_param, param_types=ws_type,
        ))[0][0]
        baseline_row = list(snap.execute_sql(
            baseline_sql, params=ws_param, param_types=ws_type,
        ))
        recorded_row = [
            r for r in snap.execute_sql(_RECORDED_USAGE_BASELINES) if str(r[0]) == workspace_id
        ]
        settled = list(snap.execute_sql(
            _SETTLED_CREDIT_ACTUALS, params=ws_param, param_types=ws_type,
        ))[0][0]
        federated = list(snap.execute_sql(
            _FEDERATED_SETTLEMENT_APPLIED, params=ws_param, param_types=ws_type,
        ))[0][0]

    baseline = None
    if recorded_row and recorded_row[0][1] is not None:
        baseline = int(recorded_row[0][1])
    elif baseline_row and baseline_row[0][0] is not None:
        baseline = int(baseline_row[0][0])

    if workspace is None or not getattr(workspace, "billing_paused", False):
        res.reasons.append("workspace not billing-paused — pause it before repair")
    if credit_account is not None and credit_shard_count(credit_account) != 1:
        res.reasons.append("credit ledger is sharded — consolidate before shard-zero repair")
    if not usage_row:
        res.reasons.append("no typed credit row")
    if int(open_holds) != 0:
        res.reasons.append(
            f"{open_holds} holds still open — drain them first, or their actuals "
            "land after this write and the counter is wrong again"
        )
    booked_total = int(settled) + int(federated)
    current_usage = int(usage_row[0][0]) if usage_row else None
    if baseline is None and current_usage is not None and current_usage <= booked_total:
        # Reconciled to zero rather than assumed: a baseline is pre-ledger
        # usage, so it cannot be negative, and a counter at or below booked
        # spend leaves nothing for one to explain. Repairing to `booked` can
        # only raise the counter, which is the direction it is allowed to move.
        baseline = 0
    if baseline is None:
        res.reasons.append(
            f"usage exceeds the ledger by {(current_usage or 0) - booked_total} "
            "and no baseline is recorded. That remainder is history from before "
            "the ledger; the JSON copy was removed by the credit-JSON cleanup "
            "without being archived. Record it as "
            f"kind={USAGE_BASELINE_KIND!r} first, then repair"
        )

    res.baseline = baseline
    res.settled_actuals = int(settled)
    res.federated_applied = int(federated)
    if usage_row:
        res.total_usage_before = int(usage_row[0][0])
    if baseline is not None:
        res.total_usage_after = baseline + int(settled) + int(federated)

    if (
        res.total_usage_before is not None
        and res.total_usage_after is not None
        and res.total_usage_after < res.total_usage_before
        and not allow_decrease
    ):
        res.reasons.append(
            f"refusing to lower total_usage "
            f"{res.total_usage_before} -> {res.total_usage_after}: it is monotonic, "
            "so a decrease means the ledger is missing rows rather than the "
            "counter being wrong. Investigate first; pass allow_decrease=True "
            "only once you know why"
        )

    res.ready = not res.reasons
    if not res.ready or not apply:
        return res

    cts = store._spanner.COMMIT_TIMESTAMP
    target = res.total_usage_after

    def _txn(transaction: Any) -> dict | None:
        # Re-read and re-validate the whole plan inside the txn, same discipline
        # as repair_typed_reserved: a workspace unpaused, a hold opened, or the
        # baseline cleaned up between the snapshot and here must abort.
        ws = store._read_entity_tx(transaction, "workspace", workspace_id, Workspace)
        if ws is None or not ws.billing_paused:
            return None
        credit = store._read_entity_tx(transaction, "credit", workspace_id, CreditAccount)
        if credit is not None and credit_shard_count(credit) != 1:
            return None
        if int(list(transaction.execute_sql(
            open_holds_sql, params=ws_param, param_types=ws_type,
        ))[0][0]) != 0:
            return None
        current = list(transaction.execute_sql(
            usage_row_sql, params={"pk": workspace_id}, param_types={"pk": pt.STRING},
        ))
        if not current:
            return None  # typed row vanished — never re-create it as a partial row
        fresh_booked = int(list(transaction.execute_sql(
            _SETTLED_CREDIT_ACTUALS, params=ws_param, param_types=ws_type,
        ))[0][0]) + int(list(transaction.execute_sql(
            _FEDERATED_SETTLEMENT_APPLIED, params=ws_param, param_types=ws_type,
        ))[0][0])
        # Same three-way resolution as the plan above, re-derived in the txn so
        # a baseline recorded or a credit body cleaned between plan and write
        # aborts rather than writing a total computed from the other one.
        fresh_recorded = [
            r
            for r in transaction.execute_sql(_RECORDED_USAGE_BASELINES)
            if str(r[0]) == workspace_id and r[1] is not None
        ]
        fresh_json = list(transaction.execute_sql(
            baseline_sql, params=ws_param, param_types=ws_type,
        ))
        if fresh_recorded:
            fresh_base = int(fresh_recorded[0][1])
        elif fresh_json and fresh_json[0][0] is not None:
            fresh_base = int(fresh_json[0][0])
        elif int(current[0][0]) <= fresh_booked:
            fresh_base = 0
        else:
            return None
        recomputed = fresh_base + fresh_booked
        if recomputed != target:
            return None  # the ledger moved under us — abort rather than write a stale total
        if recomputed < int(current[0][0]) and not allow_decrease:
            return None
        transaction.insert_or_update(
            table="tr_credit_balance",
            columns=("workspace_id", "shard", "total_usage", "updated_at"),
            values=[(workspace_id, 0, recomputed, cts)],
        )
        return {"total_usage": recomputed}

    result = store._run_in_transaction(_txn)
    if result is None:
        res.ready = False
        res.reasons.append(
            "aborted: not paused / sharded / a hold opened / typed row missing / "
            "baseline gone / the ledger moved between plan and write"
        )
        return res
    res.applied = True
    res.total_usage_after = result["total_usage"]
    return res


# ── Recording a pre-ledger usage baseline ───────────────────────────────────
# For the workspaces whose JSON baseline was deleted before anything needed it.
# The value is not invented: it is the remainder the ledger cannot account for,
# which is by definition the usage booked before the ledger began. Recording it
# makes the workspace auditable from then on; it changes no counter and moves no
# money.


@dataclass
class UsageBaselineProposal:
    workspace_id: str
    typed_total_usage: int
    ledger_booked: int
    baseline_microdollars: int
    already_recorded: int | None = None

    @property
    def needed(self) -> bool:
        return self.already_recorded is None and self.baseline_microdollars > 0


def propose_usage_baselines(store: Any) -> list[UsageBaselineProposal]:
    """Every workspace whose counter exceeds its ledger, with the value to record.

    Read-only. A workspace whose ledger already explains its counter is absent:
    its baseline is zero and needs no row.
    """
    report_rows: list[UsageBaselineProposal] = []
    with store._database.snapshot(multi_use=True) as snap:
        typed: dict[str, int] = {}
        for row in snap.execute_sql(_TYPED_USAGE_ROWS):
            key = str(row[0])
            typed[key] = typed.get(key, 0) + int(row[2] or 0)
        settled = {
            str(r[0]): int(r[1] or 0)
            for r in snap.execute_sql(_SETTLED_CREDIT_ACTUALS_BY_WS)
        }
        federated = {
            str(r[0]): int(r[1] or 0)
            for r in snap.execute_sql(_FEDERATED_SETTLEMENT_APPLIED_BY_WS)
            if r[0] is not None
        }
        recorded = {
            str(r[0]): int(r[1])
            for r in snap.execute_sql(_RECORDED_USAGE_BASELINES)
            if r[0] is not None and r[1] is not None
        }
    for workspace_id, usage in sorted(typed.items()):
        booked = settled.get(workspace_id, 0) + federated.get(workspace_id, 0)
        if usage <= booked:
            continue
        report_rows.append(
            UsageBaselineProposal(
                workspace_id=workspace_id,
                typed_total_usage=usage,
                ledger_booked=booked,
                baseline_microdollars=usage - booked,
                already_recorded=recorded.get(workspace_id),
            )
        )
    return report_rows


def record_usage_baseline(
    store: Any,
    proposal: UsageBaselineProposal,
    *,
    recorded_at: str,
    apply: bool = False,
) -> bool:
    """Write ONE baseline row. Read-only when apply=False.

    Refuses to overwrite an existing row: a baseline is a statement about
    history, so a second, different value for the same workspace means one of
    them is wrong and a human should decide which.
    """
    if proposal.already_recorded is not None:
        return False
    if not apply:
        return proposal.needed
    if not proposal.needed:
        return False

    def _txn(transaction: Any) -> bool:
        existing = [
            r
            for r in transaction.execute_sql(_RECORDED_USAGE_BASELINES)
            if str(r[0]) == proposal.workspace_id
        ]
        if existing:
            return False
        store._write_entity_tx(
            transaction,
            USAGE_BASELINE_KIND,
            proposal.workspace_id,
            {
                "workspace_id": proposal.workspace_id,
                "baseline_microdollars": proposal.baseline_microdollars,
                "recorded_at": recorded_at,
                "reason": (
                    "usage booked before the typed ledger; the JSON copy was "
                    "removed by the credit-JSON cleanup without being archived"
                ),
                "typed_total_usage_at_record": proposal.typed_total_usage,
                "ledger_booked_at_record": proposal.ledger_booked,
            },
        )
        return True

    return bool(store._run_in_transaction(_txn))
