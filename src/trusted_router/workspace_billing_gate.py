"""A process-local queue for one workspace's credit-row transactions.

Every synchronous authorize, settle and refund of a workspace with one credit
shard writes the same ``tr_credit_balance`` row and, for a capped key, the
same ``tr_key_limit`` row. A burst from one workspace puts dozens of those
transactions inside Spanner at once. The reserves, and since 2026-11-21 the
settle and refund releases too, scan the row under the exclusive lock and
queue for it (docs/incidents/2026-11-10-reserve-lock-upgrade-aborts.md, "The
releases, November 21"). Before that the releases took a shared lock and
upgraded, so they aborted each other and were aborted by older reserves, and
every abort redid its statements on the same row. Queued or not, dozens of
them then wait inside Spanner behind one another, each spending its own
deadline.

This gate lets ``width`` of a workspace's credit-row transactions from this
process be inside Spanner at a time. The rest wait here for a bounded time
and then start ungated, the same transaction with what is left of its
deadline. The settle-outbox worker already takes turns per workspace this way
(services/settle_outbox_worker._WorkspaceGate); this is the same idea for
the request path.

Order: a waiting settle, refund or finalize goes before a waiting reserve,
and within each group the order is arrival order. A settle completes a
request that already holds credit and has the short budget (the snapshot
settle has Settings.async_settle_sync_budget_seconds, two seconds by default
and five deployed, a one-commit settle five); a reserve is new work and has
twenty. One row cannot serve a burst's reserves and settles at once, so
the reserves are the ones that wait. A workspace's reserves cannot be
starved for long: each settle follows one of its own reserves, and a reserve
that reaches its bound starts ungated.

What it changes and what it does not:

* It changes WHEN a transaction starts, and therefore how much of its
  deadline is left when it does. The transaction's function, arguments,
  statements, tag, retries and outcome handling are the caller's, untouched,
  so the money cannot depend on the gate. A request that gets no slot (bound
  reached, waiters full, gate off) runs the same code at once.
* The wait is spent inside the caller's existing Spanner deadline, never in
  addition to it, so no request can take longer than it could before. That
  is also its cost: a caller that waited starts with less time, and a
  transaction that needed more than what is left fails on its deadline
  where, ungated, it would have finished. The loss is bounded twice. One
  wait is at most ``BUDGET_SHARE`` of the budget left: five seconds of a
  reserve's twenty, a second and a quarter of a one-commit settle's five or
  of a five-second snapshot settle, half a second of a two-second one. And
  all of one request's waits
  together are at most the configured bound (five seconds), however many of
  its transactions meet the gate (a one-commit attempt and then an inline
  finalize; a reserve re-entered after a replay or a repair).
* A caller with no such deadline (the settle-outbox drain) is not gated: its
  wait would be extra time, and its applies already take turns.
* It applies only where it is known to help: a transaction that writes the
  credit row (not a BYOK-only reserve or the settle of one), of a workspace
  this process knows to have exactly one credit shard. A workspace whose
  shard count the process has not loaded is not gated: gating on a guess
  would make a sharded workspace's settles on different rows take turns and
  spend each other's budget for nothing, while not gating leaves today's
  behaviour. A process learns the count on the workspace's next reserve.
* It is a harm limiter, not a mutex. Correctness still comes from the
  conditional updates. Other processes hold their own gates, and callers
  that reached a bound or are not gated run beside the holder, so ``width``
  bounds the pile-up on the row; it is not a hard limit on it.

Bounds: a workspace has at most ``max_waiters`` callers parked here and the
process at most ``MAX_TOTAL_WAITERS``; one more starts ungated at once.
These bound the queue, not the instance: a waiting caller holds the request
slot it would otherwise have held while waiting inside Spanner, and nothing
here reserves slots for other workspaces (the per-key in-flight limits in
front of the routes do that). Entries exist only while a slot is held or
awaited. A slot held past ``ABANDONED_HOLD_SECONDS`` (three times the
longest billing budget) stops counting, so a leaked slot heals; a
transaction that really runs that long loses its turn's exclusivity, which
only weakens the limit.

Locks: the gate is taken before the transaction and released after it, and
is never requested while holding another lock. The transaction it surrounds
takes short locks of its own (the outbox probe's cache, the shard-count
cache, the session pool), always after the gate and never across a wait for
it, so there is no cycle. A strict-budget key's authorize is not gated: it
already takes turns on its own per-key slot
(strict_budget.strict_budget_slot), and waiting here while holding that slot
would turn the key's next authorize into a strict-budget-busy 503. So the
two are never nested. The gate's own mutex guards bookkeeping and is never
held while waiting.

Telemetry: one INFO line for each caller that waited or went around (about
two per request pair during a burst, none otherwise), written best-effort:
a failure to write it never stops the transaction.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
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
# time than it would have had; a quarter keeps that loss small (a snapshot
# settle's two seconds keep one and a half, its five three and three quarters).
BUDGET_SHARE = 0.25

FREE = "free"
WAITED = "waited"
TIMEOUT = "timeout"
WAITERS_FULL = "waiters_full"
SUBJECTS_FULL = "subjects_full"
REENTRANT = "reentrant"
NO_BUDGET = "no_budget"
ALLOWANCE_SPENT = "allowance_spent"
OFF = "off"
# A request's waits are counted together for this long after its first one.
# No billing request lives longer (the gateway stops waiting at 25 seconds),
# so a context that outlives it (a worker thread, a test) starts afresh.
REQUEST_WINDOW_SECONDS = 30.0


@dataclass
class _Waiter:
    event: threading.Event = field(default_factory=threading.Event)
    token: int | None = None
    # Served before the waiters that are not first (see ``acquire``).
    first: bool = False


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
    subject that were ahead of it in the queue when it arrived (for one that
    did not join the queue, the number waiting).
    """

    outcome: str
    held: bool
    wait_seconds: float = 0.0
    queued: int = 0


