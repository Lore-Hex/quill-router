# Spanner reliability

TrustedRouter's production ledger uses the Enterprise Plus `nam6`
multi-region configuration. The application must still monitor transaction
contention and client behavior because the managed-service SLA does not cover
application bugs, quota exhaustion, or hot-row retries.

## Production baseline

- Source project: `quill-cloud-proxy`
- Source instance/database: `trusted-router-nam6` / `trusted-router`
- Configuration: `nam6`, Enterprise Plus, 300 processing units
- Replica topology: two read-write locations (`us-central1`, `us-east1`), two
  read-only locations (`us-west1`, `us-west2`), and one witness
  (`us-central2`). This is five replica locations, not four application
  regions. `nam6` cannot be edited down to three locations; a three-location
  topology would require a separate migration to `nam7`.
- Database deletion protection: enabled
- Point-in-time recovery: 7 days
- Backups: daily full plus incremental every 4 hours, retained for 7 days
- Isolated copy: latest daily full backup copied every day to
  `trustedrouter-dr/trusted-router-backups`, retained for 30 days
- Alert destination: `security@trustedrouter.com`
- Alert conditions reduce replica and method time series before evaluation, so
  one condition opens one incident instead of one incident per series.

Apply or reconcile the baseline:

```bash
bash scripts/deploy/spanner_reliability.sh --apply
```

Set `TR_DR_BILLING_ACCOUNT` on the first run if the DR project does not exist.
Without `--apply`, the script prints the intended changes.

Customer-facing billing-path alerts are managed separately so they can be
reconciled without touching Spanner capacity or backup resources:

```bash
bash scripts/deploy/gateway_reliability.sh --apply
```

These policies deliberately trigger before the broad Spanner contention
policy:

- Any `5xx` from `/internal/gateway/authorize`, `settle`, or `refund` opens an
  incident immediately.
- Successful billing calls taking at least 10 seconds are counted. More than
  two per minute for three consecutive minutes opens one incident.
- The general Spanner contention policy has three independent gates: more than
  100 aborted commits per minute for 10 minutes, lock wait above 2 seconds per
  second for 10 minutes, or lock wait above 30 seconds per second for 5
  minutes. It is intentionally separate because metadata, rate-limit, and
  maintenance transactions must not be reported as customer-facing billing
  degradation.

Every condition reduces all regions and revisions into one time series. The
log-based `5xx` policy rate-limits notifications to one every 30 minutes and
auto-closes after 30 quiet minutes. Metric policies do not renotify while an
incident remains open. Unauthenticated safe reads use a process-local
rate-limit counter so a crawler cannot create a transactional Spanner hot row.
The alerts use status, latency, path, and aggregate transaction metadata only;
they never inspect or export prompts or outputs.

New workspaces and their new API keys start with 16 exact billing shards.
Existing workspaces retain their current shard count until the pause, drain,
verify, reshard workflow completes; changing the default never rewrites a live
ledger in place.

## Alert response

### High CPU

Keep high-priority CPU below 45% in every multi-region replica. Confirm whether
the load is user traffic or system work before scaling. Scaling adds headroom
but does not repair transaction contention. The alert intentionally uses the
maximum observed CPU so short high-priority spikes remain actionable.

Operational reports must read request, generation, token, provider, model,
latency, and error analytics from ClickHouse. Do not aggregate or full-scan raw
Spanner generation, entity, or analytics-outbox rows for reporting. Spanner is
reserved for bounded ledger and control-plane reads on this path.

### Transaction contention

Inspect Spanner transaction and lock insights. A high abort ratio or lock-wait
rate generally means transactions repeatedly update the same keys. Decode the
reported row key before assuming the billing ledger is involved. Check the
billing shard distribution only when the hot row belongs to a billing table;
otherwise repair the specific metadata or worker access pattern before raising
capacity.

### Latency and API failures

Separate Spanner server latency from network and application latency. Inspect
the `method`, `status`, and serving-region labels. `ABORTED` is tracked by the
contention policy; unexpected statuses are tracked by the API-failure policy.

For a gateway billing-path incident:

1. Query Cloud Run request logs for the affected internal gateway path and
   note status, latency, service region, and request ID.
2. Check `SPANNER_SYS.TXN_STATS_TOP_MINUTE` and
   `SPANNER_SYS.LOCK_STATS_TOP_MINUTE`. Decode hot row keys and determine
   whether one workspace or API key dominates.
3. Run the typed billing invariant audit before changing counters.
4. Verify shard distribution. Use the guarded online split for an eligible
   hot workspace; never delete or rewrite live reservations.
5. Confirm post-fix requests settle across multiple shards, no new `5xx`
   appears, and the invariant audit remains clean.

Authorize, idempotency-replay, settle, refund, and typed-finalize transactions
carry stable `tr_*` Spanner transaction tags. Tags identify the operation in
transaction and lock insights without containing a workspace, key, request,
authorization, prompt, or output. Hot row keys still identify the affected
ledger row and must be decoded through the read-only operator path.

The guarded online split briefly pauses new authorizations while existing holds
drain and invariants are checked. A `503 Workspace billing is paused` during
that interval is real customer impact, not an alert false positive. The gateway
logs `billing.authorize_workspace_paused` with only workspace ID and request ID
so the incident is attributable. Schedule proactive legacy-account splits
serially in low-traffic windows; GitHub concurrency keeps at most one pending
run and cancels older pending runs even when `cancel-in-progress` is false.

### Backup workflow

Check the latest workflow execution:

```bash
gcloud workflows executions list tr-spanner-cross-project-backup \
  --location=us-central1 \
  --project=quill-cloud-proxy \
  --limit=5
```

Then verify a recent READY backup exists:

```bash
gcloud spanner backups list \
  --instance=trusted-router-backups \
  --project=trustedrouter-dr \
  --sort-by='~createTime' \
  --limit=5
```

The workflow is idempotent. Re-running it for the same source backup returns
`already_copied`.

## Metadata retention

Backups expose storage growth; they do not cause it. Inspect the latest
per-table footprint with:

```sql
SELECT interval_end, table_name, used_bytes
FROM spanner_sys.table_sizes_stats_1hour
WHERE interval_end = (
  SELECT MAX(interval_end)
  FROM spanner_sys.table_sizes_stats_1hour
)
ORDER BY used_bytes DESC;
```

Per-request state is bounded without deleting active or unresolved billing
records:

- `tr_gateway_authorization`, `tr_reservation`, and `tr_settle_outbox` use a
  nullable `terminal_at` and a 30-day row deletion policy.
- Active, pending, dead, and repairable rows keep `terminal_at=NULL`. Spanner
  TTL does not select a row while that value is NULL.
- A successful settlement makes the reservation immutable but leaves all three
  retention clocks stopped while Bigtable repair is pending. After the
  deterministic activity row is confirmed, one transaction sets
  `terminal_at` on the reservation, authorization, and outbox. It also clears
  the terminal outbox body. The authorization keeps its content-free replay
  record for the bounded idempotency window.
- New terminal generations are not written to `tr_entities`. Existing generic
  rows remain untouched by the migration and can be archived or removed in a
  separate, reviewed operation.
- Bigtable writes new activity, benchmark, synthetic, and rollup cells to
  distinct families with 30-day, 30-day, 14-day, and 730-day GC policies.
  Readers prefer those families and fall back to legacy `m` cells. The
  migration never adds a GC policy to `m`.

Apply the additive migration:

```bash
scripts/deploy/migrate_request_retention.sh
scripts/deploy/migrate_request_retention.sh --apply
```

The first command is a dry run. The apply path refuses to add a Spanner policy
if any row would be immediately eligible. It performs no `DELETE`, `DROP`, or
`terminal_at` backfill.

Roll out application code with `TR_REQUEST_RECORD_WRITE_MODE=legacy` in every
region first. After all revisions can read both layouts and production smoke
passes, switch regions to `typed`. Do not roll back to a revision that predates
typed-table reads after the first typed authorization is created.

Spanner table-size metrics include retained historical versions. TTL deletion
is asynchronous, and physical storage can remain visible until the database
version-retention window and compaction have elapsed.
