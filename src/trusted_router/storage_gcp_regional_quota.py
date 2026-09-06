"""Global Spanner escrow and reconciliation for regional quota leases."""

from __future__ import annotations

import dataclasses
import json
import logging
import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from google.api_core.exceptions import AlreadyExists

from trusted_router.regional_quota_ledger import settled_key_totals
from trusted_router.services.regional_quota_leases import (
    RegionalQuotaLease,
    bounded_lease_grant_microdollars,
)
from trusted_router.spend_windows import utcnow, window_floors
from trusted_router.storage_gcp_codec import json_body
from trusted_router.storage_gcp_counter_dml import (
    delete_entity_dml,
    insert_entity_dml_at,
    insert_reservation,
    key_limit_exists,
    read_reservation_by_idempotency,
    release_credit,
    release_key,
)
from trusted_router.storage_gcp_io import run_in_transaction_with_retry
from trusted_router.storage_gcp_request_records import insert_gateway_authorization
from trusted_router.storage_models import GatewayAuthorization

log = logging.getLogger(__name__)

_LEASE_KIND = "regional_quota_lease"
_OPEN_LEASE_KIND = "regional_quota_lease_open"
_FENCE_KIND = "regional_quota_fence"
_RECONCILER_LOCK_KIND = "regional_quota_reconciler_lock"
_RECONCILER_LOCK_ID = "singleton"


@dataclass
class GlobalRegionalQuotaLease:
    lease_id: str
    workspace_id: str
    region: str
    fencing_token: int
    granted_microdollars: int
    credit_shard: int
    expires_at: str
    quota_shard: int = 0
    state: str = "pending"
    reconciled_spent_microdollars: int = 0
    reconciled_key_microdollars: dict[str, int] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: _iso(utcnow()))
    updated_at: str = field(default_factory=lambda: _iso(utcnow()))
    last_error: str | None = None
    issuance_tier: int | None = None
    tier_cap_micro: int | None = None

    @property
    def entity_id(self) -> str:
        return _lease_entity_id(self.workspace_id, self.region, self.lease_id)

    @property
    def expires_datetime(self) -> datetime:
        return _parse_iso(self.expires_at)


@dataclass
class RegionalQuotaFence:
    workspace_id: str
    region: str
    fencing_token: int
    quota_shard: int = 0
    active_lease_id: str | None = None
    updated_at: str = field(default_factory=lambda: _iso(utcnow()))


@dataclass(frozen=True)
class OpenRegionalQuotaLease:
    lease_entity_id: str
    workspace_id: str
    region: str
    lease_id: str
    expires_at: str

    @property
    def entity_id(self) -> str:
        return _open_lease_entity_id(self)


@dataclass(frozen=True)
class RegionalReconcileResult:
    lease_id: str
    spent_delta_microdollars: int
    unused_released_microdollars: int
    closed: bool
    replayed: bool


@dataclass(frozen=True)
class RegionalQuotaReconcilerLock:
    owner: str | None
    fencing_token: int
    expires_at: str
    updated_at: str
    # Acquisition-only metadata. These defaults keep older persisted lock rows
    # readable, and the transaction below deliberately does not persist the
    # previous holder on the new lock row.
    previous_owner: str | None = None
    previous_fencing_token: int | None = None

    @property
    def expires_datetime(self) -> datetime:
        return _parse_iso(self.expires_at)


