#!/usr/bin/env bash
# Phase 5: deploy scheduled synthetic monitor jobs and an isolated sustained
# throughput job. Jobs run outside the prompt path and write privacy-safe
# samples to internal ingest endpoints. Short uptime probes must never wait on
# the longer throughput benchmark.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"
# shellcheck source=scripts/deploy/_private_run_ingress.sh
source "${SCRIPT_DIR}/_private_run_ingress.sh"

validate_runtime_service_accounts

# Observer-token jobs cannot safely target the legacy combined service: before
# the split that service accepts only the billing gateway token, so the jobs
# would deploy successfully and then silently 401 every ingest. Require the
# independently named internal service and re-check its live
# contract immediately before every job deployment below.
[ "$TR_BILLING_SERVICE" != "$SERVICE" ] || {
  echo "ERROR: TR_BILLING_SERVICE must be separate from legacy SERVICE" >&2
  exit 1
}
case "$TR_BILLING_SERVICE" in
  *[!a-zA-Z0-9-]*|'')
    echo "ERROR: invalid TR_BILLING_SERVICE" >&2
    exit 2
    ;;
esac
SYNTHETIC_INGEST_SERVICE="$TR_BILLING_SERVICE"
[ "$SYNTHETIC_RUN_SERVICE_ACCOUNT" = "tr-synthetic@${PROJECT_ID}.iam.gserviceaccount.com" ] || {
  echo "ERROR: synthetic Jobs require the canonical dedicated tr-synthetic identity" >&2
  exit 1
}
synthetic_account_json="$(gc iam service-accounts describe \
  "$SYNTHETIC_RUN_SERVICE_ACCOUNT" --format=json)" || {
    echo "ERROR: dedicated synthetic identity is not provisioned; separate narrow IAM approval is required" >&2
    exit 1
  }
if ! printf '%s' "$synthetic_account_json" | python3 -c '
import json
import sys

account = json.load(sys.stdin)
if account.get("email") != sys.argv[1] or account.get("disabled", False) is not False:
    raise SystemExit("synthetic identity is missing, disabled, or renamed")
' "$SYNTHETIC_RUN_SERVICE_ACCOUNT"; then
  echo "ERROR: dedicated synthetic identity is missing, disabled, or renamed" >&2
  exit 1
fi
synthetic_account_policy="$(gc iam service-accounts get-iam-policy \
  "$SYNTHETIC_RUN_SERVICE_ACCOUNT" --format=json)" || exit 1
if ! printf '%s' "$synthetic_account_policy" | python3 -c '
import json
import sys

policy = json.load(sys.stdin)
bindings = policy.get("bindings") or []
if len(bindings) != 1:
    raise SystemExit("synthetic identity IAM binding inventory differs")
binding = bindings[0]
if binding.get("role") != "roles/iam.serviceAccountUser":
    raise SystemExit("synthetic identity actAs role differs")
if binding.get("condition") is not None:
    raise SystemExit("synthetic identity actAs grant is conditional")
if (binding.get("members") or []) != [sys.argv[1]]:
    raise SystemExit("synthetic identity actAs member inventory differs")
' "serviceAccount:${DEPLOY_SERVICE_ACCOUNT}"; then
  echo "ERROR: dedicated synthetic identity IAM is not the narrow reviewed policy" >&2
  exit 1
fi
verify_exact_unconditional_roles \
  "project IAM roles on the synthetic Job identity" \
  "serviceAccount:${SYNTHETIC_RUN_SERVICE_ACCOUNT}" \
  "" \
  gc projects get-iam-policy "$PROJECT_ID"

if ! gc secrets describe trustedrouter-synthetic-monitor-api-key >/dev/null 2>&1; then
  log "synthetic monitor key secret is missing; skipping synthetic monitor deploy"
  exit 0
fi
if ! gc secrets describe trustedrouter-observer-internal-token >/dev/null 2>&1; then
  echo "ERROR: trustedrouter-observer-internal-token is required for synthetic ingest" >&2
  exit 1
fi

verify_synthetic_secret_access() {
  local secret_name policy_json expected_surfaces
  for secret_name in \
    trustedrouter-observer-internal-token \
    trustedrouter-synthetic-monitor-api-key; do
    expected_surfaces="$(secret_expected_surfaces "$secret_name")" || return 1
    policy_json="$(gc secrets get-iam-policy "$secret_name" --format=json)" || return 1
    if ! printf '%s' "$policy_json" | secret_iam_policy_contract_json verify \
        "$secret_name" "$expected_surfaces"; then
      echo "ERROR: ${secret_name} does not have the exact reviewed accessor policy" >&2
      return 1
    fi
  done
}

