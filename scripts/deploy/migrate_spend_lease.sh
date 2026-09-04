#!/usr/bin/env bash
# Apply the inert Stage B spend-lease schema manifest (design decision 48).
#
# Idempotent: every column, table, and index is guarded by an
# INFORMATION_SCHEMA existence check. This migration adds schema only; no
# reader, writer, reconciler, Bigtable resource, or feature flag is enabled.
#
# Operational sequencing: apply this only when no Cloud Run deploy is rolling
# and prefer a low-traffic window. Spanner schema changes wound in-flight
# read-write transactions at schema-version boundaries, and overlapping a
# rollout doubles the churn.
#
# Usage:
#   SPANNER_INSTANCE_ID=... SPANNER_DATABASE_ID=... [GCP_PROJECT_ID=...] \
#     scripts/deploy/migrate_spend_lease.sh
set -euo pipefail

INSTANCE="${SPANNER_INSTANCE_ID:?set SPANNER_INSTANCE_ID}"
DATABASE="${SPANNER_DATABASE_ID:?set SPANNER_DATABASE_ID}"
PROJECT_ARG=()
[ -n "${GCP_PROJECT_ID:-}" ] && PROJECT_ARG=(--project "${GCP_PROJECT_ID}")

log() { printf '%s %s\n' "[migrate_spend_lease]" "$*"; }

table_exists() {
  local name="$1"
  local n
  n=$(gcloud spanner databases execute-sql "$DATABASE" \
        --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
        --sql="SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE table_name='${name}'" \
        --format='value(rows[0])' 2>/dev/null || echo 0)
  [ "${n:-0}" != "0" ]
}

