#!/usr/bin/env bash
# Give ephemeral tr_entities kinds a real expiry (issue #334, Problem 2).
#
# Mechanism: a STORED generated column extracts the numeric unix-seconds
# `expires_at` that rate_limit bodies already carry (their window reset epoch),
# and a ROW DELETION POLICY deletes rows one day after that expiry. This is the
# documented Spanner pattern for TTL on a derived timestamp.
#
# Why this is safe for every other kind: the policy column is NULL unless the
# body carries a NUMERIC expires_at. Only rate_limit writes one (an INT64
# reset epoch); api_key / auth_session / wallet_challenge carry ISO-8601
# STRINGS, which SAFE_CAST nulls out, and Spanner's TTL never deletes a row
# whose policy timestamp is NULL (the same NULL-exemption semantics the
# tr_reservation retention backfill relied on). Audited in prod 2026-08-01:
# 386,574/386,574 rate_limit rows numeric; zero numeric rows in any other kind.
#
# TRAP for future kinds: writing a NUMERIC `expires_at` into an entity body
# OPTS THAT ROW INTO DELETION one day after the epoch it names. That is the
# intended contract — do it on purpose or use a different field name.
#
# Idempotent: INFORMATION_SCHEMA-guarded, safe to re-run.
#
# Operational sequencing: apply only when no Cloud Run deploy is rolling and
# prefer a low-traffic window (receipt: 2026-07-04 Aborted burst). The STORED
# column backfills ~9.4M rows as a background schema operation; expect the DDL
# to run for several minutes. Deletion of expired rows starts on the next TTL
# background sweep (daily; deletions typically complete within 72h).
#
# Usage:
#   SPANNER_INSTANCE_ID=... SPANNER_DATABASE_ID=... [GCP_PROJECT_ID=...] \
#     scripts/deploy/migrate_entity_ttl.sh
set -euo pipefail

INSTANCE="${SPANNER_INSTANCE_ID:?set SPANNER_INSTANCE_ID}"
DATABASE="${SPANNER_DATABASE_ID:?set SPANNER_DATABASE_ID}"
PROJECT_ARG=()
[ -n "${GCP_PROJECT_ID:-}" ] && PROJECT_ARG=(--project "${GCP_PROJECT_ID}")

log() { printf '%s %s\n' "[migrate_entity_ttl]" "$*"; }

column_exists() {
  local n
  n=$(gcloud spanner databases execute-sql "$DATABASE" \
        --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
        --sql="SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE table_name='tr_entities' AND column_name='ephemeral_expires_at'" \
        --format='value(rows[0])' 2>/dev/null || echo 0)
  [ "${n:-0}" != "0" ]
}

policy_exists() {
  local ddl
  ddl=$(gcloud spanner databases ddl describe "$DATABASE" \
          --instance="$INSTANCE" "${PROJECT_ARG[@]}" 2>/dev/null || true)
  printf '%s' "$ddl" | grep -q "OLDER_THAN(ephemeral_expires_at"
}

apply_ddl() {
  local ddl="$1"
  log "applying: ${ddl%%(*}..."
  gcloud spanner databases ddl update "$DATABASE" \
    --instance="$INSTANCE" "${PROJECT_ARG[@]}" --ddl="$ddl"
}

if column_exists; then
  log "ephemeral_expires_at exists, skip"
else
  apply_ddl "ALTER TABLE tr_entities ADD COLUMN ephemeral_expires_at TIMESTAMP AS (TIMESTAMP_SECONDS(SAFE_CAST(JSON_VALUE(body, '\$.expires_at') AS INT64))) STORED"
fi

if policy_exists; then
  log "row deletion policy exists, skip"
else
  apply_ddl "ALTER TABLE tr_entities ADD ROW DELETION POLICY (OLDER_THAN(ephemeral_expires_at, INTERVAL 1 DAY))"
fi

log "verify: expired-but-undeleted rate_limit rows (drops to ~0 within 72h)"
gcloud spanner databases execute-sql "$DATABASE" \
  --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
  --sql="SELECT COUNT(*) FROM tr_entities WHERE kind='rate_limit' AND ephemeral_expires_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 DAY)"
