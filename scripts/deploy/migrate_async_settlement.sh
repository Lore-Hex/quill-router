#!/usr/bin/env bash
# Operator precondition: run before deploying any obligation guard readers.
set -euo pipefail

INSTANCE="${SPANNER_INSTANCE_ID:?set SPANNER_INSTANCE_ID}"
DATABASE="${SPANNER_DATABASE_ID:?set SPANNER_DATABASE_ID}"
PROJECT_ARG=()
[ -n "${GCP_PROJECT_ID:-}" ] && PROJECT_ARG=(--project "${GCP_PROJECT_ID}")

log() { printf '%s %s\n' "[migrate_async_settlement]" "$*"; }

table_exists() {
  local name="$1"
  local n
  n=$(gcloud spanner databases execute-sql "$DATABASE" \
        --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
        --sql="SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE table_name='${name}'" \
        --format='value(rows[0])' 2>/dev/null || echo 0)
  [ "${n:-0}" != "0" ]
}

index_exists() {
  local name="$1"
  local n
  n=$(gcloud spanner databases execute-sql "$DATABASE" \
        --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
        --sql="SELECT COUNT(*) FROM INFORMATION_SCHEMA.INDEXES WHERE index_name='${name}'" \
        --format='value(rows[0])' 2>/dev/null || echo 0)
  [ "${n:-0}" != "0" ]
}

apply_ddl() {
  log "applying: $1"
  gcloud spanner databases ddl update "$DATABASE" \
    --instance="$INSTANCE" "${PROJECT_ARG[@]}" --ddl="$1"
}

if table_exists tr_async_settlement_budget; then log "budget exists, skip"; else
  apply_ddl "CREATE TABLE tr_async_settlement_budget (
    workspace_id STRING(256) NOT NULL,
    epoch STRING(256) NOT NULL,
    region STRING(256) NOT NULL,
    cap INT64 NOT NULL,
    shards INT64 NOT NULL,
    slots INT64 NOT NULL,
    state STRING(16) NOT NULL,
    recorded_bound INT64,
    successor_epoch STRING(256),
    retention_deadline INT64,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT async_budget_state CHECK (state IN ('active', 'retiring', 'closed')),
    CONSTRAINT async_budget_bound CHECK (recorded_bound IS NULL OR (recorded_bound >= 0 AND recorded_bound <= cap)),
  ) PRIMARY KEY (workspace_id, epoch)"
fi
if table_exists tr_async_settlement_obligation; then log "obligation exists, skip"; else
  apply_ddl "CREATE TABLE tr_async_settlement_obligation (
    authorization_id STRING(256) NOT NULL,
    workspace_id STRING(256) NOT NULL,
    epoch STRING(256) NOT NULL,
    region STRING(256) NOT NULL,
    shard INT64 NOT NULL,
    slot INT64 NOT NULL,
    generation_id STRING(256) NOT NULL,
    key_id STRING(256) NOT NULL,
    invocation_nonce STRING(256) NOT NULL,
    snapshot_hash STRING(64) NOT NULL,
    snapshot_version INT64 NOT NULL,
    idempotency_deadline INT64 NOT NULL,
    state STRING(16) NOT NULL,
    payload_hash STRING(64),
    amount INT64,
    ledger_receipt STRING(MAX),
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    CONSTRAINT async_obligation_state CHECK (state IN ('pending', 'accepted', 'sync_required', 'fenced', 'acknowledged')),
  ) PRIMARY KEY (authorization_id)"
fi
if index_exists tr_async_obligation_by_slot; then log "slot index exists, skip"; else
  apply_ddl "CREATE UNIQUE INDEX tr_async_obligation_by_slot
    ON tr_async_settlement_obligation (workspace_id, epoch, shard, slot)"
fi
