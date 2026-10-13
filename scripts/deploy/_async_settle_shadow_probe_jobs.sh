# shellcheck shell=bash
# Dedicated cohort only. This library has no top-level cloud calls.
# The standalone bootstrap mirrors the regional monitor setup; parity tests
# pin the unchanged monitor helper definitions below to synthetic.sh.

verify_synthetic_ingest_service_contract() {
  local target_region="$1"
  local service_json=""

  service_json="$(gc run services describe "$SYNTHETIC_INGEST_SERVICE" \
    --region "$target_region" \
    --format=json)" || {
      echo "ERROR: internal synthetic ingest service is absent in ${target_region}" >&2
      return 1
    }
  if ! printf '%s' "$service_json" | python3 -c '
import json
import sys

data = json.load(sys.stdin)
expected_name, expected_observer_secret, expected_gateway_secret = sys.argv[1:4]
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
template = data.get("spec", {}).get("template", {})
pod_spec = template.get("spec", template)
containers = pod_spec.get("containers", [])
if len(containers) != 1:
    raise SystemExit("service must have exactly one container")
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
' "$SYNTHETIC_INGEST_SERVICE" \
      trustedrouter-observer-internal-token \
      trustedrouter-internal-gateway-token; then
    echo "ERROR: internal synthetic ingest service contract failed in ${target_region}" >&2
    return 1
  fi
}

prepare_synthetic_ingest_target() {
  local target_region="$1"

  if [ "$DEPLOYED_COMBINED_BRIDGE" = "true" ]; then
    # The combined service uses ingress=all in every region, matching the pre-#714
    # jobs' public run.app path; VPC/private-ingress setup belongs to the split path.
    return
  fi
  ensure_private_run_app_access "$target_region"
  verify_synthetic_ingest_service_contract "$target_region"
}

synthetic_ingest_base_for_region() {
  local target_region="$1"
  if [ "$DEPLOYED_COMBINED_BRIDGE" = "true" ]; then
    # The combined service now uses private Cloud Run ingress. Its run.app URL
    # intentionally returns a Google platform 404, including to Cloud Run jobs
    # without the split service's VPC path. Reach the same token-protected
    # internal routes through the external load balancer until the dedicated
    # internal observer service is active in every region.
    printf '%s\n' "https://trustedrouter.com"
  else
    printf 'https://%s-%s.%s.run.app\n' \
      "$SYNTHETIC_INGEST_SERVICE" "$PROJECT_NUMBER" "$target_region"
  fi
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
      --uri "$run_uri" \
      --http-method POST \
      --oauth-service-account-email "$RUN_SERVICE_ACCOUNT" \
      --quiet >/dev/null; then
      log "WARN: failed to update synthetic scheduler ${scheduler_name}; leaving existing schedule in place"
      return 1
    fi
  else
    log "creating synthetic scheduler ${scheduler_name}"
    if ! gc scheduler jobs create http "$scheduler_name" \
      --location "$region" \
      --schedule "$schedule" \
      --uri "$run_uri" \
      --http-method POST \
      --oauth-service-account-email "$RUN_SERVICE_ACCOUNT" \
      --quiet >/dev/null; then
      log "WARN: failed to create synthetic scheduler ${scheduler_name}; deploy the job exists but is not scheduled"
      return 1
    fi
  fi
}

async_settle_shadow_probe_setup() {
  if [ "${TR_ALLOW_DEPLOYED_COMBINED_SURFACE:-}" = "true" ]; then
    DEPLOYED_COMBINED_BRIDGE=true
    SYNTHETIC_INGEST_SERVICE="$SERVICE"
  else
    DEPLOYED_COMBINED_BRIDGE=false
    [ -n "${TR_BILLING_SERVICE:-}" ] && [ "$TR_BILLING_SERVICE" != "$SERVICE" ] || {
      echo "ERROR: a separate TR_BILLING_SERVICE or explicit combined bridge is required" >&2
      return 1
    }
    case "$TR_BILLING_SERVICE" in
      *[!a-zA-Z0-9-]*) return 2 ;;
    esac
    SYNTHETIC_INGEST_SERVICE="$TR_BILLING_SERVICE"
    gc secrets describe trustedrouter-observer-internal-token >/dev/null
  fi
  gc artifacts docker images describe "$IMAGE" >/dev/null
  SYNTHETIC_MONITOR_REGIONS="${TR_SYNTHETIC_MONITOR_REGIONS:-us-central1,europe-west4}"
  IFS=',' read -ra _REGION_LIST <<<"$SYNTHETIC_MONITOR_REGIONS"
}

