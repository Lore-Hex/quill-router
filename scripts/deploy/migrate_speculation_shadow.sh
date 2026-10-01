#!/usr/bin/env bash
# Additive shadow evidence only. REQUIRED BEFORE ENABLE, not before off deployment.
# Run outside a rolling deploy. No customer data backfill or money mutation.
set -euo pipefail
INSTANCE="${SPANNER_INSTANCE_ID:?set SPANNER_INSTANCE_ID}"
DATABASE="${SPANNER_DATABASE_ID:?set SPANNER_DATABASE_ID}"
PROJECT_ARG=()
[ -n "${GCP_PROJECT_ID:-}" ] && PROJECT_ARG=(--project "${GCP_PROJECT_ID}")
table_exists() {
  local name="$1" n
  n=$(gcloud spanner databases execute-sql "$DATABASE" --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
      --sql="SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE table_name='${name}'" --format='value(rows[0])')
  [ "${n:-0}" != "0" ]
}
apply_ddl() {
  gcloud spanner databases ddl update "$DATABASE" --instance="$INSTANCE" "${PROJECT_ARG[@]}" --ddl="$1"
}

if ! table_exists tr_speculation_shadow_event; then
  apply_ddl "CREATE TABLE tr_speculation_shadow_event (
    plane STRING(32) NOT NULL,
    producer_incarnation STRING(128) NOT NULL,
    sequence INT64 NOT NULL,
    body STRING(MAX) NOT NULL,
    updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)
  ) PRIMARY KEY (plane, producer_incarnation, sequence)"
fi

if ! table_exists tr_speculation_shadow_success; then
  apply_ddl "CREATE TABLE tr_speculation_shadow_success (
    plane STRING(32) NOT NULL,
    identity STRING(128) NOT NULL,
    body STRING(MAX) NOT NULL,
    updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)
  ) PRIMARY KEY (plane, identity)"
fi

if ! table_exists tr_speculation_shadow_scope; then
  apply_ddl "CREATE TABLE tr_speculation_shadow_scope (
    plane STRING(32) NOT NULL,
    identity STRING(128) NOT NULL,
    body STRING(MAX) NOT NULL,
    updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)
  ) PRIMARY KEY (plane, identity)"
fi

if ! table_exists tr_speculation_shadow_producer; then
  apply_ddl "CREATE TABLE tr_speculation_shadow_producer (
    plane STRING(32) NOT NULL,
    identity STRING(128) NOT NULL,
    body STRING(MAX) NOT NULL,
    updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)
  ) PRIMARY KEY (plane, identity)"
fi

if ! table_exists tr_speculation_shadow_paid; then
  apply_ddl "CREATE TABLE tr_speculation_shadow_paid (
    plane STRING(32) NOT NULL,
    identity STRING(128) NOT NULL,
    body STRING(MAX) NOT NULL,
    updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)
  ) PRIMARY KEY (plane, identity)"
fi

if ! table_exists tr_speculation_shadow_route; then
  apply_ddl "CREATE TABLE tr_speculation_shadow_route (
    plane STRING(32) NOT NULL,
    identity STRING(128) NOT NULL,
    body STRING(MAX) NOT NULL,
    updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)
  ) PRIMARY KEY (plane, identity)"
fi

if ! table_exists tr_speculation_shadow_grant; then
  apply_ddl "CREATE TABLE tr_speculation_shadow_grant (
    plane STRING(32) NOT NULL,
    identity STRING(128) NOT NULL,
    body STRING(MAX) NOT NULL,
    updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)
  ) PRIMARY KEY (plane, identity)"
fi

if ! table_exists tr_speculation_shadow_exposure; then
  apply_ddl "CREATE TABLE tr_speculation_shadow_exposure (
    plane STRING(32) NOT NULL,
    identity STRING(128) NOT NULL,
    body STRING(MAX) NOT NULL,
    updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true)
  ) PRIMARY KEY (plane, identity)"
fi
