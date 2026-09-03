"""Pure state machine for authoritative spend-lease allocations.

Implements Stage B decisions 9–22, 46, and 56 of the spend-lease design
record; each invariant below cites its decision.

This module contains no storage or clock I/O.  It implements design decisions
9--22 and 56: lifetime-monotonic capacity (9), proof-derived allocation terminality
(10, 14, 20, 22, 56), separate grant generation and CAS version (12), the ordered
authorization-first replay table (13), admission/freeze rules (15, 16, 18),
construction-time amount and provenance checks (17), the unified open predicate
(18--20, 22), owner-fenced bind/compensate transitions (21--22), and durable
recovery of never-minted candidates (46).

Only the regional precedent's immutable-record, ``__post_init__`` validation,
and true-replay-before-lifecycle shape is reused.  In particular, refunds never
restore capacity (decision 9) and a local allocation is never consulted before
the caller's durable authorization observation (decision 13, carve-outs iii and
viii).
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TypeAlias


class SpendLeaseRefusalReason(StrEnum):
    FROZEN_DRAINING = "frozen_draining"
    FROZEN_TOMBSTONED = "frozen_tombstoned"
    WINDOW_EXPIRED = "window_expired"
    WINDOW_NOT_ELAPSED = "window_not_elapsed"
    CLOSED = "closed"
    EXHAUSTED = "exhausted"


class SpendLeaseStateError(ValueError):
    """Base class for deterministic state-machine failures."""


class SpendLeaseInvariantError(SpendLeaseStateError):
    """A record cannot represent a valid durable state."""


class SpendLeaseConflictError(SpendLeaseStateError):
    """A transition conflicts with the allocation's durable state."""


class SpendLeaseMonetaryMismatch(SpendLeaseConflictError):
    """A finalized authorization cost exceeds its committed allocation."""

    finalized_cost_microdollars: int
    allocated_micro: int

    def __init__(self, *, finalized_cost_microdollars: int, allocated_micro: int) -> None:
        super().__init__("settled actual must fit inside the allocation")
        self.finalized_cost_microdollars = finalized_cost_microdollars
        self.allocated_micro = allocated_micro


class SpendLeaseProofError(SpendLeaseConflictError):
    """A supplied durable proof does not prove the requested transition."""


class SpendLeaseUnavailableError(SpendLeaseStateError):
    """The lease cannot admit a new allocation."""

    reason: SpendLeaseRefusalReason

    def __init__(self, message: str, *, reason: SpendLeaseRefusalReason) -> None:
        super().__init__(message)
        self.reason = reason


class SpendLeaseExhaustedError(SpendLeaseUnavailableError):
    """The lease lacks monotonic capacity for a new allocation."""

    def __init__(self, message: str) -> None:
        super().__init__(message, reason=SpendLeaseRefusalReason.EXHAUSTED)


def is_authoritative_exhaustion(exc: SpendLeaseUnavailableError) -> bool:
    """Return whether ``exc`` proves authoritative exhaustion (decision 46)."""

    return isinstance(exc, SpendLeaseExhaustedError) or (
        exc.reason == SpendLeaseRefusalReason.FROZEN_TOMBSTONED
    )


class BindingState(StrEnum):
    PROVISIONAL = "provisional"
    COMMITTED = "committed"


class AllocationState(StrEnum):
    RESERVED = "reserved"
    SETTLED = "settled"
    REFUNDED = "refunded"
    ABANDONED = "abandoned"
    QUARANTINED = "quarantined"


class TerminalSource(StrEnum):
    MIRROR = "mirror"
    BINDING_ABSENCE = "binding_absence"
    COMMITTED_LOST = "committed_lost"
    QUARANTINE = "quarantine"


class AuthorizationOutcome(StrEnum):
    SETTLED = "settled"
    REFUNDED = "refunded"
    TERMINAL_NO_OUTCOME = "terminal_no_outcome"
    NONE = "none"
    CONTRADICTION = "contradiction"


class SpendLeaseState(StrEnum):
    ACTIVE = "active"
    DRAINING = "draining"
    TOMBSTONED = "tombstoned"
    CLOSED = "closed"


class AuthorizationBinding(StrEnum):
    """Typed result of the caller's strong read; never a caller boolean."""

    UNBOUND = "unbound"
    BOUND = "bound"
    CLOSED_LEASE = "closed_lease"


class AuthorizationDurability(StrEnum):
    OPEN = "open"
    TERMINAL = "terminal"


class FinalizationOutcome(StrEnum):
    SETTLED = "settled"
    REFUNDED = "refunded"
    REAPED_SNAPSHOT = "reaped_snapshot"

    @property
    def charged(self) -> bool:
        return self in {self.SETTLED, self.REAPED_SNAPSHOT}


class ClaimRegistration(StrEnum):
    CLAIM = "claim"


class AbsenceObservation(StrEnum):
    ABSENT_ROW = "absent_row"
    NON_BINDING_ROW = "non_binding_row"


class RetentionDiscipline(StrEnum):
    TERMINAL_ROW_RETENTION = "terminal_row_retention"


@dataclass(frozen=True, slots=True)
class BindingTuple:
    lease_id: str
    gen: int
    allocated_micro: int

    def __post_init__(self) -> None:
        if not self.lease_id or self.gen <= 0 or self.allocated_micro <= 0:
            raise SpendLeaseInvariantError("binding tuple fields must be positive and non-empty")


