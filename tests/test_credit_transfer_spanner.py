"""Cross-plane credit transfer on the NATIVE Spanner store: the sharded parts.

WHAT THIS COVERS THAT THE CONFORMANCE SUITE CANNOT
--------------------------------------------------
`tests/conformance/test_store_semantics.py` runs its credit-transfer contract
against this store too (backend=spanner-fake), so the protocol semantics —
idempotency, refuse-and-leave-no-record, refund-once, tombstones — are asserted
there for BOTH backends by one suite and are deliberately not duplicated here.

But every workspace the conformance suite builds has `shard_count == 1`, which
is precisely the shape in which Spanner's balance looks like Postgres's. The
sharded escrow debit — the only genuinely new money DML on this plane, and the
reason the implementation was previously declined — is therefore invisible to
it. Everything below drives a MULTI-SHARD balance.

WHAT IS AND IS NOT ACTUALLY EXERCISED IN CI, PLAINLY
----------------------------------------------------
These tests run unconditionally, on `tests/fakes/spanner.py`, an in-process
model of Spanner — not the emulator and not the service. It reproduces the
behaviours whose absence has leaked production bugs before: read-set validation
with commit-time conflict ABORT and re-invocation, duplicate-PK ALREADY_EXISTS,
the DML/mutation mixing ban, single-use snapshot exhaustion
(`tests/test_fake_spanner_fidelity.py` guards these).

So a green run here means THE STORE'S LOGIC IS RIGHT — the debit is conditional,
the plan is all-or-nothing, the refund happens once. It does NOT mean the SQL is
valid Spanner: the fake is not the query planner and not the lock manager, and
it cannot catch an unsupported construct, a schema mismatch, or real ABORTED
behaviour under load. The `spanner-emulator` conformance backend is what would
prove that, and it still skips (no emulator schema provisioning). Read a pass
here as "correct logic, unproven dialect", never as "verified against Spanner".
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from tests.fakes.spanner import make_fake_store
from trusted_router import credit_transfer
from trusted_router.credit_transfer import DestinationMismatch, TransferIdReused
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_models import CreditAccount

WORKSPACE_ID = "ws-xfer"


def _seed(
    totals: list[int],
    *,
    usage: list[int] | None = None,
    reserved: list[int] | None = None,
) -> tuple[Any, Any]:
    """A workspace whose balance is spread over `len(totals)` shards.

    Deliberately builds the store with NO ready barrier: seeding runs its own
    transactions, and a barrier armed this early is consumed (and broken) by
    them rather than by the race a test means to stage. The one concurrency
    test arms its barrier itself, after setup.
    """
    store, database, _ = make_fake_store()
    usage = usage or [0] * len(totals)
    reserved = reserved or [0] * len(totals)
    store._write_entity(
        "credit",
        WORKSPACE_ID,
        CreditAccount(workspace_id=WORKSPACE_ID, shard_count=len(totals)),
    )
    table = database.typed.setdefault(CREDIT_BALANCE_TABLE, {})
    for shard, total in enumerate(totals):
        table[(WORKSPACE_ID, shard)] = {
            "workspace_id": WORKSPACE_ID,
            "shard": shard,
            "total_credits": total,
            "total_usage": usage[shard],
            "reserved": reserved[shard],
            "source_updated_at": None,
            "updated_at": None,
        }
    return store, database


def _totals(database: Any) -> list[int]:
    rows = database.typed[CREDIT_BALANCE_TABLE]
    shards = sorted(pk[1] for pk in rows if pk[0] == WORKSPACE_ID)
    return [int(rows[(WORKSPACE_ID, shard)]["total_credits"]) for shard in shards]


def _spendable(database: Any) -> int:
    """Total headroom, the quantity the conservation law is stated over."""
    rows = database.typed[CREDIT_BALANCE_TABLE]
    return sum(
        int(row["total_credits"]) - int(row["total_usage"]) - int(row["reserved"])
        for pk, row in rows.items()
        if pk[0] == WORKSPACE_ID
    )


# --------------------------------------------------------------------------
# The sharded escrow debit
# --------------------------------------------------------------------------


def test_escrow_debits_across_several_shards() -> None:
    """No single shard covers the amount, so the debit must span shards.

    This is the case Postgres never has to handle and the one the whole
    implementation exists for: a blind decrement of one shard would either
    drive it negative or refuse a transfer the workspace can clearly afford.
    """
    store, database = _seed([30, 30, 40])

    store.open_credit_transfer(
        transfer_id="t-multi",
        workspace_id=WORKSPACE_ID,
        amount_microdollars=80,
        destination="peer",
    )

    assert _spendable(database) == 20, _totals(database)
    assert all(total >= 0 for total in _totals(database)), _totals(database)


def test_escrow_takes_the_whole_balance_when_it_exactly_covers() -> None:
    store, database = _seed([30, 30, 40])

    store.open_credit_transfer(
        transfer_id="t-all",
        workspace_id=WORKSPACE_ID,
        amount_microdollars=100,
        destination="peer",
    )

    assert _spendable(database) == 0
    assert _totals(database) == [0, 0, 0]


def test_escrow_one_over_the_total_is_refused_and_changes_nothing() -> None:
    """The conditional debit, at the boundary. A plan that cannot complete must
    leave every shard untouched — not a partial debit that strands value."""
    store, database = _seed([30, 30, 40])

    with pytest.raises(ValueError, match="insufficient credits"):
        store.open_credit_transfer(
            transfer_id="t-over",
            workspace_id=WORKSPACE_ID,
            amount_microdollars=101,
            destination="peer",
        )

    assert _totals(database) == [30, 30, 40]
    assert store.get_credit_transfer("t-over") is None
    # And the id stays usable, so a genuine retry after a top-up still works.
    assert ("credit_transfer_open", "t-over") not in database.rows


def test_escrow_excludes_reserved_and_used_credits() -> None:
    """Headroom is credits - usage - reserved. A transfer that spent a live
    hold's money would let an in-flight request settle against nothing."""
    store, database = _seed([100, 100], usage=[40, 0], reserved=[0, 50])
    # Spendable is (100-40) + (100-50) = 110.
    assert _spendable(database) == 110

    with pytest.raises(ValueError, match="insufficient credits"):
        store.open_credit_transfer(
            transfer_id="t-hold",
            workspace_id=WORKSPACE_ID,
            amount_microdollars=111,
            destination="peer",
        )

    store.open_credit_transfer(
        transfer_id="t-hold-ok",
        workspace_id=WORKSPACE_ID,
        amount_microdollars=110,
        destination="peer",
    )
    assert _spendable(database) == 0
    # The hold and the booked usage survived the debit untouched.
    rows = database.typed[CREDIT_BALANCE_TABLE]
    assert int(rows[(WORKSPACE_ID, 0)]["total_usage"]) == 40
    assert int(rows[(WORKSPACE_ID, 1)]["reserved"]) == 50


