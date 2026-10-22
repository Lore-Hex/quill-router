"""Discrete-event claim contention model; milliseconds, no live I/O.

A snapshot chooses at its start. Each overlapping read-write claimant adds one
25 ms commit to the pending winner. Later reads see the committed claim. Claim
reads and snapshot reads cost 5 ms, observation takes 30 ms, publication 25 ms.
All starts are at B. This isolates contention, not host/RPC tail behavior.
"""
import heapq
from dataclasses import dataclass, field

from trusted_router.services.async_settle_shadow_health import follower_stagger


@dataclass
class FakeStore:
    commit_ms: int = 25
    read_ms: int = 5
    winner: str | None = None
    committed_at: int = 0
    transactions: int = 0
    snapshots: int = 0
    contenders: list[str] = field(default_factory=list)

    def snapshot(self, at):
        self.snapshots += 1
        return self.winner is not None and at >= self.committed_at

    def claim(self, boot, at):
        self.transactions += 1
        if self.winner is not None and at >= self.committed_at:
            return
        self.contenders.append(boot)
        if self.winner is None:
            self.winner = boot
            self.committed_at = at + self.read_ms + self.commit_ms
        else:
            self.committed_at += self.commit_ms

    def cycle(self, boots, slot, incumbent, after, cold_spread=False):
        queue = []
        for boot in boots:
            follower = after and boot != incumbent
            at = round(follower_stagger(boot, slot, not cold_spread or incumbent is not None)*1000) if follower else 0
            heapq.heappush(queue, (at, boot, 'snapshot' if follower else 'claim'))
        while queue:
            at, boot, stage = heapq.heappop(queue)
            if stage == 'snapshot':
                if not self.snapshot(at):
                    heapq.heappush(queue, (at+self.read_ms, boot, 'claim'))
            else:
                self.claim(boot, at)
        observed = self.committed_at + 30
        return self.winner if observed <= 250 and observed+25 <= 500 else None, observed


def simulate(n, after=True, warm=False, slots=1000, cold_spread=True):
    boots = [f'boot-{i}' for i in range(n)]
    incumbent = boots[0] if warm else None
    published = transactions = snapshots = 0
    first = None
    maximum = 0
    for slot in range(1000, 1000+slots):
        store = FakeStore()
        incumbent, observed = store.cycle(boots, slot, incumbent, after, cold_spread)
        transactions += store.transactions
        snapshots += store.snapshots
        if incumbent is not None:
            published += 1
            if first is None:
                first, first_ack = slot-1000, observed+25
            maximum = max(maximum, observed)
    return dict(published=published, slots=slots, rate=published/slots,
                first_published_slot_offset=first,
                first_incumbent_ms=None if first is None else first*2000+first_ack, max_published_observation_ms=maximum,
                claim_transactions=transactions, claim_snapshots=snapshots)

