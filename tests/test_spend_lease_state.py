from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from itertools import permutations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from trusted_router.services.regional_quota_leases import (
    LeaseState as RegionalLeaseState,
)
from trusted_router.services.regional_quota_leases import (
    RegionalQuotaLease,
)
from trusted_router.spend_lease_state import (
    AbsenceObservation,
    AllocationState,
    AuthorizationBinding,
    AuthorizationDurability,
    AuthorizationObservation,
    AuthorizationOutcome,
    AuthorizationView,
    BindingAbsenceProof,
    BindingState,
    BindingTuple,
    BoundProof,
    ClaimProof,
    ClosedLeaseReplay,
    CommittedRowAbsentProof,
    ConflictingBound,
    ConflictingClaim,
    Created,
    ExistingLocal,
    FinalizationOutcome,
    Mismatch,
    RowBindingMismatch,
    SpendLease,
    SpendLeaseAllocation,
    SpendLeaseConflictError,
    SpendLeaseExhaustedError,
    SpendLeaseInvariantError,
    SpendLeaseProofError,
    SpendLeaseState,
    SpendLeaseUnavailableError,
    TerminalSource,
    TrueReplay,
    UnboundExisting,
)

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)
EXPIRY = NOW + timedelta(seconds=60)
SKEW = timedelta(seconds=10)
ABANDON_AFTER = NOW + timedelta(hours=2)


def _lease(*, cap: int = 1_000, state: SpendLeaseState = SpendLeaseState.ACTIVE) -> SpendLease:
    return SpendLease(
        lease_id="lease-1",
        gen=7,
        key_hash="key-hash",
        boot_kid="boot-kid",
        workspace_id="workspace-1",
        cap_micro=cap,
        expires_at=EXPIRY,
        skew=SKEW,
        version=11,
        state=state,
    )


def _created(
    lease: SpendLease | None = None,
    *,
    scope: str = "scope-1",
    provisional_id: str = "provisional-1",
    fingerprint: str = "fingerprint-1",
    amount: int = 400,
) -> Created:
    target = _lease() if lease is None else lease
    result = target.allocate(
        authorization_view=None,
        idempotency_scope=scope,
        provisional_authorization_id=provisional_id,
        request_fingerprint=fingerprint,
        allocated_micro=amount,
        abandon_after=ABANDON_AFTER,
        now=NOW,
    )
    assert isinstance(result, Created)
    return result


def _bound_proof(
    allocation: SpendLeaseAllocation, *, authorization_id: str = "authorization-1"
) -> BoundProof:
    return BoundProof(
        idempotency_scope=allocation.idempotency_scope,
        authorization_id=authorization_id,
        lease_id=allocation.lease_id,
        gen=allocation.gen,
        allocated_micro=allocation.allocated_micro,
    )


def _bind(created: Created, *, authorization_id: str = "authorization-1") -> SpendLease:
    return created.lease.bind(
        expected_provisional_id=created.provisional_id,
        proof=_bound_proof(created.allocation, authorization_id=authorization_id),
    ).lease


def _claim(allocation: SpendLeaseAllocation) -> ClaimProof:
    return ClaimProof(allocation.idempotency_scope, allocation.authorization_id)


def _absence(allocation: SpendLeaseAllocation) -> BindingAbsenceProof:
    return BindingAbsenceProof(
        idempotency_scope=allocation.idempotency_scope,
        provisional_id=allocation.authorization_id,
        observation=AbsenceObservation.ABSENT_ROW,
    )


def _observation(
    allocation: SpendLeaseAllocation,
    *,
    durability: AuthorizationDurability = AuthorizationDurability.TERMINAL,
    outcome: FinalizationOutcome | None = FinalizationOutcome.SETTLED,
    actual: int | None = 250,
) -> AuthorizationObservation:
    return AuthorizationObservation(
        idempotency_scope=allocation.idempotency_scope,
        authorization_id=allocation.authorization_id,
        request_fingerprint=allocation.request_fingerprint,
        lease_id=allocation.lease_id,
        gen=allocation.gen,
        allocated_micro=allocation.allocated_micro,
        key_hash=allocation.key_hash,
        workspace_id=allocation.workspace_id,
        durability=durability,
        finalization_outcome=outcome,
        finalized_cost_microdollars=actual,
    )


