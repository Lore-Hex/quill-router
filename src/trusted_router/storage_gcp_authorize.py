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
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from google.api_core.exceptions import AlreadyExists

from trusted_router.app_markup_billing import (
    app_markup_microdollars_from_charge,
    app_markup_owner_share_microdollars,
    app_markup_payout_event_id,
)
from trusted_router.catalog import PROVIDERS, endpoint_for_id
from trusted_router.custom_model_billing import user_model_payout_event_id
from trusted_router.custom_model_markup_billing import (
    custom_model_markup_payout_event_id,
)
from trusted_router.services.spend_lease_settlement import clamp_spend_lease_charge
from trusted_router.spend_lease_admission import classify_receipt_replay
from trusted_router.spend_windows import (
    KeyWindowLimitDecision,
    decide_key_window_limits,
    utcnow,
    window_floors,
)
from trusted_router.stage_d import parse_pricing_snapshot
from trusted_router.storage_gcp_counter_dml import (
    KEY_ACCEPTED,
    KEY_INSUFFICIENT,
    KEY_MISSING,
    complete_reservation_retention,
    insert_entity_dml,
    insert_reservation,
    read_reservation_by_idempotency,
    reserve_credit,
    reserve_key,
)
from trusted_router.storage_gcp_counters import UNSHARDED
from trusted_router.storage_gcp_generation_records import insert_generation_record
from trusted_router.storage_gcp_io import run_in_transaction_with_retry
from trusted_router.storage_gcp_request_records import (
    complete_gateway_authorization_retention,
    insert_gateway_authorization,
    mark_gateway_authorization_settled,
    read_gateway_authorization,
    read_gateway_authorization_admission_columns,
)
from trusted_router.storage_gcp_settle_outbox import (
    _GUARD_STATUS_SQL,
    GUARD_COUNT_SQL,
    mark_done_unleased_tx,
    rewrite_frozen_settlement_tx,
)
from trusted_router.storage_gcp_stage_d import (
    delivered_usage_charge_microdollars,
    delivered_usage_counts,
    normalized_delivered_prompt,
)
from trusted_router.storage_models import (
    AppMarkupPayout,
    CustomModelMarkupPayout,
    GatewayAuthorization,
    Generation,
    UserModelPayout,
)
from trusted_router.types import UsageType

log = logging.getLogger(__name__)

# A rejected conditional UPDATE keeps its row lock until the transaction rolls
# back. Scanning every configured shard therefore lets one depleted tenant turn
# N concurrent authorizations into N * shard_count contended row locks. Keep the
# hot transaction small; the caller retains the full shard set for its lock-free
# aggregate precheck and cold-path escrow rebalance.
MAX_CREDIT_SHARD_ATTEMPTS_PER_TRANSACTION = 4


def bounded_credit_shard_candidates(candidates: tuple[int, ...]) -> tuple[int, ...]:
    """Return the fixed, pre-randomized hot-path subset for one transaction."""

    if not candidates:
        raise ValueError("credit_shard_candidates must not be empty")
    return candidates[:MAX_CREDIT_SHARD_ATTEMPTS_PER_TRANSACTION]


class AuthorizeOutcome:
    ACCEPTED = "accepted"
    REPLAY = "replay"  # idempotent replay: resume, do NOT re-execute
    INSUFFICIENT_CREDITS = "insufficient_credits"
    KEY_LIMIT_EXCEEDED = "key_limit_exceeded"
    KEY_MISSING = "key_missing"  # typed key row absent -> fail closed
    IDEMPOTENCY_MISMATCH = "idempotency_mismatch"  # same key, different request body
    ADMISSION_SCOPE_CONFLICT = "admission_scope_conflict"
    KEY_WINDOW_LIMIT_EXCEEDED = "key_window_limit_exceeded"  # a daily/weekly/monthly cap


class AuthorizeVerdict(str):
    """String-compatible typed-authorize outcome with its window decision."""

    rate_limit: KeyWindowLimitDecision | None
    spend_lease_bound: bool
    no_lease_reason: str | None
    spend_lease_outcome: str | None

    def __new__(
        cls,
        outcome: str,
        *,
        rate_limit: KeyWindowLimitDecision | None = None,
        spend_lease_bound: bool = False,
        no_lease_reason: str | None = None,
        spend_lease_outcome: str | None = None,
    ) -> AuthorizeVerdict:
        verdict = super().__new__(cls, outcome)
        verdict.rate_limit = rate_limit
        verdict.spend_lease_bound = spend_lease_bound
        verdict.no_lease_reason = no_lease_reason
        verdict.spend_lease_outcome = spend_lease_outcome
        return verdict


class _Reject(Exception):
    """Roll the authorize transaction back with a terminal outcome (not retried)."""

    def __init__(self, outcome: str) -> None:
        self.outcome = outcome


def key_window_limit_decision(
    database: Any,
    param_types: Any,
    *,
    key_hash: str,
    estimate: int,
    window_limits: dict[str, int],
    shard_count: int = 1,
    idempotency_scope: str | None = None,
    idempotency_fingerprint: str | None = None,
) -> KeyWindowLimitDecision | None:
    """Return the authoritative APPROXIMATE per-window key-cap decision.

    Runs on a lock-free SNAPSHOT, deliberately OUTSIDE the authorize read-write
    transaction: an in-txn shared read of tr_key_limit before reserve_key's
    conditional UPDATE would reintroduce the read-lock-upgrade deadlock surface
    the typed migration removed (codex #93). The wider race window this opens is
    within the accepted approximation (in-flight reserved is not counted either).

    Idempotent-replay preservation: a retry of an ALREADY-COMMITTED authorize
    must REPLAY, never 429 — so an existing same-fingerprint reservation makes
    this check a pass-through (the in-txn idempotency read stays the final
    authority). A missing typed row also passes through: reserve_key's in-txn
    classification fail-closes it as KEY_MISSING. When the configured set has
    multiple rows, usage is summed over every row and an incomplete set fails
    closed.

    The CALLER must omit windows that don't apply (e.g. a BYOK request on a key
    that excludes BYOK from its caps).
    """
    if shard_count < 1:
        raise ValueError("shard_count must be positive")
    pt = param_types
    with database.snapshot(multi_use=True) as snapshot:
        if idempotency_scope is not None:
            existing = read_reservation_by_idempotency(snapshot, pt, idempotency_scope)
            if (
                existing is not None
                and existing["idempotency_fingerprint"] == idempotency_fingerprint
            ):
                return None  # replayable — let the transaction replay it
        rows = list(
            snapshot.execute_sql(
                "SELECT shard, day_usage, day_start, week_usage, week_start, "
                "month_usage, month_start FROM tr_key_limit "
                "WHERE key_hash=@kh AND shard>=0 AND shard<@shard_count "
                "ORDER BY shard",
                params={"kh": key_hash, "shard_count": shard_count},
                param_types={"kh": pt.STRING, "shard_count": pt.INT64},
            )
        )
    if not rows:
        return None  # no typed row -> reserve_key fail-closes as KEY_MISSING
    if [int(row[0]) for row in rows] != list(range(shard_count)):
        raise RuntimeError("configured tr_key_limit usage shard set is incomplete")
    decision_now = utcnow()
    floors = window_floors(decision_now)
    # Pre-DDL rows read NULL usage; a NULL/stale start means the window rolled
    # over (or never started) = zero spend this window.
    current = {
        "daily": sum(
            int(row[1] or 0) for row in rows if row[2] is not None and row[2] >= floors["daily"]
        ),
        "weekly": sum(
            int(row[3] or 0) for row in rows if row[4] is not None and row[4] >= floors["weekly"]
        ),
        "monthly": sum(
            int(row[5] or 0) for row in rows if row[6] is not None and row[6] >= floors["monthly"]
        ),
    }
    return decide_key_window_limits(
        window_limits,
        current,
        estimate,
        now=decision_now,
    )


