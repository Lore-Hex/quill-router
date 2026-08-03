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
from unittest import mock

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
        try:
            outcome = destination.claim_credit_transfer(
                transfer_id=body["transfer_id"],
                workspace_id=body["workspace_id"],
                amount_microdollars=body["amount_microdollars"],
                source=body.get("source_plane", ""),
                accept=body["action"] == credit_transfer.ACCEPTED,
            )
        except CreditTransferConflict as exc:
            # Mirrors the real route: a reused id whose recorded terms do not
            # answer this request is a 409, never a verdict. The source must
            # see it as "unknown" and keep the value escrowed.
            return httpx.Response(409, json={"error": {"message": str(exc)}})
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


class TestReusedIdWithDifferentTerms:
    """A transfer id is an idempotency key, NOT a statement of what moved.

    Every "already done, nothing to do" branch in this design sits on the
    losing side of an insert-once. Each one is about to skip a balance change,
    so each one has to be sure the stored row answers THIS request. Where it
    did not, value went missing in both directions and every plane reported
    success — the worst shape a money bug can have.
    """

    def test_the_destination_refuses_a_verdict_meant_for_another_transfer(self) -> None:
        """DESTROYS money: a second source plane banks a verdict it never earned.

        Two AWS regions run separate DSQL clusters and both push to the same
        destination. An operator-chosen id (`topup-2026-08`) collides. Before
        the fix the destination replied "accepted" off the FIRST region's claim
        row without crediting anything, and the second region — which had
        already debited itself — recorded DELIVERED.
        """
        destination = _destination_plane()

        first = destination.claim_credit_transfer(
            transfer_id="topup-2026-08",
            workspace_id=WORKSPACE,
            amount_microdollars=1_000_000,
            source="eu-west-1",
            accept=True,
        )
        assert first == credit_transfer.ACCEPTED
        assert _spendable(destination) == 1_000_000

        # A DIFFERENT amount under the same id: not a retry of the above.
        with pytest.raises(credit_transfer.TransferIdReused):
            destination.claim_credit_transfer(
                transfer_id="topup-2026-08",
                workspace_id=WORKSPACE,
                amount_microdollars=750_000,
                source="eu-west-3",
                accept=True,
            )
        # And a different workspace under the same id.
        destination.upsert_federated_api_key(
            {
                "lookup_hash": "lh-other",
                "key_hash": "kh-other",
                "workspace_id": "ws-other",
                "name": "federated",
            }
        )
        with pytest.raises(credit_transfer.TransferIdReused):
            destination.claim_credit_transfer(
                transfer_id="topup-2026-08",
                workspace_id="ws-other",
                amount_microdollars=1_000_000,
                source="eu-west-3",
                accept=True,
            )
        assert _spendable(destination) == 1_000_000
        assert _spendable(destination, "ws-other") == 0

    def test_a_second_region_keeps_its_value_escrowed_instead_of_losing_it(self) -> None:
        """End to end: the total across all three planes must not shrink.

        The refusal is only worth having because of where the value ends up.
        The second region's push must stay ESCROWED — parked, visible and
        recoverable — rather than being marked DELIVERED against a credit that
        never happened.
        """
        destination = _destination_plane()
        region_a = _plane_with_credits(1_000_000)
        region_b = _plane_with_credits(1_000_000)
        # EVERY term matches — same id, same workspace, same amount. Only the
        # source label distinguishes two regions that independently chose
        # "topup-2026-08", which is why the claim's identity includes it.
        from_a = _client_for(destination, source_plane="https://eu-west-1.example")
        from_b = _client_for(destination, source_plane="https://eu-west-3.example")

        push_credit_transfer(
            transfer_id="topup-2026-08",
            workspace_id=WORKSPACE,
            amount_microdollars=1_000_000,
            client=from_a,
            store=region_a,
        )
        with pytest.raises(CreditTransferUnavailable):
            push_credit_transfer(
                transfer_id="topup-2026-08",
                workspace_id=WORKSPACE,
                amount_microdollars=1_000_000,
                client=from_b,
                store=region_b,
            )

        assert region_a.credit_transfers["topup-2026-08"].state == credit_transfer.DELIVERED
        # Region B debited, so its spendable is 0 — but the value is in escrow,
        # not gone. `_undecided_escrow` counts it because the destination's
        # claim row does not answer for B's transfer.
        assert region_b.credit_transfers["topup-2026-08"].state == credit_transfer.ESCROWED
        assert _spendable(region_b) == 0
        assert _escrowed(region_b) == 1_000_000
        # Region A's push landed: it is debited, the destination holds it.
        assert _spendable(region_a) == 0
        assert _spendable(destination) == 1_000_000
        # Nothing destroyed: 2,000,000 in, 2,000,000 accounted for.
        assert (
            _spendable(region_a)
            + _spendable(region_b)
            + _escrowed(region_b)
            + _spendable(destination)
        ) == 2_000_000

    def test_the_source_refuses_to_report_another_workspaces_transfer(self) -> None:
        """Silently moves nothing while reporting a completed transfer.

        An operator funding ws-B with an id already spent on ws-A used to get
        back A's record — state `delivered`, and (before the reply carried a
        workspace_id) no field to notice the swap by.
        """
        source = _plane_with_credits(1_000_000)
        source.workspaces["ws-B"] = source.workspaces[WORKSPACE]
        source.credit_money["ws-B"] = source.credit_money[WORKSPACE]

        source.open_credit_transfer(
            transfer_id="t-shared",
            workspace_id=WORKSPACE,
            amount_microdollars=100_000,
            destination=PEER_URL,
        )
        with pytest.raises(credit_transfer.TransferIdReused):
            source.open_credit_transfer(
                transfer_id="t-shared",
                workspace_id="ws-B",
                amount_microdollars=100_000,
                destination=PEER_URL,
            )

    def test_the_source_refuses_a_reused_id_with_a_different_amount(self) -> None:
        source = _plane_with_credits(1_000_000)
        source.open_credit_transfer(
            transfer_id="t-amount",
            workspace_id=WORKSPACE,
            amount_microdollars=100_000,
            destination=PEER_URL,
        )
        with pytest.raises(credit_transfer.TransferIdReused):
            source.open_credit_transfer(
                transfer_id="t-amount",
                workspace_id=WORKSPACE,
                amount_microdollars=250_000,
                destination=PEER_URL,
            )
        # The first escrow is untouched, and no second debit happened.
        assert _spendable(source) == 900_000


