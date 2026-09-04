#!/usr/bin/env bash
# Apply the typed-counter DDL (Step 1 of the billing typed-column migration).
# See docs/design/billing-typed-counters.md.
#
# Idempotent: checks INFORMATION_SCHEMA and only creates objects that are
# missing, so it is safe to re-run and safe on both fresh and existing
# databases. These tables are required by typed billing and creation-time
# credit/api-key row seeding.
#
# Operational sequencing: apply this only when no Cloud Run deploy is rolling
# and prefer a low-traffic window. Spanner schema changes wound in-flight
# read-write transactions at schema-version boundaries, and overlapping a
# rollout doubles the churn (receipt: 2026-07-04 21:25-21:31 UTC Aborted burst
# on gateway authorize). Expect a brief blip even when sequenced correctly.
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
# Needed because creation-time seeding writes the COMMIT_TIMESTAMP sentinel into
# source_updated_at; without the option the first seed write fails the txn.
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
    trust_tier INT64 DEFAULT (0),
    trust_computed_at TIMESTAMP,
    trust_latched_at TIMESTAMP,
    trust_override_tier INT64,
    billing_pause_causes ARRAY<STRING(32)>,
    pause_epoch INT64 DEFAULT (0),
    trust_reconciled_through TIMESTAMP,
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
    day_limit_micro INT64,
    week_limit_micro INT64,
    month_limit_micro INT64,
    day_usage INT64 NOT NULL DEFAULT (0),
    day_start TIMESTAMP,
    week_usage INT64 NOT NULL DEFAULT (0),
    week_start TIMESTAMP,
    month_usage INT64 NOT NULL DEFAULT (0),
    month_start TIMESTAMP,
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
    credit_shard INT64 NOT NULL DEFAULT (0),
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
    terminal_at TIMESTAMP,
  ) PRIMARY KEY (reservation_id)"
fi

if index_exists tr_reservation_by_idemp; then log "tr_reservation_by_idemp exists, skip"; else
  apply_ddl "CREATE UNIQUE NULL_FILTERED INDEX tr_reservation_by_idemp
    ON tr_reservation (idempotency_scope)"
fi

if index_exists tr_reservation_by_expiry; then log "tr_reservation_by_expiry exists, skip"; else
  apply_ddl "CREATE INDEX tr_reservation_by_expiry ON tr_reservation (settled, expires_at)"
fi