def check_key_window_limits(
    database: Any,
    param_types: Any,
    *,
    key_hash: str,
    estimate: int,
    window_limits: dict[str, int],
    shard_count: int = 1,
    idempotency_scope: str | None = None,
    idempotency_fingerprint: str | None = None,
) -> str | None:
    """Backward-compatible blocking-window view of the richer verdict."""
    decision = key_window_limit_decision(
        database,
        param_types,
        key_hash=key_hash,
        estimate=estimate,
        window_limits=window_limits,
        shard_count=shard_count,
        idempotency_scope=idempotency_scope,
        idempotency_fingerprint=idempotency_fingerprint,
    )
    if decision is None or decision.allowed:
        return None
    return decision.window


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
    build_authorization: Callable[[str, str], GatewayAuthorization] | None = None,
    build_auth_body: Callable[[str, str], str] | None = None,
    request_record_write_mode: str = "legacy",
    credit_shard: int = UNSHARDED,
    credit_shard_candidates: tuple[int, ...] | None = None,
    key_shard_candidates: tuple[int, ...] = (UNSHARDED,),
    authorization_id: str | None = None,
    spend_lease_hook: Callable[[Any, int], dict[str, Any]] | None = None,
    build_authorization_for_lease: (
        Callable[[str, str, bool], GatewayAuthorization] | None
    ) = None,
    also_retry: tuple[type[BaseException], ...] = (),
    spend_lease_receipt_hash: str | None = None,
    credit_escrowed_by_spend_lease: bool = False,
    spend_lease_admission_replay_protection: bool = False,
) -> dict:
    """Run the atomic authorize. Returns {outcome, reservation_id?, authorization_id?}.

    `request_record_write_mode="legacy"` preserves the generic tr_entities write
    during the expand rollout. `"typed"` writes the same authorization into the
    bounded typed table. The corresponding builder is required for the selected
    mode.
    `reservation_usage_type` is the HOLD usage type (Credits if any credit
    candidate, else BYOK). `has_credit_candidate` gates the credit hold.
    `credit_shard_candidates` is a bounded, pre-randomized order built outside
    the transaction so Spanner retries use the same order. The first shard with
    enough independent sub-budget is recorded durably on the reservation. The
    store wrapper applies `bounded_credit_shard_candidates`; direct callers must
    likewise pass no more than the hot-path limit.

    Per-window key caps are checked by the CALLER via check_key_window_limits on
    a lock-free snapshot BEFORE this transaction — deliberately NOT in here: a
    shared read of tr_key_limit followed by reserve_key's conditional UPDATE on
    the same row would reintroduce the read-lock-upgrade surface this DML-only
    transaction exists to eliminate (codex #93 review).
    """
    pt = param_types
    if request_record_write_mode not in {"legacy", "typed"}:
        raise ValueError("request_record_write_mode must be 'legacy' or 'typed'")
    if request_record_write_mode == "typed" and build_authorization is None:
        raise ValueError("typed request records require build_authorization")
    if request_record_write_mode == "legacy" and build_auth_body is None:
        raise ValueError("legacy request records require build_auth_body")
    shard_candidates: tuple[int, ...]
    if credit_shard_candidates is None:
        shard_candidates = (credit_shard,)
    else:
        shard_candidates = tuple(credit_shard_candidates)
        if credit_shard != UNSHARDED:
            raise ValueError("pass credit_shard or credit_shard_candidates, not both")
    if not shard_candidates:
        raise ValueError("credit_shard_candidates must not be empty")
    if any(shard < 0 for shard in shard_candidates):
        raise ValueError("credit shards must be non-negative")
    if len(set(shard_candidates)) != len(shard_candidates):
        raise ValueError("credit_shard_candidates must be unique")
    if has_credit_candidate and len(shard_candidates) > MAX_CREDIT_SHARD_ATTEMPTS_PER_TRANSACTION:
        raise ValueError("credit_shard_candidates exceeds the hot-path transaction limit")
    if not has_credit_candidate and shard_candidates != (UNSHARDED,):
        raise ValueError("BYOK-only authorization must use credit shard zero")
    if credit_escrowed_by_spend_lease and (
        not has_credit_candidate or spend_lease_hook is None
    ):
        raise ValueError("lease-escrowed credit requires a Credits route and spend-lease hook")
    key_candidates = tuple(key_shard_candidates)
    if not key_candidates:
        raise ValueError("key_shard_candidates must not be empty")
    if any(shard < 0 for shard in key_candidates):
        raise ValueError("key shards must be non-negative")
    if len(set(key_candidates)) != len(key_candidates):
        raise ValueError("key_shard_candidates must be unique")
    is_byok = not has_credit_candidate
    # Stable ids across ABORTED retries (only the committed attempt persists).
    reservation_id = str(uuid.uuid4())
    authorization_id = authorization_id or f"gwa-{uuid.uuid4().hex}"
    created_at = utcnow()
    authorization = (
        build_authorization(authorization_id, reservation_id)
        if build_authorization is not None
        else None
    )
    if authorization is not None:
        authorization.created_at = created_at.isoformat().replace("+00:00", "Z")
    legacy_auth_body = (
        build_auth_body(authorization_id, reservation_id) if build_auth_body is not None else None
    )

    def _replay(transaction: Any, existing: dict) -> dict:
        receipt_verdict = "ordinary"
        if spend_lease_admission_replay_protection:
            admission = read_gateway_authorization_admission_columns(
                transaction,
                pt,
                str(existing["authorization_id"]),
            )
            stored_receipt_hash = (
                str(admission["spend_lease_receipt_hash"])
                if admission is not None
                and admission["spend_lease_receipt_hash"] is not None
                else None
            )
            receipt_verdict = classify_receipt_replay(
                spend_lease_receipt_hash,
                stored_receipt_hash,
            )
        if receipt_verdict == "scope_conflict":
            raise _Reject(AuthorizeOutcome.ADMISSION_SCOPE_CONFLICT)
        if receipt_verdict == "ordinary" and (
            existing["idempotency_fingerprint"] != idempotency_fingerprint
        ):
            raise _Reject(AuthorizeOutcome.IDEMPOTENCY_MISMATCH)
        return {
            "outcome": AuthorizeOutcome.REPLAY,
            "reservation_id": existing["reservation_id"],
            "authorization_id": existing["authorization_id"],
            "credit_shard": int(existing.get("credit_shard", UNSHARDED)),
            "key_shard": int(existing.get("key_shard", UNSHARDED)),
        }

    def txn(transaction: Any) -> dict:
        if idempotency_scope is not None:
            existing = read_reservation_by_idempotency(transaction, pt, idempotency_scope)
            if existing is not None:
                return _replay(transaction, existing)

        key_result = KEY_MISSING
        selected_key_shard = UNSHARDED
        saw_key_row = False
        for candidate in key_candidates:
            candidate_result = reserve_key(
                transaction,
                pt,
                key_hash,
                estimate,
                is_byok=is_byok,
                shard=candidate,
            )
            if candidate_result == KEY_MISSING:
                continue
            saw_key_row = True
            if candidate_result == KEY_INSUFFICIENT:
                continue
            key_result = candidate_result
            selected_key_shard = candidate
            break
        if key_result == KEY_MISSING:
            raise _Reject(
                AuthorizeOutcome.KEY_LIMIT_EXCEEDED if saw_key_row else AuthorizeOutcome.KEY_MISSING
            )
        key_hold = estimate if key_result == KEY_ACCEPTED else 0

        credit_hold = 0
        selected_credit_shard = UNSHARDED
        if has_credit_candidate and not credit_escrowed_by_spend_lease:
            for candidate in shard_candidates:
                if reserve_credit(transaction, pt, workspace_id, estimate, shard=candidate):
                    selected_credit_shard = candidate
                    break
            else:
                raise _Reject(AuthorizeOutcome.INSUFFICIENT_CREDITS)
            credit_hold = estimate

        lease_result: dict[str, Any] = {
            "bound": False,
            "no_lease_reason": None,
            "spend_lease_outcome": None,
        }
        if spend_lease_hook is not None:
            lease_result = spend_lease_hook(transaction, selected_credit_shard)
        if spend_lease_receipt_hash is not None and not lease_result.get("bound"):
            no_lease_reason = lease_result.get("no_lease_reason")
            if no_lease_reason == "scope_arbitrated":
                reason = "scope_conflict"
            elif no_lease_reason == "unpaid_workspace":
                reason = "hold_refused"
            else:
                reason = "reuse_lost"
            raise _Reject(f"admission_rejected:{reason}")

        insert_reservation(
            transaction,
            pt,
            reservation_id=reservation_id,
            workspace_id=workspace_id,
            key_hash=key_hash,
            ws_shard=selected_credit_shard,
            credit_shard=selected_credit_shard,
            key_shard=selected_key_shard,
            credit_reserved_micro=credit_hold,
            key_reserved_micro=key_hold,
            hold_usage_type=reservation_usage_type,
            authorization_id=authorization_id,
            idempotency_scope=idempotency_scope,
            idempotency_fingerprint=idempotency_fingerprint,
            expires_at=expires_at,
            created_at=created_at,
        )
        if request_record_write_mode == "typed":
            selected_authorization = authorization
            if build_authorization_for_lease is not None:
                selected_authorization = build_authorization_for_lease(
                    authorization_id,
                    reservation_id,
                    bool(lease_result.get("bound")),
                )
                selected_authorization.created_at = created_at.isoformat().replace(
                    "+00:00", "Z"
                )
            assert selected_authorization is not None
            insert_gateway_authorization(
                transaction,
                pt,
                selected_authorization,
                created_at=created_at,
            )
        else:
            assert legacy_auth_body is not None
            insert_entity_dml(
                transaction,
                pt,
                "gateway_authorization",
                authorization_id,
                legacy_auth_body,
            )
        return {
            "outcome": AuthorizeOutcome.ACCEPTED,
            "reservation_id": reservation_id,
            "authorization_id": authorization_id,
            "credit_shard": selected_credit_shard,
            "key_shard": selected_key_shard,
            **lease_result,
        }

    try:
        return run_in_transaction_with_retry(
            database,
            txn,
            transaction_tag="tr_authorize",
            also_retry=also_retry,
        )
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
            return _replay(transaction, existing)

        try:
            return run_in_transaction_with_retry(
                database,
                replay_txn,
                transaction_tag="tr_authorize_replay",
            )
        except _Reject as reject:
            return {"outcome": reject.outcome}


