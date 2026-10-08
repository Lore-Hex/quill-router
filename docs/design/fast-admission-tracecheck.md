# Checking the spike's traces

The fast-admission design replays recorded traces through the shadows
(fast-admission-and-batched-settlement.md §5.1), and the spike plan says how
traces are recorded (fast-admission-spike.md §6). This note says how
`tracecheck` replays a run against the specs, and when it calls a run a
pass, a violation, inconclusive, or outside an assumption.

## 1. The problem

Each shadow is a transition function over one lease, small enough that its
tests compare its state graph with TLC's: `TerminalOrder` holds at most
three authorizations, `AuditorCommit` two authorizations, four owner records
and two members, `LeaseLifecycle` four holds and a clock of a few ticks. A
lease in a run has thousands of authorizations and records, real amounts,
and clocks that run for hours.

Cutting a run into per-authorization instances the shadows can hold does
not work, because the specs' guards and shared state are the lease's:

- an auditor reap needs every drain row applied (`DrainApplied = DrainLen`),
  which an authorization's projection does not see;
- a commit is the lease's, and is possible only with something applied;
- a gap and the fence's boundary compare sequence numbers across all
  authorizations, so renumbering per authorization changes them;
- checkpoints are the lease's, so they appear in every instance, and five of
  them already pass `AuditorCommit`'s four records.

So a step illegal for the lease can be legal in every projection, and a
legal one illegal in some. Instead, `tracecheck` replays each lease whole,
in one instance of each spec, with the spec's constants set from the run
(§2). Money and time stay as the specs abstract them, and the run's real
amounts and clock readings are held to those abstractions by rules of their
own (§4, §6).

## 2. Machines

The specs take their sizes as constants: the authorizations, the most
records, the members, and bounds on the environment's steps (duplicates,
late stores, raises, reassignments, crashes, restarts) that keep TLC's
search finite. A run sets each: its authorizations, its records, and for
each bound the count the run used. A constant the run's real values replace
is set so that its guard holds: `LeaseLifecycle`'s `LeaseSize`, a count of
unit holds, is unbounded, and the holds' amounts are held to the allocation
directly (§6). The spec with these constants is the one a run is checked
against.

Each shadow's package gets a machine: the same actions, under the same
labels, and the same invariants, over a state that grows with the run
(lists, maps keyed by authorization or member, 64-bit numbers), changed in
place, with an index for each guard that would otherwise scan (an
authorization's first heartbeat, first terminal and first drain row), so a
step costs time in proportion to what it changes. Each invariant names the
parts of the state it reads, and a step rechecks the invariants over the
parts it wrote.

A machine is tested equal to its shadow on every configuration the shadow's
tests run: from each reachable state of the shadow, the machine started
there takes the same steps, under the same labels, to the same states, and
its invariant checks agree with the shadow's, state by state. The shadow's
graph is TLC's, so the machine is the spec at those sizes. At a run's sizes
it is the same code over more authorizations, which no test explores
exhaustively; that it is still the spec there is assumed, as TLC's checks at
small sizes assume of the spec itself. A test of random runs at larger sizes
compares its incremental invariant checks with full ones.

## 3. The order

`tracecheck` reads every process's events (the `trace` package), the load
generator's generations, and, after the run, the store's rows. The spike
plan's §6 names the evidence that orders them:

- A process's sequence numbers order its own events. An event that takes
  effect under a lock, such as the owner's lease lock or a member's lease
  state, is recorded under that lock, so the sequence orders what the lock
  orders.
- A cause precedes what it caused.
- Spanner orders its writes by commit timestamp, the drain log's ties broken
  by record ID. A read at timestamp t follows every write committed at or
  before t and precedes every later one; what it returned is checked against
  the replay's state there (§6).
- A publish's acknowledgement precedes a publish to the same key that began
  after it, and a subscriber's deliveries of a key are in their order.
- A process the fault injector kills records nothing after the kill, which
  precedes its successor's first event.
- Clock readings order two events when the time check (§4) puts one wholly
  before the other.

Each step of a spec is anchored to an event in this order:

- most at the recorded event that took effect: an owner record issued, a
  commit, an append;
- some at another recorded event within the interval where the step could
  have happened, when the spec makes one step of what the run does in two,
  and the mapping says which (§6). `TerminalOrder`'s `Ack` both answers the
  owner and lets the enclave stream. An enclave that gave up while the
  answer was on its way gives up, in the replay, at its heartbeat's send,
  which precedes the `Ack`: its state from then until its give-up is one the
  spec's `Gone` enclave also has, since it neither streams nor settles;
- the environment's steps that no process sees are events the replay adds,
  each with the edges its evidence gives. `TerminalOrder`'s `Deliver` of
  record k follows k's publish and the `Deliver` of k-1, and precedes k's
  acknowledgement and every delivery of k to a member; it precedes the
  tick's publish if the key's order puts k before the tick, and follows it
  otherwise. A step some process does see, such as a renewal's answer lost
  to a timeout, is recorded where that process sees it.

These make a partial order. A cycle in them is an assumption broken, such as
a record acknowledged before the tick's publish began that the key's order
puts after the tick (P1), and is reported as that. The run happened in one of its linear
extensions, and `tracecheck` cannot tell which.

## 4. Time

The specs' clocks are true time, which no process reads: `LeaseLifecycle`'s
predicates take true time and allow any reading within the skew allowance
S of it (its A1). So `tracecheck` gives each event an unknown true time, and
constrains them:

- an event's true time is no earlier than its predecessors' in the order;
- a process's wall-clock reading is within S of its event's true time (A1),
  and the elapsed time between two of its events is its monotonic clock's,
  within a drift allowance;
- a Spanner commit or read is at its timestamp, a time within the
  transaction that Spanner guarantees.

These are difference constraints. The time check solves them by shortest
paths over their graph. With no solution, a negative cycle names readings
that no true times satisfy, and the report names the broken assumption: the
clock, and the readings that show it, such as a renewal's bracket around its
commit timestamp (spike plan §5, K6).

A spec's time predicate is then checked on the recorded reading and through
the constraints, never by putting a reading where the spec has true time:

- The runtime's own test must have held on the reading its event recorded:
  an owner admitted with its reading before the expiry it knew, less S; an
  auditor marked a lease draining with its reading past the expiry plus S.
- The spec's guard at true time, and each timed fact the spec keeps (an
  admission within a revocation's window or a pause's cache age, a hold
  whose life has not run out, a process past its cutoff when its lease
  drains), must hold for every solution. A bound on one event is decided by
  its earliest and latest true times; a bound between two events, by the
  shortest path between them.

