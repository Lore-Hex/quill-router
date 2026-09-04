#!/usr/bin/env bash
# Phase 4: parallel Cloud Run rollout across every control-plane region, then
# attach a Serverless NEG per region to the global LB backend service so
# trustedrouter.com routes to the nearest healthy region. Finally ensures the
# HTTP -> HTTPS redirect on :80.

# Temporary compatibility bridge for the legacy all-routes service.  Requiring
# an explicit caller opt-in keeps a direct invocation fail-closed before it can
# read or mutate cloud state; the guarded deploy workflow is the sole
# production caller that supplies it.  Delete this block and both emitted env
# vars when the six-service cutover in #712 lands.
ALLOW_DEPLOYED_COMBINED_SURFACE="${TR_ALLOW_DEPLOYED_COMBINED_SURFACE:-false}"
if [ "$ALLOW_DEPLOYED_COMBINED_SURFACE" != "true" ]; then
  echo "refusing legacy combined rollout: set TR_ALLOW_DEPLOYED_COMBINED_SURFACE=true" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_deploy_hold.sh
source "${SCRIPT_DIR}/_deploy_hold.sh"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"
# shellcheck source=scripts/deploy/deploy_mutex.sh
source "${SCRIPT_DIR}/deploy_mutex.sh"
# shellcheck source=scripts/deploy/_cloud_run_revision_probe.sh
source "${SCRIPT_DIR}/_cloud_run_revision_probe.sh"
# shellcheck source=scripts/deploy/regional_quota_rollout.sh
source "${SCRIPT_DIR}/regional_quota_rollout.sh"

WARM_PROBE_TAG="staged-probe"
WARM_PROBE_REGIONS=()
WARM_PROBE_CLEANUP_REQUIRED=0

release_rollout_deploy_mutex() {
  local rollout_status=$?
  # Finish the release uninterrupted; a signal here would leak the mutex
  # until its TTL.
  trap '' INT TERM
  trap - EXIT
  # A failed no-traffic warm must not leave its immutable revision minimum
  # activated by a tag. Once untagged and absent from the traffic split, Cloud
  # Run allocates zero instances to the failed candidate.
  if [ "$rollout_status" -ne 0 ] &&
     [ "$WARM_PROBE_CLEANUP_REQUIRED" -eq 1 ]; then
    local cleanup_region
    for cleanup_region in "${WARM_PROBE_REGIONS[@]}"; do
      if ! cloud_run_probe_tag_remove \
          "$SERVICE" "$cleanup_region" "$PROJECT_ID" "$WARM_PROBE_TAG"; then
        log "CRITICAL: failed warm left ${WARM_PROBE_TAG} cleanup required in ${cleanup_region}"
      fi
    done
  fi
  if [ "${DEPLOY_MUTEX_SCOPE_OWNS_LOCK:-0}" -eq 1 ]; then
    deploy_mutex_release
  fi
  exit "$rollout_status"
}
trap release_rollout_deploy_mutex EXIT
# Funnel signals through EXIT: a SIGTERM from workflow cancellation (or Ctrl-C
# on a manual run) must still run the trap, which now also owns removing the
# staged-probe tag from a failed warm — a tagged candidate keeps its baked
# revision minimum allocated at 0% traffic, which is paid capacity.
trap 'exit 130' INT
trap 'exit 143' TERM

# The workflow exports its outer lock through GITHUB_ENV. A direct operator
# invocation has no such operation and owns this script-level scope instead.
if [ -z "${TR_DEPLOY_MUTEX_OPERATION:-}" ]; then
  deploy_mutex_acquire
fi

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
  TRUST_SOURCE_COMMIT="$(python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("source_commit", "") if isinstance(d, dict) else "")' <<<"$TRUST_JSON")"
  TRUST_IMAGE_REFERENCE="$(python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("image_reference", "") if isinstance(d, dict) else "")' <<<"$TRUST_JSON")"
  TRUST_IMAGE_DIGEST="$(python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("image_digest", "") if isinstance(d, dict) else "")' <<<"$TRUST_JSON")"
