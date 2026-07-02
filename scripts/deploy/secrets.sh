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

ensure_secret_from_prompt_file() {
  local secret_name="$1"
  local prompt_file="$2"
  local section="$3"
  local value
  if [ ! -f "$prompt_file" ]; then
    if gc secrets describe "$secret_name" >/dev/null 2>&1; then
      log "using existing prompt secret ${secret_name}"
    else
      log "WARN: prompt file ${prompt_file} missing; ${secret_name} not uploaded"
    fi
    return 0
  fi
  value="$(python3 - "$prompt_file" "$section" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
section = sys.argv[2]
text = path.read_text()
match = re.search(
    rf"^##\s+{re.escape(section)}\s*$.*?```(?:text)?\s*\n(.*?)\n```",
    text,
    flags=re.M | re.S,
)
if not match:
    raise SystemExit(f"section not found: {section}")
print(match.group(1).strip())
PY
)"
  if [ -z "$value" ]; then
    echo "ERROR: ${prompt_file} section '${section}' is empty." >&2
    exit 1
  fi
  ensure_secret_value "$secret_name" "$value"
  log "uploaded prompt secret ${secret_name}"
}

ensure_secret_from_env_file "ANTHROPIC_API_KEY" "trustedrouter-anthropic-api-key" "CLAUDE_API_KEY"
ensure_secret_from_env_file "OPENAI_API_KEY" "trustedrouter-openai-api-key" "CHATGPT_API_KEY"
ensure_secret_from_env_file "GEMINI_API_KEY" "trustedrouter-gemini-api-key"
ensure_secret_from_env_file "CEREBRAS_API_KEY" "trustedrouter-cerebras-api-key"
ensure_secret_from_env_file "DEEPSEEK_API_KEY" "trustedrouter-deepseek-api-key"
ensure_secret_from_env_file "MISTRAL_API_KEY" "trustedrouter-mistral-api-key"
ensure_secret_from_env_file "KIMI_API_KEY" "trustedrouter-kimi-api-key" "MOONSHOT_API_KEY"
ensure_secret_from_env_file "ZAI_API_KEY" "trustedrouter-zai-api-key" "ZHIPU_API_KEY" "Z_AI_API_KEY"
ensure_secret_from_env_file "TOGETHER_API_KEY" "trustedrouter-together-api-key" "TOGETHERAI_API_KEY" "TOGETHER_AI_API_KEY"
ensure_secret_from_env_file "FIREWORKS_API_KEY" "trustedrouter-fireworks-api-key" "FIREWORKS_AI_API_KEY"
ensure_secret_from_env_file "DEEPINFRA_API_KEY" "trustedrouter-deepinfra-api-key"
# Cohere — embeddings only (native /v2/embed in the enclave). Reads
# COHERE_API_KEY from ~/.quill_cloud_keys.private. Runtime read access comes
# from the project-level secretAccessor binding (infra.sh), like every other
# provider secret.
ensure_secret_from_env_file "COHERE_API_KEY" "trustedrouter-cohere-api-key"
# Voyage AI — embeddings only (OpenAI-shaped /v1/embeddings in the enclave).
# Reads VOYAGE_API_KEY from ~/.quill_cloud_keys.private. Same project-level
# secretAccessor binding as every other provider secret.
ensure_secret_from_env_file "VOYAGE_API_KEY" "trustedrouter-voyage-api-key"
# Xiaomi MiMo — OpenAI-compatible chat (api.xiaomimimo.com/v1). Reads
# XIAOMI_API_KEY from ~/.quill_cloud_keys.private.
ensure_secret_from_env_file "XIAOMI_API_KEY" "trustedrouter-xiaomi-api-key"
# 2026-05 — six new backend providers. All OpenAI-compatible chat
# completions; the existing enclave OpenAI-shape adapter dispatches
# to whichever base URL the catalog selects per model.
ensure_secret_from_env_file "GROK_API_KEY" "trustedrouter-grok-api-key" "XAI_API_KEY"
ensure_secret_from_env_file "NOVITA_API_KEY" "trustedrouter-novita-api-key"
ensure_secret_from_env_file "PHALA_API_KEY" "trustedrouter-phala-api-key" "REDPILL_API_KEY"
# Phala's GPU-TEE-attested confidential AI tier (issued from
# cloud.phala.com dashboard). This is what the enclave actually
# routes against now via the `phala/<bare>` model id form per
# docs.phala.com/phala-cloud/confidential-ai. The plain
# trustedrouter-phala-api-key above is the upstream-pass-through
# redpill key (kept around for completeness; the enclave's
# QUILL_PHALA_SECRET default now points at the confidential one).
ensure_secret_from_env_file "PHALA_CONFIDENTIAL_API_KEY" "trustedrouter-phala-confidential-api-key"
ensure_secret_from_env_file "SILICON_FLOW_API_KEY" "trustedrouter-siliconflow-api-key" "SILICONFLOW_API_KEY"
ensure_secret_from_env_file "TINFOIL_API_KEY" "trustedrouter-tinfoil-api-key"
ensure_secret_from_env_file "VENICE_API_KEY" "trustedrouter-venice-api-key"
ensure_secret_from_env_file "NEBIUS_API_KEY" "trustedrouter-nebius-api-key" "NEBIUS_TOKEN_FACTORY_API_KEY"
ensure_secret_from_env_file "MINIMAX_API_KEY" "trustedrouter-minimax-api-key" "MINIMAX_TOKEN_PLAN_API_KEY"
ensure_secret_from_env_file "BASETEN_API_KEY" "trustedrouter-baseten-api-key"
ensure_secret_from_env_file "WAFER_API_KEY" "trustedrouter-wafer-api-key"
ensure_secret_from_env_file "CRUSOE_API_KEY" "trustedrouter-crusoe-api-key"
ensure_secret_from_env_file "ALIBABA_API_KEY" "trustedrouter-alibaba-api-key" "DASHSCOPE_API_KEY" "ALIYUN_API_KEY"

