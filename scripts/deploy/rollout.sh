#!/usr/bin/env bash
# Phase 4: parallel Cloud Run rollout across every control-plane region, then
# attach a Serverless NEG per region to the global LB backend service so
# trustedrouter.com routes to the nearest healthy region. Finally ensures the
# HTTP -> HTTPS redirect on :80.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

TRUST_SOURCE_COMMIT=""
TRUST_IMAGE_REFERENCE=""
TRUST_IMAGE_DIGEST=""
TRUST_JSON=""
if [ -f "$TRUST_FILE" ]; then
  TRUST_JSON="$(cat "$TRUST_FILE")"
elif [ -n "${TRUST_FILE_URL:-}" ]; then
  TRUST_JSON="$(curl -fsSL --max-time 10 "$TRUST_FILE_URL" 2>/dev/null || true)"
fi
if [ -n "$TRUST_JSON" ]; then
  TRUST_SOURCE_COMMIT="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("source_commit", ""))' <<<"$TRUST_JSON")"
  TRUST_IMAGE_REFERENCE="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("image_reference", ""))' <<<"$TRUST_JSON")"
  TRUST_IMAGE_DIGEST="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("image_digest", ""))' <<<"$TRUST_JSON")"
fi


SECRET_ENVS=(
  "TR_SENTRY_DSN=trustedrouter-sentry-dsn:latest"
  "TR_STRIPE_SECRET_KEY=trustedrouter-stripe-secret-key:latest"
  "TR_STRIPE_WEBHOOK_SECRET=trustedrouter-stripe-webhook-secret:latest"
  "TR_INTERNAL_GATEWAY_TOKEN=trustedrouter-internal-gateway-token:latest"
)
# Retired environment bindings remain on Cloud Run until explicitly removed.
REMOVE_SECRET_ENVS=("TR_GOOGLE_ADS_CONVERSION_FEED_PASSWORD")
add_secret_env_if_exists() {
  local env_name="$1"
  local secret_name="$2"
  local describe_error=""
  if describe_error="$(gc secrets describe "$secret_name" 2>&1)"; then
    SECRET_ENVS+=("${env_name}=${secret_name}:latest")
  elif [[ "$describe_error" == *"NOT_FOUND"* ]] || \
       [[ "$describe_error" == *"not found"* ]]; then
    # gcloud --update-secrets preserves old bindings. Explicit removal keeps
    # a deleted optional secret from making an otherwise healthy revision
    # unroutable in regions that still carry the stale environment entry.
    REMOVE_SECRET_ENVS+=("${env_name}")
  else
    log "cannot determine whether optional secret ${secret_name} exists"
    return 1
  fi
}
# Carrier credentials for /v1/notify. Optional bindings: if the secrets are
# absent the notify channels report themselves unconfigured rather than the
# revision failing to start, which is the right failure for a feature nobody
# has enabled yet.
add_secret_env_if_exists "TR_TELNYX_API_KEY" "trustedrouter-telnyx-api-key"
add_secret_env_if_exists "TR_VERIFF_API_KEY" "trustedrouter-veriff-api-key"
add_secret_env_if_exists \
  "TR_VERIFF_SHARED_SECRET_KEY" \
  "trustedrouter-veriff-shared-secret-key"
add_secret_env_if_exists "TR_TWILIO_ACCOUNT_SID" "trustedrouter-twilio-account-sid"
add_secret_env_if_exists "TR_TWILIO_API_KEY_SID" "trustedrouter-twilio-api-key-sid"
add_secret_env_if_exists "TR_TWILIO_API_KEY_SECRET" "trustedrouter-twilio-api-key-secret"
add_secret_env_if_exists "TR_TWILIO_AUTH_TOKEN" "trustedrouter-twilio-auth-token"
add_secret_env_if_exists "ANTHROPIC_API_KEY" "trustedrouter-anthropic-api-key"
add_secret_env_if_exists "OPENAI_API_KEY" "trustedrouter-openai-api-key"
add_secret_env_if_exists "GEMINI_API_KEY" "trustedrouter-gemini-api-key"
add_secret_env_if_exists "CEREBRAS_API_KEY" "trustedrouter-cerebras-api-key"
add_secret_env_if_exists "DEEPSEEK_API_KEY" "trustedrouter-deepseek-api-key"
add_secret_env_if_exists "MISTRAL_API_KEY" "trustedrouter-mistral-api-key"
add_secret_env_if_exists "KIMI_API_KEY" "trustedrouter-kimi-api-key"
add_secret_env_if_exists "ZAI_API_KEY" "trustedrouter-zai-api-key"
add_secret_env_if_exists "TOGETHER_API_KEY" "trustedrouter-together-api-key"
add_secret_env_if_exists "FIREWORKS_API_KEY" "trustedrouter-fireworks-api-key"
add_secret_env_if_exists "DEEPINFRA_API_KEY" "trustedrouter-deepinfra-api-key"
# 2026-05 — six new backends.
add_secret_env_if_exists "GROK_API_KEY" "trustedrouter-grok-api-key"
add_secret_env_if_exists "NOVITA_API_KEY" "trustedrouter-novita-api-key"
add_secret_env_if_exists "PHALA_API_KEY" "trustedrouter-phala-api-key"
add_secret_env_if_exists "SILICON_FLOW_API_KEY" "trustedrouter-siliconflow-api-key"
add_secret_env_if_exists "TINFOIL_API_KEY" "trustedrouter-tinfoil-api-key"
add_secret_env_if_exists "VENICE_API_KEY" "trustedrouter-venice-api-key"
add_secret_env_if_exists "NEBIUS_API_KEY" "trustedrouter-nebius-api-key"
add_secret_env_if_exists "MINIMAX_API_KEY" "trustedrouter-minimax-api-key"
add_secret_env_if_exists "BASETEN_API_KEY" "trustedrouter-baseten-api-key"
add_secret_env_if_exists "TELNYX_API_KEY" "trustedrouter-telnyx-api-key"
add_secret_env_if_exists "THINKING_MACHINES_API_KEY" "trustedrouter-thinking-machines-api-key"
add_secret_env_if_exists "WAFER_API_KEY" "trustedrouter-wafer-api-key"
add_secret_env_if_exists "CRUSOE_API_KEY" "trustedrouter-crusoe-api-key"
add_secret_env_if_exists "MAKORA_API_KEY" "trustedrouter-makora-api-key"
add_secret_env_if_exists "ALIBABA_API_KEY" "trustedrouter-alibaba-api-key"
add_secret_env_if_exists "ENGY_API_KEY" "trustedrouter-engy-api-key"
add_secret_env_if_exists "ZERO_G_API_KEY" "trustedrouter-zero-g-api-key"
add_secret_env_if_exists "TR_SYNTHETIC_MONITOR_API_KEY" "trustedrouter-synthetic-monitor-api-key"
# HOME side of lazy key federation: peers present this token to
# /v1/internal/federation/resolve-key and get identity + limits, never
# credits and never key material. Setting it is what turns federation
# serving ON for this plane (unset = 403 for every peer).
add_secret_env_if_exists "TR_FEDERATION_PEER_TOKEN" "trustedrouter-federation-peer-token"
# HOME side of deferred settlement: the per-peer token map
# ("plane=token,plane=token"). Which token authenticated IS the source
# plane's identity; the request body never carries it. Setting this is what
# turns /v1/internal/federation/apply-usage serving ON (unset = 403).
add_secret_env_if_exists \
  "TR_FEDERATION_SETTLEMENT_INBOUND_TOKENS" \
  "trustedrouter-federation-settlement-inbound-tokens"