class SettleOutcome:
    SETTLED = "settled"  # this caller claimed + released the holds
    ALREADY_SETTLED = "already_settled"  # replay: another caller already settled
    NOT_FOUND = "not_found"  # no such reservation
    ERROR = "error"  # a release row-count != 1 -> rolled back, re-drive/alarm
    OUTBOX_GUARDED = "outbox_guarded"  # reaper aborted: a pending/dead outbox row intends a charge
    NOT_ELIGIBLE = "not_eligible"  # reaper strong read found renewed/already-terminal state
    GUARD_LOST = "guard_lost"  # reaper candidate changed after its decisive read
    AUTHORIZATION_NOT_TYPED = "authorization_not_typed"


class _SettleError(Exception):
    """A release returned row-count != 1 — roll the settle back (don't leave the
    reservation claimed with the hold unreleased / charge unbooked)."""


class _ReapGuardLost(Exception):
    """Abort a reaper transaction whose final row-count guard lost."""


@dataclass(frozen=True, slots=True)
class ReapPassResult:
    count: int = 0
    released_hold_micro: int = 0
    started_markers: int = 0
    snapshot_bookings: int = 0
    out_of_cohort: int = 0
    not_eligible: int = 0
    guarded: int = 0
    guard_lost: int = 0
    errors: int = 0
    refunded: int = 0

    @property
    def outcome_counts(self) -> dict[str, int]:
        return {
            SettleOutcome.NOT_ELIGIBLE: self.not_eligible,
            SettleOutcome.OUTBOX_GUARDED: self.guarded,
            SettleOutcome.GUARD_LOST: self.guard_lost,
            SettleOutcome.ERROR: self.errors,
            "refunded": self.refunded,
            "snapshot_booked": self.snapshot_bookings,
        }

    @property
    def started_marker_share(self) -> float:
        return self.started_markers / self.count if self.count else 0.0

    @property
    def out_of_cohort_share(self) -> float:
        return self.out_of_cohort / self.count if self.count else 0.0