class TestResolutionIsGuardedByAnInsertOnceRow:
    """The one transition that was decided by a read-then-write.

    `credit_transfer.py` claims every transition "changes bucket only via a
    transition guarded by a single INSERT-ONCE row". `open` and `claim` were;
    `resolve` was not — it read the transfer, compared `state` in Python, and
    then refunded unconditionally. Two callers that both read ESCROWED both
    refunded. That interleaving is not exotic: `recover_credit_transfers`
    documents itself as safe to run concurrently with a first attempt, and an
    operator cancel racing the recovery cron reaches it directly.
    """

    def _escrowed_pg(self) -> Any:
        conn = sqlite_postgres_conn()
        store = postgres_store_on(conn)
        store.upsert_federated_api_key(
            {
                "lookup_hash": "lh-r",
                "key_hash": "kh-r",
                "workspace_id": WORKSPACE,
                "name": "federated",
            }
        )
        store.claim_credit_transfer(
            transfer_id="seed",
            workspace_id=WORKSPACE,
            amount_microdollars=1_000_000,
            source="home",
            accept=True,
        )
        store.open_credit_transfer(
            transfer_id="t-race",
            workspace_id=WORKSPACE,
            amount_microdollars=600_000,
            destination=PEER_URL,
        )
        return conn, store

    def test_a_second_resolve_that_still_sees_escrowed_refunds_nothing(self) -> None:
        """THE conservation test for this transition.

        Rewinding the transfer row to `escrowed` after a completed refund
        reproduces exactly what the losing transaction sees under READ
        COMMITTED: its snapshot was taken before the winner committed, so the
        Python state check passes and it walks straight into the balance
        change. The insert-once row is the only thing that can stop it, which
        is why the row and not the state check is the guard.
        """
        conn, store = self._escrowed_pg()
        assert conn.spendable(WORKSPACE) == 400_000

        store.resolve_credit_transfer(
            transfer_id="t-race", outcome=credit_transfer.REJECTED
        )
        assert conn.spendable(WORKSPACE) == 1_000_000

        # Rewind ONLY the state, as a stale snapshot would show it.
        with conn.transaction() as tx:
            store._write_entity_tx(
                tx,
                "credit_transfer",
                "t-race",
                {
                    "id": "t-race",
                    "workspace_id": WORKSPACE,
                    "amount_microdollars": 600_000,
                    "destination": PEER_URL,
                    "state": credit_transfer.ESCROWED,
                    "created_at": "2026-08-01T00:00:00Z",
                    "resolved_at": None,
                },
            )

        resolved = store.resolve_credit_transfer(
            transfer_id="t-race", outcome=credit_transfer.REJECTED
        )

        assert resolved.state == credit_transfer.RETURNED
        # 1,600,000 here would be 600,000 minted out of nothing.
        assert conn.spendable(WORKSPACE) == 1_000_000

    def test_a_disagreeing_second_resolve_raises_instead_of_applying(self) -> None:
        """A cancel and a delivery cannot both be recorded for one escrow."""
        conn, store = self._escrowed_pg()
        store.resolve_credit_transfer(
            transfer_id="t-race", outcome=credit_transfer.ACCEPTED
        )
        with conn.transaction() as tx:
            store._write_entity_tx(
                tx,
                "credit_transfer",
                "t-race",
                {
                    "id": "t-race",
                    "workspace_id": WORKSPACE,
                    "amount_microdollars": 600_000,
                    "destination": PEER_URL,
                    "state": credit_transfer.ESCROWED,
                    "created_at": "2026-08-01T00:00:00Z",
                    "resolved_at": None,
                },
            )

        with pytest.raises(CreditTransferConflict):
            store.resolve_credit_transfer(
                transfer_id="t-race", outcome=credit_transfer.REJECTED
            )
        # The accepted verdict stands: the source was debited and stays debited.
        assert conn.spendable(WORKSPACE) == 400_000

    def test_the_inmemory_twin_does_not_record_a_refund_it_cannot_make(self) -> None:
        """A dict store has no rollback, so ordering IS the transaction.

        Recording RETURNED and then failing to find the balance row leaves the
        transfer saying the value came back when it did not — value destroyed
        in the very store the conservation tests assert against.
        """
        source = _plane_with_credits(1_000_000)
        source.open_credit_transfer(
            transfer_id="t-mem",
            workspace_id=WORKSPACE,
            amount_microdollars=100_000,
            destination=PEER_URL,
        )
        del source.credit_money[WORKSPACE]

        with pytest.raises(RuntimeError):
            source.resolve_credit_transfer(
                transfer_id="t-mem", outcome=credit_transfer.REJECTED
            )

        # Still ESCROWED, so the value is still accounted for and a later
        # recovery pass can return it once the balance row is back.
        assert source.credit_transfers["t-mem"].state == credit_transfer.ESCROWED


