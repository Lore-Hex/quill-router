#!/usr/bin/env bash
# Retire the legacy status/timestamp settle-outbox index after all serving
# regions run code that reads tr_settle_outbox_due_v2.
#
# This is intentionally a separate, post-rollout step. The additive migration
# creates and backfills the generated shard plus v2 index before new code serves.
# Keeping the old index through rollout preserves rollback compatibility; this
# script removes it only after the regional canaries and production smoke pass.
set -euo pipefail

INSTANCE="${SPANNER_INSTANCE_ID:?set SPANNER_INSTANCE_ID}"
DATABASE="${SPANNER_DATABASE_ID:?set SPANNER_DATABASE_ID}"
PROJECT_ARG=()
[ -n "${GCP_PROJECT_ID:-}" ] && PROJECT_ARG=(--project "${GCP_PROJECT_ID}")

log() { printf '%s %s\n' "[retire_settle_outbox_hot_index]" "$*"; }

sql_value() {
  gcloud spanner databases execute-sql "$DATABASE" \
    --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
    --sql="$1" --format='value(rows[0])'
}

v2_count="$(sql_value \
  "SELECT COUNT(*) FROM INFORMATION_SCHEMA.INDEXES
   WHERE table_name='tr_settle_outbox'
     AND index_name='tr_settle_outbox_due_v2'
     AND is_null_filtered=true
     AND index_state='READ_WRITE'")"
if [ "${v2_count:-0}" != "1" ]; then
  log "refusing: sparse sharded index tr_settle_outbox_due_v2 is not ready"
  exit 1
fi

unsharded="$(sql_value \
  "SELECT COUNT(*) FROM tr_settle_outbox
   WHERE status='pending' AND queue_shard IS NULL")"
if [ "${unsharded:-0}" != "0" ]; then
  log "refusing: ${unsharded} pending rows have no generated queue shard"
  exit 1
fi

# Exercise the exact sparse-index predicate before removing the rollback index.
sql_value \
  "SELECT COUNT(*) FROM tr_settle_outbox@{FORCE_INDEX=tr_settle_outbox_due_v2}
   WHERE queue_shard IS NOT NULL
     AND next_attempt_at IS NOT NULL
     AND status='pending'
     AND next_attempt_at <= CURRENT_TIMESTAMP()" >/dev/null

legacy_count="$(sql_value \
  "SELECT COUNT(*) FROM INFORMATION_SCHEMA.INDEXES
   WHERE table_name='tr_settle_outbox'
     AND index_name='tr_settle_outbox_due'")"
if [ "${legacy_count:-0}" = "0" ]; then
  log "legacy index already absent"
  exit 0
fi

log "dropping legacy unsharded index tr_settle_outbox_due"
gcloud spanner databases ddl update "$DATABASE" \
  --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
  --ddl="DROP INDEX tr_settle_outbox_due"
log "done"
