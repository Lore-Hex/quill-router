#!/usr/bin/env bash
# Stripe/x402 trust-backfill marker schema. Safe to re-run.
#
# Marker identity is the five-column key (provider, account_id, environment,
# source, source_version). Production still carries slice 1c's three-column
# table (provider, account_id, environment): a plain table_exists guard skips
# the five-column DDL and the full marker can never land. Spanner cannot alter
# a primary key, so the three-column table is recreated -- but only while it
# holds nothing except owner_inventory rows (zero rows in production today).
# Any other row is real reconciliation state and this script refuses to touch
# it: resolve by hand (runbook step 2b) rather than drop history.
set -euo pipefail

INSTANCE="${SPANNER_INSTANCE_ID:?set SPANNER_INSTANCE_ID}"
DATABASE="${SPANNER_DATABASE_ID:?set SPANNER_DATABASE_ID}"
PROJECT_ARG=()
[ -n "${GCP_PROJECT_ID:-}" ] && PROJECT_ARG=(--project "${GCP_PROJECT_ID}")

sql_scalar() {
  gcloud spanner databases execute-sql "$DATABASE" \
    --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
    --sql="$1" \
    --format='value(rows[0])' 2>/dev/null || echo "$2"
}

table_exists() {
  local name="$1" count
  count=$(sql_scalar "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE table_name='${name}'" 0)
  [ "${count:-0}" != "0" ]
}

# Number of primary-key columns on the live table; 3 is slice 1c's shape, 5 is
# the marker identity every reader and writer on main uses.
marker_key_columns() {
  sql_scalar "SELECT COUNT(*) FROM INFORMATION_SCHEMA.INDEX_COLUMNS WHERE TABLE_NAME='tr_trust_backfill' AND INDEX_TYPE='PRIMARY_KEY'" ""
}

# Rows that are not owner_inventory markers. Anything here is reconciliation
# state we must not drop.
marker_foreign_rows() {
  sql_scalar "SELECT COUNT(*) FROM tr_trust_backfill WHERE provider <> 'owner_inventory'" ""
}

apply_ddl() {
  gcloud spanner databases ddl update "$DATABASE" \
    --instance="$INSTANCE" "${PROJECT_ARG[@]}" --ddl="$1"
}

# The single CREATE TABLE literal: both the fresh-install branch and the
# recreate branch apply exactly this text, so the tests that pin the marker
# DDL see one definition.
MARKER_DDL="CREATE TABLE tr_trust_backfill (
      provider STRING(16) NOT NULL,
      account_id STRING(255) NOT NULL,
      environment STRING(32) NOT NULL,
      source STRING(64) NOT NULL,
      source_version STRING(64) NOT NULL,
      history_start TIMESTAMP NOT NULL,
      closed_through TIMESTAMP NOT NULL,
      consistency_delay_seconds INT64 NOT NULL,
      unmatched_count INT64 NOT NULL,
      semantic_mismatch_count INT64 NOT NULL,
      completed_at TIMESTAMP,
      CONSTRAINT tr_trust_backfill_counts CHECK (
        consistency_delay_seconds >= 0 AND unmatched_count >= 0
        AND semantic_mismatch_count >= 0
      ),
      CONSTRAINT tr_trust_backfill_completion CHECK (
        completed_at IS NULL OR (unmatched_count = 0 AND semantic_mismatch_count = 0)
      ),
    ) PRIMARY KEY (provider, account_id, environment, source, source_version)"

if table_exists tr_trust_backfill; then
  key_columns="$(marker_key_columns)"
  case "$key_columns" in
    5)
      printf '%s\n' "tr_trust_backfill exists with the five-column marker key, skip"
      ;;
    3)
      foreign_rows="$(marker_foreign_rows)"
      if [ -z "$foreign_rows" ]; then
        printf '%s\n' "ERROR: cannot count rows in the three-column tr_trust_backfill; refusing to recreate" >&2
        exit 1
      fi
      if [ "$foreign_rows" != "0" ]; then
        printf '%s\n' "ERROR: tr_trust_backfill has the three-column key and ${foreign_rows} non-owner_inventory row(s); refusing to drop reconciliation state (runbook step 2b)" >&2
        exit 1
      fi
      printf '%s\n' "tr_trust_backfill has slice 1c's three-column key and only owner_inventory rows; recreating with the five-column marker key"
      printf '%s\n' "NOTE: owner_inventory markers are dropped with the table; re-run the owner inventory backfill (trusted_router.owner_inventory_cli) afterwards"
      apply_ddl "DROP TABLE tr_trust_backfill"
      apply_ddl "$MARKER_DDL"
      ;;
    "")
      printf '%s\n' "ERROR: cannot read the tr_trust_backfill primary key from INFORMATION_SCHEMA.INDEX_COLUMNS; refusing to guess" >&2
      exit 1
      ;;
    *)
      printf '%s\n' "ERROR: tr_trust_backfill has an unexpected ${key_columns}-column primary key; refusing to touch it" >&2
      exit 1
      ;;
  esac
else
  apply_ddl "$MARKER_DDL"
fi