add_secret_env_if_exists "TR_GOOGLE_CLIENT_ID" "trustedrouter-google-client-id"
add_secret_env_if_exists "TR_GOOGLE_CLIENT_SECRET" "trustedrouter-google-client-secret"
add_secret_env_if_exists \
  "TR_GOOGLE_ALIAS_CREDENTIALS_JSON" \
  "trustedrouter-google-alias-credentials-json"
add_secret_env_if_exists "TR_GITHUB_CLIENT_ID" "trustedrouter-github-client-id"
add_secret_env_if_exists "TR_GITHUB_CLIENT_SECRET" "trustedrouter-github-client-secret"
add_secret_env_if_exists \
  "TR_GITHUB_ALIAS_CREDENTIALS_JSON" \
  "trustedrouter-github-alias-credentials-json"
# SES email credentials only; not used for AWS hosting or failover.
add_secret_env_if_exists "TR_AWS_ACCESS_KEY_ID" "trustedrouter-aws-access-key-id"
add_secret_env_if_exists "TR_AWS_SECRET_ACCESS_KEY" "trustedrouter-aws-secret-access-key"
add_secret_env_if_exists \
  "TR_OPS_CHAT_WEBHOOK_SECRET" \
  "trustedrouter-ops-chat-webhook-secret"
add_secret_env_if_exists "TR_PAYPAL_CLIENT_ID" "trustedrouter-paypal-client-id"
add_secret_env_if_exists "TR_PAYPAL_CLIENT_SECRET" "trustedrouter-paypal-client-secret"
add_secret_env_if_exists "TR_PAYPAL_WEBHOOK_ID" "trustedrouter-paypal-webhook-id"
add_secret_env_if_exists "TR_ADYEN_API_KEY" "trustedrouter-adyen-test-api-key"
add_secret_env_if_exists "TR_ADYEN_CLIENT_KEY" "trustedrouter-adyen-test-client-key"
add_secret_env_if_exists "TR_ADYEN_HMAC_KEY" "trustedrouter-adyen-test-hmac-key"
add_secret_env_if_exists \
  "TR_ADYEN_REFERENCE_KEY" "trustedrouter-adyen-test-reference-key"
add_secret_env_if_exists "AXIOM_API_TOKEN" "trustedrouter-axiom-api-token"
add_secret_env_if_exists "TR_ATHENA_WORKER_PROMPT" "trustedrouter-athena-worker-prompt-v1"
add_secret_env_if_exists \
  "TR_PROVIDER_ANALYTICS_CLICKHOUSE_PASSWORD" \
  "trustedrouter-clickhouse-provider-read-password"
add_secret_env_if_exists \
  "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD" \
  "trustedrouter-clickhouse-control-read-password"
UPDATE_SECRETS="$(IFS=,; echo "${SECRET_ENVS[*]}")"
REMOVE_SECRETS_ARGS=()
if [ "${#REMOVE_SECRET_ENVS[@]}" -gt 0 ]; then
  REMOVE_SECRETS_ARGS=(--remove-secrets "$(IFS=,; echo "${REMOVE_SECRET_ENVS[*]}")")
fi

REQUEST_RECORD_WRITE_MODE="${TR_REQUEST_RECORD_WRITE_MODE:-}"
if [ -z "$REQUEST_RECORD_WRITE_MODE" ]; then
  REQUEST_RECORD_WRITE_MODE="$(
    gc run services describe "$SERVICE" \
      --region="$TR_PRIMARY_REGION" \
      --format=json 2>/dev/null \
      | jq -r '
          [
            .spec.template.spec.containers[0].env[]?
            | select(.name == "TR_REQUEST_RECORD_WRITE_MODE")
            | .value
          ][0] // empty
        ' || true
  )"
fi
case "$REQUEST_RECORD_WRITE_MODE" in
  legacy|typed) ;;
  *)
    log "refusing rollout: cannot determine TR_REQUEST_RECORD_WRITE_MODE; set it explicitly"
    exit 1
    ;;
esac

LIVE_STORAGE_BACKEND="$(
  gc run services describe "$SERVICE" \
    --region="$TR_PRIMARY_REGION" \
    --format=json 2>/dev/null \
    | jq -r '
        [
          .spec.template.spec.containers[0].env[]?
          | select(.name == "TR_STORAGE_BACKEND")
          | .value
        ][0] // "spanner-bigtable"
      ' || true
)"
STORAGE_BACKEND="${TR_STORAGE_BACKEND:-${LIVE_STORAGE_BACKEND:-spanner-bigtable}}"
case "$STORAGE_BACKEND" in
  spanner-bigtable|spanner-clickhouse) ;;
  *)
    log "refusing rollout: TR_STORAGE_BACKEND must be spanner-bigtable or spanner-clickhouse"
    exit 1
    ;;
esac

LIVE_GENERATION_RECORDS_ENABLED="$(
  gc run services describe "$SERVICE" \
    --region="$TR_PRIMARY_REGION" \
    --format=json 2>/dev/null \
    | jq -r '
        [
          .spec.template.spec.containers[0].env[]?
          | select(.name == "TR_GENERATION_RECORDS_ENABLED")
          | .value
        ][0] // empty
      ' || true
)"
LIVE_BIGTABLE_MIRROR_WRITES_ENABLED="$(
  gc run services describe "$SERVICE" \
    --region="$TR_PRIMARY_REGION" \
    --format=json 2>/dev/null \
    | jq -r '
        [
          .spec.template.spec.containers[0].env[]?
          | select(.name == "TR_BIGTABLE_MIRROR_WRITES_ENABLED")
          | .value
        ][0] // empty
      ' || true
)"
GENERATION_RECORDS_ENABLED="${TR_GENERATION_RECORDS_ENABLED:-${LIVE_GENERATION_RECORDS_ENABLED:-true}}"
BIGTABLE_MIRROR_WRITES_ENABLED="${TR_BIGTABLE_MIRROR_WRITES_ENABLED:-${LIVE_BIGTABLE_MIRROR_WRITES_ENABLED:-true}}"
case "$GENERATION_RECORDS_ENABLED:$BIGTABLE_MIRROR_WRITES_ENABLED" in
  true:true|true:false|false:true|false:false) ;;
  *)
    log "refusing rollout: generation-record and Bigtable-mirror flags must be true or false"
    exit 1
    ;;