@dataclass(frozen=True, slots=True)
class _ReapOneResult:
    outcome: str
    released_hold_micro: int = 0
    started_marker: bool = False
    snapshot_booked: bool = False
    out_of_cohort: bool = False
    missing_key_releases: tuple[dict[str, Any], ...] = ()


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
    outbox_available: bool | None = None,
    expires_before: Any | None = None,
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
    terminal_at = utcnow()
    resolved_outbox_available = (
        _outbox_table_available(database, pt) if outbox_available is None else outbox_available
    )

    def txn(transaction: Any) -> dict:
        res = read_reservation(transaction, pt, reservation_id)
        if res is None:
            return {"outcome": SettleOutcome.NOT_FOUND}
        if guard_outbox and resolved_outbox_available:
            aid = res.get("authorization_id")
            if aid:
                # MF2: this strong read inside the read-write claim txn is the
                # real interlock; Spanner serializes it against a concurrent
                # enqueue commit. Snapshot scans are advisory latency filters
                # only and can miss that commit.
                rows = list(
                    transaction.execute_sql(
                        GUARD_COUNT_SQL,
                        params={"aid": aid},
                        param_types={"aid": pt.STRING},
                    )
                )
                if rows and int(rows[0][0]) > 0:
                    return {"outcome": SettleOutcome.OUTBOX_GUARDED}
        won = claim_reservation(
            transaction,
            pt,
            reservation_id,
            actual_micro=book_actual,
            settled_usage_type=settled_usage_type,
            terminal_at=terminal_at,
            outbox_available=resolved_outbox_available,
            expires_before=expires_before,
        )
        if not won:
            return {"outcome": SettleOutcome.ALREADY_SETTLED}  # replay, no double-apply

        # A regional authorization spent from an already escrowed lease. The
        # regional ledger is settled/refunded before this transaction and the
        # lease reconciler imports aggregate spend later. Releasing counters
        # here would double-release the grant and recreate the hot global row.
        if res.get("hold_usage_type") == "RegionalCredits":
            return {
                "outcome": SettleOutcome.SETTLED,
                "missing_key_releases": [],
            }

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
                transaction,
                pt,
                res["workspace_id"],
                res["credit_reserved_micro"],
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
        result = run_in_transaction_with_retry(
            database,
            txn,
            transaction_tag="tr_settle" if success else "tr_refund",
        )
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


_OUTBOX_ABSENT_CACHE_SECONDS = 5.0
# Keyed by a STABLE STRING, never by the Database object itself: the production
# google.cloud.spanner_v1.database.Database defines __eq__ and sets
# __hash__ = None, so keying a (Weak)dict on it raises TypeError on every
# get/set. A cache that swallowed that error silently never cached in prod and
# every settle/refund paid an extra GUARD_COUNT_SQL probe round trip on the hot
# path — invisible to tests, whose fake database happens to be hashable.
_OUTBOX_AVAILABILITY_CACHE: dict[str, tuple[bool, float]] = {}
_OUTBOX_AVAILABILITY_CACHE_LOCK = threading.Lock()
_OUTBOX_AVAILABILITY_PROBE_LOCK = threading.Lock()


def _outbox_cache_key(database: Any) -> str | None:
    """Stable identity for a Spanner database, or None to skip caching.

    `Database.name` is the fully-qualified `projects/.../databases/...` path:
    unique per database and stable for the process, so the cache is bounded by
    the number of databases a process talks to (one, in practice).

    A database WITHOUT a name is not cached at all. `id()` would be the obvious
    fallback, but object addresses are REUSED after garbage collection, so a
    destroyed double's entry could be inherited by an unrelated database and
    select stale SQL — present-then-absent would emit guarded DML against a
    missing table and break settlement. Only test doubles lack a name, and for
    them the probe is an in-memory call, so re-probing is strictly cheaper than
    that risk.
    """
    name = getattr(database, "name", None)
    if isinstance(name, str) and name:
        return name
    return None


def _cached_outbox_availability(database: Any, *, now: float) -> bool | None:
    key = _outbox_cache_key(database)
    if key is None:
        return None
    with _OUTBOX_AVAILABILITY_CACHE_LOCK:
        cached = _OUTBOX_AVAILABILITY_CACHE.get(key)
        if cached is None:
            return None
        available, expires_at = cached
        if expires_at <= now:
            _OUTBOX_AVAILABILITY_CACHE.pop(key, None)
            return None
        return available


def _remember_outbox_availability(
    database: Any,
    *,
    available: bool,
    now: float,
) -> None:
    key = _outbox_cache_key(database)
    if key is None:
        return
    expires_at = float("inf") if available else now + _OUTBOX_ABSENT_CACHE_SECONDS
    with _OUTBOX_AVAILABILITY_CACHE_LOCK:
        _OUTBOX_AVAILABILITY_CACHE[key] = (available, expires_at)


def _outbox_table_available(database: Any, param_types: Any) -> bool:
    """Return whether guarded SQL may reference the rollout-added outbox table.

    A positive result is stable for the life of the process. A missing-table
    result has a short TTL so a process deployed before the DDL automatically
    adopts the guard shortly after the migration lands. Only the exact Spanner
    table-missing shape selects unguarded SQL; transient/schema probe failures
    fail toward the guarded variant and are not cached.
    """
    now = time.monotonic()
    cached = _cached_outbox_availability(database, now=now)
    if cached is not None:
        return cached

    # Serialize cache misses so concurrent first settles do not stampede the
    # one process-level availability probe.
    with _OUTBOX_AVAILABILITY_PROBE_LOCK:
        now = time.monotonic()
        cached = _cached_outbox_availability(database, now=now)
        if cached is not None:
            return cached
        try:
            with database.snapshot() as snapshot:
                list(
                    snapshot.execute_sql(
                        GUARD_COUNT_SQL,
                        params={"aid": ""},
                        param_types={"aid": param_types.STRING},
                    )
                )
        except Exception as exc:
            if not _is_table_missing(exc):
                log.warning(
                    "tr_settle_outbox availability probe failed; using guarded SQL",
                    exc_info=True,
                )
                return True
            available = False
        else:
            available = True
        _remember_outbox_availability(database, available=available, now=now)
        return available


# Reaper scan, two forms. The guarded form excludes holds with an outbox row
# whose status is in GUARD_STATUSES IN THE SCAN so frozen holds never consume @limit and cannot
# starve unguarded expired holds behind them (PR #116 review P2). The NOT
# EXISTS runs on a snapshot, so it is ADVISORY ONLY — the strong re-read
# inside settle_atomic(guard_outbox=True) remains the MF2 interlock.
_REAP_SCAN_SQL = (
    "SELECT reservation_id, authorization_id, credit_reserved_micro, key_reserved_micro "
    "FROM tr_reservation "
    "WHERE settled=false AND expires_at < @now LIMIT @limit"
)
_REAP_SCAN_GUARDED_SQL = (
    "SELECT reservation_id, authorization_id, credit_reserved_micro, "  # noqa: S608
    "key_reserved_micro "
    "FROM tr_reservation "
    "WHERE settled=false AND expires_at < @now "
    "AND NOT EXISTS (SELECT 1 FROM tr_settle_outbox o "
    "WHERE o.authorization_id = tr_reservation.authorization_id "
    f"AND o.status IN ({_GUARD_STATUS_SQL})) "
    "LIMIT @limit"
)