@dataclass(frozen=True, slots=True)
class BoundProof:
    idempotency_scope: str
    authorization_id: str
    lease_id: str
    gen: int
    allocated_micro: int

    def __post_init__(self) -> None:
        if not self.idempotency_scope or not self.authorization_id:
            raise SpendLeaseInvariantError("bound proof identity is required")
        BindingTuple(self.lease_id, self.gen, self.allocated_micro)

    @property
    def binding(self) -> BindingTuple:
        return BindingTuple(self.lease_id, self.gen, self.allocated_micro)


@dataclass(frozen=True, slots=True)
class ClaimProof:
    idempotency_scope: str
    provisional_id: str
    registration: ClaimRegistration = ClaimRegistration.CLAIM

    def __post_init__(self) -> None:
        if not self.idempotency_scope or not self.provisional_id:
            raise SpendLeaseInvariantError("claim proof identity is required")
        if self.registration != ClaimRegistration.CLAIM:
            raise SpendLeaseInvariantError("claim proof must carry a CLAIM registration")


@dataclass(frozen=True, slots=True)
class RecoveryProof:
    """A durable control-plane work row committed to the ``recovering`` phase."""

    recovering_at: datetime
    creating_authorization_id: str

    def __post_init__(self) -> None:
        _require_aware(self.recovering_at, "recovering_at")
        if not self.creating_authorization_id:
            raise SpendLeaseInvariantError("recovery proof identity is required")


@dataclass(frozen=True, slots=True)
class BindingAbsenceProof:
    """Durable absence/non-binding observation paired with a committed claim."""

    idempotency_scope: str
    provisional_id: str
    observation: AbsenceObservation
    observed_authorization_id: str | None = None
    observed_tuple: BindingTuple | None = None

    def __post_init__(self) -> None:
        if not self.idempotency_scope or not self.provisional_id:
            raise SpendLeaseInvariantError("binding-absence proof identity is required")
        if self.observation == AbsenceObservation.ABSENT_ROW and (
            self.observed_authorization_id is not None or self.observed_tuple is not None
        ):
            raise SpendLeaseInvariantError("absent-row proof cannot contain an observed row")


@dataclass(frozen=True, slots=True)
class CommittedRowAbsentProof:
    idempotency_scope: str
    authorization_id: str
    retention: RetentionDiscipline = RetentionDiscipline.TERMINAL_ROW_RETENTION

    def __post_init__(self) -> None:
        if not self.idempotency_scope or not self.authorization_id:
            raise SpendLeaseInvariantError("committed-row absence proof identity is required")
        if self.retention != RetentionDiscipline.TERMINAL_ROW_RETENTION:
            raise SpendLeaseInvariantError("unknown authorization-row retention discipline")


@dataclass(frozen=True, slots=True)
class ConflictingBound:
    authorization_id: str
    observed_tuple: BindingTuple

    def __post_init__(self) -> None:
        if not self.authorization_id:
            raise SpendLeaseInvariantError("conflicting authorization ID is required")


@dataclass(frozen=True, slots=True)
class ConflictingClaim:
    provisional_id: str

    def __post_init__(self) -> None:
        if not self.provisional_id:
            raise SpendLeaseInvariantError("conflicting provisional ID is required")


@dataclass(frozen=True, slots=True)
class RowBindingMismatch:
    authorization_id: str
    observed_tuple_or_absent: BindingTuple | None

    def __post_init__(self) -> None:
        if not self.authorization_id:
            raise SpendLeaseInvariantError("row authorization ID is required")


@dataclass(frozen=True, slots=True)
class MonetaryMismatchProof:
    finalized_cost_microdollars: int
    allocated_micro: int

    def __post_init__(self) -> None:
        if self.finalized_cost_microdollars <= self.allocated_micro:
            raise SpendLeaseProofError(
                "monetary mismatch proof requires finalized cost above allocation"
            )


ContradictionProof: TypeAlias = (
    ConflictingBound | ConflictingClaim | RowBindingMismatch | MonetaryMismatchProof
)


@dataclass(frozen=True, slots=True)
class AuthorizationView:
    """Strong-read view used by the ordered replay table (decision 13)."""

    idempotency_scope: str
    authorization_id: str
    request_fingerprint: str
    key_hash: str
    workspace_id: str
    binding: AuthorizationBinding
    lease_id: str | None = None
    gen: int | None = None
    allocated_micro: int | None = None

    def __post_init__(self) -> None:
        if not all(
            (
                self.idempotency_scope,
                self.authorization_id,
                self.request_fingerprint,
                self.key_hash,
                self.workspace_id,
            )
        ):
            raise SpendLeaseInvariantError("authorization view identity fields are required")
        typed_binding = (self.lease_id, self.gen, self.allocated_micro)
        if self.binding == AuthorizationBinding.UNBOUND:
            if typed_binding != (None, None, None):
                raise SpendLeaseInvariantError("unbound authorization cannot carry a binding")
        elif any(value is None for value in typed_binding):
            raise SpendLeaseInvariantError("bound authorization requires every binding field")
        else:
            BindingTuple(self.lease_id or "", self.gen or 0, self.allocated_micro or 0)


@dataclass(frozen=True, slots=True)
class AuthorizationObservation:
    """Typed, durable authorization observation used only for MIRROR."""

    idempotency_scope: str
    authorization_id: str
    request_fingerprint: str
    lease_id: str
    gen: int
    allocated_micro: int
    key_hash: str
    workspace_id: str
    durability: AuthorizationDurability
    finalization_outcome: FinalizationOutcome | None = None
    finalized_cost_microdollars: int | None = None

    def __post_init__(self) -> None:
        if not all(
            (
                self.idempotency_scope,
                self.authorization_id,
                self.request_fingerprint,
                self.lease_id,
                self.key_hash,
                self.workspace_id,
            )
        ):
            raise SpendLeaseInvariantError("authorization observation identity is required")
        BindingTuple(self.lease_id, self.gen, self.allocated_micro)
        if self.durability == AuthorizationDurability.OPEN and (
            self.finalization_outcome is not None
            or self.finalized_cost_microdollars is not None
        ):
            raise SpendLeaseInvariantError("open authorization cannot carry finalization facts")
        if self.finalized_cost_microdollars is not None and self.finalized_cost_microdollars < 0:
            raise SpendLeaseInvariantError("finalized cost cannot be negative")