esac

LIVE_ANALYTICS_READ_MODE="$(
  gc run services describe "$SERVICE" \
    --region="$TR_PRIMARY_REGION" \
    --format=json 2>/dev/null \
    | jq -r '
        [
          .spec.template.spec.containers[0].env[]?
          | select(.name == "TR_ANALYTICS_READ_MODE")
          | .value
        ][0] // "bigtable"
      ' || true
)"
ANALYTICS_READ_MODE="${TR_ANALYTICS_READ_MODE:-$LIVE_ANALYTICS_READ_MODE}"
case "$ANALYTICS_READ_MODE" in
  bigtable|dual|clickhouse|clickhouse-only) ;;
  *)
    log "refusing rollout: invalid TR_ANALYTICS_READ_MODE"
    exit 1
    ;;
esac
if [ "$STORAGE_BACKEND" = "spanner-clickhouse" ] && \
   [ "$ANALYTICS_READ_MODE" != "clickhouse-only" ]; then
  log "refusing rollout: spanner-clickhouse requires clickhouse-only reads"
  exit 1
fi
if [ "$STORAGE_BACKEND" = "spanner-clickhouse" ] && \
   { [ "$BIGTABLE_MIRROR_WRITES_ENABLED" != "false" ] ||
     [ "$GENERATION_RECORDS_ENABLED" != "true" ] ||
     [ "$REQUEST_RECORD_WRITE_MODE" != "typed" ]; }; then
  log "refusing rollout: spanner-clickhouse requires typed generation records and no Bigtable mirror"
  exit 1
fi
if [ "$GENERATION_RECORDS_ENABLED" = "true" ]; then
  generation_table_count="$(gc spanner databases execute-sql "$SPANNER_DATABASE_ID" \
    --instance="$SPANNER_INSTANCE_ID" \
    --sql="SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE table_name='tr_generation'" \
    --format='value(rows[0])')"
  if [ "${generation_table_count:-0}" != "1" ]; then
    log "refusing rollout: tr_generation is missing; run migrate_generation_records.sh --apply"
    exit 1
  fi
fi

ANALYTICS_DUAL_READ_STARTED_AT="${TR_ANALYTICS_DUAL_READ_STARTED_AT:-}"
if [ -z "$ANALYTICS_DUAL_READ_STARTED_AT" ]; then
  ANALYTICS_DUAL_READ_STARTED_AT="$(
    gc run services describe "$SERVICE" \
      --region="$TR_PRIMARY_REGION" \
      --format=json 2>/dev/null \
      | jq -r '
          [
            .spec.template.spec.containers[0].env[]?
            | select(.name == "TR_ANALYTICS_DUAL_READ_STARTED_AT")
            | .value
          ][0] // empty
        ' || true
  )"
fi
if [ "$ANALYTICS_READ_MODE" = "dual" ] && {
  [ "$LIVE_ANALYTICS_READ_MODE" != "dual" ] ||
  [ -z "$ANALYTICS_DUAL_READ_STARTED_AT" ];
}; then
  ANALYTICS_DUAL_READ_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
fi

ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT="${TR_ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT:-}"
if [ -z "$ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT" ]; then
  ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT="$(
    gc run services describe "$SERVICE" \
      --region="$TR_PRIMARY_REGION" \
      --format=json 2>/dev/null \
      | jq -r '
          [
            .spec.template.spec.containers[0].env[]?
            | select(.name == "TR_ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT")
            | .value
          ][0] // empty
        ' || true
  )"
fi
if [ "$ANALYTICS_READ_MODE" = "clickhouse" ] && {
  [ "$LIVE_ANALYTICS_READ_MODE" != "clickhouse" ] ||
  [ -z "$ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT" ];
}; then
  ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
fi

read_primary_env() {
  local name="$1"
  local default_value="${2:-}"
  gc run services describe "$SERVICE" \
    --region="$TR_PRIMARY_REGION" \
    --format=json 2>/dev/null \
    | jq -r --arg name "$name" --arg default_value "$default_value" '
        [
          .spec.template.spec.containers[0].env[]?
          | select(.name == $name)
          | .value
        ][0] // $default_value
      ' || true
}

# Regional quota leases are opt-in and workspace allowlisted. Preserve the
# serving revision's state during ordinary deploys so a routine release cannot
# silently turn a canary on or off. A fresh environment remains fail-closed.
LIVE_REGIONAL_QUOTA_LEASES_ENABLED="$(
  read_primary_env "TR_REGIONAL_QUOTA_LEASES_ENABLED" "false"
)"
REGIONAL_QUOTA_LEASES_ENABLED="${TR_REGIONAL_QUOTA_LEASES_ENABLED:-${LIVE_REGIONAL_QUOTA_LEASES_ENABLED:-false}}"
case "$REGIONAL_QUOTA_LEASES_ENABLED" in
  true|false) ;;
  *)
    log "refusing rollout: TR_REGIONAL_QUOTA_LEASES_ENABLED must be true or false"
    exit 1
    ;;
esac
REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS="${TR_REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS:-$(
  read_primary_env "TR_REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS"
)}"
REGIONAL_QUOTA_BIGTABLE_TABLE="${TR_REGIONAL_QUOTA_BIGTABLE_TABLE:-$(
  read_primary_env "TR_REGIONAL_QUOTA_BIGTABLE_TABLE" "trustedrouter-regional-quota"
)}"
REGIONAL_QUOTA_BIGTABLE_APP_PROFILES="${TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES:-$(
  read_primary_env "TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES"
)}"
REGIONAL_QUOTA_LEASE_TTL_SECONDS="${TR_REGIONAL_QUOTA_LEASE_TTL_SECONDS:-$(
  read_primary_env "TR_REGIONAL_QUOTA_LEASE_TTL_SECONDS" "60"
)}"
REGIONAL_QUOTA_LEASE_MAX_MICRODOLLARS="${TR_REGIONAL_QUOTA_LEASE_MAX_MICRODOLLARS:-$(
  read_primary_env "TR_REGIONAL_QUOTA_LEASE_MAX_MICRODOLLARS" "10000000"
)}"
REGIONAL_QUOTA_LEASE_MAX_AVAILABLE_BASIS_POINTS="${TR_REGIONAL_QUOTA_LEASE_MAX_AVAILABLE_BASIS_POINTS:-$(
  read_primary_env "TR_REGIONAL_QUOTA_LEASE_MAX_AVAILABLE_BASIS_POINTS" "1000"
)}"
REGIONAL_QUOTA_LEASE_SHARD_COUNT="${TR_REGIONAL_QUOTA_LEASE_SHARD_COUNT:-$(
  read_primary_env "TR_REGIONAL_QUOTA_LEASE_SHARD_COUNT" "16"
)}"
if [ "$REGIONAL_QUOTA_LEASES_ENABLED" = "true" ] && {
  [ -z "$REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS" ] ||
  [ -z "$REGIONAL_QUOTA_BIGTABLE_APP_PROFILES" ];
}; then
  log "refusing rollout: regional quota canary requires pilot workspaces and fixed Bigtable app profiles"
  exit 1