def reap_expired_reservations_result(
    database: Any,
    param_types: Any,
    *,
    now: Any,
    limit: int = 100,
    snapshot_booking_enabled: bool = False,
    operational_analytics_outbox: Any | None = None,
) -> ReapPassResult:
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
            list(
                snapshot.execute_sql(
                    GUARD_COUNT_SQL,
                    params={"aid": ""},
                    param_types={"aid": pt.STRING},
                )
            )
    except Exception as exc:
        if not _is_table_missing(exc):
            raise
        # Pre-migration: the table does not exist, so no intent rows can exist
        # either. Unguarded free-release is exactly today's behavior; the guard
        # arms itself the moment the DDL is applied.
        guard_active = False
    _remember_outbox_availability(
        database,
        available=guard_active,
        now=time.monotonic(),
    )

    scan_sql = _REAP_SCAN_GUARDED_SQL if guard_active else _REAP_SCAN_SQL
    with database.snapshot() as snapshot:
        rows = list(
            snapshot.execute_sql(
                scan_sql,
                params={"now": now, "limit": int(limit)},
                param_types={"now": pt.TIMESTAMP, "limit": pt.INT64},
            )
        )
    count = 0
    released_hold_micro = 0
    started_markers = 0
    snapshot_bookings = 0
    out_of_cohort = 0
    not_eligible = 0
    guarded = 0
    guard_lost = 0
    errors = 0
    refunded = 0
    for (
        reservation_id,
        _authorization_id,
        advisory_credit_hold_micro,
        advisory_key_hold_micro,
    ) in rows:
        result = _finalize_reaped_reservation_atomic(
            database,
            pt,
            reservation_id=str(reservation_id),
            reap_now=now,
            guard_outbox=guard_active,
            snapshot_booking_enabled=snapshot_booking_enabled,
            operational_analytics_outbox=operational_analytics_outbox,
        )
        if result.outcome == SettleOutcome.AUTHORIZATION_NOT_TYPED:
            # Rolling legacy authorizations have no heartbeat columns. Preserve
            # their existing zero-release behavior, now with the same strong
            # expiry predicate that protects typed Stage D reservations.
            legacy = settle_atomic(
                database,
                pt,
                reservation_id=str(reservation_id),
                actual_micro=0,
                settled_usage_type="Credits",
                success=False,
                guard_outbox=guard_active,
                outbox_available=guard_active,
                expires_before=now,
            )
            if legacy["outcome"] == SettleOutcome.SETTLED:
                result = _ReapOneResult(
                    outcome=SettleOutcome.SETTLED,
                    released_hold_micro=max(
                        int(advisory_credit_hold_micro or 0),
                        int(advisory_key_hold_micro or 0),
                    ),
                    out_of_cohort=True,
                )
            elif legacy["outcome"] == SettleOutcome.OUTBOX_GUARDED:
                result = _ReapOneResult(SettleOutcome.OUTBOX_GUARDED)
            elif legacy["outcome"] == SettleOutcome.ERROR:
                result = _ReapOneResult(SettleOutcome.ERROR)
            else:
                result = _ReapOneResult(SettleOutcome.GUARD_LOST)
        if result.outcome == SettleOutcome.NOT_ELIGIBLE:
            not_eligible += 1
            continue
        if result.outcome == SettleOutcome.OUTBOX_GUARDED:
            guarded += 1
            continue
        if result.outcome == SettleOutcome.GUARD_LOST:
            guard_lost += 1
            continue
        if result.outcome == SettleOutcome.ERROR:
            errors += 1
            continue
        if result.outcome != SettleOutcome.SETTLED:
            errors += 1
            continue
        count += 1
        released_hold_micro += result.released_hold_micro
        started_markers += int(result.started_marker)
        snapshot_bookings += int(result.snapshot_booked)
        refunded += int(not result.snapshot_booked)
        out_of_cohort += int(result.out_of_cohort)
    return ReapPassResult(
        count=count,
        released_hold_micro=released_hold_micro,
        started_markers=started_markers,
        snapshot_bookings=snapshot_bookings,
        out_of_cohort=out_of_cohort,
        not_eligible=not_eligible,
        guarded=guarded,
        guard_lost=guard_lost,
        errors=errors,
        refunded=refunded,
    )


def reap_expired_reservations(
    database: Any, param_types: Any, *, now: Any, limit: int = 100
) -> int:
    """Compatibility wrapper for callers that only need the reaped count."""

    return reap_expired_reservations_result(
        database,
        param_types,
        now=now,
        limit=limit,
    ).count


