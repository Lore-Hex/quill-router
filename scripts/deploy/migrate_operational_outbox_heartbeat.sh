#!/usr/bin/env bash
# Give the operational outbox drain's heartbeat a table of its own.
#
# The ClickHouse node's drain publishes one heartbeat row that /status.json
# reads (clickhouse/ingest_operational_outbox.py, OutboxHeartbeatPublisher).
# It has lived in tr_entities. A table of its own lets the nodes later run
# under a table-scoped database role that has no access to tr_entities. The
# drain writes both rows until that role is turned on; the control plane
# reads the newer of the two. Additive and idempotent: it checks
# INFORMATION_SCHEMA first, and a check that cannot read the schema fails the
# run instead of reading as absent.
set -euo pipefail

PROJECT="${GCP_PROJECT_ID:-quill-cloud-proxy}"
INSTANCE="${SPANNER_INSTANCE_ID:-trusted-router-nam6}"
DATABASE="${SPANNER_DATABASE_ID:-trusted-router}"

count=$(gcloud spanner databases execute-sql "$DATABASE" \
  --project="$PROJECT" \
  --instance="$INSTANCE" \
  --sql="SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES
         WHERE table_name='tr_operational_outbox_heartbeat'" \
  --format='value(rows[0])') || exit $?

if [ "${count:-0}" != "0" ]; then
  echo "tr_operational_outbox_heartbeat exists"
  exit 0
fi

gcloud spanner databases ddl update "$DATABASE" \
  --project="$PROJECT" \
  --instance="$INSTANCE" \
  --ddl="CREATE TABLE tr_operational_outbox_heartbeat (
    id STRING(64) NOT NULL,
    body STRING(MAX) NOT NULL,
    updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true),
  ) PRIMARY KEY (id)"

echo "created tr_operational_outbox_heartbeat"
