#!/usr/bin/env bash
# Cover pending AND dead fleet observations without scanning retained done history.
set -euo pipefail
INSTANCE="${SPANNER_INSTANCE_ID:?set SPANNER_INSTANCE_ID}"
DATABASE="${SPANNER_DATABASE_ID:?set SPANNER_DATABASE_ID}"
PROJECT_ARG=()
[ -n "${GCP_PROJECT_ID:-}" ] && PROJECT_ARG=(--project "${GCP_PROJECT_ID}")
column_exists() {
  local n
  n=$(gcloud spanner databases execute-sql "$DATABASE" --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
    --sql="SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE table_name='tr_settle_outbox' AND column_name='unresolved_at'" --format='value(rows[0])') || exit $?
  [ "${n:-0}" != "0" ]
}
index_exists() {
  local n
  n=$(gcloud spanner databases execute-sql "$DATABASE" --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
    --sql="SELECT COUNT(*) FROM INFORMATION_SCHEMA.INDEXES WHERE table_name='tr_settle_outbox' AND index_name='tr_settle_outbox_unresolved'" --format='value(rows[0])') || exit $?
  [ "${n:-0}" != "0" ]
}
apply_ddl() {
  gcloud spanner databases ddl update "$DATABASE" --instance="$INSTANCE" "${PROJECT_ARG[@]}" --ddl="$1"
}
if ! column_exists; then
  apply_ddl "ALTER TABLE tr_settle_outbox ADD COLUMN unresolved_at TIMESTAMP AS (IF(status IN ('pending', 'dead'), COALESCE(created_at, TIMESTAMP '1970-01-01T00:00:00Z'), NULL)) STORED"
  echo "added unresolved_at"
else
  echo "unresolved_at exists; skipped"
fi
if ! index_exists; then
  apply_ddl "CREATE NULL_FILTERED INDEX tr_settle_outbox_unresolved ON tr_settle_outbox (unresolved_at) STORING (actual_cost_micro, status)"
  echo "created tr_settle_outbox_unresolved"
else
  echo "tr_settle_outbox_unresolved exists; skipped"
fi
