"""Cross-plane credit transfer: the state machine and its conservation law.

Backend-neutral on purpose — states, validation and the invariant live here so
the stores, the HTTP surface and the recovery driver all agree on one
definition instead of three that drift.

Why this exists
---------------
Federation copies IDENTITY between planes because an identity is an assertion
and assertions copy safely. A balance is a QUANTITY: copying it mints money.
So a federated key arrives with zero local credits (services/federation.py),
and spending on the peer plane requires value to actually MOVE. This module is
that move.

THE INVARIANT
-------------
For every workspace, across ANY interleaving of transfers, retries, duplicate
deliveries, crashes and refunds::

    spendable(source plane) + escrowed(UNDECIDED) + spendable(destination plane)
        == constant

Every microdollar is in exactly one of those three buckets at every instant,
and it changes bucket only via a transition guarded by a single INSERT-ONCE
row. No transition adds to one bucket without removing from another in the
same transaction, so no sequence of operations can create or destroy value.

UNDECIDED means *the destination has not written its claim row* — NOT merely
"the source's record still says ESCROWED". The distinction is the whole
subtlety of the middle bucket, so be exact about it:

    Once the destination's claim row exists, the value is already wherever
    that row says it is, whether or not the source has heard. The source's
    lingering ESCROWED record is then a STALE VIEW, not a third copy of the
    money.

Count it the other way — treat every source-side ESCROWED record as held
value, regardless of the destination's row — and the accounting appears to
mint on exactly the interleaving that matters most: destination accepted, ack
lost. Nothing was minted there; the auditor's formula was wrong. This is worth
belabouring because that formula is what an audit would use to decide whether
the system is solvent.

It follows that the source can NEVER answer "who holds this value?" for an
unresolved escrow on its own. Only the destination's claim row answers it,
which is the same fact that makes unilateral cancellation unsafe below.

THE STATES, AND WHICH PLANE HOLDS THE VALUE
-------------------------------------------
Source-side record (``credit_transfer``, one per transfer id):

``ESCROWED``
    The source plane has DEBITED the amount from the workspace's spendable
    balance in the same transaction that created this record. **The value is
    parked in escrow on the SOURCE plane and is spendable by NOBODY** — not the
    source (already debited), not the destination (not yet credited).

    With one caveat that matters for accounting: this state means the source
    does not YET KNOW the outcome, which is not the same as the outcome not
    having happened. If the destination has already written its claim row, the
    value has already moved (or been refused) and this record is simply stale.
    See "THE INVARIANT" above.

    This is the only durable intermediate state, and it is deliberately
    durable: a crash here loses nothing, it just leaves the value parked and
    visible until the destination is asked again.

``DELIVERED``
    The destination plane's claim row says ``ACCEPTED``. **The value is held by
    the DESTINATION plane**, spendable there. The source's escrow is void: the
    debit already happened at ESCROWED and nothing further touches the source
    balance. Terminal.

``RETURNED``
    The destination plane's claim row says ``REJECTED``. **The value is held by
    the SOURCE plane**, spendable again — the escrowed amount was credited back
    in the same transaction that recorded this state. Terminal.

Destination-side record (``credit_transfer_claim``, one per transfer id):

``ACCEPTED``
    Credited to the local balance in the same transaction as this row.
``REJECTED``
    A tombstone. Nothing was credited and nothing ever can be for this id.

ORDERING: DEBIT ALWAYS PRECEDES CREDIT
--------------------------------------
The source escrows (debits) and commits BEFORE the destination is contacted.
The reverse order — credit first, debit after — mints money on any crash
between the two, because the value would exist on both planes simultaneously.
This order can only ever park value, never duplicate it. Parked value is an
operational problem; duplicated value is a solvency problem.

WHY THE SOURCE MAY NOT UNILATERALLY CANCEL
------------------------------------------
The obvious "the destination didn't answer, so give the money back" is a
double-spend. The destination may have accepted and lost the ack. So the
source NEVER decides a transfer's fate: it only RECORDS the destination's
verdict, and the verdict is a single insert-once row on the destination. To
cancel, the source asks the destination to REJECT the id; the tombstone that
insert writes makes a later accept impossible. Accept and reject race exactly
once, on one row, on one plane, and the loser learns the winner's answer.

The consequence is deliberate: if the destination is unreachable forever, the
value stays ESCROWED forever. Visible, auditable, recoverable the moment the
destination answers — and never minted. For money that is the correct failure
mode; there is no timeout that can safely stand in for the destination's
answer.

WHY THERE IS NO "IN FLIGHT" STATE
---------------------------------
A state meaning "we have sent the delivery but not heard back" would be a lie:
a process can crash between committing that state and sending the request, or
after sending and before the reply. ESCROWED already means exactly "the fate is
unknown, ask the destination", which is the only thing a recovery pass can act
on. Fewer states, no unrecoverable gaps.
"""

