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
not work, because the specs' guards and shared state are the lease's: an
auditor reap needs every drain row applied, a commit is the lease's, a gap
and the fence's boundary compare sequence numbers across authorizations,
and checkpoints are the lease's. So `tracecheck` replays each lease whole,
in one instance of each spec, with the spec's constants set from the run
(§2), through a mapping from the runtime's events to the spec's steps (§4).

The specs also abstract the runtime: `TerminalOrder` has one heartbeat per
authorization and no checkpoints, `AuditorCommit` one fence tick and
whole-commit acknowledgements, `LeaseLifecycle` holds as units, time in
ticks and renewals as single steps. Larger state alone does not bridge
that. Where the specs' safety arguments rest on what they leave out, the
specs grow first, through TLC (§4.1); the rest the mapping bridges, spec by
spec, saying for each runtime behavior a spec leaves out which step it is,
why that is sound, or what checks it instead (§4.2).

## 2. Machines

The specs take their sizes as constants: the authorizations, the members,
and bounds on the environment's steps (duplicates, late stores, raises,
reassignments, crashes, restarts) that keep TLC's search finite. A run sets
each: its authorizations, its members, and for each bound the count the run
used. A constant the run's real values replace is set so that its guard
holds: `LeaseLifecycle`'s `LeaseSize`, a count of unit holds, is unbounded,
and the holds' amounts are held to the allocation directly (§7). The spec
with these constants is the one a run is checked against.

