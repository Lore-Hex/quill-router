# Rolling out fast admission in production

Joseph decided on 2026-10-09: no separate spike project. The new admission
and settle service is tested in production, a stage at a time, slowly. This
replaces the spike plan's separate project, its first run and its measured
runs (`fast-admission-spike.md` §3, S7, S8 and decisions D1 to D3). It is
step 5 of the design's rollout (`fast-admission-and-batched-settlement.md`
§8), done in production; steps 6 to 9, shadow, the benchmark gate, the pilot
and widening, follow as the design has them.

The service as merged was built for a project of its own: it admits for any
workspace a caller names, keeps renewing what it holds, reads its key from a
file, and reports trouble by printing. Before it runs in production it gains
the controls below, each a pull request with its tests, reviewed, and none
switched on by merging it.

## 1. What holds at every stage

- **Nothing is admitted or granted for a workspace that is not enabled.**
  Enabled workspaces are an allow-list in the service's configuration,
  empty by default, with one switch that empties it for all (W1).
- **Turning a workspace off ends its leases, and only then does anything
  stop.** Its owners stop admitting and stop renewing; its leases expire,
  drain and close; and the auditor, the ticker, the stager and the pending
  worker keep running until a read-only check shows every lease of it
  closed, its reservations returned and its pending work done (W2). The
  fleet is never scaled to zero with a lease open.
- **The fast path's tables are new.** The only existing table it writes is
  `tr_credit_balance`, an enabled workspace's rows: grants, bookings,
  shortfall and drain raises, returns at close, debt marks and the moves
  between shards that cover a negative one. Membership, records, pending
  work and staging are new tables of its own.
- **Production's load and spend have ceilings.** Each stage states its
  request rate, duration and concurrency, the Spanner CPU, Pub/Sub backlog
  and spend at which it stops, and the job that stops it (W3).
- **Schema changes are schema only,** in production's own migration job,
  which nodes wait for (W4). No Spanner topology change.
- **Every stage has an entry gate, an exit check and a way back,** below.

## 2. Work before P0

- **W1. The switch.** The front door and the owner refuse an authorize for a
  workspace not on the allow-list, the direct owner route included; the
  owner asks for no grant and makes no renewal for one; `Store.Grant`
  refuses one as well, so no caller can reserve for it. Taking a workspace
  off the list closes its leases to admission at once and stops their
  renewal; settles, reaps and recovery of what they admitted go on. The
  list's changes reach every node within a stated bound, and a test holds
  each refusal and the bound.
- **W2. Turning off, and stopping the fleet.** A command reads, by the
  workspace's keys and read only, its open and draining leases, its
  reservations and its pending work, and says whether it is done. The
  procedure: take the workspace off; wait out its leases' expiry and the
  longest a hold lives plus the grace; check with the command; and only
  then stop the nodes. A lease stopped at a gap, or whose log cannot be
  read, is recovered from the archive (design §4.8) before the fleet stops.
- **W3. Ceilings and their stop.** Each node's membership write, once a
  second, and the auditor's scans are measured on the emulator and stated as
  P0's baseline load. A watcher reads production Spanner CPU, the settle
  log's subscription backlog and the stage's spend, and stops the load
  generator, then turns the workspace off, past stated thresholds.
- **W4. The migration.** `scripts/deploy/migrate_fastpath.sh`, idempotent,
  every statement guarded by an `INFORMATION_SCHEMA` check, run by
  `deploy.yml`'s serialized `migrate-schema` job, with
  `tests/conformance/spanner_schema_source.py`'s digests and the schema
  audit's fixture brought up to date, and a test holding its statements to
  the Go schema's. Indexes are checked ready on a rerun. Nodes start only
  after the job has succeeded for the commit they run.
- **W5. Access and network.** Which identity provisions each resource and
  which one runs the nodes, each with only what it needs; the roles that
  only an owner can grant are a short script Joseph runs, as for the
  autoscaler's role. The subnet's egress to Google's APIs and the nodes'
  reach of each other are checked before the nodes start. The service's
  HTTP routes accept only the fleet's own callers.
