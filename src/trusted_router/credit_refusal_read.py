"""The credit read a credit refusal names, for the fast-path edge's decide shadow.

The internal authorize route's 402 ``insufficient_credits`` says only that the
workspace's credit could not take the request's hold. The edge's decide shadow
(fastpath/internal/edge/funding.go) reads the workspace's credit rows itself
after such a refusal, and a hold the workspace's other requests release between
Python's read and the shadow's can make the shadow's read cover the hold
moments after Python found it short. So the route also names a read of the
credit rows its refusal stands on, in ``X-TR-Credit-Refusal-Read``: which read
it was, the hold it was held to, and the rows' count, signed sum and largest
row, plus the read timestamp when it was a snapshot, at which the shadow
re-reads the same rows.

The read is the last read of the workspace's credit rows Python made after its
last authorize transaction refused, or the one it refused on without a
transaction:

- ``remembered``: the lock-free snapshot that answers a workspace this process
  remembers as exhausted (credit_exhaustion_precheck), every row. It decides
  the refusal.
- ``heal``: the snapshot every refused transaction is followed by, which looks
  for a stale debt mark (_heal_stale_debt_mark), every row. For a one-row
  workspace this is not the read that decided: the transaction's conditional
  UPDATE decided at its own instant, and reads nothing, and this snapshot is
  later, after that decision and before the answer. It corroborates the
  refusal: the rows short at an instant after Python decided.
- ``headroom``: the snapshot that proves a workspace of several rows exhausted
  (credit_headroom_precheck), rows 0 to n-1 of the count Python holds. It
  decides the refusal.
- ``rebalance``: the locked read of rows 0 to n-1 inside the rebalance's
  read-write transaction (rebalance_credit_for_estimate), which has no read
  timestamp before its commit. It decides the refusal. It does not read the
  debt mark: selecting ``in_debt`` there would take shared locks on every
  row's mark inside that transaction, which a marking write that touches only
  the mark (storage_gcp_credit_debt.cover_or_mark) would then contend with.

A later credit read is not named: with fastpath_lease_headroom_503, the route
reads the rows and the leases once more after a final refusal
(_lease_verdict_after_refusal) to tell a balance held in leases (503) from one
exhausted (402). When that read, or the remembered one, finds live leases
holding the shortfall of a workspace whose headroom, theirs included, is
under the fast path nodes' floor, the answer stays the 402 and the verdict
carries the read's amounts (LeaseUnderFloor) for the route's log line; no
header carries them.

A snapshot read is named only with its read timestamp, which the client keeps
for a multi-use snapshot (storage_gcp_authorize.snapshot_read_timestamp_ns);
without it the 402 names no read at all, rather than one the shadow cannot
re-read.

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

# The reads a refusal can name, as the header names them: the snapshots, each
# named with its read timestamp, and the rebalance's locked read, which has
# none.
REMEMBERED = "remembered"
HEAL = "heal"
HEADROOM = "headroom"
REBALANCE = "rebalance"
SNAPSHOT_READS = frozenset({REMEMBERED, HEAL, HEADROOM})


@dataclass(frozen=True)
class CreditRefusalRead:
    """One read of a workspace's credit rows: which read, the hold it was held
    to, the rows' count, their signed sum (``available``: credit less usage
    less reservations, negative rows included) and the largest row, and the
    snapshot's read timestamp in Unix nanoseconds (None for a read inside a
    read-write transaction, or a snapshot whose timestamp the client did not
    keep)."""

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


@dataclass(frozen=True)
class LeaseUnderFloor:
    """The lease-aware read behind a 402 that live leases would otherwise have
    made the lease-headroom 503 (fastpath_lease_headroom_503_floor_micro): the
    rows' signed headroom outside leases (``available``), what the workspace's
    live leases hold (``lease_held``), both from one snapshot, and the floor
    their sum was under. Amounts only, for the route's log line."""

    available: int
    lease_held: int
    floor: int


def credit_refusal_read_headers(read: CreditRefusalRead | None) -> dict[str, str]:
    """``X-TR-Credit-Refusal-Read`` for the INTERNAL authorize route's 402
    insufficient_credits. One line of space-separated fields in a fixed
    order, ``read_at`` for a snapshot and never for the rebalance's locked
    read:

        read=remembered read_at=2026-11-18T07:42:14.123456789Z hold=82074 rows=32 sum=72803 most=9000
        read=rebalance hold=82074 rows=32 sum=72803 most=9000

    Empty for no read, a read of no name above, a snapshot without its read
    timestamp, or a locked read with one: the shadow then treats the refusal
    as it did before the header existed."""
    if read is None:
        return {}
    snapshot = read.read in SNAPSHOT_READS
    if not (snapshot or read.read == REBALANCE) or snapshot != (read.read_timestamp_ns is not None):
        return {}
    fields = [f"read={read.read}"]
    if read.read_timestamp_ns is not None:
        fields.append(f"read_at={read_timestamp_rfc3339(read.read_timestamp_ns)}")
    fields += [f"hold={read.hold}", f"rows={read.rows}", f"sum={read.available}", f"most={read.most}"]
    return {CREDIT_REFUSAL_READ_HEADER: " ".join(fields)}