def _view(
    allocation: SpendLeaseAllocation,
    *,
    binding: AuthorizationBinding = AuthorizationBinding.BOUND,
) -> AuthorizationView:
    if binding == AuthorizationBinding.UNBOUND:
        return AuthorizationView(
            idempotency_scope=allocation.idempotency_scope,
            authorization_id=allocation.authorization_id,
            request_fingerprint=allocation.request_fingerprint,
            key_hash=allocation.key_hash,
            workspace_id=allocation.workspace_id,
            binding=binding,
        )
    return AuthorizationView(
        idempotency_scope=allocation.idempotency_scope,
        authorization_id=allocation.authorization_id,
        request_fingerprint=allocation.request_fingerprint,
        key_hash=allocation.key_hash,
        workspace_id=allocation.workspace_id,
        binding=binding,
        lease_id=allocation.lease_id,
        gen=allocation.gen,
        allocated_micro=allocation.allocated_micro,
    )


def test_capacity_is_lifetime_monotonic_across_refund_and_abandon() -> None:
    first = _created(amount=300)
    refunded = first.lease.compensate(
        idempotency_scope=first.allocation.idempotency_scope,
        expected_provisional_id=first.provisional_id,
        claim=_claim(first.allocation),
        absence=_absence(first.allocation),
    ).lease
    second = _created(
        refunded,
        scope="scope-2",
        provisional_id="provisional-2",
        fingerprint="fingerprint-2",
        amount=200,
    )
    abandoned = second.lease.abandon(
        idempotency_scope=second.allocation.idempotency_scope,
        expected_provisional_id=second.provisional_id,
        claim=_claim(second.allocation),
        absence=_absence(second.allocation),
        now=ABANDON_AFTER,
    ).lease

    assert abandoned.allocated_micro == 500
    assert abandoned.available_micro == 500
    with pytest.raises(SpendLeaseExhaustedError):
        _created(
            abandoned,
            scope="scope-3",
            provisional_id="provisional-3",
            amount=501,
        )


def test_cas_version_advances_without_changing_grant_generation() -> None:
    initial = _lease()
    created = _created(initial)
    bound = _bind(created)
    assert (initial.gen, created.lease.gen, bound.gen) == (7, 7, 7)
    assert (initial.version, created.lease.version, bound.version) == (11, 12, 13)


def test_allocated_positive_and_actual_null_vs_zero_are_construction_invariants() -> None:
    base = _created().allocation
    with pytest.raises(SpendLeaseInvariantError, match="positive"):
        replace(base, allocated_micro=0)
    with pytest.raises(SpendLeaseInvariantError, match="non-null exactly"):
        replace(base, actual_micro=0)
    committed = _bind(_created()).allocations[0]
    settled_zero = committed.mirror(_observation(committed, actual=0)).allocation
    assert settled_zero.actual_micro == 0
    assert settled_zero.state == AllocationState.SETTLED


def test_terminal_source_and_authorization_outcome_equivalence_is_exact() -> None:
    committed = _bind(_created()).allocations[0]
    with pytest.raises(SpendLeaseInvariantError, match="provenance conflicts"):
        replace(
            committed,
            state=AllocationState.REFUNDED,
            terminal_source=TerminalSource.MIRROR,
            authorization_outcome=AuthorizationOutcome.TERMINAL_NO_OUTCOME,
            bound_proof=_bound_proof(committed),
        )


