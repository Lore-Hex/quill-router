"""Driving a cross-plane credit transfer: transport plus the recovery pass.

The state machine, which plane holds the value in each state, and the
conservation invariant live in :mod:`trusted_router.credit_transfer`. This
module is only the part that talks to the other plane and then RECORDS what it
said. It decides nothing about money on its own.

The shape, and why each piece is load-bearing:

  * PUSH, from the plane that HOLDS the value. A pull ("peer asks home to send
    money") would have to be authorized by the federation peer token, and that
    token exists precisely because it must NOT be able to move money — see
    routes/internal/federation.py. So credit transfer gets its OWN token, and
    the direction of the call follows the direction of the value.

  * ESCROW COMMITS BEFORE THE NETWORK CALL. `push_credit_transfer` debits and
    commits, then delivers. A crash anywhere after that leaves the transfer
    ESCROWED, which `recover_credit_transfers` can finish by asking the
    destination again. The reverse order would mint money.

  * DELIVERY IS A QUESTION, NOT A COMMAND. The destination's reply is its
    verdict on an insert-once row, so a redelivery of an already-accepted
    transfer returns "accepted" without crediting twice, and a redelivery of an
    already-rejected one returns "rejected" without crediting at all. The
    source applies whatever came back. That is why recovery is safe to run
    repeatedly and why it is safe to run concurrently with a first attempt.

  * RECOVERY NEVER TIMES OUT INTO A REFUND. There is no elapsed time after
    which "the destination probably didn't take it" becomes safe; it either
    answers or the value stays escrowed. `cancel_credit_transfer` exists for
    the operator who wants the value back, and it works by asking the
    DESTINATION to reject — never by crediting the source directly.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from trusted_router import credit_transfer
from trusted_router.credit_transfer import (
    validate_amount,
    validate_transfer_id,
)
from trusted_router.storage import STORE
from trusted_router.storage_models import CreditTransfer

logger = logging.getLogger(__name__)

#: Transfers are operator/customer-initiated, not on an inference request's
#: latency budget, so this is far more generous than the key-resolve timeouts
#: in services/federation.py. Being slow costs nothing here; giving up early
#: leaves value escrowed for no reason.
CONNECT_TIMEOUT_SECONDS = 5.0
TOTAL_TIMEOUT_SECONDS = 30.0

CREDIT_TRANSFER_PATH = "/v1/internal/federation/credit-transfer"
CREDIT_TOKEN_HEADER = "x-trustedrouter-federation-credit-token"  # noqa: S105 - header name.


class CreditTransferUnavailable(RuntimeError):
    """The destination plane could not be reached, or answered unusably.

    NOT a verdict. The transfer stays ESCROWED and the value stays on the
    source plane; a later delivery or recovery pass resolves it. Callers must
    never translate this into a refund.
    """


class CreditTransferClient:
    """Asks a destination plane to accept (or reject) a transfer id.

    Deliberately thin: one request, one verdict, no retry loop of its own.
    Retrying is the recovery pass's job, and it is safe there because the
    destination's answer is idempotent.
    """

    def __init__(
        self,
        *,
        destination_base_url: str,
        credit_token: str,
        source_plane: str = "",
        client: httpx.Client | None = None,
    ) -> None:
        self._destination = destination_base_url.rstrip("/")
        self._credit_token = credit_token
        self._source_plane = source_plane
        self._client = client

    @property
    def destination(self) -> str:
        return self._destination

    def deliver(self, transfer: CreditTransfer, *, accept: bool = True) -> str:
        """Return the destination's DECIDED outcome for this transfer id.

        The returned outcome may disagree with `accept`: the destination's
        claim row is written once and every later caller learns that verdict.
        A cancel that arrives after an accept gets back "accepted", and the
        source must record exactly that.
        """
        url = f"{self._destination}{CREDIT_TRANSFER_PATH}"
        timeout = httpx.Timeout(TOTAL_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
        payload = {
            "transfer_id": transfer.id,
            "workspace_id": transfer.workspace_id,
            "amount_microdollars": transfer.amount_microdollars,
            "action": credit_transfer.ACCEPTED if accept else credit_transfer.REJECTED,
            "source_plane": self._source_plane,
        }
        headers = {CREDIT_TOKEN_HEADER: self._credit_token}
        try:
            if self._client is None:
                with httpx.Client(timeout=timeout) as owned:
                    response = owned.post(url, json=payload, headers=headers)
            else:
                response = self._client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise CreditTransferUnavailable(f"destination unreachable: {exc}") from exc

        if response.status_code != 200:
            # EVERY non-200 is "unknown", including 4xx. A 401 means our token
            # is wrong and a 400 means we sent something malformed — neither
            # tells us whether an EARLIER delivery of this id was accepted, so
            # neither may be treated as a rejection. Only the destination's
            # explicit verdict can move a transfer out of escrow.
            raise CreditTransferUnavailable(
                f"destination returned {response.status_code} for transfer {transfer.id}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise CreditTransferUnavailable("destination returned a non-JSON body") from exc
        data = body.get("data") if isinstance(body, dict) else None
        outcome = str(data.get("outcome")) if isinstance(data, dict) else ""
        if outcome not in credit_transfer.CLAIM_OUTCOMES:
            raise CreditTransferUnavailable(
                f"destination returned an unusable outcome {outcome!r}"
            )
        return outcome


def push_credit_transfer(
    *,
    transfer_id: str,
    workspace_id: str,
    amount_microdollars: int,
    client: CreditTransferClient,
    store: Any = STORE,
) -> CreditTransfer:
    """Move credits from THIS plane to the destination. Idempotent by id.

    Order is the whole point:

      1. escrow (conditional debit) and COMMIT — value held by this plane,
         spendable by nobody;
      2. ask the destination for its verdict;
      3. record that verdict — value held by whichever plane the verdict says.

    A crash after step 1 leaves the transfer ESCROWED, which
    `recover_credit_transfers` resolves. A crash after step 2 but before step 3
    is the same case: the destination's answer is durable and repeating the
    question returns it again.
    """
    transfer_id = validate_transfer_id(transfer_id)
    amount = validate_amount(amount_microdollars)
    transfer = store.open_credit_transfer(
        transfer_id=transfer_id,
        workspace_id=workspace_id,
        amount_microdollars=amount,
        destination=client.destination,
    )
    if transfer.state != credit_transfer.ESCROWED:
        # Already resolved by an earlier delivery or a recovery pass. Nothing
        # to do; re-asking would be harmless but pointless.
        return transfer
    return _deliver_and_record(transfer, client=client, store=store, accept=True)


def cancel_credit_transfer(
    *,
    transfer_id: str,
    client: CreditTransferClient,
    store: Any = STORE,
) -> CreditTransfer:
    """Try to get an escrowed transfer's value back — via the DESTINATION.

    This does NOT credit the source directly. It asks the destination to write
    a REJECTED tombstone for the id; only if that write wins the race does the
    source return the escrow. If the destination had already accepted, the
    reply is "accepted" and this call DELIVERS the transfer instead of
    cancelling it. That is not a failure — it is the protocol refusing to
    double-spend, and the caller sees the resulting state.

    CAVEAT worth knowing before relying on this: cancel INTENT is not durable.
    If the destination is unreachable, this raises and the transfer stays
    ESCROWED with nothing recorded about the operator's wish to cancel — so a
    recovery pass that runs before the operator retries will DELIVER it. Value
    is conserved either way and one verdict still wins, but the intent is
    silently reversed. Making cancellation durable would need a fourth state
    ("cancel requested"), which was rejected here for a reason that may not
    hold forever: it is a state a crash can strand between "written" and
    "sent", and the operator can simply retry the cancel, which is idempotent.
    """
    transfer_id = validate_transfer_id(transfer_id)
    transfer = store.get_credit_transfer(transfer_id)
    if transfer is None:
        raise KeyError(transfer_id)
    # cancel reads by id and never passes through open_credit_transfer, so it
    # gets none of that path's protection. Without this, cancelling a transfer
    # escrowed for destination A using a client for destination B asks B to
    # write a REJECTED tombstone for an id B has never seen; B agrees, the
    # source returns the escrow, and A may already have accepted the same
    # transfer. Value ends up in two places.
    credit_transfer.require_matching_destination(transfer, client.destination)
    if transfer.state != credit_transfer.ESCROWED:
        return transfer
    return _deliver_and_record(transfer, client=client, store=store, accept=False)


def recover_credit_transfers(
    *,
    client: CreditTransferClient,
    store: Any = STORE,
    limit: int = 100,
) -> dict[str, Any]:
    """Resolve every ESCROWED transfer by re-asking the destination.

    This is the crash-recovery path, and it is deliberately dumb: for each
    unresolved transfer, ask again and record the answer. It is safe to run at
    any time, concurrently with live pushes, because the destination's verdict
    is an insert-once row — asking twice cannot credit twice.

    A destination that is still unreachable leaves the transfer ESCROWED and
    counted under "unresolved". Value is never lost, and never returned on a
    guess.

    It resolves toward DELIVERY, because that was the intent that created the
    escrow. See `cancel_credit_transfer` for the consequence: a cancel that
    never reached the destination is not recorded anywhere, so this can deliver
    a transfer an operator meant to cancel.
    """
    delivered = returned = unresolved = skipped = failed = 0
    for transfer in store.list_open_credit_transfers(limit):
        if transfer.destination and transfer.destination != client.destination:
            # This transfer was escrowed for a DIFFERENT plane. Asking this one
            # about it would not be a harmless no-op: a destination accepts any
            # (id, workspace, amount) presented with a valid credit token — it
            # has no way to check that this source escrowed it — so the wrong
            # plane would happily credit itself and the intended one would
            # never receive the value.
            skipped += 1
            continue
        try:
            resolved = _deliver_and_record(transfer, client=client, store=store, accept=True)
        except CreditTransferUnavailable:
            unresolved += 1
            logger.warning(
                "credit transfer %s remains escrowed: destination unreachable",
                transfer.id,
            )
            continue
        except Exception:  # noqa: BLE001 - one bad row must not abort the batch.
            # Same reasoning as the settle-outbox drain: a single anomalous row
            # (a disagreeing verdict, a storage blip) must not strand every
            # OTHER escrow behind it. The row keeps its escrow and is retried
            # next pass; nothing is refunded on the way out.
            failed += 1
            logger.exception("credit transfer %s could not be resolved", transfer.id)
            continue
        if resolved.state == credit_transfer.DELIVERED:
            delivered += 1
        else:
            returned += 1
    return {
        "delivered": delivered,
        "returned": returned,
        "unresolved": unresolved,
        "skipped_other_destination": skipped,
        "failed": failed,
    }


def _deliver_and_record(
    transfer: CreditTransfer,
    *,
    client: CreditTransferClient,
    store: Any,
    accept: bool,
) -> CreditTransfer:
    """Ask, then record. The source contributes no opinion of its own."""
    outcome = client.deliver(transfer, accept=accept)
    return store.resolve_credit_transfer(transfer_id=transfer.id, outcome=outcome)


def credit_transfer_client_from_settings(settings: Any) -> CreditTransferClient | None:
    """Build the outbound client, or None when this plane cannot push credits.

    Both halves are required. A base URL with no token would produce 401s that
    look like an outage, and a token with no base URL is a secret configured
    for nothing.
    """
    base_url = str(getattr(settings, "federation_credit_peer_base_url", "") or "")
    token = str(getattr(settings, "federation_credit_peer_token", "") or "")
    if not base_url or not token:
        return None
    return CreditTransferClient(
        destination_base_url=base_url,
        credit_token=token,
        # Audit label only: it tells the destination's operator which plane
        # sent the value. Never used for authorization on either side.
        source_plane=str(getattr(settings, "api_base_url", "") or ""),
    )
