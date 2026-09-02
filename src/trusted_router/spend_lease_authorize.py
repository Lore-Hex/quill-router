"""Pure authorize-path decisions for bound spend leases (unit 2).

Storage and clocks are inputs.  In particular, this module never imports a
cloud SDK and never logs or exposes a lease token while classifying a loss.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from trusted_router.spend_lease_state import SpendLease, SpendLeaseRefusalReason

NoLeaseReason = Literal[
    "no_idempotency_key",
    "escrow_headroom",
    "scope_arbitrated",
    "predecessor_limit",
    "lease_transferred",
    "lease_expired",
    "stale_advisory",
    "ledger_unavailable",
    "window_open",
    "mint_lost",
]


class SpendLeaseArbitrationConflict(RuntimeError):
    """A foreign BOUND won; retry only in a fresh Spanner transaction."""


class SpendLeaseMintLost(RuntimeError):
    """Recovery or the statement-time expiry guard fenced this mint."""


class SpendLeaseContractError(RuntimeError):
    """Persisted fence/arbitration state violates the protocol contract."""


class FenceOutcome(StrEnum):
    LOST_RACE = "lost_race"
    STALE_ADVISORY = "stale_advisory"
    COUNT_EXHAUSTED = "count_exhausted"
    WINDOW_OPEN = "window_open"
    MISSING_OR_CORRUPT = "missing_or_corrupt"
    CONTRACT_VIOLATION = "contract_violation"


@dataclass(frozen=True, slots=True)
class FenceView:
    gen: int | None
    open_predecessor_count: int | None
    active_lease_id: str | None
    active_lease_valid: bool | None


@dataclass(frozen=True, slots=True)
class FenceDecision:
    outcome: FenceOutcome
    reason: NoLeaseReason | None


def derive_candidate_lease_id(
    key_hash: str,
    boot_kid: str,
    gen: int,
    creating_authorization_id: str,
) -> str:
    """Decision 46's stable, attempt-unique candidate identity."""

    if not key_hash or not boot_kid or gen <= 0 or not creating_authorization_id:
        raise ValueError("candidate lease identity fields must be non-empty and positive")
    material = "\0".join((key_hash, boot_kid, str(gen), creating_authorization_id))
    return hashlib.sha256(material.encode()).hexdigest()


def classify_fence_loss(
    *,
    observed_gen: int,
    incumbent_mark_count: int,
    predecessor_limit: int,
    window_closed: bool,
    authoritative_exhaustion: bool,
    statement_window_open: bool,
    current: FenceView | None,
) -> FenceDecision:
    """Decision 45's precedence-ordered zero-row truth table."""

    if not statement_window_open:
        raise SpendLeaseMintLost("candidate expiry guard rejected the fence update")
    if (
        current is None
        or current.gen is None
        or current.open_predecessor_count is None
        or current.active_lease_id is None
        or current.active_lease_valid is None
        or current.gen < observed_gen
        or current.open_predecessor_count < 0
        or current.open_predecessor_count > predecessor_limit
    ):
        return FenceDecision(FenceOutcome.MISSING_OR_CORRUPT, None)
    if current.gen > observed_gen:
        if current.active_lease_valid:
            return FenceDecision(FenceOutcome.LOST_RACE, "lease_transferred")
        return FenceDecision(FenceOutcome.STALE_ADVISORY, "stale_advisory")
    if current.open_predecessor_count + incumbent_mark_count > predecessor_limit:
        return FenceDecision(FenceOutcome.COUNT_EXHAUSTED, "predecessor_limit")
    if not window_closed and not authoritative_exhaustion:
        return FenceDecision(FenceOutcome.WINDOW_OPEN, "window_open")
    return FenceDecision(FenceOutcome.CONTRACT_VIOLATION, None)


PresentedRoute = Literal["reuse", "successor", "ordinary"]


@dataclass(frozen=True, slots=True)
class PresentedDecision:
    route: PresentedRoute
    reason: NoLeaseReason | None = None
    authoritative_exhaustion: bool = False


def route_local_presented(
    local: SpendLease,
    *,
    now: datetime,
) -> PresentedDecision:
    """Decision 47 Table A, evaluated from local state and deadline."""

    from trusted_router.spend_lease_state import SpendLeaseState

    if local.state == SpendLeaseState.ACTIVE:
        if now < local.expires_at + local.skew:
            return PresentedDecision("reuse")
        return PresentedDecision("successor")
    if local.state == SpendLeaseState.TOMBSTONED:
        return PresentedDecision("successor", authoritative_exhaustion=True)
    if local.state in {SpendLeaseState.DRAINING, SpendLeaseState.CLOSED}:
        return PresentedDecision("successor")
    raise SpendLeaseContractError(f"unknown local lease state: {local.state!r}")


def route_local_refusal(reason: SpendLeaseRefusalReason) -> PresentedDecision:
    """Decision 47 A2--A5 for a typed allocation refusal."""

    if reason == SpendLeaseRefusalReason.WINDOW_NOT_ELAPSED:
        return PresentedDecision("ordinary", "window_open")
    if reason == SpendLeaseRefusalReason.EXHAUSTED:
        return PresentedDecision("successor", authoritative_exhaustion=True)
    if reason == SpendLeaseRefusalReason.FROZEN_TOMBSTONED:
        return PresentedDecision("successor", authoritative_exhaustion=True)
    if reason in {
        SpendLeaseRefusalReason.FROZEN_DRAINING,
        SpendLeaseRefusalReason.CLOSED,
        SpendLeaseRefusalReason.WINDOW_EXPIRED,
    }:
        return PresentedDecision("successor")
    raise SpendLeaseContractError(f"unknown refusal reason: {reason!r}")


@dataclass(frozen=True, slots=True)
class GlobalLeaseView:
    state: Literal["ACTIVE", "DRAINING", "TOMBSTONED", "CLOSED"]
    expires_at: datetime
    skew_seconds: int


def route_missing_local(
    global_lease: GlobalLeaseView | None,
    *,
    now: datetime,
) -> PresentedDecision:
    """Decision 47 Table B, deadline first and state second."""

    if global_lease is None:
        return PresentedDecision("ordinary", "ledger_unavailable")
    if now >= global_lease.expires_at + timedelta(seconds=global_lease.skew_seconds):
        return PresentedDecision("successor", authoritative_exhaustion=True)
    if global_lease.state in {"CLOSED", "TOMBSTONED"}:
        return PresentedDecision("successor", authoritative_exhaustion=True)
    return PresentedDecision("ordinary", "ledger_unavailable")