def test_replay_table_is_ordered_disjoint_and_capacity_neutral() -> None:
    created = _created()
    bound = _bind(created)
    allocation = bound.allocations[0]
    before = bound.available_micro

    replay = bound.allocate(
        authorization_view=_view(allocation),
        idempotency_scope=allocation.idempotency_scope,
        provisional_authorization_id="unused",
        request_fingerprint=allocation.request_fingerprint,
        allocated_micro=allocation.allocated_micro,
        abandon_after=ABANDON_AFTER,
        now=EXPIRY + SKEW + timedelta(days=1),
    )
    assert isinstance(replay, TrueReplay)
    assert replay.replayed

    mismatch = bound.allocate(
        authorization_view=replace(_view(allocation), allocated_micro=401),
        idempotency_scope=allocation.idempotency_scope,
        provisional_authorization_id="unused",
        request_fingerprint=allocation.request_fingerprint,
        allocated_micro=999,
        abandon_after=ABANDON_AFTER,
        now=NOW,
    )
    assert isinstance(mismatch, Mismatch)
    assert mismatch.lease.available_micro == before

    unbound = bound.allocate(
        authorization_view=_view(allocation, binding=AuthorizationBinding.UNBOUND),
        idempotency_scope=allocation.idempotency_scope,
        provisional_authorization_id="unused",
        request_fingerprint="different-is-not-tested-before-unbound",
        allocated_micro=999,
        abandon_after=ABANDON_AFTER,
        now=NOW,
    )
    assert isinstance(unbound, UnboundExisting)
    assert len(unbound.lease.allocations) == 1

    closed = bound.allocate(
        authorization_view=_view(allocation, binding=AuthorizationBinding.CLOSED_LEASE),
        idempotency_scope=allocation.idempotency_scope,
        provisional_authorization_id="unused",
        request_fingerprint="unused",
        allocated_micro=999,
        abandon_after=ABANDON_AFTER,
        now=NOW,
    )
    assert isinstance(closed, ClosedLeaseReplay)
    assert closed.lease.available_micro == before


def test_same_provisional_id_kills_always_existing_local_mutant() -> None:
    created = _created()
    capacity_before = created.lease.available_micro
    allocations_before = len(created.lease.allocations)

    replay = created.lease.allocate(
        authorization_view=None,
        idempotency_scope=created.allocation.idempotency_scope,
        provisional_authorization_id=created.provisional_id,
        request_fingerprint=created.allocation.request_fingerprint,
        allocated_micro=created.allocation.allocated_micro,
        abandon_after=ABANDON_AFTER,
        now=NOW,
    )

    assert isinstance(replay, Created)
    assert replay.replayed
    assert replay.provisional_id == created.provisional_id
    assert replay.allocation is created.allocation
    assert replay.lease is created.lease
    assert replay.lease.available_micro == capacity_before
    assert len(replay.lease.allocations) == allocations_before


def test_different_provisional_id_kills_any_reserved_created_mutant() -> None:
    created = _created()
    existing = created.lease.allocate(
        authorization_view=None,
        idempotency_scope=created.allocation.idempotency_scope,
        provisional_authorization_id="racing-provisional",
        request_fingerprint="racing-fingerprint",
        allocated_micro=600,
        abandon_after=ABANDON_AFTER,
        now=NOW,
    )
    assert isinstance(existing, ExistingLocal)
    assert not hasattr(existing, "provisional_id")
    assert existing.lease is created.lease
    assert len(existing.lease.allocations) == 1


def test_same_provisional_id_for_committed_allocation_is_existing_local() -> None:
    created = _created()
    committed = _bind(created, authorization_id=created.provisional_id)
    allocation = committed.allocations[0]

    existing = committed.allocate(
        authorization_view=None,
        idempotency_scope=allocation.idempotency_scope,
        provisional_authorization_id=created.provisional_id,
        request_fingerprint=allocation.request_fingerprint,
        allocated_micro=allocation.allocated_micro,
        abandon_after=ABANDON_AFTER,
        now=NOW,
    )

    assert isinstance(existing, ExistingLocal)
    assert not hasattr(existing, "provisional_id")
    assert existing.lease is committed
    assert len(existing.lease.allocations) == 1


@given(
    first_scope=st.sampled_from(["scope-k1", "scope-k2"]),
    first_amount=st.integers(min_value=1, max_value=499),
)
def test_round_fourteen_scope_identity_prevents_fingerprint_sharing(
    first_scope: str, first_amount: int
) -> None:
    other_scope = "scope-k2" if first_scope == "scope-k1" else "scope-k1"
    first = _created(scope=first_scope, fingerprint="same-body", amount=first_amount)
    second = _created(
        first.lease,
        scope=other_scope,
        provisional_id="other-provisional",
        fingerprint="same-body",
        amount=500 - first_amount,
    )
    assert {item.idempotency_scope for item in second.lease.allocations} == {
        "scope-k1",
        "scope-k2",
    }
    assert len(second.lease.allocations) == 2


def test_bind_committed_to_different_authorization_conflicts() -> None:
    created = _created()
    bound = _bind(created)
    with pytest.raises(SpendLeaseConflictError, match="another authorization"):
        bound.bind(
            expected_provisional_id=created.provisional_id,
            proof=_bound_proof(created.allocation, authorization_id="authorization-2"),
        )


