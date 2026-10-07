# Fast admission: the spike

Status: plan, for review. It is step 5 of the rollout in
`docs/design/fast-admission-and-batched-settlement.md` (§8), which this
document calls the design. Nothing here serves production traffic or reads
production data.

The spike builds the owner, renewals and the auditor in Go, runs them in one
GCP region against synthetic load, and answers the five questions §8 lists.
What it learns changes the design before shadow (§8 step 6) begins.

## 1. What it answers

| # | Question (§8 step 5) | Measured by | Passes when |
|---|---|---|---|
| Q1 | Ownership hand-off, and an owner killed mid-stream | Scenarios K1-K4 (§5) with streams open | Every stream ends as §4.5 says: settled through the drain log, reaped at its snapshot, or released. The trace check (Q5) finds no violation. The time from a kill to every lease closed is recorded. |
| Q2 | The hottest workspace's rate on one owner | One lease driven up to its ordering key's limit | One owner admits and decides at least 1,000 generations a second on one lease, the per-key figure §6 assumes, with warm authorize at about 2-3 ms p50 (§6). The CPU per 1,000 generations a second is recorded. |
| Q3 | Pub/Sub ordering across publishers in one region, the per-key limit, record sizes, and redelivery | Probes P1-P4 (§5) | Each of §4.1's assumptions about the settle log holds as stated, or the design is changed before shadow. |
| Q4 | The auditor's conditional commits while its members change, and what it writes | Scenario A1-A3 (§5) | No lease is booked twice or skipped while members start and stop. Rows and bytes per commit, commits a second, and the time to restore a lease on takeover and during draining are recorded. |
| Q5 | Traces from the spike's leases, checked against the specs | Every run's traces through the shadows | Every recorded step is one of its spec's actions and every invariant holds after it (§5.1, "Recorded traces are replayed through the shadow"). |

Each question gets a section in the results document (§7 step S9), with its
numbers and anything in the design that has to change.

## 2. What is built

**One Go service,** `fastpath/cmd/fastpath`, with three roles that a flag
turns on: front door, owner and auditor member. The design runs the front
door and the owner on every admission node (§4.1); the spike does too, and
runs auditor members as separate processes so that they can be killed alone.

**The shadows,** one pure package per spec, as §5.1 places them:

- `fastpath/internal/leaselifecycle`;
- `fastpath/internal/terminalorder`;
- `fastpath/internal/auditorcommit`.

Each is a transition function over the spec's variables with one function per
action, its invariants as functions, and property tests that drive random
sequences of actions and check every invariant after each. The runtime calls
the shadow's transitions, so the code and the checker share one model of each
protocol. When a package first appears, its manifest entry in
`proofs/manifest.toml` moves from planned to implemented, naming the package
and its tests.

**The runtime packages,** under `fastpath/internal/`:

- `ring`: membership and rendezvous hashing (§4);
- `store`: Spanner reads and writes for grants, renewals, the shortfall
  write, drain-log appends, the auditor's per-lease commit and close;
- `settlelog`: the Pub/Sub publisher, one ordering key per lease through the
  region's locational endpoint, and the auditor's subscriber;
- `owner`: the lease's lock, books, holds, sequence numbers, cutoff,
  renewals, checkpoints, returns and adoption;
- `frontdoor`: routing, the drain log, peer retry and revocation;
- `auditor`: per-lease application, the checkpoint audit, ticks, the fence,
  S and close;
- `trace`: the recorder (§6).

**The tools,** under `fastpath/cmd/`:

- `loadgen` plays gateways: authorize, heartbeats on streams, then settle or
  refund, echoing the envelope, at a configured rate and mix;
- `chaos` runs the scenarios in §5 against a running spike;
- `tracecheck` replays recorded traces through the shadows.

**What is left out,** because the spike does not need it:

- the enclave: `loadgen` signs its own boot binding with a key made for the
  spike;
- Python, routing snapshots and catalog prices: one fixed price table;
- ClickHouse: the record topic has a consumer that checks digests and counts,
  and stages nothing;
- keys, caps and trust tiers: one tier, no capped keys;
- other regions, AWS and Azure.

## 3. Where it runs

The spike needs real Pub/Sub and real Spanner: the emulators have no
per-key limit, no locational endpoints, no redelivery timing and no
multi-region commit latency, and those are what Q2 to Q4 measure.

