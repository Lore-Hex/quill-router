"""Step 3b-3: the atomic gateway-authorize transaction (DML-only).

See docs/design/billing-typed-counters.md.

ONE Spanner read-write transaction (no mutation mixing) owns the whole authorize
decision, so a crash can never leak a hold (codex#1 #1):

  scoped idempotency read (+ fingerprint) ->
  conditional key-cap DML -> conditional credit DML ->
  tr_reservation INSERT (exact holds + hold usage type + authorization_id) ->
  gateway_authorization DML INSERT.

A rejection (insufficient credits / key cap) raises inside the callback, which
rolls the whole transaction back — releasing any hold already taken atomically,
no compensation needed. A duplicate idempotency_scope (concurrent first-call
loser) surfaces ALREADY_EXISTS (NOT retried); we re-read and REPLAY — no second
debit. Replay is resume/no-execute: the caller must NOT re-run the LLM call
(codex#2 #4).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any

from google.api_core.exceptions import AlreadyExists

from trusted_router.spend_windows import utcnow, window_floors
from trusted_router.storage_gcp_counter_dml import (
    KEY_ACCEPTED,
    KEY_INSUFFICIENT,
    KEY_MISSING,
    insert_entity_dml,
    insert_reservation,
    read_reservation_by_idempotency,
    reserve_credit,
    reserve_key,
)
from trusted_router.storage_gcp_counters import UNSHARDED
from trusted_router.storage_gcp_io import run_in_transaction_with_retry
from trusted_router.storage_gcp_settle_outbox import _GUARD_STATUS_SQL, GUARD_COUNT_SQL

log = logging.getLogger(__name__)


class AuthorizeOutcome:
    ACCEPTED = "accepted"
    REPLAY = "replay"  # idempotent replay: resume, do NOT re-execute
    INSUFFICIENT_CREDITS = "insufficient_credits"
    KEY_LIMIT_EXCEEDED = "key_limit_exceeded"
    KEY_MISSING = "key_missing"  # typed key row absent -> fail closed
    IDEMPOTENCY_MISMATCH = "idempotency_mismatch"  # same key, different request body
    KEY_WINDOW_LIMIT_EXCEEDED = "key_window_limit_exceeded"  # a daily/weekly/monthly cap


class _Reject(Exception):
    """Roll the authorize transaction back with a terminal outcome (not retried)."""

    def __init__(self, outcome: str) -> None:
        self.outcome = outcome


def check_key_window_limits(
    database: Any,
    param_types: Any,
    *,
    key_hash: str,
    estimate: int,
    window_limits: dict[str, int],
    idempotency_scope: str | None = None,
    idempotency_fingerprint: str | None = None,
) -> str | None:
    """APPROXIMATE per-window key-cap check. Returns the blocking window name
    ("daily"/"weekly"/"monthly") or None to proceed.

    Runs on a lock-free SNAPSHOT, deliberately OUTSIDE the authorize read-write
    transaction: an in-txn shared read of tr_key_limit before reserve_key's
    conditional UPDATE would reintroduce the read-lock-upgrade deadlock surface
    the typed migration removed (codex #93). The wider race window this opens is
    within the accepted approximation (in-flight reserved is not counted either).

    Idempotent-replay preservation: a retry of an ALREADY-COMMITTED authorize
    must REPLAY, never 429 — so an existing same-fingerprint reservation makes
    this check a pass-through (the in-txn idempotency read stays the final
    authority). A missing typed row also passes through: reserve_key's in-txn
    classification fail-closes it as KEY_MISSING.

    The CALLER must omit windows that don't apply (e.g. a BYOK request on a key
    that excludes BYOK from its caps).
    """
    pt = param_types
    with database.snapshot(multi_use=True) as snapshot:
        if idempotency_scope is not None:
            existing = read_reservation_by_idempotency(snapshot, pt, idempotency_scope)
            if (
                existing is not None
                and existing["idempotency_fingerprint"] == idempotency_fingerprint
            ):
                return None  # replayable — let the transaction replay it
        rows = list(snapshot.execute_sql(
            "SELECT day_usage, day_start, week_usage, week_start, "
            "month_usage, month_start FROM tr_key_limit "
            "WHERE key_hash=@kh AND shard=0",
            params={"kh": key_hash},
            param_types={"kh": pt.STRING},
        ))
    if not rows:
        return None  # no typed row -> reserve_key fail-closes as KEY_MISSING
    floors = window_floors(utcnow())
    day_u, day_s, week_u, week_s, month_u, month_s = rows[0]
    # Pre-DDL rows read NULL usage; a NULL/stale start means the window rolled
    # over (or never started) = zero spend this window.
    current = {
        "daily": int(day_u or 0) if day_s is not None and day_s >= floors["daily"] else 0,
        "weekly": int(week_u or 0) if week_s is not None and week_s >= floors["weekly"] else 0,
        "monthly": (
            int(month_u or 0) if month_s is not None and month_s >= floors["monthly"] else 0
        ),
    }
    for window in ("daily", "weekly", "monthly"):
        limit = window_limits.get(window)
        if limit is not None and current[window] + estimate > limit:
            return window
    return None


def authorize_atomic(
    database: Any,
    param_types: Any,
    *,
    workspace_id: str,
    key_hash: str,
    estimate: int,
    has_credit_candidate: bool,
    reservation_usage_type: str,
    idempotency_scope: str | None,
    idempotency_fingerprint: str | None,
    expires_at: Any,
    build_auth_body: Callable[[str, str], str],
    credit_shard: int = UNSHARDED,
) -> dict:
    """Run the atomic authorize. Returns {outcome, reservation_id?, authorization_id?}.

    `build_auth_body(authorization_id, reservation_id) -> json str` lets the caller
    construct the gateway_authorization body once the ids are known.
    `reservation_usage_type` is the HOLD usage type (Credits if any credit
    candidate, else BYOK). `has_credit_candidate` gates the credit hold.

    Per-window key caps are checked by the CALLER via check_key_window_limits on
    a lock-free snapshot BEFORE this transaction — deliberately NOT in here: a
    shared read of tr_key_limit followed by reserve_key's conditional UPDATE on
    the same row would reintroduce the read-lock-upgrade surface this DML-only
    transaction exists to eliminate (codex #93 review).
    """
    pt = param_types
    if credit_shard < 0:
        raise ValueError("credit_shard must be non-negative")
    is_byok = not has_credit_candidate
    # Stable ids across ABORTED retries (only the committed attempt persists).
    reservation_id = str(uuid.uuid4())
    authorization_id = f"gwa-{uuid.uuid4().hex}"

    def _replay(existing: dict) -> dict:
        return {
            "outcome": AuthorizeOutcome.REPLAY,
            "reservation_id": existing["reservation_id"],
            "authorization_id": existing["authorization_id"],
        }

    def txn(transaction: Any) -> dict:
        if idempotency_scope is not None:
            existing = read_reservation_by_idempotency(transaction, pt, idempotency_scope)
            if existing is not None:
                if existing["idempotency_fingerprint"] != idempotency_fingerprint:
                    raise _Reject(AuthorizeOutcome.IDEMPOTENCY_MISMATCH)
                return _replay(existing)

        key_result = reserve_key(transaction, pt, key_hash, estimate, is_byok=is_byok)
        if key_result == KEY_INSUFFICIENT:
            raise _Reject(AuthorizeOutcome.KEY_LIMIT_EXCEEDED)
        if key_result == KEY_MISSING:
            raise _Reject(AuthorizeOutcome.KEY_MISSING)
        key_hold = estimate if key_result == KEY_ACCEPTED else 0

        credit_hold = 0
        if has_credit_candidate:
            if not reserve_credit(
                transaction, pt, workspace_id, estimate, shard=credit_shard
            ):
                raise _Reject(AuthorizeOutcome.INSUFFICIENT_CREDITS)
            credit_hold = estimate

        insert_reservation(
            transaction, pt,
            reservation_id=reservation_id, workspace_id=workspace_id, key_hash=key_hash,
            ws_shard=credit_shard, credit_shard=credit_shard, key_shard=0,
            credit_reserved_micro=credit_hold, key_reserved_micro=key_hold,
            hold_usage_type=reservation_usage_type, authorization_id=authorization_id,
            idempotency_scope=idempotency_scope, idempotency_fingerprint=idempotency_fingerprint,
            expires_at=expires_at,
        )
        insert_entity_dml(
            transaction, pt, "gateway_authorization", authorization_id,
            build_auth_body(authorization_id, reservation_id),
        )
        return {
            "outcome": AuthorizeOutcome.ACCEPTED,
            "reservation_id": reservation_id,
            "authorization_id": authorization_id,
        }

    try:
        return run_in_transaction_with_retry(database, txn)
    except _Reject as reject:
        return {"outcome": reject.outcome}
    except AlreadyExists:
        # Concurrent first-call lost the unique-idempotency-index race: the winner
        # committed; re-read and replay (codex Step-3 #4) — never a second debit.
        # The conflict was on idempotency_scope, so it is necessarily non-None.
        assert idempotency_scope is not None
        conflict_scope: str = idempotency_scope

        def replay_txn(transaction: Any) -> dict:
            existing = read_reservation_by_idempotency(transaction, pt, conflict_scope)
            if existing is None:  # pragma: no cover - winner must exist post-conflict
                raise _Reject(AuthorizeOutcome.IDEMPOTENCY_MISMATCH)
            # Same fingerprint check as the normal replay path (codex keystone
            # review): a concurrent same-scope but DIFFERENT-body loser must get
            # IDEMPOTENCY_MISMATCH, not the winner's authorization as a replay.
            if existing["idempotency_fingerprint"] != idempotency_fingerprint:
                raise _Reject(AuthorizeOutcome.IDEMPOTENCY_MISMATCH)
            return _replay(existing)

        try:
            return run_in_transaction_with_retry(database, replay_txn)
        except _Reject as reject:
            return {"outcome": reject.outcome}


class SettleOutcome:
    SETTLED = "settled"  # this caller claimed + released the holds
    ALREADY_SETTLED = "already_settled"  # replay: another caller already settled
    NOT_FOUND = "not_found"  # no such reservation
    ERROR = "error"  # a release row-count != 1 -> rolled back, re-drive/alarm
    OUTBOX_GUARDED = "outbox_guarded"  # reaper aborted: a pending/dead outbox row intends a charge


class _SettleError(Exception):
    """A release returned row-count != 1 — roll the settle back (don't leave the
    reservation claimed with the hold unreleased / charge unbooked)."""


def _release_key_or_skip_deleted(
    transaction: Any,
    param_types: Any,
    res: dict[str, Any],
    actual_micro: int,
    *,
    book_to_byok: bool,
) -> tuple[int, dict[str, Any] | None]:
    """Shared key-release classification for settle, reaper, and drain paths.

    `release_key` deliberately returns the raw UPDATE count. A 0 count is
    ambiguous only here, after the reservation has been claimed: the key row may
    have been deleted, or the `reserved >= hold` corruption guard may have fired.
    Missing row is a committed-success warning; present row keeps the loud
    row-count failure path.
    """
    from trusted_router.storage_gcp_counter_dml import key_limit_exists, release_key

    key_hash = str(res["key_hash"])
    key_hold = int(res["key_reserved_micro"])
    key_shard = int(res.get("key_shard", 0) or 0)
    count = release_key(
        transaction,
        param_types,
        key_hash,
        key_hold,
        int(actual_micro),
        book_to_byok=book_to_byok,
        window_floors=window_floors(utcnow()),
        shard=key_shard,
    )
    if count == 1:
        return count, None
    if key_limit_exists(transaction, param_types, key_hash, shard=key_shard):
        return count, None
    return 1, {"key_hash": key_hash, "hold_micro": key_hold}


def _log_missing_key_releases(result: dict[str, Any]) -> None:
    warnings = result.pop("missing_key_releases", ())
    for warning in warnings:
        log.warning(
            "skipped key release for missing tr_key_limit row key_hash=%s hold_micro=%s",
            warning["key_hash"],
            warning["hold_micro"],
        )


def settle_atomic(
    database: Any,
    param_types: Any,
    *,
    reservation_id: str,
    actual_micro: int,
    settled_usage_type: str,
    success: bool,
    guard_outbox: bool = False,
) -> dict:
    """Claim-gated settle/refund in ONE transaction (key then credit lock order).

    Claim flips settled false->true (first-writer-wins); only the winner releases
    the EXACT recorded holds and books `actual`. `success=False` is a refund:
    release the holds, book nothing. Booking matches the legacy finalize: key
    usage by settled usage type (usage vs byok_usage); credit total_usage only
    when the settled usage type is Credits.
    """
    from trusted_router.storage_gcp_counter_dml import (
        claim_reservation,
        read_reservation,
        release_credit,
    )

    pt = param_types
    book_actual = actual_micro if success else 0
    book_to_byok = settled_usage_type == "BYOK"

    def txn(transaction: Any) -> dict:
        res = read_reservation(transaction, pt, reservation_id)
        if res is None:
            return {"outcome": SettleOutcome.NOT_FOUND}
        if guard_outbox:
            aid = res.get("authorization_id")
            if aid:
                # MF2: this strong read inside the read-write claim txn is the
                # real interlock; Spanner serializes it against a concurrent
                # enqueue commit. Snapshot scans are advisory latency filters
                # only and can miss that commit.
                rows = list(transaction.execute_sql(
                    GUARD_COUNT_SQL,
                    params={"aid": aid},
                    param_types={"aid": pt.STRING},
                ))
                if rows and int(rows[0][0]) > 0:
                    return {"outcome": SettleOutcome.OUTBOX_GUARDED}
        won = claim_reservation(
            transaction, pt, reservation_id,
            actual_micro=book_actual, settled_usage_type=settled_usage_type,
        )
        if not won:
            return {"outcome": SettleOutcome.ALREADY_SETTLED}  # replay, no double-apply

        # key first, then credit (single lock order everywhere — codex#2 #2).
        key_actual = book_actual  # key usage counts under both Credits and BYOK
        missing_key_releases = []
        key_count, warning = _release_key_or_skip_deleted(
            transaction, pt, res, key_actual, book_to_byok=book_to_byok
        )
        if warning is not None:
            missing_key_releases.append(warning)
        # A recorded hold MUST release; an uncapped/no-hold row (key_reserved==0)
        # may 0-row and is tolerated (best-effort usage tracking).
        if res["key_reserved_micro"] > 0 and key_count != 1:
            raise _SettleError("key release row-count != 1")

        if res["credit_reserved_micro"] > 0:
            credit_actual = book_actual if settled_usage_type == "Credits" else 0
            credit_count = release_credit(
                transaction, pt, res["workspace_id"], res["credit_reserved_micro"],
                credit_actual,
                shard=res["credit_shard"],
            )
            if credit_count != 1:
                raise _SettleError("credit release row-count != 1")
        return {
            "outcome": SettleOutcome.SETTLED,
            "missing_key_releases": missing_key_releases,
        }

    try:
        result = run_in_transaction_with_retry(database, txn)
        _log_missing_key_releases(result)
        return result
    except _SettleError:
        return {"outcome": SettleOutcome.ERROR}


def _is_table_missing(exc: Exception) -> bool:
    # Rollout guard: code can deploy before the operator-applied outbox DDL.
    # FAIL CLOSED on everything except the ONE real "table itself is missing"
    # shape: Cloud Spanner (and its emulator) raise NotFound("Table not found:
    # tr_settle_outbox") — table name AFTER the phrase. Anchoring on position
    # keeps wrapped transients ("Session not found while querying
    # tr_settle_outbox") and schema errors ('column "status" of relation
    # "tr_settle_outbox" does not exist') from silently unguarding a cycle;
    # any other probe error re-raises, so a mismatch can only delay reaping,
    # never free-release a guarded hold.
    lowered = str(exc).lower()
    return (
        "table not found" in lowered
        and "tr_settle_outbox" in lowered.split("table not found", 1)[1]
    )


# Reaper scan, two forms. The guarded form excludes holds with an outbox row
# whose status is in GUARD_STATUSES IN THE SCAN so frozen holds never consume @limit and cannot
# starve unguarded expired holds behind them (PR #116 review P2). The NOT
# EXISTS runs on a snapshot, so it is ADVISORY ONLY — the strong re-read
# inside settle_atomic(guard_outbox=True) remains the MF2 interlock.
_REAP_SCAN_SQL = (
    "SELECT reservation_id, authorization_id FROM tr_reservation "
    "WHERE settled=false AND expires_at < @now LIMIT @limit"
)
_REAP_SCAN_GUARDED_SQL = (
    "SELECT reservation_id, authorization_id FROM tr_reservation "  # noqa: S608
    "WHERE settled=false AND expires_at < @now "
    "AND NOT EXISTS (SELECT 1 FROM tr_settle_outbox o "
    "WHERE o.authorization_id = tr_reservation.authorization_id "
    f"AND o.status IN ({_GUARD_STATUS_SQL})) "
    "LIMIT @limit"
)


def reap_expired_reservations(
    database: Any, param_types: Any, *, now: Any, limit: int = 100
) -> int:
    """Reclaim crashed-before-settle reservations (settled=false AND expires_at<now).

    Releases each stranded reservation's holds via the SAME claim-gated settle
    path (success=False = refund, books nothing), so a late settle racing the
    reaper is safe — whoever claims the row first wins, the other no-ops.

    The outbox guard is live: advisory filtering happens in the scan SQL, so a
    hold whose authorization has a `tr_settle_outbox` row with status in
    GUARD_STATUSES is invisible to the scan and cannot starve later unguarded
    holds behind @limit (PR #116 review P2). `settle_atomic(..., guard_outbox=True)`
    still does the in-txn re-check; that strong read remains the MF2 interlock.
    `release_approved` is the only human-set status that re-permits this free
    release. Returns the count reaped.
    """
    pt = param_types
    guard_active = True
    try:
        with database.snapshot() as snapshot:
            list(snapshot.execute_sql(
                GUARD_COUNT_SQL,
                params={"aid": ""},
                param_types={"aid": pt.STRING},
            ))
    except Exception as exc:
        if not _is_table_missing(exc):
            raise
        # Pre-migration: the table does not exist, so no intent rows can exist
        # either. Unguarded free-release is exactly today's behavior; the guard
        # arms itself the moment the DDL is applied.
        guard_active = False

    scan_sql = _REAP_SCAN_GUARDED_SQL if guard_active else _REAP_SCAN_SQL
    with database.snapshot() as snapshot:
        rows = list(
            snapshot.execute_sql(
                scan_sql,
                params={"now": now, "limit": int(limit)},
                param_types={"now": pt.TIMESTAMP, "limit": pt.INT64},
            )
        )
    reaped = 0
    for reservation_id, _authorization_id in rows:
        result = settle_atomic(
            database, pt, reservation_id=reservation_id, actual_micro=0,
            settled_usage_type="Credits", success=False, guard_outbox=guard_active,
        )
        if result["outcome"] == SettleOutcome.SETTLED:
            reaped += 1
    return reaped


def typed_finalize_atomic(
    database: Any,
    param_types: Any,
    *,
    reservation_id: str,
    authorization_id: str,
    success: bool,
    actual_micro: int,
    settled_usage_type: str,
    now: Any,
    auth_body_settled: str,
    generation_writes: list | None = None,
) -> dict:
    """Full DML-only finalize for the typed path (codex 3e, Option B).

    ONE transaction reproduces legacy finalize_gateway_authorization's whole
    behavior so a crash can't leave counters charged but the generation missing /
    auth unsettled: claim the reservation -> release the EXACT holds (key then
    credit) and book actual -> on success DML-insert the generation entities ->
    DML-mark the gateway_authorization settled. All writes use a client `now`
    timestamp (NOT PENDING_COMMIT_TIMESTAMP) so the multiple tr_entities DML
    statements don't hit the PCT same-table trap. The caller does the Bigtable
    index AFTER commit (like legacy index_after_commit), and must NOT use
    SpannerGenerations.add() (it would double-book key usage already booked here).

    `generation_writes` = [(kind, entity_id, body_json), ...] inserted only on
    success. `auth_body_settled` = the gateway_authorization JSON body with
    settled=true. Returns {outcome: settled|already_settled|not_found|error}.
    """
    from trusted_router.storage_gcp_counter_dml import (
        claim_reservation,
        insert_entity_dml_at,
        read_reservation,
        release_credit,
        update_entity_body_dml,
    )

    pt = param_types
    book_actual = actual_micro if success else 0
    book_to_byok = settled_usage_type == "BYOK"
    writes = generation_writes or []

    def txn(transaction: Any) -> dict:
        res = read_reservation(transaction, pt, reservation_id)
        if res is None:
            return {"outcome": SettleOutcome.NOT_FOUND}
        won = claim_reservation(
            transaction, pt, reservation_id,
            actual_micro=book_actual, settled_usage_type=settled_usage_type,
        )
        if not won:
            return {"outcome": SettleOutcome.ALREADY_SETTLED}

        missing_key_releases = []
        key_count, warning = _release_key_or_skip_deleted(
            transaction, pt, res, book_actual, book_to_byok=book_to_byok
        )
        if warning is not None:
            missing_key_releases.append(warning)
        if res["key_reserved_micro"] > 0 and key_count != 1:
            raise _SettleError("key release row-count != 1")

        if res["credit_reserved_micro"] > 0:
            credit_actual = book_actual if settled_usage_type == "Credits" else 0
            credit_count = release_credit(
                transaction, pt, res["workspace_id"], res["credit_reserved_micro"],
                credit_actual,
                shard=res["credit_shard"],
            )
            if credit_count != 1:
                raise _SettleError("credit release row-count != 1")

        if success:
            for kind, entity_id, body_json in writes:
                insert_entity_dml_at(transaction, pt, kind, entity_id, body_json, now)

        marked = update_entity_body_dml(
            transaction, pt, "gateway_authorization", authorization_id,
            auth_body_settled, now,
        )
        if marked != 1:
            raise _SettleError("gateway_authorization update row-count != 1")
        return {
            "outcome": SettleOutcome.SETTLED,
            "missing_key_releases": missing_key_releases,
        }

    try:
        attempts_box: list[int] = []
        result = run_in_transaction_with_retry(database, txn, attempts_out=attempts_box)
        result["attempts"] = attempts_box[0] if attempts_box else 1
        _log_missing_key_releases(result)
        return result
    except _SettleError:
        return {"outcome": SettleOutcome.ERROR}


def typed_billing_enabled_for_workspace(
    workspace_id: str, *, allowlist_csv: str, denylist_csv: str
) -> bool:
    """Cohort gate for the typed AUTHORIZE path. Default-off; the denylist is an
    emergency kill switch that always wins; "*" in the allowlist = all. Settle/
    refund route by reservation ORIGIN, not this gate (codex 3e).

    Fast global kill-switch: "*" in the DENYLIST disables typed enforcement for
    every workspace at once (deny always wins, so it beats an "*" allowlist).
    Flip it with `gcloud run services update --update-env-vars
    TR_TYPED_BILLING_WORKSPACE_DENYLIST=*` — takes effect on the next request
    across the region without a code deploy, and reverting is just clearing the
    var. Every typed path (authorize + the typed-aware balance reads) funnels
    through this one predicate, so the kill is comprehensive.

    This is a BREAK-GLASS AVAILABILITY brake, NOT a billing-clean rollback.
    Reach for it when the typed gate itself misbehaves (deadlocks, elevated
    errors/latency) and you need requests flowing again NOW; killed workspaces
    fall back to the legacy JSON authorize path, so the app keeps serving. But
    it is deliberately NOT revenue-neutral for a workspace that was already
    running typed: its JSON usage columns are stale-LOW because the typed-owned
    counters are never mirrored back to JSON (the #79 ownership split), so the
    JSON fallback under-counts spend and can OVER-admit / UNDER-bill until typed
    is re-enabled. A *correct* rollback is the pause -> drain -> backsync runbook
    (storage_gcp_counter_reconcile / #32), which reconciles JSON before cutting
    over; denylisting alone is knowingly lossy. Use the kill-switch to stop the
    bleeding, then run the backsync — don't treat it as a clean off-switch."""
    deny = {w.strip() for w in denylist_csv.split(",") if w.strip()}
    if "*" in deny or workspace_id in deny:
        return False
    allow = {w.strip() for w in allowlist_csv.split(",") if w.strip()}
    return "*" in allow or workspace_id in allow