def test_compensate_stale_owner_never_refunds() -> None:
    created = _created()
    with pytest.raises(SpendLeaseConflictError, match="stale"):
        created.lease.compensate(
            idempotency_scope=created.allocation.idempotency_scope,
            expected_provisional_id="stale-owner",
            claim=ClaimProof(created.allocation.idempotency_scope, "stale-owner"),
            absence=BindingAbsenceProof(
                created.allocation.idempotency_scope,
                "stale-owner",
                AbsenceObservation.ABSENT_ROW,
            ),
        )
    assert created.lease.allocations[0].state == AllocationState.RESERVED


def test_committed_allocation_requires_matching_bound_proof_for_construction() -> None:
    provisional = _created().allocation
    with pytest.raises(SpendLeaseProofError, match="requires"):
        replace(provisional, binding_state=BindingState.COMMITTED)
    proof = _bound_proof(provisional)
    committed = replace(
        provisional,
        authorization_id=proof.authorization_id,
        binding_state=BindingState.COMMITTED,
        bound_proof=proof,
    )
    assert committed.binding_state == BindingState.COMMITTED
    with pytest.raises(SpendLeaseProofError, match="tuple mismatch"):
        replace(committed, bound_proof=replace(proof, gen=proof.gen + 1))


def test_matching_bound_elsewhere_binds_and_different_tuple_quarantines() -> None:
    created = _created()
    matching = created.lease.bind(
        expected_provisional_id=created.provisional_id,
        proof=_bound_proof(created.allocation, authorization_id="winner"),
    )
    assert matching.allocation.authorization_id == "winner"
    assert matching.allocation.binding_state == BindingState.COMMITTED

    conflicting = _created(scope="scope-other", provisional_id="provisional-other")
    quarantined = conflicting.lease.quarantine(
        idempotency_scope=conflicting.allocation.idempotency_scope,
        proof=ConflictingBound(
            "winner",
            BindingTuple(
                conflicting.allocation.lease_id,
                conflicting.allocation.gen,
                conflicting.allocation.allocated_micro + 1,
            ),
        ),
    )
    assert quarantined.allocation.state == AllocationState.QUARANTINED
    with pytest.raises(SpendLeaseProofError, match="must bind"):
        conflicting.lease.quarantine(
            idempotency_scope=conflicting.allocation.idempotency_scope,
            proof=ConflictingBound("winner", conflicting.allocation.binding),
        )


def test_all_three_quarantine_transitions_revalidate_state_and_evidence() -> None:
    provisional = _created()
    claimed = provisional.lease.quarantine(
        idempotency_scope=provisional.allocation.idempotency_scope,
        proof=ConflictingClaim("other-provisional"),
    )
    assert claimed.allocation.binding_state == BindingState.PROVISIONAL
    assert claimed.allocation.terminal_source == TerminalSource.QUARANTINE
    with pytest.raises(SpendLeaseProofError, match="not contradictory"):
        provisional.lease.quarantine(
            idempotency_scope=provisional.allocation.idempotency_scope,
            proof=ConflictingClaim(provisional.provisional_id),
        )

    committed = _bind(_created(scope="committed-scope"))
    allocation = committed.allocations[0]
    mismatch = committed.quarantine(
        idempotency_scope=allocation.idempotency_scope,
        proof=RowBindingMismatch(allocation.authorization_id, None),
    )
    assert mismatch.allocation.binding_state == BindingState.COMMITTED
    with pytest.raises(SpendLeaseProofError, match="matches"):
        committed.quarantine(
            idempotency_scope=allocation.idempotency_scope,
            proof=RowBindingMismatch(allocation.authorization_id, allocation.binding),
        )


def test_quarantined_allocation_blocks_close_under_unified_open_predicate() -> None:
    created = _created()
    quarantined = created.lease.quarantine(
        idempotency_scope=created.allocation.idempotency_scope,
        proof=ConflictingClaim("other"),
    ).lease
    frozen = quarantined.tombstone().lease
    assert frozen.open_allocations[0].state == AllocationState.QUARANTINED
    with pytest.raises(SpendLeaseUnavailableError, match="open allocations"):
        frozen.close(now=EXPIRY + SKEW)