from __future__ import annotations

from typing import Any

# --- Source-side states ---------------------------------------------------
#: Debited from the source, credited nowhere. Value held by the SOURCE plane.
ESCROWED = "escrowed"
#: Destination accepted. Value held by the DESTINATION plane. Terminal.
DELIVERED = "delivered"
#: Destination rejected; escrow returned. Value held by the SOURCE plane. Terminal.
RETURNED = "returned"

#: Once a transfer leaves ESCROWED its fate is fixed — the destination's claim
#: row is immutable, so nothing can move it again.
TERMINAL_STATES = frozenset({DELIVERED, RETURNED})

# --- Destination-side claim outcomes --------------------------------------
#: Credited locally in the same transaction as the claim row.
ACCEPTED = "accepted"
#: Tombstone: nothing credited, and nothing ever can be for this transfer id.
REJECTED = "rejected"

CLAIM_OUTCOMES = frozenset({ACCEPTED, REJECTED})

#: The source state each destination verdict resolves to. This mapping is the
#: whole coupling between the two planes: the source has no other way to leave
#: ESCROWED.
STATE_FOR_OUTCOME = {ACCEPTED: DELIVERED, REJECTED: RETURNED}


class CreditTransferConflict(ValueError):
    """A second, DISAGREEING verdict arrived for a transfer id.

    Never a retry (a repeated identical verdict is a no-op) and never
    reachable through the normal protocol, because the destination's claim row
    is written once and never rewritten. Reaching this means two different
    destinations answered for one id, or a caller invented a verdict the
    destination never gave. Both would break conservation if applied, so this
    raises instead of quietly picking one.
    """


def validate_transfer_id(transfer_id: str) -> str:
    """A transfer id is the idempotency key for the whole cross-plane move.

    It must be supplied by the CALLER and stable across retries — a generated
    id would make every retry a new transfer, i.e. a new debit, which is the
    exact failure this design exists to prevent.
    """
    cleaned = str(transfer_id or "").strip()
    if not cleaned:
        raise ValueError("transfer_id is required")
    if len(cleaned) > 128:
        raise ValueError("transfer_id must be at most 128 characters")
    return cleaned


def validate_amount(amount_microdollars: int) -> int:
    """Strictly positive. Zero is a no-op that would still consume a transfer
    id, and a negative amount would turn a "transfer" into a remote debit of
    the destination — a way to reach into another plane's balance that the
    protocol must not have.
    """
    if isinstance(amount_microdollars, bool) or not isinstance(amount_microdollars, int):
        raise ValueError("amount_microdollars must be an integer")
    if amount_microdollars <= 0:
        raise ValueError("amount_microdollars must be positive")
    return int(amount_microdollars)


def validate_outcome(outcome: str) -> str:
    if outcome not in CLAIM_OUTCOMES:
        raise ValueError(f"unknown claim outcome {outcome!r}")
    return outcome


class TransferIdReused(CreditTransferConflict):
    """A transfer id was reused with DIFFERENT terms.

    Idempotency is keyed on the transfer id, but AN ID IS NOT AN AGREEMENT.
    The id says "this is the same move"; the workspace, the amount and the
    destination say WHICH move. Treating a reused id as a retry without
    checking the terms loses value in both directions:

      * On the SOURCE, returning workspace A's escrow to a caller asking about
        workspace B reports a completed transfer that moved nothing for B.
      * On the DESTINATION, replaying the recorded verdict for a DIFFERENT
        (workspace, amount) hands a second source plane "accepted" for free.
        That plane debited; nothing here was credited. Value destroyed, and
        both planes report success.

    A reused id with different terms is never a retry, so it is refused rather
    than answered. Refusing parks value; answering loses it.
    """