def _finalize_reaped_reservation_atomic(
    database: Any,
    param_types: Any,
    *,
    reservation_id: str,
    reap_now: Any,
    guard_outbox: bool,
    snapshot_booking_enabled: bool,
    operational_analytics_outbox: Any | None,
) -> _ReapOneResult:
    """Finalize one advisory reaper candidate under strong transaction reads."""

    from trusted_router.storage_gcp_counter_dml import (
        claim_reservation,
        read_reservation,
        release_credit,
    )

    pt = param_types
    reap_timestamp = _reap_datetime(reap_now)

    def txn(transaction: Any) -> _ReapOneResult:
        # Decision 70's decisive read: the scan may be stale, but this read and
        # every write below share one serializable transaction.
        res = read_reservation(transaction, pt, reservation_id)
        if (
            res is None
            or bool(res.get("settled"))
            or res.get("expires_at") is None
            or _reap_datetime(res["expires_at"]) >= reap_timestamp
        ):
            return _ReapOneResult(SettleOutcome.NOT_ELIGIBLE)
        authorization_id = str(res.get("authorization_id") or "")
        if guard_outbox and authorization_id:
            guarded = list(
                transaction.execute_sql(
                    GUARD_COUNT_SQL,
                    params={"aid": authorization_id},
                    param_types={"aid": pt.STRING},
                )
            )
            if guarded and int(guarded[0][0]) > 0:
                return _ReapOneResult(SettleOutcome.OUTBOX_GUARDED)

        authorization = read_gateway_authorization(transaction, pt, authorization_id)
        if authorization is None:
            return _ReapOneResult(SettleOutcome.AUTHORIZATION_NOT_TYPED)
        if (
            authorization.settled
            or authorization.credit_reservation_id != reservation_id
        ):
            raise _ReapGuardLost("authorization terminal/binding guard lost")

        started_marker = authorization.started_at is not None
        out_of_cohort = authorization.pricing_snapshot is None
        snapshot_booked = bool(
            snapshot_booking_enabled and started_marker and not out_of_cohort
        )
        generation: Generation | None = None
        app_markup_payout: AppMarkupPayout | None = None
        actual_micro = 0
        if snapshot_booked:
            if (
                authorization.selected_endpoint_id is None
                or authorization.delivered_usage is None
            ):
                raise _SettleError("started snapshot is missing endpoint or delivered usage")
            usage = delivered_usage_counts(authorization.delivered_usage)
            document = parse_pricing_snapshot(authorization.pricing_snapshot or "")
            selected_endpoint = endpoint_for_id(authorization.selected_endpoint_id)
            selected_provider = (
                selected_endpoint.provider
                if selected_endpoint is not None
                else authorization.provider
            )
            selected_model_id = (
                selected_endpoint.model_id
                if selected_endpoint is not None
                else authorization.model_id
            )
            actual_micro = delivered_usage_charge_microdollars(
                authorization,
                document,
                selected_provider,
                authorization.selected_endpoint_id,
                usage,
            )
            if authorization.settlement == "spend_lease":
                actual_micro = min(
                    clamp_spend_lease_charge(authorization, actual_micro),
                    int(res["credit_reserved_micro"]),
                )
            _uncached_input, total_input = normalized_delivered_prompt(
                selected_provider,
                usage,
            )
            provider = PROVIDERS.get(selected_provider)
            app_markup_micro = 0
            if authorization.app_markup_basis_points > 0:
                app_markup_micro = app_markup_microdollars_from_charge(
                    actual_micro,
                    authorization.app_markup_basis_points,
                )
                payout_micro = min(
                    app_markup_owner_share_microdollars(app_markup_micro),
                    app_markup_micro,
                )
                app_markup_payout = AppMarkupPayout(
                    owner_user_id=authorization.app_owner_user_id,
                    app_id=authorization.app_id,
                    amount_microdollars=payout_micro,
                    payer_workspace_id=authorization.workspace_id,
                )
            generation = Generation.from_settle_body(
                authorization=authorization,
                provider_name=str(getattr(provider, "name", selected_provider)),
                model_id=selected_model_id,
                usage_type=UsageType.CREDITS,
                provider=selected_provider,
                body={
                    "streamed": True,
                    "usage_estimated": True,
                    "cache_read_input_tokens": usage["cache_read_input_tokens"],
                    "reasoning_tokens": usage["reasoning_tokens"],
                    "status": "success",
                    "finish_reason": "reaped_snapshot",
                },
                input_tokens=total_input,
                output_tokens=usage["output_tokens"],
                actual_cost_microdollars=actual_micro,
                app_markup_microdollars=app_markup_micro,
            )
            generation.settled_from = "heartbeat"

        won = claim_reservation(
            transaction,
            pt,
            reservation_id,
            actual_micro=actual_micro,
            settled_usage_type="Credits",
            terminal_at=reap_timestamp,
            defer_retention=True,
            outbox_available=guard_outbox,
            expires_before=reap_timestamp,
        )
        if not won:
            raise _ReapGuardLost("expired reservation claim guard lost")

        missing_key_releases = []
        if res.get("hold_usage_type") != "RegionalCredits":
            key_count, warning = _release_key_or_skip_deleted(
                transaction,
                pt,
                res,
                actual_micro,
                book_to_byok=False,
            )
            if warning is not None:
                missing_key_releases.append(warning)
            if res["key_reserved_micro"] > 0 and key_count != 1:
                raise _SettleError("key release row-count != 1")
            if res["credit_reserved_micro"] > 0:
                credit_count = release_credit(
                    transaction,
                    pt,
                    res["workspace_id"],
                    res["credit_reserved_micro"],
                    actual_micro,
                    shard=res["credit_shard"],
                )
                if credit_count != 1:
                    raise _SettleError("credit release row-count != 1")

        if (
            snapshot_booked
            and app_markup_payout is not None
            and app_markup_payout.amount_microdollars > 0
        ):
            _apply_app_markup_payout_tx(
                transaction,
                pt,
                authorization_id=authorization_id,
                payout=app_markup_payout,
                now=reap_timestamp,
            )

        authorization.record_finalization(
            success=snapshot_booked,
            actual_microdollars=actual_micro,
            selected_usage_type=UsageType.CREDITS,
            generation=generation,
            outcome="reaped_snapshot" if snapshot_booked else "refunded",
        )
        marked = mark_gateway_authorization_settled(transaction, pt, authorization)
        if marked != 1:
            raise _ReapGuardLost("gateway authorization finalize guard lost")
        if generation is not None:
            insert_generation_record(
                transaction,
                pt,
                generation,
                terminal_at=reap_timestamp,
            )
            if operational_analytics_outbox is not None:
                operational_analytics_outbox.enqueue_activity_tx(transaction, generation)
        if (
            complete_reservation_retention(
                transaction,
                pt,
                reservation_id,
                terminal_at=reap_timestamp,
                outbox_available=guard_outbox,
            )
            != 1
        ):
            raise _ReapGuardLost("reservation retention guard lost")
        if (
            complete_gateway_authorization_retention(
                transaction,
                pt,
                authorization_id,
                terminal_at=reap_timestamp,
                outbox_available=guard_outbox,
            )
            != 1
        ):
            raise _ReapGuardLost("authorization retention guard lost")
        return _ReapOneResult(
            outcome=SettleOutcome.SETTLED,
            released_hold_micro=max(
                int(res["credit_reserved_micro"] or 0),
                int(res["key_reserved_micro"] or 0),
            ),
            started_marker=started_marker,
            snapshot_booked=snapshot_booked,
            out_of_cohort=out_of_cohort,
            missing_key_releases=tuple(missing_key_releases),
        )

    try:
        result = run_in_transaction_with_retry(
            database,
            txn,
            transaction_tag="tr_reap_snapshot_finalize",
        )
        _log_missing_key_releases(
            {"missing_key_releases": result.missing_key_releases}
        )
        return result
    except _ReapGuardLost as exc:
        log.warning(
            "gateway snapshot reap guard lost reservation_id=%s reason=%s",
            reservation_id,
            exc,
        )
        return _ReapOneResult(SettleOutcome.GUARD_LOST)
    except (_SettleError, TypeError, ValueError):
        log.exception("gateway snapshot reap finalize failed reservation_id=%s", reservation_id)
        return _ReapOneResult(SettleOutcome.ERROR)


