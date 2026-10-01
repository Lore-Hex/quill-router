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

# Seven days covers the one-day delivery bound plus the ten-minute history
# window. Never attach this policy to retained exposure or scope/producer state.
ensure_policy() {
  local table="$1" current normalized
  current=$(gcloud spanner databases execute-sql "$DATABASE" --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
    --sql="SELECT COALESCE(ROW_DELETION_POLICY_EXPRESSION, '') FROM INFORMATION_SCHEMA.TABLES WHERE table_name='${table}'" --format='value(rows[0])')
  normalized=$(printf '%s' "$current" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
  if [ -z "$normalized" ]; then
    apply_ddl "ALTER TABLE $table ADD ROW DELETION POLICY (OLDER_THAN(updated_at, INTERVAL 7 DAY))"
  elif [ "$normalized" != 'older_than(updated_at,interval7day)' ]; then
    printf 'Refusing unexpected retention policy for %s: %s\n' "$table" "$current" >&2
    exit 1
  fi
}
ensure_policy tr_speculation_shadow_event
ensure_policy tr_speculation_shadow_success
