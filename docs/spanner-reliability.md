# Spanner reliability

TrustedRouter's production ledger uses the Enterprise Plus `nam6`
multi-region configuration. The application must still monitor transaction
contention and client behavior because the managed-service SLA does not cover
application bugs, quota exhaustion, or hot-row retries.

## Production baseline

- Source project: `quill-cloud-proxy`
- Source instance/database: `trusted-router-nam6` / `trusted-router`
- Configuration: `nam6`, Enterprise Plus, 300 processing units
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

## Alert response

### High CPU

Keep high-priority CPU below 45% in every multi-region replica. Confirm whether
the load is user traffic or system work before scaling. Scaling adds headroom
but does not repair transaction contention.

### Transaction contention

Inspect Spanner transaction and lock insights. A high abort ratio or lock-wait
rate generally means transactions repeatedly update the same keys. Check the
billing shard distribution and recent schema or settlement changes before
raising capacity again.

### Latency and API failures

Separate Spanner server latency from network and application latency. Inspect
the `method`, `status`, and serving-region labels. `ABORTED` is tracked by the
contention policy; unexpected statuses are tracked by the API-failure policy.

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
