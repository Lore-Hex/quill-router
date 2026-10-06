#!/usr/bin/env bash
# Additive only; apply outside rolling deploys, before enabling async admission.
# Existing enqueue/mark statements leave these columns NULL. No backfill.
set -euo pipefail
INSTANCE="${SPANNER_INSTANCE_ID:?set SPANNER_INSTANCE_ID}"
DATABASE="${SPANNER_DATABASE_ID:?set SPANNER_DATABASE_ID}"
PROJECT_ARG=()
[ -n "${GCP_PROJECT_ID:-}" ] && PROJECT_ARG=(--project "${GCP_PROJECT_ID}")
column_exists() {
  local n
  n=$(gcloud spanner databases execute-sql "$DATABASE" --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
    --sql="SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE table_name='tr_settle_outbox' AND column_name='$1'" --format='value(rows[0])')
  [ "${n:-0}" != "0" ]
}
index_exists() {
  local n
  n=$(gcloud spanner databases execute-sql "$DATABASE" --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
    --sql="SELECT COUNT(*) FROM INFORMATION_SCHEMA.INDEXES WHERE table_name='tr_settle_outbox' AND index_name='tr_settle_outbox_workspace_status'" --format='value(rows[0])')
  [ "${n:-0}" != "0" ]
}
apply_ddl() {
  gcloud spanner databases ddl update "$DATABASE" --instance="$INSTANCE" "${PROJECT_ARG[@]}" --ddl="$1"
}
if ! column_exists async_version; then
  apply_ddl "ALTER TABLE tr_settle_outbox ADD COLUMN async_version INT64"
fi
if ! column_exists workspace_id; then
  apply_ddl "ALTER TABLE tr_settle_outbox ADD COLUMN workspace_id STRING(64)"
fi
if ! column_exists snapshot_hash; then
  apply_ddl "ALTER TABLE tr_settle_outbox ADD COLUMN snapshot_hash STRING(64)"
fi
if ! column_exists payload_hash; then
  apply_ddl "ALTER TABLE tr_settle_outbox ADD COLUMN payload_hash STRING(64)"
fi
if ! index_exists; then
  apply_ddl "CREATE NULL_FILTERED INDEX tr_settle_outbox_workspace_status ON tr_settle_outbox (workspace_id, status) STORING (actual_cost_micro)"
fi
