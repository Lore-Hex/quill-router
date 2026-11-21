"""A process-local queue for one workspace's credit-row transactions.

Every synchronous authorize, settle and refund of a workspace with one credit
shard writes the same ``tr_credit_balance`` row and, for a capped key, the
same ``tr_key_limit`` row. A burst from one workspace puts dozens of those
transactions inside Spanner at once. The reserves queue for the row (they
scan it under the exclusive lock), but the settle and refund releases scan
under a shared lock and upgrade, so they abort each other and are aborted by
older reserves, and every abort redoes its statements on the same row
(docs/incidents/2026-11-10-reserve-lock-upgrade-aborts.md, "Not repaired
here"). The row then serves a fraction of what it can.

This gate lets ``width`` of a workspace's credit-row transactions from this
process be inside Spanner at a time. The rest wait here, in arrival order,
for a bounded time, and then start exactly as they would have without it.
The settle-outbox worker already takes turns per workspace this way
(services/settle_outbox_worker._WorkspaceGate); this is the same idea for
the request path.

What it changes and what it does not:

* It changes only WHEN a transaction starts. The transaction's function,
  arguments, statements, tag, retries and outcome handling are the caller's,
  untouched. A request that gets no slot (bound reached, waiters full, gate
  off) runs the same code at once.
* The wait is spent inside the caller's existing Spanner deadline, never in
  addition to it, and is capped at ``BUDGET_SHARE`` of what remains, so no
  request can take longer than it could before, and a transaction always
  keeps at least three quarters of the budget it arrived with. A caller
  with no such deadline (the settle-outbox drain) is not gated: its wait
  would be extra time, and its applies already take turns.
* It is a harm limiter, not a mutex. Correctness still comes from the
  conditional updates. Other processes hold their own gates, so the row sees
  up to ``width`` transactions per process instead of every in-flight one.

Bounds: a workspace has at most ``max_waiters`` callers parked here and the
process at most ``MAX_TOTAL_WAITERS`` (of an instance's 16 request slots), so
one workspace cannot park every worker. Entries exist only while a slot is
held or awaited. A slot held past ``ABANDONED_HOLD_SECONDS`` (longer than any
billing transaction's budget) is treated as leaked and stops counting.

Locks: a caller holding a slot here only runs its transaction, and takes no
other in-process lock while it waits or holds. A strict-budget key's
authorize is not gated: it already takes turns on its own per-key slot
(strict_budget.strict_budget_slot), and waiting here while holding that slot
would turn the key's next authorize into a strict-budget-busy 503. So the
two are never nested. The gate's own mutex guards bookkeeping and is never
held while waiting.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Entries exist only while held or awaited, so this bounds a pathological
# fan-out, not ordinary traffic. Past it a caller proceeds ungated.
MAX_SUBJECTS = 4096
# Callers parked at the gate across all workspaces. An instance serves 16
# requests at a time (TR_CLOUD_RUN_CONCURRENCY), so at least a quarter of
# them are never waiting here.
MAX_TOTAL_WAITERS = 12
# The longest billing transaction budget is 20 seconds and the route around
# it ends within 25. A slot held far longer belongs to a thread that will
# not release it in useful time.
ABANDONED_HOLD_SECONDS = 60.0
# The share of the caller's remaining Spanner budget a wait may use. A caller
# that waits its bound and then goes around starts its transaction with less
# time than it would have had; a quarter keeps that loss small (the snapshot
# settle's two seconds keep one and a half).
BUDGET_SHARE = 0.25

FREE = "free"
WAITED = "waited"
TIMEOUT = "timeout"
WAITERS_FULL = "waiters_full"
SUBJECTS_FULL = "subjects_full"
REENTRANT = "reentrant"
NO_BUDGET = "no_budget"
OFF = "off"


@dataclass
class _Waiter:
    event: threading.Event = field(default_factory=threading.Event)
    token: int | None = None


@dataclass
class _Subject:
    # token -> monotonic time the slot was granted.
    holders: dict[int, float] = field(default_factory=dict)
    waiters: deque[_Waiter] = field(default_factory=deque)


@dataclass(frozen=True)
class GatePass:
    """How one caller got past the gate.

    ``held`` says whether it holds a slot (free, waited) or runs ungated
    (every other outcome). ``queued`` is the number of callers of the same
    subject that were waiting when it arrived.
    """

    outcome: str
    held: bool
    wait_seconds: float = 0.0
    queued: int = 0


class FifoKeyedGate:
    """``width`` concurrent holders per subject; the rest wait in arrival order."""

    def __init__(
        self,
        *,
        max_subjects: int = MAX_SUBJECTS,
        max_total_waiters: int = MAX_TOTAL_WAITERS,
        abandoned_hold_seconds: float = ABANDONED_HOLD_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_subjects <= 0 or max_total_waiters <= 0 or abandoned_hold_seconds <= 0:
            raise ValueError("gate bounds must be positive")
        self._max_subjects = max_subjects
        self._max_total_waiters = max_total_waiters
        self._abandoned_hold_seconds = abandoned_hold_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._subjects: dict[str, _Subject] = {}
        self._total_waiters = 0
        self._next_token = 0
        self.abandoned = 0

    # -- bookkeeping, all under self._lock ---------------------------------

    def _grant(self, state: _Subject) -> int:
        self._next_token += 1
        state.holders[self._next_token] = self._clock()
        return self._next_token

    def _drop_abandoned(self, state: _Subject) -> None:
        cutoff = self._clock() - self._abandoned_hold_seconds
        stale = [token for token, since in state.holders.items() if since <= cutoff]
        for token in stale:
            del state.holders[token]
            self.abandoned += 1

    def _sweep_abandoned(self) -> None:
        """Forget subjects whose only holders leaked; run when the map is full."""
        for subject, state in list(self._subjects.items()):
            if not state.waiters:
                self._drop_abandoned(state)
                self._forget_if_idle(subject, state)

    def _promote(self, state: _Subject, width: int) -> None:
        while state.waiters and len(state.holders) < width:
            waiter = state.waiters.popleft()
            self._total_waiters -= 1
            waiter.token = self._grant(state)
            waiter.event.set()

    def _forget_if_idle(self, subject: str, state: _Subject) -> None:
        if not state.holders and not state.waiters and self._subjects.get(subject) is state:
            del self._subjects[subject]

    # -- the two operations -------------------------------------------------

    def acquire(
        self, subject: str, *, width: int, max_waiters: int, wait_seconds: float,
    ) -> tuple[int | None, GatePass]:
        """A slot token and how it was obtained, or ``None`` to run ungated."""
        if not subject:
            raise ValueError("subject must not be empty")
        if width <= 0 or max_waiters <= 0:
            raise ValueError("width and max_waiters must be positive")
        with self._lock:
            state = self._subjects.get(subject)
            if state is None:
                if len(self._subjects) >= self._max_subjects:
                    self._sweep_abandoned()
                if len(self._subjects) >= self._max_subjects:
                    return None, GatePass(SUBJECTS_FULL, held=False)
                state = self._subjects[subject] = _Subject()
            else:
                abandoned_before = self.abandoned
                self._drop_abandoned(state)
                if self.abandoned != abandoned_before:
                    self._promote(state, width)
            queued = len(state.waiters)
            if not queued and len(state.holders) < width:
                return self._grant(state), GatePass(FREE, held=True)
            if (
                wait_seconds <= 0
                or queued >= max_waiters
                or self._total_waiters >= self._max_total_waiters
            ):
                self._forget_if_idle(subject, state)
                return None, GatePass(
                    WAITERS_FULL if wait_seconds > 0 else NO_BUDGET, held=False, queued=queued,
                )
            waiter = _Waiter()
            state.waiters.append(waiter)
            self._total_waiters += 1
        started = self._clock()
        settled = False
        try:
            waiter.event.wait(wait_seconds)
            with self._lock:
                settled = True
                waited = self._clock() - started
                if waiter.token is not None:
                    return waiter.token, GatePass(
                        WAITED, held=True, wait_seconds=waited, queued=queued,
                    )
                self._withdraw(subject, waiter)
                return None, GatePass(TIMEOUT, held=False, wait_seconds=waited, queued=queued)
        finally:
            if not settled:
                # The wait itself was interrupted. Leave no waiter behind and
                # pass on a slot that was granted in the meantime.
                with self._lock:
                    token = waiter.token
                    if token is None:
                        self._withdraw(subject, waiter)
                if token is not None:
                    self.release(subject, token, width=width)

    def _withdraw(self, subject: str, waiter: _Waiter) -> None:
        state = self._subjects.get(subject)
        if state is None:
            return
        try:
            state.waiters.remove(waiter)
        except ValueError:
            return
        self._total_waiters -= 1
        self._forget_if_idle(subject, state)

    def release(self, subject: str, token: int, *, width: int) -> None:
        """Give a slot back. A token already dropped as abandoned is ignored."""
        with self._lock:
            state = self._subjects.get(subject)
            if state is None:
                return
            state.holders.pop(token, None)
            self._promote(state, width)
            self._forget_if_idle(subject, state)

    # -- diagnostics and deterministic tests --------------------------------

    def holders(self, subject: str) -> int:
        with self._lock:
            state = self._subjects.get(subject)
            return len(state.holders) if state is not None else 0

    def waiting(self, subject: str) -> int:
        with self._lock:
            state = self._subjects.get(subject)
            return len(state.waiters) if state is not None else 0

    def total_waiting(self) -> int:
        with self._lock:
            return self._total_waiters

    def subjects(self) -> int:
        with self._lock:
            return len(self._subjects)


WORKSPACE_GATE = FifoKeyedGate()
_HELD = threading.local()
_UNGATED = GatePass(OFF, held=False)


@contextmanager
def ungated() -> Iterator[GatePass]:
    """The gate when it does not apply: the body runs at once."""
    yield _UNGATED


def wait_bound(wait_seconds: float, budget_seconds: float) -> float:
    """The longest this caller may wait: the configured bound, and never more
    than ``BUDGET_SHARE`` of the Spanner budget it has left."""
    return max(0.0, min(wait_seconds, budget_seconds * BUDGET_SHARE))


@contextmanager
def workspace_billing_gate(
    workspace_id: str,
    *,
    width: int,
    max_waiters: int,
    wait_seconds: float,
    budget_seconds: float,
    kind: str,
    gate: FifoKeyedGate | None = None,
) -> Iterator[GatePass]:
    """Hold one of the workspace's slots around the caller's transaction.

    Always yields, with or without a slot: a caller that could not get one
    runs ungated, as it would with the gate off. ``budget_seconds`` is what
    is left of the caller's Spanner deadline; the wait is spent inside it.
    ``kind`` names the code path (authorize, settle_one_commit, finalize,
    frozen_finalize) for the log line, which carries no workspace, key or
    request identifier.
    """
    active = WORKSPACE_GATE if gate is None else gate
    held: set[str] | None = getattr(_HELD, "subjects", None)
    if held is None:
        held = _HELD.subjects = set()
    if workspace_id in held:
        # This thread already holds the workspace's slot around an outer
        # transaction. Waiting for itself would only spend its bound.
        yield GatePass(REENTRANT, held=False)
        return
    token, passed = active.acquire(
        workspace_id, width=width, max_waiters=max_waiters,
        wait_seconds=wait_bound(wait_seconds, budget_seconds),
    )
    if token is not None:
        held.add(workspace_id)
    # Everything after the slot is taken is inside the try: a failure to
    # write the line below must give the slot back like any other exit.
    try:
        if passed.outcome != FREE:
            # One line per caller that waited or went around; the
            # uncontended path logs nothing.
            logger.info(
                "metric=workspace_billing_gate outcome=%s kind=%s wait_ms=%.1f queued=%d",
                passed.outcome, kind, passed.wait_seconds * 1000, passed.queued,
                extra={
                    "metric": "workspace_billing_gate", "outcome": passed.outcome,
                    "kind": kind, "value": passed.wait_seconds * 1000,
                    "queued": passed.queued,
                },
            )
        yield passed
    finally:
        if token is not None:
            held.discard(workspace_id)
            active.release(workspace_id, token, width=width)