fi


SECRET_ENVS=(
  "TR_SENTRY_DSN=trustedrouter-sentry-dsn:latest"
  "TR_STRIPE_SECRET_KEY=trustedrouter-stripe-secret-key:latest"
  "TR_STRIPE_WEBHOOK_SECRET=trustedrouter-stripe-webhook-secret:latest"
  "TR_INTERNAL_GATEWAY_TOKEN=trustedrouter-internal-gateway-token:latest"
  "TR_OPERATOR_TOKEN=trustedrouter-operator-token:latest"
)
# Retired environment bindings remain on Cloud Run until explicitly removed.
REMOVE_SECRET_ENVS=("TR_GOOGLE_ADS_CONVERSION_FEED_PASSWORD")

# Read the metadata inventory once. Probing every optional name with
# `secrets describe` records an ERROR audit event for each expected missing
# secret, which obscures real Secret Manager failures and inflates logging
# volume. The inventory contains names only, never secret values. Failure to
# list is fatal so an IAM or API outage cannot silently strip bindings.
OPTIONAL_SECRET_NAMES=""
if ! OPTIONAL_SECRET_NAMES="$(gc secrets list --format='value(name)')"; then
  log "cannot list optional secret inventory"
  exit 1
fi

add_secret_env_if_exists() {
  local env_name="$1"
  local secret_name="$2"
  if grep -Fxq -- "$secret_name" <<<"$OPTIONAL_SECRET_NAMES"; then
    SECRET_ENVS+=("${env_name}=${secret_name}:latest")
  else
    # gcloud --update-secrets preserves old bindings. Explicit removal keeps
    # a deleted optional secret from making an otherwise healthy revision
    # unroutable in regions that still carry the stale environment entry.
    REMOVE_SECRET_ENVS+=("${env_name}")
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
add_secret_env_if_exists "TR_ROUTABLE_API_TOKEN" "trustedrouter-routable-api-token"
add_secret_env_if_exists \
  "TR_ROUTABLE_WEBHOOK_SECRET" "trustedrouter-routable-webhook-secret"
add_secret_env_if_exists "TR_ROUTABLE_COMPANY_ID" "trustedrouter-routable-company-id"
add_secret_env_if_exists \
  "TR_ROUTABLE_TEAM_MEMBER_ID" "trustedrouter-routable-team-member-id"
add_secret_env_if_exists \
  "TR_ROUTABLE_WITHDRAW_FROM_ACCOUNT_ID" \
  "trustedrouter-routable-withdraw-from-account-id"
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
add_secret_env_if_exists \
  "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_WRITE_PASSWORD" \
  "trustedrouter-clickhouse-ops-ingest-password"
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

# Regional quota capability and traffic issuance are separate switches. Resolve
# preserved values from the one revision receiving 100% of primary traffic,
# never the latest candidate or service template: both still point at a rejected
# revision after rollback. Only an exact missing-service response represents a
# fresh environment; every other control-plane read error aborts the rollout.
REGIONAL_QUOTA_PRIMARY_FRESH=false
REGIONAL_QUOTA_PRIMARY_REVISION_JSON=""
if REGIONAL_QUOTA_PRIMARY_REVISION_JSON="$(
  regional_quota_active_revision_json "$TR_PRIMARY_REGION" true
)"; then
  :
else
  regional_quota_primary_status=$?
  if [ "$regional_quota_primary_status" -eq 3 ]; then
    REGIONAL_QUOTA_PRIMARY_FRESH=true
  else
    exit "$regional_quota_primary_status"
  fi
fi

read_primary_regional_quota_env() {
  local name="$1"
  local default_value="${2:-}"
  if [ "$REGIONAL_QUOTA_PRIMARY_FRESH" = "true" ]; then
    printf '%s\n' "$default_value"
    return 0
  fi
  regional_quota_revision_env \
    "$REGIONAL_QUOTA_PRIMARY_REVISION_JSON" \
    "$name" \
    "$default_value"
}

