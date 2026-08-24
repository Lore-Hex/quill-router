#!/usr/bin/env bash
# Add the bounded durable queue for ClickHouse activity and status metadata.
set -euo pipefail

PROJECT="${GCP_PROJECT_ID:-quill-cloud-proxy}"
INSTANCE="${SPANNER_INSTANCE_ID:-trusted-router-nam6}"
DATABASE="${SPANNER_DATABASE_ID:-trusted-router}"

count=$(gcloud spanner databases execute-sql "$DATABASE" \
  --project="$PROJECT" \
  --instance="$INSTANCE" \
  --sql="SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES
         WHERE table_name='tr_operational_analytics_outbox'" \
  --format='value(rows[0])' 2>/dev/null || echo 0)

if [ "${count:-0}" != "0" ]; then
  echo "tr_operational_analytics_outbox exists"
  exit 0
fi

gcloud spanner databases ddl update "$DATABASE" \
  --project="$PROJECT" \
  --instance="$INSTANCE" \
  --ddl="CREATE TABLE tr_operational_analytics_outbox (
    shard INT64 NOT NULL,
    commit_ts TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true),
    event_kind STRING(32) NOT NULL,
    event_id STRING(128) NOT NULL,
    payload STRING(MAX) NOT NULL,
  ) PRIMARY KEY (shard, commit_ts, event_kind, event_id),
    ROW DELETION POLICY (OLDER_THAN(commit_ts, INTERVAL 30 DAY))"

echo "created tr_operational_analytics_outbox"