def acquire_regional_quota_reconciler_lock(
    store: Any,
    *,
    owner: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> RegionalQuotaReconcilerLock | None:
    """Acquire the singleton worker lease with an expiry and fencing token."""

    now = utcnow() if now is None else now
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if not owner or len(owner) > 128:
        raise ValueError("owner must contain between 1 and 128 characters")
    if not 1 <= ttl_seconds <= 600:
        raise ValueError("ttl_seconds must be between 1 and 600")

    def txn(transaction: Any) -> RegionalQuotaReconcilerLock | None:
        current = store._read_entity_tx(
            transaction,
            _RECONCILER_LOCK_KIND,
            _RECONCILER_LOCK_ID,
            RegionalQuotaReconcilerLock,
        )
        if current is not None and current.owner is not None and current.expires_datetime > now:
            return None
        lock = RegionalQuotaReconcilerLock(
            owner=owner,
            fencing_token=1 if current is None else current.fencing_token + 1,
            expires_at=_iso(now + timedelta(seconds=ttl_seconds)),
            updated_at=_iso(now),
        )
        _upsert_entity_dml(
            transaction,
            store._param_types,
            _RECONCILER_LOCK_KIND,
            _RECONCILER_LOCK_ID,
            lock,
        )
        if current is not None and current.owner is not None:
            return dataclasses.replace(
                lock,
                previous_owner=current.owner,
                previous_fencing_token=current.fencing_token,
            )
        return lock

    return store._run_in_transaction(txn)


def release_regional_quota_reconciler_lock(
    store: Any,
    *,
    owner: str,
    fencing_token: int,
    now: datetime | None = None,
) -> bool:
    """Release only the lock generation owned by this worker."""

    now = utcnow() if now is None else now
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    def txn(transaction: Any) -> bool:
        current = store._read_entity_tx(
            transaction,
            _RECONCILER_LOCK_KIND,
            _RECONCILER_LOCK_ID,
            RegionalQuotaReconcilerLock,
        )
        if (
            current is None
            or current.owner != owner
            or current.fencing_token != fencing_token
        ):
            return False
        released = dataclasses.replace(
            current,
            owner=None,
            expires_at=_iso(now),
            updated_at=_iso(now),
        )
        _upsert_entity_dml(
            transaction,
            store._param_types,
            _RECONCILER_LOCK_KIND,
            _RECONCILER_LOCK_ID,
            released,
        )
        return True

    return bool(store._run_in_transaction(txn))


def grant_regional_quota_lease(
    store: Any,
    *,
    workspace_id: str,
    region: str,
    requested_microdollars: int,
    per_lease_cap_microdollars: int,
    max_available_basis_points: int,
    ttl_seconds: int,
    minimum_grant_microdollars: int,
    quota_shard: int = 0,
    now: datetime | None = None,
) -> GlobalRegionalQuotaLease | None:
    """Reserve a bounded exact grant and persist its fence in one transaction."""

    now = utcnow() if now is None else now
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if quota_shard < 0:
        raise ValueError("quota_shard must not be negative")
    rows = _credit_rows(store, workspace_id)
    available_by_shard = {
        int(shard): max(0, int(total) - int(usage) - int(reserved))
        for shard, total, usage, reserved in rows
    }
    total_available = sum(available_by_shard.values())
    bounded = bounded_lease_grant_microdollars(
        available_microdollars=total_available,
        requested_microdollars=requested_microdollars,
        per_lease_cap_microdollars=per_lease_cap_microdollars,
        max_available_basis_points=max_available_basis_points,
    )
    if bounded < minimum_grant_microdollars:
        return None
    candidates = list(available_by_shard)
    random.SystemRandom().shuffle(candidates)
    candidates.sort(key=lambda shard: available_by_shard[shard], reverse=True)
    selected_shard = next(
        (
            shard
            for shard in candidates
            if min(bounded, available_by_shard[shard]) >= minimum_grant_microdollars
        ),
        None,
    )
    if selected_shard is None:
        return None
    grant = min(bounded, available_by_shard[selected_shard])
    lease_id = f"rql-{uuid.uuid4().hex}"
    expires_at = now + timedelta(seconds=ttl_seconds)
    entity_id = _lease_entity_id(workspace_id, region, lease_id)
    fence_id = _fence_entity_id(workspace_id, region, quota_shard)

    def txn(transaction: Any) -> GlobalRegionalQuotaLease | None:
        fence = store._read_entity_tx(
            transaction,
            _FENCE_KIND,
            fence_id,
            RegionalQuotaFence,
        )
        # Exactly one globally escrowed grant may own a local quota shard at a
        # time. Without this guard, an exhausted row could mint another lease
        # before reconciliation and multiply the configured regional exposure.
        if fence is not None and fence.active_lease_id is not None:
            return None
        from trusted_router.trust_eligibility import lease_eligibility, tier_cap
        settings: Any = getattr(store, "trust_settings", None)
        tier = None
        cap = None
        tx_grant = grant
        armed = settings is not None and settings.spend_lease_trust_eligibility_enabled
        if armed:
            tier, reason = lease_eligibility(store, settings, workspace_id, reader=transaction)
            if reason:
                log.info("regional_quota.no_lease_reason=%s workspace_id=%s", reason, workspace_id)
                return None
            cap = tier_cap(settings, tier or 0)
            pool = min(settings.regional_quota_lease_max_microdollars, cap)
            tx_grant = min(grant, max(0, pool - _active_regional_escrow(transaction, workspace_id)))
            if tx_grant < minimum_grant_microdollars:
                return None
        from trusted_router.storage_gcp_counter_dml import reserve_credit_for_spend_lease
        if not reserve_credit_for_spend_lease(
            transaction, store._param_types, workspace_id, tx_grant, shard=selected_shard,
            trust_eligibility_enabled=armed, expected_trust_tier=tier,
            trust_max_age_seconds=settings.trust_reconcile_max_age_seconds if armed else 3600,
        ):
            return None
        fencing_token = 1 if fence is None else fence.fencing_token + 1
        updated_fence = RegionalQuotaFence(
            workspace_id=workspace_id,
            region=region,
            fencing_token=fencing_token,
            quota_shard=quota_shard,
            active_lease_id=lease_id,
            updated_at=_iso(now),
        )
        lease = GlobalRegionalQuotaLease(
            lease_id=lease_id,
            workspace_id=workspace_id,
            region=region,
            fencing_token=fencing_token,
            granted_microdollars=tx_grant,
            issuance_tier=tier,
            tier_cap_micro=cap,
            credit_shard=selected_shard,
            expires_at=_iso(expires_at),
            quota_shard=quota_shard,
            created_at=_iso(now),
            updated_at=_iso(now),
        )
        _upsert_entity_dml(
            transaction,
            store._param_types,
            _FENCE_KIND,
            fence_id,
            updated_fence,
        )
        _insert_entity_dml(
            transaction,
            store._param_types,
            _LEASE_KIND,
            entity_id,
            lease,
        )
        open_lease = OpenRegionalQuotaLease(
            lease_entity_id=entity_id,
            workspace_id=workspace_id,
            region=region,
            lease_id=lease_id,
            expires_at=_iso(expires_at),
        )
        # Use a client timestamp for this third tr_entities DML statement;
        # Spanner permits only one pending commit timestamp write per table in
        # a transaction.
        insert_entity_dml_at(
            transaction,
            store._param_types,
            _OPEN_LEASE_KIND,
            open_lease.entity_id,
            json_body(open_lease),
            now,
        )
        return lease

    return store._run_in_transaction(txn)


def activate_regional_quota_lease(
    store: Any,
    lease: GlobalRegionalQuotaLease,
    *,
    now: datetime | None = None,
) -> GlobalRegionalQuotaLease:
    return _transition_global_lease(
        store,
        lease,
        expected_states={"pending", "active"},
        state="active",
        now=now,
    )


def quarantine_regional_quota_lease(
    store: Any,
    lease: GlobalRegionalQuotaLease,
    *,
    reason: str,
    now: datetime | None = None,
) -> GlobalRegionalQuotaLease:
    return _transition_global_lease(
        store,
        lease,
        expected_states={"pending", "active", "draining", "quarantined"},
        state="quarantined",
        last_error=reason[:500],
        now=now,
    )


def active_regional_quota_leases(
    store: Any,
    *,
    workspace_id: str,
    region: str,
    quota_shard: int | None = None,
    now: datetime | None = None,
) -> list[GlobalRegionalQuotaLease]:
    now = utcnow() if now is None else now
    if quota_shard is None:
        raise ValueError("quota_shard is required for fenced regional lease lookup")
    fence = store._read_entity(
        _FENCE_KIND,
        _fence_entity_id(workspace_id, region, quota_shard),
        RegionalQuotaFence,
    )
    if fence is None or fence.active_lease_id is None:
        return []
    lease = get_global_regional_quota_lease(
        store,
        workspace_id=workspace_id,
        region=region,
        lease_id=fence.active_lease_id,
    )
    if (
        lease is None
        or lease.quota_shard != quota_shard
        or lease.fencing_token != fence.fencing_token
        or lease.state != "active"
        or lease.expires_datetime <= now
    ):
        return []
    return [lease]


def get_global_regional_quota_lease(
    store: Any,
    *,
    workspace_id: str,
    region: str,
    lease_id: str,
) -> GlobalRegionalQuotaLease | None:
    return store._read_entity(
        _LEASE_KIND,
        _lease_entity_id(workspace_id, region, lease_id),
        GlobalRegionalQuotaLease,
    )


def reconcile_regional_quota_lease(
    store: Any,
    global_lease: GlobalRegionalQuotaLease,
    local_lease: RegionalQuotaLease,
    *,
    close: bool,
    now: datetime | None = None,
) -> RegionalReconcileResult:
    """Import settled spend and optionally release all unused escrow atomically."""

    now = utcnow() if now is None else now
    _validate_local_snapshot(global_lease, local_lease)
    if close and local_lease.reserved_microdollars:
        raise ValueError("cannot close a regional lease with open reservations")
    current_key_totals = {
        _key_total_id(key_hash, shard): amount
        for (key_hash, shard), amount in settled_key_totals(local_lease).items()
    }

    def txn(transaction: Any) -> RegionalReconcileResult:
        current = store._read_entity_tx(
            transaction,
            _LEASE_KIND,
            global_lease.entity_id,
            GlobalRegionalQuotaLease,
        )
        if current is None:
            raise RuntimeError("global regional lease is missing")
        _validate_same_global_lease(global_lease, current)
        if current.state == "closed":
            delete_entity_dml(
                transaction,
                store._param_types,
                _OPEN_LEASE_KIND,
                _open_lease_entity_id(current),
            )
            return RegionalReconcileResult(
                lease_id=current.lease_id,
                spent_delta_microdollars=0,
                unused_released_microdollars=0,
                closed=True,
                replayed=True,
            )
        if local_lease.spent_microdollars < current.reconciled_spent_microdollars:
            raise RuntimeError("regional settled total moved backwards")
        spent_delta = local_lease.spent_microdollars - current.reconciled_spent_microdollars
        key_deltas: dict[str, int] = {}
        for key, total in current_key_totals.items():
            previous = int(current.reconciled_key_microdollars.get(key, 0))
            if total < previous:
                raise RuntimeError("regional key usage moved backwards")
            if total > previous:
                key_deltas[key] = total - previous
        if sum(key_deltas.values()) != spent_delta:
            raise RuntimeError("regional workspace and key settlement totals differ")

        if (
            spent_delta
            and release_credit(
                transaction,
                store._param_types,
                current.workspace_id,
                spent_delta,
                spent_delta,
                shard=current.credit_shard,
            )
            != 1
        ):
            raise RuntimeError("global regional credit escrow release failed")
        floors = window_floors(now)
        for key, amount in key_deltas.items():
            key_hash, key_shard = _parse_key_total_id(key)
            count = release_key(
                transaction,
                store._param_types,
                key_hash,
                0,
                amount,
                book_to_byok=False,
                window_floors=floors,
                shard=key_shard,
            )
            if count != 1 and key_limit_exists(
                transaction,
                store._param_types,
                key_hash,
                shard=key_shard,
            ):
                raise RuntimeError("regional API key usage reconciliation failed")

        unused = 0
        next_state = "active"
        if close:
            unused = current.granted_microdollars - local_lease.spent_microdollars
            if unused < 0:
                raise RuntimeError("regional lease spent more than its grant")
            if (
                unused
                and release_credit(
                    transaction,
                    store._param_types,
                    current.workspace_id,
                    unused,
                    0,
                    shard=current.credit_shard,
                )
                != 1
            ):
                raise RuntimeError("unused regional credit escrow release failed")
            next_state = "closed"
            fence_id = _fence_entity_id(
                current.workspace_id,
                current.region,
                current.quota_shard,
            )
            fence = store._read_entity_tx(
                transaction,
                _FENCE_KIND,
                fence_id,
                RegionalQuotaFence,
            )
            if fence is None or fence.active_lease_id != current.lease_id:
                raise RuntimeError("regional lease no longer owns its global fence")
            _upsert_entity_dml(
                transaction,
                store._param_types,
                _FENCE_KIND,
                fence_id,
                dataclasses.replace(
                    fence,
                    active_lease_id=None,
                    updated_at=_iso(now),
                ),
            )
            delete_entity_dml(
                transaction,
                store._param_types,
                _OPEN_LEASE_KIND,
                _open_lease_entity_id(current),
            )
        updated = dataclasses.replace(
            current,
            state=next_state,
            reconciled_spent_microdollars=local_lease.spent_microdollars,
            reconciled_key_microdollars=current_key_totals,
            updated_at=_iso(now),
        )
        _upsert_entity_dml(
            transaction,
            store._param_types,
            _LEASE_KIND,
            current.entity_id,
            updated,
        )
        return RegionalReconcileResult(
            lease_id=current.lease_id,
            spent_delta_microdollars=spent_delta,
            unused_released_microdollars=unused,
            closed=close,
            replayed=spent_delta == 0 and unused == 0,
        )

    return store._run_in_transaction(txn)


def close_expired_uninitialized_regional_quota_lease(
    store: Any,
    global_lease: GlobalRegionalQuotaLease,
    *,
    now: datetime | None = None,
) -> RegionalReconcileResult:
    """Release escrow after a failed Bigtable initialization left no row.

    A missing row is recoverable only for an expired quarantined lease with no
    previously imported spend. Bigtable reads through a single-cluster profile
    are strongly consistent, so absence proves the grant was never usable.
    """

    now = utcnow() if now is None else now
    if global_lease.state != "quarantined":
        raise RuntimeError("regional lease row is missing")
    if global_lease.expires_datetime > now:
        raise RuntimeError("quarantined regional lease has not expired")
    if (
        global_lease.reconciled_spent_microdollars != 0
        or global_lease.reconciled_key_microdollars
    ):
        raise RuntimeError("quarantined regional lease has imported usage")
    empty = regional_lease_from_global(global_lease).begin_drain(
        fencing_token=global_lease.fencing_token
    )
    return reconcile_regional_quota_lease(
        store,
        global_lease,
        empty,
        close=True,
        now=now,
    )


def delete_closed_regional_quota_open_index(
    store: Any,
    global_lease: GlobalRegionalQuotaLease,
    open_lease: OpenRegionalQuotaLease,
) -> bool:
    """Idempotently remove a stale derived index after canonical close."""

    def txn(transaction: Any) -> bool:
        current = store._read_entity_tx(
            transaction,
            _LEASE_KIND,
            global_lease.entity_id,
            GlobalRegionalQuotaLease,
        )
        if current is None:
            raise RuntimeError("indexed global regional lease is missing")
        _validate_same_global_lease(global_lease, current)
        if current.state != "closed":
            return False
        delete_entity_dml(
            transaction,
            store._param_types,
            _OPEN_LEASE_KIND,
            open_lease.entity_id,
        )
        return True

    return bool(store._run_in_transaction(txn))


def record_regional_gateway_authorization(
    store: Any,
    *,
    authorization: GatewayAuthorization,
    idempotency_scope: str | None,
    idempotency_fingerprint: str | None,
    expires_at: datetime,
) -> dict[str, Any]:
    """Persist replay state without touching the globally escrowed counters."""

    if store.request_record_write_mode != "typed":
        raise RuntimeError("regional quota leases require typed request records")
    reservation_id = str(uuid.uuid4())
    created_at = utcnow()
    authorization.credit_reservation_id = reservation_id
    authorization.created_at = _iso(created_at)

    def replay(existing: dict[str, Any]) -> dict[str, Any]:
        return {
            "outcome": "replay",
            "reservation_id": existing["reservation_id"],
            "authorization_id": existing["authorization_id"],
        }

    def txn(transaction: Any) -> dict[str, Any]:
        if idempotency_scope is not None:
            existing = read_reservation_by_idempotency(
                transaction,
                store._param_types,
                idempotency_scope,
            )
            if existing is not None:
                if existing["idempotency_fingerprint"] != idempotency_fingerprint:
                    return {"outcome": "idempotency_mismatch"}
                return replay(existing)
        from trusted_router.trust_eligibility import billing_paused_tx, lease_eligibility, tier_cap
        settings: Any = getattr(store, "trust_settings", None)
        reason = "billing_paused" if billing_paused_tx(transaction, store._param_types, authorization.workspace_id) else None
        armed = settings is not None and settings.spend_lease_trust_eligibility_enabled
        current = None
        if armed or reason:
            current = store._read_entity_tx(transaction, _LEASE_KIND,
                _lease_entity_id(authorization.workspace_id, str(authorization.region),
                                 str(authorization.regional_lease_id)), GlobalRegionalQuotaLease)
        if armed:
            tier, gate_reason = lease_eligibility(store, settings, authorization.workspace_id, reader=transaction)
            reason = reason or gate_reason
            if reason is None:
                cap = tier_cap(settings, tier or 0)
                if (current is None or current.state != "active"
                    or current.issuance_tier != tier or current.tier_cap_micro != cap
                    or current.fencing_token != authorization.regional_fencing_token
                    or current.expires_datetime <= utcnow()
                    or _active_regional_escrow(transaction, authorization.workspace_id)
                       > min(settings.regional_quota_lease_max_microdollars, cap)):
                    reason = "unpaid_workspace"
        if reason:
            if current is not None and current.state != "closed":
                retired = dataclasses.replace(current, state="quarantined", last_error=reason,
                                               updated_at=_iso(utcnow()))
                _upsert_entity_dml(transaction, store._param_types, _LEASE_KIND, current.entity_id, retired)
            return {"outcome": reason}
        insert_reservation(
            transaction,
            store._param_types,
            reservation_id=reservation_id,
            workspace_id=authorization.workspace_id,
            key_hash=authorization.key_hash,
            ws_shard=0,
            credit_shard=0,
            key_shard=0,
            credit_reserved_micro=0,
            key_reserved_micro=0,
            hold_usage_type="RegionalCredits",
            authorization_id=authorization.id,
            idempotency_scope=idempotency_scope,
            idempotency_fingerprint=idempotency_fingerprint,
            expires_at=expires_at,
            created_at=created_at,
        )
        insert_gateway_authorization(
            transaction,
            store._param_types,
            authorization,
            created_at=created_at,
        )
        return {
            "outcome": "accepted",
            "reservation_id": reservation_id,
            "authorization_id": authorization.id,
        }

    try:
        return run_in_transaction_with_retry(store._database, txn)
    except AlreadyExists:
        if idempotency_scope is None:
            raise

        def replay_txn(transaction: Any) -> dict[str, Any]:
            existing = read_reservation_by_idempotency(
                transaction,
                store._param_types,
                idempotency_scope,
            )
            if existing is None:
                return {"outcome": "idempotency_mismatch"}
            if existing["idempotency_fingerprint"] != idempotency_fingerprint:
                return {"outcome": "idempotency_mismatch"}
            return replay(existing)

        return run_in_transaction_with_retry(store._database, replay_txn)


def regional_lease_from_global(record: GlobalRegionalQuotaLease) -> RegionalQuotaLease:
    return RegionalQuotaLease(
        lease_id=record.lease_id,
        workspace_id=record.workspace_id,
        region=record.region,
        fencing_token=record.fencing_token,
        granted_microdollars=record.granted_microdollars,
        expires_at=record.expires_datetime,
    )


def _credit_rows(store: Any, workspace_id: str) -> list[list[Any]]:
    with store._database.snapshot() as snapshot:
        return list(
            snapshot.execute_sql(
                "SELECT shard, total_credits, total_usage, reserved "
                "FROM tr_credit_balance WHERE workspace_id=@ws ORDER BY shard",
                params={"ws": workspace_id},
                param_types={"ws": store._param_types.STRING},
            )
        )


def _transition_global_lease(
    store: Any,
    lease: GlobalRegionalQuotaLease,
    *,
    expected_states: set[str],
    state: str,
    last_error: str | None = None,
    now: datetime | None = None,
) -> GlobalRegionalQuotaLease:
    now = utcnow() if now is None else now

    def txn(transaction: Any) -> GlobalRegionalQuotaLease:
        current = store._read_entity_tx(
            transaction,
            _LEASE_KIND,
            lease.entity_id,
            GlobalRegionalQuotaLease,
        )
        if current is None:
            raise RuntimeError("global regional lease is missing")
        _validate_same_global_lease(lease, current)
        if current.state not in expected_states:
            raise RuntimeError(f"regional lease is {current.state}")
        updated = dataclasses.replace(
            current,
            state=state,
            updated_at=_iso(now),
            last_error=last_error,
        )
        _upsert_entity_dml(
            transaction,
            store._param_types,
            _LEASE_KIND,
            current.entity_id,
            updated,
        )
        return updated

    return store._run_in_transaction(txn)


def _validate_same_global_lease(
    expected: GlobalRegionalQuotaLease,
    current: GlobalRegionalQuotaLease,
) -> None:
    identity = (
        "lease_id",
        "workspace_id",
        "region",
        "fencing_token",
        "granted_microdollars",
        "credit_shard",
        "quota_shard",
    )
    if any(getattr(expected, field) != getattr(current, field) for field in identity):
        raise RuntimeError("global regional lease identity changed")


def _validate_local_snapshot(
    global_lease: GlobalRegionalQuotaLease,
    local_lease: RegionalQuotaLease,
) -> None:
    if (
        global_lease.lease_id != local_lease.lease_id
        or global_lease.workspace_id != local_lease.workspace_id
        or global_lease.region != local_lease.region
        or global_lease.fencing_token != local_lease.fencing_token
        or global_lease.granted_microdollars != local_lease.granted_microdollars
    ):
        raise RuntimeError("regional lease snapshot does not match global escrow")
    if local_lease.spent_microdollars > global_lease.granted_microdollars:
        raise RuntimeError("regional lease snapshot exceeds global escrow")


def _insert_entity_dml(
    transaction: Any,
    param_types: Any,
    kind: str,
    entity_id: str,
    value: Any,
) -> None:
    transaction.execute_update(
        "INSERT INTO tr_entities (kind, id, body, updated_at) "
        "VALUES (@kind, @id, @body, PENDING_COMMIT_TIMESTAMP())",
        params={"kind": kind, "id": entity_id, "body": _regional_json_body(value)},
        param_types={
            "kind": param_types.STRING,
            "id": param_types.STRING,
            "body": param_types.STRING,
        },
    )


def _upsert_entity_dml(
    transaction: Any,
    param_types: Any,
    kind: str,
    entity_id: str,
    value: Any,
) -> None:
    params = {"kind": kind, "id": entity_id, "body": _regional_json_body(value)}
    types = {
        "kind": param_types.STRING,
        "id": param_types.STRING,
        "body": param_types.STRING,
    }
    updated = transaction.execute_update(
        "UPDATE tr_entities SET body=@body, updated_at=PENDING_COMMIT_TIMESTAMP() "
        "WHERE kind=@kind AND id=@id",
        params=params,
        param_types=types,
    )
    if updated == 0:
        transaction.execute_update(
            "INSERT INTO tr_entities (kind, id, body, updated_at) "
            "VALUES (@kind, @id, @body, PENDING_COMMIT_TIMESTAMP())",
            params=params,
            param_types=types,
        )


def _lease_entity_id(workspace_id: str, region: str, lease_id: str) -> str:
    return f"{workspace_id}#{region}#{lease_id}"


def _fence_entity_id(workspace_id: str, region: str, quota_shard: int) -> str:
    return f"{workspace_id}#{region}#{quota_shard}"


def _open_lease_entity_id(lease: GlobalRegionalQuotaLease | OpenRegionalQuotaLease) -> str:
    return f"{lease.expires_at}#{lease.workspace_id}#{lease.region}#{lease.lease_id}"


def _key_total_id(key_hash: str, shard: int) -> str:
    return f"{key_hash}#{shard}"


def _parse_key_total_id(value: str) -> tuple[str, int]:
    key_hash, shard = value.rsplit("#", 1)
    return key_hash, int(shard)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _active_regional_escrow(transaction: Any, workspace_id: str) -> int:
    """Range read serializes all quota shards and regions against concurrent mint."""
    rows = transaction.execute_sql(
        "SELECT id, body FROM tr_entities WHERE kind='regional_quota_lease' ORDER BY id")
    leases = [GlobalRegionalQuotaLease(**json.loads(str(row[1]))) for row in rows]
    # Quarantined/expired grants still own escrow until the reconciler closes them.
    return sum(max(0, lease.granted_microdollars - lease.reconciled_spent_microdollars)
               for lease in leases if lease.workspace_id == workspace_id and lease.state != "closed")


def _regional_json_body(value: Any) -> str:
    if isinstance(value, GlobalRegionalQuotaLease) and value.issuance_tier is None:
        body = dataclasses.asdict(value)
        body.pop("issuance_tier")
        body.pop("tier_cap_micro")
        return json_body(body)
    return json_body(value)
