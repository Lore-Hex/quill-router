#!/usr/bin/env bash
# Phase 3: push provider/OAuth/SES/Stripe secrets to Secret Manager and
# grant the Cloud Run runtime service account access to them. Reads
# values from $TR_LOCAL_KEYS_FILE (~/.quill_cloud_keys.private by default)
# or from the current shell environment.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

ensure_secret_from_env_file() {
  local env_name="$1"
  local secret_name="$2"
  shift 2 || true
  local value
  value="${!env_name:-}"
  if [ -z "$value" ]; then
    value="$(read_key_file_var "$env_name" "$@")"
  fi
  if [ -z "$value" ]; then
    return 0
  fi
  ensure_secret_value "$secret_name" "$value"
  log "uploaded secret ${secret_name}"
}

require_secret_from_env_file() {
  local env_name="$1"
  local secret_name="$2"
  shift 2 || true
  local value
  value="${!env_name:-}"
  if [ -z "$value" ]; then
    value="$(read_key_file_var "$env_name" "$@")"
  fi
  if [ -z "$value" ]; then
    if gc secrets describe "$secret_name" >/dev/null 2>&1; then
      log "using existing secret ${secret_name}"
      return 0
    fi
    echo "ERROR: ${env_name} is required for production deploy." >&2
    echo "Set it in ${KEY_FILE} or export ${env_name}, then re-run." >&2
    exit 1
  fi
  ensure_secret_value "$secret_name" "$value"
  log "uploaded secret ${secret_name}"
}

ensure_secret_from_env_file "ANTHROPIC_API_KEY" "trustedrouter-anthropic-api-key" "CLAUDE_API_KEY"
ensure_secret_from_env_file "OPENAI_API_KEY" "trustedrouter-openai-api-key" "CHATGPT_API_KEY"
ensure_secret_from_env_file "GEMINI_API_KEY" "trustedrouter-gemini-api-key"
ensure_secret_from_env_file "CEREBRAS_API_KEY" "trustedrouter-cerebras-api-key"
ensure_secret_from_env_file "DEEPSEEK_API_KEY" "trustedrouter-deepseek-api-key"
ensure_secret_from_env_file "MISTRAL_API_KEY" "trustedrouter-mistral-api-key"
ensure_secret_from_env_file "ZAI_API_KEY" "trustedrouter-zai-api-key" "ZHIPU_API_KEY" "Z_AI_API_KEY"
ensure_secret_from_env_file "SENTRY_DSN" "trustedrouter-sentry-dsn"
require_secret_from_env_file "STRIPE_SECRET_KEY" "trustedrouter-stripe-secret-key" "STRIPE_KEY"
require_secret_from_env_file "STRIPE_WEBHOOK_SECRET" "trustedrouter-stripe-webhook-secret"

# OAuth + SES secrets — independently optional. Push to Secret Manager only
# when the local keys file (or env) supplies a value; production fail-closed
# rule treats half-configured providers as a hard error.
ensure_secret_from_env_file "GOOGLE_CLIENT_ID" "trustedrouter-google-client-id"
ensure_secret_from_env_file "GOOGLE_CLIENT_SECRET" "trustedrouter-google-client-secret"
ensure_secret_from_env_file "GITHUB_CLIENT_ID" "trustedrouter-github-client-id"
ensure_secret_from_env_file "GITHUB_CLIENT_SECRET" "trustedrouter-github-client-secret"
ensure_secret_from_env_file "AWS_ACCESS_KEY_ID" "trustedrouter-aws-access-key-id"
ensure_secret_from_env_file "AWS_SECRET_ACCESS_KEY" "trustedrouter-aws-secret-access-key"
if ! gc secrets describe trustedrouter-internal-gateway-token >/dev/null 2>&1; then
  ensure_secret_value trustedrouter-internal-gateway-token "$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"
  log "generated secret trustedrouter-internal-gateway-token"
fi

log "ensuring runtime IAM for ${RUN_SERVICE_ACCOUNT}"
ensure_project_role "serviceAccount:${RUN_SERVICE_ACCOUNT}" "roles/secretmanager.secretAccessor"
ensure_project_role "serviceAccount:${RUN_SERVICE_ACCOUNT}" "roles/spanner.databaseUser"
ensure_project_role "serviceAccount:${RUN_SERVICE_ACCOUNT}" "roles/bigtable.user"
ensure_project_role "serviceAccount:${RUN_SERVICE_ACCOUNT}" "roles/aiplatform.user"
