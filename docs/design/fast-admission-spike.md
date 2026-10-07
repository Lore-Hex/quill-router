# Fast admission: the spike

Status: plan, for review. It is step 5 of the rollout in
`docs/design/fast-admission-and-batched-settlement.md` (§8), which this
document calls the design. Nothing here serves production traffic. One
read-only query of ClickHouse, for aggregates only, sets the load's mix
(§5); nothing else reads production data.

The spike builds the owner, renewals and the auditor in Go, runs them in one
GCP region against synthetic load, and answers the five questions §8 lists.
What it learns changes the design before shadow (§8 step 6) begins.

A question's result is PASS or FAIL on its own terms. A FAIL is a finding,
not a failed spike: the design changes before shadow, and the results
document says what changed. The spike is done when every question has a
result, whichever it is.

## 1. What it answers

| # | Question (§8 step 5) | Measured by | Passes when |
|---|---|---|---|
| Q1 | Ownership hand-off, and an owner killed mid-stream | Scenarios K1-K6 (§5), each with streams open | Each scenario's own assertions in §5 hold, and the trace check (Q5) finds no violation in its ordinary runs. |
| Q2 | The hottest workspace's rate on one owner | H1 and H2 (§5) | At 1,000 generations a second on one lease, the figure §6 assumes for one ordering key, warm authorize measured at the load generator is at most 3 ms p50 and 10 ms p99 (§6, and the design's target of about 10 ms of overhead). One owner's own limit, across as many leases as it takes to reach it, is recorded: it sizes the fleet. |
| Q3 | Pub/Sub ordering across publishers in one region, the per-key limit, record sizes, and redelivery | Probes P1-P4 (§5) | Each probe's condition in §5 holds. |
| Q4 | The auditor's conditional commits while its members change, and what it writes | Scenarios A1-A4 (§5) | A1-A3's assertions hold. A4 records rows and bytes per commit, commits a second, and the time to restore a lease, with thousands of open holds and of winners with pending work. |
| Q5 | Traces from the spike's leases, checked against the specs | Every run's traces through the shadows (§6) | No trace of an ordinary run shows a violation, and every negative control's trace shows the violation it was built to cause (K6). A trace the evidence cannot order is inconclusive, not a pass: the recorder gains the evidence it lacked, and the run is repeated, until every scenario's traces are decided. |

Each question gets a section in the results document (§7, S8), with its
numbers and anything in the design that has to change.

## 2. What is built

**One Go service,** `fastpath/cmd/fastpath`, with three roles that a flag
turns on: front door, owner and auditor member. The design runs the front
door and the owner on every admission node (§4.1); the spike does too, and
runs auditor members as separate processes so that they can be stopped alone.

**The shadows,** one pure package per spec, as §5.1 places them:

- `fastpath/internal/leaselifecycle`;
- `fastpath/internal/terminalorder`;
- `fastpath/internal/auditorcommit`.

Each is a transition function over the spec's variables with one function per
action, and its invariants as functions. The runtime calls the shadow's
transitions, so the code and the checker share one model of each protocol.
Each has three kinds of test:

- property tests that drive random sequences of actions, with larger bounds
  than the spec's, and check every invariant after each;
- exact comparisons with TLC. TLC writes an instance's whole state graph
  with its actions' names (`-dump dot,actionlabels`), and the test checks
  that from every state the shadow enables the same actions and reaches the
  same successors. A transition the shadow adds between states TLC reaches
  anyway shows here, where a count of states would miss it. The tests
  compare small instances and every configuration `proofs/` checks, whose
  graphs reach some 800,000 states and are read as they stream;