LIVE_REGIONAL_QUOTA_LEASES_ENABLED="$(
  read_primary_regional_quota_env "TR_REGIONAL_QUOTA_LEASES_ENABLED" "false"
)"
REGIONAL_QUOTA_LEASES_ENABLED="${TR_REGIONAL_QUOTA_LEASES_ENABLED:-${LIVE_REGIONAL_QUOTA_LEASES_ENABLED:-false}}"
case "$REGIONAL_QUOTA_LEASES_ENABLED" in
  true|false) ;;
  *)
    log "refusing rollout: TR_REGIONAL_QUOTA_LEASES_ENABLED must be true or false"
    exit 1
    ;;
esac

# workflow_dispatch passes one of preserve/false/true through unchanged. The
# deploy shell, not GitHub's expression coercion, turns that raw operator intent
# into the boolean written on the Cloud Run revision. A missing marker on the
# first compatibility deploy defaults OFF.
LIVE_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED="$(
  read_primary_regional_quota_env \
    "TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED" \
    "false"
)"
REGIONAL_QUOTA_LEASE_ISSUANCE_CONTROL="${TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED:-}"
REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED="$(
  regional_quota_normalize_issuance_control \
    "$REGIONAL_QUOTA_LEASE_ISSUANCE_CONTROL" \
    "$LIVE_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED"
)"
if [ "$REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED" = "true" ] &&
   [ "$REGIONAL_QUOTA_LEASES_ENABLED" != "true" ]; then
  log "refusing rollout: regional quota issuance requires lease capability"
  exit 1
fi

REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS="${TR_REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS:-$(
  read_primary_regional_quota_env "TR_REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS"
)}"
REGIONAL_QUOTA_BIGTABLE_TABLE="${TR_REGIONAL_QUOTA_BIGTABLE_TABLE:-$(
  read_primary_regional_quota_env "TR_REGIONAL_QUOTA_BIGTABLE_TABLE" "trustedrouter-regional-quota"
)}"
REGIONAL_QUOTA_BIGTABLE_APP_PROFILES="${TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES:-$(
  read_primary_regional_quota_env "TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES"
)}"
REGIONAL_QUOTA_LEASE_TTL_SECONDS="${TR_REGIONAL_QUOTA_LEASE_TTL_SECONDS:-$(
  read_primary_regional_quota_env "TR_REGIONAL_QUOTA_LEASE_TTL_SECONDS" "60"
)}"
REGIONAL_QUOTA_LEASE_MAX_MICRODOLLARS="${TR_REGIONAL_QUOTA_LEASE_MAX_MICRODOLLARS:-$(
  read_primary_regional_quota_env "TR_REGIONAL_QUOTA_LEASE_MAX_MICRODOLLARS" "10000000"
)}"
REGIONAL_QUOTA_LEASE_MAX_AVAILABLE_BASIS_POINTS="${TR_REGIONAL_QUOTA_LEASE_MAX_AVAILABLE_BASIS_POINTS:-$(
  read_primary_regional_quota_env "TR_REGIONAL_QUOTA_LEASE_MAX_AVAILABLE_BASIS_POINTS" "1000"
)}"
REGIONAL_QUOTA_LEASE_SHARD_COUNT="${TR_REGIONAL_QUOTA_LEASE_SHARD_COUNT:-$(
  read_primary_regional_quota_env "TR_REGIONAL_QUOTA_LEASE_SHARD_COUNT" "16"
)}"
# Bigtable budget per ledger read/CAS. Callbacks read the primary's cluster
# from every region, so this must cover a cross-continent round trip.
REGIONAL_QUOTA_LEDGER_TIMEOUT_SECONDS="${TR_REGIONAL_QUOTA_LEDGER_TIMEOUT_SECONDS:-$(
  read_primary_regional_quota_env "TR_REGIONAL_QUOTA_LEDGER_TIMEOUT_SECONDS" "4"
)}"
if [ "$REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED" = "true" ] && {
  [ -z "$REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS" ] ||
  [ -z "$REGIONAL_QUOTA_BIGTABLE_APP_PROFILES" ];
}; then
  log "refusing rollout: regional quota issuance requires pilot workspaces and fixed Bigtable app profiles"
  exit 1
