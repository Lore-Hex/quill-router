#!/usr/bin/env bash
# Add bounded metadata-only generation lookup records. Dry-run by default.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

APPLY=0
if [ "${1:-}" = "--apply" ]; then
  APPLY=1
elif [ $# -ne 0 ]; then
  printf 'usage: %s [--apply]\n' "$0" >&2
  exit 2
fi

exists="$(gc spanner databases execute-sql "$SPANNER_DATABASE_ID" \
  --instance="$SPANNER_INSTANCE_ID" \
  --sql="SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE table_name='tr_generation'" \
  --format='value(rows[0])')"

DDL="CREATE TABLE tr_generation (
  generation_id STRING(128) NOT NULL,
  workspace_id STRING(64) NOT NULL,
  key_hash STRING(128) NOT NULL,
  created_at TIMESTAMP NOT NULL,
  terminal_at TIMESTAMP NOT NULL,
  payload STRING(MAX) NOT NULL,
) PRIMARY KEY (generation_id),
  ROW DELETION POLICY (OLDER_THAN(terminal_at, INTERVAL 30 DAY))"

if [ "${exists:-0}" = "0" ]; then
  if [ "$APPLY" -eq 0 ]; then
    log "dry-run DDL: ${DDL}"
  else
    gc spanner databases ddl update "$SPANNER_DATABASE_ID" \
      --instance="$SPANNER_INSTANCE_ID" \
      --ddl="$DDL"
    log "created tr_generation; no existing rows were changed or deleted"
  fi
else
  log "tr_generation exists"
fi

index_exists="$(gc spanner databases execute-sql "$SPANNER_DATABASE_ID" \
  --instance="$SPANNER_INSTANCE_ID" \
  --sql="SELECT COUNT(*) FROM INFORMATION_SCHEMA.INDEXES WHERE index_name='tr_generation_by_terminal_at'" \
  --format='value(rows[0])')"
if [ "${index_exists:-0}" = "0" ]; then
  INDEX_DDL="CREATE INDEX tr_generation_by_terminal_at ON tr_generation(terminal_at DESC) STORING (payload)"
  if [ "$APPLY" -eq 0 ]; then
    log "dry-run DDL: ${INDEX_DDL}"
  else
    gc spanner databases ddl update "$SPANNER_DATABASE_ID" \
      --instance="$SPANNER_INSTANCE_ID" \
      --ddl="$INDEX_DDL"
    log "created tr_generation_by_terminal_at delivery-audit index"
  fi
else
  log "tr_generation_by_terminal_at exists"
fi