resolve_newest_enabled_secret_version() {
  local secret_name="$1" versions_json
  versions_json="$(gc secrets versions list "$secret_name" \
    --filter='state=ENABLED' --format=json)" || return 1
  printf '%s' "$versions_json" | python3 -c '
import json
import sys

items = json.load(sys.stdin)
versions = []
for item in items:
    if item.get("state") != "ENABLED":
        continue
    version = str(item.get("name") or "").rstrip("/").split("/")[-1]
    if not version.isdigit() or version.startswith("0"):
        raise SystemExit("enabled secret version name is not numeric")
    versions.append(int(version))
if not versions:
    raise SystemExit("secret has no enabled numeric version")
print(max(versions))
'
}

OBSERVER_SECRET_VERSION="$(resolve_newest_enabled_secret_version \
  trustedrouter-observer-internal-token)" || {
    echo "ERROR: cannot resolve an enabled observer-token version" >&2
    exit 1
  }
MONITOR_SECRET_VERSION="$(resolve_newest_enabled_secret_version \
  trustedrouter-synthetic-monitor-api-key)" || {
    echo "ERROR: cannot resolve an enabled synthetic-monitor version" >&2
    exit 1
  }
SECRET_ENVS=(
  "TR_OBSERVER_INTERNAL_TOKEN=trustedrouter-observer-internal-token:${OBSERVER_SECRET_VERSION}"
  "TR_SYNTHETIC_MONITOR_API_KEY=trustedrouter-synthetic-monitor-api-key:${MONITOR_SECRET_VERSION}"
)
# Complete allowlist: --set-secrets removes legacy gateway/payment bindings
# from an existing job instead of preserving omitted secrets across updates.
SET_SECRETS="$(IFS=,; echo "${SECRET_ENVS[*]}")"

verify_synthetic_job_secret_contract() {
  local job_name="$1" region="$2" job_json
  job_json="$(gc run jobs describe "$job_name" --region="$region" --format=json)" || {
    echo "ERROR: cannot read deployed synthetic Job ${region}/${job_name}" >&2
    return 1
  }
  printf '%s' "$job_json" | python3 -c '
import json
import sys

data = json.load(sys.stdin)
expected_identity, observer_version, monitor_version = sys.argv[1:]
containers = []
identities = []

def visit(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"serviceAccount", "serviceAccountName"} and isinstance(child, str):
                identities.append(child)
            if key == "containers" and isinstance(child, list):
                containers.extend(child)
            visit(child)
    elif isinstance(value, list):
        for child in value:
            visit(child)

visit(data.get("spec") or {})
if sorted(set(identities)) != [expected_identity]:
    raise SystemExit("synthetic Job runtime identity differs")
if len(containers) != 1:
    raise SystemExit("synthetic Job container inventory differs")
env = {
    item.get("name"): item
    for item in containers[0].get("env", [])
    if item.get("name")
}
actual = {}
for name, item in env.items():
    if "valueFrom" not in item:
        continue
    reference = (item.get("valueFrom") or {}).get("secretKeyRef") or {}
    actual[name] = {
        "resource": str(reference.get("name") or reference.get("secret") or "").split("/")[-1],
        "version": str(reference.get("key") or reference.get("version") or ""),
    }
expected = {
    "TR_OBSERVER_INTERNAL_TOKEN": {
        "resource": "trustedrouter-observer-internal-token",
        "version": observer_version,
    },
    "TR_SYNTHETIC_MONITOR_API_KEY": {
        "resource": "trustedrouter-synthetic-monitor-api-key",
        "version": monitor_version,
    },
}
if actual != expected:
    raise SystemExit("synthetic Job exact numeric secret map differs")
' "$SYNTHETIC_RUN_SERVICE_ACCOUNT" \
    "$OBSERVER_SECRET_VERSION" "$MONITOR_SECRET_VERSION" || {
      echo "ERROR: deployed synthetic Job ${region}/${job_name} secret contract failed" >&2
      return 1
    }
}

