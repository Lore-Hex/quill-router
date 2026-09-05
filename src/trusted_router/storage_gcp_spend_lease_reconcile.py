"""One-pass spend-lease recovery and close coordinator.

Spanner owns work and global escrow; the regional Bigtable row owns the local
state machine.  Every cross-store step is replayable, and the candidate phase
handoff is the only fence against an in-flight mint.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from trusted_router.spend_lease_ledger import SpendLeaseLedger
from trusted_router.spend_lease_state import (
    AbsenceObservation,
    AllocationState,
    AuthorizationDurability,
    AuthorizationObservation,
    BindingAbsenceProof,
    BindingState,
    BindingTuple,
    BoundProof,
    ClaimProof,
    ConflictingBound,
    FinalizationOutcome,
    MonetaryMismatchProof,
    RecoveryProof,
    RowBindingMismatch,
    SpendLease,
    SpendLeaseConflictError,
    SpendLeaseMonetaryMismatch,
    SpendLeaseState,
)
from trusted_router.storage_gcp_counter_dml import release_credit
from trusted_router.storage_gcp_io import run_in_transaction_with_retry
from trusted_router.storage_gcp_spend_lease import (
    OpenRow,
    complete_candidate,
    dead_rows,
    defer_open_row,
    delete_open_row,
    due_candidates,
    due_open,
    lag_inputs,
    mark_global_closed,
    mark_local_closed,
    read_open_row,
    read_registration,
    register_claim,
    requeue_dead,
    retained_done_candidates,
    set_close_eligible_once,
    take_recovery_ownership,
)
from trusted_router.storage_gcp_spend_lease_authorize import (
    SPEND_LEASE_KIND,
    _close_global_lease,
    _decrement_fence,
    _mark_closing,
    _read_fence,
    _unmark_incumbent,
)

log = logging.getLogger(__name__)

_LOCK_KIND = "spend_lease_reconciler_lock"
_LOCK_ID = "singleton"
_RETENTION = timedelta(days=30)
_LAG_LIMIT = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class SpendLeaseReconcilerLock:
    owner: str | None
    fencing_token: int
    expires_at: str
    updated_at: str
    previous_owner: str | None = None
    previous_fencing_token: int | None = None

    @property
    def expires_datetime(self) -> datetime:
        return datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))


def acquire_spend_lease_reconciler_lock(
    store: Any,
    *,
    owner: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> SpendLeaseReconcilerLock | None:
    """Acquire the singleton worker lease with an expiry and fencing token."""

    now = datetime.now(UTC) if now is None else now
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if not owner or len(owner) > 128:
        raise ValueError("owner must contain between 1 and 128 characters")
    if not 1 <= ttl_seconds <= 600:
        raise ValueError("ttl_seconds must be between 1 and 600")

    def txn(transaction: Any) -> SpendLeaseReconcilerLock | None:
        current = store._read_entity_tx(
            transaction, _LOCK_KIND, _LOCK_ID, SpendLeaseReconcilerLock
        )
        if current is not None and current.owner is not None and current.expires_datetime > now:
            return None
        lock = SpendLeaseReconcilerLock(
            owner=owner,
            fencing_token=1 if current is None else current.fencing_token + 1,
            expires_at=_iso(now + timedelta(seconds=ttl_seconds)),
            updated_at=_iso(now),
        )
        _upsert_lock(store, transaction, lock)
        if current is not None and current.owner is not None:
            return dataclasses.replace(
                lock,
                previous_owner=current.owner,
                previous_fencing_token=current.fencing_token,
            )
        return lock

    return cast(SpendLeaseReconcilerLock | None, store._run_in_transaction(txn))


def release_spend_lease_reconciler_lock(
    store: Any,
    *,
    owner: str,
    fencing_token: int,
    now: datetime | None = None,
) -> bool:
    """Release only the lock generation owned by this worker."""

    now = datetime.now(UTC) if now is None else now
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    def txn(transaction: Any) -> bool:
        current = store._read_entity_tx(
            transaction, _LOCK_KIND, _LOCK_ID, SpendLeaseReconcilerLock
        )
        if current is None or current.owner != owner or current.fencing_token != fencing_token:
            return False
        _upsert_lock(
            store,
            transaction,
            dataclasses.replace(
                current,
                owner=None,
                expires_at=_iso(now),
                updated_at=_iso(now),
            ),
        )
        return True

    return bool(store._run_in_transaction(txn))


def reconcile_spend_leases(
    store: Any,
    *,
    limit: int = 25,
    max_attempts: int = 12,
    now: datetime | None = None,
) -> dict[str, int | float]:
    """Run one ordered candidate pass followed by one ordered open pass."""

    ledger: SpendLeaseLedger | None = store._spend_lease_ledger
    if ledger is None:
        raise RuntimeError("spend lease ledger is disabled")
    now = datetime.now(UTC) if now is None else now
    bounded_limit = max(1, min(int(limit), 1000))
    result: dict[str, int | float] = {
        "candidates": 0,
        "open": 0,
        "recovered": 0,
        "bound": 0,
        "closed": 0,
        "deleted": 0,
        "deferred": 0,
        "errors": 0,
        "dead": 0,
    }

    with store._database.snapshot() as snapshot:
        candidates = due_candidates(snapshot, store._param_types, bounded_limit)
    for row in candidates:
        result["candidates"] += 1
        try:
            if _recover_candidate(store, ledger, row, now=now):
                result["recovered"] += 1
        except Exception as exc:
            result["errors"] += 1
            if _record_failure(store, row.lease_id, exc, max_attempts=max_attempts, now=now):
                result["dead"] += 1
            log.exception("spend_lease.reconcile_candidate_failed lease_id=%s", row.lease_id)

    with store._database.snapshot() as snapshot:
        open_rows = due_open(snapshot, store._param_types, bounded_limit)
    for row in open_rows:
        result["open"] += 1
        try:
            outcome = _reconcile_open(store, ledger, row, now=now)
            result[outcome] = int(result.get(outcome, 0)) + 1
        except _Contradiction as exc:
            result["errors"] += 1
            result["dead"] += 1
            _mark_contradiction(store, row.lease_id, str(exc), now=now)
            log.error(
                "spend_lease.reconcile_contradiction lease_id=%s error=%s",
                row.lease_id,
                exc,
            )
        except Exception as exc:
            result["errors"] += 1
            if _record_failure(store, row.lease_id, exc, max_attempts=max_attempts, now=now):
                result["dead"] += 1
            log.exception("spend_lease.reconcile_open_failed lease_id=%s", row.lease_id)

    with store._database.snapshot() as snapshot:
        retained = retained_done_candidates(
            snapshot, store._param_types, now - _RETENTION, bounded_limit
        )
    for row in retained:
        changed = run_in_transaction_with_retry(
            store._database,
            lambda transaction, item=row: delete_open_row(
                transaction, store._param_types, item.lease_id, phase="done"
            ),
            transaction_tag="tr_spend_lease_delete_done",
        )
        result["deleted"] += int(changed)

    _log_lag(store, now=now, result=result)
    return result


def requeue_dead_spend_leases(
    store: Any,
    *,
    lease_ids: tuple[str, ...] = (),
    limit: int = 1000,
    now: datetime | None = None,
) -> int:
    now = datetime.now(UTC) if now is None else now
    if lease_ids:
        selected = lease_ids
    else:
        with store._database.snapshot() as snapshot:
            selected = tuple(
                row.lease_id for row in dead_rows(snapshot, store._param_types, limit)
            )
    count = 0
    for lease_id in selected:
        count += int(
            run_in_transaction_with_retry(
                store._database,
                lambda transaction, item=lease_id: requeue_dead(
                    transaction, store._param_types, item, now
                ),
                transaction_tag="tr_spend_lease_requeue",
            )
        )
    return count


class _Contradiction(RuntimeError):
    pass


def _recover_candidate(
    store: Any,
    ledger: SpendLeaseLedger,
    candidate: OpenRow,
    *,
    now: datetime,
) -> bool:
    if candidate.phase == "candidate":
        owned = run_in_transaction_with_retry(
            store._database,
            lambda transaction: take_recovery_ownership(
                transaction, store._param_types, candidate.lease_id
            ),
            transaction_tag="tr_spend_lease_recovery_ownership",
        )
        # The ownership transaction deliberately ends immediately after 4a.
        if owned == 0:
            return False

    row = _strong_open_row(store, candidate.lease_id)
    if row is None or row.phase != "recovering" or row.recovering_at is None:
        return False
    proof = RecoveryProof(_aware(row.recovering_at), row.creating_authorization_id)
    absence = BindingAbsenceProof(
        row.idempotency_scope,
        row.creating_authorization_id,
        AbsenceObservation.ABSENT_ROW,
    )
    for _ in range(16):
        local = ledger.get(row.lease_id, region=row.region)
        if local is None:
            ledger.initialize(_candidate_from_row(row), region=row.region)
            local = ledger.get(row.lease_id, region=row.region)
            if local is None:
                raise RuntimeError("candidate initialization was not durable")
        creating = next(
            (
                allocation
                for allocation in local.allocations
                if allocation.authorization_id == row.creating_authorization_id
            ),
            None,
        )
        if creating is not None and creating.is_open:
            ledger.compensate(
                row.lease_id,
                region=row.region,
                idempotency_scope=creating.idempotency_scope,
                expected_provisional_id=row.creating_authorization_id,
                claim=proof,
                absence=absence,
            )
        try:
            ledger.tombstone_unminted(row.lease_id, region=row.region, proof=proof)
        except SpendLeaseConflictError:
            continue
        break
    else:
        raise RuntimeError("candidate recovery did not converge after CAS losses")

    completed = run_in_transaction_with_retry(
        store._database,
        lambda transaction: complete_candidate(
            transaction, store._param_types, row.lease_id
        ),
        transaction_tag="tr_spend_lease_recovery_complete",
    )
    if completed != 1:
        raise RuntimeError("candidate completion lost its recovering guard")
    return True


def _reconcile_open(
    store: Any,
    ledger: SpendLeaseLedger,
    row: OpenRow,
    *,
    now: datetime,
) -> str:
    local = ledger.get(row.lease_id, region=row.region)
    if local is None:
        raise RuntimeError("open spend lease has no local row")
    global_body = _read_global_body(store, row.lease_id)
    if global_body is None:
        raise RuntimeError("open spend lease has no global record")
    _validate_global_identity(row, global_body)

    if row.local_closed_at is not None:
        if now < _aware(row.local_closed_at) + _RETENTION:
            return _defer(store, row, now=now)
        fence = _strong_fence(store, row)
        if fence is not None and fence.get("lease_id") == row.lease_id:
            return _defer(store, row, now=now)
        ledger.delete(row.lease_id, region=row.region)
        _delete_work_row(store, row)
        return "deleted"

    bound_count = _bind_last_resort(ledger, row, local, global_body)
    if bound_count:
        local = ledger.get(row.lease_id, region=row.region)
        if local is None:
            raise RuntimeError("bound spend lease disappeared")
    local = _mirror_allocations(store, ledger, row, local, now=now)

    cutoff = _aware(row.expires_at) + timedelta(seconds=row.skew_seconds)
    if now >= cutoff and local.state == SpendLeaseState.ACTIVE:
        ledger.begin_drain(row.lease_id, region=row.region, now=now)
        local = ledger.get(row.lease_id, region=row.region)
        if local is None:
            raise RuntimeError("draining spend lease disappeared")

    eligible = local.state in {SpendLeaseState.DRAINING, SpendLeaseState.TOMBSTONED} and not (
        local.open_allocations
    )
    if row.close_eligible_since is not None and not eligible and row.global_closed_at is None:
        raise _Contradiction("close eligibility was followed by an open allocation or state")
    if not eligible and row.global_closed_at is None:
        return _defer(store, row, now=now)
    if now < cutoff:
        return _defer(store, row, now=now)
    if row.close_eligible_since is None:
        run_in_transaction_with_retry(
            store._database,
            lambda transaction: set_close_eligible_once(
                transaction, store._param_types, row.lease_id, now
            ),
            transaction_tag="tr_spend_lease_close_eligible",
        )

    if row.global_closed_at is None:
        _close_global(store, row, local, now=now)
    else:
        frozen_version = global_body.get("frozen_local_version")
        expected = local.version - int(local.state == SpendLeaseState.CLOSED)
        if frozen_version != expected:
            raise _Contradiction("global frozen_local_version disagrees with local snapshot")

    if local.state != SpendLeaseState.CLOSED:
        ledger.close(row.lease_id, region=row.region, now=now)
    marked = run_in_transaction_with_retry(
        store._database,
        lambda transaction: mark_local_closed(
            transaction, store._param_types, row.lease_id, now
        ),
        transaction_tag="tr_spend_lease_local_closed",
    )
    if marked != 1:
        refreshed = _strong_open_row(store, row.lease_id)
        if refreshed is None or refreshed.local_closed_at is None:
            raise RuntimeError("local close marker lost its guard")
    return "closed" if not bound_count else "bound"


def _bind_last_resort(
    ledger: SpendLeaseLedger,
    row: OpenRow,
    local: SpendLease,
    global_body: dict[str, Any],
) -> int:
    if global_body.get("state") not in {"ACTIVE", "DRAINING", "TOMBSTONED", "CLOSED"}:
        return 0
    bound = 0
    for allocation in local.allocations:
        if allocation.binding_state != BindingState.PROVISIONAL:
            continue
        if allocation.authorization_id != row.creating_authorization_id:
            continue
        proof = BoundProof(
            allocation.idempotency_scope,
            allocation.authorization_id,
            row.lease_id,
            row.gen,
            allocation.allocated_micro,
        )
        ledger.bind(
            row.lease_id,
            region=row.region,
            expected_provisional_id=allocation.authorization_id,
            proof=proof,
        )
        bound += 1
    return bound


def _mirror_allocations(
    store: Any,
    ledger: SpendLeaseLedger,
    row: OpenRow,
    local: SpendLease,
    *,
    now: datetime,
) -> SpendLease:
    for allocation in local.allocations:
        if allocation.state != AllocationState.RESERVED:
            continue
        authorization = store.get_gateway_authorization(allocation.authorization_id)
        if authorization is None:
            if allocation.binding_state == BindingState.COMMITTED and now >= allocation.abandon_after:
                # Retention makes disappearance after the abandonment horizon a
                # durable terminal observation for a committed allocation.
                from trusted_router.spend_lease_state import CommittedRowAbsentProof

                ledger.lost(
                    row.lease_id,
                    region=row.region,
                    proof=CommittedRowAbsentProof(
                        allocation.idempotency_scope, allocation.authorization_id
                    ),
                )
            elif now >= allocation.abandon_after:
                _abandon_provisional(store, ledger, row, allocation)
            continue
        observed_tuple = (
            authorization.spend_lease_id,
            authorization.spend_lease_gen,
            authorization.spend_lease_allocated_micro,
        )
        expected_tuple = (row.lease_id, row.gen, allocation.allocated_micro)
        observed_binding = (
            BindingTuple(
                str(observed_tuple[0]),
                int(observed_tuple[1]),
                int(observed_tuple[2]),
            )
            if all(value is not None for value in observed_tuple)
            else None
        )
        if observed_tuple != expected_tuple:
            if allocation.binding_state == BindingState.PROVISIONAL and observed_binding is None:
                if now >= allocation.abandon_after:
                    _abandon_provisional(store, ledger, row, allocation, authorization)
                continue
            assert observed_binding is not None
            proof = (
                ConflictingBound(allocation.authorization_id, observed_binding)
                if allocation.binding_state == BindingState.PROVISIONAL
                else RowBindingMismatch(allocation.authorization_id, observed_binding)
            )
            ledger.quarantine(
                row.lease_id,
                region=row.region,
                idempotency_scope=allocation.idempotency_scope,
                proof=proof,
            )
            log.error(
                "spend_lease.reconcile_quarantine lease_id=%s authorization_id=%s",
                row.lease_id,
                allocation.authorization_id,
            )
            continue
        if allocation.binding_state == BindingState.PROVISIONAL:
            ledger.bind(
                row.lease_id,
                region=row.region,
                expected_provisional_id=allocation.authorization_id,
                proof=BoundProof(
                    allocation.idempotency_scope,
                    allocation.authorization_id,
                    row.lease_id,
                    row.gen,
                    allocation.allocated_micro,
                ),
            )
        outcome = (
            None
            if authorization.finalization_outcome is None
            else FinalizationOutcome(authorization.finalization_outcome)
        )
        observation = AuthorizationObservation(
            idempotency_scope=allocation.idempotency_scope,
            authorization_id=allocation.authorization_id,
            request_fingerprint=allocation.request_fingerprint,
            lease_id=row.lease_id,
            gen=row.gen,
            allocated_micro=allocation.allocated_micro,
            key_hash=row.key_hash,
            workspace_id=row.workspace_id,
            durability=(
                AuthorizationDurability.TERMINAL
                if authorization.settled
                else AuthorizationDurability.OPEN
            ),
            finalization_outcome=outcome,
            finalized_cost_microdollars=(
                authorization.finalized_cost_microdollars
                if outcome is not None and outcome.charged
                else None
            ),
        )
        try:
            ledger.mirror(
                row.lease_id,
                region=row.region,
                observation=observation,
            )
        except SpendLeaseMonetaryMismatch as exc:
            ledger.quarantine(
                row.lease_id,
                region=row.region,
                idempotency_scope=allocation.idempotency_scope,
                proof=MonetaryMismatchProof(
                    exc.finalized_cost_microdollars,
                    exc.allocated_micro,
                ),
            )
            log.error(
                "spend_lease.historical_overcharge",
                extra={
                    "authorization_id": allocation.authorization_id,
                    "spend_lease_id": row.lease_id,
                    "spend_lease_gen": row.gen,
                    "spend_lease_allocated_micro": allocation.allocated_micro,
                    "finalized_cost_microdollars": exc.finalized_cost_microdollars,
                },
            )
    refreshed = ledger.get(row.lease_id, region=row.region)
    if refreshed is None:
        raise RuntimeError("spend lease disappeared during allocation mirror")
    return refreshed


def _abandon_provisional(
    store: Any,
    ledger: SpendLeaseLedger,
    row: OpenRow,
    allocation: Any,
    authorization: Any | None = None,
) -> None:
    def claim_txn(transaction: Any) -> ClaimProof:
        if register_claim(
            transaction,
            store._param_types,
            allocation.idempotency_scope,
            allocation.authorization_id,
        ) == 1:
            return ClaimProof(allocation.idempotency_scope, allocation.authorization_id)
        registration = read_registration(
            transaction, store._param_types, allocation.idempotency_scope
        )
        if (
            registration is None
            or registration.kind != "CLAIM"
            or registration.provisional_id != allocation.authorization_id
        ):
            observed = None if registration is None else registration.lease
            raise _Contradiction(f"provisional allocation has conflicting registration {observed}")
        return ClaimProof(allocation.idempotency_scope, allocation.authorization_id)

    claim = run_in_transaction_with_retry(
        store._database, claim_txn, transaction_tag="tr_spend_lease_abandon_claim"
    )
    ledger.abandon(
        row.lease_id,
        region=row.region,
        idempotency_scope=allocation.idempotency_scope,
        expected_provisional_id=allocation.authorization_id,
        claim=claim,
        absence=BindingAbsenceProof(
            allocation.idempotency_scope,
            allocation.authorization_id,
            (
                AbsenceObservation.ABSENT_ROW
                if authorization is None
                else AbsenceObservation.NON_BINDING_ROW
            ),
            observed_authorization_id=(
                None if authorization is None else authorization.id
            ),
        ),
        now=datetime.now(UTC),
    )


def _close_global(
    store: Any,
    row: OpenRow,
    local: SpendLease,
    *,
    now: datetime,
) -> None:
    if (
        local.state not in {SpendLeaseState.DRAINING, SpendLeaseState.TOMBSTONED}
        or local.open_allocations
        or now < _aware(row.expires_at) + timedelta(seconds=row.skew_seconds)
    ):
        raise _Contradiction("global close requires an expired frozen local lease")
    fence_id = store._spend_lease_pair_id(row.key_hash, row.boot_kid)

    def txn(transaction: Any) -> None:
        # A successor can claim the predecessor slot after the outer snapshot.
        # Freeze, inspect ownership, and release escrow in one serializable txn.
        records = list(transaction.execute_sql(
            "SELECT body FROM tr_entities WHERE kind=@kind AND id=@id",
            params={"kind": SPEND_LEASE_KIND, "id": row.lease_id},
            param_types={"kind": store._param_types.STRING, "id": store._param_types.STRING},
        ))
        if not records:
            raise _Contradiction("global spend lease disappeared before close")
        body = dict(json.loads(records[0][0]))
        _validate_global_identity(row, body)
        work = read_open_row(transaction, store._param_types, row.lease_id)
        if work is None:
            raise _Contradiction("spend lease close work row disappeared")
        if work.global_closed_at is not None:
            if body.get("state") != "CLOSED" or body.get("frozen_local_version") != local.version:
                raise _Contradiction("global close replay disagrees with frozen local snapshot")
            return
        if body.get("state") == "ACTIVE":
            _require_one(int(transaction.execute_update(
                "UPDATE tr_entities SET body=TO_JSON_STRING(JSON_SET(PARSE_JSON(body), "
                "'$.state', @state)) WHERE kind=@kind AND id=@id "
                "AND JSON_VALUE(body, '$.state')='ACTIVE'",
                params={"kind": SPEND_LEASE_KIND, "id": row.lease_id, "state": local.state.value.upper()},
                param_types={"kind": store._param_types.STRING, "id": store._param_types.STRING,
                             "state": store._param_types.STRING},
            )), "spend lease freeze guard")
        elif body.get("state") not in {"DRAINING", "TOMBSTONED"}:
            raise _Contradiction("global spend lease is not closeable")
        credit_shard = int(body["credit_shard"])
        holds_slot = bool(body.get("holds_predecessor_slot"))
        closed_body = dict(body)
        closed_body.update({
            "state": "CLOSED",
            "frozen_local_version": local.version,
            "closing_at": body.get("closing_at") or now.isoformat(),
        })
        _require_one(
            release_credit(
                transaction,
                store._param_types,
                row.workspace_id,
                row.cap_micro,
                0,
                shard=credit_shard,
            ),
            "spend lease escrow release",
        )
        if holds_slot:
            _require_one(
                _unmark_incumbent(
                    transaction,
                    store._param_types,
                    row.lease_id,
                    frozen_only=True,
                ),
                "predecessor owner clear",
            )
            _require_one(
                _decrement_fence(transaction, store._param_types, fence_id),
                "predecessor fence decrement",
            )
            closed_body["holds_predecessor_slot"] = False
        else:
            _require_one(
                _mark_closing(transaction, store._param_types, row.lease_id, now),
                "spend lease closing guard",
            )
        _require_one(
            mark_global_closed(transaction, store._param_types, row.lease_id, now),
            "global close work marker",
        )
        _require_one(
            _close_global_lease(
                transaction,
                store._param_types,
                row.lease_id,
                json.dumps(closed_body, separators=(",", ":"), sort_keys=True),
            ),
            "global spend lease close",
        )

    run_in_transaction_with_retry(
        store._database, txn, transaction_tag="tr_spend_lease_global_close"
    )


def _record_failure(
    store: Any,
    lease_id: str,
    error: Exception,
    *,
    max_attempts: int,
    now: datetime,
) -> bool:
    row = _strong_open_row(store, lease_id)
    if row is None or row.phase == "done":
        return False
    next_attempts = row.attempts + 1
    dead = next_attempts >= max_attempts
    next_at = now + timedelta(seconds=_backoff_seconds(next_attempts))
    changed = run_in_transaction_with_retry(
        store._database,
        lambda transaction: defer_open_row(
            transaction,
            store._param_types,
            row,
            next_attempt_at=next_at,
            error=str(error),
            increment_attempts=True,
            dead=dead,
        ),
        transaction_tag="tr_spend_lease_retry",
    )
    if changed != 1:
        return False
    if dead:
        log.error("spend_lease.reconcile_dead lease_id=%s count=1", lease_id)
    return dead


def _mark_contradiction(store: Any, lease_id: str, error: str, *, now: datetime) -> None:
    row = _strong_open_row(store, lease_id)
    if row is None:
        return
    run_in_transaction_with_retry(
        store._database,
        lambda transaction: defer_open_row(
            transaction,
            store._param_types,
            row,
            next_attempt_at=now,
            error=error,
            increment_attempts=False,
            dead=True,
        ),
        transaction_tag="tr_spend_lease_contradiction",
    )


def _defer(store: Any, row: OpenRow, *, now: datetime) -> str:
    current = _strong_open_row(store, row.lease_id) or row
    next_at = now + timedelta(seconds=_backoff_seconds(max(current.attempts, 1)))
    run_in_transaction_with_retry(
        store._database,
        lambda transaction: defer_open_row(
            transaction,
            store._param_types,
            current,
            next_attempt_at=next_at,
            error=current.last_error,
            increment_attempts=False,
        ),
        transaction_tag="tr_spend_lease_not_closeable",
    )
    return "deferred"


def _log_lag(store: Any, *, now: datetime, result: dict[str, int | float]) -> None:
    with store._database.snapshot() as snapshot:
        inputs = lag_inputs(snapshot, store._param_types, now)
    eligibility = _lag_seconds(now, inputs.close_eligible_since)
    open_age = _lag_seconds(now, inputs.expired_open_created_at)
    result["eligibility_lag_seconds"] = eligibility
    result["open_age_lag_seconds"] = open_age
    result["dead"] = inputs.dead_rows
    log.info(
        "spend_lease.reconcile_lag eligibility_lag_seconds=%.3f "
        "open_age_lag_seconds=%.3f dead_rows=%d",
        eligibility,
        open_age,
        inputs.dead_rows,
    )
    if eligibility > _LAG_LIMIT.total_seconds() or open_age > _LAG_LIMIT.total_seconds():
        log.error(
            "spend_lease.reconcile_lag_exceeded eligibility_lag_seconds=%.3f "
            "open_age_lag_seconds=%.3f",
            eligibility,
            open_age,
        )
    if inputs.dead_rows:
        log.error("spend_lease.reconcile_dead_count count=%d", inputs.dead_rows)


def _strong_open_row(store: Any, lease_id: str) -> OpenRow | None:
    with store._database.snapshot() as snapshot:
        return read_open_row(snapshot, store._param_types, lease_id)


def _read_global_body(store: Any, lease_id: str) -> dict[str, Any] | None:
    with store._database.snapshot() as snapshot:
        rows = list(
            snapshot.execute_sql(
                "SELECT body FROM tr_entities WHERE kind=@kind AND id=@id",
                params={"kind": SPEND_LEASE_KIND, "id": lease_id},
                param_types={
                    "kind": store._param_types.STRING,
                    "id": store._param_types.STRING,
                },
            )
        )
    return None if not rows else dict(json.loads(rows[0][0]))


def _strong_fence(store: Any, row: OpenRow) -> dict[str, Any] | None:
    with store._database.snapshot(multi_use=True) as snapshot:
        return _read_fence(
            snapshot,
            store._param_types,
            store._spend_lease_pair_id(row.key_hash, row.boot_kid),
            row.lease_id,
        )


def _delete_work_row(store: Any, row: OpenRow) -> None:
    changed = run_in_transaction_with_retry(
        store._database,
        lambda transaction: delete_open_row(
            transaction, store._param_types, row.lease_id, phase="open"
        ),
        transaction_tag="tr_spend_lease_delete_open",
    )
    if changed != 1:
        raise RuntimeError("retained spend lease work row delete lost its guard")


def _candidate_from_row(row: OpenRow) -> SpendLease:
    return SpendLease(
        lease_id=row.lease_id,
        gen=row.gen,
        key_hash=row.key_hash,
        boot_kid=row.boot_kid,
        workspace_id=row.workspace_id,
        creating_authorization_id=row.creating_authorization_id,
        cap_micro=row.cap_micro,
        expires_at=_aware(row.expires_at),
        skew=timedelta(seconds=row.skew_seconds),
        version=0,
    )


def _validate_global_identity(row: OpenRow, body: dict[str, Any]) -> None:
    expected = (row.lease_id, row.gen, row.key_hash, row.boot_kid, row.workspace_id, row.region)
    observed = tuple(body.get(key) for key in (
        "lease_id", "gen", "key_hash", "boot_kid", "workspace_id", "region"
    ))
    if observed != expected:
        raise _Contradiction("global spend lease identity changed")


def _upsert_lock(store: Any, transaction: Any, lock: SpendLeaseReconcilerLock) -> None:
    body = json.dumps(dataclasses.asdict(lock), separators=(",", ":"), sort_keys=True)
    params = {"kind": _LOCK_KIND, "id": _LOCK_ID, "body": body}
    types = {
        "kind": store._param_types.STRING,
        "id": store._param_types.STRING,
        "body": store._param_types.STRING,
    }
    changed = transaction.execute_update(
        "UPDATE tr_entities SET body=@body, updated_at=PENDING_COMMIT_TIMESTAMP() "
        "WHERE kind=@kind AND id=@id",
        params=params,
        param_types=types,
    )
    if changed == 0:
        transaction.execute_update(
            "INSERT INTO tr_entities (kind, id, body, updated_at) "
            "VALUES (@kind, @id, @body, PENDING_COMMIT_TIMESTAMP())",
            params=params,
            param_types=types,
        )


def _require_one(count: int, operation: str) -> None:
    if count != 1:
        raise RuntimeError(f"{operation} modified {count} rows")


def _backoff_seconds(attempts: int) -> int:
    return int(min(60 * 60, 2 ** max(attempts - 1, 0)))


def _lag_seconds(now: datetime, value: Any | None) -> float:
    return 0.0 if value is None else max(0.0, (now - _aware(value)).total_seconds())


def _aware(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
