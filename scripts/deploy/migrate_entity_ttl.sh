#!/usr/bin/env bash
# Give rate_limit tr_entities rows a real expiry (issue #334, Problem 2).
#
# Mechanism: a STORED generated column extracts the unix-seconds window reset
# epoch that rate_limit bodies carry in `expires_at`, and a ROW DELETION POLICY
# deletes rows one day after that expiry. Generated-column TTL is the
# documented Spanner pattern for a derived expiration.
#
# The column is DOUBLY scoped, and both guards are load-bearing:
#   1. `kind = 'rate_limit'` — no other kind can EVER opt in, castable value or
#      not. Opting in a future kind requires changing this DDL on purpose.
#   2. JSON_QUERY, not JSON_VALUE — JSON_VALUE strips quotes, so a customer
#      string like "20270101" (a valid ISO-basic date some writers accept)
#      would cast to unix-seconds 1970 and make the row TTL-eligible.
#      JSON_QUERY preserves quotes, so only a bare JSON NUMBER casts; any
#      quoted string yields NULL, and Spanner TTL never deletes a
#      NULL-timestamp row. Verified against prod's SQL engine 2026-08-01.
#   SAFE.TIMESTAMP_SECONDS additionally turns a pathological out-of-range
#   epoch into NULL (row simply never expires) instead of failing writes.
#
# Idempotent: INFORMATION_SCHEMA-guarded (scoped ROW_DELETION_POLICY_EXPRESSION
# check, same pattern as migrate_request_retention.sh), safe to re-run. A
# conflicting pre-existing policy on tr_entities aborts loudly rather than
# stacking (Spanner allows one policy per table).
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

sql_value() {
  gcloud spanner databases execute-sql "$DATABASE" \
    --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
    --sql="$1" --format='value(rows[0])' 2>/dev/null || echo ""
}

column_exists() {
  local n
  n=$(sql_value "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE table_name='tr_entities' AND column_name='ephemeral_expires_at'")
  [ "${n:-0}" != "0" ] && [ -n "${n}" ]
}

column_expression() {
  sql_value "SELECT COALESCE(GENERATION_EXPRESSION, '') FROM INFORMATION_SCHEMA.COLUMNS WHERE table_name='tr_entities' AND column_name='ephemeral_expires_at'"
}

policy_expression() {
  sql_value "SELECT COALESCE(ROW_DELETION_POLICY_EXPRESSION, '') FROM INFORMATION_SCHEMA.TABLES WHERE table_name='tr_entities'"
}

apply_ddl() {
  local ddl="$1"
  log "applying: ${ddl:0:60}..."
  gcloud spanner databases ddl update "$DATABASE" \
    --instance="$INSTANCE" "${PROJECT_ARG[@]}" --ddl="$ddl"
}

if column_exists; then
  # "Column exists" is NOT enough: a stored generated-column expression cannot
  # be altered in place, and attaching the policy to a column with a DIFFERENT
  # expression (e.g. an earlier unscoped draft) would re-open the string-cast
  # hole. Require BOTH safety discriminators to be present in the actual
  # deployed expression; abort loudly otherwise so a human drops/rebuilds.
  expr="$(column_expression)"
  if printf '%s' "$expr" | grep -q "kind = 'rate_limit'" \
     && printf '%s' "$expr" | grep -q "JSON_QUERY"; then
    log "ephemeral_expires_at exists with the kind-scoped JSON_QUERY expression, skip"
  else
    log "ERROR: ephemeral_expires_at exists with an UNEXPECTED expression:"
    log "  ${expr:-<empty / not a generated column>}"
    log "Stored generated expressions cannot be altered in place. Drop the"
    log "policy and column manually, then re-run:"
    log "  ALTER TABLE tr_entities DROP ROW DELETION POLICY;"
    log "  ALTER TABLE tr_entities DROP COLUMN ephemeral_expires_at;"
    exit 1
  fi
else
  apply_ddl "ALTER TABLE tr_entities ADD COLUMN ephemeral_expires_at TIMESTAMP AS (CASE WHEN kind = 'rate_limit' THEN SAFE.TIMESTAMP_SECONDS(SAFE_CAST(JSON_QUERY(body, '\$.expires_at') AS INT64)) END) STORED"
fi

policy="$(policy_expression)"
if printf '%s' "$policy" | grep -q "OLDER_THAN(ephemeral_expires_at, INTERVAL 1 DAY)"; then
  log "row deletion policy exists with the expected interval, skip"
elif [ -n "$policy" ]; then
  log "ERROR: tr_entities already has a DIFFERENT row deletion policy: $policy"
  log "Spanner allows one policy per table; refusing to replace it implicitly."
  exit 1
else
  apply_ddl "ALTER TABLE tr_entities ADD ROW DELETION POLICY (OLDER_THAN(ephemeral_expires_at, INTERVAL 1 DAY))"
fi

log "verify: non-NULL policy timestamps by kind (must be rate_limit ONLY)"
gcloud spanner databases execute-sql "$DATABASE" \
  --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
  --sql="SELECT kind, COUNT(*) AS opted_in FROM tr_entities WHERE ephemeral_expires_at IS NOT NULL GROUP BY kind"

log "verify: expired-but-undeleted rate_limit rows (drops to ~0 within 72h)"
gcloud spanner databases execute-sql "$DATABASE" \
  --instance="$INSTANCE" "${PROJECT_ARG[@]}" \
  --sql="SELECT COUNT(*) FROM tr_entities WHERE kind='rate_limit' AND ephemeral_expires_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 DAY)"
