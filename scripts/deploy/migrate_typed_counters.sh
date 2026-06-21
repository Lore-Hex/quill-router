#!/usr/bin/env bash
# Apply the typed-counter DDL (Step 1 of the billing typed-column migration).
# See docs/design/billing-typed-counters.md.
#
# Idempotent: checks INFORMATION_SCHEMA and only creates objects that are
# missing, so it is safe to re-run and safe on both fresh and existing
# databases. Apply this BEFORE deploying the mirror code with
# TR_TYPED_COUNTER_MIRROR=1 — the dual-write writes to these tables.
#
# Usage:
#   SPANNER_INSTANCE_ID=... SPANNER_DATABASE_ID=... [GCP_PROJECT_ID=...] \
#     scripts/deploy/migrate_typed_counters.sh
set -euo pipefail

INSTANCE="${SPANNER_INSTANCE_ID:?set SPANNER_INSTANCE_ID}"
DATABASE="${SPANNER_DATABASE_ID:?set SPANNER_DATABASE_ID}"
PROJECT_ARG=()
[ -n "${GCP_PROJECT_ID:-}" ] && PROJECT_ARG=(--project "${GCP_PROJECT_ID}")

log() { printf '%s %s\n' "[migrate_typed_counters]" "$*"; }

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

# Idempotent guard: ensure a timestamp column carries allow_commit_timestamp=true.
# Needed because the mirror writes the COMMIT_TIMESTAMP sentinel into
# source_updated_at; without the option the first mirrored write fails the txn.
# Covers tables created by an earlier version of this script without the option.
ensure_commit_ts_col() {
  local table="$1" col="$2" n
  n=$(gcloud spanner databases execute-sql "$DATABASE" \
        --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
        --sql="SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMN_OPTIONS
               WHERE table_name='${table}' AND column_name='${col}'
                 AND option_name='allow_commit_timestamp' AND option_value='TRUE'" \
        --format='value(rows[0])' 2>/dev/null || echo 0)
  if [ "${n:-0}" = "0" ]; then
    apply_ddl "ALTER TABLE ${table} ALTER COLUMN ${col} SET OPTIONS (allow_commit_timestamp=true)"
  else
    log "${table}.${col} already has allow_commit_timestamp, skip"
  fi
}

# shard is in the PK from day one (DEFAULT 0): the long tail lives on shard 0;
# sharding a whale later is a data change, not a schema migration.
if table_exists tr_credit_balance; then log "tr_credit_balance exists, skip"; else
  apply_ddl "CREATE TABLE tr_credit_balance (
    workspace_id STRING(64) NOT NULL,
    shard INT64 NOT NULL DEFAULT (0),
    total_credits INT64 NOT NULL DEFAULT (0),
    total_usage INT64 NOT NULL DEFAULT (0),
    reserved INT64 NOT NULL DEFAULT (0),
    source_updated_at TIMESTAMP OPTIONS (allow_commit_timestamp=true),
    updated_at TIMESTAMP OPTIONS (allow_commit_timestamp=true),
  ) PRIMARY KEY (workspace_id, shard)"
fi

if table_exists tr_key_limit; then log "tr_key_limit exists, skip"; else
  apply_ddl "CREATE TABLE tr_key_limit (
    key_hash STRING(64) NOT NULL,
    shard INT64 NOT NULL DEFAULT (0),
    limit_micro INT64,
    usage INT64 NOT NULL DEFAULT (0),
    byok_usage INT64 NOT NULL DEFAULT (0),
    reserved INT64 NOT NULL DEFAULT (0),
    include_byok BOOL NOT NULL DEFAULT (true),
    source_updated_at TIMESTAMP OPTIONS (allow_commit_timestamp=true),
    updated_at TIMESTAMP OPTIONS (allow_commit_timestamp=true),
  ) PRIMARY KEY (key_hash, shard)"
fi

# Backfill the commit-timestamp option on source_updated_at for tables that may
# predate the option being added to the CREATE statements above.
ensure_commit_ts_col tr_credit_balance source_updated_at
ensure_commit_ts_col tr_key_limit source_updated_at

# tr_reservation + its indexes are used at the Step 3 enforcement flip; created
# now so the schema is in place ahead of cutover.
if table_exists tr_reservation; then log "tr_reservation exists, skip"; else
  apply_ddl "CREATE TABLE tr_reservation (
    reservation_id STRING(64) NOT NULL,
    workspace_id STRING(64),
    key_hash STRING(64),
    ws_shard INT64,
    key_shard INT64,
    credit_reserved_micro INT64,
    key_reserved_micro INT64,
    actual_micro INT64,
    hold_usage_type STRING(16),
    settled_usage_type STRING(16),
    authorization_id STRING(64),
    settled BOOL NOT NULL DEFAULT (false),
    idempotency_scope STRING(256),
    idempotency_fingerprint STRING(64),
    created_at TIMESTAMP OPTIONS (allow_commit_timestamp=true),
    expires_at TIMESTAMP,
  ) PRIMARY KEY (reservation_id)"
fi

if index_exists tr_reservation_by_idemp; then log "tr_reservation_by_idemp exists, skip"; else
  apply_ddl "CREATE UNIQUE NULL_FILTERED INDEX tr_reservation_by_idemp
    ON tr_reservation (idempotency_scope)"
fi

if index_exists tr_reservation_by_expiry; then log "tr_reservation_by_expiry exists, skip"; else
  apply_ddl "CREATE INDEX tr_reservation_by_expiry ON tr_reservation (settled, expires_at)"
fi

log "done"