SYNTHETIC_RELEASE="${TR_RELEASE:-$(git rev-parse --short HEAD 2>/dev/null || echo local)}"
[[ "$SYNTHETIC_RELEASE" =~ ^[A-Za-z0-9._-]{1,128}$ ]] || {
  echo "ERROR: synthetic release is invalid" >&2
  exit 1
}
BASE_ENV_VARS=(
  # These are one-shot workers, not the public control-plane process. Using
  # the worker runtime keeps control-plane-only dependencies such as SES out
  # of the monitor containers while their actual storage and probe inputs
  # remain explicit below.
  "TR_ENVIRONMENT=worker"
  "TR_SERVICE_SURFACE=observer"
  "TR_RELEASE=${SYNTHETIC_RELEASE}"
  "TR_ENABLE_LIVE_PROVIDERS=false"
  "TR_API_BASE_URL=https://api.trustedrouter.com/v1"
  "TR_TRUSTED_DOMAIN=trustedrouter.com"
  "TR_STORAGE_BACKEND=spanner-bigtable"
  "TR_GCP_PROJECT_ID=${PROJECT_ID}"
  "TR_SPANNER_INSTANCE_ID=${SPANNER_INSTANCE_ID}"
  "TR_SPANNER_DATABASE_ID=${SPANNER_DATABASE_ID}"
  "TR_BIGTABLE_INSTANCE_ID=${BIGTABLE_INSTANCE_ID}"
  "TR_BIGTABLE_GENERATION_TABLE=${BIGTABLE_GENERATION_TABLE}"
  "TR_REGIONS=${TR_REGIONS}"
  "TR_PRIMARY_REGION=${TR_PRIMARY_REGION}"
  "TR_SYNTHETIC_MONITOR_MODEL=trustedrouter/monitor"
  # Provider-effective checks exercise real LLM responses. Keep this above
  # the p95 of the cheap monitor pool so slow successes don't become false
  # downtime, while still bounding true hangs.
  "TR_SYNTHETIC_MONITOR_TIMEOUT_SECONDS=30"
  "TR_SYNTHETIC_CONTROL_PLANE_URL=https://trustedrouter.com"
  # One bounded pass per scheduler tick. Sub-minute passes made
  # provider-effective timeouts stack up behind health probes and caused
  # Cloud Run Job self-timeouts.
  "TR_SYNTHETIC_RUNS_PER_INVOCATION=1"
  "TR_SYNTHETIC_RUN_SPACING_SECONDS=0"
  "VERTEX_PROJECT_ID=${PROJECT_ID}"
  "VERTEX_LOCATION=${REGION}"
)

verify_synthetic_ingest_service_contract() {
  local target_region="$1"
  local service_json=""
  local revision_name=""
  local revision_json=""

  service_json="$(gc run services describe "$SYNTHETIC_INGEST_SERVICE" \
    --region "$target_region" \
    --format=json)" || {
      echo "ERROR: internal synthetic ingest service is absent in ${target_region}" >&2
      return 1
    }
  revision_name="$(printf '%s' "$service_json" | python3 -c '
import json
import sys

data = json.load(sys.stdin)
expected_name = sys.argv[1]
metadata = data.get("metadata", {})
reported_name = metadata.get("name")
if reported_name and reported_name != expected_name:
    raise SystemExit(f"service name is {reported_name!r}, expected {expected_name!r}")
conditions = data.get("status", {}).get("conditions", [])
ready = any(
    item.get("type") == "Ready"
    and str(item.get("status", "")).casefold() == "true"
    for item in conditions
)
if not ready:
    raise SystemExit("service Ready condition is not True")
annotations = metadata.get("annotations", {}) or {}
if annotations.get("run.googleapis.com/ingress") != "internal-and-cloud-load-balancing":
    raise SystemExit("service ingress is not internal-and-cloud-load-balancing")
traffic = [
    item
    for item in data.get("status", {}).get("traffic", []) or []
    if int(item.get("percent", 0) or 0) > 0
]
if len(traffic) != 1 or int(traffic[0].get("percent", 0) or 0) != 100:
    raise SystemExit("service must have exactly one revision serving 100 percent")
revision = traffic[0].get("revisionName")
if not revision:
    raise SystemExit("serving traffic does not name an immutable revision")
print(revision)
' "$SYNTHETIC_INGEST_SERVICE")" || {
    echo "ERROR: internal synthetic ingest service contract failed in ${target_region}" >&2
    return 1
  }
  revision_json="$(gc run revisions describe "$revision_name" \
    --region "$target_region" --format=json)" || {
    echo "ERROR: serving internal revision ${revision_name} is absent in ${target_region}" >&2
    return 1
  }
  if ! printf '%s' "$revision_json" | python3 -c '