A timed predicate that holds for every solution passes; one that holds for
none is a violation; one that holds for some is inconclusive, and the report
names the events whose times decide it.

When the clocks break A1, the replay goes on without the constraints of the
clocks the cycle names, so the run's steps are still checked. K6's negative
control is reported twice, as the spike plan requires: as a broken
assumption, from the offset the injector recorded and the renewals'
brackets, and as a violation, an admission at a true time after the auditor
marked the lease draining.

## 5. Orders the evidence leaves open

Two steps are independent when neither makes the other possible or
impossible, and, where both are possible, both orders end in the same state,
each step recording the same facts. If every pair the order leaves
unordered is independent, every linear extension is a run of the spec
exactly when one is, since any two are linked by swapping independent
neighbors. So `tracecheck` replays one extension, earliest true time first
(ties by node, epoch and sequence), and checks every unordered pair against
a table of each shadow's independent steps. Each event's unordered events
lie, in each process's sequence, in one interval, so the pairs are found
without comparing every two.

The table is by action and by how the actions' parameters relate: the same
authorization or another, the same member or another. A test builds it from
every reachable state of every configuration the shadow's tests run, and
fails if the table in the code claims a pair is independent that is not.
That it holds at a run's sizes is assumed, as the machine is (§2); a test of
random runs at larger sizes checks the pairs it claims. Steps on different
authorizations are mostly independent; what makes most others dependent is
a lease-wide position, which the evidence orders.

A dependent pair left unordered makes the run inconclusive, and the report
names the two events and the evidence that would order them. A step that
fails in the replayed extension is a violation if every unordered pair
among the events that some extension puts before it is independent: then
it fails in every extension. Otherwise it is inconclusive, with the failure
and the pairs named.

No other extension is replayed. Replaying them all costs one state per
downset of the order, up to ∏(nᵢ+1) for chains of nᵢ events covering it,
at most (n/w+1)^w for w chains of n events in all, which hundreds of
concurrent streams put out of reach.

## 6. Mappings

Each shadow's package has a mapping from events to its steps. A step
carries the facts its event recorded, and the replay checks them against the
machine's state: a record's sequence number, authorization and kind, a
winner, a version, the rows a read returned.

**`TerminalOrder`**

- The owner's records are `OwnerHeartbeat`, `OwnerSettle`, `OwnerReap`,
  `OwnerRelease` and `OwnerAdopt`, at the sequence numbers the run gave
  them; `Ack` at the owner's acknowledgement; `CutoffPass` and
  `DeadlinePass` where the owner records them; `OwnerCrash` at the kill.
- `Deliver` as §3 says. `PublishTick` at the tick's publish, where the
  `Deliver` steps' edges make the machine's `Delivered` the number of owner
  records the key's order puts before the tick.