column_exists() {
  local table="$1" col="$2" n
  n=$(gcloud spanner databases execute-sql "$DATABASE" \
        --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
        --sql="SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
               WHERE table_name='${table}' AND column_name='${col}'" \
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

ensure_column() {
  local table="$1" col="$2" ddl="$3"
  if column_exists "$table" "$col"; then
    log "${table}.${col}: already present"
  else
    apply_ddl "ALTER TABLE ${table} ADD COLUMN ${col} ${ddl}"
    log "${table}.${col}: created"
  fi
}

wait_index_read_write() {
  local name="$1" state=""
  for _ in $(seq 1 360); do
    state=$(gcloud spanner databases execute-sql "$DATABASE" \
      --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
      --sql="SELECT INDEX_STATE FROM INFORMATION_SCHEMA.INDEXES
             WHERE index_name='${name}'" \
      --format='value(rows[0])' 2>/dev/null || true)
    if [ "$state" = "READ_WRITE" ]; then
      log "${name} is read-write"
      return 0
    fi
    log "waiting for ${name} backfill (state=${state:-unknown})"
    sleep 5
  done
  log "timed out waiting for ${name} to become read-write"
  return 1
}

# Existing authorization rows must retain NULL, which is distinct from zero or
# an empty finalization result. Spanner also forbids adding NOT NULL columns to
# a populated table, so these additions deliberately have no backfill and no
# DEFAULT clauses.
ensure_column tr_gateway_authorization spend_lease_id \
  "STRING(64)"
ensure_column tr_gateway_authorization spend_lease_gen \
  "INT64"
ensure_column tr_gateway_authorization spend_lease_allocated_micro \
  "INT64"
ensure_column tr_gateway_authorization spend_lease_token \
  "STRING(MAX)"
ensure_column tr_gateway_authorization spend_lease_status \
  "STRING(16)"
ensure_column tr_gateway_authorization spend_lease_exp \
  "TIMESTAMP"
ensure_column tr_gateway_authorization idempotency_fingerprint \
  "STRING(64)"
ensure_column tr_gateway_authorization finalization_outcome \
  "STRING(32)"
ensure_column tr_gateway_authorization finalized_cost_microdollars \
  "INT64"
ensure_column tr_gateway_authorization spend_lease_admission_receipt \
  "STRING(MAX)"
ensure_column tr_gateway_authorization spend_lease_receipt_hash \
  "STRING(64)"
ensure_column tr_gateway_authorization started_at \
  "TIMESTAMP"
ensure_column tr_gateway_authorization heartbeat_seq \
  "INT64"
ensure_column tr_gateway_authorization heartbeat_at \
  "TIMESTAMP"
ensure_column tr_gateway_authorization heartbeat_hash \
  "STRING(64)"
ensure_column tr_gateway_authorization selected_endpoint_id \
  "STRING(128)"
ensure_column tr_gateway_authorization delivered_usage \
  "STRING(MAX)"
ensure_column tr_gateway_authorization pricing_snapshot \
  "STRING(MAX)"
ensure_column tr_gateway_authorization stage_d_boot_kid \
  "STRING(128)"
ensure_column tr_gateway_authorization invocation_nonce \
  "STRING(64)"
ensure_column tr_gateway_authorization gateway_request_id \
  "STRING(37)"

if index_exists tr_gateway_authorization_by_gateway_request_id; then
  log "tr_gateway_authorization_by_gateway_request_id: already present"
else
  apply_ddl "CREATE UNIQUE NULL_FILTERED INDEX tr_gateway_authorization_by_gateway_request_id
    ON tr_gateway_authorization (gateway_request_id)"
  log "tr_gateway_authorization_by_gateway_request_id: created"
fi
wait_index_read_write tr_gateway_authorization_by_gateway_request_id

if table_exists spend_lease_scope_arbitration; then
  log "spend_lease_scope_arbitration: already present"
else
  apply_ddl "CREATE TABLE spend_lease_scope_arbitration (
    scope_salt STRING(4) NOT NULL,
    idempotency_scope STRING(256) NOT NULL,
    registration_kind STRING(16) NOT NULL,
    authorization_id STRING(64),
    spend_lease_id STRING(64),
    spend_lease_gen INT64,
    spend_lease_allocated_micro INT64,
    provisional_id STRING(64),
    created_at TIMESTAMP NOT NULL,
    terminal_at TIMESTAMP,
    CONSTRAINT spend_lease_scope_arbitration_shape CHECK ((registration_kind = 'BOUND' AND authorization_id IS NOT NULL AND spend_lease_id IS NOT NULL AND spend_lease_gen IS NOT NULL AND spend_lease_allocated_micro IS NOT NULL AND provisional_id IS NULL) OR (registration_kind = 'CLAIM' AND provisional_id IS NOT NULL AND authorization_id IS NULL AND spend_lease_id IS NULL AND spend_lease_gen IS NULL AND spend_lease_allocated_micro IS NULL AND terminal_at IS NOT NULL)),
  ) PRIMARY KEY (scope_salt, idempotency_scope),
    ROW DELETION POLICY (OLDER_THAN(terminal_at, INTERVAL 30 DAY))"
  log "spend_lease_scope_arbitration: created"
fi

if index_exists spend_lease_scope_arbitration_by_authorization; then
  log "spend_lease_scope_arbitration_by_authorization: already present"
else
  apply_ddl "CREATE NULL_FILTERED INDEX spend_lease_scope_arbitration_by_authorization
    ON spend_lease_scope_arbitration (authorization_id)"
  log "spend_lease_scope_arbitration_by_authorization: created"
fi
wait_index_read_write spend_lease_scope_arbitration_by_authorization

# The immutable candidate identity is complete and unconditionally NOT NULL:
# gen, key_hash, boot_kid, cap_micro, skew_seconds, workspace_id, region,
# creating_authorization_id, idempotency_scope, and expires_at. Only mutable
# work/closure timestamps and error text may be absent. Rows are deleted
# explicitly after reconciliation; an age-based row deletion policy here could
# delete a live or quarantined lease.
if table_exists spend_lease_open; then
  log "spend_lease_open: already present"
else
  apply_ddl "CREATE TABLE spend_lease_open (
    lease_id STRING(64) NOT NULL,
    phase STRING(16) NOT NULL,
    gen INT64 NOT NULL,
    key_hash STRING(64) NOT NULL,
    boot_kid STRING(64) NOT NULL,
    cap_micro INT64 NOT NULL,
    skew_seconds INT64 NOT NULL,
    workspace_id STRING(64) NOT NULL,
    region STRING(32) NOT NULL,
    creating_authorization_id STRING(64) NOT NULL,
    idempotency_scope STRING(256) NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    next_attempt_at TIMESTAMP,
    attempts INT64 NOT NULL DEFAULT (0),
    last_error STRING(MAX),
    dead BOOL NOT NULL DEFAULT (false),
    close_eligible_since TIMESTAMP,
    global_closed_at TIMESTAMP,
    local_closed_at TIMESTAMP,
    recovering_at TIMESTAMP OPTIONS (allow_commit_timestamp = true),
    created_at TIMESTAMP NOT NULL,
    CONSTRAINT spend_lease_open_phase CHECK (phase IN ('candidate', 'recovering', 'open', 'done')),
  ) PRIMARY KEY (lease_id)"
  log "spend_lease_open: created"
fi

if index_exists spend_lease_open_due; then
  log "spend_lease_open_due: already present"
else
  apply_ddl "CREATE NULL_FILTERED INDEX spend_lease_open_due
    ON spend_lease_open (next_attempt_at)"
  log "spend_lease_open_due: created"
fi
wait_index_read_write spend_lease_open_due

log "done"