async_settle_shadow_probe_jobs() {
  if ! gc secrets describe trustedrouter-async-settle-shadow-probe-api-key >/dev/null 2>&1; then
    [ "${1:-optional}" = optional ] && return 0
    echo "ERROR: dedicated shadow probe secret is required" >&2
    return 1
  fi
  if [ "${1:-optional}" = required ]; then
    async_settle_shadow_probe_setup
  fi
  shadow_cadence="${TR_ASYNC_SETTLE_SHADOW_PROBE_CADENCE_MINUTES:-4}"
  shadow_repetitions="${TR_ASYNC_SETTLE_SHADOW_PROBE_REPETITIONS:-1}"
  case "$shadow_cadence" in
    1|2|3|4|5|6|10|12|15|20|30) ;;
    *) echo "ERROR: shadow cadence must divide 60 minutes" >&2; exit 2 ;;
  esac
  case "$shadow_repetitions" in
    [1-8]) ;;
    *) echo "ERROR: shadow repetitions must be between 1 and 8" >&2; exit 2 ;;
  esac
  shadow_surface="observer"
  shadow_secrets="/var/run/secrets/shadow-probe/api-key=trustedrouter-async-settle-shadow-probe-api-key:latest"
  if [ "$DEPLOYED_COMBINED_BRIDGE" = "true" ]; then
    shadow_surface="combined"
    shadow_secrets+=",TR_INTERNAL_GATEWAY_TOKEN=trustedrouter-internal-gateway-token:latest"
  else
    shadow_secrets+=",TR_OBSERVER_INTERNAL_TOKEN=trustedrouter-observer-internal-token:latest"
  fi
  for shadow_region in "${_REGION_LIST[@]}"; do
    [ -n "$shadow_region" ] || continue
    shadow_job="trusted-router-async-settle-shadow-probe-${shadow_region}"
    shadow_ingest="$(synthetic_ingest_base_for_region "$shadow_region")"
    shadow_env=(
      "TR_ENVIRONMENT=worker"
      "TR_SERVICE_SURFACE=${shadow_surface}"
      "TR_STORAGE_BACKEND=memory"
      "TR_ASYNC_SETTLE_SHADOW_PROBE_API_KEY_FILE=/var/run/secrets/shadow-probe/api-key"
      "TR_ENABLE_LIVE_PROVIDERS=false"
      "TR_SYNTHETIC_MONITOR_REGION=${shadow_region}"
      "TR_SYNTHETIC_INGEST_URL=${shadow_ingest}/v1/internal/synthetic/samples"
      "TR_ASYNC_SETTLE_SHADOW_PROBE_REPETITIONS=${shadow_repetitions}"
      "TR_ASYNC_SETTLE_SHADOW_PROBE_CADENCE_MINUTES=${shadow_cadence}"
    )
    if [ "$DEPLOYED_COMBINED_BRIDGE" = "true" ]; then
      shadow_env+=("TR_ALLOW_DEPLOYED_COMBINED_SURFACE=true" "TR_RATE_LIMIT_ENABLED=false")
    fi
    shadow_set_env="$(IFS='|'; echo "^|^${shadow_env[*]}")"
    prepare_synthetic_ingest_target "$shadow_region"
    gc run jobs deploy "$shadow_job" \
      --region "$shadow_region" --image "$IMAGE" \
      --command="/app/.venv/bin/python" \
      --args="-m,trusted_router.synthetic.cli,--async-settle-shadow-probe" \
      --service-account "$RUN_SERVICE_ACCOUNT" \
      "${PRIVATE_RUN_APP_JOB_NETWORK_ARGS[@]+"${PRIVATE_RUN_APP_JOB_NETWORK_ARGS[@]}"}" \
      --set-env-vars "$shadow_set_env" --set-secrets "$shadow_secrets" \
      --max-retries 0 --task-timeout "$((240 * shadow_repetitions + 60))s" --cpu 1 --memory 512Mi --quiet >/dev/null
    upsert_scheduler "${shadow_job}-scheduled" "$shadow_job" "$shadow_region" "*/${shadow_cadence} * * * *"
  done
}