- `AllowanceElapse(a)` is added just before `OwnerRelease(a)`, the end of
  its interval: only the release reads it. For a declared boot its guard is
  that the enclave gave up or a heartbeat reached the owner (the spec's A3);
  a release where it fails is reported as that assumption broken.
- `EnclaveGiveUp(a)` at the enclave's last send before it gave up, or, for a
  request that does not stream, at the provider's failure;
  `EnclaveDeliver(a)` at the provider's answer.
- `FrontDoorAppend`, `MarkDraining`, `StoreS`, `AuditorReap` and `Close` at
  their commits; `AuditorApplyOwner` and `ApplyDrain` at the member's
  application; `RebuildStoreS` with the boundary the rebuild stored.

**`AuditorCommit`**

- `IssueHeartbeat`, `IssueTerminal` and the checkpoints at the owner's
  records; a checkpoint is `IssueCheckpoint` or `IssueWrongCheckpoint` as
  the money check below finds it, and the configuration's `Lying` is set if
  any is wrong.
- The log is what the members were given. A member's next delivery is the
  log's record at the member's position; if the log is not that long yet,
  `Store`, `StoreAhead`, `StoreLate` or `StoreAgain` is added before it, as
  the machine's outstanding records and the tick say. A delivery that is
  none of these breaks the spec's model of Pub/Sub, P4's question, and is
  reported as that.
- `Assign(m)` is added before the first of member m's steps that needs it
  to hold the key. `Load`, `LoadWinners`, `Commit`, `Reread`, `Reap` and
  `Close` at their reads and commits, with versions checked; `Ack(m)` at the
  member's acknowledgements; `Crash(m)` at the kill; `Raise` at a raise's or
  a shortfall write's commit; `FenceTick` at the tick's publish.
- Money stays the spec's: a settle charges `SettleCharge`, an append
  `DoorCharge`. The run's amounts are checked directly, by CreditDebt's
  rules as the store's walks check them: each commit's bookings, returns
  and raises against the allocation, and each fault by amount. The runtime
  records each fault with its kind:
  - a consumption fault: a checkpoint's `consumed` differs from the member's
    sum of the owner's terminals before it. A checkpoint is
    `IssueWrongCheckpoint` exactly when its `consumed` differs from the sum
    of the run's terminals with lower sequence numbers, so the machine's
    `Alert` and the runtime's consumption faults must agree;
  - a return past the allocation's room, and holds past the allocation
    (store/money.go), which the spec does not model: each is checked against
    the ledger, and is no `Alert`.

**`LeaseLifecycle`**

- Renewals, revocations, pauses, views refreshed, admissions, terminals,
  stops, checkpoints, hand-offs and restarts, and the auditor's reads, marks
  and closes, at their events; a renewal's answer at the owner's receipt,
  and its loss at the owner's timeout.
- `Tick` is the passing of true time between steps (§4). A hold ends at its
  terminal, or when its life runs out; a terminal after that is no step
  here, since the spec's hold ended with its life.
- Holds are the run's holds, keyed by authorization, each one unit as the
  spec has them, and `HoldsFitAllocation` is checked on their amounts.

## 7. Verdicts

- **Pass:** the time constraints have a solution; every step of the replayed
  extension is a step of its spec with its facts, every invariant holds
  after it, and every timed predicate holds for every solution; every pair
  the order leaves unordered is independent; every direct check holds.
- **Violation:** a step that is no step of its spec, a fact that differs, a
  direct check that fails, or a timed predicate that holds for no solution,
  which no dependent unordered pair could change (§5). An invariant that
  fails after steps that are all the spec's is a fault of the spec or its
  machine, and is reported as that.
- **Inconclusive:** a dependent pair left unordered, or a timed predicate
  that holds for some solutions only. The report names the events and the
  evidence that would decide them.
- **Assumption broken:** constraints with no solution, a delivery the spec's
  Pub/Sub does not allow, or a release before the enclave gave up. The
  report names the assumption and its evidence, and the replay goes on
  without it, so a violation behind it is still found.

## 8. Tests

- **The machines,** each equal to its shadow on every configuration the
  shadow's tests run, state by state (§2), and its incremental invariant
  checks equal to full ones on random runs at larger sizes.
- **The independence tables,** built from the reachable states (§5) and
  checked against the tables in the code.
- **Traces from the machines.** Each step of a machine emits the events the
  runtime records for it, with their facts, clocks within S, and evidence,
  so a random run becomes a trace. With all its evidence it must pass. With
  evidence removed only between independent steps it must still pass;
  removed between two dependent ones, it must be inconclusive, naming them.
- **Altered traces whose verdict is known.** Each alteration comes with the
  check that must report it:
  - a fact changed (a sequence number, an authorization, a winner, a charge,
    a version): the fact check at that event;
  - two ordered steps swapped where the shadow, from the state before them,
    does not allow the swapped order: a violation at the second;
  - an event dropped that a later recorded fact depends on: that fact's
    check;
  - a reading moved beyond S of the others: a broken assumption naming that
    clock; a timestamp moved past a timed guard's bound: that guard.

  And alterations that must leave the verdict as it was: independent events
  the order leaves unordered, written in the other order; a reading or a
  timestamp moved within an interval in which no derived order and no timed
  predicate changes; a tick repeated where the spec allows it; a process's
  sequence numbers renumbered in order.
- **The runtime's own traces,** once the roles record events: the scenarios
  of the spike plan's §5, each checked, K6's negative control reported as
  both a broken assumption and a violation.

## 9. Steps

- **S6c.** The order and the time check; `TerminalOrder`'s machine,
  independence table and mapping, and their tests.
- **S6d.** `AuditorCommit`'s machine, table and mapping, and the money check,
  with the store walks' ledger moved where both use it.
- **S6e.** `LeaseLifecycle`'s machine and mapping, with its timed facts.
- **S6f.** The roles record their events, with the facts each mapping reads,
  under the locks that order them; the scenarios' traces are checked.
