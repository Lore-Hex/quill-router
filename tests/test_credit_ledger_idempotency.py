"""Idempotency-key contract tests for the credit ledger.

The credit ledger's `reserve()` is the most-retried write in the system —
the gateway-authorize handler calls it on every inference request. With
no idempotency, a retry that lands twice would lock workspace credits
twice and never release the second lock (settle() only matches the
specific reservation ID, not "all reservations for this request").

These tests verify:
  1. Two `reserve()` calls with the same idempotency_key return the
     SAME Reservation and only debit credit once.
  2. `reserve()` with no idempotency_key behaves as before (each call
     creates a new reservation).
  3. The storage protocol supports the kwarg (so dual-write wrappers
     in Stage 5a can pass identical keys to both Spanner instances and
     trust that a replayed mirror write is a no-op).

Foundational for Stage 1 zero-downtime migration (change-stream replay
must be safe) and Stage 5a dual-Spanner active-passive (mirror replay
must be safe).
"""

from __future__ import annotations

from trusted_router.storage import STORE


def _seed_credited_workspace(initial_credits_microdollars: int = 100_000_000) -> tuple[str, str]:
    """Helper: create a workspace with a fresh API key and the given
    credit balance. Returns (workspace_id, key_hash)."""
    user = STORE.ensure_user("idempotency-test@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    STORE.credits[workspace.id].total_credits_microdollars = initial_credits_microdollars
    _, api_key = STORE.create_api_key(
        workspace_id=workspace.id,
        name="idempotency-test",
        creator_user_id=user.id,
    )
    return workspace.id, api_key.hash


def test_reserve_with_same_idempotency_key_returns_same_reservation() -> None:
    workspace_id, key_hash = _seed_credited_workspace(initial_credits_microdollars=10_000_000)

    first = STORE.reserve(
        workspace_id, key_hash, 3_000_000, idempotency_key="req-abc"
    )
    second = STORE.reserve(
        workspace_id, key_hash, 3_000_000, idempotency_key="req-abc"
    )

    # Same reservation object — second call is a read, not a new debit.
    assert first.id == second.id
    assert first.idempotency_key == "req-abc"
    # Credit was only locked once. With $10M initial credit and one $3M
    # reservation, $7M should remain available (not $4M as it would be
    # if both reserve calls had debited).
    account = STORE.get_credit_account(workspace_id)
    assert account is not None
    assert account.reserved_microdollars == 3_000_000


def test_reserve_without_idempotency_key_still_creates_distinct_reservations() -> None:
    workspace_id, key_hash = _seed_credited_workspace(initial_credits_microdollars=10_000_000)

    first = STORE.reserve(workspace_id, key_hash, 3_000_000)
    second = STORE.reserve(workspace_id, key_hash, 3_000_000)

    assert first.id != second.id
    assert first.idempotency_key is None
    assert second.idempotency_key is None
    account = STORE.get_credit_account(workspace_id)
    assert account is not None
    assert account.reserved_microdollars == 6_000_000


def test_reserve_with_different_idempotency_keys_creates_distinct_reservations() -> None:
    workspace_id, key_hash = _seed_credited_workspace(initial_credits_microdollars=10_000_000)

    first = STORE.reserve(workspace_id, key_hash, 2_000_000, idempotency_key="req-1")
    second = STORE.reserve(workspace_id, key_hash, 2_000_000, idempotency_key="req-2")

    assert first.id != second.id
    assert first.idempotency_key == "req-1"
    assert second.idempotency_key == "req-2"
    account = STORE.get_credit_account(workspace_id)
    assert account is not None
    assert account.reserved_microdollars == 4_000_000


def test_reserve_idempotency_returns_existing_even_when_amounts_differ() -> None:
    """If a retry passes a different amount with the same key, we trust
    the original. This matches the standard idempotency-key contract
    (the second request's body is ignored if the key is a duplicate).

    The gateway-authorize handler uses a per-request UUID so this case
    only triggers in pathological retries, but the contract should be
    explicit."""
    workspace_id, key_hash = _seed_credited_workspace(initial_credits_microdollars=10_000_000)

    first = STORE.reserve(
        workspace_id, key_hash, 1_000_000, idempotency_key="req-stable"
    )
    # Caller's retry mistakenly passes a larger amount. We return the
    # original reservation; only the original amount is debited.
    second = STORE.reserve(
        workspace_id, key_hash, 5_000_000, idempotency_key="req-stable"
    )

    assert first.id == second.id
    assert first.amount_microdollars == 1_000_000
    assert second.amount_microdollars == 1_000_000
    account = STORE.get_credit_account(workspace_id)
    assert account is not None
    assert account.reserved_microdollars == 1_000_000
