#!/usr/bin/env bash
# Additive Stripe/x402 trust-backfill marker schema. Safe to re-run.
set -euo pipefail

INSTANCE="${SPANNER_INSTANCE_ID:?set SPANNER_INSTANCE_ID}"
DATABASE="${SPANNER_DATABASE_ID:?set SPANNER_DATABASE_ID}"
PROJECT_ARG=()
[ -n "${GCP_PROJECT_ID:-}" ] && PROJECT_ARG=(--project "${GCP_PROJECT_ID}")

table_exists() {
  local name="$1" count
  count=$(gcloud spanner databases execute-sql "$DATABASE" \
    --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
    --sql="SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE table_name='${name}'" \
    --format='value(rows[0])' 2>/dev/null || echo 0)
  [ "${count:-0}" != "0" ]
}

if table_exists tr_trust_backfill; then
  printf '%s\n' "tr_trust_backfill exists, skip"
else
  gcloud spanner databases ddl update "$DATABASE" \
    --instance="$INSTANCE" "${PROJECT_ARG[@]}" --ddl="CREATE TABLE tr_trust_backfill (
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
fi
