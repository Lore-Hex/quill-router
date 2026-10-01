#!/usr/bin/env bash
# Online payment-debt index backfill; run separately from rolling deployments.
# Key-only on purpose: the no-debt predicate (workspace_id, kind='payment',
# unrecovered_micro > 0) seeks an EMPTY range for a no-debt workspace, and the
# base primary key (event_id) rides along; positive-debt rows are rare, so the
# recovery SELECT's base-row lookups cost less than a covering copy of the table.
# See docs/design/rpc-diet-c1.md for operator sequencing and PLAN acceptance.
set -euo pipefail

INSTANCE="${SPANNER_INSTANCE_ID:?set SPANNER_INSTANCE_ID}"
DATABASE="${SPANNER_DATABASE_ID:?set SPANNER_DATABASE_ID}"
PROJECT_ARG=()
[ -n "${GCP_PROJECT_ID:-}" ] && PROJECT_ARG=(--project "${GCP_PROJECT_ID}")

gcloud spanner databases ddl update "$DATABASE" \
  --instance="$INSTANCE" ${PROJECT_ARG[@]+"${PROJECT_ARG[@]}"} \
  --ddl="CREATE INDEX IF NOT EXISTS tr_trust_event_by_debt
    ON tr_trust_event (workspace_id, kind, unrecovered_micro)"

# A rerun can find an index whose earlier client disconnected during backfill.
for _ in $(seq 1 "${TR_DEBT_INDEX_WAIT_ATTEMPTS:-360}"); do
  state=$(gcloud spanner databases execute-sql "$DATABASE" \
    --instance="$INSTANCE" ${PROJECT_ARG[@]+"${PROJECT_ARG[@]}"} \
    --sql="SELECT INDEX_STATE FROM INFORMATION_SCHEMA.INDEXES
      WHERE TABLE_NAME='tr_trust_event' AND INDEX_NAME='tr_trust_event_by_debt'" \
    --format='value(rows[0])')
  if [ "$state" = "READ_WRITE" ]; then
    printf '%s\n' 'tr_trust_event_by_debt is read-write; operator PLAN review still required'
    exit 0
  fi
  sleep "${TR_DEBT_INDEX_WAIT_SECONDS:-5}"
done
printf '%s\n' 'tr_trust_event_by_debt did not become READ_WRITE' >&2
exit 1