def test_an_overspent_shard_counts_against_affordability() -> None:
    """The signed-sum rule, and the sharpest way to mint on this path.

    Shard 0 is overdrawn by 50 (usage exceeds credits); shard 1 has 100 of real
    headroom. POSITIVE headroom alone says "100 is affordable". The workspace
    globally holds 50. Summing only the positive shards would move 100 off a
    plane that owns 50 — value created from an accounting choice.
    """
    store, database = _seed([100, 100], usage=[150, 0])
    assert _spendable(database) == 50

    with pytest.raises(ValueError, match="insufficient credits"):
        store.open_credit_transfer(
            transfer_id="t-signed",
            workspace_id=WORKSPACE_ID,
            amount_microdollars=80,
            destination="peer",
        )

    assert _totals(database) == [100, 100]

    # 50 — the true global headroom — is still allowed.
    store.open_credit_transfer(
        transfer_id="t-signed-ok",
        workspace_id=WORKSPACE_ID,
        amount_microdollars=50,
        destination="peer",
    )
    assert _spendable(database) == 0


def test_escrow_fails_closed_when_a_shard_row_is_missing() -> None:
    """A missing shard is an unknown balance, not an empty one. Planning
    against the shards that happen to exist would spend against a total the
    transaction cannot see."""
    store, database = _seed([100, 100, 100])
    database.typed[CREDIT_BALANCE_TABLE].pop((WORKSPACE_ID, 2))

    with pytest.raises(ValueError, match="insufficient credits"):
        store.open_credit_transfer(
            transfer_id="t-gap",
            workspace_id=WORKSPACE_ID,
            amount_microdollars=10,
            destination="peer",
        )

    assert _totals(database) == [100, 100]