class DestinationMismatch(TransferIdReused):
    """A transfer id was reused for a DIFFERENT destination.

    The most dangerous flavour of reuse, and why it keeps its own type: two
    planes must not be able to answer for one escrow. A REJECTED tombstone
    written by B releases an escrow that A may already have accepted, putting
    the value in two places at once.
    """


def _field(record: Any, name: str) -> Any:
    """Read a field off either a `CreditTransfer` or a raw claim dict.

    The source keeps a dataclass and the destination keeps a JSON row; both
    have to be compared against the same request, and one accessor is better
    than two copies of the comparison that can drift apart.
    """
    if isinstance(record, dict):
        return record.get(name)
    return getattr(record, name, None)


def require_matching_destination(existing: Any, destination: str) -> None:
    """Refuse to hand an escrow to a caller bound for somewhere else.

    A transfer escrowed FOR destination A, returned to a caller holding a
    client for destination B, lets push/cancel ask B to rule on value held for
    A. A REJECTED tombstone from B releases the escrow while A may already have
    accepted — value in two places at once. Fail closed instead.

    Compared only when BOTH sides carry a label: the destination is an audit
    field, a record written before it existed has none, and refusing to resolve
    a legitimately escrowed transfer over a missing label would strand value
    that is otherwise recoverable.
    """
    held = str(_field(existing, "destination") or "")
    asked = str(destination or "")
    if held and asked and held != asked:
        raise DestinationMismatch(
            f"transfer {_field(existing, 'id') or '?'!r} is escrowed for {held!r}, "
            f"not {asked!r}"
        )


def require_matching_transfer(
    transfer_id: str,
    existing: Any,
    *,
    workspace_id: str,
    amount_microdollars: int,
    destination: str | None = None,
    source: str | None = None,
) -> None:
    """Assert a reused id names the SAME move, on either side of the wire.

    Called on the LOSING branch of every insert-once — precisely where the code
    is about to say "already done" and skip a balance change. If the terms
    disagree, "already done" is a statement about somebody else's transfer, so
    this raises and every plane's value stays where it is.

    `destination` is checked on the source side, `source` on the destination
    side; each plane compares the counterparty it is not.

    WHY THE SOURCE LABEL IS PART OF A CLAIM'S IDENTITY, despite being free text
    the wire supplies: two INDEPENDENT source planes — the two AWS regions run
    separate databases — can pick the same operator-chosen id ("topup-2026-08")
    for the same workspace and the same amount. Every other field matches, so
    nothing else can tell a genuine retry from a collision, and the loser is
    handed "accepted" for a credit that never happened after it has already
    debited itself. This is duplicate detection, NOT authorization: the label
    is never consulted to decide whether a caller may credit anything, only
    whether this is the same move as the one already recorded. A caller that
    lies about it can strand or destroy its OWN value and cannot mint.

    KNOWN GAP: two planes that both leave `api_base_url` unset send an empty
    label and become indistinguishable again. Compared only when both sides are
    non-empty, because refusing over a MISSING label would strand recoverable
    value on every record written before the field existed — so a plane that
    pushes credits should set it.
    """
    if destination is not None:
        require_matching_destination(existing, destination)
    if source is not None:
        held_source = str(_field(existing, "source") or "")
        asked_source = str(source or "")
        if held_source and asked_source and held_source != asked_source:
            raise TransferIdReused(
                f"transfer {transfer_id!r} was already claimed from "
                f"{held_source!r}, not {asked_source!r}"
            )
    held_workspace = str(_field(existing, "workspace_id") or "")
    asked_workspace = str(workspace_id or "")
    if held_workspace != asked_workspace:
        raise TransferIdReused(
            f"transfer {transfer_id!r} already names workspace {held_workspace!r}, "
            f"not {asked_workspace!r}"
        )
    held_amount = _field(existing, "amount_microdollars")
    if held_amount is not None and int(held_amount) != int(amount_microdollars):
        raise TransferIdReused(
            f"transfer {transfer_id!r} already names {int(held_amount)} "
            f"microdollars, not {int(amount_microdollars)}"
        )
