"""Pure state machine for bounded regional prepaid quota escrow."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum


class LeaseState(StrEnum):
    ACTIVE = "active"
    DRAINING = "draining"
    CLOSED = "closed"
    QUARANTINED = "quarantined"


class HoldState(StrEnum):
    RESERVED = "reserved"
    SETTLED = "settled"
    REFUNDED = "refunded"


class RegionalQuotaLeaseError(ValueError):
    """Base class for deterministic lease-state failures."""


class LeaseUnavailableError(RegionalQuotaLeaseError):
    pass


class LeaseExhaustedError(RegionalQuotaLeaseError):
    pass


class LeaseFenceMismatchError(RegionalQuotaLeaseError):
    pass


class LeaseIdempotencyConflictError(RegionalQuotaLeaseError):
    pass


class LeaseSettlementError(RegionalQuotaLeaseError):
    pass


@dataclass(frozen=True, slots=True)
class QuotaLeaseHold:
    hold_id: str
    fingerprint: str
    reserved_microdollars: int
    key_hash: str = ""
    key_shard: int = 0
    expires_at: datetime | None = None
    state: HoldState = HoldState.RESERVED
    actual_microdollars: int | None = None

    def __post_init__(self) -> None:
        if not self.hold_id or not self.fingerprint:
            raise RegionalQuotaLeaseError("hold_id and fingerprint are required")
        if self.reserved_microdollars <= 0:
            raise RegionalQuotaLeaseError("reserved_microdollars must be positive")
        if self.key_shard < 0:
            raise RegionalQuotaLeaseError("key_shard must not be negative")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise RegionalQuotaLeaseError("hold expires_at must be timezone-aware")
        if self.state == HoldState.RESERVED and self.actual_microdollars is not None:
            raise RegionalQuotaLeaseError("reserved hold cannot have an actual amount")
        if self.state == HoldState.SETTLED and not (
            self.actual_microdollars is not None
            and 0 <= self.actual_microdollars <= self.reserved_microdollars
        ):
            raise RegionalQuotaLeaseError(
                "settled hold actual amount must fit inside its reservation"
            )
        if self.state == HoldState.REFUNDED and self.actual_microdollars != 0:
            raise RegionalQuotaLeaseError("refunded hold actual amount must be zero")


@dataclass(frozen=True, slots=True)
class LeaseTransition:
    lease: RegionalQuotaLease
    hold: QuotaLeaseHold
    replayed: bool


@dataclass(frozen=True, slots=True)
class RegionalQuotaLease:
    lease_id: str
    workspace_id: str
    region: str
    fencing_token: int
    granted_microdollars: int
    expires_at: datetime
    state: LeaseState = LeaseState.ACTIVE
    holds: tuple[QuotaLeaseHold, ...] = ()

    def __post_init__(self) -> None:
        if not self.lease_id or not self.workspace_id or not self.region:
            raise RegionalQuotaLeaseError("lease_id, workspace_id, and region are required")
        if self.fencing_token <= 0:
            raise RegionalQuotaLeaseError("fencing_token must be positive")
        if self.granted_microdollars <= 0:
            raise RegionalQuotaLeaseError("granted_microdollars must be positive")
        if self.expires_at.tzinfo is None:
            raise RegionalQuotaLeaseError("expires_at must be timezone-aware")
        if len({hold.hold_id for hold in self.holds}) != len(self.holds):
            raise RegionalQuotaLeaseError("hold IDs must be unique within a lease")
        if self.accounted_microdollars > self.granted_microdollars:
            raise RegionalQuotaLeaseError("lease accounting exceeds its grant")

    @property
    def reserved_microdollars(self) -> int:
        return sum(
            hold.reserved_microdollars for hold in self.holds if hold.state == HoldState.RESERVED
        )

    @property
    def spent_microdollars(self) -> int:
        return sum(
            hold.actual_microdollars or 0 for hold in self.holds if hold.state == HoldState.SETTLED
        )

    @property
    def accounted_microdollars(self) -> int:
        return self.reserved_microdollars + self.spent_microdollars

    @property
    def available_microdollars(self) -> int:
        return self.granted_microdollars - self.accounted_microdollars

    def reserve(
        self,
        *,
        hold_id: str,
        fingerprint: str,
        amount_microdollars: int,
        fencing_token: int,
        key_hash: str = "",
        key_shard: int = 0,
        hold_expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> LeaseTransition:
        now = _utc_now() if now is None else now
        self._require_fence(fencing_token)
        self._require_aware(now)
        existing = self._hold(hold_id)
        if existing is not None:
            if (
                existing.fingerprint != fingerprint
                or existing.reserved_microdollars != amount_microdollars
                or existing.key_hash != key_hash
                or existing.key_shard != key_shard
                or existing.expires_at != hold_expires_at
            ):
                raise LeaseIdempotencyConflictError(
                    "hold ID was reused with different reservation inputs"
                )
            return LeaseTransition(self, existing, True)
        if self.state != LeaseState.ACTIVE:
            raise LeaseUnavailableError(f"lease is {self.state}")
        if now >= self.expires_at:
            raise LeaseUnavailableError("lease has expired")
        if amount_microdollars <= 0:
            raise RegionalQuotaLeaseError("reservation must be positive")
        if amount_microdollars > self.available_microdollars:
            raise LeaseExhaustedError("regional lease has insufficient quota")
        hold = QuotaLeaseHold(
            hold_id=hold_id,
            fingerprint=fingerprint,
            reserved_microdollars=amount_microdollars,
            key_hash=key_hash,
            key_shard=key_shard,
            expires_at=hold_expires_at,
        )
        lease = replace(self, holds=(*self.holds, hold))
        return LeaseTransition(lease, hold, False)

    def settle(
        self,
        *,
        hold_id: str,
        actual_microdollars: int,
        fencing_token: int,
    ) -> LeaseTransition:
        self._require_fence(fencing_token)
        hold = self._required_hold(hold_id)
        if hold.state == HoldState.SETTLED:
            if hold.actual_microdollars != actual_microdollars:
                raise LeaseIdempotencyConflictError("settlement replay changed the actual amount")
            return LeaseTransition(self, hold, True)
        if hold.state == HoldState.REFUNDED:
            raise LeaseSettlementError("refunded hold cannot be settled")
        if not 0 <= actual_microdollars <= hold.reserved_microdollars:
            raise LeaseSettlementError(
                "actual amount must fit inside the exact regional reservation"
            )
        settled = replace(
            hold,
            state=HoldState.SETTLED,
            actual_microdollars=actual_microdollars,
        )
        lease = self._replace_hold(settled)
        return LeaseTransition(lease, settled, False)

    def refund(self, *, hold_id: str, fencing_token: int) -> LeaseTransition:
        self._require_fence(fencing_token)
        hold = self._required_hold(hold_id)
        if hold.state == HoldState.REFUNDED:
            return LeaseTransition(self, hold, True)
        if hold.state == HoldState.SETTLED:
            raise LeaseSettlementError("settled hold cannot be refunded")
        refunded = replace(hold, state=HoldState.REFUNDED, actual_microdollars=0)
        lease = self._replace_hold(refunded)
        return LeaseTransition(lease, refunded, False)

    def begin_drain(self, *, fencing_token: int) -> RegionalQuotaLease:
        self._require_fence(fencing_token)
        if self.state == LeaseState.DRAINING:
            return self
        if self.state != LeaseState.ACTIVE:
            raise LeaseUnavailableError(f"lease is {self.state}")
        return replace(self, state=LeaseState.DRAINING)

    def close(self, *, fencing_token: int) -> RegionalQuotaLease:
        self._require_fence(fencing_token)
        if self.state == LeaseState.CLOSED:
            return self
        if self.state != LeaseState.DRAINING:
            raise LeaseUnavailableError("lease must drain before close")
        if self.reserved_microdollars:
            raise LeaseUnavailableError("lease still has open reservations")
        return replace(self, state=LeaseState.CLOSED)

    def quarantine(self, *, fencing_token: int) -> RegionalQuotaLease:
        self._require_fence(fencing_token)
        if self.state == LeaseState.CLOSED:
            raise LeaseUnavailableError("closed lease cannot be quarantined")
        return replace(self, state=LeaseState.QUARANTINED)

    def _require_fence(self, fencing_token: int) -> None:
        if fencing_token != self.fencing_token:
            raise LeaseFenceMismatchError("stale regional lease fencing token")

    @staticmethod
    def _require_aware(value: datetime) -> None:
        if value.tzinfo is None:
            raise RegionalQuotaLeaseError("now must be timezone-aware")

    def _hold(self, hold_id: str) -> QuotaLeaseHold | None:
        return next((hold for hold in self.holds if hold.hold_id == hold_id), None)

    def _required_hold(self, hold_id: str) -> QuotaLeaseHold:
        hold = self._hold(hold_id)
        if hold is None:
            raise LeaseSettlementError("unknown regional reservation")
        return hold

    def _replace_hold(self, replacement: QuotaLeaseHold) -> RegionalQuotaLease:
        return replace(
            self,
            holds=tuple(
                replacement if hold.hold_id == replacement.hold_id else hold for hold in self.holds
            ),
        )


def bounded_lease_grant_microdollars(
    *,
    available_microdollars: int,
    requested_microdollars: int,
    per_lease_cap_microdollars: int,
    max_available_basis_points: int,
) -> int:
    """Return a bounded grant; the global transaction must escrow this amount."""

    if (
        min(
            available_microdollars,
            requested_microdollars,
            per_lease_cap_microdollars,
        )
        < 0
    ):
        raise RegionalQuotaLeaseError("lease grant inputs cannot be negative")
    if not 1 <= max_available_basis_points <= 10_000:
        raise RegionalQuotaLeaseError("max_available_basis_points must be between 1 and 10000")
    fraction_cap = available_microdollars * max_available_basis_points // 10_000
    return min(requested_microdollars, per_lease_cap_microdollars, fraction_cap)


def _utc_now() -> datetime:
    return datetime.now(UTC)
