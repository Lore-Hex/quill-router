#!/usr/bin/env bash
# Provision the isolated Bigtable ledger used by bounded regional quota leases.
#
# Usage:
#   GCP_PROJECT_ID=quill-cloud-proxy \
#   TR_BIGTABLE_INSTANCE_ID=trusted-router-logs \
#   TR_REGIONAL_QUOTA_CLUSTER_MAP='us-central1=tr-us-central1' \
#     scripts/deploy/regional_quota_ledger.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

PROJECT="${GCP_PROJECT_ID:-$PROJECT_ID}"
INSTANCE="${TR_BIGTABLE_INSTANCE_ID:-$BIGTABLE_INSTANCE_ID}"
TABLE="${TR_REGIONAL_QUOTA_BIGTABLE_TABLE:-trustedrouter-regional-quota}"
CLUSTER_MAP="$TR_REGIONAL_QUOTA_CLUSTER_MAP"

log() { printf '%s %s\n' '[regional_quota_ledger]' "$*"; }

if gcloud bigtable instances tables describe "$TABLE" \
  --project="$PROJECT" --instance="$INSTANCE" >/dev/null 2>&1; then
  log "table $TABLE exists"
else
  log "creating $TABLE with seven-day, latest-version-only lease state"
  gcloud bigtable instances tables create "$TABLE" \
    --project="$PROJECT" \
    --instance="$INSTANCE" \
    --column-families='lease:maxversions=1||maxage=7d'
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
  profile="tr-quota-${region}"
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
      log "refusing regional quota profile drift: ${profile} must route only to ${cluster} with transactional writes"
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
      --description="TrustedRouter regional quota escrow for ${region}"
  fi
done

profile_csv="$(IFS=','; printf '%s' "${profiles[*]}")"
log "set TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES=${profile_csv}"