fi

# Prefer the private three-replica ClickHouse load balancer once provisioned.
# The direct node-1 address remains only as a migration fallback for projects
# that have not run clickhouse_cluster.sh yet.
PROVIDER_ANALYTICS_CLICKHOUSE_URL="${TR_PROVIDER_ANALYTICS_CLICKHOUSE_URL:-}"
if [ -z "$PROVIDER_ANALYTICS_CLICKHOUSE_URL" ]; then
  clickhouse_ilb_ip="$(gc compute addresses describe tr-clickhouse-ilb \
    --region=us-central1 --format='value(address)' 2>/dev/null || true)"
  if [ -n "$clickhouse_ilb_ip" ]; then
    PROVIDER_ANALYTICS_CLICKHOUSE_URL="http://${clickhouse_ilb_ip}:8123"
  else
    PROVIDER_ANALYTICS_CLICKHOUSE_URL="http://10.128.15.214:8123"
  fi
fi

ENV_VARS=(
  "TR_ENVIRONMENT=production"
  "TR_RELEASE=$(git rev-parse --short HEAD 2>/dev/null || echo local)"
  # Request-based Cloud Run CPU can pause background coroutines. The scheduled
  # synthetic job invokes /internal/synthetic/remediate instead.
  "TR_REMEDIATOR_IN_PROCESS_ENABLED=false"
  "TR_ENABLE_LIVE_PROVIDERS=false"
  "TR_API_BASE_URL=https://api.trustedrouter.com/v1"
  "TR_TRUSTED_DOMAIN=trustedrouter.com"
  "TR_TRUSTED_DOMAIN_ALIASES=allyrouter.com,uptimerouter.com"
  "TR_STORAGE_BACKEND=${STORAGE_BACKEND}"
  # Exactly $0.30 once per newly created email/social OAuth account. Wallet-only
  # accounts stay at $0. Keep this explicit so stale env cannot change policy.
  "TR_SIGNUP_TRIAL_CREDIT_MICRODOLLARS=300000"
  "TR_GCP_PROJECT_ID=${PROJECT_ID}"
  # Owner notifications. The identifiers here are not secrets — the numbers are
  # public and the ids name resources, not authorise them — so they live in
  # plain env alongside the credentials in Secret Manager.
  #
  # Telnyx voice needs BOTH ids: the account id is the ORGANISATION id from
  # /v2/whoami (the connection id and application id both 404 there), and the
  # application id is what carries the outbound voice profile. Missing either
  # one is a 422 or a 403 on every call.
  "TR_NOTIFY_ENABLED=true"
  "TR_NOTIFY_SMS_AVAILABLE=false"
  # Identity verification and the custom-model verification gate are a PAIR
  # and must flip together: enabling the gate without a reachable Veriff
  # would 403 every custom-model create/edit with an unsatisfiable
  # "identity_verified" — a silent lockout, not a boot failure. Activated
  # 2026-08-16 once trustedrouter-veriff-{api-key,shared-secret-key} existed
  # in Secret Manager (the deploy SA reads them via add_secret_env_if_exists
  # above; it cannot create them). The config validator refuses
  # TR_VERIFF_ENABLED=true without both secrets, so a rollout that lost them
  # fails loud at boot rather than serving broken. To go dark again, set BOTH
  # back to false in one commit.
  "TR_VERIFF_ENABLED=true"
  "TR_CUSTOM_MODELS_REQUIRE_VERIFICATION=true"
  # User-provided models serve from here. The half that made this unsafe —
  # settle/refund of the synthetic user-model endpoint, and the exactly-once
  # payout — shipped in #608, and the attested enclave that dispatches them
  # shipped in quill-cloud-proxy 7925a4f. Registration, probing and the public
  # section always worked; this is the switch that lets the gateway AUTHORIZE
  # and route to one. Off again = the gateway 404s user-model ids; in-flight
  # holds still settle, because settle does not read this flag.
  "TR_USER_MODELS_DISPATCH_ENABLED=true"
  "TR_VERIFF_BASE_URL=https://stationapi.veriff.com"
  "TR_TELNYX_FROM_NUMBER=+17869471547"
  "TR_TELNYX_TEXML_ACCOUNT_ID=1eea716a-02e0-4d4f-96fa-36d1f556edca"
  "TR_TELNYX_TEXML_APPLICATION_ID=3026758434193146987"
  # The 10DLC-registered sender. SMS defaults to Twilio because registration is
  # per carrier and Telnyx is not registered — it rejects US SMS outright.
  "TR_TWILIO_FROM_NUMBER=+15055313623"
  "TR_SPANNER_INSTANCE_ID=${SPANNER_INSTANCE_ID}"
  "TR_SPANNER_DATABASE_ID=${SPANNER_DATABASE_ID}"
  "TR_BIGTABLE_INSTANCE_ID=${BIGTABLE_INSTANCE_ID}"
  "TR_BIGTABLE_GENERATION_TABLE=${BIGTABLE_GENERATION_TABLE}"
  "TR_BIGTABLE_MIRROR_WRITES_ENABLED=${BIGTABLE_MIRROR_WRITES_ENABLED}"
  "TR_GENERATION_RECORDS_ENABLED=${GENERATION_RECORDS_ENABLED}"
  "TR_BYOK_KMS_KEY_NAME=${BYOK_KMS_KEY_NAME}"
  "TR_GOOGLE_DATA_MANAGER_KMS_KEY_NAME=${GOOGLE_ADS_KMS_KEY_NAME}"
  "TR_REGIONS=${TR_REGIONS}"
  "TR_PRIMARY_REGION=${TR_PRIMARY_REGION}"
  "VERTEX_PROJECT_ID=${PROJECT_ID}"
  "VERTEX_LOCATION=${REGION}"
  "TR_TRUST_GCP_SOURCE_COMMIT=${TRUST_SOURCE_COMMIT}"
  "TR_TRUST_GCP_IMAGE_REFERENCE=${TRUST_IMAGE_REFERENCE}"
  "TR_TRUST_GCP_IMAGE_DIGEST=${TRUST_IMAGE_DIGEST}"
  "TR_TRUST_GCP_RELEASE_URL=${TRUST_FILE_URL}"
  "TR_TRUST_GCP_RELEASE_FALLBACK_URLS=https://raw.githubusercontent.com/Lore-Hex/quill-cloud-proxy/main/trust-page/gcp-release.json"
  # AWS and Azure measurements are NOT injected here any more. Each plane
  # publishes its own record, produced from a live attestation by
  # quill-cloud-proxy's tools/capture-plane-measurements.py, and the control
  # plane mirrors it from the URL below. Pinning them here made this one
  # GCP-region rollout script the authority for what three independent planes
  # were running — one place to falsify, one place to fail, and one more value
  # to remember to update on every enclave rebuild.
  "TR_TRUST_AWS_RELEASE_URL=https://trust.trustedrouter.com/trust/aws-release.json"
  "TR_TRUST_AZURE_RELEASE_URL=https://trust.trustedrouter.com/trust/azure-release.json"
  "TR_GOOGLE_OAUTH_REDIRECT_URL=https://trustedrouter.com/google_oauth_callback"
  "TR_GITHUB_OAUTH_REDIRECT_URL=https://trustedrouter.com/github_oauth_callback"
  "TR_SIWE_DOMAIN=trustedrouter.com"
  "TR_AWS_REGION=us-east-1" # SES region only; hosted compute is GCP-only.
  "TR_SES_FROM_EMAIL=noreply@trustedrouter.com"
  "TR_SES_FROM_NAME=TrustedRouter"
  "TR_SES_ALERT_FROM_EMAIL=alerts@alerts.trustedrouter.com"
  "TR_SES_ALERT_FROM_NAME=TrustedRouter Alerts"
  "TR_SES_ALERT_CONFIGURATION_SET=trustedrouter-alerts"
  # Reputation brake: keep optional activation nudges off until SES class-level
  # telemetry has a clean observation window. Login and verification email are
  # unaffected because they are sent synchronously by their auth routes.
  "TR_ACTIVATION_REMINDER_INTERVAL_SECONDS=0"
  "TR_SUPPORT_EMAIL=help@trustedrouter.com"
  # Adyen ships dark. Activating checkout is an intentional one-line release
  # after the merchant, HMAC webhook, and test-payment canary are green.
  "TR_ADYEN_ENABLED=false"
  "TR_ADYEN_ENVIRONMENT=test"
  "TR_ADYEN_MERCHANT_ACCOUNT=TrustedRouterUS"
  "TR_ADYEN_CHECKOUT_API_VERSION=72"
  "TR_ADYEN_WEB_VERSION=6.41.0"
  # Replace these zeros with the signed Adyen commercial terms before launch.
  "TR_ADYEN_CARD_FEE_BASIS_POINTS=0"
  "TR_ADYEN_CARD_FEE_FIXED_CENTS=0"
  "TR_CHECKOUT_CARD_FEE_MINIMUM_CENTS=80"
  "TR_OPS_CHAT_WEBHOOK_URLS=https://a.uptimerouter.com,https://b.trustedrouter.com,https://c.allyrouter.com"
  # /trustedos partner-inquiry form leads. Plain env (an address, not a
  # secret); without it the handler falls back to TR_SES_FROM_EMAIL, which
  # is send-only and effectively a black hole.
  "TR_PARTNER_INQUIRY_EMAIL=joseph@jperla.com"
  # Axiom log shipping. Token comes from Secret Manager via the
  # add_secret_env_if_exists block above; dataset name is plain config.
  # Empty AXIOM_API_TOKEN at runtime → handler is not registered (graceful no-op).
  "TR_AXIOM_DATASET=trusted-router-logs"
  "TR_AXIOM_URL=https://eu-central-1.aws.edge.axiom.co"
  # Durable settle outbox (docs/design/durable-settle-outbox.md §5.4, §8):
  # every gateway settle/refund durably records its frozen intent BEFORE the
  # inline finalize, and /internal/gateway/settle-outbox/drain recovers any
  # that were lost so the reaper never free-releases a completed request.
  # DDL applied 2026-07-04 (migrate_typed_counters.sh); reaper guard armed.
  # Flipped 2026-07-04 with Joseph's authorization. Remove to revert — the
  # flag-off settle path is byte-identical.
  "TR_SETTLE_OUTBOX_ENABLED=true"
  # Provider benchmark events use their own best-effort durable queue. Tenant
  # activity is different: its operational outbox insert is part of the typed
  # settlement transaction, so a charge and its delivery intent cannot split.
  # ClickHouse remains asynchronous and never participates in inference.
  "TR_ANALYTICS_OUTBOX_ENABLED=true"
  "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED=true"
  # Client-observed reliability beacons (docs/client-telemetry.md §4): SDKs
  # POST content-free per-request outcomes + exact minute counters to
  # /v1/client-events; one operational-outbox row per batch; the ClickHouse
  # node fans it out (DDL 008 applied on all replicas 2026-08-16, ingester
  # quarantines poison rows, rollup timer live). Flipped 2026-08-17 per the
  # approved plan after the local 200 POST/s x 64 KB smoke (all 202, p99 134
  # ms) and R-PR4's canary probe + corroborated alerts landed. Off = the route
  # answers 202 + `x-tr-telemetry: off` + pause 86400 before reading a body,
  # so removing this line stops every SDK within one flush.
  "TR_CLIENT_EVENTS_ENABLED=true"
  # Private provider operations portal. Direct VPC egress below reaches this
  # RFC1918 address; ClickHouse has no public IP and the credential is a
  # SELECT-only account scoped to the benchmark table.
  "TR_PROVIDER_ANALYTICS_CLICKHOUSE_URL=${PROVIDER_ANALYTICS_CLICKHOUSE_URL}"
  "TR_PROVIDER_ANALYTICS_CLICKHOUSE_USER=tr_provider_read"
  "TR_PROVIDER_ANALYTICS_CLICKHOUSE_DATABASE=tr"
  "TR_PROVIDER_ANALYTICS_CLICKHOUSE_TABLE=provider_benchmark_samples"
  "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL=${PROVIDER_ANALYTICS_CLICKHOUSE_URL}"
  "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER=tr_control_read"
  "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE=tr"
  "TR_ANALYTICS_READ_MODE=${ANALYTICS_READ_MODE}"
  "TR_ANALYTICS_DUAL_READ_GRACE_SECONDS=30"
  "TR_ANALYTICS_DUAL_READ_STARTED_AT=${ANALYTICS_DUAL_READ_STARTED_AT}"
  "TR_ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT=${ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT}"
  # The first expand deployment defaults to legacy. After an explicit typed
  # cutover, preserve the primary region's live mode on later deploys unless an
  # operator overrides it. This prevents routine rollouts from reopening the
  # unbounded generic write path.
  "TR_REQUEST_RECORD_WRITE_MODE=${REQUEST_RECORD_WRITE_MODE}"
  # Bounded regional escrow. This stays off by default and can serve only the
  # explicit workspace allowlist. Ordinary deploys preserve the serving
  # revision's state; startup fails closed if a required fixed profile is gone.
  "TR_REGIONAL_QUOTA_LEASES_ENABLED=${REGIONAL_QUOTA_LEASES_ENABLED}"
  "TR_REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS=${REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS}"
  "TR_REGIONAL_QUOTA_LEASE_TTL_SECONDS=${REGIONAL_QUOTA_LEASE_TTL_SECONDS}"
  "TR_REGIONAL_QUOTA_LEASE_MAX_MICRODOLLARS=${REGIONAL_QUOTA_LEASE_MAX_MICRODOLLARS}"
  "TR_REGIONAL_QUOTA_LEASE_MAX_AVAILABLE_BASIS_POINTS=${REGIONAL_QUOTA_LEASE_MAX_AVAILABLE_BASIS_POINTS}"
  "TR_REGIONAL_QUOTA_LEASE_SHARD_COUNT=${REGIONAL_QUOTA_LEASE_SHARD_COUNT}"
  "TR_REGIONAL_QUOTA_BIGTABLE_TABLE=${REGIONAL_QUOTA_BIGTABLE_TABLE}"
  "TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES=${REGIONAL_QUOTA_BIGTABLE_APP_PROFILES}"
)
SET_ENV_VARS="$(IFS='|'; echo "^|^${ENV_VARS[*]}")"