@pytest.mark.parametrize("order", list(permutations(("bind", "compensate"))))
def test_round_fifteen_bind_compensate_race_has_exactly_one_winner(
    order: tuple[str, str],
) -> None:
    created = _created()
    lease = created.lease
    successes = 0
    for action in order:
        try:
            if action == "bind":
                lease = lease.bind(
                    expected_provisional_id=created.provisional_id,
                    proof=_bound_proof(created.allocation),
                ).lease
            else:
                lease = lease.compensate(
                    idempotency_scope=created.allocation.idempotency_scope,
                    expected_provisional_id=created.provisional_id,
                    claim=_claim(created.allocation),
                    absence=_absence(created.allocation),
                ).lease
            successes += 1
        except SpendLeaseConflictError:
            pass
    assert successes == 1
    allocation = lease.allocations[0]
    assert not (
        allocation.binding_state == BindingState.COMMITTED
        and allocation.state == AllocationState.REFUNDED
    )


@pytest.mark.parametrize(
    "actions",
    list(permutations(("bind", "compensate", "close"))),
)
def test_round_sixteen_delayed_creator_never_closes_over_bound_open_authorization(
    actions: tuple[str, str, str],
) -> None:
    created = _created()
    lease = created.lease.tombstone().lease
    for action in actions:
        try:
            if action == "bind":
                lease = lease.bind(
                    expected_provisional_id=created.provisional_id,
                    proof=_bound_proof(created.allocation),
                ).lease
            elif action == "compensate":
                lease = lease.compensate(
                    idempotency_scope=created.allocation.idempotency_scope,
                    expected_provisional_id=created.provisional_id,
                    claim=_claim(created.allocation),
                    absence=_absence(created.allocation),
                ).lease
            else:
                lease = lease.close(now=EXPIRY + SKEW).lease
        except SpendLeaseConflictError:
            pass
        except SpendLeaseUnavailableError:
            pass
    allocation = lease.allocations[0]
    if allocation.binding_state == BindingState.COMMITTED and allocation.state == AllocationState.RESERVED:
        assert lease.state != SpendLeaseState.CLOSED


def test_committed_lost_is_refused_for_provisional_and_replays_for_committed() -> None:
    created = _created()
    provisional_proof = CommittedRowAbsentProof(
        created.allocation.idempotency_scope,
        created.allocation.authorization_id,
    )
    with pytest.raises(SpendLeaseConflictError, match="committed"):
        created.lease.lost(provisional_proof)

    committed = _bind(created)
    allocation = committed.allocations[0]
    proof = CommittedRowAbsentProof(allocation.idempotency_scope, allocation.authorization_id)
    lost = committed.lost(proof)
    replay = lost.lease.lost(proof)
    assert lost.allocation.terminal_source == TerminalSource.COMMITTED_LOST
    assert replay.replayed


def test_claim_and_bound_proofs_for_one_scope_cannot_both_win() -> None:
    created = _created()
    bound = _bind(created)
    with pytest.raises(SpendLeaseConflictError, match="stale|committed"):
        bound.compensate(
            idempotency_scope=created.allocation.idempotency_scope,
            expected_provisional_id=created.provisional_id,
            claim=_claim(created.allocation),
            absence=_absence(created.allocation),
        )
    compensated = created.lease.compensate(
        idempotency_scope=created.allocation.idempotency_scope,
        expected_provisional_id=created.provisional_id,
        claim=_claim(created.allocation),
        absence=_absence(created.allocation),
    ).lease
    with pytest.raises(SpendLeaseConflictError, match="reserved"):
        compensated.bind(
            expected_provisional_id=created.provisional_id,
            proof=_bound_proof(created.allocation),
        )


def test_frozen_lease_refuses_new_but_returns_true_replay() -> None:
    created = _created()
    bound = _bind(created)
    allocation = bound.allocations[0]
    frozen = bound.tombstone().lease
    with pytest.raises(SpendLeaseUnavailableError, match="tombstoned"):
        _created(frozen, scope="new-scope", provisional_id="new-provisional")
    replay = frozen.allocate(
        authorization_view=_view(allocation),
        idempotency_scope=allocation.idempotency_scope,
        provisional_authorization_id="unused",
        request_fingerprint=allocation.request_fingerprint,
        allocated_micro=allocation.allocated_micro,
        abandon_after=ABANDON_AFTER,
        now=EXPIRY + SKEW,
    )
    assert isinstance(replay, TrueReplay)


