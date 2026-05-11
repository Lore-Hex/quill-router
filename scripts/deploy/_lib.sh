# shellcheck shell=bash
# Shared config + helpers for the deploy-gcp phase scripts. Sourced from
# scripts/deploy-gcp.sh (the orchestrator) and each phase under
# scripts/deploy/. Each phase script can also be run standalone for
# partial deploys (`scripts/deploy/rollout.sh` to redeploy without
# re-pushing secrets, etc.).

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-quill-cloud-proxy}"
REGION="${REGION:-us-central1}"
# Only enumerate regions where a real attested-gateway VM is deployed.
# The control plane (this Cloud Run service) gets deployed to each of
# these for low-latency dashboard reads, AND each entry shows up in
# /v1/regions which the SDK's region= shortcut resolves against. Adding
# a region without a backing gateway VM gives callers a TLS-broken
# `api-<region>.quillrouter.com` and weakens the trust story.
TR_REGIONS="${TR_REGIONS:-us-central1,europe-west4,us-east4,asia-northeast1,asia-southeast1,southamerica-east1}"
TR_PRIMARY_REGION="${TR_PRIMARY_REGION:-us-central1}"
# Comma-separated subset of TR_REGIONS that should run with min_scale=1
# (always-on warm capacity). Anything in TR_REGIONS but NOT in
# TR_WARM_REGIONS gets min_scale=0 (scale-to-zero — ~$0/mo idle, cold-
# start tax on first request). Defaults to the regions where we run an
# attested enclave MIG; the APAC + LATAM additions stay cold so they
# show on the homepage map without paying for always-on Cloud Run.
TR_WARM_REGIONS="${TR_WARM_REGIONS:-us-central1,europe-west4,us-east4}"
# Cloud Run memory limit. 2Gi as of 2026-05-10 — 1Gi was OOM-killing
# under steady-state load (Cloud Run repeatedly logged
# "container instance was found to be using too much memory and was
# terminated" on us-central1 / europe-west4). The resident set settles
# around 600-900 MiB once Spanner + Bigtable gRPC channels are warmed
# and concurrency=4 requests are in flight; 2Gi gives 2x headroom.
# Real bloat investigation is open — the snapshot is 290KB on disk
# but in-memory uses ~600MB+ before any traffic, suggesting either
# Python import overhead (fastapi + httpx + google-cloud SDKs) or
# unbounded dicts in storage.py (auth sessions, broadcast jobs,
# email blocks live in per-process dicts that grow indefinitely).
TR_CLOUD_RUN_MEMORY="${TR_CLOUD_RUN_MEMORY:-2Gi}"
SERVICE="${SERVICE:-trusted-router}"
REPO="${REPO:-trusted-router}"
IMAGE="${IMAGE:-${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}:$(git rev-parse --short HEAD 2>/dev/null || echo local)}"
KEY_FILE="${TR_LOCAL_KEYS_FILE:-${HOME}/.quill_cloud_keys.private}"
SPANNER_INSTANCE_ID="${TR_SPANNER_INSTANCE_ID:-trusted-router-nam6}"
SPANNER_DATABASE_ID="${TR_SPANNER_DATABASE_ID:-trusted-router}"
BIGTABLE_INSTANCE_ID="${TR_BIGTABLE_INSTANCE_ID:-trusted-router-logs}"
BIGTABLE_CLUSTER_ID="${TR_BIGTABLE_CLUSTER_ID:-trusted-router-logs-c1}"
BIGTABLE_APP_PROFILE_ID="${TR_BIGTABLE_APP_PROFILE_ID:-}"
BIGTABLE_GENERATION_TABLE="${TR_BIGTABLE_GENERATION_TABLE:-trustedrouter-generations}"
BIGTABLE_INSTANCE_TYPE="${TR_BIGTABLE_INSTANCE_TYPE:-PRODUCTION}"
KMS_KEYRING_ID="${TR_KMS_KEYRING_ID:-trusted-router}"
BYOK_KMS_KEY_ID="${TR_BYOK_KMS_KEY_ID:-byok-envelope}"
BYOK_KMS_KEY_NAME="${TR_BYOK_KMS_KEY_NAME:-projects/${PROJECT_ID}/locations/${REGION}/keyRings/${KMS_KEYRING_ID}/cryptoKeys/${BYOK_KMS_KEY_ID}}"
TRUST_FILE="${TRUST_FILE:-/Users/jperla/claude/quill-cloud-proxy/trust-page/gcp-release.json}"

log() { echo "[$(date +%H:%M:%S)] $*" >&2; }
gc() { gcloud --project "$PROJECT_ID" "$@"; }

PROJECT_NUMBER="$(gc projects describe "$PROJECT_ID" --format='value(projectNumber)')"
RUN_SERVICE_ACCOUNT="${RUN_SERVICE_ACCOUNT:-${PROJECT_NUMBER}-compute@developer.gserviceaccount.com}"

read_key_file_var() {
  local env_name="$1"
  shift || true
  if [ ! -f "$KEY_FILE" ]; then
    return 0
  fi
  python3 - "$KEY_FILE" "$env_name" "$@" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
wanted = sys.argv[2:]
values = {}
for raw in path.read_text().splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    values[key.strip()] = value.strip().strip('"').strip("'")
for name in wanted:
    if values.get(name):
        print(values[name])
        break
PY
}

ensure_secret_value() {
  local secret_name="$1"
  local value="$2"
  if ! gc secrets describe "$secret_name" >/dev/null 2>&1; then
    printf '%s' "$value" | gc secrets create "$secret_name" \
      --replication-policy=automatic \
      --data-file=-
  else
    local current
    current="$(gc secrets versions access latest --secret="$secret_name" 2>/dev/null || true)"
    if [ "$current" != "$value" ]; then
      printf '%s' "$value" | gc secrets versions add "$secret_name" --data-file=- >/dev/null
    fi
  fi
}

ensure_project_role() {
  local member="$1"
  local role="$2"
  # Retry on the etag-conflict error gcloud's own message says to retry on.
  # Concurrent IAM changes on a busy project — including the parallel
  # Cloud Run deploys later in this script, which each provision their
  # own service-account bindings — collide on add-iam-policy-binding's
  # read-modify-write. Sleep + retry is the documented mitigation.
  local attempt=0
  local max_attempts=6
  while [ "$attempt" -lt "$max_attempts" ]; do
    if gc projects add-iam-policy-binding "$PROJECT_ID" \
        --member="$member" \
        --role="$role" \
        --quiet >/dev/null 2>&1; then
      return 0
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -lt "$max_attempts" ]; then
      sleep "$attempt"
    fi
  done
  echo "ERROR: failed to bind ${role} to ${member} after ${max_attempts} attempts" >&2
  return 1
}
