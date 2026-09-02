#!/usr/bin/env bash
# Provision the isolated Bigtable ledger used by spend-lease escrow.
#
# Usage:
#   GCP_PROJECT_ID=quill-cloud-proxy \
#   TR_BIGTABLE_INSTANCE_ID=trusted-router-logs \
#   TR_SPEND_LEASE_CLUSTER_MAP='us-central1=tr-us-central1' \
#     scripts/deploy/spend_lease_ledger.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

PROJECT="${GCP_PROJECT_ID:-$PROJECT_ID}"
INSTANCE="${TR_BIGTABLE_INSTANCE_ID:-$BIGTABLE_INSTANCE_ID}"
TABLE="${TR_SPEND_LEASE_BIGTABLE_TABLE:-trustedrouter-spend-lease}"
CLUSTER_MAP="$TR_SPEND_LEASE_CLUSTER_MAP"

log() { printf '%s %s\n' '[spend_lease_ledger]' "$*"; }

if gcloud bigtable instances tables describe "$TABLE" \
  --project="$PROJECT" --instance="$INSTANCE" >/dev/null 2>&1; then
  log "table $TABLE exists"
  table_schema="$(
    gcloud bigtable instances tables describe "$TABLE" \
      --project="$PROJECT" \
      --instance="$INSTANCE" \
      --format=json
  )"
  if ! python3 - "$table_schema" <<'PY'
import json
import sys

try:
    schema = json.loads(sys.argv[1])
    lease_rule = schema.get("columnFamilies", {}).get("lease", {}).get("gcRule")
except (AttributeError, json.JSONDecodeError, TypeError):
    raise SystemExit(1) from None
raise SystemExit(0 if lease_rule == {"maxNumVersions": 1} else 1)
PY
  then
    log "refusing spend-lease table drift: lease must use maxversions=1 with no maxage"
    exit 1
  fi
else
  log "creating $TABLE with latest-version-only lease state and no age-based GC"
  # Decision 33: No age-based GC on the state family: QUARANTINED is open and may
  # hold escrow until an operator acts, so no finite maxage is safe; committed rows
  # are deleted by the reconciler's explicit deletion.
  gcloud bigtable instances tables create "$TABLE" \
    --project="$PROJECT" \
    --instance="$INSTANCE" \
    --column-families='lease:maxversions=1'
fi

IFS=',' read -r -a entries <<< "$CLUSTER_MAP"
profiles=()
for entry in "${entries[@]}"; do
  region="${entry%%=*}"
  cluster="${entry#*=}"
  if [ -z "$region" ] || [ -z "$cluster" ] || [ "$region" = "$cluster" ]; then
    log "invalid cluster-map entry: $entry"
    exit 2
  fi
  profile="tr-spend-${region}"
  profiles+=("${region}=${profile}")
  if gcloud bigtable app-profiles describe "$profile" \
    --project="$PROJECT" --instance="$INSTANCE" >/dev/null 2>&1; then
    profile_config="$(
      gcloud bigtable app-profiles describe "$profile" \
        --project="$PROJECT" \
        --instance="$INSTANCE" \
        --format='value(singleClusterRouting.clusterId,singleClusterRouting.allowTransactionalWrites)'
    )"
    read -r actual_cluster transactional_writes <<<"$profile_config"
    if [ "$actual_cluster" != "$cluster" ] || [ "$transactional_writes" != "True" ]; then
      log "refusing spend-lease profile drift: ${profile} must route only to ${cluster} with transactional writes"
      exit 1
    fi
    log "app profile $profile is transactionally pinned to $cluster"
  else
    log "creating transactional single-cluster profile $profile -> $cluster"
    gcloud bigtable app-profiles create "$profile" \
      --project="$PROJECT" \
      --instance="$INSTANCE" \
      --route-to="$cluster" \
      --transactional-writes \
      --description="TrustedRouter spend-lease escrow for ${region}"
  fi
done

profile_csv="$(IFS=','; printf '%s' "${profiles[*]}")"
log "set TR_SPEND_LEASE_BIGTABLE_APP_PROFILES=${profile_csv}"