SYNTH_PROMPTS_FILE="${TR_SYNTH_PROMPTS_FILE:-${HOME}/.trustedrouter_synth_prompts_v1.md}"
SYNTH_CODE_PROMPTS_FILE="${TR_SYNTH_CODE_PROMPTS_FILE:-/Users/jperla/claude/fusion-code-prompts-v1.md}"
SOCRATES_PROMPTS_FILE="${TR_SOCRATES_PROMPTS_FILE:-${HOME}/.trustedrouter_socrates_prompts_v1.md}"
ATHENA_PROMPTS_FILE="${TR_ATHENA_PROMPTS_FILE:-${HOME}/.trustedrouter_athena_prompts_v1.md}"
ensure_secret_from_prompt_file "trustedrouter-synth-panel-prompt-v1" "$SYNTH_PROMPTS_FILE" "Panel Prompt V1"
ensure_secret_from_prompt_file "trustedrouter-synth-synthesis-prompt-v1" "$SYNTH_PROMPTS_FILE" "Synthesis Prompt V1"
ensure_secret_from_prompt_file "trustedrouter-synth-code-panel-prompt-v1" "$SYNTH_CODE_PROMPTS_FILE" "Panel Prompt V1"
ensure_secret_from_prompt_file "trustedrouter-synth-code-synthesis-prompt-v1" "$SYNTH_CODE_PROMPTS_FILE" "Synthesis Prompt V1"
ensure_secret_from_prompt_file "trustedrouter-socrates-worker-prompt-v1" "$SOCRATES_PROMPTS_FILE" "Worker Prompt V1"
ensure_secret_from_prompt_file "trustedrouter-socrates-advisor-prompt-v1" "$SOCRATES_PROMPTS_FILE" "Advisor Prompt V1"
ensure_secret_from_prompt_file "trustedrouter-athena-worker-prompt-v1" "$ATHENA_PROMPTS_FILE" "Worker Prompt V1"

ensure_secret_from_env_file "TR_SYNTHETIC_MONITOR_API_KEY" "trustedrouter-synthetic-monitor-api-key" "SYNTHETIC_MONITOR_API_KEY"
ensure_secret_from_env_file "SENTRY_DSN" "trustedrouter-sentry-dsn"