- **W6. The key.** Each node reads the envelope key from Secret Manager at a
  pinned version at startup. It is not rotated while any lease is open; a
  rotation, when one is needed, waits for every lease to close, or adds
  verification of the previous key first.
- **W7. Alerts.** The service's alerts become log entries at error severity
  that Cloud Monitoring alerts on, and each is exercised once: the auditor
  making no progress, a gap, an audit fault, a drain overdue, pending work
  overdue, and the subscriptions' and archive's lag. The archive keeps the
  settle log's messages with their metadata for the design's retention.
- **W8. Deploys that keep leases.** A new version starts beside the old;
  the old node is marked leaving (`SIGUSR1`), stays reachable until its
  holds have ended, and only then stops. A forced exit is a separate,
  stated procedure. Only a commit whose CI passed is deployed.

## 3. Stages

### P0. Dark install

- **Entry:** W1 to W8 merged.
- The migration adds the fast path's tables; Terraform adds the settle
  log's topic, ordered by lease, the record topic, the auditor's, stager's
  and archive's subscriptions, the archive bucket, the nodes' service
  account and the key's secret; and two `e2-standard-2` nodes start in
  `us-central1`, with no external address, the allow-list empty.
- **Exit:** both nodes are members, the auditor holds its keys and commits
  nothing, every refusal of W1 is seen from inside the network against a
  workspace that exists, the alerts of W7 fire once on an injected fault,
  and the load is W3's baseline. The only rows written are membership's.
- **Way back:** the nodes stopped. No lease can exist, since no workspace
  is enabled.

### P1. Synthetic load on one internal workspace

- **Entry:**
  - The counter reconciler (`storage_gcp_counter_reconcile.py`) counts a
    workspace's lease reservations and bookings with its synchronous holds,
    the design's combined identity, and its repairs refuse a workspace with
    a lease until they do the same.
  - A read-only audit command checks CreditDebt's claims on one workspace's
    rows, against the workspace's funding: its reservations, its bookings
    and their winners, its debt marks, credit conserved, each booking once.
  - The load generator takes the workspace's ID, a load mix from a
    checked-in fixture, and refuses to start until the workspace is funded,
    enabled and at the tier that allows leases; its report gives admitted
    throughput and the latency of admitted, warm authorizes apart from
    refusals.
  - One workspace of our own, made for this and funded by an admin grant
    Joseph approves. No request calls a model; no customer's money is in
    it. It has no synchronous requests, so its only holds are leases'.
- The load runs inside production's network at about one request a second,
  raised in steps toward the design's benchmark rate (§6), each step within
  W3's ceilings.
- **Measured:** the time the fast path adds to an authorize, against the
  10 ms budget; Spanner commits and CPU per generation; renewals, auditor
  commits, and Pub/Sub's latency and redeliveries.
- **Checked:** the audit command after every run.
- **Exit:**
  - The numbers are in a results document, and the audit finds nothing.
  - The trace checker (`fast-admission-tracecheck.md`) is in, the roles
    record their events (its step S6f), and every run's traces are decided:
    ordinary runs pass. Runs made before it was in are repeated.
  - The scenarios run, with the inventory showing every lease on the
    targeted nodes is the synthetic workspace's: a node stopped and handing
    its leases off (K1, K3), and an owner's clock offset (K6), whose traces
    must show the violation K6 is built to cause.
- **Way back:** W2's procedure.

### P2 onward

Shadow, the benchmark gate, the pilot and widening are the design's §8
steps 6 to 9. Each gets a short note before it starts, saying what it
enables, what it checks and how it turns off. Two things are settled now:

- In shadow, the fast path's leases are granted from a balance kept apart
  for the shadow, never from the workspace's credit rows, so a shadow
  cannot hold up or overspend a workspace's real requests.
- The pilot's first workspace is Joseph's own, and it starts only when he
  says so.
