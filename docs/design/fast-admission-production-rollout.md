# Rolling out fast admission in production

Joseph decided on 2026-10-09: no separate spike project. The new admission
and settle service is tested in production, a stage at a time, slowly. This
replaces the spike plan's separate project, its first run and its measured
runs (`fast-admission-spike.md` §3, S7, S8 and decisions D1 to D3). It is
step 5 of the design's rollout (`fast-admission-and-batched-settlement.md`
§8), done in production; steps 6 to 9, shadow, the benchmark gate, the pilot
and widening, follow as the design has them.

## 1. What holds at every stage

- **No workspace's money moves through the fast path unless that workspace
  is enabled.** A per-workspace switch enables it, read from config as code,
  with one switch that turns the fast path off for every workspace. With a
  workspace off, nothing grants it a lease, and its open leases drain by
  time, as a lease does whose owner stopped (design §4.8).
- **The fast path's tables are new.** The only existing table it writes is
  `tr_credit_balance`, and only an enabled workspace's rows: a lease's grant
  reserves on the workspace's shards, and its close returns what is left,
  as the design's §4.2 has it. Its definition is production's: the Go
  schema's copy is held equal to `scripts/deploy/migrate_typed_counters.sh`'s
  by a test.
- **Schema changes are schema only.** An idempotent migration adds the
  tables, as `scripts/deploy/migrate_spend_lease.sh` does: every statement
  guarded by an `INFORMATION_SCHEMA` check, run by the deploy, and applied
  when no Cloud Run rollout is in flight, since a schema change wounds
  in-flight read-write transactions at its version boundaries.
- **No Spanner topology change.** The production instance and its
  autoscaler carry the load; the 45% CPU alarm stays as it is.
- **Every stage has an exit check and a way back,** below. A stage starts
  only once the one before it has met its exit check.

## 2. Stages

### P0. Dark install

- `scripts/deploy/migrate_fastpath.sh` adds the fast path's tables, from
  `fastpath/schema/spike.sql` less `tr_credit_balance`, which production
  has. A test holds the script's statements to the Go schema's, so the two
  cannot drift.
- Terraform in `infra/` adds the settle log's topic, ordered by lease, and
  the record topic, each stored in `us-central1`; the auditor's
  subscription, with ordering, the record stager's, and the archive
  subscription to a Cloud Storage bucket; a service account with no more
  than the service needs; the fleet's envelope key in Secret Manager; and a
  managed instance group of two `e2-standard-2` admission nodes in
  `us-central1`, with no external address, reachable on 8080 from inside
  the network only.
- A deploy workflow builds the Go service at a commit and rolls the group.
  Its workflow ref joins the WIF allow-list (`infra/gcp_wif.tf`).
- **Exit:** both nodes are members, the auditor holds its keys and commits
  nothing, no workspace is enabled, and the nodes' logs and metrics are in
  Cloud Logging and Monitoring.
- **Way back:** the group scaled to zero. The tables stay, empty.

### P1. Synthetic load on one internal workspace

- One workspace of our own, made for this and funded by an admin grant
  Joseph approves. Its credits are bookkeeping: no request calls a model,
  and no customer's money is in it.
- The load generator runs inside production's network against the front
  doors, at about one request a second first, raised in steps toward the
  benchmark's rate (design §6). The scenario controls run there too:
  a node stopped and handing its leases off (K1, K3), and an owner's clock
  offset (K6), whose expected violation is the synthetic workspace's alone.
- **Measured:** the time the fast path adds to an authorize, against the
  10 ms budget; Spanner commits and CPU per generation; renewals, auditor
  commits, and Pub/Sub's latency and redeliveries.
- **Checked:** after each run, CreditDebt's claims on the workspace's rows,
  as the store's walks check them, read only, by the workspace's keys; and
  each run's traces against the specs, once the trace checker
  (`fast-admission-tracecheck.md`) is in.
- **Exit:** the numbers are in a results document, and no claim fails.
- **Way back:** the load stopped; the leases drain by time.

### P2 onward

Shadow, the benchmark gate, the pilot and widening are the design's §8
steps 6 to 9. Each gets a short note before it starts, saying what it
enables, what it checks and how it turns off. Two things are settled now:

- In shadow, the fast path's leases are granted from a balance kept apart
  for the shadow, never from the workspace's credit rows, so a shadow
  cannot hold up or overspend a workspace's real requests.
- The pilot's first workspace is Joseph's own, and it starts only when he
  says so.