def test_escrow_is_idempotent_on_a_sharded_balance() -> None:
    store, database = _seed([30, 30, 40])

    for _ in range(3):
        store.open_credit_transfer(
            transfer_id="t-dup",
            workspace_id=WORKSPACE_ID,
            amount_microdollars=80,
            destination="peer",
        )

    assert _spendable(database) == 20


# --------------------------------------------------------------------------
# Resolution: the refund, and the double-refund race
# --------------------------------------------------------------------------


def test_rejected_transfer_returns_the_exact_total_across_shards() -> None:
    """Conservation, not per-shard symmetry. The refund spreads evenly while
    the debit took greedily, so individual shards differ — only the SUM is the
    conserved quantity, and skew is the rebalancer's job."""
    store, database = _seed([30, 30, 40])
    before = _spendable(database)

    store.open_credit_transfer(
        transfer_id="t-ret",
        workspace_id=WORKSPACE_ID,
        amount_microdollars=80,
        destination="peer",
    )
    assert _spendable(database) == before - 80

    store.resolve_credit_transfer(transfer_id="t-ret", outcome=credit_transfer.REJECTED)

    assert _spendable(database) == before
    # The debit drained shards 2 and 1 and took 10 from shard 0; the refund
    # spreads 80 over three shards (28/26/26, remainder to shard 0). Pinned
    # exactly because conservation alone does not catch a refund that dumps the
    # whole amount onto shard 0: the total would still be right while the
    # workspace quietly loses the write-spreading that sharding exists to give
    # it, recreating the hot row on every returned transfer.
    assert _totals(database) == [48, 26, 26]


def test_delivered_transfer_never_touches_the_source_balance_again() -> None:
    store, database = _seed([30, 30, 40])
    store.open_credit_transfer(
        transfer_id="t-del",
        workspace_id=WORKSPACE_ID,
        amount_microdollars=80,
        destination="peer",
    )
    after_escrow = _spendable(database)

    for _ in range(3):
        resolved = store.resolve_credit_transfer(
            transfer_id="t-del", outcome=credit_transfer.ACCEPTED
        )
        assert resolved.state == credit_transfer.DELIVERED

    assert _spendable(database) == after_escrow


