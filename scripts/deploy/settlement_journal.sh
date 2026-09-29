#!/usr/bin/env bash
# Provision only; never enables admission. NOT part of rollout.sh.
# GCP_PROJECT_ID=... TR_BIGTABLE_INSTANCE_ID=... \
# TR_ASYNC_SETTLEMENT_JOURNAL_CLUSTER_MAP='us-central1=cluster-a,europe-west4=cluster-b' \
# bash scripts/deploy/settlement_journal.sh --dry-run
set -euo pipefail
PROJECT="${GCP_PROJECT_ID:?set GCP_PROJECT_ID}"
INSTANCE="${TR_BIGTABLE_INSTANCE_ID:?set TR_BIGTABLE_INSTANCE_ID}"
BASE="${TR_ASYNC_SETTLEMENT_JOURNAL_BIGTABLE_TABLE:-trustedrouter-settlement-intents}"
MAP="${TR_ASYNC_SETTLEMENT_JOURNAL_CLUSTER_MAP:?set region=cluster entries}"
DRY=false
if [ "${1:-}" = '--dry-run' ]; then DRY=true; shift; fi
if [ "$#" -ne 0 ]; then printf 'usage: %s [--dry-run]\n' "$0" >&2; exit 2; fi
run() {
  if "$DRY"; then printf '%q ' "$@"; printf '\n'; else "$@"; fi
}
IFS=',' read -r -a entries <<< "$MAP"
seen=','
for entry in "${entries[@]}"; do
  region="${entry%%=*}"
  cluster="${entry#*=}"
  case "$region:$cluster" in *[!a-z0-9:-]*|:*|*:) echo 'invalid region=cluster' >&2; exit 2;; esac
  if [ "$region" = "$cluster" ] || [[ "$seen" == *",$region,"* ]]; then
    echo 'missing separator or duplicate region' >&2; exit 2
  fi
  seen="${seen}${region},"
  table="${BASE}-${region}"
  profile="tr-settlement-${region}"
  common=("--project=$PROJECT" "--instance=$INSTANCE")
  if "$DRY"; then
    printf '# Verify cluster location, existing table GC and app-profile routing before use.\n'
    run gcloud bigtable instances tables create "$table" "${common[@]}" \
      --column-families='journal:maxversions=1'
    run gcloud bigtable app-profiles create "$profile" "${common[@]}" \
      "--route-to=$cluster" --transactional-writes
    continue
  fi
  location="$(gcloud bigtable clusters describe "$cluster" "${common[@]}" --format='value(location)')"
  zone="${location##*/}"
  if [ "${zone%-*}" != "$region" ]; then echo 'cluster region mismatch' >&2; exit 1; fi
  # List failures are fatal. Do not treat permission/network failures as absence.
  tables="$(gcloud bigtable instances tables list "${common[@]}" --format='value(name)')"
  if ! printf '%s\n' "$tables" | grep -Fxq "projects/$PROJECT/instances/$INSTANCE/tables/$table"; then
    gcloud bigtable instances tables create "$table" "${common[@]}" \
      --column-families='journal:maxversions=1'
  fi
  gcloud bigtable instances tables describe "$table" "${common[@]}" --format=json |
    python3 -c 'import json,sys; t=json.load(sys.stdin); assert t["columnFamilies"]["journal"]["gcRule"] == {"maxNumVersions": 1}, "unsafe journal GC policy"'
  profiles="$(gcloud bigtable app-profiles list "${common[@]}" --format='value(name)')"
  if ! printf '%s\n' "$profiles" | grep -Fxq "projects/$PROJECT/instances/$INSTANCE/appProfiles/$profile"; then
    gcloud bigtable app-profiles create "$profile" "${common[@]}" \
      "--route-to=$cluster" --transactional-writes
  fi
  gcloud bigtable app-profiles describe "$profile" "${common[@]}" --format=json |
    python3 -c 'import json,sys; p=json.load(sys.stdin); assert "multiClusterRoutingUseAny" not in p; assert p["singleClusterRouting"] == {"clusterId": sys.argv[1], "allowTransactionalWrites": True}, "unsafe app profile"' "$cluster"
  printf 'TR_ASYNC_SETTLEMENT_JOURNAL_BIGTABLE_APP_PROFILES entry: %s=%s\n' "$region" "$profile"
done