**Spanner.** Three ways, for Joseph (D1, §8):

| Option | Cost | For | Against |
|---|---|---|---|
| A. A separate GCP project with its own `nam6` instance at 100 processing units, destroyed after the spike | about $270 a month while it exists | Production's configuration and commit latency, with nothing shared with production | A second instance to create and destroy |
| B. A database on the production instance | nothing beyond storage | Production's latency exactly | The spike's IAM and load sit beside production's |
| C. A regional instance in a separate project | about $65 a month | Cheapest real Spanner | Regional commits are faster than `nam6`'s, so renewal and auditor timings would be optimistic |

The recommendation is A. The spike writes little: renewals and bookings are
about one transaction per lease every few seconds (§6), and drain-log appends
come in bursts after a kill. 100 processing units carry that.

**Compute.** A managed instance group in `us-central1` (D2):

- four admission nodes, e2-standard-4;
- three auditor members, e2-standard-2;
- one load generator, c3-standard-8, so that the generator is never what
  limits Q2.

About $1 an hour with everything up. The group is scaled to zero between
runs, so the spike's compute should cost a few hundred dollars in all.

GKE would give stable names and rolling deploys, but adds a cluster to run
for a spike. Cloud Run cannot route a request to a chosen instance, which
the owner needs (§4.3).

**Pub/Sub.** In the spike's project:

- the settle-log topic, with message ordering, published through
  `us-central1-pubsub.googleapis.com`;
- the record topic, unordered;
- the auditor's subscription, with ordering on;
- an archive subscription to a Cloud Storage bucket, Avro with metadata, as
  §4.8's rebuild reads it.

At 1,000 generations a second and about 1 KB each (§6), a run carries about
3.6 GB an hour, about $0.14 at $40 a TiB.

**Everything is Terraform,** in `infra/fastpath-spike/`, a root module with
its own state. Creating the project needs Joseph's identity. A `destroy` is
the last step.

## 4. Schema and membership

**Schema.** The spike's DDL lives in `fastpath/schema/spike.sql` and is
applied only to the spike's database. It is a first draft of what production
will need, not a migration:

- `tr_lease`: §4.2's row, with the allocation per donor shard, the fence F,
  the boundary S and T, the commit version, the shortfall total and the
  key-status version;
- `tr_lease_drain`: §4.5's drain log, keyed by lease, authorization and
  record ID, ordered by commit timestamp;
- `tr_lease_winners`: §4.8's packs, one row per lease per commit, with the
  row-deletion policy's timestamp column;
- `tr_credit_balance`, with the columns grants and bookings touch, its debt
  mark included (§4.7);
- `tr_fastpath_member`: membership (below).

Production's schema comes after the spike, from what it learned, through the
deploy's migration scripts and with Postgres's counterpart, as #1571 added
`in_debt`.

**Membership.** The design says how a front door picks an owner (§4.3) but
not how nodes learn of each other. The spike's proposal:

- each node writes its row in `tr_fastpath_member` every second: its
  address, a start time, and whether it is leaving;
- a node is live while its row is younger than three seconds;
- a front door picks a workspace shard's owner by rendezvous hashing over the
  live members that are not leaving. With few members it moves the least work
  when one comes or goes, and it needs no ring of virtual nodes;
- a node marked leaving keeps the leases it has until they drain, and gets
  no new ones (§4.2, retiring).

Correctness does not rest on membership (§4.3): two nodes that both think
they own a shard hold two leases. The spike measures how often that happens
and what it costs in reservation.

## 5. Scenarios and probes

**Pub/Sub probes** (Q3), each a small program run alone:

- **P1, ordering across publishers.** Two publishers in the region publish
  to one ordering key, each waiting for its acknowledgements. The subscriber
  checks that it receives the key's messages in the order Pub/Sub
  acknowledged them, which is what the auditor's fence and S rest on (§4.8).
- **P2, the per-key limit.** One publisher raises its rate on one key until
  publishes slow or fail, with 250-byte messages. This sets the shard count
  K (§4.3, §6).
- **P3, record sizes.** The real records, serialized: settle, refund,
  heartbeat, first heartbeat, reap, checkpoint, tick, hand-off chunk and
  manifest. The design assumes about 250 bytes for most (§4.1).
