#!/usr/bin/env bash
# Phase 5: deploy scheduled synthetic monitor jobs and an isolated sustained
# throughput job. Jobs run outside the prompt path and write privacy-safe
# samples to internal ingest endpoints. Short uptime probes must never wait on
# the longer throughput benchmark.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

if ! gc secrets describe trustedrouter-synthetic-monitor-api-key >/dev/null 2>&1; then
  log "synthetic monitor key secret is missing; skipping synthetic monitor deploy"
  exit 0
fi

SECRET_ENVS=(
  "TR_SENTRY_DSN=trustedrouter-sentry-dsn:latest"
  "TR_STRIPE_SECRET_KEY=trustedrouter-stripe-secret-key:latest"
  "TR_STRIPE_WEBHOOK_SECRET=trustedrouter-stripe-webhook-secret:latest"
  "TR_INTERNAL_GATEWAY_TOKEN=trustedrouter-internal-gateway-token:latest"
  "TR_SYNTHETIC_MONITOR_API_KEY=trustedrouter-synthetic-monitor-api-key:latest"
)
add_secret_env_if_exists() {
  local env_name="$1"
  local secret_name="$2"
  if gc secrets describe "$secret_name" >/dev/null 2>&1; then
    SECRET_ENVS+=("${env_name}=${secret_name}:latest")
  fi
}
add_secret_env_if_exists "ANTHROPIC_API_KEY" "trustedrouter-anthropic-api-key"
add_secret_env_if_exists "OPENAI_API_KEY" "trustedrouter-openai-api-key"
add_secret_env_if_exists "GEMINI_API_KEY" "trustedrouter-gemini-api-key"
add_secret_env_if_exists "CEREBRAS_API_KEY" "trustedrouter-cerebras-api-key"
add_secret_env_if_exists "DEEPSEEK_API_KEY" "trustedrouter-deepseek-api-key"
add_secret_env_if_exists "MISTRAL_API_KEY" "trustedrouter-mistral-api-key"
add_secret_env_if_exists "KIMI_API_KEY" "trustedrouter-kimi-api-key"
add_secret_env_if_exists "ZAI_API_KEY" "trustedrouter-zai-api-key"
UPDATE_SECRETS="$(IFS=,; echo "${SECRET_ENVS[*]}")"

