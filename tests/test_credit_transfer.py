"""Cross-plane credit transfer: conservation under every hostile interleaving.

The tests that matter here are not the happy path. Credits are a QUANTITY
under a conservation law — the failure mode is not "the request errored", it is
"the ledger now says a different total than it did before", which nobody
notices until an audit. So every test below either drives a real concurrent /
crash / duplicate interleaving, or asserts the total across both planes.

Two InMemoryStores stand in for two planes. That is the right fidelity for the
properties being tested: every transition is a single insert-once row plus a
conditional balance change under one lock, and the Postgres implementation is
the same shape with `_insert_entity_once_tx` and `ON CONFLICT` doing the same
job. The cross-backend contract is pinned separately, in the conformance suite.
"""

from __future__ import annotations

import random
import threading
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
from trusted_router import credit_transfer
from trusted_router.config import Settings
from trusted_router.credit_transfer import CreditTransferConflict
from trusted_router.main import create_app
from trusted_router.services.credit_transfer import (
    CreditTransferClient,
    CreditTransferUnavailable,
    cancel_credit_transfer,
    push_credit_transfer,
    recover_credit_transfers,
)
from trusted_router.storage import STORE, InMemoryStore, configure_store

WORKSPACE = "ws-federated-1"
#: The destination label a transfer is escrowed FOR. It must match the client
#: doing the delivering: a recovery pass asks only the plane a transfer was
#: meant for, because any plane holding a valid credit token would happily
#: accept an id it never saw.
PEER_URL = "https://aws.trustedrouter.com"


def _plane_with_credits(amount: int) -> InMemoryStore:
    """A source plane holding `amount` spendable microdollars."""
    store = InMemoryStore()
    user = store.ensure_user("owner", "owner@example.com")
    workspace = store.create_workspace(user.id, "src", trial_credit_microdollars=amount)
    # Tests address the workspace by a stable id rather than the generated one.
    store.workspaces[WORKSPACE] = workspace
    store.credit_money[WORKSPACE] = store.credit_money[workspace.id]
    store.credits[WORKSPACE] = store.credits[workspace.id]
    return store


def _destination_plane() -> InMemoryStore:
    """A peer plane with the workspace federated in at ZERO balance."""
    store = InMemoryStore()
    store.upsert_federated_api_key(
        {
            "lookup_hash": "lh-dest",
            "key_hash": "kh-dest",
            "workspace_id": WORKSPACE,
            "name": "federated",
        }
    )
    return store


def _spendable(store: InMemoryStore, workspace_id: str = WORKSPACE) -> int:
    money = store.credit_money.get(workspace_id)
    if money is None:
        return 0
    return (
        money.total_credits_microdollars
        - money.total_usage_microdollars
        - money.reserved_microdollars
    )


def _escrowed(store: InMemoryStore) -> int:
    """Value the SOURCE has debited and not yet resolved."""
    return sum(
        transfer.amount_microdollars
        for transfer in store.credit_transfers.values()
        if transfer.state == credit_transfer.ESCROWED
    )


def _undecided_escrow(source: InMemoryStore, destination: InMemoryStore) -> int:
    """Escrow the DESTINATION has not yet ruled on.

    "Undecided" is keyed on the destination's claim row, not on the source's
    state. Once that row exists the value is already wherever it says, and the
    source's lingering ESCROWED record is a stale view rather than a third copy
    — so counting it would make the auditor's total appear to mint on exactly
    the interleaving that matters most (destination accepted, ack lost).
    """
    return sum(
        transfer.amount_microdollars
        for transfer in source.credit_transfers.values()
        if transfer.state == credit_transfer.ESCROWED
        and transfer.id not in destination.credit_transfer_claims
    )


def _total_value(source: InMemoryStore, destination: InMemoryStore) -> int:
    """The quantity the whole design exists to keep constant.

    Three buckets, and every microdollar is in exactly one: spendable on the
    source, parked in UNDECIDED escrow (spendable by nobody), spendable on the
    destination. See trusted_router.credit_transfer for why the middle bucket
    is defined by the destination's claim row.
    """
    return _spendable(source) + _undecided_escrow(source, destination) + _spendable(destination)


