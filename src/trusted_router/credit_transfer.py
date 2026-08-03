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

    spendable(source plane) + escrowed(unresolved) + spendable(destination plane)
        == constant

Every microdollar is in exactly one of those three buckets at every instant,
and it changes bucket only via a transition guarded by a single INSERT-ONCE
row. No transition adds to one bucket without removing from another in the
same transaction, so no sequence of operations can create or destroy value.

THE STATES, AND WHICH PLANE HOLDS THE VALUE
-------------------------------------------
Source-side record (``credit_transfer``, one per transfer id):

``ESCROWED``
    The source plane has DEBITED the amount from the workspace's spendable
    balance in the same transaction that created this record. **The value is
    held by the SOURCE plane, in escrow.** It is spendable by NOBODY — not the
    source (already debited), not the destination (not yet credited). This is
    the only durable intermediate state, and it is deliberately durable: a
    crash here loses nothing, it just leaves the value parked and visible.

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