class FifoKeyedGate:
    """``width`` concurrent holders per subject; the rest wait in arrival order,
    callers marked ``first`` ahead of the others."""

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
        first: bool = False,
    ) -> tuple[int | None, GatePass]:
        """A slot token and how it was obtained, or ``None`` to run ungated.

        A ``first`` caller waits ahead of the callers that are not, behind the
        other ``first`` ones; within each group the order is arrival order.
        """
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
            waiter = _Waiter(first=first)
            if first:
                # The first group is always the head of the queue.
                queued = sum(1 for other in state.waiters if other.first)
                state.waiters.insert(queued, waiter)
            else:
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


@dataclass
class _RequestWaits:
    """What one request has already spent waiting at the gate."""

    since: float
    waited: float = 0.0


# Request-scoped by construction: every route runs its storage work in its
# own copy of the context (a threadpool call or the request's task), so a
# value set here is seen by that request's later transactions and by no
# other request. REQUEST_WINDOW_SECONDS covers a context that is reused.
_REQUEST_WAITS: ContextVar[_RequestWaits | None] = ContextVar(
    "trusted_router_workspace_gate_waits", default=None,
)


def _request_waits() -> _RequestWaits:
    now = time.monotonic()
    waits = _REQUEST_WAITS.get()
    if waits is None or now - waits.since >= REQUEST_WINDOW_SECONDS:
        waits = _RequestWaits(since=now)
        _REQUEST_WAITS.set(waits)
    return waits


def forget_request_waits() -> None:
    """Start a new request's allowance in this context.

    Routes never need it (each request has its own context). A loop that
    plays many requests on one thread, as a model or a test does, calls it
    between them.
    """
    _REQUEST_WAITS.set(None)


def _write_line(passed: GatePass, kind: str) -> None:
    """One line per caller that waited or went around. Best-effort: the
    transaction must run whether or not the line could be written."""
    try:
        logger.info(
            "metric=workspace_billing_gate outcome=%s kind=%s wait_ms=%.1f queued=%d",
            passed.outcome, kind, passed.wait_seconds * 1000, passed.queued,
            extra={
                "metric": "workspace_billing_gate", "outcome": passed.outcome,
                "kind": kind, "value": passed.wait_seconds * 1000,
                "queued": passed.queued,
            },
        )
    except Exception:  # noqa: BLE001, S110 - telemetry never stops billing
        pass


@contextmanager
def workspace_billing_gate(
    workspace_id: str,
    *,
    width: int,
    max_waiters: int,
    wait_seconds: float,
    budget_seconds: float,
    kind: str,
    first: bool = False,
    gate: FifoKeyedGate | None = None,
) -> Iterator[GatePass]:
    """Hold one of the workspace's slots around the caller's transaction.

    Always yields, with or without a slot: a caller that could not get one
    runs ungated, as it would with the gate off. ``budget_seconds`` is what
    is left of the caller's Spanner deadline; the wait is spent inside it,
    at most ``BUDGET_SHARE`` of it, and at most what is left of the
    request's allowance: all of one request's waits together never exceed
    ``wait_seconds``. ``first`` puts the caller ahead of the waiters that
    are not (the store passes it for every kind but a reserve). ``kind``
    names the code path (authorize, settle_one_commit, finalize,
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
    waits = _request_waits()
    allowance = max(0.0, wait_seconds - waits.waited)
    token, passed = active.acquire(
        workspace_id, width=width, max_waiters=max_waiters,
        wait_seconds=min(wait_bound(wait_seconds, budget_seconds), allowance), first=first,
    )
    waits.waited += passed.wait_seconds
    if passed.outcome == NO_BUDGET and allowance <= 0 < budget_seconds:
        # The slot was taken and this request has used its waits already.
        passed = GatePass(ALLOWANCE_SPENT, held=False, queued=passed.queued)
    if token is not None:
        held.add(workspace_id)
    try:
        if passed.outcome != FREE:
            _write_line(passed, kind)
        yield passed
    finally:
        if token is not None:
            held.discard(workspace_id)
            active.release(workspace_id, token, width=width)
