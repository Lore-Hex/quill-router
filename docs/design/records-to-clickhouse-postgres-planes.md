# Request records in ClickHouse on the Postgres planes

Joseph asked on 2026-10-19 to try "ClickHouse-only" on AWS and Azure, where
the control planes run on Postgres, not Spanner, and carry little traffic.
This is the design's "move request records to ClickHouse" (§9 of
`fast-admission-and-batched-settlement.md`), the step the capacity models
of 2026-10-18 named as the one that removes the per-request record writes
from the ledger at 100T, tried first where a mistake costs least and where
the same code then carries to GCP.

It is about records and their readers. Money stays where it is.

## 1. What is true today

- **The settle on a Postgres plane writes one transaction**
  (`PostgresStore.finalize_gateway_authorization`): the authorization
  entity rewritten with its finalization facts; an insert-once
  finalization marker (exactly-once settle); the credit balance and key
  rows released and booked; and two things for the record: a `generation`
  entity in `tr_entities` (the full generation, read only by id, never
  purged) and a row in `tr_operational_analytics_outbox` (the content-free
  activity projection). The outbox row is the only thing that reaches
  ClickHouse. Benchmarks are a separate entity write with no outbox.
- **No tenant record page is served on these planes.** The AWS ECS and
  Azure control planes run the `observer` surface only
  (`TR_SERVICE_SURFACE=observer`; `prepare_ecs_release.py` refuses any
  other), and `/activity`, `/v1/activity` and the console's activity pages
  are registered on the control and combined surfaces only, so on AWS and
  Azure they answer 404. Behind them, `PostgresStore` had no reader for
  activity, activity events or usage series until R1 (#1899), and still
  raises not-implemented for key usage (`typed_key_usage`).
  `GET /generation?id=` is registered with the activity routes, so it is
  not served on these planes either.
- **The enclave's lookups do not read records.** The disposition and the
  evidence lookups read the authorization entity, as does a settle replay.
  Moving records touches none of them.
- **Each plane has its own ClickHouse,** reached inside the plane's
  network: AWS `tr-eu-clickhouse-1` in Paris with a plain replica in
  Stockholm and a Parquet archive bucket; Azure `tr-azure-clickhouse-1`
  in uaenorth with no archive. One drain per plane
  (`tr-clickhouse-operational-ingest-postgres.service`) reads the outbox
  in batches, inserts, and deletes only once every target acknowledged:
  at least once, with `ReplacingMergeTree` and `FINAL` collapsing the
  redelivery. ClickHouse down means the outbox grows; nothing is lost and
  no settle fails. The standalone nodes carry the single-node schema set
  only: no benchmark table and no lookup-by-generation-id projection.
- **Gaps:** the AWS eu-west-1 control plane runs with the outbox off, so
  a settle finalized there never reaches ClickHouse; a lookup by
  generation id on these nodes is a full-table scan; the activity
  projection lacks three fields the public generation shape returns
  (`app_id` and the two markup amounts); the repair and delivery
  verifiers are Spanner-only; the control planes read ClickHouse as the
  write user.

So "ClickHouse-only" here means: give the Postgres planes the ClickHouse
readers GCP already has, prove them, then stop writing the `generation`
entity. The ledger, the authorization entity and the key counters stay in
Postgres, and so does every lookup the enclave makes.

## 2. What ClickHouse cannot hold

The exactly-once settle is an insert-once marker plus a conditional update
in one transaction. ClickHouse has no transaction and no conditional
read-modify-write; its deduplication is asynchronous and read-time, and
the drain is at least once by contract. A replayed row would double-count
until a merge. Holds, bookings, reservations, key usage and the
authorization's finalization facts stay in Postgres (`docs/clickhouse-
reliability.md`: a ClickHouse failure must not fail inference or
settlement). This note moves nothing of them.

## 3. Stages

Every stage: a per-plane setting, AWS first, Postgres rows kept in
parallel until the readers are proven, a way back that is one setting, and
the measurement that decides the next stage. The code is shared with GCP;
only the plane's environment, its ClickHouse user and the single-node
schema set are plane-specific.

### R0. Close the gaps (no change for customers)

- Turn the outbox on for eu-west-1 or stop finalizing there (the rollout
  gate's expected outbox state moves with it).
- A single-node variant of the by-generation-id projection, applied to
  Paris, Stockholm and Azure; without it the lookups in R2 scan the table.
- The activity projection and `activity_generations` gain `app_id` and
  the two markup amounts, in all three copies of the column list, which
  must move together.
- A Postgres-sourced delivery verifier: `generation` entities in a window
  against `activity_generations` by id. This is the missing-record
  instrument for every later stage.
- A read-only ClickHouse user on the standalone nodes for the control
  plane.
- **Measured:** drain lag on `/status.json`, the verifier's missing and
  mismatched counts over a day, the projection built.

### R1. ClickHouse readers on the Postgres plane, additive

- A setting, record reads from `postgres` (today) or `clickhouse`, per
  plane, refusing `clickhouse` where the plane sets no ClickHouse URL (the
  standby regions). `PostgresStore` gets the operational-analytics client
  `SpannerStore` has and answers activity, activity events and usage
  series by the code GCP uses (key usage still raises not-implemented).
  Nothing stops being written. The routes that call these readers are not
  served on the Postgres planes; see **Traffic** below.
- **Way back:** the setting, which restores today's behaviour.
- **Measured:** the activity query's p50 and p99 per workspace and its
  error rate, once a reader has traffic; drain lag; the verifier.
- **Traffic:** none yet. The readers serve only the control surface's
  activity and console routes, and no AWS or Azure service runs that
  surface, so a plane switched to `clickhouse` answers no more requests
  than before and its `/activity` measurement is empty. R1's measurement
  needs either a control surface on the plane (a separate decision: it
  makes the plane serve the console) or a probe that calls the readers
  directly as the read-only user. The same holds for R2 and R3:
  `/generation` is a control-surface route. Until a plane serves that
  surface, the evidence that ClickHouse holds the plane's records is the
  delivery verifier alone, and R2 to R4 have no reader to measure.

### R2. `/generation` reads both

- The Postgres entity first, ClickHouse by generation id on a miss; every
  fallback and every field that differs between the two shapes logged.
  The write stays.
- **Measured:** the fallback rate (near zero while Postgres is written),
  the field mismatch count, the ClickHouse point lookup's p99 against the
  entity read's.

### R3. `/generation` reads ClickHouse first

- ClickHouse first, Postgres on a miss. The miss window is the ingest lag:
  the drain's poll, its batch and the insert; `FINAL` reads see a row at
  the insert's acknowledgement. The write stays.
- **Measured:** the Postgres fallback rate, which is the share of lookups
  inside the lag; the settle-commit-to-visible distribution (the synthetic
  probes carry a generation id, or the activity row takes a settle
  timestamp); the enclave's disposition lookups' count and latency, a
  control that must not move.

### R4. Stop writing the `generation` entity

- `TR_GENERATION_RECORDS_ENABLED` exists and `PostgresStore` ignores it;
  it is honoured. The outbox row is the record; the finalization facts
  stay on the authorization entity, so the disposition, the evidence
  lookup and the settle replay are unchanged. The readers keep the
  Postgres path, so the way back is re-enabling the write; rows from the
  gap live in ClickHouse and the fallback never hid them.
- **Before the flip:** a Postgres-sourced repair path (re-enqueue from the
  outbox's own history or the archive), since after this the only copies
  are ClickHouse and the Parquet archive; AWS's archive verified
  restorable; Azure given one. And a decision on what `/generation`
  answers for an authorization that is settled but whose record is not
  visible yet: `pending`, as the fast path's §4.9 answers on GCP, or a
  404 until delivery resumes.
- **Failure mode accepted:** ClickHouse or its drain down means new
  generations are not readable until delivery resumes, the outbox holding
  every record, bounded by the ledger's growth and alarmed by the drain's
  lag. Settles are unaffected.

### R5. Benchmarks

- A `benchmark` event on the Postgres outbox, the benchmark table in the
  single-node schema set, the drain routing it, and the route-health and
  leaderboard readers served from ClickHouse as GCP's are; then the
  benchmark entity stops. The route-health gates tolerate lag only inside
  their window, so the entity write stays until the lag is measured.

### R6. Azure

- The same settings and migrations; the plane-specific work is its
  environment, its drain role, and an archive it does not have.

## 4. Failure modes the path already answers

- **ClickHouse down at settle:** the settle never touches ClickHouse; the
  outbox row commits with the money; the drain stops deleting; the backlog
  shows as lag. Only reads degrade.
- **A settle followed at once by a lookup:** R2 and R3 fall back to
  Postgres; R4 needs the `pending` answer or an in-process just-settled
  cache.
- **Replay:** the outbox's primary key absorbs the ledger's retries; the
  finalization marker prevents a second generation per authorization;
  `ReplacingMergeTree` with `FINAL` absorbs the drain's redelivery; the
  projection is rebuilt on dedup merges.
- **Two drains on one outbox** split rows between nodes: one drain per
  plane, always.

## 5. What this does not remove, and what GCP gets from it

Stays on the Postgres planes: the credit balance, the reservation and its
finalization marker, the authorization and its marker, the key limits and
windows, the deferred-settlement tables and the credit movements; the
disposition and evidence lookups; the settle replay. The ledger's writes
per request are unchanged by this note; what changes is the two record
rows and the readers.

GCP gets the readers and the verifier as shared code, the single-node
projection as a template for the cluster's, and the measured settle-to-
visible lag, which is the number the fast path's record model (§4.9's
`pending`) needs before GCP's own `tr_generation` write can stop.

## 6. Open questions

- Whether any customer traffic is finalized in eu-west-1 today; those
  records exist only as `generation` entities and would be lost at R4
  without R0.
- The `pending` answer for `/generation` at R4, and whether the enclave's
  post-stream pipeline ever reads `/generation` (a stale comment says so;
  the route does not).
- Retention of the authorization entity on Postgres, which this note does
  not touch and which is never purged either.
- The read credential: the planes read ClickHouse as `default` today.

## 7. Order of work

R0 first, as its own pull request series (the eu-west-1 outbox, the
projection, the columns, the verifier, the user), then R1 behind the
setting on AWS Paris, each later stage after its measurement holds for a
week.