import json
import sys

data = json.load(sys.stdin)
expected_identity, expected_observer_secret, expected_gateway_secret = sys.argv[1:4]
spec = data.get("spec", {})
actual_identity = spec.get("serviceAccountName") or spec.get("serviceAccount")
if actual_identity != expected_identity:
    raise SystemExit(
        f"serving revision identity is {actual_identity!r}, expected {expected_identity!r}"
    )
containers = spec.get("containers", [])
if len(containers) != 1:
    raise SystemExit("serving revision must have exactly one container")
env = {
    item.get("name"): item
    for item in containers[0].get("env", [])
    if item.get("name")
}
if env.get("TR_SERVICE_SURFACE", {}).get("value") != "internal":
    raise SystemExit("service surface is not internal")

def secret_name(env_name):
    ref = env.get(env_name, {}).get("valueFrom", {}).get("secretKeyRef", {})
    return ref.get("name") or ref.get("secret")

if secret_name("TR_OBSERVER_INTERNAL_TOKEN") != expected_observer_secret:
    raise SystemExit("observer token is not bound to the dedicated secret")
if secret_name("TR_INTERNAL_GATEWAY_TOKEN") != expected_gateway_secret:
    raise SystemExit("billing gateway token is not bound to the dedicated secret")
' "$INTERNAL_RUN_SERVICE_ACCOUNT" \
      trustedrouter-observer-internal-token \
      trustedrouter-internal-gateway-token; then
    echo "ERROR: serving internal revision contract failed in ${target_region}" >&2
    return 1
  fi
}

IMAGE_METADATA="$(gc artifacts docker images describe "$IMAGE" --format=json)" || {
  echo "ERROR: image ${IMAGE} does not exist. Run scripts/deploy/image.sh before synthetic.sh." >&2
  exit 1
}
IMAGE="$(printf '%s' "$IMAGE_METADATA" | python3 -c '
import json
import sys

data = json.load(sys.stdin)
summary = data.get("image_summary") or data.get("imageSummary") or {}
value = (
    summary.get("fully_qualified_digest")
    or summary.get("fullyQualifiedDigest")
    or data.get("fully_qualified_digest")
    or data.get("fullyQualifiedDigest")
)
if not isinstance(value, str):
    raise SystemExit("image metadata omits fully qualified digest")
print(value)
')" || {
  echo "ERROR: synthetic image did not resolve to an immutable digest" >&2
  exit 1
}
[[ "$IMAGE" =~ ^[^,|[:space:]@]+@sha256:[0-9a-f]{64}$ ]] || {
  echo "ERROR: synthetic image digest is invalid" >&2
  exit 1
}

verify_exact_synthetic_job_invoker_policy() {
  local job_name="$1"
  local region="$2"
  local policy_json
  policy_json="$(gc run jobs get-iam-policy "$job_name" \
    --region="$region" --format=json)" || return 1
  if ! printf '%s' "$policy_json" | python3 -c '
import json
import sys

policy = json.load(sys.stdin)
bindings = policy.get("bindings") or []
if len(bindings) != 1:
    raise SystemExit("synthetic Job IAM binding inventory differs")
binding = bindings[0]
if binding.get("role") != "roles/run.invoker" or binding.get("condition") is not None:
    raise SystemExit("synthetic Job invoker binding differs")
if (binding.get("members") or []) != [sys.argv[1]]:
    raise SystemExit("synthetic Job invoker member inventory differs")
' "serviceAccount:${SYNTHETIC_RUN_SERVICE_ACCOUNT}"; then
    echo "ERROR: Cloud Run Job ${region}/${job_name} IAM is not the exact singleton invoker policy" >&2
    return 1
  fi
}

ensure_synthetic_job_invoker() {
  local job_name="$1"
  local region="$2"
  local member="serviceAccount:${SYNTHETIC_RUN_SERVICE_ACCOUNT}"
  gc run jobs add-iam-policy-binding "$job_name" \
    --region="$region" \
    --member="$member" \
    --role="roles/run.invoker" \
    --condition=None \
    --quiet >/dev/null
  verify_exact_synthetic_job_invoker_policy "$job_name" "$region"
}

