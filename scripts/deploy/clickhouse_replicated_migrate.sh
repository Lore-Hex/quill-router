#!/usr/bin/env bash
# Apply numbered `clickhouse/NNN_*_replicated.sql` migrations on the GCP ClickHouse
# cluster without half-applying them.
#
# Every `ALTER TABLE tr.<table> ON CLUSTER <cluster>` in a migration is executed
# on every replica by ClickHouse; a replica that lacks the table fails with
# UNKNOWN_TABLE while the others succeed, which leaves the cluster with mixed
# schemas and a migration that "ran". This script refuses to apply a file until
# each altered table exists on every replica of its cluster, and after applying
# it fails if any replica reported a non-zero status.
#
# Usage:
#   scripts/deploy/clickhouse_replicated_migrate.sh [--apply] clickhouse/017_regional_coverage_replicated.sql ...
#
# Without --apply the script only parses the files and runs the presence checks.
# A missing table is created with the definition of a replica that has it:
#   SHOW CREATE TABLE tr.<table>   (on that replica)
#   CREATE TABLE IF NOT EXISTS tr.<table> ON CLUSTER <cluster> (...) ENGINE = ...
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

NAMES=(tr-clickhouse-1 tr-clickhouse-2 tr-clickhouse-3)
ZONES=(us-central1-a us-central1-b us-central1-c)

log() { printf '%s %s\n' '[clickhouse_replicated_migrate]' "$*"; }

APPLY=0
FILES=()
for argument in "$@"; do
  case "$argument" in
    --apply) APPLY=1 ;;
    --help|-h)
      sed -n '2,20p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *) FILES+=("$argument") ;;
  esac
done
if [ "${#FILES[@]}" -eq 0 ]; then
  echo "usage: $0 [--apply] clickhouse/NNN_*_replicated.sql ..." >&2
  exit 2
fi

node_ssh() {
  local index="$1"
  shift
  gc compute ssh "${NAMES[$index]}" \
    --zone="${ZONES[$index]}" \
    --tunnel-through-iap \
    --quiet \
    "$@"
}

# The query travels in the ssh command line (not stdin) so that a transcript of
# the calls shows exactly which statement ran on which node.
node_query() {
  local index="$1"
  local query="$2"
  local quoted
  quoted="$(printf '%q' "$query")"
  node_ssh "$index" --command="sudo sh -c 'set -eu; set -a; . /etc/tr-clickhouse-ingest.env; set +a; /usr/bin/clickhouse-client --user tr --password \"\$CH_PASSWORD\" --database tr --multiquery --query ${quoted}'"
}

# Replica host names for a cluster: every replica answers clusterAllReplicas.
cluster_replicas() {
  node_query 0 "SELECT hostName() FROM clusterAllReplicas('$1', system.one) ORDER BY 1 FORMAT TSV" | tr -d '\r'
}

# Replicas of a cluster on which tr.<table> exists.
table_replicas() {
  node_query 0 "SELECT hostName() FROM clusterAllReplicas('$1', system.tables) WHERE database = 'tr' AND name = '$2' ORDER BY 1 FORMAT TSV" | tr -d '\r'
}

status=0
for file in "${FILES[@]}"; do
  path="$file"
  [ -f "$path" ] || path="${ROOT}/${file}"
  if [ ! -f "$path" ]; then
    echo "migration file not found: ${file}" >&2
    exit 2
  fi
  # ALTER TABLE tr.<table> ON CLUSTER <cluster>  ->  "<table> <cluster>"
  targets="$(grep -oiE 'ALTER TABLE +(tr\.)?[A-Za-z0-9_]+ +ON CLUSTER +[A-Za-z0-9_]+' "$path" \
    | sed -E 's/^ALTER TABLE +(tr\.)?//I; s/ +ON CLUSTER +/ /I' | sort -u || true)"
  if [ -z "$targets" ]; then
    echo "refusing ${file}: no 'ALTER TABLE ... ON CLUSTER' statement found; this applier only handles replicated migrations" >&2
    exit 2
  fi
  while read -r table cluster; do
    [ -n "$table" ] || continue
    replicas="$(cluster_replicas "$cluster")"
    present="$(table_replicas "$cluster" "$table")"
    missing="$(comm -23 <(printf '%s\n' "$replicas" | sort) <(printf '%s\n' "$present" | sort))"
    if [ -n "$missing" ]; then
      echo "refusing ${file}: tr.${table} is missing on $(printf '%s' "$missing" | tr '\n' ' ')of cluster ${cluster}; an ON CLUSTER ALTER would succeed on the other replicas and leave mixed schemas. Create it first with CREATE TABLE IF NOT EXISTS tr.${table} ON CLUSTER ${cluster} using SHOW CREATE TABLE from a replica that has it." >&2
      status=1
      continue
    fi
    log "tr.${table} present on all $(printf '%s\n' "$replicas" | grep -c .) replicas of ${cluster}"
  done <<<"$targets"
done
[ "$status" -eq 0 ] || exit "$status"

if [ "$APPLY" -ne 1 ]; then
  log "dry-run: presence checks passed for ${#FILES[@]} file(s); rerun with --apply"
  exit 0
fi

for file in "${FILES[@]}"; do
  path="$file"
  [ -f "$path" ] || path="${ROOT}/${file}"
  log "applying ${file}"
  output="$(node_query 0 "$(cat "$path")")"
  printf '%s\n' "$output"
  # ON CLUSTER statements report one row per replica: host, port, status, error, ...
  failed="$(printf '%s\n' "$output" | awk -F'\t' 'NF >= 3 && $3 != "" && $3 != 0 { print $1 ": " $4 }' || true)"
  if [ -n "$failed" ]; then
    echo "migration ${file} failed on a replica:" >&2
    printf '%s\n' "$failed" >&2
    exit 1
  fi
  log "applied ${file} on every replica"
done
