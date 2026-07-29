#!/usr/bin/env bash
# Apply the stage-1 live analytics outbox DDL.
#
# Idempotent and additive. The table is dormant until
# TR_ANALYTICS_OUTBOX_ENABLED=true; this script never changes that setting.
set -euo pipefail

PROJECT="${GCP_PROJECT_ID:-quill-cloud-proxy}"
INSTANCE="${SPANNER_INSTANCE_ID:-trusted-router-nam6}"
DATABASE="${SPANNER_DATABASE_ID:-trusted-router}"

log() { printf '%s %s\n' "[migrate_analytics_outbox]" "$*"; }

table_exists() {
  local count
  count=$(gcloud spanner databases execute-sql "$DATABASE" \
    --project="$PROJECT" \
    --instance="$INSTANCE" \
    --sql="SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES
           WHERE table_name='tr_analytics_outbox'" \
    --format='value(rows[0])' 2>/dev/null || echo 0)
  [ "${count:-0}" != "0" ]
}

if table_exists; then
  log "tr_analytics_outbox exists, skip"
else
  # The ROW DELETION POLICY below is a BACKSTOP, not the cleanup mechanism.
  # Normal operation is drain-then-delete by primary key, which keeps this
  # table near-empty. The policy exists for the failure mode where the drainer
  # stops — unit crashed, node down, ClickHouse full — because an outbox with a
  # dead drainer grows without bound on the production database.
  #
  # That is not hypothetical here: tr_settle_outbox is sitting at ~549k rows
  # and tr_entities has ~9.4M rows with no policy at all (issue #334). Every
  # one of those grew for exactly this reason.
  #
  # 7 days is safe specifically BECAUSE analytics is loss-tolerant: rows older
  # than that mean the ingester has been dead for a week, and the documented
  # recovery is clickhouse/reconcile_benchmark_samples.py replaying from
  # Bigtable, not this table. Never copy this policy onto a money table, where
  # dropping an undrained row would lose a settlement.
  log "creating tr_analytics_outbox on ${PROJECT}/${INSTANCE}/${DATABASE}"
  gcloud spanner databases ddl update "$DATABASE" \
    --project="$PROJECT" \
    --instance="$INSTANCE" \
    --ddl="CREATE TABLE tr_analytics_outbox (
      shard INT64 NOT NULL,
      commit_ts TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true),
      event_id STRING(128) NOT NULL,
      payload STRING(MAX) NOT NULL,
    ) PRIMARY KEY (shard, commit_ts, event_id),
      ROW DELETION POLICY (OLDER_THAN(commit_ts, INTERVAL 7 DAY))"
fi

log "done; enqueue setting was not changed"