verify_existing_synthetic_job_invoker_or_absent() {
  local job_name="$1"
  local region="$2"
  local describe_error=""
  if describe_error="$(gc run jobs describe "$job_name" \
      --region="$region" --format='value(metadata.name)' 2>&1)"; then
    verify_exact_synthetic_job_invoker_policy "$job_name" "$region"
    return
  fi
  case "$describe_error" in
    *NOT_FOUND*|*not\ found*|*Not\ Found*|*Cannot\ find*|*could\ not\ be\ found*|*was\ not\ found*)
      return 0
      ;;
    *)
      echo "ERROR: cannot determine whether Cloud Run job ${region}/${job_name} exists: ${describe_error}" >&2
      return 1
      ;;
  esac
}

upsert_scheduler() {
  local scheduler_name="$1"
  local job_name="$2"
  local region="$3"
  local schedule="$4"
  local run_uri="https://${region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${job_name}:run"

  if gc scheduler jobs describe "$scheduler_name" --location "$region" >/dev/null 2>&1; then
    log "updating synthetic scheduler ${scheduler_name}"
    if ! gc scheduler jobs update http "$scheduler_name" \
      --location "$region" \
      --schedule "$schedule" \
      --time-zone Etc/UTC \
      --uri "$run_uri" \
      --http-method POST \
      --oauth-service-account-email "$SYNTHETIC_RUN_SERVICE_ACCOUNT" \
      --clear-headers \
      --clear-message-body \
      --attempt-deadline=300s \
      --max-retry-attempts=0 \
      --max-retry-duration=0s \
      --min-backoff=5s \
      --max-backoff=60s \
      --max-doublings=3 \
      --quiet >/dev/null; then
      log "WARN: failed to update synthetic scheduler ${scheduler_name}; leaving existing schedule in place"
      return 1
    fi
  else
    log "creating synthetic scheduler ${scheduler_name}"
    if ! gc scheduler jobs create http "$scheduler_name" \
      --location "$region" \
      --schedule "$schedule" \
      --time-zone Etc/UTC \
      --uri "$run_uri" \
      --http-method POST \
      --oauth-service-account-email "$SYNTHETIC_RUN_SERVICE_ACCOUNT" \
      --attempt-deadline=300s \
      --max-retry-attempts=0 \
      --max-retry-duration=0s \
      --min-backoff=5s \
      --max-backoff=60s \
      --max-doublings=3 \
      --quiet >/dev/null; then
      log "WARN: failed to create synthetic scheduler ${scheduler_name}; deploy the job exists but is not scheduled"
      return 1
    fi
  fi
}