prune_failed_revisions() {
  # `gcloud run deploy --no-traffic` waits for the LATEST revision on the
  # service to be Ready before returning success. If a previous deploy
  # left a NotReady revision (container failed to start, OOM during
  # startup probe, missing env, etc.) AND the latest revision is that
  # NotReady one, the new deploy gets misreported as failed even when
  # it successfully created a fresh revision behind the latest tag.
  #
  # Caught the hard way during the 2026-05-10 cutover: paypal.py was
  # uncommitted, an earlier deploy created revision 00131-zkk (Failed),
  # and every subsequent deploy returned "Revision 00131-zkk is not
  # ready" instead of failing-clean — leaving the operator to
  # manually `update-traffic` to the actually-healthy fresh revision.
  #
  # Fix: before deploying, find revisions whose Ready condition is
  # neither True nor pending and which currently have no traffic
  # routed (so they're safe to delete) and remove them. Idempotent;
  # no-op when everything is healthy.
  local target="$1"
  local serving
  serving=$(gc run services describe "$SERVICE" --region "$target" \
    --format='value(status.traffic[].revisionName)' 2>/dev/null \
    | tr ';' ' ')
  local failed_revs
  failed_revs=$(gc run revisions list --service "$SERVICE" --region "$target" \
    --format='value(metadata.name,status.conditions[0].status)' 2>/dev/null \
    | awk '$2 == "False" { print $1 }')
  for rev in $failed_revs; do
    # Skip if this NotReady revision is somehow still in the traffic
    # split — better to leave it and let the operator decide than risk
    # hitting a revision we deleted while live.
    case " $serving " in
      *" $rev "*) continue ;;
    esac
    log "  pruning failed revision ${rev} in ${target}"
    gc run revisions delete "$rev" --region "$target" --quiet >/dev/null 2>&1 \
      || log "  WARN: failed to prune ${rev}; will let gcloud's deploy step error if it cares"
  done
}