# Self-heal LLM key for the hourly pricing refresh GHA workflow.
# Read from the local key file like every other TR secret; pushed to
# Secret Manager and granted to tr-deploy@ (the GHA WIF SA) so the
# workflow can pull it via `gcloud secrets versions access`.
ensure_secret_from_env_file "TR_API_KEY_FOR_SELF_HEAL" "trustedrouter-tr-api-key-for-self-heal"
# Bind tr-deploy@ here, right next to the secret creation, so that a
# downstream `set -e` on a later step (e.g. an etag-conflict on a
# project-role call) cannot strand this secret without an accessor.
# `|| log` so add-iam-policy-binding's harmless "already exists" or
# transient etag conflicts do not abort the rest of the script.
TR_DEPLOY_SA="${TR_DEPLOY_SA:-tr-deploy@${PROJECT_ID}.iam.gserviceaccount.com}"
if gc secrets describe trustedrouter-tr-api-key-for-self-heal >/dev/null 2>&1; then
  log "granting ${TR_DEPLOY_SA} accessor on trustedrouter-tr-api-key-for-self-heal"
  gc secrets add-iam-policy-binding trustedrouter-tr-api-key-for-self-heal \
    --member="serviceAccount:${TR_DEPLOY_SA}" \
    --role="roles/secretmanager.secretAccessor" \
    --quiet >/dev/null \
    || log "WARN: per-secret binding returned non-zero (may already be present)"
fi

# Axiom logging — ship structured logs to a dedicated dataset for
# slice-and-dice analysis (request_id correlation, rate-limit hits,
# Bigtable write failures, etc.). The runtime SA reads
# AXIOM_API_TOKEN from Secret Manager; the dataset name is plain
# config and lives in env, not in Secret Manager.
ensure_secret_from_env_file "AXIOM_API_TOKEN" "trustedrouter-axiom-api-token" "AXIOM_TOKEN" "AXIOM_API_KEY"
require_secret_from_env_file "STRIPE_SECRET_KEY" "trustedrouter-stripe-secret-key" "STRIPE_KEY"
require_secret_from_env_file "STRIPE_WEBHOOK_SECRET" "trustedrouter-stripe-webhook-secret"

# OAuth + SES secrets — independently optional. Push to Secret Manager only
# when the local keys file (or env) supplies a value; production fail-closed
# rule treats half-configured providers as a hard error.
ensure_secret_from_env_file "GOOGLE_CLIENT_ID" "trustedrouter-google-client-id"
ensure_secret_from_env_file "GOOGLE_CLIENT_SECRET" "trustedrouter-google-client-secret"
ensure_secret_from_env_file "GITHUB_CLIENT_ID" "trustedrouter-github-client-id"
ensure_secret_from_env_file "GITHUB_CLIENT_SECRET" "trustedrouter-github-client-secret"
# SES email credentials only; not used for AWS hosting or failover.
ensure_secret_from_env_file "AWS_ACCESS_KEY_ID" "trustedrouter-aws-access-key-id"
ensure_secret_from_env_file "AWS_SECRET_ACCESS_KEY" "trustedrouter-aws-secret-access-key"
ensure_secret_from_env_file "PAYPAL_CLIENT_ID" "trustedrouter-paypal-client-id"
ensure_secret_from_env_file "PAYPAL_CLIENT_SECRET" "trustedrouter-paypal-client-secret"
ensure_secret_from_env_file "PAYPAL_WEBHOOK_ID" "trustedrouter-paypal-webhook-id"
if ! gc secrets describe trustedrouter-internal-gateway-token >/dev/null 2>&1; then
  ensure_secret_value trustedrouter-internal-gateway-token "$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"
  log "generated secret trustedrouter-internal-gateway-token"
fi

# Runtime-SA project-level IAM bindings live in infra.sh (Phase 1
# bootstrap, run as a project Owner). Calling projects.setIamPolicy
# requires roles/resourcemanager.projectIamAdmin, which the deploy SA
# (tr-deploy@) deliberately does not have — granting it would let any
# CI run mutate project IAM. secrets.sh runs as tr-deploy@ for secret
# rotation, so it cannot do project-level bindings.
