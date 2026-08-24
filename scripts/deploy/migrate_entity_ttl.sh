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
# This is deliberately NOT part of the routine deployment workflow. Adding a
# STORED generated column backfills the entire legacy table and can consume
# enough Spanner system capacity to delay router-core transactions. The default
# mode is verification only. A schema change requires both --apply and the
# migration-specific acknowledgement below.
#
# Usage (verify only):
#   SPANNER_INSTANCE_ID=... SPANNER_DATABASE_ID=... [GCP_PROJECT_ID=...] \
#     scripts/deploy/migrate_entity_ttl.sh
#
# Usage (maintenance window only):
#   TR_HEAVY_DDL_ACK=tr_entities.ephemeral_expires_at \
#   SPANNER_INSTANCE_ID=... SPANNER_DATABASE_ID=... [GCP_PROJECT_ID=...] \
#     scripts/deploy/migrate_entity_ttl.sh --apply
set -euo pipefail

APPLY=0
case "${1:-}" in
  "") ;;
  --apply) APPLY=1 ;;
  *) echo "usage: $0 [--apply]" >&2; exit 2 ;;
esac

INSTANCE="${SPANNER_INSTANCE_ID:?set SPANNER_INSTANCE_ID}"
DATABASE="${SPANNER_DATABASE_ID:?set SPANNER_DATABASE_ID}"
# Bash-3.2-safe under set -u: expanding an EMPTY array with "${arr[@]}" aborts
# as "unbound variable" on macOS bash. The ${arr[@]+...} idiom expands to
# nothing when the array is empty and to the quoted elements otherwise.
PROJECT_ARG=()
[ -n "${GCP_PROJECT_ID:-}" ] && PROJECT_ARG=(--project "${GCP_PROJECT_ID}")

log() { printf '%s %s\n' "[migrate_entity_ttl]" "$*"; }

sql_value() {
  gcloud spanner databases execute-sql "$DATABASE" \
    --instance="$INSTANCE" ${PROJECT_ARG[@]+"${PROJECT_ARG[@]}"} \
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
  if [ "$APPLY" != "1" ]; then
    log "schema differs; verification mode will not run heavy DDL"
    log "re-run with --apply and TR_HEAVY_DDL_ACK=tr_entities.ephemeral_expires_at"
    exit 2
  fi
  if [ "${TR_HEAVY_DDL_ACK:-}" != "tr_entities.ephemeral_expires_at" ]; then
    log "refusing heavy DDL without the migration-specific acknowledgement"
    log "set TR_HEAVY_DDL_ACK=tr_entities.ephemeral_expires_at in a maintenance window"
    exit 2
  fi
  local active_operations
  if ! active_operations=$(gcloud spanner operations list \
    --instance="$INSTANCE" --database="$DATABASE" \
    ${PROJECT_ARG[@]+"${PROJECT_ARG[@]}"} \
    --filter='done=false' --format='value(name)'); then
    log "could not verify active Spanner operations; refusing heavy DDL"
    exit 1
  fi
  if [ -n "$active_operations" ]; then
    log "another Spanner operation is active; refusing concurrent heavy DDL"
    printf '%s\n' "$active_operations"
    exit 1
  fi
  log "applying: ${ddl:0:60}..."
  gcloud spanner databases ddl update "$DATABASE" \
    --instance="$INSTANCE" ${PROJECT_ARG[@]+"${PROJECT_ARG[@]}"} --ddl="$ddl"
}

if column_exists; then
  # "Column exists" is NOT enough: a stored generated-column expression cannot
  # be altered in place, and attaching the policy to a column with a DIFFERENT
  # expression (e.g. an earlier unscoped draft) would re-open the string-cast
  # hole. Require BOTH safety discriminators to be present in the actual
  # deployed expression; abort loudly otherwise so a human drops/rebuilds.
  expr="$(column_expression)"
  # Match the discriminators robustly: Spanner may normalize whitespace/case
  # when storing the expression, but the quoted literal 'rate_limit' and the
  # JSON_QUERY function name survive any reformatting. A false ABORT here is
  # safe-noisy; the by-kind verify query below backstops a false pass.
  if printf '%s' "$expr" | grep -q "'rate_limit'" \
     && printf '%s' "$expr" | grep -qi "json_query"; then
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
  --instance="$INSTANCE" ${PROJECT_ARG[@]+"${PROJECT_ARG[@]}"} \
  --sql="SELECT kind, COUNT(*) AS opted_in FROM tr_entities WHERE ephemeral_expires_at IS NOT NULL GROUP BY kind"

log "verify: expired-but-undeleted rate_limit rows (drops to ~0 within 72h)"
gcloud spanner databases execute-sql "$DATABASE" \
  --instance="$INSTANCE" ${PROJECT_ARG[@]+"${PROJECT_ARG[@]}"} \
  --sql="SELECT COUNT(*) FROM tr_entities WHERE kind='rate_limit' AND ephemeral_expires_at < TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 DAY)"
