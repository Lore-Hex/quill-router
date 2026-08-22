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
TR_REGIONS="${TR_REGIONS:-us-central1,us-east4,europe-west4,southamerica-east1}"
TR_PRIMARY_REGION="${TR_PRIMARY_REGION:-us-central1}"
# Cloud Run control-plane regions behind trustedrouter.com. This is broader
# than TR_REGIONS because cold control-plane regions can serve cached public
# pages without advertising non-existent regional attested gateway hostnames.
TR_CONTROL_PLANE_REGIONS="${TR_CONTROL_PLANE_REGIONS:-us-central1,us-east4,europe-west4,southamerica-east1}"
# Comma-separated subset of TR_REGIONS that should run with always-on warm
# capacity. Anything outside TR_WARM_REGIONS gets min_scale=0 unless the
# per-region map below says otherwise.
TR_WARM_REGIONS="${TR_WARM_REGIONS:-us-central1,europe-west4,us-east4,southamerica-east1}"
# Service-level minimums stay allocated across staged revision traffic shifts.
# US East is deliberately larger: a 2026-08-22 customer burst exhausted the
# old one-instance / concurrency-two ceiling before Cloud Run could scale.
TR_CLOUD_RUN_MIN_INSTANCES_BY_REGION="${TR_CLOUD_RUN_MIN_INSTANCES_BY_REGION:-us-central1=2,us-east4=8,europe-west4=2,southamerica-east1=2}"
# Billing handlers are small, synchronous Spanner operations dispatched to a
# worker thread. Eight concurrent requests fit comfortably in 2 GiB and avoid
# cold-starting dozens of instances for a short burst.
TR_CLOUD_RUN_CONCURRENCY="${TR_CLOUD_RUN_CONCURRENCY:-8}"
TR_SPANNER_POOL_SIZE="${TR_SPANNER_POOL_SIZE:-8}"
# Cloud Run memory limit. 2Gi as of 2026-05-10.
#
# History of the bloat profile (RSS at idle, then under load):
#   pre-fix:   ~600-900 MB at concurrency=4 → OOM at 1Gi
#   post-fix:  ~150-250 MB at concurrency=2 → comfortable at 1Gi
#
# Where the bytes go (measured 2026-05-10 with tr_mem_profile2.py):
#   ~85 MB    google-cloud SDK imports (Spanner gRPC stubs, Bigtable,
#             KMS, protobuf descriptors) — unavoidable floor
#   ~50 MB    Spanner FixedSizePool(size=10) (SDK default) at first
#             use, ~5 MB per gRPC session × 10 sessions; production pins
#             eight sessions to match request concurrency without using 10
#   ~20 MB    FastAPI + Pydantic + Starlette + uvicorn
#   ~25 MB    create_app() route registration (244 routes worth of
#             Pydantic dataclass shape metadata + dependency graphs)
#   Billing calls are small metadata payloads and mostly wait on Spanner. The
#   browser proxy and public routes remain bounded by the 2 GiB container
#   limit and are covered by staged traffic checks.
#   ~10 MB    Sentry SDK breadcrumb + transport buffers
#
# Lazy-imported only when their first route is hit (not in startup):
#   eth_account (~13 MB)  — wallet OAuth route
#   boto3 (~3 MB)         — SES email send
#
# 2Gi is kept (not lowered to 1Gi) because the per-request peak under
# spiky bursts can still pin a single instance; the surplus is cheap.
TR_CLOUD_RUN_MEMORY="${TR_CLOUD_RUN_MEMORY:-2Gi}"
# The public service must only be reachable from the external Application Load
# Balancer or from an explicitly private Google path. Authentication is a
# separate control: public pages remain unauthenticated, but the run.app origin
# is not an Internet bypass around Cloud Armor.
TR_CLOUD_RUN_INGRESS="${TR_CLOUD_RUN_INGRESS:-internal-and-cloud-load-balancing}"
# The legacy combined service has trusted regional synthetic jobs that use its
# run.app URL through Private Google Access, so it keeps that URL by default.
# Split public/control/billing services override this independently and disable
# the URL when they have no trusted direct consumer.
TR_CLOUD_RUN_DISABLE_DEFAULT_URL="${TR_CLOUD_RUN_DISABLE_DEFAULT_URL:-0}"
# Service-level cost/saturation bulkhead. Split services set their own value;
# 20 per region is the safe intermediate ceiling for the legacy combined
# service. rollout.sh uses Cloud Run's mutable --max service cap, not the
# per-revision --max-instances setting, so staged traffic cannot double it.
TR_CLOUD_RUN_MAX_INSTANCES="${TR_CLOUD_RUN_MAX_INSTANCES:-20}"
SERVICE="${SERVICE:-trusted-router}"
REPO="${REPO:-trusted-router}"
IMAGE="${IMAGE:-${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}:$(git rev-parse --short HEAD 2>/dev/null || echo local)}"
KEY_FILE="${TR_LOCAL_KEYS_FILE:-${HOME}/.quill_cloud_keys.private}"
SPANNER_INSTANCE_ID="${TR_SPANNER_INSTANCE_ID:-trusted-router-nam6}"
SPANNER_DATABASE_ID="${TR_SPANNER_DATABASE_ID:-trusted-router}"
SPANNER_CONFIG="${TR_SPANNER_CONFIG:-nam6}"
SPANNER_EDITION="${TR_SPANNER_EDITION:-ENTERPRISE_PLUS}"
SPANNER_PROCESSING_UNITS="${TR_SPANNER_PROCESSING_UNITS:-300}"
BIGTABLE_INSTANCE_ID="${TR_BIGTABLE_INSTANCE_ID:-trusted-router-logs}"
BIGTABLE_CLUSTER_ID="${TR_BIGTABLE_CLUSTER_ID:-trusted-router-logs-c1}"
BIGTABLE_APP_PROFILE_ID="${TR_BIGTABLE_APP_PROFILE_ID:-}"
BIGTABLE_GENERATION_TABLE="${TR_BIGTABLE_GENERATION_TABLE:-trustedrouter-generations}"
BIGTABLE_INSTANCE_TYPE="${TR_BIGTABLE_INSTANCE_TYPE:-PRODUCTION}"
# The first canary has one transactional writer. Bigtable rejects a second
# transactional profile on another cluster unless its split-brain warning is
# forcibly bypassed. EU and other gateways therefore use exact Spanner until
# they receive isolated regional ledgers.
TR_REGIONAL_QUOTA_CLUSTER_MAP="${TR_REGIONAL_QUOTA_CLUSTER_MAP:-us-central1=trusted-router-logs-c1}"
TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES="${TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES:-us-central1=tr-quota-us-central1}"
KMS_KEYRING_ID="${TR_KMS_KEYRING_ID:-trusted-router}"
BYOK_KMS_KEY_ID="${TR_BYOK_KMS_KEY_ID:-byok-envelope}"
BYOK_KMS_KEY_NAME="${TR_BYOK_KMS_KEY_NAME:-projects/${PROJECT_ID}/locations/${REGION}/keyRings/${KMS_KEYRING_ID}/cryptoKeys/${BYOK_KMS_KEY_ID}}"
GOOGLE_ADS_KMS_KEY_ID="${TR_GOOGLE_DATA_MANAGER_KMS_KEY_ID:-google-ads-click-envelope}"
GOOGLE_ADS_KMS_KEY_NAME="${TR_GOOGLE_DATA_MANAGER_KMS_KEY_NAME:-projects/${PROJECT_ID}/locations/${REGION}/keyRings/${KMS_KEYRING_ID}/cryptoKeys/${GOOGLE_ADS_KMS_KEY_ID}}"
TRUST_FILE="${TRUST_FILE:-/Users/jperla/claude/quill-cloud-proxy/trust-page/gcp-release.json}"
TRUST_FILE_URL="${TRUST_FILE_URL:-https://trust.trustedrouter.com/trust/gcp-release.json}"

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
  local bound_roles=""
  if bound_roles="$(gc projects get-iam-policy "$PROJECT_ID" \
      --flatten='bindings[].members' \
      --filter="bindings.role=${role} AND bindings.members=${member}" \
      --format='value(bindings.role)' 2>/dev/null)"; then
    if grep -Fxq "$role" <<<"$bound_roles"; then
      return 0
    fi
  fi

  # Retry only etag conflicts. Retrying an authorization failure creates a
  # burst of misleading ERROR audit entries and cannot make the call succeed.
  local attempt=0
  local max_attempts=6
  local last_stderr=""
  while [ "$attempt" -lt "$max_attempts" ]; do
    if last_stderr="$(gc projects add-iam-policy-binding "$PROJECT_ID" \
        --member="$member" \
        --role="$role" \
        --quiet 2>&1 >/dev/null)"; then
      return 0
    fi
    attempt=$((attempt + 1))
    if echo "$last_stderr" | grep -qE 'PERMISSION_DENIED|setIamPolicy|Policy update access denied'; then
      break
    fi
    if [ "$attempt" -lt "$max_attempts" ]; then
      sleep "$attempt"
    fi
  done
  echo "ERROR: failed to bind ${role} to ${member} after ${attempt} attempt(s). Last stderr: ${last_stderr}" >&2
  return 1
}