fi

# This executes before gcloud run deploy can create any issuance-enabled
# revision. Every currently active fleet member must already be settlement-
# capable and must explicitly carry the new boolean marker. That creates a
# compatibility phase between landing the code and enabling issuance.
if [ "$REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED" = "true" ]; then
  regional_quota_preflight_issuance_fleet
fi

# Binding makes the unit-4 settlement clamp and repair/mirror path load-bearing.
# Refuse a source rollback that would build a binding-enabled image without
# those rules. The emergency rollback path is explicit: deploy with binding
# disabled, then investigate or roll forward from there.
SPEND_LEASE_BINDING_TARGET="${TR_SPEND_LEASE_BINDING_ENABLED:-true}"
case "$SPEND_LEASE_BINDING_TARGET" in
  true)
    spend_lease_unit_4_source="${SCRIPT_DIR}/../../src/trusted_router/services/spend_lease_settlement.py"
    if ! grep -Fq "def clamp_spend_lease_charge(" "$spend_lease_unit_4_source"; then
      log "refusing rollout: TR_SPEND_LEASE_BINDING_ENABLED=true requires spend-lease unit 4 (missing clamp_spend_lease_charge); rollback only with TR_SPEND_LEASE_BINDING_ENABLED=false"
      exit 1
    fi
    ;;
  false) ;;
  *)
    log "refusing rollout: TR_SPEND_LEASE_BINDING_ENABLED must be true or false"
    exit 1
    ;;
