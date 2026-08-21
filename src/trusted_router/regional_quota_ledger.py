"""Durable regional ledger adapters for bounded prepaid quota escrow.

The gateway hot path mutates one lease row atomically. Global Spanner has
already reserved the full grant, so this ledger can never authorize more money
than the globally escrowed amount. The Bigtable adapter uses an explicit
compare-and-swap version cell and a fixed regional app profile for every lease.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from trusted_router.services.regional_quota_leases import (
    HoldState,
    LeaseState,
    QuotaLeaseHold,
    RegionalQuotaLease,
)

_FAMILY = "lease"
_STATE_COLUMN = b"state"
_VERSION_COLUMN = b"version"
_MAX_CAS_ATTEMPTS = 16


class RegionalLeaseLedgerError(RuntimeError):
    """The durable regional ledger could not complete an operation."""


class RegionalLeaseCasExhausted(RegionalLeaseLedgerError):
    pass


class RegionalLeaseNotFound(RegionalLeaseLedgerError):
    pass


class RegionalQuotaLedger(Protocol):
    def supports_region(self, region: str) -> bool: ...

    def initialize(self, lease: RegionalQuotaLease) -> RegionalQuotaLease: ...

    def get(self, lease_id: str, *, region: str) -> RegionalQuotaLease | None: ...

    def reserve(
        self,
        lease_id: str,
        *,
        region: str,
        hold_id: str,
        fingerprint: str,
        amount_microdollars: int,
        fencing_token: int,
        key_hash: str,
        key_shard: int,
        hold_expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> RegionalQuotaLease: ...

    def settle(
        self,
        lease_id: str,
        *,
        region: str,
        hold_id: str,
        actual_microdollars: int,
        fencing_token: int,
    ) -> RegionalQuotaLease: ...

    def refund(
        self,
        lease_id: str,
        *,
        region: str,
        hold_id: str,
        fencing_token: int,
    ) -> RegionalQuotaLease: ...

    def begin_drain(
        self,
        lease_id: str,
        *,
        region: str,
        fencing_token: int,
    ) -> RegionalQuotaLease: ...

    def close(
        self,
        lease_id: str,
        *,
        region: str,
        fencing_token: int,
    ) -> RegionalQuotaLease: ...


class InMemoryRegionalQuotaLedger:
    """Transactionally faithful test twin of the Bigtable row adapter."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._leases: dict[tuple[str, str], RegionalQuotaLease] = {}

    def supports_region(self, region: str) -> bool:
        return bool(region)

    def initialize(self, lease: RegionalQuotaLease) -> RegionalQuotaLease:
        key = (lease.region, lease.lease_id)
        with self._lock:
            existing = self._leases.get(key)
            if existing is not None:
                if existing != lease:
                    raise RegionalLeaseLedgerError(
                        "lease ID already contains different durable state"
                    )
                return existing
            self._leases[key] = lease
            return lease

    def get(self, lease_id: str, *, region: str) -> RegionalQuotaLease | None:
        with self._lock:
            return self._leases.get((region, lease_id))

    def _apply(
        self,
        lease_id: str,
        region: str,
        transition: Callable[[RegionalQuotaLease], RegionalQuotaLease],
    ) -> RegionalQuotaLease:
        with self._lock:
            key = (region, lease_id)
            current = self._leases.get(key)
            if current is None:
                raise RegionalLeaseNotFound("regional lease was not found")
            updated = transition(current)
            self._leases[key] = updated
            return updated

    def reserve(
        self,
        lease_id: str,
        *,
        region: str,
        hold_id: str,
        fingerprint: str,
        amount_microdollars: int,
        fencing_token: int,
        key_hash: str,
        key_shard: int,
        hold_expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> RegionalQuotaLease:
        return self._apply(
            lease_id,
            region,
            lambda lease: (
                lease.reserve(
                    hold_id=hold_id,
                    fingerprint=fingerprint,
                    amount_microdollars=amount_microdollars,
                    fencing_token=fencing_token,
                    key_hash=key_hash,
                    key_shard=key_shard,
                    hold_expires_at=hold_expires_at,
                    now=now,
                ).lease
            ),
        )

    def settle(
        self,
        lease_id: str,
        *,
        region: str,
        hold_id: str,
        actual_microdollars: int,
        fencing_token: int,
    ) -> RegionalQuotaLease:
        return self._apply(
            lease_id,
            region,
            lambda lease: (
                lease.settle(
                    hold_id=hold_id,
                    actual_microdollars=actual_microdollars,
                    fencing_token=fencing_token,
                ).lease
            ),
        )

    def refund(
        self,
        lease_id: str,
        *,
        region: str,
        hold_id: str,
        fencing_token: int,
    ) -> RegionalQuotaLease:
        return self._apply(
            lease_id,
            region,
            lambda lease: (
                lease.refund(
                    hold_id=hold_id,
                    fencing_token=fencing_token,
                ).lease
            ),
        )

    def begin_drain(
        self,
        lease_id: str,
        *,
        region: str,
        fencing_token: int,
    ) -> RegionalQuotaLease:
        return self._apply(
            lease_id,
            region,
            lambda lease: lease.begin_drain(fencing_token=fencing_token),
        )

    def close(
        self,
        lease_id: str,
        *,
        region: str,
        fencing_token: int,
    ) -> RegionalQuotaLease:
        return self._apply(
            lease_id,
            region,
            lambda lease: lease.close(fencing_token=fencing_token),
        )


class BigtableRegionalQuotaLedger:
    """Single-row CAS ledger routed through fixed regional app profiles."""

    def __init__(self, tables_by_region: dict[str, Any]) -> None:
        if not tables_by_region:
            raise ValueError("at least one regional Bigtable table is required")
        self._tables = dict(tables_by_region)
        try:
            from google.cloud.bigtable.row_filters import (
                CellsColumnLimitFilter,
                ColumnQualifierRegexFilter,
                FamilyNameRegexFilter,
                RowFilterChain,
                ValueRegexFilter,
            )
        except ImportError as exc:  # pragma: no cover - production dependency
            raise RuntimeError("google-cloud-bigtable is required") from exc
        self._cells_limit_filter = CellsColumnLimitFilter
        self._column_filter = ColumnQualifierRegexFilter
        self._family_filter = FamilyNameRegexFilter
        self._chain_filter = RowFilterChain
        self._value_filter = ValueRegexFilter

    def supports_region(self, region: str) -> bool:
        return region in self._tables

    def initialize(self, lease: RegionalQuotaLease) -> RegionalQuotaLease:
        table = self._table(lease.region)
        row_key = _row_key(lease)
        state = _serialize_lease(lease)
        version = uuid.uuid4().hex.encode("ascii")
        predicate = self._version_exists_filter()
        row = table.row(row_key, filter_=predicate)
        row.set_cell(_FAMILY, _STATE_COLUMN, state, state=False)
        row.set_cell(_FAMILY, _VERSION_COLUMN, version, state=False)
        try:
            matched = bool(row.commit())
        except Exception as exc:  # pragma: no cover - remote transport
            raise RegionalLeaseLedgerError("regional lease initialization failed") from exc
        if not matched:
            return lease
        existing = self.get(lease.lease_id, region=lease.region)
        if existing is None:
            raise RegionalLeaseLedgerError("lease initialization was ambiguous")
        if existing != lease:
            raise RegionalLeaseLedgerError("lease ID already contains different durable state")
        return existing

    def get(self, lease_id: str, *, region: str) -> RegionalQuotaLease | None:
        table = self._table(region)
        try:
            row = table.read_row(
                _row_key_for(region, lease_id),
                filter_=self._state_filter(),
            )
        except Exception as exc:  # pragma: no cover - remote transport
            raise RegionalLeaseLedgerError("regional lease read failed") from exc
        if row is None:
            return None
        state, _version = _state_and_version(row)
        return _deserialize_lease(state)

    def health_check(self) -> tuple[str, ...]:
        """Prove conditional writes and reads through every fixed profile."""

        for region in sorted(self._tables):
            table = self._tables[region]
            row_key = f"health#regional-quota#{region}".encode()
            version = uuid.uuid4().hex.encode("ascii")
            row = table.row(row_key, filter_=self._version_exists_filter())
            for state in (False, True):
                row.set_cell(_FAMILY, _STATE_COLUMN, b'{"status":"ok"}', state=state)
                row.set_cell(_FAMILY, _VERSION_COLUMN, version, state=state)
            try:
                row.commit()
                durable = table.read_row(row_key, filter_=self._state_filter())
            except Exception as exc:  # pragma: no cover - remote transport
                raise RegionalLeaseLedgerError(
                    "regional ledger transactional health check failed"
                ) from exc
            if durable is None:
                raise RegionalLeaseLedgerError(
                    "regional ledger health check write was not durable"
                )
        return tuple(sorted(self._tables))

    def reserve(
        self,
        lease_id: str,
        *,
        region: str,
        hold_id: str,
        fingerprint: str,
        amount_microdollars: int,
        fencing_token: int,
        key_hash: str,
        key_shard: int,
        hold_expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> RegionalQuotaLease:
        return self._transition(
            lease_id,
            region=region,
            transition=lambda lease: (
                lease.reserve(
                    hold_id=hold_id,
                    fingerprint=fingerprint,
                    amount_microdollars=amount_microdollars,
                    fencing_token=fencing_token,
                    key_hash=key_hash,
                    key_shard=key_shard,
                    hold_expires_at=hold_expires_at,
                    now=now,
                ).lease
            ),
        )

    def settle(
        self,
        lease_id: str,
        *,
        region: str,
        hold_id: str,
        actual_microdollars: int,
        fencing_token: int,
    ) -> RegionalQuotaLease:
        return self._transition(
            lease_id,
            region=region,
            transition=lambda lease: (
                lease.settle(
                    hold_id=hold_id,
                    actual_microdollars=actual_microdollars,
                    fencing_token=fencing_token,
                ).lease
            ),
        )

    def refund(
        self,
        lease_id: str,
        *,
        region: str,
        hold_id: str,
        fencing_token: int,
    ) -> RegionalQuotaLease:
        return self._transition(
            lease_id,
            region=region,
            transition=lambda lease: (
                lease.refund(
                    hold_id=hold_id,
                    fencing_token=fencing_token,
                ).lease
            ),
        )

    def begin_drain(
        self,
        lease_id: str,
        *,
        region: str,
        fencing_token: int,
    ) -> RegionalQuotaLease:
        return self._transition(
            lease_id,
            region=region,
            transition=lambda lease: lease.begin_drain(fencing_token=fencing_token),
        )

    def close(
        self,
        lease_id: str,
        *,
        region: str,
        fencing_token: int,
    ) -> RegionalQuotaLease:
        return self._transition(
            lease_id,
            region=region,
            transition=lambda lease: lease.close(fencing_token=fencing_token),
        )

    def _transition(
        self,
        lease_id: str,
        *,
        region: str,
        transition: Callable[[RegionalQuotaLease], RegionalQuotaLease],
    ) -> RegionalQuotaLease:
        table = self._table(region)
        row_key = _row_key_for(region, lease_id)
        last_error: Exception | None = None
        for _attempt in range(_MAX_CAS_ATTEMPTS):
            try:
                row_data = table.read_row(row_key, filter_=self._state_filter())
            except Exception as exc:  # pragma: no cover - remote transport
                raise RegionalLeaseLedgerError("regional lease read failed") from exc
            if row_data is None:
                raise RegionalLeaseNotFound("regional lease was not found")
            state, current_version = _state_and_version(row_data)
            updated = transition(_deserialize_lease(state))
            updated_state = _serialize_lease(updated)
            next_version = uuid.uuid4().hex.encode("ascii")
            predicate = self._version_equals_filter(current_version)
            row = table.row(row_key, filter_=predicate)
            row.set_cell(_FAMILY, _STATE_COLUMN, updated_state, state=True)
            row.set_cell(_FAMILY, _VERSION_COLUMN, next_version, state=True)
            try:
                if row.commit():
                    return updated
            except Exception as exc:  # pragma: no cover - remote transport
                # The commit may have reached Bigtable. Re-read on the next
                # attempt; every transition is idempotent by hold ID/fence.
                last_error = exc
                continue
        raise RegionalLeaseCasExhausted(
            "regional lease compare-and-swap retries were exhausted"
        ) from last_error

    def _table(self, region: str) -> Any:
        table = self._tables.get(region)
        if table is None:
            raise RegionalLeaseLedgerError(
                f"no fixed Bigtable app profile is configured for region {region}"
            )
        return table

    def _version_exists_filter(self) -> Any:
        return self._chain_filter(
            [
                self._family_filter(f"^{_FAMILY}$"),
                self._column_filter(b"^version$"),
                self._cells_limit_filter(1),
            ]
        )

    def _version_equals_filter(self, version: bytes) -> Any:
        return self._chain_filter(
            [
                self._family_filter(f"^{_FAMILY}$"),
                self._column_filter(b"^version$"),
                self._value_filter(b"^" + re.escape(version) + b"$"),
                self._cells_limit_filter(1),
            ]
        )

    def _state_filter(self) -> Any:
        return self._chain_filter(
            [
                self._family_filter(f"^{_FAMILY}$"),
                self._column_filter(b"^(state|version)$"),
                self._cells_limit_filter(1),
            ]
        )


def settled_key_totals(lease: RegionalQuotaLease) -> dict[tuple[str, int], int]:
    totals: dict[tuple[str, int], int] = {}
    for hold in lease.holds:
        if hold.state != HoldState.SETTLED or not hold.key_hash:
            continue
        key = (hold.key_hash, hold.key_shard)
        totals[key] = totals.get(key, 0) + int(hold.actual_microdollars or 0)
    return totals


def _row_key(lease: RegionalQuotaLease) -> bytes:
    return _row_key_for(lease.region, lease.lease_id)


def _row_key_for(region: str, lease_id: str) -> bytes:
    spread = hashlib.sha256(lease_id.encode("utf-8")).hexdigest()[:4]
    return f"{spread}#lease#{region}#{lease_id}".encode()


def _serialize_lease(lease: RegionalQuotaLease) -> bytes:
    payload = {
        "lease_id": lease.lease_id,
        "workspace_id": lease.workspace_id,
        "region": lease.region,
        "fencing_token": lease.fencing_token,
        "granted_microdollars": lease.granted_microdollars,
        "expires_at": lease.expires_at.astimezone(UTC).isoformat(),
        "state": lease.state.value,
        "holds": [
            {
                "hold_id": hold.hold_id,
                "fingerprint": hold.fingerprint,
                "reserved_microdollars": hold.reserved_microdollars,
                "key_hash": hold.key_hash,
                "key_shard": hold.key_shard,
                "expires_at": (
                    hold.expires_at.astimezone(UTC).isoformat()
                    if hold.expires_at is not None
                    else None
                ),
                "state": hold.state.value,
                "actual_microdollars": hold.actual_microdollars,
            }
            for hold in lease.holds
        ],
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _deserialize_lease(value: bytes) -> RegionalQuotaLease:
    payload = json.loads(value.decode("utf-8"))
    return RegionalQuotaLease(
        lease_id=str(payload["lease_id"]),
        workspace_id=str(payload["workspace_id"]),
        region=str(payload["region"]),
        fencing_token=int(payload["fencing_token"]),
        granted_microdollars=int(payload["granted_microdollars"]),
        expires_at=datetime.fromisoformat(str(payload["expires_at"])),
        state=LeaseState(str(payload["state"])),
        holds=tuple(
            QuotaLeaseHold(
                hold_id=str(item["hold_id"]),
                fingerprint=str(item["fingerprint"]),
                reserved_microdollars=int(item["reserved_microdollars"]),
                key_hash=str(item.get("key_hash") or ""),
                key_shard=int(item.get("key_shard") or 0),
                expires_at=(
                    None
                    if item.get("expires_at") is None
                    else datetime.fromisoformat(str(item["expires_at"]))
                ),
                state=HoldState(str(item["state"])),
                actual_microdollars=(
                    None
                    if item.get("actual_microdollars") is None
                    else int(item["actual_microdollars"])
                ),
            )
            for item in payload.get("holds", [])
        ),
    )


def _state_and_version(row: Any) -> tuple[bytes, bytes]:
    try:
        family = row.cells[_FAMILY]
        state = bytes(family[_STATE_COLUMN][0].value)
        version = bytes(family[_VERSION_COLUMN][0].value)
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise RegionalLeaseLedgerError("regional lease row is incomplete") from exc
    return state, version