# ── Per-key window spend limits (daily/weekly/monthly) ──────────────────────
# Config columns (*_limit_micro) are seeded from the api_key row on create; the
# window usage/state columns are typed-DML-owned and bumped lazily by release_key.
column_exists() {
  local table="$1" col="$2" n
  n=$(gcloud spanner databases execute-sql "$DATABASE" \
        --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
        --sql="SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
               WHERE table_name='${table}' AND column_name='${col}'" \
        --format='value(rows[0])' 2>/dev/null || echo 0)
  [ "${n:-0}" != "0" ]
}

ensure_column() {
  local table="$1" col="$2" ddl="$3"
  if column_exists "$table" "$col"; then
    log "${table}.${col} exists, skip"
  else
    apply_ddl "ALTER TABLE ${table} ADD COLUMN ${col} ${ddl}"
  fi
}

wait_generated_column_committed() {
  local table="$1" col="$2" state=""
  for _ in $(seq 1 360); do
    state=$(gcloud spanner databases execute-sql "$DATABASE" \
      --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
      --sql="SELECT SPANNER_STATE FROM INFORMATION_SCHEMA.COLUMNS
             WHERE table_name='${table}' AND column_name='${col}'" \
      --format='value(rows[0])' 2>/dev/null || true)
    if [ "$state" = "COMMITTED" ]; then
      log "${table}.${col} is committed"
      return 0
    fi
    log "waiting for ${table}.${col} backfill (state=${state:-unknown})"
    sleep 5
  done
  log "timed out waiting for ${table}.${col} to become committed"
  return 1
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

# Converged trust-tier facts. These additions are nullable/defaulted so the DDL
# does not rewrite existing balance rows. The explicit backfill below is safe
# to run separately after trust-unaware revisions have drained.
ensure_column tr_credit_balance trust_tier "INT64 DEFAULT (0)"
ensure_column tr_credit_balance trust_computed_at "TIMESTAMP"
ensure_column tr_credit_balance trust_latched_at "TIMESTAMP"
ensure_column tr_credit_balance trust_override_tier "INT64"
ensure_column tr_credit_balance billing_pause_causes "ARRAY<STRING(32)>"
ensure_column tr_credit_balance pause_epoch "INT64 DEFAULT (0)"
ensure_column tr_credit_balance trust_reconciled_through "TIMESTAMP"

if table_exists tr_trust_event; then log "tr_trust_event exists, skip"; else
  apply_ddl "CREATE TABLE tr_trust_event (
    workspace_id STRING(64) NOT NULL,
    event_id STRING(255) NOT NULL,
    kind STRING(16) NOT NULL,
    provider STRING(16) NOT NULL,
    amount_micro INT64,
    original_payment_ref STRING(255),
    adverse_ref STRING(255),
    occurred_at TIMESTAMP NOT NULL,
    recorded_at TIMESTAMP NOT NULL,
    payment_amount_micro INT64,
    currency STRING(8),
    credited_micro INT64,
    recovered_micro INT64,
    provider_subtype STRING(64),
    lifecycle_status STRING(32),
    cumulative_refunded INT64,
    recovery_target INT64,
    debit_status STRING(16),
    unrecovered_micro INT64,
    provider_ordering_watermark STRING(255),
    CONSTRAINT tr_trust_event_kind CHECK (kind IN ('payment','refund','dispute','abuse','grant')),
    CONSTRAINT tr_trust_event_provider CHECK (provider IN ('stripe','paypal','adyen','x402','operator','system')),
    CONSTRAINT tr_trust_event_lifecycle CHECK (lifecycle_status IS NULL OR lifecycle_status IN ('pending','succeeded','failed','reversed','won','lost','closed','terminal_by_horizon')),
    CONSTRAINT tr_trust_event_debit CHECK (debit_status IS NULL OR debit_status IN ('debited','partial','unrecovered')),
  ) PRIMARY KEY (workspace_id, event_id)"
fi

if index_exists tr_trust_event_adverse_dedup; then
  log "tr_trust_event_adverse_dedup exists, skip"
else
  apply_ddl "CREATE UNIQUE NULL_FILTERED INDEX tr_trust_event_adverse_dedup
    ON tr_trust_event (provider, adverse_ref, kind)"
fi
if index_exists tr_trust_event_payment_dedup; then
  log "tr_trust_event_payment_dedup exists, skip"
else
  apply_ddl "CREATE UNIQUE NULL_FILTERED INDEX tr_trust_event_payment_dedup
    ON tr_trust_event (provider, original_payment_ref, kind)"
fi

# Historical trust-column backfill statement. It is intentionally a separate,
# operator-run artifact rather than an automatic deploy-time DML rewrite.
log "historical trust-column backfill: scripts/deploy/backfill_credit_balance_trust.sql"

# NOTE: Spanner forbids ADD COLUMN ... NOT NULL on an existing table, so the
# usage columns are added NULLABLE with DEFAULT (0) (future writes default; old
# rows read NULL until first touched — all readers COALESCE/None-guard). The
# fresh CREATE TABLE above keeps them NOT NULL.
ensure_column tr_key_limit day_limit_micro   "INT64"
ensure_column tr_key_limit week_limit_micro  "INT64"
ensure_column tr_key_limit month_limit_micro "INT64"
ensure_column tr_key_limit day_usage   "INT64 DEFAULT (0)"
ensure_column tr_key_limit day_start   "TIMESTAMP"
ensure_column tr_key_limit week_usage  "INT64 DEFAULT (0)"
ensure_column tr_key_limit week_start  "TIMESTAMP"
ensure_column tr_key_limit month_usage "INT64 DEFAULT (0)"
ensure_column tr_key_limit month_start "TIMESTAMP"

# Credit-row sharding increment 1. Existing reservations retain ws_shard=0;
# old rows read NULL for the additive column and the application falls back to
# ws_shard. New rows write both during the rolling-compatibility window.
# Existing databases intentionally add this as nullable: adding NOT NULL would
# force a synchronous backfill. Fresh installs use NOT NULL DEFAULT(0); a later
# post-backfill migration may tighten existing databases after the fallback is
# retired. Do not "fix" this rolling-schema divergence in this additive step.
ensure_column tr_reservation credit_shard "INT64 DEFAULT (0)"

# ── Durable settle outbox (docs/design/durable-settle-outbox.md) ────────────
# Recovers completed-but-settle-lost charges. Additive + guarded; safe to apply
# before the code reads it (the mechanism is gated off by settle_outbox_enabled).
# PK is (authorization_id, intent_kind) so a settle and a refund on one
# authorization never clobber each other. Frozen settle inputs (actual_cost_micro,
# selected_endpoint_id, model_id, selected_usage_type, settle_origin,
# reservation_id) are captured at enqueue so a drain replays a deterministic
# amount+origin regardless of any later pricing or serving-env change.
if table_exists tr_settle_outbox; then log "tr_settle_outbox exists, skip"; else
  apply_ddl "CREATE TABLE tr_settle_outbox (
    authorization_id STRING(64) NOT NULL,
    intent_kind STRING(16) NOT NULL,
    settle_origin STRING(16) NOT NULL,
    reservation_id STRING(64),
    actual_cost_micro INT64 NOT NULL,
    selected_endpoint_id STRING(128),
    model_id STRING(128),
    selected_usage_type STRING(16),
    settle_body STRING(MAX),
    status STRING(24) NOT NULL DEFAULT ('pending'),
    attempts INT64 NOT NULL DEFAULT (0),
    last_error STRING(MAX),
    next_attempt_at TIMESTAMP,
    lease_owner STRING(64),
    leased_until TIMESTAMP,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    terminal_at TIMESTAMP,
    auto_refill_workspace_id STRING(64),
    auto_refill_status STRING(24),
    auto_refill_attempts INT64 NOT NULL DEFAULT (0),
    auto_refill_last_error STRING(MAX),
    auto_refill_next_attempt_at TIMESTAMP,
    auto_refill_lease_owner STRING(64),
    auto_refill_leased_until TIMESTAMP,
    auto_refill_enqueued_at TIMESTAMP,
    auto_refill_updated_at TIMESTAMP,
    auto_refill_terminal_at TIMESTAMP,
    queue_shard INT64 NOT NULL AS (
      MOD(
        MOD(FARM_FINGERPRINT(CONCAT(authorization_id, '#', intent_kind)), 16) + 16,
        16
      )
    ) STORED,
  ) PRIMARY KEY (authorization_id, intent_kind)"
fi

# Generate the queue shard from immutable PK columns. Old application revisions
# omit this column on INSERT and are still rolling-safe because Spanner computes
# it. Existing rows are backfilled by the schema operation before the new index
# is created.
ensure_column tr_settle_outbox queue_shard \
  "INT64 NOT NULL AS (
    MOD(
      MOD(FARM_FINGERPRINT(CONCAT(authorization_id, '#', intent_kind)), 16) + 16,
      16
    )
  ) STORED"