def test_close_refuses_reserved_allocation_and_before_expiry_plus_skew() -> None:
    frozen_empty = _lease().tombstone().lease
    with pytest.raises(SpendLeaseUnavailableError, match="before"):
        frozen_empty.close(now=EXPIRY + SKEW - timedelta(microseconds=1))
    frozen_open = _created().lease.tombstone().lease
    with pytest.raises(SpendLeaseUnavailableError, match="open allocations"):
        frozen_open.close(now=EXPIRY + SKEW)


def test_mirror_requires_committed_terminal_view_and_open_view_leaves_reserved() -> None:
    created = _created()
    with pytest.raises(SpendLeaseProofError, match="committed"):
        created.lease.mirror(_observation(created.allocation))
    committed = _bind(created)
    allocation = committed.allocations[0]
    open_observation = _observation(
        allocation,
        durability=AuthorizationDurability.OPEN,
        outcome=None,
        actual=None,
    )
    unchanged = committed.mirror(open_observation)
    assert unchanged.replayed
    assert unchanged.allocation.state == AllocationState.RESERVED
    with pytest.raises(SpendLeaseConflictError, match="fit inside"):
        committed.mirror(_observation(allocation, actual=allocation.allocated_micro + 1))


def test_terminal_allocations_are_absorbing_and_same_terminal_input_replays() -> None:
    committed = _bind(_created())
    allocation = committed.allocations[0]
    observation = _observation(allocation, actual=100)
    settled = committed.mirror(observation)
    assert settled.lease.mirror(observation).replayed
    with pytest.raises(SpendLeaseConflictError):
        settled.lease.quarantine(
            idempotency_scope=allocation.idempotency_scope,
            proof=RowBindingMismatch(allocation.authorization_id, None),
        )
    with pytest.raises(SpendLeaseConflictError):
        settled.lease.lost(
            CommittedRowAbsentProof(allocation.idempotency_scope, allocation.authorization_id)
        )


def test_closed_lease_is_absorbing_for_lifecycle_transitions() -> None:
    closed = _lease().tombstone().lease.close(now=EXPIRY + SKEW).lease
    assert closed.close(now=EXPIRY + SKEW).replayed
    assert closed.tombstone().replayed
    assert closed.begin_drain(now=EXPIRY + SKEW).replayed
    assert closed.state == SpendLeaseState.CLOSED


@pytest.mark.parametrize(
    ("outcome", "actual", "state", "authorization_outcome"),
    [
        (FinalizationOutcome.SETTLED, 0, AllocationState.SETTLED, AuthorizationOutcome.SETTLED),
        (FinalizationOutcome.REFUNDED, 0, AllocationState.REFUNDED, AuthorizationOutcome.REFUNDED),
        (None, None, AllocationState.ABANDONED, AuthorizationOutcome.TERMINAL_NO_OUTCOME),
        (
            FinalizationOutcome.SETTLED,
            None,
            AllocationState.ABANDONED,
            AuthorizationOutcome.TERMINAL_NO_OUTCOME,
        ),
    ],
)
def test_mirror_mapping_and_same_outcome_replay_are_exact(
    outcome: FinalizationOutcome | None,
    actual: int | None,
    state: AllocationState,
    authorization_outcome: AuthorizationOutcome,
) -> None:
    committed = _bind(_created())
    allocation = committed.allocations[0]
    observation = _observation(allocation, outcome=outcome, actual=actual)
    first = committed.mirror(observation)
    replay = first.lease.mirror(observation)
    assert first.allocation.state == state
    assert first.allocation.authorization_outcome == authorization_outcome
    assert replay.replayed
    if outcome == FinalizationOutcome.SETTLED and actual is not None:
        with pytest.raises(SpendLeaseConflictError, match="different terminal"):
            first.lease.mirror(replace(observation, finalized_cost_microdollars=actual + 1))


