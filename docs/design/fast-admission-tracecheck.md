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

Each shadow's package keeps a registry of its exhaustive configurations:
the `.cfg` instances TLC explores, each with the distinct states TLC
reports, on which the shadow's tests already compare its graph with TLC's.
The machine is tested against its shadow on each, by exploring the
machine's own reachable states, indexes and all, never rebuilt from a
shadow's: from the initial state, each machine state reached maps to a
shadow state, and the machine's steps from it, under the same labels, reach
states that map to the shadow's successors, one for one. After every step of
every test, the machine checks its representation: each index equals what a
scan of the state it indexes finds, and each incremental invariant check
equals the full one. So on the registry's configurations the machine is the
spec. The shadows' larger randomized tests run the machine beside the
shadow, step for step, its representation checked, and claim no equality.
At a run's sizes the machine is the same code over more authorizations;
that it is still the spec there is assumed, as TLC's checks at small sizes
assume of the spec itself.

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
  its commit timestamp, a read at its read timestamp. A commit's follows its
  call's request, and precedes its response when the call's outcome was
  confirmed. A read's precedes its response and follows every commit to the
  rows it reads that was acknowledged before its request, and may come before
  the request itself, and before a commit to other rows acknowledged before
  it: a strong read may be served at the timestamp of the last write to its
  rows. So a read's timestamp says which state it saw, not when it ran; the
  read's step, what its caller learns, is at the caller's receipt of its
  result, and what it read is held to the rows as of its timestamp. A point
  carries no clock reading. A call that timed out or failed may still have
  committed, and its caller has no timestamp for it. So, when a run is traced,
  every read-write transaction of the store also writes one row of an
  operations journal: the attempt's ID, which the caller chose and recorded
  with its request, and the commit timestamp. A row proves its attempt
  committed, at the row's timestamp, whatever its caller learned. Its absence
  proves the attempt did not commit only once nothing can still commit it: the
  journal is read after every process has exited and every attempt's deadline
  has passed by a sealing interval, an assumption the run states; an attempt
  that read cannot settle is pending, and the events that rest on it
  inconclusive. Each attempt is its own: a retried append that finds its drain
  row writes only its journal row, and the original raise and timestamp it
  returns are checked against the row, never taken as its own point. A
  statement's `CURRENT_TIMESTAMP` is no database point: Spanner does not take
  it from TrueTime, and it cannot be compared with a commit timestamp. It is a
  reading of Spanner's clock, a local event of the transaction's, between the
  call's request and its commit, and within S of true time like any other
  reading (A1, §5).
- **Added events** are the environment's steps that no process records:
  Pub/Sub storing a message, a key's delivery moving to another member, a
  time boundary passing. `tracecheck` builds them from the whole trace
  before it replays any of it, each with the edges its evidence gives (§4).
  A step some process does see, such as a renewal's answer lost to a
  timeout, is recorded where that process sees it.

The order's edges are the evidence the spike plan's §6 names:

- a process's sequence orders its local events;
- a cause precedes what it caused; a call's request precedes its response; a
  commit's request precedes its database point, which precedes its response
  when its caller learned the outcome; and a read's database point precedes
  its response and follows every commit to the rows it reads acknowledged
  before its request, but need not follow the request, nor a commit to other
  rows (§3);
- database points are ordered by their timestamps, the drain log's ties
  broken by record ID; a read follows every commit at or before its
  timestamp and precedes every later one, and what it returned is checked
  against the replay's state there (§4);
- a publish's acknowledgement precedes a publish to the same key that began
  after it (P1), and the key's messages are stored in the order each run of
  deliveries gives them (§4.2);
- a process the fault injector kills records nothing after the kill, which
  precedes its successor's first event;
- clock readings order two events when the time check (§5) puts one wholly
  before the other.

These make a partial order. A cycle in them is an assumption broken, such as
two subscribers given one key's messages in different orders, and is
reported as that. The run happened in one of the order's linear extensions,
and `tracecheck` cannot tell which.

## 4. The specs and the runtime

A spec step is a change of the state the spec keeps, placed where the
evidence shows it happened, and what a process learns of it is a step of its
own. So a call's request, its durable effect and its caller's learning of
the outcome are three events: the effect is a step at its database point,
or where the log stored a message, whether or not the caller ever learns of
it; the learning, an answer, a timeout, a member's state discarded, is the
caller's. And a process acts on its own view, as its last read or answer
left it, not on the database's state. Where a spec merges these, as a
member's version moving at its commit or an enclave's permission moving at a
publish's acknowledgement, it grows to separate them before any mapping is
written.