def test_concurrent_resolvers_refund_exactly_once() -> None:
    """THE HAZARD, on Spanner. Two resolvers both read ESCROWED and both run
    the refund => the escrow is returned twice => money minted.

    Postgres closes this with `SELECT ... FOR UPDATE`. Spanner has no such
    statement; the guarantee comes from commit-time read-set validation plus
    abort-and-re-invoke, which makes each loser RE-READ the transfer and find it
    already terminal (see `storage_gcp_credit_transfer.resolve_credit_transfer`).

    Note what this test does NOT establish: deleting the insert-once
    `credit_transfer_resolution` row leaves it green. That row is defence in
    depth and the replay path's record, not the barrier holding this case up —
    the transfer-row read-and-rewrite is. The docstring on the implementation
    says so for the same reason this comment does: nobody should read a pass
    here as evidence for the wrong mechanism.

    THE BARRIER IS ARMED ONLY AFTER SETUP, and that detail is the difference
    between this test and a vacuous one. The fake makes every transaction's
    FIRST attempt wait on `_ready_barrier` just before it commits. Passing the
    barrier to `make_fake_store` therefore makes the SETUP transactions wait on
    it too: `open_credit_transfer` above arrives alone, times out, and leaves
    the barrier BROKEN, after which every later `wait()` returns instantly and
    the four resolvers run one after another. That version of this test passed
    while never once contending. Arming the barrier here, on a store built
    without one, is what makes the workers actually collide — and the
    `aborts` assertion below is what keeps it that way.
    """
    workers = 4
    store, database = _seed([30, 30, 40])
    store.open_credit_transfer(
        transfer_id="t-race",
        workspace_id=WORKSPACE_ID,
        amount_microdollars=80,
        destination="peer",
    )
    escrowed = _spendable(database)
    aborts_before = database.aborts
    database._ready_barrier = threading.Barrier(workers + 1)
    barrier = database._ready_barrier

    errors: list[Exception] = []
    lock = threading.Lock()

    def resolve() -> None:
        try:
            store.resolve_credit_transfer(
                transfer_id="t-race", outcome=credit_transfer.REJECTED
            )
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=resolve, daemon=True) for _ in range(workers)]
    for thread in threads:
        thread.start()
    try:
        barrier.wait(timeout=10)
    except threading.BrokenBarrierError:
        pass
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads), "resolver hang"

    assert errors == [], errors
    # ANTI-VACUITY: without a conflict this test asserts nothing about
    # concurrency, and a green run would be meaningless. Every loser must have
    # been aborted and re-run at least once.
    assert database.aborts - aborts_before >= workers - 1, (
        "resolvers did not contend — the race never happened, so the "
        f"single-refund assertion below proves nothing (aborts="
        f"{database.aborts - aborts_before})"
    )
    # Refunded once: exactly the escrowed amount came back, not 2x or 4x.
    assert _spendable(database) == escrowed + 80
    transfer = store.get_credit_transfer("t-race")
    assert transfer is not None
    assert transfer.state == credit_transfer.RETURNED


def test_a_disagreeing_second_verdict_is_refused() -> None:
    """The source never invents a verdict, and never applies two."""
    store, _database = _seed([100])
    store.open_credit_transfer(
        transfer_id="t-conflict",
        workspace_id=WORKSPACE_ID,
        amount_microdollars=40,
        destination="peer",
    )
    store.resolve_credit_transfer(
        transfer_id="t-conflict", outcome=credit_transfer.ACCEPTED
    )

    with pytest.raises(credit_transfer.CreditTransferConflict):
        store.resolve_credit_transfer(
            transfer_id="t-conflict", outcome=credit_transfer.REJECTED
        )


def test_resolving_an_unknown_transfer_raises_keyerror() -> None:
    store, _database = _seed([100])
    with pytest.raises(KeyError):
        store.resolve_credit_transfer(
            transfer_id="t-nope", outcome=credit_transfer.ACCEPTED
        )


# --------------------------------------------------------------------------
# An id is not an agreement
# --------------------------------------------------------------------------


def test_reusing_a_transfer_id_for_another_destination_is_refused() -> None:
    """Two planes must not be able to answer for one escrow: a REJECTED
    tombstone written by B would release value A may already have accepted."""
    store, database = _seed([100])
    store.open_credit_transfer(
        transfer_id="t-dest",
        workspace_id=WORKSPACE_ID,
        amount_microdollars=40,
        destination="plane-a",
    )
    after = _spendable(database)

    with pytest.raises(DestinationMismatch):
        store.open_credit_transfer(
            transfer_id="t-dest",
            workspace_id=WORKSPACE_ID,
            amount_microdollars=40,
            destination="plane-b",
        )

    assert _spendable(database) == after


def test_reusing_a_transfer_id_for_another_amount_is_refused() -> None:
    store, _database = _seed([100])
    store.open_credit_transfer(
        transfer_id="t-amt",
        workspace_id=WORKSPACE_ID,
        amount_microdollars=40,
        destination="peer",
    )

    with pytest.raises(TransferIdReused):
        store.open_credit_transfer(
            transfer_id="t-amt",
            workspace_id=WORKSPACE_ID,
            amount_microdollars=41,
            destination="peer",
        )