def test_abandon_requires_claim_absence_and_deadline_not_timer_alone() -> None:
    created = _created()
    with pytest.raises(TypeError):
        created.allocation.abandon(  # type: ignore[call-arg]
            expected_provisional_id=created.provisional_id,
            now=ABANDON_AFTER,
        )
    with pytest.raises(SpendLeaseUnavailableError, match="not eligible"):
        created.lease.abandon(
            idempotency_scope=created.allocation.idempotency_scope,
            expected_provisional_id=created.provisional_id,
            claim=_claim(created.allocation),
            absence=_absence(created.allocation),
            now=ABANDON_AFTER - timedelta(microseconds=1),
        )


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("idempotency_scope", "wrong-scope"),
        ("lease_id", "wrong-lease"),
        ("gen", 999),
        ("allocated_micro", 399),
    ],
)
def test_bound_proof_revalidates_every_scope_and_tuple_field(field: str, bad_value: object) -> None:
    created = _created()
    proof = replace(_bound_proof(created.allocation), **{field: bad_value})
    with pytest.raises(SpendLeaseProofError, match="scope|tuple"):
        created.allocation.bind(expected_provisional_id=created.provisional_id, proof=proof)


@pytest.mark.parametrize("proof_name", ["claim_scope", "claim_owner", "absence_scope", "absence_owner"])
def test_binding_absence_revalidates_claim_and_observation_identity(proof_name: str) -> None:
    created = _created()
    claim = _claim(created.allocation)
    absence = _absence(created.allocation)
    if proof_name == "claim_scope":
        claim = replace(claim, idempotency_scope="wrong")
    elif proof_name == "claim_owner":
        claim = replace(claim, provisional_id="wrong")
    elif proof_name == "absence_scope":
        absence = replace(absence, idempotency_scope="wrong")
    else:
        absence = replace(absence, provisional_id="wrong")
    with pytest.raises(SpendLeaseProofError, match="scope|owner"):
        created.lease.compensate(
            idempotency_scope=created.allocation.idempotency_scope,
            expected_provisional_id=created.provisional_id,
            claim=claim,
            absence=absence,
        )


def test_binding_absence_rejects_a_row_that_matches_the_allocation_tuple() -> None:
    created = _created()
    matching_row = BindingAbsenceProof(
        created.allocation.idempotency_scope,
        created.provisional_id,
        AbsenceObservation.NON_BINDING_ROW,
        observed_authorization_id=created.provisional_id,
        observed_tuple=created.allocation.binding,
    )
    with pytest.raises(SpendLeaseProofError, match="bound"):
        created.lease.compensate(
            idempotency_scope=created.allocation.idempotency_scope,
            expected_provisional_id=created.provisional_id,
            claim=_claim(created.allocation),
            absence=matching_row,
        )


@pytest.mark.parametrize(
    "field",
    [
        "idempotency_scope",
        "authorization_id",
        "request_fingerprint",
        "lease_id",
        "gen",
        "allocated_micro",
        "key_hash",
        "workspace_id",
    ],
)
def test_mirror_observation_revalidates_every_identity_and_binding_field(field: str) -> None:
    committed = _bind(_created())
    allocation = committed.allocations[0]
    observation = _observation(allocation)
    value: object = 999 if field in {"gen", "allocated_micro"} else "wrong"
    with pytest.raises(SpendLeaseProofError, match="mismatch"):
        allocation.mirror(replace(observation, **{field: value}))


@pytest.mark.parametrize("field", ["idempotency_scope", "authorization_id"])
def test_committed_lost_proof_revalidates_identity(field: str) -> None:
    committed = _bind(_created())
    allocation = committed.allocations[0]
    proof = CommittedRowAbsentProof(allocation.idempotency_scope, allocation.authorization_id)
    with pytest.raises(SpendLeaseProofError, match="mismatch"):
        allocation.lost(replace(proof, **{field: "wrong"}))


def test_authorization_scope_view_wrong_scope_is_rejected_before_local_lookup() -> None:
    created = _created()
    with pytest.raises(SpendLeaseProofError, match="identity"):
        created.lease.allocate(
            authorization_view=replace(_view(created.allocation), idempotency_scope="wrong"),
            idempotency_scope=created.allocation.idempotency_scope,
            provisional_authorization_id="unused",
            request_fingerprint=created.allocation.request_fingerprint,
            allocated_micro=created.allocation.allocated_micro,
            abandon_after=ABANDON_AFTER,
            now=NOW,
        )