is_warm_region() {
  # Returns 0 if $1 is in TR_WARM_REGIONS, 1 otherwise. Cold regions
  # (not in TR_WARM_REGIONS) deploy with --min-instances=0 so they don't
  # pay for always-on capacity at idle. They serve local users with a
  # ~5-10s cold-start tax on the first request, but ~$0/mo when idle.
  local r="$1"
  case ",${TR_WARM_REGIONS}," in
    *",${r},"*) return 0 ;;
    *) return 1 ;;
  esac
}

deploy_one_region() {
  local target="$1"
  local logfile="${2:-/dev/null}"
  # When TR_DEPLOY_NO_TRAFFIC=1 is set (the staged-traffic flow in the
  # GHA workflow), the new revision is created with 0% traffic. The
  # workflow then ramps it up via `gcloud run services update-traffic`
  # in 10% / 50% / 100% stages with synthetic checks between, so a bug
  # that breaks the new revision under real load is caught while most
  # traffic is still on the old revision.
  local traffic_arg=""
  if [ "${TR_DEPLOY_NO_TRAFFIC:-0}" = "1" ]; then
    traffic_arg="--no-traffic"
    log "deploying Cloud Run service ${SERVICE} to ${target} with --no-traffic (staged shift to follow)"
  else
    log "deploying Cloud Run service ${SERVICE} to ${target}"
  fi
  prune_failed_revisions "$target" >>"$logfile" 2>&1 || true
  # Cold regions (not in TR_WARM_REGIONS) scale to zero. The first request
  # pays a ~5-10s cold-start tax; subsequent requests within the
  # keep-warm window are fast. Explicit override via
  # TR_CLOUD_RUN_MIN_INSTANCES wins for either kind.
  local min_instances="${TR_CLOUD_RUN_MIN_INSTANCES:-}"
  if [ -z "$min_instances" ]; then
    if is_warm_region "$target"; then
      min_instances=1
    else
      min_instances=0
    fi
  fi
  # /chat and /synth stream through /chat-proxy/v1 for browser CORS.
  # Synth can legitimately take several model calls before final output, so
  # match the proxy's 300s upstream read timeout unless explicitly overridden.
  if gc run deploy "$SERVICE" \
      --region "$target" \
      --image "$IMAGE" \
      --allow-unauthenticated \
      --port 8080 \
      --memory "${TR_CLOUD_RUN_MEMORY:-1Gi}" \
      --concurrency "${TR_CLOUD_RUN_CONCURRENCY:-2}" \
      --min-instances "$min_instances" \
      --timeout "${TR_CLOUD_RUN_TIMEOUT_SECONDS:-300}" \
      --network "${TR_CLOUD_RUN_NETWORK:-default}" \
      --subnet "${TR_CLOUD_RUN_SUBNET:-default}" \
      --vpc-egress private-ranges-only \
      --set-env-vars "$SET_ENV_VARS" \
      --update-secrets "$UPDATE_SECRETS" \
      "${REMOVE_SECRETS_ARGS[@]}" \
      ${traffic_arg} \
      --quiet >>"$logfile" 2>&1; then
    log "deploy succeeded: ${target}"
    return 0
  fi
  log "deploy FAILED: ${target} (see ${logfile})"
  return 1
}

