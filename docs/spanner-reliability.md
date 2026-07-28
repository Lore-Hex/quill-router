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

Do not retain unbounded per-request authorization, idempotency, generation, or
outbox rows in Spanner. Before high-volume traffic, move ephemeral records to
typed tables with row deletion policies, or run a tested partitioned-DML
retention job that deletes only terminal records after their replay and audit
windows. Bigtable remains the metadata activity store; prompt and output
content must never enter either system.