@given(
    operations=st.lists(
        st.sampled_from(["allocate-a", "allocate-b", "freeze", "replay-a", "replay-b"]),
        min_size=1,
        max_size=40,
    )
)
def test_round_eleven_over_admission_execution_never_exceeds_allocations(
    operations: list[str],
) -> None:
    lease = _lease(cap=20)
    executions: set[str] = set()
    views: dict[str, AuthorizationView] = {}
    for operation in operations:
        scope = operation[-1] if operation[-1] in {"a", "b"} else ""
        try:
            if operation.startswith("allocate"):
                result = lease.allocate(
                    authorization_view=None,
                    idempotency_scope=scope,
                    provisional_authorization_id=f"provisional-{scope}",
                    request_fingerprint="shared-body",
                    allocated_micro=10,
                    abandon_after=ABANDON_AFTER,
                    now=NOW,
                )
                lease = result.lease
                if isinstance(result, Created):
                    lease = _bind(result, authorization_id=f"authorization-{scope}")
                    allocation = next(a for a in lease.allocations if a.idempotency_scope == scope)
                    views[scope] = _view(allocation)
                    executions.add(scope)
            elif operation == "freeze":
                lease = lease.tombstone().lease
            elif scope in views:
                result = lease.allocate(
                    authorization_view=views[scope],
                    idempotency_scope=scope,
                    provisional_authorization_id="unused",
                    request_fingerprint="shared-body",
                    allocated_micro=10,
                    abandon_after=ABANDON_AFTER,
                    now=EXPIRY + SKEW,
                )
                assert isinstance(result, TrueReplay)
        except SpendLeaseUnavailableError:
            pass
        assert len(executions) * 10 <= lease.allocated_micro <= lease.cap_micro


def test_differential_copied_shape_is_frozen_validated_and_replays_before_lifecycle() -> None:
    regional = RegionalQuotaLease(
        lease_id="regional",
        workspace_id="workspace",
        region="us-east4",
        fencing_token=1,
        granted_microdollars=100,
        expires_at=NOW + timedelta(seconds=1),
    )
    regional_reserved = regional.reserve(
        hold_id="scope",
        fingerprint="fingerprint",
        amount_microdollars=10,
        fencing_token=1,
        now=NOW,
    ).lease
    regional_draining = regional_reserved.begin_drain(fencing_token=1)
    regional_replay = regional_draining.reserve(
        hold_id="scope",
        fingerprint="fingerprint",
        amount_microdollars=10,
        fencing_token=1,
        now=NOW + timedelta(days=1),
    )

    spend_created = _created()
    spend_bound = _bind(spend_created)
    spend_frozen = spend_bound.tombstone().lease
    spend_allocation = spend_frozen.allocations[0]
    spend_replay = spend_frozen.allocate(
        authorization_view=_view(spend_allocation),
        idempotency_scope=spend_allocation.idempotency_scope,
        provisional_authorization_id="unused",
        request_fingerprint=spend_allocation.request_fingerprint,
        allocated_micro=spend_allocation.allocated_micro,
        abandon_after=ABANDON_AFTER,
        now=NOW + timedelta(days=1),
    )

    assert regional_draining.state == RegionalLeaseState.DRAINING
    assert regional_replay.replayed and isinstance(spend_replay, TrueReplay)
    with pytest.raises(FrozenInstanceError):
        spend_frozen.version = 99  # type: ignore[misc]
    with pytest.raises(SpendLeaseInvariantError):
        replace(spend_frozen, cap_micro=spend_frozen.allocated_micro - 1)


def test_carveouts_refund_does_not_restore_capacity_and_authorization_lookup_is_first() -> None:
    created = _created(amount=400)
    refunded = created.lease.compensate(
        idempotency_scope=created.allocation.idempotency_scope,
        expected_provisional_id=created.provisional_id,
        claim=_claim(created.allocation),
        absence=_absence(created.allocation),
    ).lease
    assert refunded.available_micro == 600

    # Unlike the regional local-hold-first precedent, UNBOUND wins even though
    # a conflicting local allocation exists for this scope.
    outcome = refunded.allocate(
        authorization_view=_view(created.allocation, binding=AuthorizationBinding.UNBOUND),
        idempotency_scope=created.allocation.idempotency_scope,
        provisional_authorization_id="different",
        request_fingerprint="different",
        allocated_micro=1,
        abandon_after=ABANDON_AFTER,
        now=NOW,
    )
    assert isinstance(outcome, UnboundExisting)
    assert len(outcome.lease.allocations) == 1