esac

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
  "TR_SERVICE_SURFACE=combined"
  "TR_OPERATOR_IDENTITIES=${TR_OPERATOR_IDENTITIES:-joseph@jperla.com}"
  "TR_ALLOW_DEPLOYED_COMBINED_SURFACE=${ALLOW_DEPLOYED_COMBINED_SURFACE}"
  # The legacy backend does not yet receive a trusted, edge-overwritten client
  # identity. The #714 process-local limiter would collapse all Internet users
  # into one 240/min bucket. #712 removes this exception while installing each
  # split service's edge identity and independent capacity policy.
  "TR_RATE_LIMIT_ENABLED=false"
  "TR_SETTLE_PER_KEY_INFLIGHT_LIMIT=16"
  "TR_RELEASE=${TR_DEPLOY_RELEASE_ID:-$(git rev-parse --short HEAD 2>/dev/null || echo local)}"
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
  "TR_SPANNER_POOL_SIZE=${TR_SPANNER_POOL_SIZE}"
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
  # Creator payouts stay dark until the Routable account, funding account,
  # signed webhook, and production canary are all configured. The optional
  # secrets above make activation a reviewed one-line release.
  "TR_ROUTABLE_ENABLED=false"
  "TR_ROUTABLE_API_BASE_URL=https://api.routable.com"
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
  # Stage D decision 70 is an explicit billing-policy switch. Keep snapshot
  # booking dark until Joseph approves the dedicated rollout step.
  "TR_REAP_SNAPSHOT_BOOKING_ENABLED=false"
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
  # 2026-08-26: the direct sink is RETIRED from deploys until it is reworked.
  # It failed twice in one day — first silently (Cloud Run direct VPC egress
  # cannot reliably reach the passthrough ILB at the operational ClickHouse
  # URL; delivery stopped with zero errors and the status page read healthy
  # while ~3.5h of telemetry dropped), then loudly after the #853 logging fix
  # exposed every flush failing HTTP 403. Each deploy also silently overwrote
  # the operator's live outbox revert, re-breaking prod telemetry mid-release.
  # The outbox drain's ~25% idle Spanner cost is accepted until a re-cutover
  # that (a) targets a supported endpoint (instance IPs, an internal
  # Application LB, or PSC — never a passthrough ILB forwarding rule),
  # (b) is verified FROM the Cloud Run runtime rather than by hairpin from a
  # ClickHouse VM, and (c) ships after the direct-mode freshness fields from
  # #853 are live so a dead sink can never again read as healthy.
  "TR_OPERATIONAL_ANALYTICS_SINK=outbox"
  "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_WRITE_USER=tr_ops_ingest"
  "TR_ANALYTICS_READ_MODE=${ANALYTICS_READ_MODE}"
  "TR_ANALYTICS_DUAL_READ_GRACE_SECONDS=30"
  "TR_ANALYTICS_DUAL_READ_STARTED_AT=${ANALYTICS_DUAL_READ_STARTED_AT}"
  "TR_ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT=${ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT}"
  # The first expand deployment defaults to legacy. After an explicit typed
  # cutover, preserve the primary region's live mode on later deploys unless an
  # operator overrides it. This prevents routine rollouts from reopening the
  # unbounded generic write path.
  "TR_REQUEST_RECORD_WRITE_MODE=${REQUEST_RECORD_WRITE_MODE}"
  # Bounded regional escrow. Capability keeps settlement/reconciliation ready;
  # the independent issuance marker stays off through the compatibility phase.
  "TR_REGIONAL_QUOTA_LEASES_ENABLED=${REGIONAL_QUOTA_LEASES_ENABLED}"
  "TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED=${REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED}"
  "TR_REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS=${REGIONAL_QUOTA_LEASE_PILOT_WORKSPACE_IDS}"
  "TR_REGIONAL_QUOTA_LEASE_TTL_SECONDS=${REGIONAL_QUOTA_LEASE_TTL_SECONDS}"
  "TR_REGIONAL_QUOTA_LEASE_MAX_MICRODOLLARS=${REGIONAL_QUOTA_LEASE_MAX_MICRODOLLARS}"
  "TR_REGIONAL_QUOTA_LEASE_MAX_AVAILABLE_BASIS_POINTS=${REGIONAL_QUOTA_LEASE_MAX_AVAILABLE_BASIS_POINTS}"
  "TR_REGIONAL_QUOTA_LEASE_SHARD_COUNT=${REGIONAL_QUOTA_LEASE_SHARD_COUNT}"
  "TR_REGIONAL_QUOTA_LEDGER_TIMEOUT_SECONDS=${REGIONAL_QUOTA_LEDGER_TIMEOUT_SECONDS}"
  "TR_REGIONAL_QUOTA_BIGTABLE_TABLE=${REGIONAL_QUOTA_BIGTABLE_TABLE}"
  "TR_REGIONAL_QUOTA_BIGTABLE_APP_PROFILES=${REGIONAL_QUOTA_BIGTABLE_APP_PROFILES}"
  # 2026-08-30 pilot: Joseph's own Personal Workspace (first-party, his account,
  # at his direction). The previous pilot, TrustedRouter Synthetic Monitoring
  # (d385c399-b245-4147-a528-0a4f6f170c71), was structurally ineligible because
  # it is the regional-quota pilot workspace and Stage A excludes regional-lease
  # authorizations by design: no_lease_reason=regional_lease on 190 of 213 pilot
  # events. Shadow grants are authoritative=false and escrow nothing. Revert by
  # removing these three pins and redeploying.
  # Deliberately non-sticky: pilot state is source-controlled; the sticky idiom
  # is for operator-set values, and a source default cannot override an existing
  # deployed marker.
  "TR_SPEND_LEASE_ISSUANCE_ENABLED=true"
  "TR_SPEND_LEASE_BINDING_ENABLED=${TR_SPEND_LEASE_BINDING_ENABLED:-true}"
  # Stage C ships inert. This literal source-controlled default is the router
  # kill switch; verification stays deployed so in-flight receipts fail closed.
  "TR_SPEND_LEASE_ADMISSION_ACCEPT=false"
  "TR_STAGE_D_HEARTBEAT_ENABLED=true"
  "TR_STAGE_D_ELIGIBILITY_ENABLED=false"
  "TR_STAGE_D_PILOT_WORKSPACE_IDS=45819281-0ce9-4811-a0cd-c660ab3a116d"
  # Slice 1a ships the trust guard inert. A later arm-gate slice owns enabling.
  "TR_SPEND_LEASE_TRUST_ELIGIBILITY_ENABLED=false"
  "TR_HEARTBEAT_GRACE_SECONDS=${TR_HEARTBEAT_GRACE_SECONDS:-300}"
  "TR_SPEND_LEASE_PILOT_WORKSPACE_IDS=45819281-0ce9-4811-a0cd-c660ab3a116d"
  "TR_SPEND_LEASE_SIGNING_SECRET_NAME=trustedrouter-spend-lease-signing-seed"
  "TR_SPEND_LEASE_ACCEPTED_GCP_IMAGE_DIGESTS="
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