def test_a_claim_from_a_different_source_is_refused_not_replayed() -> None:
    """DESTINATION side. Replaying a recorded verdict for a different source
    hands a second plane "accepted" for a credit that never happened here — it
    debited, nothing was credited, and both planes report success."""
    store, database = _seed([0])
    assert (
        store.claim_credit_transfer(
            transfer_id="t-src",
            workspace_id=WORKSPACE_ID,
            amount_microdollars=100,
            source="plane-a",
            accept=True,
        )
        == credit_transfer.ACCEPTED
    )
    credited = _spendable(database)

    with pytest.raises(TransferIdReused):
        store.claim_credit_transfer(
            transfer_id="t-src",
            workspace_id=WORKSPACE_ID,
            amount_microdollars=100,
            source="plane-b",
            accept=True,
        )

    assert _spendable(database) == credited


def test_claim_credits_across_shards_and_only_once() -> None:
    store, database = _seed([0, 0, 0])

    for _ in range(3):
        assert (
            store.claim_credit_transfer(
                transfer_id="t-claim",
                workspace_id=WORKSPACE_ID,
                amount_microdollars=100,
                source="home",
                accept=True,
            )
            == credit_transfer.ACCEPTED
        )

    assert _spendable(database) == 100
    assert sum(_totals(database)) == 100


def test_claiming_for_an_unknown_workspace_credits_nothing() -> None:
    """No balance here means no such workspace. Rolling back discards the claim
    row too, so the source can retry after federation instead of being told the
    transfer was accepted by a plane that never credited it."""
    store, database = _seed([100])

    with pytest.raises(ValueError, match="no credit balance"):
        store.claim_credit_transfer(
            transfer_id="t-ghost",
            workspace_id="ws-does-not-exist",
            amount_microdollars=100,
            source="home",
            accept=True,
        )

    assert ("credit_transfer_claim", "t-ghost") not in database.rows
    assert _spendable(database) == 100


# --------------------------------------------------------------------------
# The recovery queue
# --------------------------------------------------------------------------


def test_open_transfers_are_listed_until_resolved() -> None:
    store, _database = _seed([100])
    for index in range(3):
        store.open_credit_transfer(
            transfer_id=f"t-q{index}",
            workspace_id=WORKSPACE_ID,
            amount_microdollars=10,
            destination="peer",
        )

    assert sorted(t.id for t in store.list_open_credit_transfers()) == [
        "t-q0",
        "t-q1",
        "t-q2",
    ]

    store.resolve_credit_transfer(transfer_id="t-q1", outcome=credit_transfer.ACCEPTED)

    assert sorted(t.id for t in store.list_open_credit_transfers()) == ["t-q0", "t-q2"]


def test_open_transfer_listing_pages_on_after_id() -> None:
    """Paging is what keeps a permanently-skipped transfer (one escrowed for a
    destination this driver cannot reach) from stalling the whole queue."""
    store, _database = _seed([100])
    for index in range(4):
        store.open_credit_transfer(
            transfer_id=f"t-p{index}",
            workspace_id=WORKSPACE_ID,
            amount_microdollars=10,
            destination="peer",
        )

    first = store.list_open_credit_transfers(limit=2)
    assert [t.id for t in first] == ["t-p0", "t-p1"]
    second = store.list_open_credit_transfers(limit=2, after_id=first[-1].id)
    assert [t.id for t in second] == ["t-p2", "t-p3"]