### 4.1 Where the specs grow first

Some runtime behavior the specs abstract is behavior their safety arguments
rest on, so no mapping may wave it away. Before any machine, each spec grows
to have it, through TLC at its registry's configurations, its guard table's
state counts updated, and its shadow and the shadow's comparisons with it
(step S6c-0, one pull request a spec):

- **`TerminalOrder`**
  - Refunds. An enclave that gave up with nothing delivered may refund. The
    owner's refund and a front door's append of one are terminals of their
    own, enabled for such an enclave, while a settle still needs something
    delivered (A4).
  - Permission, delivery and give-up. They are three things. The owner answers
    a heartbeat accepted only after its record is acknowledged, before its
    cutoff and, for a heartbeat that echoes a deadline, by it (a stream's
    first heartbeat echoes none, and a retry of one is answered by what the
    heartbeat it repeats echoed), and may answer retry or deadline passed
    after the acknowledgement (`heartbeatAnswer`); the answer may also be lost
    on its way. So `Ack` is the record's acknowledgement alone; a new
    `Answer(a)`, after it, permits a's stream to deliver; `EnclaveDeliver(a)`
    is its first byte delivered, which needs that permission (A4); and an
    enclave that delivered nothing, permitted or not, may give up and refund.
  - Reaps. `AuditorReap(a)` needs no drain row of a's, as `store.Reap` checks
    in its transaction, not every row applied: the auditor reaps several
    overdue holds before it books the rows they made.
  - Listed holds. A hold the auditor learned only from a hand-off's manifest
    is durably known without a heartbeat (`Listed(a)`), once the commit
    after the manifest stores it: `AuditorReap(a)` is enabled for it as for a
    hold whose heartbeat is durable, at no charge for one that does not
    stream, as `store.Reap` allows.
- **`AuditorCommit`**
  - Receipt and processing. A member receives a message while it holds the
    key (`Receive`), and processes what it received later, whether or not it
    still holds the key: the runtime keeps a handler that began before a
    reassignment. A former holder's processing is guarded by its commit
    alone, which a newer version refuses.
  - Views. A member's guards read its own view of the lease: the status,
    version and winners its last read or commit left it. Another member's
    or the owner's writes change the row, not the view; the member learns
    them at its next read, at its commit's answer, or when its commit is
    refused. So a member may apply an owner record to a lease it still sees
    open after the lease was marked draining; the draining write leaves the
    commit version as it was, so its commit, under the version it read,
    lands, and its answer tells the member the lease is draining.
  - Commits, landed and learned. A commit's write (`CommitLands`) and its
    member's learning of the outcome are steps apart. A member that learns
    success moves to the version it wrote. One whose commit was refused, or
    whose outcome it never learned, discards what it holds in memory
    (`Discard`), reads the lease again and applies its kept records again,
    whether the write landed or not, skipping what a landed write made
    durable.
  - Reads, served and received. A member's read of the lease is two steps: it
    is served at its timestamp, taking the row's version, status and winners
    as they were there (`ReadServed`), and the member's receipt of it installs
    what was served in its view (`Load`, or `LoadWinners` for the winners),
    whatever writes have landed since. So a member can install a version
    another member's commit has passed, and its next commit, under that
    version, is refused.
  - Hand-offs. A chunk advances progress and stages its holds, stored with
    the lease's row from the next commit, so a member that takes the lease
    over has them. The manifest installs them only when every chunk it names
    is staged and their holds have its digest: each becomes a stored open
    hold unless the lease has a winner for it, and one the member holds a
    newer snapshot for keeps that snapshot. A manifest that does not match,
    or the fence tick with a hand-off incomplete, drops the staged chunks.
    Installing reads the winners, which the member loads for a lease still
    open (`LoadWinners`, enabled at a manifest).
  - Acknowledgements. A member acknowledges a message once nothing it did is
    left to make durable: after the commit that made it durable, or at once
    for one that needed none, such as a copy of a record already committed,
    a tick that stores no S, or a record of a lease that is done. Pub/Sub
    may lose an acknowledgement and may deliver an acknowledged message
    again. So a run of deliveries of the key, to any member, starts at a
    message no later than the first that no member acknowledged, and goes on
    in the log's order, skipping none, acknowledged or not, until its member
    stops receiving the key; and `acked` is what the members sent, not a
    frontier Pub/Sub keeps. The spec's claim is that every acknowledged
    message's effect is durable or needed none, which no redelivery can
    break.
  - Drain-log refunds. A front door appends a refund as well as a settle:
    `FrontDoorAppend` takes the row's kind, a refund charging nothing
    through application, commit and the winner checks.
  - Ticks. The ticker publishes ticks throughout, each with its reading. The
    fence tick is the first in the key's order whose reading is at or past
    F plus S, the member's test, F being the fence the lease's row stores:
    its expiry plus S plus the publish deadline. A tick before it, at or
    past F or not, stores nothing, and an owner record before it can still
    move S. Each tick's reading is its own; its place is where the log
    stored it.
  - Draining. A member applies drain rows, reaps and closes once S is stored
    and its records through the fence tick are applied, whatever the log
    holds after them: a later record or tick may be stored and not yet
    delivered.
  - Gaps and reaps. A member decides a gap or a reap in memory and then
    writes it, conditionally: the write can be refused and change nothing,
    and only a write that lands changes the lease's row.
