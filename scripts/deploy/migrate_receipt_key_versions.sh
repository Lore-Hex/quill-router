#!/usr/bin/env bash
# Add nullable receipt-version projections to the generic entity table.
# Existing JSON rows remain readable; the deploy workflow backfills their
# projections immediately after this DDL becomes readable.
set -euo pipefail

INSTANCE="${SPANNER_INSTANCE_ID:?set SPANNER_INSTANCE_ID}"
DATABASE="${SPANNER_DATABASE_ID:?set SPANNER_DATABASE_ID}"
PROJECT_ARG=()
[ -n "${GCP_PROJECT_ID:-}" ] && PROJECT_ARG=(--project "${GCP_PROJECT_ID}")
INDEX_WAIT_ATTEMPTS="${RECEIPT_KEY_INDEX_WAIT_ATTEMPTS:-360}"
INDEX_WAIT_SECONDS="${RECEIPT_KEY_INDEX_WAIT_SECONDS:-5}"

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

index_state() {
  sql_value "SELECT INDEX_STATE FROM INFORMATION_SCHEMA.INDEXES
    WHERE index_name='tr_receipt_key_versions'" || true
}

index_exists() {
  local count
  count=$(sql_value "SELECT COUNT(*) FROM INFORMATION_SCHEMA.INDEXES
    WHERE index_name='tr_receipt_key_versions'" || echo 0)
  [ "${count:-0}" != "0" ]
}

wait_index_read_write() {
  local state=""
  for _ in $(seq 1 "$INDEX_WAIT_ATTEMPTS"); do
    state=$(index_state)
    if [ "$state" = "READ_WRITE" ]; then
      log "tr_receipt_key_versions is read-write"
      return 0
    fi
    log "waiting for tr_receipt_key_versions backfill (state=${state:-missing})"
    sleep "$INDEX_WAIT_SECONDS"
  done
  log "ERROR: timed out waiting for tr_receipt_key_versions to become READ_WRITE after ${INDEX_WAIT_ATTEMPTS} attempts (last state=${state:-missing})"
  return 1
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
wait_index_read_write
log "receipt-key version schema is ready"