def test_a_stale_queue_row_is_dropped_rather_than_stalling_the_walk() -> None:
    """A queue row whose transfer is already terminal is garbage from a partial
    repair. Leaving it would make "a row means an escrowed transfer" false and
    let a filtered-out row consume a page forever."""
    store, database = _seed([100])
    store.open_credit_transfer(
        transfer_id="t-stale",
        workspace_id=WORKSPACE_ID,
        amount_microdollars=10,
        destination="peer",
    )
    store.resolve_credit_transfer(
        transfer_id="t-stale", outcome=credit_transfer.ACCEPTED
    )
    # Re-add the index row the resolution deleted, simulating a partial repair.
    store._write_entity(
        "credit_transfer_open", "t-stale", {"transfer_id": "t-stale"}
    )
    assert ("credit_transfer_open", "t-stale") in database.rows

    assert store.list_open_credit_transfers() == []
    assert ("credit_transfer_open", "t-stale") not in database.rows


# --------------------------------------------------------------------------
# Error-contract parity with the other backends
# --------------------------------------------------------------------------
#
# One conformance suite covers three backends, so a caller that branches on
# these errors must get the same answer whichever plane it is talking to.
# `storage_postgres.py` and `storage.py` (in-memory) already agree; these pin
# the Spanner store to the same three, because each one means something
# different to the push/recovery protocol.


def test_opening_against_an_unknown_workspace_reports_insufficient_credits() -> None:
    """NOT "no credit balance". A workspace with no account can afford nothing,
    and both other backends reach that verdict by the same route (a 0-row
    conditional UPDATE / `money is None`). A different error here would make
    the source treat an ordinary refusal as an unknown outcome."""
    store, _database = _seed([100])

    with pytest.raises(ValueError, match="insufficient credits"):
        store.open_credit_transfer(
            transfer_id="t-noacct",
            workspace_id="ws-no-such-account",
            amount_microdollars=10,
            destination="peer",
        )


def test_a_refund_whose_balance_vanished_raises_runtimeerror_not_valueerror() -> None:
    """The escrow was debited from a balance that no longer exists: corruption,
    not a business outcome. ValueError would let a caller that catches "refused"
    swallow it, and the rollback would strand the escrow silently."""
    store, database = _seed([100])
    store.open_credit_transfer(
        transfer_id="t-vanish",
        workspace_id=WORKSPACE_ID,
        amount_microdollars=40,
        destination="peer",
    )
    database.rows.pop(("credit", WORKSPACE_ID))

    with pytest.raises(RuntimeError, match="missing authoritative tr_credit_balance"):
        store.resolve_credit_transfer(
            transfer_id="t-vanish", outcome=credit_transfer.REJECTED
        )

    # Rolled back: the transfer is still escrowed and still recoverable.
    transfer = store.get_credit_transfer("t-vanish")
    assert transfer is not None
    assert transfer.state == credit_transfer.ESCROWED


# --------------------------------------------------------------------------
# A shard row that vanished under a CREDIT
# --------------------------------------------------------------------------
#
# The two tests above delete the `credit` ENTITY, so `_shard_count_tx` returns
# None and both paths raise before `_credit_across_shards` is ever entered.
# That leaves the row-count guard inside it — the one that stops a refund or a
# claim from crediting only SOME shards — with no coverage at all, on a module
# whose entire reason to exist is that the balance is sharded.
#
# The state below is the one that matters and is not hypothetical: the account
# entity still says `shard_count = 3` while one `tr_credit_balance` row is
# absent. `rebalance_precheck` has a dedicated INCOMPLETE outcome for exactly
# this drift, and a partially-applied shard grow in
# `storage_gcp_credit_shard_admin.py` can produce it.
#
# Both tests fail if the `!= 1` guard in `_credit_across_shards` is weakened.


