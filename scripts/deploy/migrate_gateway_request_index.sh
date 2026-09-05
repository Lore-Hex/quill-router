#!/usr/bin/env bash
# Two-phase migration. Prepare before deploying trace-aware readers to every
# region; retire the old unique index only after those readers are verified.
# No authorization, reservation, balance, or idempotency records are deleted.
set -euo pipefail

INSTANCE="${SPANNER_INSTANCE_ID:?set SPANNER_INSTANCE_ID}"
DATABASE="${SPANNER_DATABASE_ID:?set SPANNER_DATABASE_ID}"
PROJECT="${GCP_PROJECT_ID:?set GCP_PROJECT_ID}"
MODE="${1:---prepare}"
case "$MODE" in
  --prepare|--retire-unique) ;;
  *) echo "usage: $0 [--prepare|--retire-unique]" >&2; exit 2 ;;
esac

sql() {
  gcloud spanner databases execute-sql "$DATABASE" --instance="$INSTANCE" \
    --project="$PROJECT" --sql="$1" --format='value(rows[0])'
}
ddl() {
  gcloud spanner databases ddl update "$DATABASE" --instance="$INSTANCE" \
    --project="$PROJECT" --ddl="$1"
}
NEW=tr_gateway_authorization_by_trace_id
OLD=tr_gateway_authorization_by_gateway_request_id

count=$(sql "SELECT COUNT(*) FROM INFORMATION_SCHEMA.INDEXES WHERE INDEX_NAME='$NEW'")
if [[ "$count" == 0 && "$MODE" == --prepare ]]; then
  ddl "CREATE NULL_FILTERED INDEX $NEW ON tr_gateway_authorization (gateway_request_id)"
elif [[ "$count" != 1 ]]; then
  echo "Trace index is absent or its existence could not be verified" >&2
  exit 1
fi
ready=$(sql "SELECT COUNT(*) FROM INFORMATION_SCHEMA.INDEXES WHERE INDEX_NAME='$NEW' AND INDEX_STATE='READ_WRITE' AND IS_UNIQUE=FALSE AND IS_NULL_FILTERED=TRUE")
if [[ "$ready" != 1 ]]; then
  echo "Trace index is not ready with the expected nonunique, sparse shape" >&2
  exit 1
fi
if [[ "$MODE" == --retire-unique ]]; then
  old_count=$(sql "SELECT COUNT(*) FROM INFORMATION_SCHEMA.INDEXES WHERE INDEX_NAME='$OLD'")
  case "$old_count" in
    1) ddl "DROP INDEX $OLD" ;;
    0) echo "Legacy unique index is already absent" ;;
    *) echo "Could not verify legacy index state" >&2; exit 1 ;;
  esac
fi
echo "Gateway trace index migration complete: $MODE"