cloud_run_min_instances_for_region() {
  local target="$1"
  local entry
  local region
  local count
  local entries=()
  IFS=',' read -ra entries <<<"$TR_CLOUD_RUN_MIN_INSTANCES_BY_REGION"
  for entry in "${entries[@]}"; do
    region="${entry%%=*}"
    count="${entry#*=}"
    if [ "$region" = "$target" ] && [[ "$count" =~ ^[0-9]+$ ]]; then
      printf '%s\n' "$count"
      return 0
    fi
  done
  if is_warm_region "$target"; then
    printf '1\n'
  else
    printf '0\n'
  fi
}

cloud_run_service_min_instances_for_region() {
  local target="$1"
  if [ -n "${TR_CLOUD_RUN_MIN_INSTANCES:-}" ]; then
    printf '%s\n' "$TR_CLOUD_RUN_MIN_INSTANCES"
  else
    cloud_run_min_instances_for_region "$target"
  fi
}

cloud_run_candidate_min_instances() {
  local service_min="$1"
  local prewarm_floor="${TR_CLOUD_RUN_PREWARM_MIN_INSTANCES:-2}"
  if [[ ! "$prewarm_floor" =~ ^[0-9]+$ ]]; then
    log "invalid TR_CLOUD_RUN_PREWARM_MIN_INSTANCES=${prewarm_floor}; using 2" >&2
    prewarm_floor=2
  fi
  if [ "$service_min" != "default" ] && [ "$prewarm_floor" -gt "$service_min" ]; then
    prewarm_floor="$service_min"
  fi
  printf '%s\n' "$prewarm_floor"
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
  if [ "${TR_DEPLOY_NO_TRAFFIC:-0}" = "1" ]; then
    SERVICE="$SERVICE" PROJECT_ID="$PROJECT_ID" \
      bash "${SCRIPT_DIR}/normalize_staged_traffic.sh" "$target" \
      >>"$logfile" 2>&1
  fi
  prune_failed_revisions "$target" >>"$logfile" 2>&1 || true
  # A global override wins. Otherwise use the per-region service minimum;
  # unknown warm regions retain one instance and unknown cold regions scale to
  # zero.
  local min_instances
  min_instances="$(cloud_run_service_min_instances_for_region "$target")"
  local revision_min_instances="default"
  if [ "${TR_DEPLOY_NO_TRAFFIC:-0}" = "1" ]; then
    # Cloud Run revisions are immutable: clearing a temporary revision minimum
    # would create a second cold revision and recreate the 100% cutover stall.
    # The candidate therefore bakes a revision minimum, activated at 0% by the
    # probe tag below; effective capacity is max(service, revision), so
    # convergence is cost-neutral and rollback deactivates it.
    #
    # PRIMER, not full capacity (measured 2026-08-25): matching the service
    # minimum made `gcloud run deploy` WAIT for that many instances to go
    # Ready at warm time — us-east4 pins 8, and the parallel warm step ran
    # 7m37 instead of ~2m; the 100%-stall cost moved into the warm and grew.
    # A small primer absorbs the 10% step instantly, and the staged
    # 10/50/100 ramp itself gives Cloud Run scale time for the rest.
    # Never prime ABOVE the service minimum: a cold region (min 0/1) should
    # not pay for primer instances its steady state never runs.
    revision_min_instances="$(cloud_run_candidate_min_instances "$min_instances")"
    # CAVEAT (review F5): this bakes today's primer into the revision
    # forever — effective capacity is max(service, revision). With the primer
    # capped at min(2, service minimum), a later reduction of a region's
    # service minimum below the primer still needs a redeploy to fully take
    # effect, but the exposure is at most 2 instances. The max-not-sum
    # and tag-activation semantics are platform behavior asserted here, not
    # testable against stubs — verify billing once after the first prod run.
    log "capacity before ${target}: service min=${min_instances}; candidate revision min=${revision_min_instances}"
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
      --concurrency "$TR_CLOUD_RUN_CONCURRENCY" \
      --min "$min_instances" \
      --min-instances "$revision_min_instances" \
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

warm_no_traffic_candidate() {
  local target="$1"
  local revision="$2"
  local min_instances="$3"
  local base_url
  local service_ingress

  # The tag mutation itself is traffic-free. Mark cleanup required before the
  # call because a failed reconciliation can still have applied the tag.
  WARM_PROBE_REGIONS+=("$target")
  WARM_PROBE_CLEANUP_REQUIRED=1
  log "assigning ${WARM_PROBE_TAG} to ${revision} during the no-traffic warm"
  cloud_run_probe_tag_reconcile \
    "$SERVICE" "$target" "$PROJECT_ID" "$WARM_PROBE_TAG" "$revision"
  if ! service_ingress="$(cloud_run_service_ingress \
      "$SERVICE" "$target" "$PROJECT_ID")"; then
    echo "ERROR: could not determine Cloud Run ingress for ${SERVICE} in ${target}" >&2
    return 1
  fi
  if [ "$service_ingress" = "all" ]; then
    base_url="$(cloud_run_probe_tagged_base_url \
      "$SERVICE" "$target" "$PROJECT_ID" "$WARM_PROBE_TAG" "$revision")"
    # Public run.app ingress can prove the application route before traffic
    # moves. Protected services deliberately return a Google platform 404 on
    # this URL and use the capacity check below instead.
    curl -fsS --max-time 30 --retry 5 --retry-all-errors \
      "${base_url}/ready" >/dev/null
  else
    # The tag activates the revision minimum without assigning production
    # traffic. Verify both process readiness and the requested warm capacity
    # through the Cloud Run control plane. The staged LB watchdog below owns
    # HTTP validation once the candidate receives canary traffic.
    cloud_run_revision_capacity_ready \
      "$SERVICE" "$target" "$PROJECT_ID" "$revision" "$min_instances"
  fi
  log "capacity after ${target} warm: ${revision} ready at revision min=${min_instances}; service min unchanged"
}

if [ "${TR_DEPLOY_NO_TRAFFIC:-0}" = "1" ]; then
  for warm_target in "${TARGETS[@]}"; do
    warm_revision="$(latest_ready_revision_for_region "$warm_target")"
    if [ -z "$warm_revision" ]; then
      echo "ERROR: could not find warmed Ready revision for ${warm_target}" >&2
      exit 1
    fi
    if deploy_region_is_held "$warm_target"; then
      deploy_warn_region_held "$warm_target"
    else
      warm_service_min_instances="$(cloud_run_service_min_instances_for_region "$warm_target")"
      warm_min_instances="$(cloud_run_candidate_min_instances "$warm_service_min_instances")"
      warm_no_traffic_candidate \
        "$warm_target" "$warm_revision" "$warm_min_instances"
    fi
  done
fi

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
    --serve-while-stale=86400 \
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
