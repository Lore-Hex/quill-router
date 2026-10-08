# Checking the spike's traces

The fast-admission design replays recorded traces through the shadows
(fast-admission-and-batched-settlement.md §5.1), and the spike plan says how
traces are recorded (fast-admission-spike.md §6). This note says how
`tracecheck` maps a run onto the shadows, which are small models of one
lease, and when it calls a run a pass, a violation, or inconclusive.

## 1. The problem

Each shadow is a transition function over one lease with a few
authorizations: `TerminalOrder` holds at most three, `AuditorCommit` two and
four owner records, `LeaseLifecycle` four holds and a clock of a few ticks.
Their states are fixed-size values so that the tests can compare them with
TLC's state graphs. A lease in a run has thousands of authorizations, its
records are numbered into the thousands, its amounts are real, and its clocks
run for hours. So a run cannot be fed to a shadow as it is. It is cut into
instances the shadows can hold, and each instance is replayed. What makes
that sound is said per shadow in §3, and tested in §5.

## 2. One order per lease

`tracecheck` reads every process's events (the `trace` package), the load
generator's generations, and, after the run, the store's rows. It puts each
lease's events in one order from the evidence §6 of the spike plan names:

- a process's sequence numbers order its own events;
- a cause precedes what it caused: a request's event, the receiver's;
- Spanner's commit timestamps order its writes, the drain log's ties broken
  by record ID; other ties are left open;
- a publish's acknowledgement precedes another publish that began after it,
  and a subscriber's deliveries of one key are in their order.

These make a partial order, and the run happened in one of its linear
extensions, `tracecheck` cannot tell which. So it replays them all, sharing
what they share. A point of the replay is a downset of the order: the events
some extension has replayed so far, each with every event the evidence puts
before it. `tracecheck` reaches each downset from each of the downsets one
event smaller, and must reach the same shadow state every way, with the
invariants holding there. A downset reached in two different states is two
extensions the shadow judges apart, and the run is inconclusive at the events
that differ, which the report names. A lease's events are mostly ordered, so
its downsets are few: at most about (n/w)^w for n events and the widest set w
the evidence leaves unordered. Past a bound on them, `tracecheck` stops and
calls the run inconclusive too.

## 3. Instances

### TerminalOrder: one instance per lease and authorization

`TerminalOrder`'s actions on an authorization `a` read lease-wide flags (the
lease's state, the owner's cutoff and publish deadline, whether the owner is
up), `a`'s own records and rows, and positions: how many owner records were
issued at the cutoff, delivered, acknowledged, applied, before the tick, and
S, and how many drain rows were appended and applied. They only compare
positions, and add one to them; no action subtracts two positions or reads
another authorization's record.

So the instance for `a` keeps the lease-wide events and `a`'s own records and
rows, and renumbers each position as the count of `a`'s records or rows at
or before it. An event for another authorization's record that moves a
position (a delivery, an acknowledgement, an application) moves the
projected position only if the record is `a`'s, and is otherwise no step of
the instance. `TerminalOrder`'s invariants are each about one authorization,
or compare lease-wide positions (`CountsInOrder`, `TypeOK`'s bounds).
Projection keeps the order of positions, and two positions out of order have
a record between them, so the instance of that record's authorization shows
it. So the invariants hold for the lease if they hold for every instance.

The claim this rests on, that a sequence of steps is a run of the spec with
several authorizations exactly when each authorization's projection is a run
of the spec with one, is tested (§5) rather than taken on trust.

### AuditorCommit: one instance per lease and authorization, money apart

The members' protocol (loads, versions, commits, crashes, takeovers,
redelivery) is lease-wide, and every instance carries it. An authorization's
records, holds, snapshots, winner and reap go to its own instance, numbered
as for `TerminalOrder`.

What does not project is money: the allocation, what is booked, the owner's
running sum and the checkpoint audit, which compares the owner's sum over
every authorization with the member's. The shadow's eight-bit sums cannot
hold real amounts either. So these are checked on the lease's events
directly, not in an instance: what each commit books is the sum of its
winners' charges, the allocation moves only by grants, raises, shortfall
writes and returns, and a checkpoint is faulted exactly when the owner's sums
in it differ from the sums of the records before it. In each instance a
checkpoint is `IssueCheckpoint` or `IssueWrongCheckpoint` as the direct check
found it, so the instances check the audit's outcome and the direct check
its arithmetic.

### LeaseLifecycle: one instance per lease, time and holds abstracted

`LeaseLifecycle` counts time in ticks and holds in four slots. Its clocks'
predicates (`WithinCutoff`, `CanAdmit`, the auditor's tests) are the ones the
owner and the auditor call, so `tracecheck` evaluates them on each event's
recorded clocks, against the skew allowance, rather than mapping hours onto
ticks: an event is an action of the spec if its predicate held at its
recorded time within the skew, and a run whose clocks were off by more is
reported out of model, not passed (K6's negative control, spike plan §5).
Holds go one at a time, as `TerminalOrder`'s authorizations do, for the
invariants about one hold (`AdmittedOnlyUnderOpenLease`,
`NoOpenHoldOnClosedLease`); `HoldsFitAllocation` is a sum, checked directly.

## 4. Verdicts

- **Pass:** every instance replays, every invariant holds after every step,
  and every direct check holds.
- **Violation:** a step that is no action of its spec, or after which an
  invariant or a direct check fails. The report names the instance, the
  step, its events and the invariant.
- **Inconclusive:** events the evidence leaves unordered whose orders the
  shadow judges apart, or more unordered than the bound (§2). The report
  names them, and the evidence a run would need to order them.
- **Out of model:** an event the mapping cannot place, such as a clock beyond
  the skew allowance. The report names it.

## 5. Tests

- **The projection.** Random runs of `TerminalOrder` and `AuditorCommit`
  with two or three authorizations: each authorization's projection is a run
  of the one-authorization spec, and the invariants agree; and for a step
  sequence that is not a run, some projection is not one. The same for
  `LeaseLifecycle`'s holds.
- **Traces from the shadows.** Each action of each shadow emits the events
  the runtime records for it, with their facts and evidence, so a random run
  of a shadow becomes a trace. `tracecheck` must pass it.
- **Altered copies.** Each such trace altered one way at a time: two ordered
  events swapped, an event dropped or repeated, a terminal's charge changed,
  a commit timestamp moved, a cause removed. `tracecheck` must report each,
  as a violation or as inconclusive, never as a pass.
- **The runtime's own traces,** once the roles record events: the scenarios
  of the spike plan's §5, each checked, K6's negative control reported.

## 6. Steps

- **S6c.** The order, the replay of its downsets, `TerminalOrder`'s
  projection and instance, and their tests.
- **S6d.** `AuditorCommit`'s instances and the money checks.
- **S6e.** `LeaseLifecycle`'s instances and the clock predicates.
- **S6f.** The roles record their events, with the facts each mapping reads;
  the scenarios' traces are checked.