class TestRecoveryQueueCannotStarve:
    def test_skipped_transfers_do_not_hide_the_ones_behind_them(self) -> None:
        """A permanently-unresolvable row must not block every later escrow.

        Rows escrowed for a DIFFERENT destination are skipped on every pass and
        never leave the queue. Re-reading "the first N" each pass means that
        once N of them sort ahead of the live ones, recovery silently stops
        resolving anything — while still reporting success.
        """
        source = _plane_with_credits(1_000_000)
        destination = _destination_plane()

        # Three rows for a plane this pass will never talk to, sorting FIRST.
        for index in range(3):
            source.open_credit_transfer(
                transfer_id=f"aaa-other-{index}",
                workspace_id=WORKSPACE,
                amount_microdollars=1_000,
                destination="https://elsewhere.example",
            )
        # ...and one real escrow for the plane we are about to recover against.
        source.open_credit_transfer(
            transfer_id="zzz-live",
            workspace_id=WORKSPACE,
            amount_microdollars=250_000,
            destination=PEER_URL,
        )

        # A page size smaller than the skip backlog: the live row is invisible
        # to an unpaged scan.
        report = recover_credit_transfers(
            client=_client_for(destination), store=source, limit=2
        )

        assert report["skipped_other_destination"] == 3
        assert report["delivered"] == 1
        assert source.credit_transfers["zzz-live"].state == credit_transfer.DELIVERED
        assert _spendable(destination) == 250_000


