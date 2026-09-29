#!/usr/bin/env bash
# Read-only schema / enablement preflight; never installs schema.
set -euo pipefail
MODE="${1:---schema}"
case "$MODE" in
  --schema|--enablement) ;;
  *) echo "usage: $0 [--schema|--enablement]" >&2; exit 2 ;;
esac
INSTANCE="${SPANNER_INSTANCE_ID:?set SPANNER_INSTANCE_ID}"
DATABASE="${SPANNER_DATABASE_ID:?set SPANNER_DATABASE_ID}"
PROJECT_ARG=()
[ -n "${GCP_PROJECT_ID:-}" ] && PROJECT_ARG=(--project "${GCP_PROJECT_ID}")
gcloud spanner databases execute-sql "$DATABASE" \
  --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
  --sql="SELECT workspace_id, epoch, region, cap, shards, slots, state, recorded_bound,
    successor_epoch, retention_deadline, created_at, updated_at
    FROM tr_async_settlement_budget WHERE FALSE" --format=none
gcloud spanner databases execute-sql "$DATABASE" \
  --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
  --sql="SELECT authorization_id, workspace_id, epoch, region, shard, slot, generation_id,
    key_id, invocation_nonce, snapshot_hash, snapshot_version, idempotency_deadline,
    state, payload_hash, amount, ledger_receipt, created_at, updated_at
    FROM tr_async_settlement_obligation WHERE FALSE" --format=none
state=$(gcloud spanner databases execute-sql "$DATABASE" \
  --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
  --sql="SELECT INDEX_STATE FROM INFORMATION_SCHEMA.INDEXES
    WHERE TABLE_NAME='tr_async_settlement_obligation'
      AND INDEX_NAME='tr_async_obligation_by_slot' AND IS_UNIQUE=TRUE" \
  --format='value(rows[0])')
[ "$state" = "READ_WRITE" ] || { echo "async slot index is not READ_WRITE" >&2; exit 1; }
if [ "$MODE" = "--enablement" ]; then
  # Start the wait AFTER confirming the migration, so every pre-migration
  # negative cache has expired before tooling can enable async admission.
  # Keep in sync with OBLIGATION_ABSENT_CACHE_SECONDS (five seconds).
  sleep 5
  echo "async settlement enablement ready: schema verified and availability TTL elapsed"
else
  echo "async settlement schema ready (enablement also requires one TTL or fresh processes)"
fi