IFS=',' read -ra _REGION_LIST <<<"$TR_SYNTHETIC_MONITOR_REGIONS"
monitor_index=0
for monitor_region in "${_REGION_LIST[@]}"; do
  [ -n "$monitor_region" ] || continue
  regional_ingest_base="https://${SYNTHETIC_INGEST_SERVICE}-${PROJECT_NUMBER}.${monitor_region}.run.app"
  job_name="trusted-router-synthetic-${monitor_region//[^a-zA-Z0-9-]/-}"
  scheduler_name="${job_name}-every-three-minutes"
  legacy_scheduler_names=(
    "${job_name}-every-minute"
    "${job_name}-every-five-minutes"
  )
  env_vars=(
    "${BASE_ENV_VARS[@]}"
    "TR_SYNTHETIC_MONITOR_REGION=${monitor_region}"
    # Probe the public control-plane domain, but ingest through the regional
    # Cloud Run URL. Otherwise an apex TLS/DNS incident prevents the monitor
    # from recording the very failure it observed.
    "TR_SYNTHETIC_INGEST_URL=${regional_ingest_base}/v1/internal/synthetic/samples"
    "TR_SYNTHETIC_CONTROL_PLANE_HEALTH_URL=${regional_ingest_base}"
    "TR_SYNTHETIC_BENCHMARK_INGEST_URL=${regional_ingest_base}/v1/internal/synthetic/benchmark"
    "TR_SYNTHETIC_ROUTE_HEALTH_URL=${regional_ingest_base}/v1/internal/synthetic/route-health"
    "TR_SYNTHETIC_BILLING_CONCURRENCY=2"
    "TR_SYNTHETIC_START_DELAY_SECONDS=$((monitor_index * 20))"
    # Short random provider/model probes feed uptime and TTFT. Sustained
    # throughput is deliberately disabled in these health jobs.
    "TR_SYNTHETIC_ROTATION_ENABLED=true"
    # The three-minute cadence keeps regional health inside the five-minute
    # freshness contract despite Cloud Run startup latency. Two rotations per
    # pass keeps provider probe volume close to the old 4-per-5-minute rate.
    "TR_SYNTHETIC_ROTATION_PER_PASS=2"
    "TR_SYNTHETIC_THROUGHPUT_ENABLED=false"
    "TR_SYNTHETIC_THROUGHPUT_ONLY=false"
  )
  if [ "$monitor_region" = "$TR_PRIMARY_REGION" ]; then
    env_vars+=(
      "TR_SYNTHETIC_REMEDIATOR_URL=${regional_ingest_base}/v1/internal/synthetic/remediate"
    )
  fi
  set_env_vars="$(IFS='|'; echo "^|^${env_vars[*]}")"

  ensure_private_run_app_access "$monitor_region"
  verify_synthetic_ingest_service_contract "$monitor_region"
  verify_synthetic_secret_access
  verify_existing_synthetic_job_invoker_or_absent "$job_name" "$monitor_region"
  log "deploying synthetic Cloud Run job ${job_name} in ${monitor_region}"
  gc run jobs deploy "$job_name" \
    --region "$monitor_region" \
    --image "$IMAGE" \
    --command="/app/.venv/bin/python" \
    --args="-m,trusted_router.synthetic.cli" \
    --service-account "$SYNTHETIC_RUN_SERVICE_ACCOUNT" \
    "${PRIVATE_RUN_APP_JOB_NETWORK_ARGS[@]}" \
    --set-env-vars "$set_env_vars" \
    --set-secrets "$SET_SECRETS" \
    --tasks 1 \
    --parallelism 1 \
    --max-retries 0 \
    --task-timeout 300s \
    --cpu 2 \
    --memory 1Gi \
    --quiet >/dev/null
  verify_synthetic_job_secret_contract "$job_name" "$monitor_region"
  ensure_synthetic_job_invoker "$job_name" "$monitor_region"
  # CPU 2 + 1Gi mem: the synthetic CLI fans out N probes per pass
  # concurrently via asyncio.gather + httpx. On 1 CPU / 512Mi (the
  # Cloud Run default) the parallel TLS handshakes serialize and
  # probe latency balloons from ~2s to ~12s, blowing past task-
  # timeout. 2 CPU / 1Gi keeps the concurrent regional probes bounded.

  upsert_scheduler "$scheduler_name" "$job_name" "$monitor_region" "*/3 * * * *"
  for legacy_scheduler_name in "${legacy_scheduler_names[@]}"; do
    if gc scheduler jobs describe \
      "$legacy_scheduler_name" \
      --location "$monitor_region" >/dev/null 2>&1; then
      log "deleting legacy synthetic scheduler ${legacy_scheduler_name}"
      gc scheduler jobs delete \
        "$legacy_scheduler_name" \
        --location "$monitor_region" \
        --quiet >/dev/null
    fi
  done
  monitor_index=$((monitor_index + 1))
done