Each shadow's package gets a machine: the same actions, under the same
labels, and the same invariants, over a state that grows with the run
(lists, maps keyed by authorization or member, 64-bit numbers), changed in
place, with an index for each guard that would otherwise scan (an
authorization's first heartbeat, first terminal and first drain row), so a
step costs time in proportion to what it changes. Each invariant names the
parts of the state it reads, and a step rechecks the invariants over the
parts it wrote.

A machine is tested against its shadow on every configuration the shadow's
tests run, by exploring the machine's own reachable states, indexes and
all, never rebuilt from a shadow's: from the initial state, each machine
state reached maps to a shadow state, and the machine's steps from it, under
the same labels, reach states that map to the shadow's successors, one for
one. After every step of every test, the machine checks its representation:
each index equals what a scan of the state it indexes finds, and each
incremental invariant check equals the full one. The shadow's graph is
TLC's, so the machine is the spec at those sizes. At a run's sizes it is the
same code over more authorizations, which no test explores exhaustively;
that it is still the spec there is assumed, as TLC's checks at small sizes
assume of the spec itself, and random runs at larger sizes keep checking its
representation.

## 3. Events and their order

`tracecheck` reads every process's events (the `trace` package), the load
generator's generations, and, after the run, the store's rows. Three kinds
of event go into the order:

- **Local events** are a process's own: what it decided, sent or received,
  each with its wall and monotonic clock readings. An event that takes
  effect under a lock, such as the owner's lease lock or a member's lease
  state, is recorded under that lock, so a process's sequence orders what
  the lock orders.
- **Database points** are where Spanner serialized an operation: a commit at
  its commit timestamp, a read at its read timestamp, a statement at the
  statement time its result shows. Each sits between the local events of its
  call's request and response, and carries no clock reading.
- **Added events** are the environment's steps that no process records:
  Pub/Sub storing a message, a key's delivery moving to another member, a
  time boundary passing. `tracecheck` builds them from the whole trace
  before it replays any of it, each with the edges its evidence gives (§4).
  A step some process does see, such as a renewal's answer lost to a
  timeout, is recorded where that process sees it.

The order's edges are the evidence the spike plan's §6 names:

- a process's sequence orders its local events;
- a cause precedes what it caused, and a call's request precedes its
  database point, which precedes its response;
- database points are ordered by their timestamps, the drain log's ties
  broken by record ID; a read follows every commit at or before its
  timestamp and precedes every later one, and what it returned is checked
  against the replay's state there (§4);
- a publish's acknowledgement precedes a publish to the same key that began
  after it (P1), and the key's messages are stored in the order every
  subscriber was given them;
- a process the fault injector kills records nothing after the kill, which
  precedes its successor's first event;
- clock readings order two events when the time check (§5) puts one wholly
  before the other.

These make a partial order. A cycle in them is an assumption broken, such as
two subscribers given one key's messages in different orders, and is
reported as that. The run happened in one of the order's linear extensions,
and `tracecheck` cannot tell which.

## 4. The specs and the runtime

### 4.1 Where the specs grow first

Some runtime behavior the specs abstract is behavior their safety arguments
rest on, so no mapping may wave it away: per-message acknowledgements and
redelivery decide what a member applies twice, ticks before and after the
fence decide where S falls, a refund is a terminal of its own. Before any
machine, each spec grows to have it, through TLC at the configurations it
checks, its guard table's state counts updated, and its shadow and the
shadow's comparisons with it (step S6c-0, one pull request a spec):

- **`TerminalOrder`**
  - Refunds. An enclave that gave up with nothing delivered may refund. The
    owner's refund and a front door's append of one are terminals of their
    own, enabled for such an enclave, while a settle still needs something
    delivered (A4).
  - Reaps. `AuditorReap(a)` needs no drain row of a's, as `store.Reap` checks
    in its transaction, not every row applied: the auditor reaps several
    overdue holds before it books the rows they made.
- **`AuditorCommit`**
  - Hand-offs. A chunk is a record that advances progress and lists holds,
    each a stored open hold from then on (a hold the auditor had not seen
    with its end of life); the manifest completes the list.
  - Acknowledgements. A member acknowledges each record it committed, in
    order, so `acked` moves to any prefix of what is done, not only to all
    of it.
  - Redelivery. Pub/Sub may give the member that holds a key its
    unacknowledged messages again, from the first, with no reassignment,
    crash or re-read.
  - Ticks. The ticker publishes ticks before the fence F and after it, each
    with its time, and every one is in the log. A member applies the first
    at or past F, which stores S, and skips the others.
  - Gaps and reaps. A member decides a gap or a reap in memory and then
    writes it, conditionally: the write can be refused and change nothing,
    and only a write that lands changes the lease's row.
- **`LeaseLifecycle`**
  - Renewals. A renewal's statement reads Spanner's time and computes the
    expiry, which its commit makes visible later; a renewal that timed out at
    its owner may still commit, under the same epoch; and an answer that
    leaves the expiry as it was still tells the owner what it is.
  - Tickers. Several tickers may read an expired lease before one marks it;
    each read is its own, and a refused mark ends only its own.
  - Views. A process refreshes its view of the workspace whether or not a
    pause came.

Each change is checked by TLC like the specs are now, and the mappings
below are written against the specs as they will be.

### 4.2 Mappings

Each shadow's package has a mapping from the runtime's events to its steps.
An event maps to a sequence of steps, at its place in the order, or to none
when the spec keeps no state it changes; the steps carry the facts the event
recorded, and the replay checks them against the machine's state: a
record's number, authorization and kind, a winner, a version, the rows a
read returned. Some steps the mapping places itself, within the interval the
evidence allows; each such choice below says why its outcome is the same
anywhere in that interval. The mapping is tested on the runtime's own traces
(§9), where a failure is the runtime's or the mapping's, and is found out
either way.

**`TerminalOrder`**

- **The owner's records.** The runtime's numbering is checked first, as it
  is: the owner's records are numbered from 1 with no number missing or
  used twice, and every position a commit or a tick names is one of them.
  Then a hold's first heartbeat record is `OwnerHeartbeat`; the owner's
  settle `OwnerSettle`, its refund the refund step, its reap `OwnerReap`,
  its release `OwnerRelease`, an adopted drain row `OwnerAdopt`. Later
  heartbeats, checkpoints and a hand-off's records are no step: the spec's
  state about a heartbeat is whether one was issued, acknowledged or made
  durable, and the first decides all three. Every position the spec compares
  is the rank of a runtime number among the records the spec models, which
  keeps every comparison the spec makes once the numbering is whole.
- **Acknowledgements.** `Ack` at the owner's acknowledgement of each modeled
  record, in rank order; one of a record the spec does not model is no step.
- **The log.** `Deliver` of rank k is added: after k's publish and the
  `Deliver` of rank k-1, before k's acknowledgement and every delivery of k
  to a member, and before the fence tick's publish if the key's order puts
  k before it, after otherwise. Within that interval only these events read
  `Delivered`, so where it falls changes nothing else.
- **Ticks.** The fence tick, the first tick at or past F, the one a member
  applied to store S, is `PublishTick`; earlier and later ticks are no step,
  since `TerminalOrder`'s state changes at the fence alone.
- **Time boundaries.** Every record's issuance must have passed the owner's
  cutoff test on its recorded reading, and every acknowledgement of a record
  issued before the cutoff must precede the owner's publish deadline, for
  every solution of §5's constraints. `CutoffPass` is added after the
  owner's last issuance and at the latest when its clock would read its
  known expiry less S, and `DeadlinePass` at the publish deadline after it,
  both before the fence tick; a killed owner's lease reaches its tick all
  the same. A late issuance or acknowledgement fails its timed check, where
  it is, not wherever the boundary was placed.
- **The enclave.** `EnclaveDeliver` at a request's provider answer, for a
  request that does not stream; a stream's enclave learns its answer at
  `Ack`. An enclave that gives up maps to `EnclaveGiveUp` at its give-up,
  unless the owner acknowledged a heartbeat of its hold, when the spec's
  enclave has the answer and the give-up is no step. Whatever the mapping,
  the run's own deliveries are checked directly (A4): an enclave that gave
  up having delivered nothing sends no settle. A release's `AllowanceElapse`
  is added just before `OwnerRelease`, since only the release reads it, and
  for a declared boot its guard needs the enclave's real give-up before it:
  a release while the enclave was still trying is the spec's A3 broken.
- **The auditor's durable steps.** `TerminalOrder`'s auditor is the lease's
  durable progress, so its steps are the successful commits: a commit that
  moves the stored progress from p to p' is `AuditorApplyOwner` for each
  modeled record of rank above rank(p) through rank(p'), then `ApplyDrain`
  for each row it books, then `StoreS` if it stores S. What a member applied
  and lost to a crash or a refused commit is no step. `AuditorReap` at a
  reap row's insert, `Close` at the close's commit, `RebuildStoreS` with the
  boundary a rebuild stored, `MarkDraining` and `FrontDoorAppend` at their
  commits.

**`AuditorCommit`**

- **The owner's records.** Each record is the step of its kind: a
  heartbeat, a terminal (settle for a settle or a charging reap, refund for
  a refund or a release, an adoption as its row's kind), a checkpoint
  (`IssueCheckpoint` or `IssueWrongCheckpoint` as the money check finds it,
  §7), a hand-off's chunk or manifest. Numbers are the runtime's own, every
  record being modeled.
- **The log.** The spec's log is the key's messages, ticks among them, in
  the order Pub/Sub stored them, each published copy once. `tracecheck`
  builds it before replay from every member's deliveries, by message ID,
  each entry added after its publish and before its first delivery and its
  first acknowledgement. Deliveries that no single log explains, such as one
  member given a later message before an earlier one, break P4's
  assumption, and are reported as that.
- **Assignment and redelivery.** A member's deliveries say where each of its
  runs through the log starts: a run that starts after another member's
  last delivery is `Assign`; one that starts again at the first
  unacknowledged message for the same member is a redelivery, or the
  member's crash or re-read where it recorded one. Each is placed after the
  acknowledgements that bring `acked` to one less than where the run starts,
  and before the next one.
- **Members.** `Load` and `LoadWinners` at the member's reads; applying,
  skipping, a tick and a drain row at the member's own steps, in memory; a
  gap and a reap at their conditional writes, a refused one as its refusal;
  `Commit` at a successful commit, with the version it read and the one it
  wrote; `Reread` at the read after a refused commit; each acknowledgement
  at the member's acknowledgement of that record; `Crash(m)` at the kill;
  `Close` at the close's commit.
- **Other writers.** `Raise` at a raise's or a shortfall write's commit,
  and at an append's raise. `MarkDraining` at its commit. An append's row is
  `FrontDoorAppend` at the later of its commit and `MarkDraining`: the
  runtime appends to an open lease, and while the lease is open no step of
  the spec reads the drain log.

**`LeaseLifecycle`**

- **Renewals.** A renewal's statement at the time its result shows, the
  new expiry less `Window` or, for an expiry it left as it was, no later than
  the commit; its commit at its commit timestamp, where the new expiry
  becomes visible. A renewal that commits after its owner timed out commits
  all the same. `RenewAnswer` at the owner's receipt, whatever it says;
  `AnswerLost` at its timeout, or at its kill with an answer outstanding;
  `ReplayedRenew` for a renewal committed for a process the lease's epoch is
  no longer.
- **Holds.** `Admit` at the owner's admission, with the reading and the
  known expiry it decided on. `HoldEnds` at the hold's first terminal,
  wherever it lands; a hold whose life runs out first ends by `Tick`, and
  §5 checks that its request had stopped by then.
- **The owner's ending.** `OwnerStop`, `FinalCheckpoint`, `OwnerDrains` and
  `OwnerDrops` at the owner's events; `ForcedExitStart` with the holds whose
  chunks the auditor applied, and `ForcedExitManifest` at the manifest;
  `Restart` at a new process for the node.
- **Others.** `Revoke` at the revocation's commit; `Pause` at a workspace's
  pause, and a view's refresh at a process's read of it; each ticker's read
  and its conditional mark, a refused one as its refusal; `CloseOnTheList`
  or `CloseOnTime` at the close's commit, by the kind it records. `Tick` is
  time passing between steps (§5).
- Holds are the run's, keyed by authorization, each one unit as the spec
  has them; `HoldsFitAllocation` is checked on their amounts (§7).

## 5. Time

The specs' clocks are true time, which no process reads: `LeaseLifecycle`'s
predicates take true time and allow any reading within the skew allowance
S of it (its A1). So `tracecheck` gives each local event and database point
an unknown true time, and constrains them:

- a true time is no earlier than its predecessors' in the order;
- a local event's wall-clock reading is within S of its true time (A1), and
  the time between two events of one process is its monotonic clock's,
  within a drift allowance;
- a database point is at its timestamp, which Spanner places within the
  call; it has no reading of its own, so a reply recorded long after its
  read is no contradiction.

These are difference constraints. The time check solves them by shortest
paths over their graph. With no solution, a negative cycle names readings
that no true times satisfy, and the report names the broken assumption: the
clock, and the readings that show it, such as a renewal's request and
response around its statement's time (spike plan §5, K6).

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
- Every request stopped within its hold's life (A3). The load generator
  records each request's end, the gateway's own: a stream completed, cut
  short or given up, a request answered or failed. That end, not its last
  delivery or its terminal, must come before its admission plus the longest
  life, for every solution. A terminal message that arrives later, retried,
  is no breach: `HoldEnds` is then no step, the hold having ended by `Tick`.
  A request that went on past its life is A3 broken, and reported, whatever
  the replay finds.

A timed predicate that holds for every solution passes; one that holds for
none is a violation; one that holds for some is inconclusive, and the report
names the events whose times decide it.

When the clocks break A1, the replay goes on without the constraints of the
clocks the cycle names, so the run's steps are still checked. K6's negative
control is reported twice, as the spike plan requires: as a broken
assumption, from the offset the injector recorded and the renewals'
brackets, and as a violation, an admission at a true time after the auditor
marked the lease draining.

## 6. Orders the evidence leaves open

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
That it holds at a run's sizes is assumed, as the machine is (§2).

A dependent pair left unordered makes the run inconclusive, and the report
names the two events and the evidence that would order them. A step that
fails in the replayed extension is a violation if every unordered pair
among it and the events that some extension puts before it is independent,
the failed step's own pairs included: then it fails in every extension.
Otherwise it is inconclusive, with the failure and the pairs named.

No other extension is replayed. Replaying them all costs one state per
downset of the order, up to ∏(nᵢ+1) for chains of nᵢ events covering it, at
most (n/w+1)^w for w chains of n events in all, which hundreds of concurrent
streams put out of reach.

## 7. Money and faults

Money stays the specs' abstraction: in `AuditorCommit` a settle charges
`SettleCharge` and an append `DoorCharge`, and holds are units in
`LeaseLifecycle`. The run's amounts are checked directly, by CreditDebt's
rules as the store's walks check them, with the walks' ledger moved where
both use it: each commit's bookings, returns and raises against the
allocation, each fault by kind and amount, and the open holds' amounts
against the allocation.

The runtime records each fault with its kind:

- a **consumption** fault: a checkpoint's `consumed` differs from the
  member's sum of the owner's terminals before it;
- an **over-return**: a checkpoint's return past the allocation's room;
- a **coverage** fault: open holds past the allocation, at a checkpoint;
- a **usage** fault: a booking past the allocation, by its amount
  (`FaultUsage`).

The first three are audit faults, and a lease keeps only the first
(`audit_fault`); usage faults are summed. The spec's `Alert` is sticky and
set only by a wrong checkpoint. A checkpoint is `IssueWrongCheckpoint`
exactly when its `consumed` differs from the sum of the run's terminals with
lower sequence numbers, as the ledger finds, whatever the runtime raised.
So the relation checked is: the lease is audit-faulted exactly when the
spec's `Alert` is set or the ledger finds an over-return or a coverage
fault; its first audit fault is the first of these by sequence number, of
the kind the ledger says; and each usage fault is the ledger's, by amount.
A later consumption fault that the first stored fault suppressed is still a
wrong checkpoint to the spec, and its `Alert` agrees with the lease's
fault, which is already set.

## 8. Verdicts

- **Pass:** the time constraints have a solution; every step of the replayed
  extension is a step of its spec with its facts, every invariant holds
  after it, and every timed predicate holds for every solution; every pair
  the order leaves unordered is independent; every direct check holds.
- **Violation:** a step that is no step of its spec, a fact that differs, a
  direct check that fails, or a timed predicate that holds for no solution,
  which no dependent unordered pair could change (§6). An invariant that
  fails after steps that are all the spec's is a fault of the spec or its
  machine, and is reported as that.
- **Inconclusive:** a dependent pair left unordered, or a timed predicate
  that holds for some solutions only. The report names the events and the
  evidence that would decide them.
- **Assumption broken:** constraints with no solution, a cycle in the order,
  deliveries no single log explains, a release before the enclave gave up,
  or a request that went on past its life. The report names the assumption
  and its evidence, and the replay goes on without it, so a violation behind
  it is still found.

## 9. Tests

- **The machines,** explored from their own states against their shadows
  on every configuration the shadows' tests run, with their representation
  checked after every step (§2).
- **The independence tables,** built from the reachable states (§6) and
  checked against the tables in the code.
- **An exact oracle for small traces.** For a trace of a few dozen events on
  a configuration the shadows hold, the verdict can be had without
  `tracecheck`'s method: every linear extension of its order replayed
  through the shadow, each time check solved again by an independent method
  (all pairs' shortest paths), the verdict pass if every extension is a run
  and every timed predicate holds for every solution, violation if none is
  or one holds for none, inconclusive otherwise, with the first step that
  fails in each extension. `tracecheck` may be more careful than the
  oracle, never less: its pass must be the oracle's pass, its violation the
  oracle's violation at the step it names, and it may call inconclusive a
  trace the oracle decides, since its independence table is judged over every
  state a pair could meet, not the states this trace does. The tests count
  how often it does, so a table grown too careful shows.
- **Traces from the machines.** Each step of a machine emits the events the
  runtime records for it, with their facts, clock readings within S, and
  evidence, so a random run becomes a trace. Each is checked as it is, with
  evidence removed, and altered: a fact changed, two events swapped, an event
  dropped or repeated, a reading or a timestamp moved. The small ones are
  held to the oracle; for every one, an alteration that turns a verdict to
  a violation must name a step the oracle shows failing, or a cycle the
  oracle's solver finds, and one that turns it to inconclusive must name the
  pair or the timed predicate that leaves it open.
- **The runtime's own traces,** once the roles record events: the service's
  end-to-end tests and the scenarios of the spike plan's §5, each checked,
  K6's negative control reported as both a broken assumption and a
  violation. These are where the mappings of §4 meet the runtime.

## 10. Steps

- **S6c-0.** Each spec grows as §4.1 says, one pull request a spec: the
  spec, TLC at its configurations, its guard table, its shadow and the
  shadow's comparisons.
- **S6c.** The events and their order, the time check, and the oracle for
  small traces; `TerminalOrder`'s machine, independence table and mapping,
  and their tests.
- **S6d.** `AuditorCommit`'s machine, table and mapping, with the log and
  assignments built from the deliveries, and the money check, with the
  store walks' ledger moved where both use it.
- **S6e.** `LeaseLifecycle`'s machine and mapping, with its timed facts and
  the renewals' statement times.
- **S6f.** The roles record their events, with the facts each mapping reads,
  under the locks that order them, their calls' requests and responses
  apart from the database points; the service's tests and the scenarios'
  traces are checked.