def _client_for(destination: InMemoryStore, **kwargs: Any) -> CreditTransferClient:
    """A transfer client whose wire hop lands in `destination`'s claim table.

    Goes through httpx.MockTransport rather than calling the store directly so
    the serialization, the verdict parsing, and the "any non-200 is unknown"
    rule are all exercised — those are where a delivery can silently become a
    rejection.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        outcome = destination.claim_credit_transfer(
            transfer_id=body["transfer_id"],
            workspace_id=body["workspace_id"],
            amount_microdollars=body["amount_microdollars"],
            source=body.get("source_plane", ""),
            accept=body["action"] == credit_transfer.ACCEPTED,
        )
        return httpx.Response(200, json={"data": {"outcome": outcome}})

    return CreditTransferClient(
        destination_base_url=kwargs.pop("destination_base_url", PEER_URL),
        credit_token="credit-secret",  # noqa: S106 - test fixture.
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


class TestHappyPathConservation:
    def test_a_completed_transfer_moves_value_without_changing_the_total(self) -> None:
        source, destination = _plane_with_credits(1_000_000), _destination_plane()
        before = _total_value(source, destination)

        transfer = push_credit_transfer(
            transfer_id="t-1",
            workspace_id=WORKSPACE,
            amount_microdollars=400_000,
            client=_client_for(destination),
            store=source,
        )

        assert transfer.state == credit_transfer.DELIVERED
        assert _spendable(source) == 600_000
        assert _spendable(destination) == 400_000
        assert _total_value(source, destination) == before

    def test_escrow_is_spendable_by_nobody(self) -> None:
        """The intermediate state must not be spendable on EITHER plane.

        If escrow left the money spendable on the source, a transfer plus a
        concurrent inference request would spend the same microdollars twice.
        """
        source, destination = _plane_with_credits(1_000_000), _destination_plane()
        source.open_credit_transfer(
            transfer_id="t-escrow",
            workspace_id=WORKSPACE,
            amount_microdollars=400_000,
            destination=PEER_URL,
        )

        assert _spendable(source) == 600_000, "escrowed value must leave the source balance"
        assert _spendable(destination) == 0, "nothing is credited until the peer accepts"
        assert _escrowed(source) == 400_000
        assert _total_value(source, destination) == 1_000_000


class TestIdempotence:
    def test_duplicate_delivery_of_the_same_transfer_id_moves_value_once(self) -> None:
        """The retry path. A redelivered transfer that credited twice would
        mint money silently and pass any single-call smoke test."""
        source, destination = _plane_with_credits(1_000_000), _destination_plane()
        client = _client_for(destination)

        first = push_credit_transfer(
            transfer_id="t-dup",
            workspace_id=WORKSPACE,
            amount_microdollars=250_000,
            client=client,
            store=source,
        )
        second = push_credit_transfer(
            transfer_id="t-dup",
            workspace_id=WORKSPACE,
            amount_microdollars=250_000,
            client=client,
            store=source,
        )

        assert first.state == second.state == credit_transfer.DELIVERED
        assert _spendable(destination) == 250_000, "duplicate delivery credited twice"
        assert _spendable(source) == 750_000, "duplicate delivery debited twice"
        assert _total_value(source, destination) == 1_000_000

    def test_a_second_open_with_the_same_id_debits_once(self) -> None:
        source = _plane_with_credits(1_000_000)
        first = source.open_credit_transfer(
            transfer_id="t-open",
            workspace_id=WORKSPACE,
            amount_microdollars=100_000,
            destination=PEER_URL,
        )
        second = source.open_credit_transfer(
            transfer_id="t-open",
            workspace_id=WORKSPACE,
            amount_microdollars=100_000,
            destination=PEER_URL,
        )

        assert first.id == second.id
        assert _spendable(source) == 900_000
        assert _escrowed(source) == 100_000

    def test_the_destination_claim_is_written_once(self) -> None:
        destination = _destination_plane()
        for _ in range(5):
            outcome = destination.claim_credit_transfer(
                transfer_id="t-claim",
                workspace_id=WORKSPACE,
                amount_microdollars=50_000,
                source="home",
                accept=True,
            )
            assert outcome == credit_transfer.ACCEPTED
        assert _spendable(destination) == 50_000


class TestOverdraw:
    def test_a_transfer_larger_than_the_balance_is_refused(self) -> None:
        source, destination = _plane_with_credits(100_000), _destination_plane()

        with pytest.raises(ValueError, match="insufficient credits"):
            push_credit_transfer(
                transfer_id="t-over",
                workspace_id=WORKSPACE,
                amount_microdollars=100_001,
                client=_client_for(destination),
                store=source,
            )

        assert _spendable(source) == 100_000, "a refused transfer must debit nothing"
        assert _total_value(source, destination) == 100_000

    def test_a_refused_transfer_leaves_no_record(self) -> None:
        """The id must stay usable. Recording a failed transfer would make the
        customer's retry-after-top-up return the FAILED record instead of
        moving the money."""
        source, destination = _plane_with_credits(100_000), _destination_plane()
        with pytest.raises(ValueError):
            push_credit_transfer(
                transfer_id="t-retry",
                workspace_id=WORKSPACE,
                amount_microdollars=200_000,
                client=_client_for(destination),
                store=source,
            )
        assert source.get_credit_transfer("t-retry") is None

        source.credit_workspace_once(WORKSPACE, 200_000, "topup-1")
        transfer = push_credit_transfer(
            transfer_id="t-retry",
            workspace_id=WORKSPACE,
            amount_microdollars=200_000,
            client=_client_for(destination),
            store=source,
        )
        assert transfer.state == credit_transfer.DELIVERED

    def test_reserved_headroom_is_not_transferable(self) -> None:
        """Escrow may only take FREE balance. Transferring reserved value would
        let an in-flight inference settle against money that has left."""
        source = _plane_with_credits(100_000)
        source.credit_money[WORKSPACE].reserved_microdollars = 60_000

        with pytest.raises(ValueError, match="insufficient credits"):
            source.open_credit_transfer(
                transfer_id="t-reserved",
                workspace_id=WORKSPACE,
                amount_microdollars=50_000,
                destination=PEER_URL,
            )

    @pytest.mark.parametrize("amount", [0, -1, -100_000])
    def test_non_positive_amounts_are_rejected(self, amount: int) -> None:
        """A negative "transfer" would be a remote DEBIT of the destination —
        a way to reach into another plane's balance."""
        source = _plane_with_credits(100_000)
        with pytest.raises(ValueError, match="positive"):
            source.open_credit_transfer(
                transfer_id="t-bad",
                workspace_id=WORKSPACE,
                amount_microdollars=amount,
                destination=PEER_URL,
            )