# Sustained-output benchmark: one deterministic top-200 route per tick. This
# has a separate Cloud Run Job so a slow 512-token stream cannot delay or
# overlap TLS, attestation, billing, fallback, or short provider probes.
throughput_region="$TR_SYNTHETIC_THROUGHPUT_REGION"
throughput_ingest_base="https://${SYNTHETIC_INGEST_SERVICE}-${PROJECT_NUMBER}.${throughput_region}.run.app"
throughput_job_name="trusted-router-throughput-${throughput_region}"
throughput_scheduler_name="${throughput_job_name}-every-five-minutes"
legacy_throughput_scheduler_names=(
  "${throughput_job_name}-every-minute"
  "${throughput_job_name}-every-two-minutes"
)
throughput_env_vars=(
  "${BASE_ENV_VARS[@]}"
  "TR_SYNTHETIC_MONITOR_REGION=${throughput_region}"
  "TR_SYNTHETIC_INGEST_URL=${throughput_ingest_base}/v1/internal/synthetic/samples"
  "TR_SYNTHETIC_CONTROL_PLANE_HEALTH_URL=${throughput_ingest_base}"
  "TR_SYNTHETIC_BENCHMARK_INGEST_URL=${throughput_ingest_base}/v1/internal/synthetic/benchmark"
  "TR_SYNTHETIC_ROUTE_HEALTH_URL=${throughput_ingest_base}/v1/internal/synthetic/route-health"
  "TR_SYNTHETIC_BILLING_CONCURRENCY=1"
  # This isolated job does not contend with the regional health jobs, so it
  # does not need their startup staggering. The probe itself gets 210 seconds
  # and the task keeps another 90 seconds for cold start and sample ingestion.
  "TR_SYNTHETIC_START_DELAY_SECONDS=0"
  "TR_SYNTHETIC_ROTATION_ENABLED=false"
  "TR_SYNTHETIC_ROTATION_PER_PASS=0"
  "TR_SYNTHETIC_THROUGHPUT_ENABLED=true"
  "TR_SYNTHETIC_THROUGHPUT_ONLY=true"
  "TR_SYNTHETIC_THROUGHPUT_REGION=${throughput_region}"
  "TR_SYNTHETIC_THROUGHPUT_ROUTE_LIMIT=200"
  "TR_SYNTHETIC_THROUGHPUT_MAX_TOKENS=512"
  "TR_SYNTHETIC_THROUGHPUT_MINIMUM_OUTPUT_TOKENS=128"
  "TR_SYNTHETIC_THROUGHPUT_TIMEOUT_SECONDS=90"
  "TR_SYNTHETIC_THROUGHPUT_TIMEOUT_CEILING_SECONDS=210"
  "TR_SYNTHETIC_THROUGHPUT_INTERVAL_SECONDS=300"
)
throughput_set_env_vars="$(IFS='|'; echo "^|^${throughput_env_vars[*]}")"

ensure_private_run_app_access "$throughput_region"
verify_synthetic_ingest_service_contract "$throughput_region"
verify_synthetic_secret_access
verify_existing_synthetic_job_invoker_or_absent \
  "$throughput_job_name" "$throughput_region"
log "deploying isolated throughput Cloud Run job ${throughput_job_name}"
gc run jobs deploy "$throughput_job_name" \
  --region "$throughput_region" \
  --image "$IMAGE" \
  --command="/app/.venv/bin/python" \
  --args="-m,trusted_router.synthetic.cli" \
  --service-account "$SYNTHETIC_RUN_SERVICE_ACCOUNT" \
  "${PRIVATE_RUN_APP_JOB_NETWORK_ARGS[@]}" \
  --set-env-vars "$throughput_set_env_vars" \
  --set-secrets "$SET_SECRETS" \
  --tasks 1 \
  --parallelism 1 \
  --max-retries 0 \
  --task-timeout 300s \
  --cpu 1 \
  --memory 1Gi \
  --quiet >/dev/null
verify_synthetic_job_secret_contract "$throughput_job_name" "$throughput_region"
ensure_synthetic_job_invoker "$throughput_job_name" "$throughput_region"

upsert_scheduler \
  "$throughput_scheduler_name" \
  "$throughput_job_name" \
  "$throughput_region" \
  "*/5 * * * *"

# Avoid double-sampling after cadence changes. Delete every historical
# throughput scheduler only after the canonical scheduler is healthy.
for legacy_throughput_scheduler_name in "${legacy_throughput_scheduler_names[@]}"; do
  if gc scheduler jobs describe \
    "$legacy_throughput_scheduler_name" \
    --location "$throughput_region" >/dev/null 2>&1; then
    log "deleting legacy throughput scheduler ${legacy_throughput_scheduler_name}"
    gc scheduler jobs delete \
      "$legacy_throughput_scheduler_name" \
      --location "$throughput_region" \
      --quiet >/dev/null
  fi
done

# Image generation is materially more expensive than text PONG probes. Keep it
# isolated and run one canonical end-to-end request every six hours.
image_region="$TR_SYNTHETIC_IMAGE_REGION"
image_ingest_base="https://${SYNTHETIC_INGEST_SERVICE}-${PROJECT_NUMBER}.${image_region}.run.app"
image_job_name="trusted-router-image-generation-${image_region}"
image_scheduler_name="${image_job_name}-every-six-hours"
image_env_vars=(
  "${BASE_ENV_VARS[@]}"
  "TR_SYNTHETIC_MONITOR_REGION=${image_region}"
  "TR_SYNTHETIC_INGEST_URL=${image_ingest_base}/v1/internal/synthetic/samples"
  "TR_SYNTHETIC_CONTROL_PLANE_HEALTH_URL=${image_ingest_base}"
  "TR_SYNTHETIC_IMAGE_MODEL=google/gemini-3.1-flash-image-preview"
  "TR_SYNTHETIC_IMAGE_PROVIDER=google-ai-studio"
  "TR_SYNTHETIC_IMAGE_TIMEOUT_SECONDS=120"
  "TR_SYNTHETIC_IMAGE_CONFIRMATION_DELAY_SECONDS=2"
)
image_set_env_vars="$(IFS='|'; echo "^|^${image_env_vars[*]}")"