@dataclass(frozen=True, slots=True)
class AllocationTransition:
    allocation: SpendLeaseAllocation
    replayed: bool


@dataclass(frozen=True, slots=True)
class SpendLeaseAllocation:
    idempotency_scope: str
    authorization_id: str
    request_fingerprint: str
    lease_id: str
    gen: int
    allocated_micro: int
    abandon_after: datetime
    key_hash: str
    workspace_id: str
    binding_state: BindingState = BindingState.PROVISIONAL
    actual_micro: int | None = None
    state: AllocationState = AllocationState.RESERVED
    terminal_source: TerminalSource | None = None
    authorization_outcome: AuthorizationOutcome | None = None
    contradiction_proof: ContradictionProof | None = None
    bound_proof: InitVar[BoundProof | None] = None

    def __post_init__(self, bound_proof: BoundProof | None) -> None:
        # Decision 17: identity and amount corruption must fail construction.
        if not all(
            (
                self.idempotency_scope,
                self.authorization_id,
                self.request_fingerprint,
                self.lease_id,
                self.key_hash,
                self.workspace_id,
            )
        ):
            raise SpendLeaseInvariantError("allocation identity fields are required")
        if self.gen <= 0:
            raise SpendLeaseInvariantError("allocation generation must be positive")
        if self.allocated_micro <= 0:
            raise SpendLeaseInvariantError("allocated_micro must be positive")
        _require_aware(self.abandon_after, "abandon_after")

        # Decision 17: NULL and zero are distinct; only SETTLED has an actual.
        if (self.actual_micro is not None) != (self.state == AllocationState.SETTLED):
            raise SpendLeaseInvariantError("actual_micro is non-null exactly for SETTLED")
        if self.actual_micro is not None and not 0 <= self.actual_micro <= self.allocated_micro:
            raise SpendLeaseInvariantError("settled actual must fit inside the allocation")

        # Decision 17: terminal source and outcome are both NULL exactly while RESERVED.
        reserved = self.state == AllocationState.RESERVED
        if (self.terminal_source is None) != reserved:
            raise SpendLeaseInvariantError("terminal_source is null exactly for RESERVED")
        if (self.authorization_outcome is None) != reserved:
            raise SpendLeaseInvariantError("authorization_outcome is null exactly for RESERVED")

        # Decision 17: enforce the full state/source/outcome equivalence, not implications.
        expected_pairs: dict[AllocationState, frozenset[tuple[TerminalSource, AuthorizationOutcome]]] = {
            AllocationState.SETTLED: frozenset(
                {(TerminalSource.MIRROR, AuthorizationOutcome.SETTLED)}
            ),
            AllocationState.REFUNDED: frozenset(
                {
                    (TerminalSource.MIRROR, AuthorizationOutcome.REFUNDED),
                    (TerminalSource.BINDING_ABSENCE, AuthorizationOutcome.NONE),
                }
            ),
            AllocationState.ABANDONED: frozenset(
                {
                    (TerminalSource.MIRROR, AuthorizationOutcome.TERMINAL_NO_OUTCOME),
                    (TerminalSource.COMMITTED_LOST, AuthorizationOutcome.TERMINAL_NO_OUTCOME),
                    (TerminalSource.BINDING_ABSENCE, AuthorizationOutcome.NONE),
                }
            ),
            AllocationState.QUARANTINED: frozenset(
                {(TerminalSource.QUARANTINE, AuthorizationOutcome.CONTRADICTION)}
            ),
        }
        if not reserved and (self.terminal_source, self.authorization_outcome) not in expected_pairs[
            self.state
        ]:
            raise SpendLeaseInvariantError("allocation state/source/outcome provenance conflicts")

        # Decisions 17 and 22: contradiction provenance exists only for quarantine.
        if (self.contradiction_proof is not None) != (
            self.state == AllocationState.QUARANTINED
        ):
            raise SpendLeaseInvariantError("contradiction proof exists exactly for QUARANTINED")
        if isinstance(self.contradiction_proof, (ConflictingBound, ConflictingClaim)) and (
            self.binding_state != BindingState.PROVISIONAL
        ):
            raise SpendLeaseInvariantError("phase-two contradictions retain PROVISIONAL binding")
        if isinstance(self.contradiction_proof, RowBindingMismatch) and (
            self.binding_state != BindingState.COMMITTED
        ):
            raise SpendLeaseInvariantError("row-binding contradiction retains COMMITTED binding")
        if isinstance(self.contradiction_proof, MonetaryMismatchProof) and (
            self.binding_state != BindingState.COMMITTED
        ):
            raise SpendLeaseInvariantError("monetary contradiction retains COMMITTED binding")

        # Decisions 17, 21, 22: provenance constrains binding, and COMMITTED needs proof.
        if self.terminal_source == TerminalSource.MIRROR and (
            self.binding_state != BindingState.COMMITTED
        ):
            raise SpendLeaseInvariantError("MIRROR terminal state requires COMMITTED binding")
        if self.terminal_source == TerminalSource.BINDING_ABSENCE and (
            self.binding_state != BindingState.PROVISIONAL
        ):
            raise SpendLeaseInvariantError("BINDING_ABSENCE requires PROVISIONAL binding")
        if self.terminal_source == TerminalSource.COMMITTED_LOST and (
            self.binding_state != BindingState.COMMITTED
        ):
            raise SpendLeaseInvariantError("COMMITTED_LOST requires COMMITTED binding")
        if self.binding_state == BindingState.COMMITTED:
            if bound_proof is None:
                raise SpendLeaseProofError("COMMITTED allocation requires a BoundProof")
            self._validate_bound_proof(bound_proof)
        elif bound_proof is not None:
            raise SpendLeaseProofError("PROVISIONAL allocation cannot consume a BoundProof")

    @property
    def binding(self) -> BindingTuple:
        return BindingTuple(self.lease_id, self.gen, self.allocated_micro)

    @property
    def is_open(self) -> bool:
        # Decisions 5, 18--20, 22: this is the single close predicate definition.
        return self.state in {AllocationState.RESERVED, AllocationState.QUARANTINED}

    def bind(self, *, expected_provisional_id: str, proof: BoundProof) -> AllocationTransition:
        # Decisions 21(c), 22: validate every proof field before owner/state handling.
        self._validate_bound_proof(proof, validate_authorization=False)
        if self.binding_state == BindingState.COMMITTED:
            if self.authorization_id != proof.authorization_id:
                raise SpendLeaseConflictError("allocation is committed to another authorization")
            return AllocationTransition(self, True)
        if self.state != AllocationState.RESERVED:
            raise SpendLeaseConflictError("only a reserved allocation can bind")
        if self.authorization_id != expected_provisional_id:
            raise SpendLeaseConflictError("stale provisional allocation owner")
        committed = replace(
            self,
            authorization_id=proof.authorization_id,
            binding_state=BindingState.COMMITTED,
            bound_proof=proof,
        )
        return AllocationTransition(committed, False)

    def compensate(
        self,
        *,
        expected_provisional_id: str,
        claim: ClaimProof | RecoveryProof,
        absence: BindingAbsenceProof,
        lease_creating_authorization_id: str | None = None,
    ) -> AllocationTransition:
        # Decisions 10, 15, 21(d/g), 22, 46: durable non-binding proof, owner fenced.
        self._validate_binding_absence(
            expected_provisional_id,
            claim,
            absence,
            lease_creating_authorization_id=lease_creating_authorization_id,
        )
        if self.state == AllocationState.REFUNDED and (
            self.terminal_source == TerminalSource.BINDING_ABSENCE
        ):
            return AllocationTransition(self, True)
        self._require_provisional_owner(expected_provisional_id)
        refunded = replace(
            self,
            state=AllocationState.REFUNDED,
            terminal_source=TerminalSource.BINDING_ABSENCE,
            authorization_outcome=AuthorizationOutcome.NONE,
        )
        return AllocationTransition(refunded, False)

    def abandon(
        self,
        *,
        expected_provisional_id: str,
        claim: ClaimProof,
        absence: BindingAbsenceProof,
        now: datetime,
    ) -> AllocationTransition:
        # Decisions 10, 20, 21(g): a timer gates a proof-bearing transition only.
        _require_aware(now, "now")
        self._validate_binding_absence(expected_provisional_id, claim, absence)
        if self.state == AllocationState.ABANDONED and (
            self.terminal_source == TerminalSource.BINDING_ABSENCE
        ):
            return AllocationTransition(self, True)
        self._require_provisional_owner(expected_provisional_id)
        if now < self.abandon_after:
            raise SpendLeaseUnavailableError(
                "allocation is not eligible for abandonment",
                reason=SpendLeaseRefusalReason.WINDOW_NOT_ELAPSED,
            )
        abandoned = replace(
            self,
            state=AllocationState.ABANDONED,
            terminal_source=TerminalSource.BINDING_ABSENCE,
            authorization_outcome=AuthorizationOutcome.NONE,
        )
        return AllocationTransition(abandoned, False)

    def mirror(self, observation: AuthorizationObservation) -> AllocationTransition:
        # Decisions 10, 14, 22: MIRROR consumes a matching durable committed observation.
        self._validate_observation(observation)
        if self.binding_state != BindingState.COMMITTED:
            raise SpendLeaseProofError("MIRROR requires a committed allocation binding")
        if observation.durability == AuthorizationDurability.OPEN:
            if self.state != AllocationState.RESERVED:
                raise SpendLeaseConflictError("terminal allocation conflicts with open observation")
            return AllocationTransition(self, True)

        if (
            observation.finalization_outcome is not None
            and observation.finalization_outcome.charged
            and observation.finalized_cost_microdollars is not None
        ):
            if observation.finalized_cost_microdollars > self.allocated_micro:
                raise SpendLeaseMonetaryMismatch(
                    finalized_cost_microdollars=observation.finalized_cost_microdollars,
                    allocated_micro=self.allocated_micro,
                )
            target_state = AllocationState.SETTLED
            target_outcome = AuthorizationOutcome.SETTLED
            actual = observation.finalized_cost_microdollars
        elif observation.finalization_outcome == FinalizationOutcome.REFUNDED:
            target_state = AllocationState.REFUNDED
            target_outcome = AuthorizationOutcome.REFUNDED
            actual = None
        else:
            target_state = AllocationState.ABANDONED
            target_outcome = AuthorizationOutcome.TERMINAL_NO_OUTCOME
            actual = None

        if self.state != AllocationState.RESERVED:
            if (
                self.state == target_state
                and self.terminal_source == TerminalSource.MIRROR
                and self.authorization_outcome == target_outcome
                and self.actual_micro == actual
            ):
                return AllocationTransition(self, True)
            raise SpendLeaseConflictError("different terminal authorization outcome")
        proof = self._current_bound_proof()
        mirrored = replace(
            self,
            state=target_state,
            terminal_source=TerminalSource.MIRROR,
            authorization_outcome=target_outcome,
            actual_micro=actual,
            bound_proof=proof,
        )
        return AllocationTransition(mirrored, False)

    def lost(self, proof: CommittedRowAbsentProof) -> AllocationTransition:
        # Decisions 10 and 20: only retention-proven loss of a COMMITTED row is terminal.
        if proof.idempotency_scope != self.idempotency_scope:
            raise SpendLeaseProofError("committed-row proof scope mismatch")
        if proof.authorization_id != self.authorization_id:
            raise SpendLeaseProofError("committed-row proof authorization mismatch")
        if self.state == AllocationState.ABANDONED and (
            self.terminal_source == TerminalSource.COMMITTED_LOST
        ):
            return AllocationTransition(self, True)
        if self.state != AllocationState.RESERVED or self.binding_state != BindingState.COMMITTED:
            raise SpendLeaseConflictError("COMMITTED_LOST requires reserved committed allocation")
        lost = replace(
            self,
            state=AllocationState.ABANDONED,
            terminal_source=TerminalSource.COMMITTED_LOST,
            authorization_outcome=AuthorizationOutcome.TERMINAL_NO_OUTCOME,
            bound_proof=self._current_bound_proof(),
        )
        return AllocationTransition(lost, False)

    def quarantine(self, proof: ContradictionProof) -> AllocationTransition:
        # Decisions 22 and 56: contradiction proofs retain their binding phase.
        if self.state == AllocationState.QUARANTINED:
            if self.contradiction_proof == proof:
                return AllocationTransition(self, True)
            raise SpendLeaseConflictError("different contradiction proof")
        if self.state != AllocationState.RESERVED:
            raise SpendLeaseConflictError("terminal allocations are absorbing")
        if isinstance(proof, ConflictingBound):
            if self.binding_state != BindingState.PROVISIONAL:
                raise SpendLeaseProofError("ConflictingBound requires PROVISIONAL allocation")
            if proof.observed_tuple == self.binding:
                raise SpendLeaseProofError("matching BOUND_ELSEWHERE must bind, not quarantine")
        elif isinstance(proof, ConflictingClaim):
            if self.binding_state != BindingState.PROVISIONAL:
                raise SpendLeaseProofError("ConflictingClaim requires PROVISIONAL allocation")
            if proof.provisional_id == self.authorization_id:
                raise SpendLeaseProofError("allocation's own claim is not contradictory")
        elif isinstance(proof, RowBindingMismatch):
            if self.binding_state != BindingState.COMMITTED:
                raise SpendLeaseProofError("RowBindingMismatch requires COMMITTED allocation")
            if (
                proof.authorization_id == self.authorization_id
                and proof.observed_tuple_or_absent == self.binding
            ):
                raise SpendLeaseProofError("observed row binding matches the allocation")
        elif isinstance(proof, MonetaryMismatchProof):
            if self.binding_state != BindingState.COMMITTED:
                raise SpendLeaseProofError("MonetaryMismatchProof requires COMMITTED allocation")
            if proof.allocated_micro != self.allocated_micro:
                raise SpendLeaseProofError("monetary mismatch proof allocation amount mismatch")
        else:  # pragma: no cover - closed union, defensive against dynamic callers
            raise SpendLeaseProofError("unknown contradiction proof")
        quarantined = replace(
            self,
            state=AllocationState.QUARANTINED,
            terminal_source=TerminalSource.QUARANTINE,
            authorization_outcome=AuthorizationOutcome.CONTRADICTION,
            contradiction_proof=proof,
            bound_proof=self._current_bound_proof()
            if self.binding_state == BindingState.COMMITTED
            else None,
        )
        return AllocationTransition(quarantined, False)

    def _validate_bound_proof(
        self,
        proof: BoundProof,
        *,
        validate_authorization: bool = True,
    ) -> None:
        if proof.idempotency_scope != self.idempotency_scope:
            raise SpendLeaseProofError("bound proof scope mismatch")
        if proof.binding != self.binding:
            raise SpendLeaseProofError("bound proof tuple mismatch")
        if validate_authorization and proof.authorization_id != self.authorization_id:
            raise SpendLeaseProofError("bound proof authorization mismatch")

    def _validate_binding_absence(
        self,
        expected_provisional_id: str,
        claim: ClaimProof | RecoveryProof,
        absence: BindingAbsenceProof,
        *,
        lease_creating_authorization_id: str | None = None,
    ) -> None:
        if expected_provisional_id != self.authorization_id:
            raise SpendLeaseConflictError("stale provisional allocation owner")
        if isinstance(claim, ClaimProof):
            if claim.idempotency_scope != self.idempotency_scope:
                raise SpendLeaseProofError("claim proof scope mismatch")
            if claim.provisional_id != expected_provisional_id:
                raise SpendLeaseProofError("claim proof provisional owner mismatch")
        else:
            if claim.creating_authorization_id != lease_creating_authorization_id:
                raise SpendLeaseProofError("recovery proof lease identity mismatch")
            if self.authorization_id != lease_creating_authorization_id:
                raise SpendLeaseProofError("recovery proof requires the creating allocation")
        if absence.idempotency_scope != self.idempotency_scope:
            raise SpendLeaseProofError("absence proof scope mismatch")
        if absence.provisional_id != expected_provisional_id:
            raise SpendLeaseProofError("absence proof provisional owner mismatch")
        if (
            absence.observation == AbsenceObservation.NON_BINDING_ROW
            and absence.observed_authorization_id == self.authorization_id
            and absence.observed_tuple == self.binding
        ):
            raise SpendLeaseProofError("observed row is bound to this allocation")

    def _require_provisional_owner(self, expected_provisional_id: str) -> None:
        if self.state != AllocationState.RESERVED:
            raise SpendLeaseConflictError("terminal allocations are absorbing")
        if self.binding_state != BindingState.PROVISIONAL:
            raise SpendLeaseConflictError("committed allocation cannot be compensated")
        if self.authorization_id != expected_provisional_id:
            raise SpendLeaseConflictError("stale provisional allocation owner")

    def _validate_observation(self, observation: AuthorizationObservation) -> None:
        expected = (
            self.idempotency_scope,
            self.authorization_id,
            self.request_fingerprint,
            self.lease_id,
            self.gen,
            self.allocated_micro,
            self.key_hash,
            self.workspace_id,
        )
        observed = (
            observation.idempotency_scope,
            observation.authorization_id,
            observation.request_fingerprint,
            observation.lease_id,
            observation.gen,
            observation.allocated_micro,
            observation.key_hash,
            observation.workspace_id,
        )
        if observed != expected:
            raise SpendLeaseProofError("authorization observation identity or binding mismatch")

    def _current_bound_proof(self) -> BoundProof:
        if self.binding_state != BindingState.COMMITTED:
            raise SpendLeaseProofError("allocation has no committed binding proof")
        return BoundProof(
            idempotency_scope=self.idempotency_scope,
            authorization_id=self.authorization_id,
            lease_id=self.lease_id,
            gen=self.gen,
            allocated_micro=self.allocated_micro,
        )