# Fan deploys out in parallel across every TR_REGIONS entry. Each
# region's gcloud invocation runs in its own subshell so a slow image
# pull in one cold region doesn't block the warm regions. Cloud Run scales to zero in
# unused regions so the bill stays the same as a single-region deploy
# at idle.
log_dir="$(mktemp -d "${TMPDIR:-/tmp}/tr-deploy-XXXXXX")"
log "parallel deploy logs in ${log_dir}"

if ! gc artifacts docker images describe "$IMAGE" >/dev/null 2>&1; then
  echo "ERROR: image ${IMAGE} does not exist. Run scripts/deploy/image.sh before rollout." >&2
  exit 1
fi

DEPLOY_TARGET_REGIONS="${TR_DEPLOY_TARGET_REGIONS:-$TR_CONTROL_PLANE_REGIONS}"
IFS=',' read -ra _REGION_LIST <<<"$DEPLOY_TARGET_REGIONS"
TARGETS=()
for r in "${_REGION_LIST[@]}"; do
  [ -n "$r" ] && TARGETS+=("$r")
done
if [ "${TR_DEPLOY_ALL_REGIONS:-1}" != "1" ]; then
  TARGETS=("$REGION")
fi

# Full set of control-plane regions that SHOULD be in the LB (independent of
# what this deploy run targets). The detach-stale-NEG step below compares
# attached regions against this — NOT against TARGETS — so a narrow-target
# deploy (e.g. TR_DEPLOY_TARGET_REGIONS=us-central1) doesn't accidentally rip
# cold public-site regions out of the LB.
#
# Lost ~30s of trustedrouter.com 504s on 2026-05-10 from exactly this:
# a cold-region-only deploy detached all three warm-region NEGs from
# trusted-router-control-backend because the original loop compared
# against TARGETS (the cold subset) instead of the full control-plane region
# set. TR_REGIONS remains the attested API region set exposed to SDK callers.
IFS=',' read -ra _ALL_REGION_LIST <<<"$TR_CONTROL_PLANE_REGIONS"
ALL_REGIONS=()
for r in "${_ALL_REGION_LIST[@]}"; do
  [ -n "$r" ] && ALL_REGIONS+=("$r")
done

REGION_PIDS=()
REGION_LOGS=()
for fanout_region in "${TARGETS[@]}"; do
  region_log="${log_dir}/${fanout_region}.log"
  REGION_LOGS+=("$region_log")
  deploy_one_region "$fanout_region" "$region_log" &
  REGION_PIDS+=("$!")
done

deploy_failed=0
for idx in "${!TARGETS[@]}"; do
  fanout_region="${TARGETS[$idx]}"
  pid="${REGION_PIDS[$idx]}"
  if ! wait "$pid"; then
    deploy_failed=1
    log "deploy log for failed region ${fanout_region}:"
    tail -20 "${REGION_LOGS[$idx]}" >&2 || true
  fi
done

if [ "$deploy_failed" -ne 0 ]; then
  echo "ERROR: at least one region's deploy failed; see logs in ${log_dir}" >&2
  exit 1
fi

latest_ready_revision_for_region() {
  local target="$1"
  # `status.latestReadyRevisionName` can lag behind when a deploy reuses a
  # mutable tag or when we immediately force a nonce-only redeploy. Prefer the
  # newest revision from the revision list whose Ready condition is True.
  gc run revisions list --service "$SERVICE" --region "$target" \
    --limit=10 \
    --sort-by='~metadata.creationTimestamp' \
    --format='value(metadata.name,status.conditions[0].status)' 2>/dev/null \
    | awk '$2 == "True" { print $1; exit }'
}

if [ "${TR_DEPLOY_NO_TRAFFIC:-0}" != "1" ]; then
  # Defense against a real 2026-06-05 rollout bug: Cloud Run created Ready
  # revisions in us-east4/europe-west4, but service traffic remained pinned
  # to older revisions, so prod served a mixed catalog. Make the intended
  # no-staging path explicit: after every successful regional deploy, route
  # 100% to the newest Ready revision in that region.
  for traffic_region in "${TARGETS[@]}"; do
    latest_rev="$(latest_ready_revision_for_region "$traffic_region")"
    if [ -z "$latest_rev" ]; then
      echo "ERROR: could not find newest Ready revision for ${traffic_region}" >&2
      exit 1
    fi
    log "routing ${traffic_region} traffic to newest Ready revision ${latest_rev}"
    gc run services update-traffic "$SERVICE" \
      --region="$traffic_region" \
      --to-revisions="${latest_rev}=100" \
      --quiet >/dev/null
  done
fi

log "Cloud Run URLs:"
for url_region in "${TARGETS[@]}"; do
  url="$(gc run services describe "$SERVICE" --region "$url_region" --format='value(status.url)' 2>/dev/null || true)"
  if [ -n "$url" ]; then
    printf '  %-28s %s\n' "$url_region" "$url"
  fi
done

# ---------------------------------------------------------------------------
# Attach a Serverless NEG per region to the global LB backend service so
# trustedrouter.com routes to the nearest healthy region instead of always
# us-central1. The backend service was created out-of-band when the LB
# was first set up; this block discovers + reuses its existing config so
# we don't have to know LB topology upfront.
#
# Idempotent: skip-if-exists on every step (NEG create, backend add).
# Safe to re-run on every deploy.
# ---------------------------------------------------------------------------
LB_BACKEND_SERVICE="${LB_BACKEND_SERVICE:-trusted-router-control-backend}"
LB_NEG_NAME="${LB_NEG_NAME:-trusted-router-control-neg}"