wait_generated_column_committed tr_settle_outbox queue_shard

# Sparse, write-distributed due index. Terminal rows have next_attempt_at=NULL,
# so NULL_FILTERED keeps the completed history out of this index. The generated
# shard precedes the monotonic timestamp to avoid a moving-edge write hotspot.
if index_exists tr_settle_outbox_due_v2; then log "tr_settle_outbox_due_v2 exists, skip"; else
  apply_ddl "CREATE NULL_FILTERED INDEX tr_settle_outbox_due_v2
    ON tr_settle_outbox (queue_shard, next_attempt_at)"
fi
wait_index_read_write tr_settle_outbox_due_v2

# Control-owned auto-refill delivery. The successful internal credit settle
# attaches this sub-state to the same durable row it already has to enqueue;
# Stripe remains absent from the machine-to-machine service. Additive columns
# keep old revisions rolling-compatible, and the sparse index contains only
# refill work that still needs a control worker.
ensure_column tr_settle_outbox auto_refill_workspace_id "STRING(64)"
ensure_column tr_settle_outbox auto_refill_status "STRING(24)"
ensure_column tr_settle_outbox auto_refill_attempts "INT64 NOT NULL DEFAULT (0)"
ensure_column tr_settle_outbox auto_refill_last_error "STRING(MAX)"
ensure_column tr_settle_outbox auto_refill_next_attempt_at "TIMESTAMP"
ensure_column tr_settle_outbox auto_refill_lease_owner "STRING(64)"
ensure_column tr_settle_outbox auto_refill_leased_until "TIMESTAMP"
ensure_column tr_settle_outbox auto_refill_enqueued_at "TIMESTAMP"
ensure_column tr_settle_outbox auto_refill_updated_at "TIMESTAMP"
ensure_column tr_settle_outbox auto_refill_terminal_at "TIMESTAMP"
if index_exists tr_settle_outbox_auto_refill_due; then
  log "tr_settle_outbox_auto_refill_due exists, skip"
else
  apply_ddl "CREATE NULL_FILTERED INDEX tr_settle_outbox_auto_refill_due
    ON tr_settle_outbox (queue_shard, auto_refill_next_attempt_at)"
fi
wait_index_read_write tr_settle_outbox_auto_refill_due

log "done"