class TestConflictIsNeverReportedAsInsufficientCredits:
    def test_a_reused_id_is_409_not_402(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """402 here tells an operator the exact wrong thing.

        `CreditTransferConflict` subclasses `ValueError`, so it used to land in
        the insufficient-credits arm, whose message promises "the same transfer
        id is still usable after a top-up". For a reused id that is false — the
        escrow is debited and live — and an operator who believes it retries
        with a fresh id and takes a SECOND debit.
        """
        from trusted_router.routes.internal import federation as federation_routes

        destination = _destination_plane()
        settings = Settings(
            environment="test",
            sentry_dsn=None,
            internal_gateway_token=None,
            stripe_secret_key=None,
            stripe_webhook_secret=None,
        )
        monkeypatch.setattr(
            federation_routes,
            "credit_transfer_client_from_settings",
            lambda _s: _client_for(destination),
        )
        client = TestClient(create_app(settings, init_observability=False))
        user = STORE.ensure_user("owner", "owner@example.com")
        first = STORE.create_workspace(user.id, "a", trial_credit_microdollars=1_000_000)
        second = STORE.create_workspace(user.id, "b", trial_credit_microdollars=1_000_000)
        # The destination can only credit a workspace it has federated in.
        for index, workspace_id in enumerate((first.id, second.id)):
            destination.upsert_federated_api_key(
                {
                    "lookup_hash": f"lh-dup-{index}",
                    "key_hash": f"kh-dup-{index}",
                    "workspace_id": workspace_id,
                    "name": "federated",
                }
            )

        ok = client.post(
            "/v1/internal/federation/credit-transfers",
            json={
                "transfer_id": "t-dup",
                "workspace_id": first.id,
                "amount_microdollars": 100_000,
            },
        )
        assert ok.status_code == 200, ok.text
        # The reply names the workspace it actually moved value for.
        assert ok.json()["data"]["workspace_id"] == first.id

        clash = client.post(
            "/v1/internal/federation/credit-transfers",
            json={
                "transfer_id": "t-dup",
                "workspace_id": second.id,
                "amount_microdollars": 100_000,
            },
        )

        assert clash.status_code == 409, clash.text
        assert "insufficient" not in clash.text.lower()
        # The second workspace was never debited and never reported delivered.
        assert STORE.credit_money[second.id].total_credits_microdollars == 1_000_000


class TestResolveTakesTheRowLock:
    """The refund transition must SELECT ... FOR UPDATE.

    resolve_credit_transfer is the one transition not guarded by an
    insert-once row: it reads the transfer, compares state in Python, then
    writes and refunds. Without a lock on that read, two concurrent resolvers
    (an operator cancel and the scheduled recovery pass — whose docstring
    explicitly says it may run concurrently with a first attempt) both read
    ESCROWED, both pass the state check, and both run the unconditional
    refund. That MINTS money.

    Why this test asserts on the call rather than driving the race: the
    Postgres fake executes the real SQL on SQLite, which has no cross-
    connection row locking to observe, so a genuine interleaving cannot be
    reproduced here. The conformance suite covers it against a real backend
    when TR_CONFORMANCE_POSTGRES_DSN is set. Until that runs, this pins the
    one line that stands between us and a mint — removing for_update turns
    this red, which is the whole point.
    """

    def test_the_escrow_read_is_locked(self) -> None:
        from trusted_router.storage_postgres import PostgresStore

        seen: list[dict[str, Any]] = []
        real = PostgresStore._read_entity_tx

        def spy(self: Any, conn: Any, kind: str, key: str, model: Any, **kw: Any) -> Any:
            if kind == "credit_transfer":
                seen.append(kw)
            return real(self, conn, kind, key, model, **kw)

        conn = sqlite_postgres_conn()
        store = postgres_store_on(conn)
        store.upsert_federated_api_key(
            {
                "lookup_hash": "lh-lock",
                "key_hash": "kh-lock",
                "workspace_id": WORKSPACE,
                "name": "federated",
            }
        )
        store.claim_credit_transfer(
            transfer_id="seed-lock",
            workspace_id=WORKSPACE,
            amount_microdollars=10_000,
            source=PEER_URL,
            accept=True,
        )
        store.open_credit_transfer(
            transfer_id="lock-1",
            workspace_id=WORKSPACE,
            amount_microdollars=1_000,
            destination=PEER_URL,
        )
        with mock.patch.object(PostgresStore, "_read_entity_tx", spy):
            store.resolve_credit_transfer(transfer_id="lock-1", outcome=credit_transfer.REJECTED)

        assert seen, "resolve_credit_transfer did not read the transfer row"
        assert any(kw.get("for_update") for kw in seen), (
            "the escrow read in resolve_credit_transfer must use for_update=True; "
            "without it two concurrent resolvers both see ESCROWED and both refund"
        )
