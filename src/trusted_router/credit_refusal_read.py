"""The read a credit refusal stands on, for the fast-path edge's decide shadow.

The internal authorize route's 402 ``insufficient_credits`` says only that the
workspace's credit could not take the request's hold. The edge's decide shadow
(fastpath/internal/edge/funding.go) reads the workspace's credit rows itself
after such a refusal, and a hold the workspace's other requests release between
Python's read and the shadow's can make the shadow's read cover the hold
moments after Python found it short. So the route also names the read its
refusal stands on, in ``X-TR-Credit-Refusal-Read``: which read it was, the hold
it was held to, and the rows' count, signed sum and largest row, plus the read
timestamp when it was a snapshot, at which the shadow re-reads the same rows.

The read is the last read of the workspace's credit rows Python made after its
last authorize transaction refused, or the one it refused on without a
transaction:

- ``remembered``: the lock-free snapshot that answers a workspace this process
  remembers as exhausted (credit_exhaustion_precheck), every row;
- ``heal``: the snapshot every refused transaction is followed by, which looks
  for a stale debt mark (_heal_stale_debt_mark), every row: the read after a
  one-row workspace's refusal, which its conditional UPDATE made without a read;
- ``headroom``: the snapshot that proves a workspace of several rows exhausted
  (credit_headroom_precheck), rows 0 to n-1 of the count Python holds;
- ``rebalance``: the locked read of every row inside the rebalance's
  read-write transaction (rebalance_credit_for_estimate), which has no read
  timestamp before its commit.

Internal plumbing, on the internal route alone: the enclave keeps only a
control-plane error's status, body and ``Retry-After`` (quill-cloud-proxy
enclave-go/internal/trustedrouter/client.go), and no customer-facing route
sends it. It carries amounts and an instant, never an identifier.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from trusted_router.spend_windows import read_timestamp_rfc3339

CREDIT_REFUSAL_READ_HEADER = "X-TR-Credit-Refusal-Read"

# The reads a refusal can stand on, as the header names them.
REMEMBERED = "remembered"
HEAL = "heal"
HEADROOM = "headroom"
REBALANCE = "rebalance"


@dataclass(frozen=True)
class CreditRefusalRead:
    """One read of a workspace's credit rows: which read, the hold it was held
    to, the rows' count, their signed sum (``available``: credit less usage
    less reservations, negative rows included) and the largest row, and the
    snapshot's read timestamp in Unix nanoseconds (None for a read inside a
    read-write transaction)."""

    read: str
    hold: int
    rows: int
    available: int
    most: int
    read_timestamp_ns: int | None = None

    @classmethod
    def of(
        cls, read: str, hold: int, headroom: Sequence[int], read_timestamp_ns: int | None = None,
    ) -> CreditRefusalRead | None:
        """The read of these rows' headroom, or None for no rows."""
        if not headroom:
            return None
        return cls(read, int(hold), len(headroom), sum(headroom), max(headroom), read_timestamp_ns)


def credit_refusal_read_headers(read: CreditRefusalRead | None) -> dict[str, str]:
    """``X-TR-Credit-Refusal-Read`` for the INTERNAL authorize route's 402
    insufficient_credits, empty for no read. One line of space-separated
    fields in a fixed order, ``read_at`` only for a snapshot:

        read=remembered read_at=2026-11-18T07:42:14.123456789Z hold=82074 rows=32 sum=72803 most=9000
        read=rebalance hold=82074 rows=32 sum=72803 most=9000
    """
    if read is None:
        return {}
    fields = [f"read={read.read}"]
    if read.read_timestamp_ns is not None:
        fields.append(f"read_at={read_timestamp_rfc3339(read.read_timestamp_ns)}")
    fields += [f"hold={read.hold}", f"rows={read.rows}", f"sum={read.available}", f"most={read.most}"]
    return {CREDIT_REFUSAL_READ_HEADER: " ".join(fields)}