def test_a_refund_whose_shard_row_vanished_rolls_back_rather_than_part_paying() -> None:
    """A refund that lands on only SOME shards would destroy the difference.

    `distribute_credit_amount(80, 3)` is (28, 26, 26), so the missing shard's
    26 has nowhere to go. Without the row-count guard the transaction would
    commit RETURNED having credited 54, delete the recovery-queue row in the
    same breath, and report success — 26 microdollars gone, terminally, with
    nothing left to revisit it. Rolling back keeps the transfer ESCROWED and
    therefore recoverable.
    """
    store, database = _seed([30, 30, 40])
    store.open_credit_transfer(
        transfer_id="t-shardgone",
        workspace_id=WORKSPACE_ID,
        amount_microdollars=80,
        destination="peer",
    )
    assert _totals(database) == [20, 0, 0]
    database.typed[CREDIT_BALANCE_TABLE].pop((WORKSPACE_ID, 2))
    before = _spendable(database)

    with pytest.raises(RuntimeError, match="missing authoritative tr_credit_balance"):
        store.resolve_credit_transfer(
            transfer_id="t-shardgone", outcome=credit_transfer.REJECTED
        )

    # Nothing partial was committed: no shard took its share of the refund.
    assert _totals(database) == [20, 0]
    assert _spendable(database) == before
    # Still escrowed, still queued, so a later pass can retry the refund.
    transfer = store.get_credit_transfer("t-shardgone")
    assert transfer is not None
    assert transfer.state == credit_transfer.ESCROWED
    assert ("credit_transfer_open", "t-shardgone") in database.rows
    assert ("credit_transfer_resolution", "t-shardgone") not in database.rows


def test_a_claim_whose_shard_row_vanished_accepts_nothing() -> None:
    """Worse on the destination side: a part-credited claim returns ACCEPTED.

    The source would mark the transfer DELIVERED against a plane that credited
    67 of 100, and both planes would report success while 33 ceased to exist.
    The claim row must roll back with the balance change so the source keeps
    the value escrowed and can retry.
    """
    store, database = _seed([0, 0, 0])
    database.typed[CREDIT_BALANCE_TABLE].pop((WORKSPACE_ID, 2))

    with pytest.raises(ValueError, match="no credit balance"):
        store.claim_credit_transfer(
            transfer_id="t-claimgap",
            workspace_id=WORKSPACE_ID,
            amount_microdollars=100,
            source="home",
            accept=True,
        )

    # No shard kept a partial credit, and no verdict was recorded.
    assert _totals(database) == [0, 0]
    assert _spendable(database) == 0
    assert ("credit_transfer_claim", "t-claimgap") not in database.rows


def test_an_incomplete_shard_set_is_logged_not_just_reported_as_insufficient(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """402 "insufficient credits" is the right WIRE answer and the wrong story.

    The error itself is contract: one conformance suite holds all three
    backends to ValueError("insufficient credits") for a refused open, and
    Postgres reaches it as a 0-row conditional UPDATE. But a workspace whose
    `credit` entity says shard_count=3 while a `tr_credit_balance` row is
    absent is not broke — its balance table is damaged, and it will keep
    getting 402 after every top-up. Without a log there is nothing anywhere
    that says so.
    """
    store, database = _seed([100, 100, 100])
    database.typed[CREDIT_BALANCE_TABLE].pop((WORKSPACE_ID, 1))

    with caplog.at_level("ERROR"), pytest.raises(
        ValueError, match="insufficient credits"
    ):
        store.open_credit_transfer(
            transfer_id="t-incomplete",
            workspace_id=WORKSPACE_ID,
            amount_microdollars=10,
            destination="peer",
        )

    assert "INCOMPLETE" in caplog.text
    assert WORKSPACE_ID in caplog.text
    # The operator needs to see WHICH shards are actually there to repair it.
    assert "[0, 2]" in caplog.text


def test_a_genuine_refusal_does_not_log_balance_corruption(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The counterpart, so the signal above stays worth paging on.

    A workspace that simply cannot afford the amount is an ordinary business
    refusal. If that logged an error too, the corruption signal would be pure
    noise within a day and nobody would look at it.
    """
    store, _database = _seed([10, 10, 10])

    with caplog.at_level("ERROR"), pytest.raises(
        ValueError, match="insufficient credits"
    ):
        store.open_credit_transfer(
            transfer_id="t-poor",
            workspace_id=WORKSPACE_ID,
            amount_microdollars=500,
            destination="peer",
        )

    assert "INCOMPLETE" not in caplog.text
