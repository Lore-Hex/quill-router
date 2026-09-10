#!/usr/bin/env bash
# Add nullable receipt-version projections to the generic entity table.
# Existing JSON rows remain readable and are populated on their next observation.
set -euo pipefail

INSTANCE="${SPANNER_INSTANCE_ID:?set SPANNER_INSTANCE_ID}"
DATABASE="${SPANNER_DATABASE_ID:?set SPANNER_DATABASE_ID}"
PROJECT_ARG=()
[ -n "${GCP_PROJECT_ID:-}" ] && PROJECT_ARG=(--project "${GCP_PROJECT_ID}")

log() { printf '%s %s\n' "[migrate_receipt_key_versions]" "$*"; }

sql_value() {
  gcloud spanner databases execute-sql "$DATABASE" \
    --instance="$INSTANCE" "${PROJECT_ARG[@]}" --sql="$1" \
    --format='value(rows[0])' 2>/dev/null
}

apply_ddl() {
  log "applying: $1"
  gcloud spanner databases ddl update "$DATABASE" \
    --instance="$INSTANCE" "${PROJECT_ARG[@]}" --ddl="$1"
}

column_exists() {
  local column="$1" count
  count=$(sql_value "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
    WHERE table_name='tr_entities' AND column_name='${column}'" || echo 0)
  [ "${count:-0}" != "0" ]
}

index_exists() {
  local count
  count=$(sql_value "SELECT COUNT(*) FROM INFORMATION_SCHEMA.INDEXES
    WHERE index_name='tr_receipt_key_versions'" || echo 0)
  [ "${count:-0}" != "0" ]
}

if ! column_exists kid; then
  apply_ddl "ALTER TABLE tr_entities ADD COLUMN kid STRING(43)"
fi
if ! column_exists att_sha256; then
  apply_ddl "ALTER TABLE tr_entities ADD COLUMN att_sha256 STRING(43)"
fi
if ! index_exists; then
  apply_ddl "CREATE NULL_FILTERED INDEX tr_receipt_key_versions ON tr_entities (kid, att_sha256)"
fi
# Spanner continues an accepted index build after the initiating gcloud process
# exits.  A later rollout must not turn that durable asynchronous operation into
# a 30-minute deployment failure by polling its transient CREATING state.  The
# read path is correct without the index while Spanner finishes the build.
log "receipt-key version schema is ready"