- **`LeaseLifecycle`**
  - Renewals. A renewal's statement computes the expiry from
    `CURRENT_TIMESTAMP`, a reading of Spanner's clock within S of true time
    (A1 for Spanner's clock), not true time, and its commit makes the expiry
    visible later; a renewal that timed out at its owner may still commit,
    under the same epoch; an answer that leaves the expiry as it was still
    tells the owner what it is; an answer that comes after its owner let
    the lease go is discarded (`AnswerDiscarded`); and an answer that
    refuses the renewal, the lease revoked or no longer the owner's,
    carries no expiry, and the owner drops the lease on it (`OwnerDrops`),
    or, the answer lost, keeps it to its known cutoff.
  - Grants. A lease begins with a grant's statement, which reads Spanner's
    clock as a renewal's does, its commit, and the owner's receipt of its
    answer, three steps, not an initial state at true time zero: an answer
    lost leaves the owner without the lease, and a retry of the grant finds
    it and returns its expiry, as it was, with no second grant.
  - Revocation's bound. A renewal's statement before a revocation may read
    up to S ahead, and the expiry it stores, which the owner's cutoff takes
    as it is, may stand up to S later than true time would have set it: an
    owner's last admission comes before the revocation plus `Window` plus
    S, not plus `Window`. `RevocationBoundsAdmission` and its shadow's check
    say `Window` plus S, and a grant's expiry, read the same way, is checked
    the same way.
  - Decisions and their landing. The owner's decision of a hold's terminal
    takes the hold out of the owner's own holds at once (`OwnerDecides`);
    the terminal lands in the auditor's books later, or never (`HoldEnds`).
  - The owner's last records. Its final checkpoint, and a forced exit's
    chunks and manifest, are each published, acknowledged to the owner, and
    committed by the auditor, three steps. The final checkpoint needs only
    the owner's own holds gone; the manifest lists the holds the owner
    published, not those it holds when the auditor commits it; and the
    auditor's commit may come after the owner died, restarted or abandoned
    the lease. The owner's draining write follows the acknowledgement, not
    the auditor's commit; it lands at its journal row, and its answer is
    learned later, or never. `CutoffBeforeDrain` becomes the rule the owner
    keeps: it admits nothing once the lease is marked draining, by its own
    write or the auditor's.
  - Abandonment. An owner whose publishes have failed for longer than the
    window stops renewing, and lets the lease go once past its cutoff,
    though the stored lease is still open and its own (`OwnerAbandons`). The
    lease then expires and drains by time.
  - Tickers. Several tickers may read an expired lease before one marks it;
    each read is its own, served at its timestamp, which takes the expiry
    there, and received by its ticker later, as a member's is, a renewal
    perhaps landing between, when the ticker's mark, conditional on the expiry
    it read, is refused; a refused mark ends only its own.
  - Views. A process refreshes its view of the workspace whether or not a
    pause came, its read served at its timestamp and received later, as a
    ticker's is. The view it installs is as old as its read: its age runs from
    the read's request, by the process's monotonic clock, not from its
    receipt, so a result that comes late installs a view already aged by its
    wait, as the ring's watcher counts a read's latency in its view's age
    (`ring/watch.go`). `RefreshView` grows so, installing the elapsed age, not
    zero.
  - Pauses. A workspace's pause clears and may come again, as a debt mark
    is set and repaid: `Unpause` clears it and a later `Pause` starts a new
    cache window, and an admission is held to the pause its view could have
    missed, the one in force within its view's age.

Each change is checked by TLC like the specs are now, and the mappings
below are written against the specs as they will be. A gap found later, on
the runtime's own traces (§9), is handled the same way: the spec grows
through TLC, and no mapping papers over it.

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
  used twice, and a position a commit or a tick names, a progress or an S,
  is a prefix of them, counted from 0, which is none: the S of an empty
  lease, or of one whose owner died before its first record. Then a hold's
  first heartbeat record is `OwnerHeartbeat`; the owner's settle
  `OwnerSettle`, its refund the refund step, its reap `OwnerReap`, its
  release `OwnerRelease`, an adopted drain row `OwnerAdopt`. Later
  heartbeats, checkpoints and a hand-off's records are no step: the spec's
  state about a heartbeat is whether one was issued, acknowledged or made
  durable, and the first decides all three. Every position the spec compares
  is the rank of a runtime position among the records the spec models, 0
  ranking 0, which keeps every comparison the spec makes once the numbering
  is whole.
- **Acknowledgements and answers.** `Ack` at the publish result of each
  modeled record, in rank order: when the settle log's client had Pub/Sub's
  acknowledgement, which it records with its reading. The owner's
  bookkeeping of it, later, is its learning and no step, and a publish
  deadline is checked on the result's reading, so a flusher that ran late
  breaks nothing. An acknowledgement of a record the spec does not model is
  no step. `Answer(a)` at the gateway's receipt of an accepted
  answer to a's first heartbeat, which the load generator records; a retry,
  a deadline passed or an answer lost is no step.
- **The log.** `Deliver` of rank k is added: after k's publish and the
  `Deliver` of rank k-1, before k's acknowledgement and every delivery of k
  to a member, and before the fence tick's publish if the key's order puts
  k before it, after otherwise. Within that interval only these events read
  `Delivered`, so where it falls changes nothing else.
- **Ticks.** The fence tick, the first in the key's order whose reading is
  at or past F plus S (§4.1), the one a member applied to store S, is
  `PublishTick`, at its place in the log; earlier and later ticks are no
  step, since `TerminalOrder`'s state changes at the fence alone. A reading
  at or past F plus S puts the tick's true time at or past F (A1).
- **Time boundaries.** Every record's issuance must have passed the owner's
  cutoff test on its recorded reading, and every acknowledgement of a record
  issued before the cutoff must precede the owner's publish deadline, for
  every solution of §5's constraints. `CutoffPass` is the cutoff of the
  expiry the lease drained at: added after the owner's last issuance and at
  the latest when its clock would read that expiry less S, and
  `DeadlinePass` at the publish deadline after it, both before the fence
  tick; a killed owner's lease reaches its tick all the same. A cutoff the
  owner passed and recovered from, by a renewal that came late, is no step:
  the owner issued nothing while past it, which each issuance's own test
  shows, on its reading and the expiry it knew then; and after it, until
  its drain log was adopted, it admitted nothing, issued no fresh
  heartbeat, decided no terminal but an adoption and answered no
  heartbeat accepted, which is checked directly, while checkpoints,
  adoptions and the republish of records issued before the cutoff go on.
  A late issuance or acknowledgement fails its timed check, where it is,
  not wherever the boundary was placed.
- **The enclave.** `EnclaveDeliver` at a request's first byte delivered,
  which the load generator records: a request that does not stream at its
  provider's answer, a stream once `Answer` permits it. An enclave that
  gives up having delivered nothing maps to `EnclaveGiveUp` at its give-up,
  permitted or not; one that delivered something settles, and its give-up
  is no step. Whatever the mapping, A4 is checked directly on the run's
  deliveries (§5): a stream delivers only while its gateway holds an
  accepted answer whose deadline its clock has not passed, and a request
  that delivered nothing sends no settle. A release's
  `AllowanceElapse` is added just before `OwnerRelease`, since only the
  release reads it, and for a declared boot its guard needs the enclave's
  real give-up before it: a release while the enclave was still trying is
  the spec's A3 broken.
- **The auditor's durable steps.** `TerminalOrder`'s auditor is the lease's
  durable progress, so its steps are the commits that land, by their
  journal rows, whether their members learned so or not: a commit that
  moves the stored progress from p to p' is `AuditorApplyOwner` for each
  modeled record of rank above rank(p) through rank(p'), then `ApplyDrain`
  for each row it books, then `StoreS` if it stores S, then `Listed(a)` for
  each hold with no durable heartbeat whose manifest it stores. A row that
  loses to a committed winner books nothing and is decided by that winner,
  as the close's checks find, from the later of its member's read of it and
  that winner's commit. `ApplyDrain` takes the rows in order, so the
  decided rows are a prefix: each row's `ApplyDrain` is where the prefix of
  decided rows grows past it, at the latest of its own decision and those
  of the rows before it. What a member applied and lost to a crash or a
  refused commit is no step. `AuditorReap` at a
  reap row's insert, `Close` at the close's commit, `RebuildStoreS` with the
  boundary a rebuild stored, `MarkDraining` and `FrontDoorAppend` at their
  commits.

**`AuditorCommit`**

- **The owner's records.** Each record is the step of its kind: a
  heartbeat, a terminal (settle for a settle or a charging reap, refund for
  a refund or a release, an adoption as its row's kind), a checkpoint
  (`IssueCheckpoint` or `IssueWrongCheckpoint` as the money check finds it,
  §7), a hand-off's chunk or manifest. Numbers are the runtime's own, every
  record being modeled. A charging reap is a settle to the spec, so what
  makes it a reap is checked directly: it names its hold's last heartbeat
  record before it, by that record's owner sequence number, and charges
  that heartbeat's running charge; a reap prepared before a newer heartbeat
  and issued after it fails this.
- **The log.** The spec's log is the key's messages, ticks among them, in
  the order Pub/Sub stored them, each published copy once. `tracecheck`
  builds it before replay from every publisher's acknowledged publishes and
  every member's deliveries, by message ID: a message whose publish was
  acknowledged is in the log, delivered or not, placed by P1 after every
  publish to the key acknowledged before its own began; each entry is added
  after its publish and before its first delivery and its first
  acknowledgement. The key's deliveries then split into runs, each to
  one member, each starting at a message no later than the first that no
  member had acknowledged, and going on in the log's order, skipping no
  message, acknowledged or not, until its member stopped receiving the key:
  its stop, crash or reassignment, or the trace's end, which cut a run
  short and are no skip. Deliveries that no single log and such runs
  explain, a run that skips a message among them, break P4's assumption,
  and are reported as that; a redelivery of an acknowledged message is no
  break.
- **Assignment, receipt and redelivery.** Each delivery is its member's
  `Receive`, and its processing is the member's steps on what it received,
  later. A run that starts after another member's last delivery begins
  with `Assign`; one that starts again for the same member is a redelivery,
  or the member's crash where it recorded one. Each is placed before its
  run's first delivery and after that member's delivery before it.
- **Members.** `ReadServed` at a member's read's timestamp, with what the rows
  held there, and `Load` and `LoadWinners` at the member's receipt of that
  read's result, installing what was served (§3, §4.1); applying, skipping, a
  tick and a drain row at the member's own steps, in memory; a gap and a reap
  at their conditional writes, a refused one as its refusal; `CommitLands` at
  a commit's journal row, with the version it read and the one it wrote, and
  the member's learning at its answer; `Discard` where the runtime drops the
  member, after a refused commit or one whose answer it never had, and the
  read that follows as `Load`; each acknowledgement at the member's
  acknowledgement of that message; `Crash(m)` at the kill; `Close` at the
  close's commit.
- **Other writers.** `Raise` at a raise's or a shortfall write's commit,
  and at an append's raise. `MarkDraining` at its commit. An append's row is
  `FrontDoorAppend`, of its kind, at the later of its commit and
  `MarkDraining`: the runtime appends to an open lease, and while the lease
  is open no step of the spec reads the drain log.

**`LeaseLifecycle`**

- **Renewals.** A renewal's statement at its `CURRENT_TIMESTAMP` reading,
  the new expiry less `Window`, a reading of Spanner's clock placed, like a
  process's, between the call's request and the commit (§3, §5), or, for an
  expiry it left as it was, no later than the commit; its commit at its
  journal row's timestamp, where the new expiry becomes visible. A renewal
  that commits after its owner timed out commits all the same.
  `RenewAnswer` at the owner's receipt of an answer for a lease it holds,
  whatever it says, and `AnswerDiscarded` for one it let go; `AnswerLost`
  at its timeout, or at its kill with an answer outstanding;
  `ReplayedRenew` for a renewal committed for a process the lease's epoch
  is no longer.
- **Holds.** `Admit` at the owner's admission, with the reading and the
  known expiry it decided on. `OwnerDecides` at the owner's decision of the
  hold's terminal; `HoldEnds` at the commit that books its first terminal,
  the owner's or the drain log's; a hold whose life runs out first ends by
  `Tick`. Either way §5 checks that its request had stopped by then: the
  hold's end is money's, the request's is its gateway's.
- **The owner's ending.** `OwnerStop`, `FinalCheckpoint`, `OwnerDrains`,
  `OwnerDrops` and `OwnerAbandons` at the owner's events; the final
  checkpoint's completion at the auditor's commit of it, wherever the owner
  is by then; `ForcedExitStart` with the holds whose chunks the auditor
  applied, and `ForcedExitManifest` at the manifest; `Restart` at a new
  process for the node.
- **Others.** `Revoke` at the revocation's commit; `Pause` at a workspace's
  pause, and a view's refresh at a process's receipt of its read; each
  ticker's read served at its timestamp, the expiry the lease's row held
  there, and received at the ticker's receipt of the result, and its
  conditional mark, a refused one as its refusal; `CloseOnTheList` or
  `CloseOnTime` at the close's commit, by the kind it records. `Tick` is time
  passing between steps (§5).
- Holds are the run's, keyed by authorization, each one unit as the spec
  has them, open until their terminals are booked. That is not the set
  money is held to: `HoldsFitAllocation` is checked on the owner's books
  (§7).

## 5. Time

The specs' clocks are true time, which no process reads: `LeaseLifecycle`'s
predicates take true time and allow any reading within the skew allowance
S of it (its A1). So `tracecheck` gives each local event and database point
an unknown true time, and constrains them:

- a true time is no earlier than its predecessors' in the order;
- a local event's wall-clock reading is within S of its true time (A1), and
  the time between two events of one process is its monotonic clock's,
  within a drift allowance; a statement's `CURRENT_TIMESTAMP` is a reading
  of Spanner's clock, within S of its own true time, which lies between its
  call's request and its commit;
- a database point is at its timestamp. Spanner places a commit's after its
  call's request and, for a call whose outcome its caller learned, before its
  response, and a read's before its response and after every commit to the
  rows it reads acknowledged before its request, though perhaps before the
  request itself and before commits to other rows (§3). A point has no reading
  of its own, so a reply recorded long after its read is no contradiction, and
  a commit after its caller's timeout is none either.

These are difference constraints. The time check solves them by shortest
paths over their graph. With no solution, a negative cycle names readings
that no true times satisfy, and the report names the broken assumption: the
clock, and the readings that show it, such as a renewal's request and
response readings around its commit's timestamp (spike plan §5, K6).

A spec's time predicate is then checked on the recorded reading and through
the constraints, never by putting a reading where the spec has true time:

- The runtime's own test must have held on the reading its event recorded:
  an owner admitted with its reading before the expiry it knew, less S; an
  auditor marked a lease draining with its reading past the expiry plus S.
- The spec's guard at true time, and each timed fact the spec keeps (an
  admission within a revocation's window or a pause's cache age, a hold whose
  life has not run out, an admission before the auditor's mark of its lease
  draining, which the auditor made with its reading past the expiry plus S),
  must hold for every solution. A bound on one event is decided by its
  earliest and latest true times; a bound between two events, by the shortest
  path between them. A lease its owner marks draining needs no cutoff: the
  owner may drain its empty closing lease long before the expiry, its write's
  reply delayed or lost, and the rule §4.1 keeps in `CutoffBeforeDrain`'s
  place, that the owner admits nothing once the lease is marked draining, is
  checked on the owner's own events in their order, with no time: no admission
  after its draining write's request.
- Every request stopped in time (A3). The load generator records each
  request's end, the gateway's own: a stream completed, cut short or given
  up, a request answered or failed. That end, not its last delivery or its
  terminal, must come, for every solution, before its admission plus the
  longest life; for a stream, before the last deadline its gateway was
  granted, by its gateway's clock; and before the first terminal of its hold
  to land, whatever its kind: the owner's, an append, a reap at the deadline
  plus the grace, a release, a close's. So a hold removed at its terminal or
  by its life had no request running. A terminal message that arrives later,
  retried, is no breach: `HoldEnds` is then no step, the hold having ended
  by `Tick`. A request that went on past any of these is A3 broken, and
  reported, whatever the replay finds.
- Every accepted answer was the owner's to give. `TerminalOrder` models a
  stream's first heartbeat alone, so each accepted answer a gateway received,
  the first or a later one, is checked directly against the owner's decision
  to give it, which the owner records with the facts it decided on: its
  reading of the record's acknowledgement, after the publish result, the
  expiry it knew then and whether its drain log was adopted. The answer is
  accepted only if that reading was before the cutoff of that expiry and, if
  the heartbeat echoed a deadline, by it, with the drain log adopted, a
  stream's first heartbeat echoing none and a retry answered by what the
  heartbeat it repeats echoed, and it grants the deadline the owner's rule
  gives; the publish result itself is held to the publish deadline (A1).
- Every stream delivered only while permitted (A4). The load generator
  records each answer as its gateway received it and each delivery, with
  their readings. Each delivery must come, by the gateway's clock, before
  the deadline of the latest accepted answer the gateway had received by
  then: a stream that went on past its deadline while its next answer was
  late is A4 broken, though the answer, when it came, granted a later
  deadline.

A timed predicate that holds for every solution passes; one that holds for
none is a violation; one that holds for some is inconclusive, and the report
names the events whose times decide it.

When the clocks break A1, the replay goes on without the constraints of the
clocks the cycle names, so the run's steps are still checked. K6's negative
control is reported twice, as the spike plan requires: as a broken
assumption, from the offset the injector recorded and the renewals' commit
timestamps between their owners' request and response readings, and as a
violation, an admission at a true time after the auditor marked the lease
draining.

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
every reachable state of every configuration in the shadow's registry (§2),
and fails if the table in the code claims a pair is independent that is
not, or claims one that no reachable state witnesses: both actions
enabled, with their parameters so related. A relation no configuration
can show, two streams' heartbeats where every configuration has one
stream, stays dependent until a configuration in the registry shows it.
That it holds at a run's sizes is assumed, as the machine is (§2).

A dependent pair left unordered makes the run inconclusive, and the report
names the two events and the evidence that would order them. A step that
fails in the replayed extension is a violation if every unordered pair
among it and the events that some extension puts before it is independent,
the failed step's own pairs included: then it fails in every extension.
Otherwise it is inconclusive, with the failure and the pairs named.

No other extension is replayed. Replaying them all visits every downset of
the order, up to ∏(nᵢ+1) for chains of nᵢ events covering it, at most
(n/w+1)^w for w chains of n events in all, and a downset may hold several
states, since two orders of one downset can end apart; hundreds of
concurrent streams put that out of reach.

## 7. Money and faults

Money stays the specs' abstraction: in `AuditorCommit` a settle charges
`SettleCharge` and an append `DoorCharge`, and holds are units in
`LeaseLifecycle`. The run's amounts are checked directly, by CreditDebt's
rules as the store's walks check them, with the walks' ledger moved where both
use it: each commit's bookings, returns and raises against the allocation,
each fault by kind and amount, and the owner's books against the allocation:
the holds it holds, the terminals it decided whose publishes are not
acknowledged, and what it booked, as CreditDebt's `OpenLog` counts stored and
unbooked records. The lifecycle's holds, open until their terminals are
booked, are not that set: a hold refunded and acknowledged frees its room for
the next admission before any commit. Each reap's amount is checked too, each
by its own rule. The owner's names its hold's last heartbeat record issued
before it, by owner sequence number, whether or not that record is durable
yet, and charges that heartbeat's running charge (§4.2): the owner reaps at
the snapshot it last issued, and may die before that snapshot or the reap is
durable. The auditor's names its hold's durable snapshot as the reap's own
transaction reads the hold's row, whichever member committed it: a member that
took the lease over reaps at a snapshot another committed. It charges
`min(running charge, estimate)` of it, or nothing for a hold listed with no
snapshot, as `store.Reap` books.

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
  machine, and is reported as that. On the runtime's own traces a violation
  is first a question: the runtime's defect, or a gap in the spec, which
  grows through TLC (§4.1).
- **Inconclusive:** a dependent pair left unordered, or a timed predicate
  that holds for some solutions only. The report names the events and the
  evidence that would decide them.
- **Assumption broken:** constraints with no solution, a cycle in the order,
  deliveries no single log and runs through it explain (§4.2), a release
  before the enclave gave up, a request that went on past its time (§5), or
  one that delivered while no accepted answer's deadline was ahead of its
  gateway's clock or settled having delivered nothing (A4, §5). The report
  names the assumption and its evidence, and the replay goes on without it,
  so a violation behind it is still found.

## 9. Tests

- **The machines,** explored from their own states against their shadows
  on every configuration in the registries, and run beside them on the
  larger random ones, with their representation checked after every step
  (§2).
- **The independence tables,** built from the reachable states (§6) and
  checked against the tables in the code.
- **An exact oracle for small traces.** For a trace of a few dozen events on a
  configuration in the registry, the verdict can be had without `tracecheck`'s
  method. Every linear extension of its order is replayed through the shadow;
  each time check is solved again by an independent method (all pairs' shortest
  paths); and each direct check is written again, apart from `tracecheck`'s and
  from its definition, over the raw events: the money ledger summed afresh per
  lease, each reap's amount against its snapshot, the owner's last issued and
  the auditor's the durable one its transaction read, whichever member committed
  it (§7), A3 and A4 per request (§5, §4.2), A4's coverage by every delivery
  against every answer its gateway had by then, every accepted answer against
  the owner's decision to give it, made again from the facts the owner recorded
  with it (§5): its reading of the record's acknowledgement before the cutoff of
  the expiry it knew then and, if the heartbeat echoed a deadline, by it, its
  drain log adopted, and the deadline granted the one the owner's rule gives,
  with the publish result held to the publish deadline, the order's cycles by
  search, the log and its runs by trying every way the acknowledged publishes
  and the deliveries could come from one. It reports each assumption broken as
  §8 says, named as `tracecheck` must name it, K6's control both broken and
  violated, and goes on without it; then pass if every extension is a run, every
  timed predicate holds for every solution and the money checks hold; violation
  if a money check fails, no extension is a run, or a timed predicate holds for
  no solution; inconclusive otherwise, with the first step that fails in each
  extension. `tracecheck` may be more careful than the oracle, never less: its
  pass must be the oracle's pass, its violation the oracle's violation at the
  step it names, each assumption it reports broken the oracle's, and it may call
  inconclusive a trace the oracle decides, since its independence table is
  judged over every state a pair could meet, not the states this trace does. The
  tests count how often it does, so a table grown too careful shows.
- **Traces from the machines.** Each step of a machine emits the events the
  runtime records for it, with their facts, clock readings within S, and
  evidence, so a random run becomes a trace. Each is checked as it is, with
  evidence removed, and altered: a fact changed, two events swapped, an event
  dropped or repeated, a reading or a timestamp moved. The small ones are held
  to the oracle; for every one, an alteration that turns a verdict to a
  violation must name a step the oracle shows failing, or a cycle the oracle's
  solver finds, and one that turns it to inconclusive must name the pair or
  the timed predicate that leaves it open. Some are written out by hand, for
  what random runs may seldom reach: an owner that drains its empty closing
  lease well before its cutoff, its draining write's reply delayed in one
  trace and lost in another; a stream's first heartbeat accepted with no
  deadline echoed, and a retry of it answered the same; a strong read served
  at the last write to its rows, its timestamp before its request and before
  another lease's commit acknowledged before the request; a member's read
  served before another member's commit and received after it, installing the
  older version, its next commit refused; a ticker's read served before a
  renewal and received after it, its mark refused; a member that took a lease
  over reaping at a snapshot another member committed; and a workspace read
  that found no pause, received after a pause came and longer than the cache's
  age after its request, which must refuse an admission,
  `PauseBoundsAdmission` holding. Each must pass.
- **The runtime's own traces,** once the roles record events: the service's
  end-to-end tests and the scenarios of the spike plan's §5, each checked,
  K6's negative control reported as both a broken assumption and a
  violation. These are where the mappings of §4 meet the runtime.

## 10. Steps

- **S6c-0.** Each spec grows as §4.1 says, one pull request a spec: the
  spec, TLC at its registry's configurations, its guard table, its shadow
  and the shadow's comparisons.
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
  apart from the database points, each attempt with its ID, each publish's
  result with its message ID and reading; the store's read-write
  transactions write their journal rows; the load generator
  records each request's answers, deliveries and end; the service's tests
  and the scenarios' traces are checked.