@dataclass(frozen=True, slots=True)
class LeaseTransition:
    lease: SpendLease
    replayed: bool


@dataclass(frozen=True, slots=True)
class LeaseAllocationTransition:
    lease: SpendLease
    allocation: SpendLeaseAllocation
    replayed: bool


@dataclass(frozen=True, slots=True)
class Created(LeaseAllocationTransition):
    provisional_id: str


@dataclass(frozen=True, slots=True)
class ExistingLocal(LeaseAllocationTransition):
    """Existing local record; intentionally carries no compensation capability."""


@dataclass(frozen=True, slots=True)
class TrueReplay(LeaseAllocationTransition):
    pass


@dataclass(frozen=True, slots=True)
class Mismatch:
    lease: SpendLease
    authorization_id: str
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class UnboundExisting:
    lease: SpendLease
    authorization_id: str
    replayed: bool = True


@dataclass(frozen=True, slots=True)
class ClosedLeaseReplay:
    lease: SpendLease
    authorization_id: str
    replayed: bool = True


AllocateResult: TypeAlias = (
    Created | ExistingLocal | TrueReplay | Mismatch | UnboundExisting | ClosedLeaseReplay
)


@dataclass(frozen=True, slots=True)
class SpendLease:
    lease_id: str
    gen: int
    key_hash: str
    boot_kid: str
    workspace_id: str
    creating_authorization_id: str
    cap_micro: int
    expires_at: datetime
    skew: timedelta
    version: int
    state: SpendLeaseState = SpendLeaseState.ACTIVE
    allocations: tuple[SpendLeaseAllocation, ...] = ()
    tombstoned_unminted: bool = False

    def __post_init__(self) -> None:
        # Decisions 12 and 17: grant generation and local CAS version are distinct.
        if not all(
            (
                self.lease_id,
                self.key_hash,
                self.boot_kid,
                self.workspace_id,
                self.creating_authorization_id,
            )
        ):
            raise SpendLeaseInvariantError("lease identity fields are required")
        if self.gen <= 0:
            raise SpendLeaseInvariantError("lease generation must be positive")
        if self.version < 0:
            raise SpendLeaseInvariantError("CAS version cannot be negative")
        if self.cap_micro <= 0:
            raise SpendLeaseInvariantError("lease cap must be positive")
        _require_aware(self.expires_at, "expires_at")
        if self.skew < timedelta(0):
            raise SpendLeaseInvariantError("lease skew cannot be negative")
        if self.tombstoned_unminted and self.state not in {
            SpendLeaseState.TOMBSTONED,
            SpendLeaseState.CLOSED,
        }:
            raise SpendLeaseInvariantError("unminted tombstone provenance requires a frozen lease")
        scopes = [allocation.idempotency_scope for allocation in self.allocations]
        owners = [allocation.authorization_id for allocation in self.allocations]
        if len(scopes) != len(set(scopes)):
            raise SpendLeaseInvariantError("allocation scopes must be unique within a lease")
        if len(owners) != len(set(owners)):
            raise SpendLeaseInvariantError("allocation authorization owners must be unique")
        for allocation in self.allocations:
            if (
                allocation.lease_id != self.lease_id
                or allocation.gen != self.gen
                or allocation.key_hash != self.key_hash
                or allocation.workspace_id != self.workspace_id
            ):
                raise SpendLeaseInvariantError("allocation does not belong to this lease")
        # Decisions 9 and 17: every historical allocation remains counted forever.
        if self.allocated_micro > self.cap_micro:
            raise SpendLeaseInvariantError("lease allocation total exceeds its cap")

    @property
    def allocated_micro(self) -> int:
        return sum(allocation.allocated_micro for allocation in self.allocations)

    @property
    def available_micro(self) -> int:
        # Decision 9: terminalization never returns capacity.
        return self.cap_micro - self.allocated_micro

    @property
    def open_allocations(self) -> tuple[SpendLeaseAllocation, ...]:
        # Decisions 5, 18--20, 22: RESERVED and QUARANTINED are both open.
        return tuple(allocation for allocation in self.allocations if allocation.is_open)

    def allocate(
        self,
        *,
        authorization_view: AuthorizationView | None,
        idempotency_scope: str,
        provisional_authorization_id: str,
        request_fingerprint: str,
        allocated_micro: int,
        abandon_after: datetime,
        now: datetime,
    ) -> AllocateResult:
        # Decision 13 and carve-out viii: the durable authorization view is classified first.
        _require_aware(now, "now")
        _require_aware(abandon_after, "abandon_after")
        if authorization_view is not None:
            if (
                authorization_view.idempotency_scope != idempotency_scope
                or authorization_view.key_hash != self.key_hash
                or authorization_view.workspace_id != self.workspace_id
            ):
                raise SpendLeaseProofError("authorization view identity mismatch")
            if authorization_view.binding == AuthorizationBinding.UNBOUND:
                return UnboundExisting(self, authorization_view.authorization_id)
            if authorization_view.binding == AuthorizationBinding.CLOSED_LEASE:
                return ClosedLeaseReplay(self, authorization_view.authorization_id)
            existing = self._allocation(idempotency_scope)
            if existing is not None and self._view_matches(authorization_view, existing):
                return TrueReplay(self, existing, True)
            return Mismatch(self, authorization_view.authorization_id)

        existing = self._allocation(idempotency_scope)
        if existing is not None:
            # Decision 34 / 21(h): equality proves this is the creator's same-attempt retry.
            if (
                existing.state == AllocationState.RESERVED
                and existing.binding_state == BindingState.PROVISIONAL
                and existing.authorization_id == provisional_authorization_id
            ):
                return Created(
                    lease=self,
                    allocation=existing,
                    replayed=True,
                    provisional_id=provisional_authorization_id,
                )
            # Decision 21(b): a local CAS loser gets no compensation capability.
            return ExistingLocal(self, existing, True)

        # Decisions 15, 16, 18: only NEW reaches lifecycle and capacity checks.
        if self.state != SpendLeaseState.ACTIVE:
            refusal_reason = {
                SpendLeaseState.DRAINING: SpendLeaseRefusalReason.FROZEN_DRAINING,
                SpendLeaseState.TOMBSTONED: SpendLeaseRefusalReason.FROZEN_TOMBSTONED,
                SpendLeaseState.CLOSED: SpendLeaseRefusalReason.CLOSED,
            }[self.state]
            raise SpendLeaseUnavailableError(
                f"lease is {self.state}", reason=refusal_reason
            )
        if now >= self.expires_at + self.skew:
            raise SpendLeaseUnavailableError(
                "lease admission window has expired",
                reason=SpendLeaseRefusalReason.WINDOW_EXPIRED,
            )
        if allocated_micro <= 0:
            raise SpendLeaseInvariantError("allocated_micro must be positive")
        if allocated_micro > self.available_micro:
            raise SpendLeaseExhaustedError("spend lease has insufficient capacity")
        allocation = SpendLeaseAllocation(
            idempotency_scope=idempotency_scope,
            authorization_id=provisional_authorization_id,
            request_fingerprint=request_fingerprint,
            lease_id=self.lease_id,
            gen=self.gen,
            allocated_micro=allocated_micro,
            abandon_after=abandon_after,
            key_hash=self.key_hash,
            workspace_id=self.workspace_id,
        )
        lease = replace(
            self,
            allocations=(*self.allocations, allocation),
            version=self.version + 1,
        )
        return Created(
            lease=lease,
            allocation=allocation,
            replayed=False,
            provisional_id=provisional_authorization_id,
        )

    def initialize(self, candidate: SpendLease) -> LeaseTransition:
        """Replay the same lease identity or reject a corrupt re-initialization."""

        if self._initialization_identity != candidate._initialization_identity:
            raise SpendLeaseInvariantError(
                "spend lease initialization identity mismatch is corruption"
            )
        return LeaseTransition(self, True)

    def bind(self, *, expected_provisional_id: str, proof: BoundProof) -> LeaseAllocationTransition:
        allocation = self._required_allocation(proof.idempotency_scope)
        transition = allocation.bind(expected_provisional_id=expected_provisional_id, proof=proof)
        return self._apply_allocation(transition)

    def compensate(
        self,
        *,
        idempotency_scope: str,
        expected_provisional_id: str,
        claim: ClaimProof | RecoveryProof,
        absence: BindingAbsenceProof,
    ) -> LeaseAllocationTransition:
        allocation = self._required_allocation(idempotency_scope)
        transition = allocation.compensate(
            expected_provisional_id=expected_provisional_id,
            claim=claim,
            absence=absence,
            lease_creating_authorization_id=self.creating_authorization_id,
        )
        return self._apply_allocation(transition)

    def abandon(
        self,
        *,
        idempotency_scope: str,
        expected_provisional_id: str,
        claim: ClaimProof,
        absence: BindingAbsenceProof,
        now: datetime,
    ) -> LeaseAllocationTransition:
        allocation = self._required_allocation(idempotency_scope)
        transition = allocation.abandon(
            expected_provisional_id=expected_provisional_id,
            claim=claim,
            absence=absence,
            now=now,
        )
        return self._apply_allocation(transition)

    def mirror(self, observation: AuthorizationObservation) -> LeaseAllocationTransition:
        allocation = self._required_allocation(observation.idempotency_scope)
        return self._apply_allocation(allocation.mirror(observation))

    def lost(self, proof: CommittedRowAbsentProof) -> LeaseAllocationTransition:
        allocation = self._required_allocation(proof.idempotency_scope)
        return self._apply_allocation(allocation.lost(proof))

    def quarantine(
        self, *, idempotency_scope: str, proof: ContradictionProof
    ) -> LeaseAllocationTransition:
        allocation = self._required_allocation(idempotency_scope)
        return self._apply_allocation(allocation.quarantine(proof))

    def begin_drain(self, *, now: datetime) -> LeaseTransition:
        # Decision 18: time enters DRAINING only at the admission boundary.
        _require_aware(now, "now")
        if self.state == SpendLeaseState.CLOSED:
            return LeaseTransition(self, True)
        if self.state == SpendLeaseState.DRAINING:
            return LeaseTransition(self, True)
        if self.state != SpendLeaseState.ACTIVE:
            refusal_reason = {
                SpendLeaseState.TOMBSTONED: SpendLeaseRefusalReason.FROZEN_TOMBSTONED,
            }[self.state]
            raise SpendLeaseUnavailableError(
                f"lease is {self.state}", reason=refusal_reason
            )
        if now < self.expires_at + self.skew:
            raise SpendLeaseUnavailableError(
                "lease admission window has not elapsed",
                reason=SpendLeaseRefusalReason.WINDOW_NOT_ELAPSED,
            )
        return LeaseTransition(replace(self, state=SpendLeaseState.DRAINING, version=self.version + 1), False)

    def tombstone(self) -> LeaseTransition:
        # Decision 18: trigger-refund freezes ACTIVE/DRAINING; CLOSED is absorbing.
        if self.state in {SpendLeaseState.TOMBSTONED, SpendLeaseState.CLOSED}:
            return LeaseTransition(self, True)
        return LeaseTransition(
            replace(self, state=SpendLeaseState.TOMBSTONED, version=self.version + 1),
            False,
        )

    def tombstone_unminted(self, proof: RecoveryProof) -> LeaseTransition:
        """Decision 46 step 4b — closes a never-minted candidate's local row so a
        lagging producer's late ``allocate()`` is refused; serialized against
        ``allocate()`` by the row CAS.
        """

        if proof.creating_authorization_id != self.creating_authorization_id:
            raise SpendLeaseProofError("recovery proof lease identity mismatch")
        if self.state == SpendLeaseState.TOMBSTONED and self.tombstoned_unminted:
            return LeaseTransition(self, True)
        if self.state != SpendLeaseState.ACTIVE:
            raise SpendLeaseConflictError("unminted tombstone requires an active lease")
        if self.open_allocations:
            raise SpendLeaseConflictError("unminted tombstone requires terminal allocations")
        return LeaseTransition(
            replace(
                self,
                state=SpendLeaseState.TOMBSTONED,
                version=self.version + 1,
                tombstoned_unminted=True,
            ),
            False,
        )

    def close(self, *, now: datetime) -> LeaseTransition:
        # Decisions 5, 18, 19, 22: two guards, using the one open predicate.
        _require_aware(now, "now")
        if self.state == SpendLeaseState.CLOSED:
            return LeaseTransition(self, True)
        if self.state not in {SpendLeaseState.DRAINING, SpendLeaseState.TOMBSTONED}:
            raise SpendLeaseUnavailableError(
                "lease must be frozen before close",
                reason=SpendLeaseRefusalReason.CLOSED,
            )
        if now < self.expires_at + self.skew:
            raise SpendLeaseUnavailableError(
                "lease cannot close before expiry plus skew",
                reason=SpendLeaseRefusalReason.WINDOW_NOT_ELAPSED,
            )
        if self.open_allocations:
            raise SpendLeaseUnavailableError(
                "lease still has open allocations",
                reason=SpendLeaseRefusalReason.CLOSED,
            )
        return LeaseTransition(replace(self, state=SpendLeaseState.CLOSED, version=self.version + 1), False)

    def _allocation(self, idempotency_scope: str) -> SpendLeaseAllocation | None:
        return next(
            (
                allocation
                for allocation in self.allocations
                if allocation.idempotency_scope == idempotency_scope
            ),
            None,
        )

    @property
    def _initialization_identity(self) -> tuple[object, ...]:
        return (
            self.lease_id,
            self.gen,
            self.key_hash,
            self.boot_kid,
            self.workspace_id,
            self.creating_authorization_id,
            self.cap_micro,
            self.expires_at,
            self.skew,
        )

    def _required_allocation(self, idempotency_scope: str) -> SpendLeaseAllocation:
        allocation = self._allocation(idempotency_scope)
        if allocation is None:
            raise SpendLeaseConflictError("unknown spend-lease allocation")
        return allocation

    def _apply_allocation(self, transition: AllocationTransition) -> LeaseAllocationTransition:
        if transition.replayed:
            return LeaseAllocationTransition(self, transition.allocation, True)
        lease = replace(
            self,
            allocations=tuple(
                transition.allocation
                if allocation.idempotency_scope == transition.allocation.idempotency_scope
                else allocation
                for allocation in self.allocations
            ),
            version=self.version + 1,
        )
        return LeaseAllocationTransition(lease, transition.allocation, False)

    @staticmethod
    def _view_matches(view: AuthorizationView, allocation: SpendLeaseAllocation) -> bool:
        return (
            view.idempotency_scope,
            view.authorization_id,
            view.request_fingerprint,
            view.lease_id,
            view.gen,
            view.allocated_micro,
            view.key_hash,
            view.workspace_id,
        ) == (
            allocation.idempotency_scope,
            allocation.authorization_id,
            allocation.request_fingerprint,
            allocation.lease_id,
            allocation.gen,
            allocation.allocated_micro,
            allocation.key_hash,
            allocation.workspace_id,
        )


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SpendLeaseInvariantError(f"{field} must be timezone-aware")