attach_region_to_lb() {
  local target="$1"
  if ! gc compute network-endpoint-groups describe "$LB_NEG_NAME" \
      --region "$target" >/dev/null 2>&1; then
    log "creating Serverless NEG ${LB_NEG_NAME} in ${target}"
    gc compute network-endpoint-groups create "$LB_NEG_NAME" \
      --region "$target" \
      --network-endpoint-type=serverless \
      --cloud-run-service="$SERVICE" \
      --quiet >/dev/null
  fi

  local already_attached
  already_attached="$(gc compute backend-services describe "$LB_BACKEND_SERVICE" \
    --global --format='value(backends[].group)' 2>/dev/null \
    | tr ';' '\n' \
    | grep -c "/regions/${target}/networkEndpointGroups/${LB_NEG_NAME}\$" || true)"
  if [ "$already_attached" = "0" ]; then
    log "attaching NEG ${LB_NEG_NAME} (${target}) to ${LB_BACKEND_SERVICE}"
    gc compute backend-services add-backend "$LB_BACKEND_SERVICE" \
      --global \
      --network-endpoint-group="$LB_NEG_NAME" \
      --network-endpoint-group-region="$target" \
      --quiet >/dev/null
  fi
}

if [ "${TR_DEPLOY_RECONCILE_LB:-1}" != "1" ]; then
  log "skipping shared load-balancer reconciliation for this regional rollout"
elif gc compute backend-services describe "$LB_BACKEND_SERVICE" --global >/dev/null 2>&1; then
  log "wiring Serverless NEGs to ${LB_BACKEND_SERVICE}"
  # Attach every control-plane region, not just this deploy's TARGETS, so the
  # LB always reflects the full intended public-site region set. Idempotent:
  # attach_region_to_lb no-ops on regions that are already attached. Without
  # this, a narrow-target deploy could leave a Cloud Run region outside LB
  # rotation.
  for fanout_region in "${ALL_REGIONS[@]}"; do
    attach_region_to_lb "$fanout_region" || log "WARN: NEG attach failed for ${fanout_region}"
  done
  existing_backend_regions="$(gc compute backend-services describe "$LB_BACKEND_SERVICE" \
    --global --format='value(backends[].group)' 2>/dev/null \
    | tr ';' '\n' \
    | sed -n 's#.*regions/\([^/]*\)/networkEndpointGroups/.*#\1#p' \
    | sort -u)"
  for attached_region in $existing_backend_regions; do
    # Compare against ALL_REGIONS (= TR_CONTROL_PLANE_REGIONS), not TARGETS.
    # TARGETS is just this deploy run's subset; detaching anything outside of
    # it would rip cold regions out of the LB when running a warm-only or
    # narrow-target deploy. We only want to detach regions that fell out of
    # TR_CONTROL_PLANE_REGIONS entirely.
    keep_region=0
    for full_region in "${ALL_REGIONS[@]}"; do
      if [ "$attached_region" = "$full_region" ]; then
        keep_region=1
        break
      fi
    done
    if [ "$keep_region" = "0" ]; then
      log "detaching stale NEG ${LB_NEG_NAME} (${attached_region}) from ${LB_BACKEND_SERVICE}"
      gc compute backend-services remove-backend "$LB_BACKEND_SERVICE" \
        --global \
        --network-endpoint-group="$LB_NEG_NAME" \
        --network-endpoint-group-region="$attached_region" \
        --quiet >/dev/null || log "WARN: stale NEG detach failed for ${attached_region}"
    fi
  done
  log "enabling origin-controlled Cloud CDN on ${LB_BACKEND_SERVICE}"
  gc compute backend-services update "$LB_BACKEND_SERVICE" \
    --global \
    --enable-cdn \
    --cache-mode=USE_ORIGIN_HEADERS \
    --cache-key-include-host \
    --cache-key-include-protocol \
    --cache-key-include-query-string \
    --cache-key-query-string-blacklist= \
    --compression-mode=AUTOMATIC \
    --serve-while-stale=600 \
    --no-negative-caching \
    --quiet >/dev/null
else
  log "WARN: ${LB_BACKEND_SERVICE} not found; skipping NEG wiring"
fi

# ---------------------------------------------------------------------------
# HTTP -> HTTPS redirect on the public load balancer
# ---------------------------------------------------------------------------
# The HTTPS forwarding rule was created out-of-band when the LB was first
# set up. We add a parallel :80 stack here that 301-redirects every HTTP
# request to HTTPS, so visitors who type `http://trustedrouter.com` don't
# get a connection-reset blank page. Three resources, all idempotent:
# skip-if-exists guards make it safe to re-run on every deploy.
LB_HTTP_URL_MAP="${LB_HTTP_URL_MAP:-trusted-router-control-http-redirect}"
LB_HTTP_PROXY="${LB_HTTP_PROXY:-trusted-router-control-http-proxy}"
LB_HTTP_FORWARDING_RULE="${LB_HTTP_FORWARDING_RULE:-trusted-router-control-http}"
LB_HTTPS_FORWARDING_RULE="${LB_HTTPS_FORWARDING_RULE:-trusted-router-control-https}"

ensure_http_redirect_lb() {
  if ! gc compute url-maps describe "$LB_HTTP_URL_MAP" --global >/dev/null 2>&1; then
    log "creating HTTP-redirect URL map ${LB_HTTP_URL_MAP}"
    gc compute url-maps import "$LB_HTTP_URL_MAP" --global \
      --source=/dev/stdin --quiet <<YAML
name: ${LB_HTTP_URL_MAP}
defaultUrlRedirect:
  httpsRedirect: true
  redirectResponseCode: MOVED_PERMANENTLY_DEFAULT
  stripQuery: false
YAML
  fi

  if ! gc compute target-http-proxies describe "$LB_HTTP_PROXY" --global >/dev/null 2>&1; then
    log "creating HTTP target proxy ${LB_HTTP_PROXY}"
    gc compute target-http-proxies create "$LB_HTTP_PROXY" \
      --url-map="$LB_HTTP_URL_MAP" --global --quiet
  fi

  if ! gc compute forwarding-rules describe "$LB_HTTP_FORWARDING_RULE" --global >/dev/null 2>&1; then
    local lb_ip
    lb_ip="$(gc compute forwarding-rules describe "$LB_HTTPS_FORWARDING_RULE" \
      --global --format='value(IPAddress)' 2>/dev/null || true)"
    if [ -z "$lb_ip" ]; then
      log "WARN: HTTPS forwarding rule ${LB_HTTPS_FORWARDING_RULE} not found; skipping HTTP rule"
      return 0
    fi
    log "creating HTTP forwarding rule ${LB_HTTP_FORWARDING_RULE} on ${lb_ip}:80"
    gc compute forwarding-rules create "$LB_HTTP_FORWARDING_RULE" \
      --address="$lb_ip" \
      --target-http-proxy="$LB_HTTP_PROXY" \
      --ports=80 --global --quiet
  fi
}

if [ "${TR_DEPLOY_RECONCILE_LB:-1}" = "1" ]; then
  ensure_http_redirect_lb
fi
