"""Durable, regional Bigtable adapter for the spend-lease state machine.

The pure state machine owns every transition and its replay classification.
This adapter owns only serialization and single-row compare-and-swap.  In
particular, a typed replay is returned unchanged and never rewritten.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from time import sleep
from typing import Any, Protocol, TypeVar, cast

from google.api_core import exceptions as google_exceptions

from trusted_router.spend_lease_state import (
    AllocateResult,
    AllocationState,
    AuthorizationObservation,
    AuthorizationOutcome,
    AuthorizationView,
    BindingAbsenceProof,
    BindingState,
    BindingTuple,
    BoundProof,
    ClaimProof,
    CommittedRowAbsentProof,
    ConflictingBound,
    ConflictingClaim,
    ContradictionProof,
    Created,
    LeaseAllocationTransition,
    LeaseTransition,
    RecoveryProof,
    RowBindingMismatch,
    SpendLease,
    SpendLeaseAllocation,
    SpendLeaseState,
    TerminalSource,
)

_FAMILY = "lease"
_STATE_COLUMN = b"state"
_VERSION_COLUMN = b"version"
_MAX_CAS_ATTEMPTS = 16
_MAX_CAS_JITTER_SECONDS = 0.025
_ISO_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?(?P<seconds>\d+(?:\.\d{1,6})?)S$"
)


class SpendLeaseLedgerError(RuntimeError):
    """The durable spend-lease ledger could not safely complete an operation."""


class SpendLeaseLedgerUnprovisioned(SpendLeaseLedgerError):
    """The configured Bigtable table or fixed app profile does not exist."""

    def __init__(self, *, table_id: str, profile: str, region: str) -> None:
        self.table_id = table_id
        self.profile = profile
        self.region = region
        super().__init__(
            "spend lease ledger is unprovisioned "
            f"(table={table_id}, profile={profile}, region={region})"
        )


class SpendLeaseNotFound(SpendLeaseLedgerError):
    """The requested regional spend lease does not exist."""


class SpendLeaseCasExhausted(SpendLeaseLedgerError):
    """The spend-lease row did not converge within the bounded CAS attempts."""


class SpendLeaseVersionError(SpendLeaseLedgerError):
    """A pure transition attempted to reuse or skip a durable CAS version."""


def _is_unprovisioned_bigtable_error(exc: Exception, *, profile: str) -> bool:
    if isinstance(exc, google_exceptions.NotFound):
        return True
    if not isinstance(
        exc,
        (google_exceptions.FailedPrecondition, google_exceptions.InvalidArgument),
    ):
        return False

    message = str(exc).casefold().replace("_", " ").replace("-", " ")
    normalized_profile = profile.casefold().replace("_", " ").replace("-", " ")
    identifies_profile = "app profile" in message or (
        profile != "<unknown>" and normalized_profile in message
    )
    missing_profile = any(
        marker in message
        for marker in ("not found", "does not exist", "unknown", "missing")
    )
    invalid_profile = isinstance(exc, google_exceptions.InvalidArgument) and (
        "invalid" in message
    )
    return identifies_profile and (missing_profile or invalid_profile)


class SpendLeaseLedger(Protocol):
    def supports_region(self, region: str) -> bool: ...

    def health_check(self) -> tuple[str, ...]: ...

    def initialize(self, candidate: SpendLease, *, region: str) -> LeaseTransition: ...

    def get(self, lease_id: str, *, region: str) -> SpendLease | None: ...

    def allocate(
        self,
        authorization_view: AuthorizationView | None,
        lease_id: str,
        *,
        region: str,
        idempotency_scope: str,
        provisional_authorization_id: str,
        request_fingerprint: str,
        allocated_micro: int,
        abandon_after: datetime,
        now: datetime,
    ) -> AllocateResult: ...

    def bind(
        self,
        lease_id: str,
        *,
        region: str,
        expected_provisional_id: str,
        proof: BoundProof,
    ) -> LeaseAllocationTransition: ...

    def compensate(
        self,
        lease_id: str,
        *,
        region: str,
        idempotency_scope: str,
        expected_provisional_id: str,
        claim: ClaimProof | RecoveryProof,
        absence: BindingAbsenceProof,
    ) -> LeaseAllocationTransition: ...

    def abandon(
        self,
        lease_id: str,
        *,
        region: str,
        idempotency_scope: str,
        expected_provisional_id: str,
        claim: ClaimProof,
        absence: BindingAbsenceProof,
        now: datetime,
    ) -> LeaseAllocationTransition: ...

    def mirror(
        self,
        lease_id: str,
        *,
        region: str,
        observation: AuthorizationObservation,
    ) -> LeaseAllocationTransition: ...

    def lost(
        self,
        lease_id: str,
        *,
        region: str,
        proof: CommittedRowAbsentProof,
    ) -> LeaseAllocationTransition: ...

    def quarantine(
        self,
        lease_id: str,
        *,
        region: str,
        idempotency_scope: str,
        proof: ContradictionProof,
    ) -> LeaseAllocationTransition: ...

    def begin_drain(
        self,
        lease_id: str,
        *,
        region: str,
        now: datetime,
    ) -> LeaseTransition: ...

    def tombstone(self, lease_id: str, *, region: str) -> LeaseTransition: ...

    def tombstone_unminted(
        self,
        lease_id: str,
        *,
        region: str,
        proof: RecoveryProof,
    ) -> LeaseTransition: ...

    def close(
        self,
        lease_id: str,
        *,
        region: str,
        now: datetime,
    ) -> LeaseTransition: ...

    def delete(self, lease_id: str, *, region: str) -> None: ...


class _TransitionResult(Protocol):
    @property
    def lease(self) -> SpendLease: ...

    @property
    def replayed(self) -> bool: ...


_ResultT = TypeVar("_ResultT", bound=_TransitionResult)


class BigtableSpendLeaseLedger:
    """One-row CAS ledger routed through fixed, regional app-profile tables."""

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
        filter_factory = Callable[..., Any]
        self._cells_limit_filter: Callable[..., Any] = cast(
            filter_factory, CellsColumnLimitFilter
        )
        self._column_filter: Callable[..., Any] = cast(
            filter_factory, ColumnQualifierRegexFilter
        )
        self._family_filter: Callable[..., Any] = cast(
            filter_factory, FamilyNameRegexFilter
        )
        self._chain_filter: Callable[..., Any] = cast(filter_factory, RowFilterChain)
        self._value_filter: Callable[..., Any] = cast(filter_factory, ValueRegexFilter)

    def supports_region(self, region: str) -> bool:
        return region in self._tables

    def initialize(self, candidate: SpendLease, *, region: str) -> LeaseTransition:
        """Create version zero, or classify an existing row through the pure layer."""

        table = self._table(region)
        row_key = _row_key_for(region, candidate.lease_id)
        _assert_next_version(-1, candidate)
        last_error: Exception | None = None
        for attempt in range(_MAX_CAS_ATTEMPTS):
            existing = self._read_lease(table, row_key)
            if existing is not None:
                return existing.initialize(candidate)

            row = table.row(row_key, filter_=self._version_exists_filter())
            row.set_cell(_FAMILY, _STATE_COLUMN, _serialize_lease(candidate), state=False)
            row.set_cell(_FAMILY, _VERSION_COLUMN, _encode_version(candidate.version), state=False)
            try:
                matched = bool(row.commit())
            except Exception as exc:  # pragma: no cover - remote transport
                # A lost response may follow a durable false-branch mutation.
                last_error = exc
                self._jitter(attempt)
                continue
            if not matched:
                return LeaseTransition(candidate, False)
            # A surviving row matched. Re-read it rather than ever placing
            # initialization mutations on the true branch.
            self._jitter(attempt)
        raise SpendLeaseCasExhausted(
            "spend lease initialization CAS attempts were exhausted"
        ) from last_error

    def get(self, lease_id: str, *, region: str) -> SpendLease | None:
        return self._read_lease(self._table(region), _row_key_for(region, lease_id))

    def health_check(self) -> tuple[str, ...]:
        """Prove a conditional write and strong read through every fixed profile."""

        for region in sorted(self._tables):
            table = self._tables[region]
            table_id = str(getattr(table, "table_id", "<unknown>"))
            profile = str(getattr(table, "_app_profile_id", "<unknown>"))
            row_key = f"health#spend-lease#{region}".encode()
            version = uuid.uuid4().hex.encode("ascii")
            row = table.row(row_key, filter_=self._version_exists_filter())
            for state in (False, True):
                row.set_cell(_FAMILY, _STATE_COLUMN, b'{"status":"ok"}', state=state)
                row.set_cell(_FAMILY, _VERSION_COLUMN, version, state=state)
            try:
                row.commit()
                durable = table.read_row(row_key, filter_=self._state_filter())
            except Exception as exc:  # pragma: no cover - remote transport
                if _is_unprovisioned_bigtable_error(exc, profile=profile):
                    raise SpendLeaseLedgerUnprovisioned(
                        table_id=table_id,
                        profile=profile,
                        region=region,
                    ) from exc
                raise
            if durable is None:
                raise SpendLeaseLedgerError(
                    "spend lease ledger health check write was not durable"
                )
        return tuple(sorted(self._tables))

    def allocate(
        self,
        authorization_view: AuthorizationView | None,
        lease_id: str,
        *,
        region: str,
        idempotency_scope: str,
        provisional_authorization_id: str,
        request_fingerprint: str,
        allocated_micro: int,
        abandon_after: datetime,
        now: datetime,
    ) -> AllocateResult:
        # The strong authorization view is deliberately supplied before the
        # lease ID. This adapter performs no local-first convenience read.
        return self._transition(
            lease_id,
            region=region,
            transition=lambda lease: lease.allocate(
                authorization_view=authorization_view,
                idempotency_scope=idempotency_scope,
                provisional_authorization_id=provisional_authorization_id,
                request_fingerprint=request_fingerprint,
                allocated_micro=allocated_micro,
                abandon_after=abandon_after,
                now=now,
            ),
            should_write=lambda result: isinstance(result, Created) and not result.replayed,
        )

    def bind(
        self,
        lease_id: str,
        *,
        region: str,
        expected_provisional_id: str,
        proof: BoundProof,
    ) -> LeaseAllocationTransition:
        return self._mutating_transition(
            lease_id,
            region=region,
            transition=lambda lease: lease.bind(
                expected_provisional_id=expected_provisional_id,
                proof=proof,
            ),
        )

    def compensate(
        self,
        lease_id: str,
        *,
        region: str,
        idempotency_scope: str,
        expected_provisional_id: str,
        claim: ClaimProof | RecoveryProof,
        absence: BindingAbsenceProof,
    ) -> LeaseAllocationTransition:
        return self._mutating_transition(
            lease_id,
            region=region,
            transition=lambda lease: lease.compensate(
                idempotency_scope=idempotency_scope,
                expected_provisional_id=expected_provisional_id,
                claim=claim,
                absence=absence,
            ),
        )

    def abandon(
        self,
        lease_id: str,
        *,
        region: str,
        idempotency_scope: str,
        expected_provisional_id: str,
        claim: ClaimProof,
        absence: BindingAbsenceProof,
        now: datetime,
    ) -> LeaseAllocationTransition:
        return self._mutating_transition(
            lease_id,
            region=region,
            transition=lambda lease: lease.abandon(
                idempotency_scope=idempotency_scope,
                expected_provisional_id=expected_provisional_id,
                claim=claim,
                absence=absence,
                now=now,
            ),
        )

    def mirror(
        self,
        lease_id: str,
        *,
        region: str,
        observation: AuthorizationObservation,
    ) -> LeaseAllocationTransition:
        return self._mutating_transition(
            lease_id,
            region=region,
            transition=lambda lease: lease.mirror(observation),
        )

    def lost(
        self,
        lease_id: str,
        *,
        region: str,
        proof: CommittedRowAbsentProof,
    ) -> LeaseAllocationTransition:
        return self._mutating_transition(
            lease_id,
            region=region,
            transition=lambda lease: lease.lost(proof),
        )

    def quarantine(
        self,
        lease_id: str,
        *,
        region: str,
        idempotency_scope: str,
        proof: ContradictionProof,
    ) -> LeaseAllocationTransition:
        return self._mutating_transition(
            lease_id,
            region=region,
            transition=lambda lease: lease.quarantine(
                idempotency_scope=idempotency_scope,
                proof=proof,
            ),
        )

    def begin_drain(
        self,
        lease_id: str,
        *,
        region: str,
        now: datetime,
    ) -> LeaseTransition:
        return self._mutating_transition(
            lease_id,
            region=region,
            transition=lambda lease: lease.begin_drain(now=now),
        )

    def tombstone(self, lease_id: str, *, region: str) -> LeaseTransition:
        return self._mutating_transition(
            lease_id,
            region=region,
            transition=SpendLease.tombstone,
        )

    def tombstone_unminted(
        self,
        lease_id: str,
        *,
        region: str,
        proof: RecoveryProof,
    ) -> LeaseTransition:
        return self._mutating_transition(
            lease_id,
            region=region,
            transition=lambda lease: lease.tombstone_unminted(proof),
        )

    def close(
        self,
        lease_id: str,
        *,
        region: str,
        now: datetime,
    ) -> LeaseTransition:
        return self._mutating_transition(
            lease_id,
            region=region,
            transition=lambda lease: lease.close(now=now),
        )

    def delete(self, lease_id: str, *, region: str) -> None:
        """Delete a reconciler-proven retained CLOSED row."""

        table = self._table(region)
        row = table.direct_row(_row_key_for(region, lease_id))
        row.delete()
        try:
            row.commit()
        except Exception as exc:  # pragma: no cover - remote transport
            raise SpendLeaseLedgerError("spend lease row deletion failed") from exc

    def _mutating_transition(
        self,
        lease_id: str,
        *,
        region: str,
        transition: Callable[[SpendLease], _ResultT],
    ) -> _ResultT:
        return self._transition(
            lease_id,
            region=region,
            transition=transition,
            should_write=lambda result: not result.replayed,
        )

    def _transition(
        self,
        lease_id: str,
        *,
        region: str,
        transition: Callable[[SpendLease], _ResultT],
        should_write: Callable[[_ResultT], bool],
    ) -> _ResultT:
        table = self._table(region)
        row_key = _row_key_for(region, lease_id)
        last_error: Exception | None = None
        for attempt in range(_MAX_CAS_ATTEMPTS):
            current = self._read_lease(table, row_key)
            if current is None:
                raise SpendLeaseNotFound("spend lease was not found")
            result = transition(current)
            if not should_write(result):
                return result

            updated = result.lease
            _assert_next_version(current.version, updated)
            row = table.row(
                row_key,
                filter_=self._version_equals_filter(_encode_version(current.version)),
            )
            row.set_cell(_FAMILY, _STATE_COLUMN, _serialize_lease(updated), state=True)
            row.set_cell(_FAMILY, _VERSION_COLUMN, _encode_version(updated.version), state=True)
            try:
                if bool(row.commit()):
                    return result
            except Exception as exc:  # pragma: no cover - remote transport
                # Ambiguous commits are classified by re-reading and invoking
                # the same idempotent pure transition on the next attempt.
                last_error = exc
            self._jitter(attempt)
        raise SpendLeaseCasExhausted(
            "spend lease compare-and-swap attempts were exhausted"
        ) from last_error

    def _read_lease(self, table: Any, row_key: bytes) -> SpendLease | None:
        try:
            row = table.read_row(row_key, filter_=self._state_filter())
            if row is None:
                return None
            state, stored_version = _state_and_version(row)
            lease = _deserialize_lease(state)
            if lease.version != stored_version:
                raise ValueError("state version does not match the CAS version cell")
            return lease
        except SpendLeaseLedgerError:
            raise
        except Exception as exc:
            # A corrupt or incomplete row is unavailable, never an absent row
            # that a caller may safely recreate.
            raise SpendLeaseLedgerError(
                "spend lease row could not be deserialized; treating it as unavailable"
            ) from exc

    def _table(self, region: str) -> Any:
        table = self._tables.get(region)
        if table is None:
            raise SpendLeaseLedgerError(
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
        # Bigtable GC is asynchronous. Select the newest version cell before
        # matching its value so a retained historical cell cannot win a CAS.
        return self._chain_filter(
            [
                self._family_filter(f"^{_FAMILY}$"),
                self._column_filter(b"^version$"),
                self._cells_limit_filter(1),
                self._value_filter(b"^" + re.escape(version) + b"$"),
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

    @staticmethod
    def _jitter(attempt: int) -> None:
        ceiling = min(_MAX_CAS_JITTER_SECONDS, 0.001 * (2**attempt))
        sleep(ceiling * secrets.randbelow(1_000_001) / 1_000_000)


def _row_key_for(region: str, lease_id: str) -> bytes:
    spread = hashlib.sha256(lease_id.encode("utf-8")).hexdigest()[:4]
    return f"{spread}#spend#{region}#{lease_id}".encode()


def _assert_next_version(old_version: int, updated: SpendLease) -> None:
    expected = old_version + 1
    if updated.version != expected:
        raise SpendLeaseVersionError(
            f"spend lease transition produced version {updated.version}; expected {expected}"
        )


def _encode_version(version: int) -> bytes:
    if version < 0:
        raise SpendLeaseVersionError("spend lease CAS version cannot be negative")
    return str(version).encode("ascii")


def _serialize_lease(lease: SpendLease) -> bytes:
    payload = {
        "allocations": [_serialize_allocation(allocation) for allocation in lease.allocations],
        "boot_kid": lease.boot_kid,
        "cap_micro": lease.cap_micro,
        "creating_authorization_id": lease.creating_authorization_id,
        "expires_at": lease.expires_at.astimezone(UTC).isoformat(),
        "gen": lease.gen,
        "key_hash": lease.key_hash,
        "lease_id": lease.lease_id,
        "skew": _serialize_duration(lease.skew),
        "state": lease.state.value,
        "tombstoned_unminted": lease.tombstoned_unminted,
        "version": lease.version,
        "workspace_id": lease.workspace_id,
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _serialize_allocation(allocation: SpendLeaseAllocation) -> dict[str, object]:
    return {
        "abandon_after": allocation.abandon_after.astimezone(UTC).isoformat(),
        "actual_micro": allocation.actual_micro,
        "allocated_micro": allocation.allocated_micro,
        "authorization_id": allocation.authorization_id,
        "authorization_outcome": (
            None
            if allocation.authorization_outcome is None
            else allocation.authorization_outcome.value
        ),
        "binding_state": allocation.binding_state.value,
        "contradiction_proof": _serialize_contradiction(allocation.contradiction_proof),
        "gen": allocation.gen,
        "idempotency_scope": allocation.idempotency_scope,
        "key_hash": allocation.key_hash,
        "lease_id": allocation.lease_id,
        "request_fingerprint": allocation.request_fingerprint,
        "state": allocation.state.value,
        "terminal_source": (
            None if allocation.terminal_source is None else allocation.terminal_source.value
        ),
        "workspace_id": allocation.workspace_id,
    }


def _serialize_contradiction(proof: ContradictionProof | None) -> dict[str, object] | None:
    if proof is None:
        return None
    if isinstance(proof, ConflictingBound):
        return {
            "authorization_id": proof.authorization_id,
            "observed_tuple": _serialize_binding(proof.observed_tuple),
            "type": "conflicting_bound",
        }
    if isinstance(proof, ConflictingClaim):
        return {"provisional_id": proof.provisional_id, "type": "conflicting_claim"}
    return {
        "authorization_id": proof.authorization_id,
        "observed_tuple_or_absent": (
            None
            if proof.observed_tuple_or_absent is None
            else _serialize_binding(proof.observed_tuple_or_absent)
        ),
        "type": "row_binding_mismatch",
    }


def _serialize_binding(binding: BindingTuple) -> dict[str, object]:
    return {
        "allocated_micro": binding.allocated_micro,
        "gen": binding.gen,
        "lease_id": binding.lease_id,
    }


def _deserialize_lease(value: bytes) -> SpendLease:
    payload = _object(json.loads(value.decode("utf-8")), "lease")
    allocations_value = payload["allocations"]
    if not isinstance(allocations_value, list):
        raise ValueError("allocations must be a list")
    return SpendLease(
        lease_id=_string(payload, "lease_id"),
        gen=_integer(payload, "gen"),
        key_hash=_string(payload, "key_hash"),
        boot_kid=_string(payload, "boot_kid"),
        workspace_id=_string(payload, "workspace_id"),
        creating_authorization_id=_string(payload, "creating_authorization_id"),
        cap_micro=_integer(payload, "cap_micro"),
        expires_at=datetime.fromisoformat(_string(payload, "expires_at")),
        skew=_deserialize_duration(_string(payload, "skew")),
        version=_integer(payload, "version"),
        state=SpendLeaseState(_string(payload, "state")),
        allocations=tuple(_deserialize_allocation(item) for item in allocations_value),
        tombstoned_unminted=_boolean(payload, "tombstoned_unminted"),
    )


def _deserialize_allocation(value: object) -> SpendLeaseAllocation:
    payload = _object(value, "allocation")
    binding_state = BindingState(_string(payload, "binding_state"))
    lease_id = _string(payload, "lease_id")
    gen = _integer(payload, "gen")
    allocated_micro = _integer(payload, "allocated_micro")
    authorization_id = _string(payload, "authorization_id")
    idempotency_scope = _string(payload, "idempotency_scope")
    bound_proof = (
        BoundProof(
            idempotency_scope=idempotency_scope,
            authorization_id=authorization_id,
            lease_id=lease_id,
            gen=gen,
            allocated_micro=allocated_micro,
        )
        if binding_state == BindingState.COMMITTED
        else None
    )
    return SpendLeaseAllocation(
        idempotency_scope=idempotency_scope,
        authorization_id=authorization_id,
        request_fingerprint=_string(payload, "request_fingerprint"),
        lease_id=lease_id,
        gen=gen,
        allocated_micro=allocated_micro,
        abandon_after=datetime.fromisoformat(_string(payload, "abandon_after")),
        key_hash=_string(payload, "key_hash"),
        workspace_id=_string(payload, "workspace_id"),
        binding_state=binding_state,
        actual_micro=_optional_integer(payload, "actual_micro"),
        state=AllocationState(_string(payload, "state")),
        terminal_source=_optional_enum(payload, "terminal_source", TerminalSource),
        authorization_outcome=_optional_enum(
            payload,
            "authorization_outcome",
            AuthorizationOutcome,
        ),
        contradiction_proof=_deserialize_contradiction(payload["contradiction_proof"]),
        bound_proof=bound_proof,
    )


def _deserialize_contradiction(value: object) -> ContradictionProof | None:
    if value is None:
        return None
    payload = _object(value, "contradiction_proof")
    discriminator = _string(payload, "type")
    if discriminator == "conflicting_bound":
        return ConflictingBound(
            authorization_id=_string(payload, "authorization_id"),
            observed_tuple=_deserialize_binding(payload["observed_tuple"]),
        )
    if discriminator == "conflicting_claim":
        return ConflictingClaim(provisional_id=_string(payload, "provisional_id"))
    if discriminator == "row_binding_mismatch":
        observed = payload["observed_tuple_or_absent"]
        return RowBindingMismatch(
            authorization_id=_string(payload, "authorization_id"),
            observed_tuple_or_absent=(
                None if observed is None else _deserialize_binding(observed)
            ),
        )
    raise ValueError(f"unknown contradiction_proof discriminator {discriminator!r}")


def _deserialize_binding(value: object) -> BindingTuple:
    payload = _object(value, "binding")
    return BindingTuple(
        lease_id=_string(payload, "lease_id"),
        gen=_integer(payload, "gen"),
        allocated_micro=_integer(payload, "allocated_micro"),
    )


def _serialize_duration(value: timedelta) -> str:
    total_microseconds = (
        value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds
    )
    if total_microseconds < 0:
        raise ValueError("duration cannot be negative")
    days, remainder = divmod(total_microseconds, 86_400_000_000)
    hours, remainder = divmod(remainder, 3_600_000_000)
    minutes, remainder = divmod(remainder, 60_000_000)
    seconds, microseconds = divmod(remainder, 1_000_000)
    seconds_text = str(seconds)
    if microseconds:
        seconds_text += f".{microseconds:06d}".rstrip("0")
    day_text = f"{days}D" if days else ""
    hour_text = f"{hours}H" if hours else ""
    minute_text = f"{minutes}M" if minutes else ""
    return f"P{day_text}T{hour_text}{minute_text}{seconds_text}S"


def _deserialize_duration(value: str) -> timedelta:
    match = _ISO_DURATION.fullmatch(value)
    if match is None:
        raise ValueError("skew must be an ISO-8601 duration")
    seconds_text = match.group("seconds")
    whole_seconds_text, separator, fraction = seconds_text.partition(".")
    microseconds = int(fraction.ljust(6, "0")) if separator else 0
    return timedelta(
        days=int(match.group("days") or 0),
        hours=int(match.group("hours") or 0),
        minutes=int(match.group("minutes") or 0),
        seconds=int(whole_seconds_text),
        microseconds=microseconds,
    )


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{field} must be an object with string keys")
    return cast(dict[str, object], value)


def _string(payload: dict[str, object], field: str) -> str:
    value = payload[field]
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def _integer(payload: dict[str, object], field: str) -> int:
    value = payload[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def _optional_integer(payload: dict[str, object], field: str) -> int | None:
    value = payload[field]
    if value is None:
        return None
    return _integer(payload, field)


def _boolean(payload: dict[str, object], field: str) -> bool:
    value = payload[field]
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


_EnumT = TypeVar("_EnumT", bound=str)


def _optional_enum(
    payload: dict[str, object],
    field: str,
    enum_type: Callable[[str], _EnumT],
) -> _EnumT | None:
    value = payload[field]
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    return enum_type(value)


def _state_and_version(row: Any) -> tuple[bytes, int]:
    try:
        family = row.cells[_FAMILY]
        state = bytes(family[_STATE_COLUMN][0].value)
        version_raw = bytes(family[_VERSION_COLUMN][0].value)
        if not version_raw or not version_raw.isdigit():
            raise ValueError("version cell must be a non-negative decimal integer")
        return state, int(version_raw)
    except (KeyError, IndexError, TypeError, AttributeError, ValueError) as exc:
        raise SpendLeaseLedgerError("spend lease row is incomplete or corrupt") from exc