def _reap_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise TypeError("reap timestamp is not a datetime")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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
    outbox_available: bool | None = None,
    authorization: GatewayAuthorization | None = None,
    auth_body_settled: str,
    generation_writes: list[tuple[str, str, str]] | None = None,
    generation: Generation | None = None,
    persist_generation_record: bool = False,
    operational_analytics_outbox: Any | None = None,
    user_model_payout: UserModelPayout | None = None,
    app_markup_payout: AppMarkupPayout | None = None,
    custom_model_markup_payout: CustomModelMarkupPayout | None = None,
    regional_hold_unknown: bool = False,
    settle_outbox_done: tuple[str, str] | None = None,
    settle_outbox_rewrite: tuple[str, str, str, int, str] | None = None,
) -> dict:
    """Full DML-only finalize for the typed path (codex 3e, Option B).

    ``settle_outbox_done=(authorization_id, intent_kind)`` also resolves that
    durable settle-outbox intent to ``done`` in the SAME transaction, when the
    activity row is durable in this commit and the outbox table exists. The
    result carries ``outbox_marked``: True (marked), False (row leased or
    already resolved -- the drain re-derives), or None (not attempted).

    ONE transaction reproduces legacy finalize_gateway_authorization's whole
    behavior so a crash can't leave counters charged but the authorization
    active: claim the reservation -> release the EXACT holds (key then credit)
    and book actual -> DML-mark the authorization settled. Typed request records
    keep their repair payload until durable analytics delivery is confirmed;
    rolling legacy records retain the old generic generation repair rows.

    `auth_body_settled` and `generation_writes` remain for rolling compatibility
    with an authorization created by the generic-table revision. Returns
    {outcome: settled|already_settled|not_found|error}.
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
    resolved_outbox_available = (
        _outbox_table_available(database, pt) if outbox_available is None else outbox_available
    )

    def txn(transaction: Any) -> dict:
        res = read_reservation(transaction, pt, reservation_id)
        if res is None:
            return {"outcome": SettleOutcome.NOT_FOUND}
        won = claim_reservation(
            transaction,
            pt,
            reservation_id,
            actual_micro=book_actual,
            settled_usage_type=settled_usage_type,
            terminal_at=now,
            defer_retention=True,
            outbox_available=resolved_outbox_available,
        )
        if not won:
            return {"outcome": SettleOutcome.ALREADY_SETTLED}

        if settle_outbox_rewrite is not None:
            rewrite_aid, rewrite_kind, lease_owner, rewrite_cost, rewrite_body = (
                settle_outbox_rewrite
            )
            rewritten = rewrite_frozen_settlement_tx(
                transaction,
                pt,
                authorization_id=rewrite_aid,
                intent_kind=rewrite_kind,
                lease_owner=lease_owner,
                actual_cost_micro=rewrite_cost,
                settle_body=rewrite_body,
                now=now,
            )
            if rewritten != 1:
                raise _SettleError("corrective settle-outbox rewrite lost its lease fence")

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
                transaction,
                pt,
                res["workspace_id"],
                res["credit_reserved_micro"],
                credit_actual,
                shard=res["credit_shard"],
            )
            if credit_count != 1:
                raise _SettleError("credit release row-count != 1")
        elif regional_hold_unknown and res.get("hold_usage_type") == "RegionalCredits":
            # The regional grant already reserved this money, but a historical
            # stale-CAS overwrite can leave no per-request Bigtable hold for the
            # reconciler to import. Charge against the reservation's atomic
            # claim without releasing the still-bounded lease grant. Closing
            # reconciliation later releases that grant as unused, leaving this
            # direct charge as the single durable booking.
            credit_actual = book_actual if settled_usage_type == "Credits" else 0
            credit_count = release_credit(
                transaction,
                pt,
                res["workspace_id"],
                0,
                credit_actual,
                shard=res["credit_shard"],
            )
            if credit_count != 1:
                raise _SettleError("regional fallback credit booking row-count != 1")

        if success and user_model_payout is not None and user_model_payout.amount_microdollars > 0:
            # Deliberately NOT wrapped in a swallow. The payout is two DML
            # statements in this same transaction; a failure between them
            # would otherwise commit the movement row without the balance
            # bump (or the reverse), and every later replay would read the
            # row as "already paid" — a permanent, silent underpayment. An
            # exception here aborts the whole finalize: transient errors are
            # retried by run_in_transaction_with_retry, and a deterministic
            # one (schema drift) fails the settle LOUDLY, which the outbox
            # repair/drain surfaces, instead of quietly not paying owners.
            _apply_user_model_payout_tx(
                transaction,
                pt,
                authorization_id=authorization_id,
                payout=user_model_payout,
                now=now,
            )
        if success and app_markup_payout is not None and app_markup_payout.amount_microdollars > 0:
            _apply_app_markup_payout_tx(
                transaction,
                pt,
                authorization_id=authorization_id,
                payout=app_markup_payout,
                now=now,
            )
        if (
            success
            and custom_model_markup_payout is not None
            and custom_model_markup_payout.amount_microdollars > 0
        ):
            _apply_custom_model_markup_payout_tx(
                transaction,
                pt,
                authorization_id=authorization_id,
                payout=custom_model_markup_payout,
                now=now,
            )

        marked = 0
        request_record_typed = False
        if authorization is not None:
            marked = mark_gateway_authorization_settled(
                transaction,
                pt,
                authorization,
            )
            request_record_typed = marked == 1
        if not request_record_typed:
            if success:
                for kind, entity_id, body_json in writes:
                    insert_entity_dml_at(
                        transaction,
                        pt,
                        kind,
                        entity_id,
                        body_json,
                        now,
                    )
            marked = update_entity_body_dml(
                transaction,
                pt,
                "gateway_authorization",
                authorization_id,
                auth_body_settled,
                now,
            )
            complete_reservation_retention(
                transaction,
                pt,
                reservation_id,
                terminal_at=now,
                outbox_available=resolved_outbox_available,
            )
        activity_durable = generation is None or operational_analytics_outbox is not None
        outbox_marked: bool | None = None
        if settle_outbox_done is not None and resolved_outbox_available and activity_durable:
            # Same commit as the charge: no post-finalize window in which a
            # crash leaves an already-charged authorization pending, and one
            # fewer multi-region round trip per settle. A leased row is left
            # to its drain worker (False), exactly as the standalone mark did.
            outbox_marked = mark_done_unleased_tx(
                transaction,
                pt,
                authorization_id=settle_outbox_done[0],
                intent_kind=settle_outbox_done[1],
            )
        if success and generation is not None:
            if persist_generation_record:
                insert_generation_record(
                    transaction,
                    pt,
                    generation,
                    terminal_at=now,
                )
            if operational_analytics_outbox is not None:
                operational_analytics_outbox.enqueue_activity_tx(
                    transaction,
                    generation,
                )
        if marked != 1:
            raise _SettleError("gateway_authorization update row-count != 1")
        return {
            "outcome": SettleOutcome.SETTLED,
            "missing_key_releases": missing_key_releases,
            "request_record_typed": request_record_typed,
            "activity_durable": activity_durable,
            "outbox_marked": outbox_marked,
        }

    try:
        attempts_box: list[int] = []
        result = run_in_transaction_with_retry(
            database,
            txn,
            attempts_out=attempts_box,
            transaction_tag="tr_finalize" if success else "tr_refund_finalize",
        )
        result["attempts"] = attempts_box[0] if attempts_box else 1
        _log_missing_key_releases(result)
        return result
    except _SettleError:
        return {"outcome": SettleOutcome.ERROR}


def _apply_user_model_payout_tx(
    transaction: Any,
    param_types: Any,
    *,
    authorization_id: str,
    payout: UserModelPayout,
    now: Any,
) -> None:
    """Credit one owner payout inside the customer finalize transaction.

    The reservation claim is the primary exactly-once guard; the movement PK
    (`account_id`, deterministic payout event id) is the secondary guard, so
    a duplicate movement skips the balance increment. Both statements live in
    the caller's transaction and either both commit or neither does — see the
    call site for why a failure here is allowed to abort the finalize.

    ``created_at`` is a client timestamp on purpose: tr_credit_movement was
    created WITHOUT ``allow_commit_timestamp`` (migrate_money_primitives.sh),
    and Spanner rejects PENDING_COMMIT_TIMESTAMP() into such a column
    (FAILED_PRECONDITION). Phase 1's credit_user_earnings writes it the same
    way; only tr_earnings_balance.updated_at is a commit-timestamp column.
    """
    pt = param_types
    movement_id = user_model_payout_event_id(authorization_id)
    inserted = transaction.execute_update(
        "INSERT OR IGNORE INTO tr_credit_movement "
        "(account_id, movement_id, kind, amount_microdollars, "
        "counterparty_account_id, custom_model_id, authorization_id, created_at) "
        "VALUES (@account_id, @movement_id, @kind, @amount, "
        "@counterparty, @custom_model_id, @authorization_id, @created_at)",
        params={
            "account_id": f"user:{payout.owner_user_id}",
            "movement_id": movement_id,
            "kind": "custom_model_payout",
            "amount": payout.amount_microdollars,
            "counterparty": payout.payer_workspace_id,
            "custom_model_id": payout.model_id,
            "authorization_id": authorization_id,
            "created_at": now,
        },
        param_types={
            "account_id": pt.STRING,
            "movement_id": pt.STRING,
            "kind": pt.STRING,
            "amount": pt.INT64,
            "counterparty": pt.STRING,
            "custom_model_id": pt.STRING,
            "authorization_id": pt.STRING,
            "created_at": pt.TIMESTAMP,
        },
    )
    if inserted == 0:
        return
    updated = transaction.execute_update(
        "UPDATE tr_earnings_balance "
        "SET total_earned = total_earned + @amount, "
        "updated_at = PENDING_COMMIT_TIMESTAMP() "
        "WHERE user_id=@user_id AND shard=0",
        params={"amount": payout.amount_microdollars, "user_id": payout.owner_user_id},
        param_types={"amount": pt.INT64, "user_id": pt.STRING},
    )
    if updated == 0:
        transaction.execute_update(
            "INSERT INTO tr_earnings_balance "
            "(user_id, shard, total_earned, total_transferred, updated_at) "
            "VALUES (@user_id, 0, @amount, 0, PENDING_COMMIT_TIMESTAMP())",
            params={"user_id": payout.owner_user_id, "amount": payout.amount_microdollars},
            param_types={"user_id": pt.STRING, "amount": pt.INT64},
        )


def _apply_app_markup_payout_tx(
    transaction: Any,
    param_types: Any,
    *,
    authorization_id: str,
    payout: AppMarkupPayout,
    now: Any,
) -> None:
    """Mirror the proven custom-model payout transaction for app markup."""
    pt = param_types
    inserted = transaction.execute_update(
        "INSERT OR IGNORE INTO tr_credit_movement "
        "(account_id, movement_id, kind, amount_microdollars, "
        "counterparty_account_id, custom_model_id, authorization_id, created_at) "
        "VALUES (@account_id, @movement_id, @kind, @amount, "
        "@counterparty, @app_id, @authorization_id, @created_at)",
        params={
            "account_id": f"user:{payout.owner_user_id}",
            "movement_id": app_markup_payout_event_id(authorization_id),
            "kind": "app_markup_payout",
            "amount": payout.amount_microdollars,
            "counterparty": payout.payer_workspace_id,
            "app_id": payout.app_id,
            "authorization_id": authorization_id,
            "created_at": now,
        },
        param_types={
            "account_id": pt.STRING,
            "movement_id": pt.STRING,
            "kind": pt.STRING,
            "amount": pt.INT64,
            "counterparty": pt.STRING,
            "app_id": pt.STRING,
            "authorization_id": pt.STRING,
            "created_at": pt.TIMESTAMP,
        },
    )
    if inserted == 0:
        return
    updated = transaction.execute_update(
        "UPDATE tr_earnings_balance "
        "SET total_earned = total_earned + @amount, "
        "updated_at = PENDING_COMMIT_TIMESTAMP() "
        "WHERE user_id=@user_id AND shard=0",
        params={"amount": payout.amount_microdollars, "user_id": payout.owner_user_id},
        param_types={"amount": pt.INT64, "user_id": pt.STRING},
    )
    if updated == 0:
        transaction.execute_update(
            "INSERT INTO tr_earnings_balance "
            "(user_id, shard, total_earned, total_transferred, updated_at) "
            "VALUES (@user_id, 0, @amount, 0, PENDING_COMMIT_TIMESTAMP())",
            params={"user_id": payout.owner_user_id, "amount": payout.amount_microdollars},
            param_types={"user_id": pt.STRING, "amount": pt.INT64},
        )


def _apply_custom_model_markup_payout_tx(
    transaction: Any,
    param_types: Any,
    *,
    authorization_id: str,
    payout: CustomModelMarkupPayout,
    now: Any,
) -> None:
    """Credit prompt-wrapper markup in the customer finalize transaction."""
    pt = param_types
    inserted = transaction.execute_update(
        "INSERT OR IGNORE INTO tr_credit_movement "
        "(account_id, movement_id, kind, amount_microdollars, "
        "counterparty_account_id, custom_model_id, authorization_id, created_at) "
        "VALUES (@account_id, @movement_id, @kind, @amount, "
        "@counterparty, @custom_model_id, @authorization_id, @created_at)",
        params={
            "account_id": f"user:{payout.owner_user_id}",
            "movement_id": custom_model_markup_payout_event_id(authorization_id),
            "kind": "custom_model_markup_payout",
            "amount": payout.amount_microdollars,
            "counterparty": payout.payer_workspace_id,
            "custom_model_id": payout.model_id,
            "authorization_id": authorization_id,
            "created_at": now,
        },
        param_types={
            "account_id": pt.STRING,
            "movement_id": pt.STRING,
            "kind": pt.STRING,
            "amount": pt.INT64,
            "counterparty": pt.STRING,
            "custom_model_id": pt.STRING,
            "authorization_id": pt.STRING,
            "created_at": pt.TIMESTAMP,
        },
    )
    if inserted == 0:
        return
    updated = transaction.execute_update(
        "UPDATE tr_earnings_balance "
        "SET total_earned = total_earned + @amount, "
        "updated_at = PENDING_COMMIT_TIMESTAMP() "
        "WHERE user_id=@user_id AND shard=0",
        params={"amount": payout.amount_microdollars, "user_id": payout.owner_user_id},
        param_types={"amount": pt.INT64, "user_id": pt.STRING},
    )
    if updated == 0:
        transaction.execute_update(
            "INSERT INTO tr_earnings_balance "
            "(user_id, shard, total_earned, total_transferred, updated_at) "
            "VALUES (@user_id, 0, @amount, 0, PENDING_COMMIT_TIMESTAMP())",
            params={"user_id": payout.owner_user_id, "amount": payout.amount_microdollars},
            param_types={"user_id": pt.STRING, "amount": pt.INT64},
        )