class TestCrashRecovery:
    def test_crash_after_escrow_leaves_value_on_the_source_and_recovers(self) -> None:
        """Crash between debit and credit — the case the ordering exists for.

        The value must be accounted for at every instant (never minted, never
        lost) and the recovery path must resolve it without a human deciding
        who has the money.
        """
        source, destination = _plane_with_credits(1_000_000), _destination_plane()

        # The crash: escrow commits, the delivery never happens.
        source.open_credit_transfer(
            transfer_id="t-crash",
            workspace_id=WORKSPACE,
            amount_microdollars=300_000,
            destination=PEER_URL,
        )

        # Invariant holds mid-flight, and the value is on the SOURCE.
        assert _total_value(source, destination) == 1_000_000
        assert _spendable(destination) == 0, "nothing may be credited before delivery"
        assert _escrowed(source) == 300_000

        result = recover_credit_transfers(client=_client_for(destination), store=source)

        assert result["delivered"] == 1
        assert result["unresolved"] == 0
        assert result["failed"] == 0
        assert _spendable(destination) == 300_000
        assert _escrowed(source) == 0
        assert _total_value(source, destination) == 1_000_000

    def test_crash_after_the_destination_accepted_does_not_double_credit(self) -> None:
        """The nastiest window: the destination took the value and the ack was
        lost. Recovery must learn the existing verdict, not create a new one."""
        source, destination = _plane_with_credits(1_000_000), _destination_plane()
        transfer = source.open_credit_transfer(
            transfer_id="t-lost-ack",
            workspace_id=WORKSPACE,
            amount_microdollars=300_000,
            destination=PEER_URL,
        )
        # Destination accepted; the reply never made it back to the source.
        destination.claim_credit_transfer(
            transfer_id=transfer.id,
            workspace_id=WORKSPACE,
            amount_microdollars=300_000,
            source="home",
            accept=True,
        )
        assert _total_value(source, destination) == 1_000_000

        recover_credit_transfers(client=_client_for(destination), store=source)

        assert _spendable(destination) == 300_000, "recovery credited a second time"
        assert source.get_credit_transfer("t-lost-ack").state == credit_transfer.DELIVERED
        assert _total_value(source, destination) == 1_000_000

    def test_an_unreachable_destination_leaves_the_value_escrowed_not_refunded(self) -> None:
        """There is no elapsed time after which "probably not delivered"
        becomes safe. Value stays parked until the destination answers."""
        source, destination = _plane_with_credits(1_000_000), _destination_plane()
        source.open_credit_transfer(
            transfer_id="t-down",
            workspace_id=WORKSPACE,
            amount_microdollars=300_000,
            destination=PEER_URL,
        )

        def dead(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("destination unreachable")

        offline = CreditTransferClient(
            destination_base_url=PEER_URL,
            credit_token="credit-secret",  # noqa: S106 - test fixture.
            client=httpx.Client(transport=httpx.MockTransport(dead)),
        )
        result = recover_credit_transfers(client=offline, store=source)

        assert result["delivered"] == 0
        assert result["unresolved"] == 1
        assert result["failed"] == 0
        assert _escrowed(source) == 300_000, "value must not be returned on a guess"
        assert _spendable(source) == 700_000
        assert _total_value(source, destination) == 1_000_000

        # ...and it resolves cleanly the moment the destination is back.
        recover_credit_transfers(client=_client_for(destination), store=source)
        assert _spendable(destination) == 300_000
        assert _total_value(source, destination) == 1_000_000

    def test_recovery_does_not_deliver_to_the_wrong_plane(self) -> None:
        """A destination accepts any (id, workspace, amount) presented with a
        valid credit token — it cannot check that this source escrowed it. So a
        recovery pass that asked the WRONG plane would not be a harmless no-op:
        that plane would credit itself and the intended one would never get the
        value. Recovery therefore only asks the plane a transfer was escrowed
        for.
        """
        source, destination = _plane_with_credits(1_000_000), _destination_plane()
        source.open_credit_transfer(
            transfer_id="t-other-peer",
            workspace_id=WORKSPACE,
            amount_microdollars=300_000,
            destination="https://azure.trustedrouter.com",
        )

        result = recover_credit_transfers(client=_client_for(destination), store=source)

        assert result["skipped_other_destination"] == 1
        assert result["delivered"] == 0
        assert _spendable(destination) == 0, "credited a plane the transfer was not sent to"
        assert _escrowed(source) == 300_000

    def test_a_non_200_reply_is_unknown_and_never_a_rejection(self) -> None:
        """A 4xx says nothing about whether an EARLIER delivery was accepted.
        Treating it as a rejection would return escrow the destination holds."""
        source, destination = _plane_with_credits(1_000_000), _destination_plane()
        transfer = source.open_credit_transfer(
            transfer_id="t-4xx",
            workspace_id=WORKSPACE,
            amount_microdollars=300_000,
            destination=PEER_URL,
        )
        destination.claim_credit_transfer(
            transfer_id=transfer.id,
            workspace_id=WORKSPACE,
            amount_microdollars=300_000,
            source="home",
            accept=True,
        )

        rejecting = CreditTransferClient(
            destination_base_url=PEER_URL,
            credit_token="wrong-token",  # noqa: S106 - test fixture.
            client=httpx.Client(
                transport=httpx.MockTransport(lambda _r: httpx.Response(401, json={}))
            ),
        )
        with pytest.raises(CreditTransferUnavailable):
            rejecting.deliver(transfer)

        assert _escrowed(source) == 300_000
        assert _total_value(source, destination) == 1_000_000


class TestCancellation:
    def test_cancel_returns_the_value_to_the_source(self) -> None:
        source, destination = _plane_with_credits(1_000_000), _destination_plane()
        source.open_credit_transfer(
            transfer_id="t-cancel",
            workspace_id=WORKSPACE,
            amount_microdollars=300_000,
            destination=PEER_URL,
        )

        transfer = cancel_credit_transfer(
            transfer_id="t-cancel", client=_client_for(destination), store=source
        )

        assert transfer.state == credit_transfer.RETURNED
        assert _spendable(source) == 1_000_000
        assert _spendable(destination) == 0
        assert _total_value(source, destination) == 1_000_000

    def test_cancel_cannot_double_refund(self) -> None:
        source, destination = _plane_with_credits(1_000_000), _destination_plane()
        source.open_credit_transfer(
            transfer_id="t-cancel-twice",
            workspace_id=WORKSPACE,
            amount_microdollars=300_000,
            destination=PEER_URL,
        )
        client = _client_for(destination)

        for _ in range(3):
            cancel_credit_transfer(
                transfer_id="t-cancel-twice", client=client, store=source
            )

        assert _spendable(source) == 1_000_000, "a repeated cancel refunded twice"
        assert _total_value(source, destination) == 1_000_000

    def test_repeating_a_rejected_verdict_refunds_once(self) -> None:
        """The store's OWN double-refund guard, hit directly.

        `cancel_credit_transfer` short-circuits on an already-resolved transfer,
        so the service path never reaches this guard on a simple repeat — which
        means a test that only drives the service would stay green with the
        guard deleted. Anything that re-delivers a verdict (a recovery pass
        racing an operator, a retried resolve after a partial failure) lands
        here instead, and here is where the money is.
        """
        source = _plane_with_credits(1_000_000)
        source.open_credit_transfer(
            transfer_id="t-refund-once",
            workspace_id=WORKSPACE,
            amount_microdollars=300_000,
            destination=PEER_URL,
        )

        for _ in range(4):
            source.resolve_credit_transfer(
                transfer_id="t-refund-once", outcome=credit_transfer.REJECTED
            )

        assert _spendable(source) == 1_000_000, "a repeated verdict refunded twice"

    def test_concurrent_cancels_refund_once(self) -> None:
        """Threads that all observe ESCROWED before any of them resolves. The
        service-level short-circuit cannot help here — only the store's guard,
        applied under the same lock as the balance change, can."""
        source, destination = _plane_with_credits(1_000_000), _destination_plane()
        client = _client_for(destination)
        source.open_credit_transfer(
            transfer_id="t-cancel-race",
            workspace_id=WORKSPACE,
            amount_microdollars=400_000,
            destination=PEER_URL,
        )
        barrier = threading.Barrier(5)

        def cancel() -> None:
            barrier.wait(timeout=5)
            cancel_credit_transfer(
                transfer_id="t-cancel-race", client=client, store=source
            )

        threads = [threading.Thread(target=cancel) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert _spendable(source) == 1_000_000, "concurrent cancels refunded more than once"
        assert _total_value(source, destination) == 1_000_000

    def test_cancel_after_the_destination_accepted_delivers_instead(self) -> None:
        """The double-spend the tombstone design exists to prevent. A source
        that could cancel unilaterally would credit itself for value the
        destination already holds."""
        source, destination = _plane_with_credits(1_000_000), _destination_plane()
        transfer = source.open_credit_transfer(
            transfer_id="t-late-cancel",
            workspace_id=WORKSPACE,
            amount_microdollars=300_000,
            destination=PEER_URL,
        )
        destination.claim_credit_transfer(
            transfer_id=transfer.id,
            workspace_id=WORKSPACE,
            amount_microdollars=300_000,
            source="home",
            accept=True,
        )

        resolved = cancel_credit_transfer(
            transfer_id="t-late-cancel", client=_client_for(destination), store=source
        )

        assert resolved.state == credit_transfer.DELIVERED, "cancel overrode an accept"
        assert _spendable(source) == 700_000
        assert _spendable(destination) == 300_000
        assert _total_value(source, destination) == 1_000_000

    def test_accept_after_a_rejection_tombstone_is_refused(self) -> None:
        """The mirror case: once rejected, the id can never credit."""
        destination = _destination_plane()
        assert (
            destination.claim_credit_transfer(
                transfer_id="t-tombstone",
                workspace_id=WORKSPACE,
                amount_microdollars=300_000,
                source="home",
                accept=False,
            )
            == credit_transfer.REJECTED
        )
        assert (
            destination.claim_credit_transfer(
                transfer_id="t-tombstone",
                workspace_id=WORKSPACE,
                amount_microdollars=300_000,
                source="home",
                accept=True,
            )
            == credit_transfer.REJECTED
        )
        assert _spendable(destination) == 0

    def test_a_disagreeing_verdict_raises_rather_than_moving_value(self) -> None:
        """Unreachable through the protocol — the claim row is immutable. If it
        ever happens, two destinations answered for one id, and applying either
        one silently would break conservation."""
        source = _plane_with_credits(1_000_000)
        source.open_credit_transfer(
            transfer_id="t-conflict",
            workspace_id=WORKSPACE,
            amount_microdollars=300_000,
            destination=PEER_URL,
        )
        source.resolve_credit_transfer(
            transfer_id="t-conflict", outcome=credit_transfer.ACCEPTED
        )

        with pytest.raises(CreditTransferConflict):
            source.resolve_credit_transfer(
                transfer_id="t-conflict", outcome=credit_transfer.REJECTED
            )
        assert _spendable(source) == 700_000


class TestConcurrency:
    def test_concurrent_transfers_cannot_overdraw(self) -> None:
        """Driven, not reasoned about. Four threads each try to move the whole
        balance with a DIFFERENT transfer id, so idempotency cannot help — only
        the conditional debit can."""
        source, destination = _plane_with_credits(600_000), _destination_plane()
        client = _client_for(destination)
        results: list[str] = []
        barrier = threading.Barrier(4)

        def attempt(index: int) -> None:
            barrier.wait(timeout=5)
            try:
                push_credit_transfer(
                    transfer_id=f"t-race-{index}",
                    workspace_id=WORKSPACE,
                    amount_microdollars=600_000,
                    client=client,
                    store=source,
                )
                results.append("moved")
            except ValueError:
                results.append("refused")

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert results.count("moved") == 1, f"double spend: {results}"
        assert _spendable(source) == 0
        assert _spendable(destination) == 600_000
        assert _total_value(source, destination) == 600_000

    def test_concurrent_deliveries_of_one_id_credit_once(self) -> None:
        """A first attempt racing a recovery pass. Both may reach the
        destination; only one may move value."""
        source, destination = _plane_with_credits(1_000_000), _destination_plane()
        client = _client_for(destination)
        barrier = threading.Barrier(6)

        def deliver() -> None:
            barrier.wait(timeout=5)
            try:
                push_credit_transfer(
                    transfer_id="t-concurrent",
                    workspace_id=WORKSPACE,
                    amount_microdollars=200_000,
                    client=client,
                    store=source,
                )
            except CreditTransferConflict:  # pragma: no cover - would be a bug
                pytest.fail("concurrent deliveries produced disagreeing verdicts")

        threads = [threading.Thread(target=deliver) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert _spendable(destination) == 200_000
        assert _spendable(source) == 800_000
        assert _total_value(source, destination) == 1_000_000

    def test_a_cancel_racing_an_accept_resolves_one_way_for_both_planes(self) -> None:
        """The two planes must never disagree about who holds the value. One
        insert-once row decides, and both sides read the same answer."""
        source, destination = _plane_with_credits(1_000_000), _destination_plane()
        client = _client_for(destination)
        source.open_credit_transfer(
            transfer_id="t-race-cancel",
            workspace_id=WORKSPACE,
            amount_microdollars=400_000,
            destination=PEER_URL,
        )
        barrier = threading.Barrier(2)

        def push() -> None:
            barrier.wait(timeout=5)
            push_credit_transfer(
                transfer_id="t-race-cancel",
                workspace_id=WORKSPACE,
                amount_microdollars=400_000,
                client=client,
                store=source,
            )

        def cancel() -> None:
            barrier.wait(timeout=5)
            cancel_credit_transfer(
                transfer_id="t-race-cancel", client=client, store=source
            )

        threads = [threading.Thread(target=push), threading.Thread(target=cancel)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        state = source.get_credit_transfer("t-race-cancel").state
        claim = destination.credit_transfer_claims["t-race-cancel"]["outcome"]
        assert credit_transfer.STATE_FOR_OUTCOME[claim] == state, (
            "the planes disagree about who holds the value"
        )
        if state == credit_transfer.DELIVERED:
            assert _spendable(source) == 600_000
            assert _spendable(destination) == 400_000
        else:
            assert _spendable(source) == 1_000_000
            assert _spendable(destination) == 0
        assert _total_value(source, destination) == 1_000_000


class TestPostgresPrimitives:
    """The same guarantees, against the SQL the AWS plane actually ships.

    The tests above run on `InMemoryStore`, where every guarantee is a Python
    `if` under a lock. On Postgres they are `ON CONFLICT DO NOTHING`, a
    conditional `UPDATE ... WHERE total_credits - total_usage - reserved >= %s`,
    and the `rowcount` checks that read them — none of which the InMemory tests
    touch. A conditional stripped to an unconditional decrement would keep
    every test above green. See tests/fakes/postgres.py for the harness's
    limits.
    """

    @pytest.fixture
    def pg(self) -> Any:
        conn = sqlite_postgres_conn()
        store = postgres_store_on(conn)
        # Federate the workspace in, then fund it the only way credits can
        # arrive on a peer plane: an accepted transfer.
        store.upsert_federated_api_key(
            {
                "lookup_hash": "lh-pg",
                "key_hash": "kh-pg",
                "workspace_id": WORKSPACE,
                "name": "federated",
            }
        )
        store.claim_credit_transfer(
            transfer_id="t-pg-seed",
            workspace_id=WORKSPACE,
            amount_microdollars=1_000_000,
            source="home",
            accept=True,
        )
        return conn, store

    def test_escrow_debits_conditionally(self, pg: Any) -> None:
        conn, store = pg
        store.open_credit_transfer(
            transfer_id="t-pg-1",
            workspace_id=WORKSPACE,
            amount_microdollars=400_000,
            destination=PEER_URL,
        )
        assert conn.spendable(WORKSPACE) == 600_000

        with pytest.raises(ValueError, match="insufficient credits"):
            store.open_credit_transfer(
                transfer_id="t-pg-2",
                workspace_id=WORKSPACE,
                amount_microdollars=600_001,
                destination=PEER_URL,
            )
        assert conn.spendable(WORKSPACE) == 600_000

    def test_a_refused_escrow_rolls_back_its_own_idempotency_row(self, pg: Any) -> None:
        """The insert-once row is written BEFORE the debit, so a refused
        transfer must roll the whole transaction back — otherwise the id is
        burned and the customer's retry-after-top-up returns the phantom
        record instead of moving money."""
        conn, store = pg
        with pytest.raises(ValueError, match="insufficient credits"):
            store.open_credit_transfer(
                transfer_id="t-pg-burn",
                workspace_id=WORKSPACE,
                amount_microdollars=2_000_000,
                destination=PEER_URL,
            )

        assert store.get_credit_transfer("t-pg-burn") is None
        assert conn.count_entities("credit_transfer") == 0
        assert conn.count_entities("credit_transfer_open") == 0

    def test_escrow_is_idempotent_by_transfer_id(self, pg: Any) -> None:
        conn, store = pg
        for _ in range(3):
            store.open_credit_transfer(
                transfer_id="t-pg-dup",
                workspace_id=WORKSPACE,
                amount_microdollars=300_000,
                destination=PEER_URL,
            )
        assert conn.spendable(WORKSPACE) == 700_000

    def test_a_rejected_verdict_returns_the_escrow_exactly_once(self, pg: Any) -> None:
        conn, store = pg
        store.open_credit_transfer(
            transfer_id="t-pg-ret",
            workspace_id=WORKSPACE,
            amount_microdollars=300_000,
            destination=PEER_URL,
        )
        for _ in range(3):
            store.resolve_credit_transfer(
                transfer_id="t-pg-ret", outcome=credit_transfer.REJECTED
            )
        assert conn.spendable(WORKSPACE) == 1_000_000

    def test_an_accepted_verdict_leaves_the_source_balance_alone(self, pg: Any) -> None:
        """The debit already happened at escrow. Debiting again on delivery
        would destroy the value the destination just credited."""
        conn, store = pg
        store.open_credit_transfer(
            transfer_id="t-pg-del",
            workspace_id=WORKSPACE,
            amount_microdollars=300_000,
            destination=PEER_URL,
        )
        store.resolve_credit_transfer(
            transfer_id="t-pg-del", outcome=credit_transfer.ACCEPTED
        )
        assert conn.spendable(WORKSPACE) == 700_000

    def test_a_duplicate_claim_credits_once(self, pg: Any) -> None:
        conn, store = pg
        for _ in range(4):
            assert (
                store.claim_credit_transfer(
                    transfer_id="t-pg-claim",
                    workspace_id=WORKSPACE,
                    amount_microdollars=250_000,
                    source="home",
                    accept=True,
                )
                == credit_transfer.ACCEPTED
            )
        assert conn.spendable(WORKSPACE) == 1_250_000

    def test_a_tombstone_blocks_a_later_accept(self, pg: Any) -> None:
        conn, store = pg
        assert (
            store.claim_credit_transfer(
                transfer_id="t-pg-tomb",
                workspace_id=WORKSPACE,
                amount_microdollars=250_000,
                source="home",
                accept=False,
            )
            == credit_transfer.REJECTED
        )
        assert (
            store.claim_credit_transfer(
                transfer_id="t-pg-tomb",
                workspace_id=WORKSPACE,
                amount_microdollars=250_000,
                source="home",
                accept=True,
            )
            == credit_transfer.REJECTED
        )
        assert conn.spendable(WORKSPACE) == 1_000_000

    def test_a_claim_for_an_unfederated_workspace_records_nothing(self, pg: Any) -> None:
        """No balance row means the workspace is not here yet. Recording an
        'accepted' claim anyway would tell the source a plane took value it
        never credited — value destroyed, and the tombstone would block the
        retry that could have fixed it."""
        conn, store = pg
        with pytest.raises(ValueError, match="no credit balance"):
            store.claim_credit_transfer(
                transfer_id="t-pg-nobody",
                workspace_id="ws-not-federated-here",
                amount_microdollars=250_000,
                source="home",
                accept=True,
            )
        assert not conn.has_entity("credit_transfer_claim", "t-pg-nobody")

    def test_the_recovery_queue_empties_as_transfers_resolve(self, pg: Any) -> None:
        """The queue must shrink, not grow: it is a bounded PK-prefix scan, and
        a queue that kept resolved rows would degrade forever."""
        conn, store = pg
        for index in range(3):
            store.open_credit_transfer(
                transfer_id=f"t-pg-q{index}",
                workspace_id=WORKSPACE,
                amount_microdollars=100_000,
                destination=PEER_URL,
            )
        assert len(store.list_open_credit_transfers(50)) == 3

        store.resolve_credit_transfer(
            transfer_id="t-pg-q1", outcome=credit_transfer.ACCEPTED
        )

        open_ids = {transfer.id for transfer in store.list_open_credit_transfers(50)}
        assert open_ids == {"t-pg-q0", "t-pg-q2"}
        assert conn.count_entities("credit_transfer_open") == 2
        assert conn.count_entities("credit_transfer") == 3, "history must be retained"


class TestHttpSurface:
    """The wire boundary, where the token rules live.

    The existing federation module states that the peer token must NEVER be
    able to move money — that is why it is not the internal gateway token. A
    credit endpoint gated on it would quietly undo that, so the boundary is
    asserted rather than left to review.
    """

    def _client(self, **overrides: Any) -> TestClient:
        settings = Settings(
            environment="test",
            sentry_dsn=None,
            internal_gateway_token=None,
            stripe_secret_key=None,
            stripe_webhook_secret=None,
            **overrides,
        )
        # Build the app BEFORE seeding: create_app installs its own store, so
        # anything written first is discarded.
        client = TestClient(create_app(settings, init_observability=False))
        STORE.upsert_federated_api_key(
            {
                "lookup_hash": "lh-http",
                "key_hash": "kh-http",
                "workspace_id": WORKSPACE,
                "name": "federated",
            }
        )
        return client

    def _transfer_body(self, **overrides: Any) -> dict[str, Any]:
        return {
            "transfer_id": "t-http",
            "workspace_id": WORKSPACE,
            "amount_microdollars": 500_000,
            "action": "accepted",
            **overrides,
        }

    def test_the_directory_peer_token_cannot_credit(self) -> None:
        """THE security boundary. The peer token reads the user directory; if
        it could also move money, one leaked low-trust secret would grant
        both."""
        client = self._client(
            federation_peer_token="peer-secret",  # noqa: S106 - test fixture.
            federation_credit_inbound_token="credit-secret",  # noqa: S106 - test fixture.
        )

        response = client.post(
            "/v1/internal/federation/credit-transfer",
            json=self._transfer_body(),
            headers={"x-trustedrouter-federation-token": "peer-secret"},
        )

        assert response.status_code == 401, response.text
        assert STORE.credit_money[WORKSPACE].total_credits_microdollars == 0

    def test_a_plane_with_no_inbound_token_refuses_every_transfer(self) -> None:
        """Closed by default: a deployment must not be fundable by accident."""
        client = self._client()

        response = client.post(
            "/v1/internal/federation/credit-transfer",
            json=self._transfer_body(),
            headers={"x-trustedrouter-federation-credit-token": "anything"},
        )

        assert response.status_code == 403, response.text
        assert STORE.credit_money[WORKSPACE].total_credits_microdollars == 0

    def test_a_valid_credit_token_credits_once_however_often_delivered(self) -> None:
        client = self._client(
            federation_credit_inbound_token="credit-secret",  # noqa: S106 - test fixture.
        )
        headers = {"x-trustedrouter-federation-credit-token": "credit-secret"}

        for _ in range(3):
            response = client.post(
                "/v1/internal/federation/credit-transfer",
                json=self._transfer_body(),
                headers=headers,
            )
            assert response.status_code == 200, response.text
            assert response.json()["data"]["outcome"] == credit_transfer.ACCEPTED

        assert STORE.credit_money[WORKSPACE].total_credits_microdollars == 500_000

    def test_an_unfederated_workspace_is_a_retryable_conflict(self) -> None:
        """409, not 400: the request is well-formed and will succeed once a key
        for that workspace has been resolved here. No claim is recorded, so the
        retry is safe — which a 4xx that looked terminal would discourage."""
        client = self._client(
            federation_credit_inbound_token="credit-secret",  # noqa: S106 - test fixture.
        )

        response = client.post(
            "/v1/internal/federation/credit-transfer",
            json=self._transfer_body(workspace_id="ws-never-seen"),
            headers={"x-trustedrouter-federation-credit-token": "credit-secret"},
        )

        assert response.status_code == 409, response.text

    def test_initiating_a_transfer_needs_the_internal_gateway_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The initiate route moves money, so it sits behind the token that can
        already move money — never behind a federation one."""
        settings = Settings(
            environment="test",
            sentry_dsn=None,
            # A configured token is what arms require_internal_gateway; the
            # environment only matters when no token is set at all.
            internal_gateway_token="gateway-secret",  # noqa: S106 - test fixture.
            stripe_secret_key=None,
            stripe_webhook_secret=None,
            federation_peer_token="peer-secret",  # noqa: S106 - test fixture.
            federation_credit_peer_base_url="https://aws.trustedrouter.com",
            federation_credit_peer_token="credit-secret",  # noqa: S106 - test fixture.
        )
        configure_store(InMemoryStore())
        client = TestClient(create_app(settings, init_observability=False))

        response = client.post(
            "/v1/internal/federation/credit-transfers",
            json={
                "transfer_id": "t-authz",
                "workspace_id": WORKSPACE,
                "amount_microdollars": 1_000,
            },
            headers={"x-trustedrouter-federation-token": "peer-secret"},
        )

        assert response.status_code == 401, response.text

    def test_an_unreachable_destination_reports_escrowed_not_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 5xx here would invite an operator to 'retry' with a NEW transfer
        id, which is a second debit. The escrow is durable, so the honest
        answer is 200 + escrowed + who holds it."""
        from trusted_router.routes.internal import federation as federation_routes

        def dead(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("destination unreachable")

        offline = CreditTransferClient(
            destination_base_url=PEER_URL,
            credit_token="credit-secret",  # noqa: S106 - test fixture.
            client=httpx.Client(transport=httpx.MockTransport(dead)),
        )
        monkeypatch.setattr(
            federation_routes, "credit_transfer_client_from_settings", lambda _s: offline
        )
        settings = Settings(
            environment="test",
            sentry_dsn=None,
            internal_gateway_token=None,
            stripe_secret_key=None,
            stripe_webhook_secret=None,
        )
        client = TestClient(create_app(settings, init_observability=False))
        user = STORE.ensure_user("owner", "owner@example.com")
        workspace = STORE.create_workspace(user.id, "src", trial_credit_microdollars=1_000_000)

        response = client.post(
            "/v1/internal/federation/credit-transfers",
            json={
                "transfer_id": "t-offline",
                "workspace_id": workspace.id,
                "amount_microdollars": 400_000,
            },
        )

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["state"] == credit_transfer.ESCROWED
        assert data["value_held_by"] == "escrow_pending_destination_verdict"
        # Debited and parked: spendable is down, and nothing was minted.
        money = STORE.credit_money[workspace.id]
        assert money.total_credits_microdollars == 600_000


class TestRandomizedConservation:
    def test_total_value_survives_a_random_sequence(self) -> None:
        """The property the whole design is for.

        A randomized mix of transfers, duplicate deliveries, cancels, crashes
        (escrow without delivery), recovery passes and overdraw attempts. After
        every single step the total across both planes must be exactly what it
        started as. A bug that mints on one specific interleaving shows up here
        as a number, not as an exception.
        """
        # Seeded for reproducibility: a conservation failure must be replayable
        # exactly, not "sometimes red on CI". Not a security use.
        rng = random.Random(20260803)  # noqa: S311
        for trial in range(25):
            source = _plane_with_credits(1_000_000)
            destination = _destination_plane()
            client = _client_for(destination)
            opened: list[str] = []

            for step in range(24):
                transfer_id = f"t-{trial}-{step}"
                action = rng.choice(
                    ["push", "push", "escrow_only", "duplicate", "cancel", "recover"]
                )
                amount = rng.choice([1, 50_000, 300_000, 900_000, 1_200_000])
                try:
                    if action == "push":
                        push_credit_transfer(
                            transfer_id=transfer_id,
                            workspace_id=WORKSPACE,
                            amount_microdollars=amount,
                            client=client,
                            store=source,
                        )
                        opened.append(transfer_id)
                    elif action == "escrow_only":
                        # A crash between debit and delivery.
                        source.open_credit_transfer(
                            transfer_id=transfer_id,
                            workspace_id=WORKSPACE,
                            amount_microdollars=amount,
                            destination=PEER_URL,
                        )
                        opened.append(transfer_id)
                    elif action == "duplicate" and opened:
                        replay = rng.choice(opened)
                        existing = source.get_credit_transfer(replay)
                        push_credit_transfer(
                            transfer_id=replay,
                            workspace_id=WORKSPACE,
                            amount_microdollars=existing.amount_microdollars,
                            client=client,
                            store=source,
                        )
                    elif action == "cancel" and opened:
                        cancel_credit_transfer(
                            transfer_id=rng.choice(opened), client=client, store=source
                        )
                    elif action == "recover":
                        recover_credit_transfers(client=client, store=source, limit=50)
                except ValueError:
                    # Overdraw refusals are expected and must move nothing.
                    pass

                assert _total_value(source, destination) == 1_000_000, (
                    f"conservation broken at trial={trial} step={step} action={action}"
                )
                assert _spendable(source) >= 0
                assert _spendable(destination) >= 0

            # Finish the sequence: everything still escrowed must resolve, and
            # the total must STILL be unchanged.
            recover_credit_transfers(client=client, store=source, limit=500)
            assert _escrowed(source) == 0
            assert _total_value(source, destination) == 1_000_000


class TestATransferIdIsNotAnAgreement:
    """An id identifies a transfer; it does NOT identify the destination.

    Idempotency is keyed on the transfer id, so a caller holding a client for
    the WRONG plane could previously be handed an escrow opened for another
    one. push/cancel would then ask that wrong plane to rule on value this
    plane is holding for somebody else — and a REJECTED tombstone from it
    releases the escrow while the real destination may already have accepted.
    Value in two places is not a retry, it is a double-spend.

    recover_credit_transfers guarded this from the start; push and cancel did
    not, and the gap survived a full adversarial pass.
    """

    def test_reopening_an_id_for_a_different_destination_is_refused(self) -> None:
        store = _plane_with_credits(10_000)
        store.open_credit_transfer(
            transfer_id="t-1",
            workspace_id=WORKSPACE,
            amount_microdollars=1_000,
            destination="https://a.example",
        )
        with pytest.raises(credit_transfer.DestinationMismatch):
            store.open_credit_transfer(
                transfer_id="t-1",
                workspace_id=WORKSPACE,
                amount_microdollars=1_000,
                destination="https://b.example",
            )

    def test_reopening_an_id_for_the_SAME_destination_still_works(self) -> None:
        """Idempotency must survive the guard — this is the retry path."""
        store = _plane_with_credits(10_000)
        first = store.open_credit_transfer(
            transfer_id="t-2",
            workspace_id=WORKSPACE,
            amount_microdollars=1_000,
            destination="https://a.example",
        )
        again = store.open_credit_transfer(
            transfer_id="t-2",
            workspace_id=WORKSPACE,
            amount_microdollars=1_000,
            destination="https://a.example",
        )
        assert again.id == first.id
        assert again.state == first.state

    def test_cancelling_via_the_wrong_destination_is_refused(self) -> None:
        """The path with no protection at all: cancel reads by id."""
        store = _plane_with_credits(10_000)
        store.open_credit_transfer(
            transfer_id="t-3",
            workspace_id=WORKSPACE,
            amount_microdollars=1_000,
            destination="https://a.example",
        )
        # A client aimed at a DIFFERENT plane than the escrow was opened for.
        wrong = _client_for(_destination_plane(), destination_base_url="https://b.example")
        with pytest.raises(credit_transfer.DestinationMismatch):
            cancel_credit_transfer(transfer_id="t-3", client=wrong, store=store)