ensure_private_run_app_access "$image_region"
verify_synthetic_ingest_service_contract "$image_region"
verify_synthetic_secret_access
verify_existing_synthetic_job_invoker_or_absent "$image_job_name" "$image_region"
log "deploying isolated image-generation Cloud Run job ${image_job_name}"
gc run jobs deploy "$image_job_name" \
  --region "$image_region" \
  --image "$IMAGE" \
  --command="/app/.venv/bin/python" \
  --args="-m,trusted_router.synthetic.image_generation" \
  --service-account "$SYNTHETIC_RUN_SERVICE_ACCOUNT" \
  "${PRIVATE_RUN_APP_JOB_NETWORK_ARGS[@]}" \
  --set-env-vars "$image_set_env_vars" \
  --set-secrets "$SET_SECRETS" \
  --tasks 1 \
  --parallelism 1 \
  --max-retries 0 \
  --task-timeout 300s \
  --cpu 1 \
  --memory 512Mi \
  --quiet >/dev/null
verify_synthetic_job_secret_contract "$image_job_name" "$image_region"
ensure_synthetic_job_invoker "$image_job_name" "$image_region"

upsert_scheduler \
  "$image_scheduler_name" \
  "$image_job_name" \
  "$image_region" \
  "17 */6 * * *"

# Video generation is the most expensive synthetic. Run exactly one shortest-
# valid direct generation per day and rotate through seven providers weekly.
# The current seven-day total is $2.499276 including TrustedRouter's 20% fee,
# or about $10.71 per 30 days. max-retries=0 plus a date-scoped idempotency key
# prevents duplicate billing.
video_region="$TR_SYNTHETIC_VIDEO_REGION"
video_ingest_base="https://${SYNTHETIC_INGEST_SERVICE}-${PROJECT_NUMBER}.${video_region}.run.app"
video_job_name="trusted-router-video-generation-${video_region}"
video_scheduler_name="${video_job_name}-daily"
video_env_vars=(
  "${BASE_ENV_VARS[@]}"
  "TR_SYNTHETIC_MONITOR_REGION=${video_region}"
  "TR_SYNTHETIC_INGEST_URL=${video_ingest_base}/v1/internal/synthetic/samples"
  "TR_SYNTHETIC_CONTROL_PLANE_HEALTH_URL=${video_ingest_base}"
  "TR_SYNTHETIC_VIDEO_TIMEOUT_SECONDS=900"
  "TR_SYNTHETIC_VIDEO_POLL_INTERVAL_SECONDS=5"
)
video_set_env_vars="$(IFS='|'; echo "^|^${video_env_vars[*]}")"

ensure_private_run_app_access "$video_region"
verify_synthetic_ingest_service_contract "$video_region"
verify_synthetic_secret_access
verify_existing_synthetic_job_invoker_or_absent "$video_job_name" "$video_region"
log "deploying isolated daily video-generation Cloud Run job ${video_job_name}"
gc run jobs deploy "$video_job_name" \
  --region "$video_region" \
  --image "$IMAGE" \
  --command="/app/.venv/bin/python" \
  --args="-m,trusted_router.synthetic.video_generation" \
  --service-account "$SYNTHETIC_RUN_SERVICE_ACCOUNT" \
  "${PRIVATE_RUN_APP_JOB_NETWORK_ARGS[@]}" \
  --set-env-vars "$video_set_env_vars" \
  --set-secrets "$SET_SECRETS" \
  --tasks 1 \
  --parallelism 1 \
  --max-retries 0 \
  --task-timeout 1200s \
  --cpu 1 \
  --memory 512Mi \
  --quiet >/dev/null
verify_synthetic_job_secret_contract "$video_job_name" "$video_region"
ensure_synthetic_job_invoker "$video_job_name" "$video_region"

upsert_scheduler \
  "$video_scheduler_name" \
  "$video_job_name" \
  "$video_region" \
  "41 9 * * *"