BASE_ENV_VARS=(
  # These are one-shot workers, not the public control-plane process. Using
  # the worker runtime keeps control-plane-only dependencies such as SES out
  # of the monitor containers while their actual storage and probe inputs
  # remain explicit below.
  "TR_ENVIRONMENT=worker"
  "TR_RELEASE=$(git rev-parse --short HEAD 2>/dev/null || echo local)"
  "TR_ENABLE_LIVE_PROVIDERS=false"
  "TR_API_BASE_URL=https://api.trustedrouter.com/v1"
  "TR_TRUSTED_DOMAIN=trustedrouter.com"
  "TR_STORAGE_BACKEND=spanner-bigtable"
  "TR_GCP_PROJECT_ID=${PROJECT_ID}"
  "TR_SPANNER_INSTANCE_ID=${SPANNER_INSTANCE_ID}"
  "TR_SPANNER_DATABASE_ID=${SPANNER_DATABASE_ID}"
  "TR_BIGTABLE_INSTANCE_ID=${BIGTABLE_INSTANCE_ID}"
  "TR_BIGTABLE_GENERATION_TABLE=${BIGTABLE_GENERATION_TABLE}"
  "TR_BYOK_KMS_KEY_NAME=${BYOK_KMS_KEY_NAME}"
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

if ! gc artifacts docker images describe "$IMAGE" >/dev/null 2>&1; then
  echo "ERROR: image ${IMAGE} does not exist. Run scripts/deploy/image.sh before synthetic.sh." >&2
  exit 1
fi

ensure_project_role "serviceAccount:${RUN_SERVICE_ACCOUNT}" "roles/run.developer"
ensure_project_role "serviceAccount:${RUN_SERVICE_ACCOUNT}" "roles/secretmanager.secretAccessor"

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

SYNTHETIC_MONITOR_REGIONS="${TR_SYNTHETIC_MONITOR_REGIONS:-us-central1,europe-west4}"
IFS=',' read -ra _REGION_LIST <<<"$SYNTHETIC_MONITOR_REGIONS"
monitor_index=0
for monitor_region in "${_REGION_LIST[@]}"; do
  [ -n "$monitor_region" ] || continue
  regional_ingest_base="https://${SERVICE}-${PROJECT_NUMBER}.${monitor_region}.run.app"
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
  set_env_vars="$(IFS='|'; echo "^|^${env_vars[*]}")"

  log "deploying synthetic Cloud Run job ${job_name} in ${monitor_region}"
  gc run jobs deploy "$job_name" \
    --region "$monitor_region" \
    --image "$IMAGE" \
    --command="/app/.venv/bin/python" \
    --args="-m,trusted_router.synthetic.cli" \
    --service-account "$RUN_SERVICE_ACCOUNT" \
    --set-env-vars "$set_env_vars" \
    --update-secrets "$UPDATE_SECRETS" \
    --max-retries 0 \
    --task-timeout 300s \
    --cpu 2 \
    --memory 1Gi \
    --quiet >/dev/null
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
throughput_region="us-central1"
throughput_ingest_base="https://${SERVICE}-${PROJECT_NUMBER}.${throughput_region}.run.app"
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

log "deploying isolated throughput Cloud Run job ${throughput_job_name}"
gc run jobs deploy "$throughput_job_name" \
  --region "$throughput_region" \
  --image "$IMAGE" \
  --command="/app/.venv/bin/python" \
  --args="-m,trusted_router.synthetic.cli" \
  --service-account "$RUN_SERVICE_ACCOUNT" \
  --set-env-vars "$throughput_set_env_vars" \
  --update-secrets "$UPDATE_SECRETS" \
  --max-retries 0 \
  --task-timeout 300s \
  --cpu 1 \
  --memory 1Gi \
  --quiet >/dev/null

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
image_region="us-central1"
image_ingest_base="https://${SERVICE}-${PROJECT_NUMBER}.${image_region}.run.app"
image_job_name="trusted-router-image-generation-${image_region}"
image_scheduler_name="${image_job_name}-every-six-hours"
image_env_vars=(
  "${BASE_ENV_VARS[@]}"
  "TR_SYNTHETIC_MONITOR_REGION=${image_region}"
  "TR_SYNTHETIC_INGEST_URL=${image_ingest_base}/v1/internal/synthetic/samples"
  "TR_SYNTHETIC_IMAGE_MODEL=google/gemini-3.1-flash-image-preview"
  "TR_SYNTHETIC_IMAGE_PROVIDER=google-ai-studio"
  "TR_SYNTHETIC_IMAGE_TIMEOUT_SECONDS=120"
  "TR_SYNTHETIC_IMAGE_CONFIRMATION_DELAY_SECONDS=2"
)
image_set_env_vars="$(IFS='|'; echo "^|^${image_env_vars[*]}")"

log "deploying isolated image-generation Cloud Run job ${image_job_name}"
gc run jobs deploy "$image_job_name" \
  --region "$image_region" \
  --image "$IMAGE" \
  --command="/app/.venv/bin/python" \
  --args="-m,trusted_router.synthetic.image_generation" \
  --service-account "$RUN_SERVICE_ACCOUNT" \
  --set-env-vars "$image_set_env_vars" \
  --update-secrets "$UPDATE_SECRETS" \
  --max-retries 0 \
  --task-timeout 300s \
  --cpu 1 \
  --memory 512Mi \
  --quiet >/dev/null

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
video_region="us-central1"
video_ingest_base="https://${SERVICE}-${PROJECT_NUMBER}.${video_region}.run.app"
video_job_name="trusted-router-video-generation-${video_region}"
video_scheduler_name="${video_job_name}-daily"
video_env_vars=(
  "${BASE_ENV_VARS[@]}"
  "TR_SYNTHETIC_MONITOR_REGION=${video_region}"
  "TR_SYNTHETIC_INGEST_URL=${video_ingest_base}/v1/internal/synthetic/samples"
  "TR_SYNTHETIC_VIDEO_TIMEOUT_SECONDS=900"
  "TR_SYNTHETIC_VIDEO_POLL_INTERVAL_SECONDS=5"
)
video_set_env_vars="$(IFS='|'; echo "^|^${video_env_vars[*]}")"

log "deploying isolated daily video-generation Cloud Run job ${video_job_name}"
gc run jobs deploy "$video_job_name" \
  --region "$video_region" \
  --image "$IMAGE" \
  --command="/app/.venv/bin/python" \
  --args="-m,trusted_router.synthetic.video_generation" \
  --service-account "$RUN_SERVICE_ACCOUNT" \
  --set-env-vars "$video_set_env_vars" \
  --update-secrets "$UPDATE_SECRETS" \
  --max-retries 0 \
  --task-timeout 1200s \
  --cpu 1 \
  --memory 512Mi \
  --quiet >/dev/null

upsert_scheduler \
  "$video_scheduler_name" \
  "$video_job_name" \
  "$video_region" \
  "41 9 * * *"