- the binding of each configuration a test declares to its `.cfg`. TLC
  reads the file as it is and evaluates the declaration as an assumption,
  and the shadow must reach as many distinct states as the guard table
  records (`[states]`, #1582). Without the binding, a declaration one
  constant off can reach the same count: LeaseLifecycle with `MaxHolds` 2
  does.

When a package first appears, its manifest entry in `proofs/manifest.toml`
moves from planned to implemented, naming the package and its tests, in the
same pull request: the `proofs` job fails once a planned entry's path
exists.

**The runtime packages,** under `fastpath/internal/`:

- `ring`: membership and rendezvous hashing (§4);
- `store`: Spanner reads and writes for grants, renewals, the shortfall
  write, drain-log appends, the auditor's per-lease commit, pending work and
  close;
- `settlelog`: the Pub/Sub publisher, one ordering key per lease through the
  region's locational endpoint, and the auditor's subscriber;
- `owner`: the lease's lock, books, holds, sequence numbers, cutoff,
  renewals, checkpoints, returns and adoption;
- `frontdoor`: routing, the drain log, peer retry, revocation and leaving
  the ring;
- `auditor`: per-lease application, the checkpoint audit, ticks, the fence,
  S, pending work and close;
- `trace`: the recorder (§6).

**The tools,** under `fastpath/cmd/`:

- `loadgen` plays gateways: authorize, heartbeats on streams, then settle or
  refund, echoing the envelope, at a configured rate and mix (§5). It keeps a
  retry queue for settles as the enclave does (§4.5);
- `chaos` runs the scenarios in §5 against a running spike;
- `tracecheck` replays recorded traces through the shadows.

**Stand-ins,** where the design names a system the spike does not run:

- the enclave: `loadgen` signs its own boot binding with a key made for the
  spike;
- Python, routing snapshots and catalog prices: one fixed price table;
- ClickHouse's staging of full records (§4.9): a table in the spike's
  database, written by the record topic's consumer, so that the auditor's
  pending work joins against real staged records. Its writes are tagged and
  counted apart, since production stages elsewhere.

Left out: keys, caps and trust tiers beyond one tier; other regions, AWS and
Azure.

**Times are scaled down for the correctness scenarios only,** so that each
fits in an hour: K1-K6 and A1-A3 run with holds that live at most 5 minutes
instead of 2 h 20 min, and the heartbeat interval, the expiry window, the
grace and the publish deadline scaled with them. The ratios the design rests
on are kept: the grace is more than twice the skew allowance, and the publish
deadline is shorter than the grace less twice the skew (§4.5). One run of K1
at the design's real times confirms the scaled ones.

Scaling changes the load: at a fixed number of open streams, a shorter
heartbeat interval multiplies the heartbeats. So the measurements, P2, H1, H2
and A4, run at production's cadence and deadlines.

## 3. Where it runs

The spike needs real Pub/Sub and real Spanner for what it measures. The
emulators run the functional tests (§7): Pub/Sub's emulator supports ordering
keys and redelivery, and Spanner's runs the store's statements. Neither
reproduces production's timing, quotas or regional behavior: the per-key
limit, locational endpoints, redelivery delays and multi-region commit
latency. Spanner's emulator also serializes read-write transactions over the
whole database, so it shows nothing about contention.

**Spanner.** Three ways, for Joseph (D1, §8):

| Option | Cost | For | Against |
|---|---|---|---|
| A. A separate GCP project with its own `nam6` instance, destroyed after the spike | about $270 a month at 100 processing units, and $3.705 an hour at 1,000 | Production's configuration, with nothing shared with production | A second instance to create and destroy |
| B. A database on the production instance | storage, plus whatever the autoscaler adds for the spike's load, which the spike would then share with production | Production's configuration and size | The spike's IAM and load sit beside production's, and production's load moves the spike's latency |
| C. A regional instance in a separate project | about $65 a month at 100 processing units | Cheapest real Spanner | Regional commits are faster than `nam6`'s, so renewal and auditor timings would be optimistic |

The recommendation is A. Development runs use 100 processing units. The
measurement runs use 1,000, production's autoscaling floor
(`infra/spanner_trusted_router.tf`), and record CPU, aborts and commit
latency tails, so the sizing comes from measurement rather than from a count
of transactions. What the spike writes grows with requests, not only with
leases: about two record rows per generation (§6), each hold's snapshots,
the winners' packs, drain-log appends after a kill, and shortfall writes. At
1,000 generations a second that is a few thousand row writes a second. A
match of production's configuration still is not a match of its tails:
production carries other load.

**Compute.** A managed instance group in `us-central1` (D2):

- four admission nodes, e2-standard-4;
- three auditor members, e2-standard-2;
- one load generator, c3-standard-8, so that the generator is never what
  limits Q2.

About $1.20 an hour with everything up. The group is scaled to zero between
runs.

GKE would give stable names and rolling deploys, but adds a cluster to run
for a spike. Cloud Run cannot route a request to a chosen instance; its
session affinity is best effort. The owner needs that routing (§4.3).

**Pub/Sub.** In the spike's project:

- the settle-log topic, with message ordering, published through
  `us-central1-pubsub.googleapis.com`;
- the record topic, unordered;
- the auditor's subscription, with ordering on, and the record topic's
  consumer;
- an archive subscription to a Cloud Storage bucket, Avro with metadata, as
  §4.8's rebuild reads it.

At 1,000 generations a second and about 1 KB of money records each (§6), the
settle log carries about 3.6 GB an hour. Publishing and delivery are billed
at $40 a TiB each and the export at $50, about $0.43 an hour, before the
full records, which are larger, and redeliveries.

**The budget** (D3): about $1,000 in all. Spanner at 100 processing units for
six weeks and about 40 hours at 1,000, compute for about 60 hours of runs,
and Pub/Sub for the runs come to roughly $700.

**Everything is Terraform,** in `infra/fastpath-spike/`, a root module with
its own state. Creating the project needs Joseph's identity. A `destroy` is
the last step.

## 4. Schema and membership

**Schema.** The design fixes what is stored, not how (§4.2, §4.5, §4.8, §4.9).
What follows is the spike's proposal, in `fastpath/schema/spike.sql`, applied
only to the spike's database. It is a first draft of what production will
need, not a migration:

- `tr_lease`: §4.2's row, with the allocation's total and its accounting
  (L as granted, the shortfall total, the front doors' raises, returns, and
  consumption beyond the allocation booked as usage), the fence F, the
  boundary S and T, the commit version, the key-status version, and the
  auditor's progress: the highest owner sequence number applied, the last
  tick, the sum the checkpoints are audited against, the applied list of
  open holds, the alert and the gap. Checks on the row hold its accounting
  and its states, and the row may be deleted seven days after the lease has
  closed with no pack's work pending;
- `tr_lease_donor`: the allocation per donor shard, a row per donor, so each
  writer's arithmetic is one conditional statement. Donors are in ascending
  shard order: bookings take from the first donor first, returns from the
  last;
- `tr_lease_hold`: §4.8's stored open holds, one row per hold the log has
  shown: its estimate, its latest valid snapshot (sequence, hash, usage and
  running charge) and its deadline, and the first heartbeat record's
  fields. A member that takes over a draining lease builds a reap's full
  record from those (§4.9), and Pub/Sub does not deliver a record again once
  it is acknowledged. The auditor's per-lease commit inserts and updates these
  rows and deletes a hold's row in the commit that stores its winner. If row
  writes dominate A4, a packed row per lease is measured against it;
- `tr_lease_winners`: §4.8's packs, one row per lease per commit, each winner
  with its pending work, and the timestamp the row-deletion policy reads,
  set once the lease is closed and the pack's work is done;
- `tr_lease_drain`: §4.5's drain log, keyed by lease, authorization and
  record ID, ordered by commit timestamp and then record ID, since two
  independent appends can share a timestamp;
- `tr_lease_record`: the records the pending work writes, standing for the
  generation and activity records and the disposition records (§4.9);
- `tr_spike_staged`: the stand-in for staging (§2);
- `tr_credit_balance`: production's table word for word, which a test holds
  equal to `scripts/deploy/migrate_typed_counters.sh`'s, so the spike's
  statements meet production's columns, nullability and defaults, the debt
  mark included (§4.7);
- `tr_fastpath_member`: membership (below).

Production's schema comes after the spike, from what it learned, through the
deploy's migration scripts and with Postgres's counterpart, as #1571 added
`in_debt`.

**Membership.** The design says how a front door picks an owner (§4.3) but
not how nodes learn of each other. The spike's proposal:

- each node writes its row in `tr_fastpath_member` every second: its
  address, a start time, and its state: serving, leaving or withdrawn;
- a node is live while its row is younger than three seconds;
- a front door picks a workspace shard's owner by rendezvous hashing over the
  live members that are serving. It moves only the departed member's shards
  when one goes, and needs no ring of virtual nodes;
- a node marked leaving keeps the leases it has until they drain, and gets
  no new ones (§4.2, retiring);
- **a front door that cannot reach several owners withdraws** (§4.3): when
  calls to two or more distinct owners fail within a few seconds while
  others' front doors reach them, it marks itself withdrawn and takes no new
  requests until it reaches owners again. `loadgen` sends only to serving
  front doors, as gateways would through the load balancer (§4.12). So a
  partitioned front door does not route healthy owners' terminals into the
  drain log.

Correctness does not rest on membership (§4.3): two nodes that both think
they own a shard hold two leases. The spike measures how often that happens
and what it costs in reservation.

## 5. Scenarios and probes

**The load,** unless a scenario says otherwise, follows today's traffic. Its
mix is a fixture of aggregates, `fastpath/testdata/load-mix.json`: shares and
a histogram, no workspace, key or request. It is made once by a read-only
ClickHouse query over the analytics tables, as AGENTS.md has request analytics
done, and committed with the query that made it:

- today's share of streaming requests, with heartbeats at the enclave's
  interval, about three per generation (§6);
- today's share of refunds among terminals;
- today's distribution of bill against hold, in which about one settle in
  eight charges more than its hold (§4.5);
- no idempotency keys, so every request gets an invocation scope (§4.3).

**Pub/Sub probes** (Q3), each a small program run alone. Google's publisher
documentation says publishers cannot know the order between them, so P1 asks
only what the design relies on.

- **P1, ordering across publishers.** The fence and S rely on this: a record
  whose publish was acknowledged before another publisher's publish began is
  received first, and a subscriber is given a key's messages in the order
  Pub/Sub received them (§4.8).
  - Precedence: publisher A publishes, waits for the acknowledgement, and
    signals B, which then publishes to the same key. Over many rounds, every
    subscriber must receive A's message first, and again after a forced
    redelivery.
  - Agreement: A and B publish to one key at once, without coordination. Two
    subscriptions, and a redelivery to each, must see one order.
  - The runtime's own case is covered in K1 and A2: the owner's last records
    and the auditor's fence tick.
  - Passes when no round violates precedence and no two observations of one
    key disagree.
- **P2, the per-key limit.** One publisher raises its rate on one key with
  real records until publishes fail or their latency grows. Passes when the
  key sustains 1 MBps, §6's figure, for ten minutes with no error and no
  growing backlog. The highest sustained rate is recorded; it sets the shard
  count K (§4.3, §6).
- **P3, record sizes.** Every record kind, serialized with realistic values:
  settle, refund, heartbeat, first heartbeat, reap, checkpoint, tick,
  hand-off chunk and manifest, with p50, p99 and largest. Passes when
  settles and heartbeats are about 250 bytes at p50 (§4.1). If they are not,
  K and the per-lease rate change.
- **P4, redelivery.** A subscriber nacks a message, or lets its deadline
  pass, with later messages of the same key outstanding. The probe records
  which messages come back, in what order and after how long. Passes when
  the behavior is what §4.8's takeover is written for: a redelivered message
  brings the key's later ones again, in order, and the auditor skips owner
  records at or below its stored sequence number (A1 checks that side).

**Owner and front door scenarios** (Q1), each with streams open. Each has
its own assertions:

- **K1, an owner killed.** SIGKILL with leases open.
  - Streams that had heartbeated stop at their next heartbeat, and their
    settles reach the drain log within one heartbeat interval (§4.5).
  - Holds without a first heartbeat send no settle, and end at the lease's
    close.
  - Every request `loadgen` made has exactly one outcome: one winning
    terminal, charged once, or a release at its lease's close. A retried
    settle may leave a second drain-log row, which loses. No settle is lost
    while the retry queue lasts.
  - The auditor marks the leases draining after expiry plus the skew, and
    closes each once its end condition holds (§4.8). The time from the kill
    to every lease closed is recorded.
- **K2, a forced exit.** SIGTERM with a short deadline.
  - A complete hand-off spares the auditor only the wait for holds it cannot
    see. It still closes each lease after its fence tick, with S stored, the
    records up to it applied and the close's read of the drain log (§4.8),
    once the holds the hand-off lists have ended, and not after the longest
    life of an unknown hold.
  - In a second run the owner is killed partway through its hand-off. That
    partial hand-off counts as none, and the leases close by time.
- **K3, a deploy.** A node marked leaving and a new node joining.
  - No stream is cut: existing streams keep heartbeating to the retiring
    owner and settle through it.
  - New admissions go to the new owner.
  - Old leases close after their holds end. The overlap in reservation and
    in capacity is recorded (§4.2).
- **K4, partitions.** Three cuts, each with its own assertions (§4.3):
  - One front door cut off from one owner that its peers still reach. Its
    terminals reach the owner through a peer, and its heartbeats too, within
    their sub-second cap. Nothing goes to the drain log, and no stream stops.
  - An owner cut off from every front door. Terminals go to the drain log
    and heartbeats get `retry`, so streams stop and settle there.
    Revocations stay within their rate limits.
  - One front door cut off from several owners. It withdraws, and after
    that sends no terminal of a reachable owner's lease to the drain log.
- **K5, publishes failing.** The owner's publishes fail for a while.
  - It admits nothing new under the lease and answers terminals with an
    error, which `loadgen` retries.
  - It republishes in order, with the same sequence numbers. The log shows
    no gap and nothing out of order.
  - Past the expiry window it stops renewing, and the lease drains (§4.2).
- **K6, clocks.** The fault injector offsets an owner's clock by a known
  amount, within the skew allowance and beyond it.
  - Within it, every assertion above holds.
  - Beyond it, the trace check reports the broken assumption from the
    injected offset, and from each renewal's bracket: the owner's clock
    before it sends and after it hears back, around the commit timestamp.
  - A too-large offset does not break an invariant in every run. So a
    negative control is built: a scheduled run in which the offset makes the
    owner admit after the auditor marked its lease draining. The trace check
    must report that violation.

**Auditor scenarios** (Q4):

- **A1, members changing.**
  - Controlled: member A loads lease L at commit version v and is paused.
    Member B takes L over and commits. A resumes, and its commit must fail on
    the version, after which it re-reads.
  - Controlled: an owner's shortfall write and a front door's raise land
    between a member's read and its commit. The raise does not advance the
    version (§4.2), and the commit must keep it.
  - Random: members stopped and started every few seconds while records
    flow. Nothing is booked twice, and no record above the stored sequence
    is skipped.
- **A2, takeover while draining.** A member stopped between storing S and
  closing the lease. The next member uses the stored S, and the close
  happens once.
- **A3, a stopped auditor.** Every member stopped for longer than a lease's
  life, then started. Leases stay reserved meanwhile, and drain and close
  afterwards.
- **A4, size.** Leases with 2,000 open streams and with thousands of winners
  whose pending work is not done. Rows and bytes per commit, commits a
  second, the time to restore a lease on takeover, and the time to finish
  pending work.

**The hot path** (Q2):

- **H1, one lease.** One workspace, one shard, one owner, the load raised to
  1,000 generations a second and then to the key's limit (P2). Warm
  authorize is timed at `loadgen`, at the front door and at the owner.
- **H2, one owner.** One owner given more and more leases at that rate each,
  until its p99 passes 10 ms or its CPU is the limit. That rate is the
  owner's limit, and the CPU per thousand generations a second is recorded.

## 6. Traces

A trace has to say enough to put every step in an order the specs can check.

- **Every event has an identity:** its node, its process's epoch and a
  sequence number local to the process, with the process's monotonic clock
  and wall clock. The local sequence orders a process's own events.
- **Every message names its cause.** A request between nodes carries the
  sender's event identity, and the receiver records it. So a renewal's
  answer follows its request, and a front door's append follows the failed
  call that caused it.
- **Spanner orders its writes,** as far as their timestamps do: each write
  records its commit timestamp, and each read its read timestamp.
  Independent transactions can commit at the same timestamp. The drain log
  breaks that tie by record ID, as §4.5 does; any other tie is left
  unordered, and counts as missing evidence if the order matters.
- **Pub/Sub's order is observed:** a publisher records each message's ID and
  the event that published it; a subscriber records the order in which it
  was given each key's messages.
- **Each shadow has a mapping** from events to its actions, in its package.
  Actions of the environment that no node records, such as time passing or a
  lost message, are inserted where the recorded steps need them and the spec
  allows them.
- `tracecheck` builds the order these give and replays the events through
  the shadow. A step that is no action of the spec, or after which an
  invariant fails, is a violation. A trace whose evidence leaves two orders
  open that the shadow judges differently is inconclusive, with the events
  it could not order named.

Traces go to a local file and then to the spike's bucket, one object per
process per run. TLC is not fed traces (§5.1).

## 7. Steps

Each step is a pull request, with its tests, reviewed before the next.

- **S1.** The Go module, `fastpath/go.mod` at Go 1.24 as the enclave's, a CI
  job (`gofmt`, `go vet`, `go test -race`, and `go test` for the whole-graph
  comparisons, too slow under the race detector), and
  `fastpath/internal/leaselifecycle` with its tests. `LeaseLifecycle` becomes
  implemented in the same pull request.
- **S2.** `fastpath/internal/terminalorder`; `TerminalOrder` becomes
  implemented.
- **S3.** `fastpath/internal/auditorcommit`; `AuditorCommit` becomes
  implemented.
- **S4.** `store` and the spike's schema, tested against the Spanner emulator
  in CI, as the Python store's conformance tests are.
- **S5.** `ring`, `settlelog`, `owner` and `frontdoor`, tested with the
  emulators and fakes; then `auditor`.
- **S6.** `loadgen`, `chaos`, `trace` and `tracecheck`, with `tracecheck`
  tested on traces recorded from the shadows' own random runs, and on
  altered copies that must be reported.
- **S7.** `infra/fastpath-spike/` and the first run.
- **S8.** The runs in §5 and a results document,
  `docs/design/fast-admission-spike-results.md`: each question's result and
  numbers, each finding, and the design changes they call for. Then
  `destroy`.

S1 to S6 need no decision and no infrastructure. S7 waits for D1 to D3.

## 8. Decisions for Joseph

- **D1. Where Spanner runs:** A, B or C in §3. The recommendation is A.
- **D2. Compute:** a managed instance group of VMs, as in §3.
- **D3. A budget:** about $1,000 for the spike, its infrastructure destroyed
  after its results are written.

## 9. Not decided here

- Production's schema, and its Postgres counterpart.
- More than one region, and the path of gateways on AWS and Azure (§4.1).
- The gateway load balancer (§4.12, §8 step 2).
- The enclave's changes: the stream-open heartbeat and its declaration (§4.5,
  §9 of the design).
- How the admission service is deployed in production. The spike's managed
  instance group is for the spike.