- **P4, redelivery.** A subscriber that does not acknowledge a message
  before its deadline, or nacks it, and the key's later messages: which are
  redelivered, and in what order. §4.8's takeover skips owner records at or
  below the stored sequence and recognizes the rest by identity, which holds
  whichever way Pub/Sub answers, but its cost depends on it.

**Owner and front door scenarios** (Q1), each with streams open:

- **K1, an owner killed.** SIGKILL with leases open. Streams stop at their
  next heartbeat and settle into the drain log (§4.5), and the auditor drains
  and closes the leases (§4.8).
- **K2, a forced exit.** SIGTERM with a short deadline: hand-off records, a
  manifest, draining.
- **K3, a deploy.** A node marked leaving, a new node joining. Old leases
  drain as their holds end; new admissions go to the new owner.
- **K4, a partition.** A front door that cannot reach an owner: peer retry,
  drain-log appends, and the revocation rate limit.
- **K5, publishes failing.** The owner's publishes fail for a while: it
  admits nothing new under the lease, republishes in order, and stops
  renewing past the expiry window (§4.2, §4.5).
- **K6, clocks.** An owner's clock offset within the skew allowance, which
  must pass, and beyond it, which the trace check must flag. Each renewal
  records the owner's clock beside its Spanner commit timestamp, and that
  pair bounds the owner's skew.

**Auditor scenarios** (Q4):

- **A1, members changing.** Members killed and started every few seconds
  while records flow: commits fail on the commit version and re-read, and
  nothing is booked twice.
- **A2, takeover while draining.** A member killed between storing S and
  closing the lease.
- **A3, a stopped auditor.** Every member stopped for longer than a lease's
  life, then started: leases stay reserved, and drain and close afterwards.

**The hot lease** (Q2): one workspace, one shard, one owner, the load raised
until either the owner's p99 rises past 10 ms or the key's publishes slow.

## 6. Traces

- Every owner, front door and auditor member records each protocol step it
  takes for a lease as one line: the step's spec action, its arguments and
  the variables it changed, with the node and its clock.
- Lines go to a local file and then to the spike's bucket, one object per
  node per run.
- `tracecheck` merges a lease's lines by the order the specs define: the
  owner's sequence numbers, the auditor's commit versions, the drain log's
  commit timestamps. It then replays them through the shadow and stops at the
  first step that is not an action of the spec, or after which an invariant
  fails.
- A trace the checker cannot order is itself a finding: it means the runtime
  takes a step the specs do not describe.

TLC is not fed traces (§5.1).

## 7. Steps

Each step is a pull request, with its tests, reviewed before the next.

- **S1.** The Go module, `fastpath/go.mod` at Go 1.24 as the enclave's, and
  a CI job: `gofmt`, `go vet` and `go test -race`.
- **S2.** `fastpath/internal/leaselifecycle` with its property tests;
  `LeaseLifecycle` becomes implemented.
- **S3.** `fastpath/internal/terminalorder`; `TerminalOrder` becomes
  implemented.
- **S4.** `fastpath/internal/auditorcommit`; `AuditorCommit` becomes
  implemented.
- **S5.** `store` and the spike's schema, tested against the Spanner emulator
  in CI, as the Python store's conformance tests are.
- **S6.** `ring`, `settlelog`, `owner` and `frontdoor`, tested with the
  emulators and fakes; then `auditor`.
- **S7.** `loadgen`, `chaos`, `trace` and `tracecheck`.
- **S8.** `infra/fastpath-spike/` and the first run.
- **S9.** The runs in §5 and a results document,
  `docs/design/fast-admission-spike-results.md`: each question's numbers,
  each finding, and the design changes they call for. Then `destroy`.

S1 to S7 need no decision and no infrastructure. S8 waits for D1 to D3.

## 8. Decisions for Joseph

- **D1. Where Spanner runs:** A, B or C in §3. The recommendation is A.
- **D2. Compute:** a managed instance group of VMs, as in §3.
- **D3. A budget:** about $500 for the spike, infrastructure destroyed after
  its results are written.

## 9. Not decided here

- Production's schema, and its Postgres counterpart.
- More than one region, and the path of gateways on AWS and Azure (§4.1).
- The gateway load balancer (§4.12, §8 step 2).
- The enclave's changes: the stream-open heartbeat and its declaration (§4.5,
  §9 of the design).
- How the admission service is deployed in production. The spike's managed
  instance group is for the spike.
