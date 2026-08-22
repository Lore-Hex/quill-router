#!/usr/bin/env bash
# Deploy (or redeploy) the canary Container App.
#
# Split from azure_canary.sh so the application can be shipped repeatedly
# without touching the database — which is the whole point of a canary: fast,
# repeatable deploys.
#
# Read azure_canary.sh's header for scope. This is a pipeline test: one region,
# no HA, no inference, not on any SLO, never advertised.
set -euo pipefail

LOCATION="${LOCATION:-northeurope}"
CANARY="${CANARY:-tr-canary}"
APP_LOCATION="${APP_LOCATION:-swedencentral}"
RG="${RG:-$CANARY}"
PG_NAME="${PG_NAME:-$CANARY-pg}"
PG_ADMIN="${PG_ADMIN:-tradmin}"
PG_DB="${PG_DB:-trustedrouter}"
ACR="${ACR:-$(echo "${CANARY}${APP_LOCATION}acr" | tr -cd "[:alnum:]")}"
APP_ENV="${APP_ENV:-$CANARY-env}"
APP="${APP:-$CANARY}"
IMAGE_TAG="${IMAGE_TAG:-canary}"
# Bootstrap password location. Fixed, not $TMPDIR — these scripts run as
# separate processes with different temp dirs.
STATE_DIR="${STATE_DIR:-$HOME/.config/$CANARY}"
PW_FILE="${PW_FILE:-$STATE_DIR/pgpw}"
ATTRIBUTION_SECRET_NAME="attribution-cookie-secret"

log() { printf '\n=== %s\n' "$*" >&2; }
exists() { "$@" >/dev/null 2>&1; }

PG_HOST="$(az postgres flexible-server show -g "$RG" -n "$PG_NAME" --query fullyQualifiedDomainName -o tsv)"
ACR_SERVER="$(az acr show -g "$RG" -n "$ACR" --query loginServer -o tsv)"
ACR_USER="$(az acr credential show -n "$ACR" --query username -o tsv)"
ACR_PASS="$(az acr credential show -n "$ACR" --query 'passwords[0].value' -o tsv)"

APP_EXISTS=0
if exists az containerapp show -g "$RG" -n "$APP"; then
  APP_EXISTS=1
fi

# Password source of truth: the Container App secret if the app already exists,
# otherwise the file azure_canary.sh wrote when it created the server. Never
# echoed either way.
if [ "$APP_EXISTS" = "1" ]; then
  PG_PASSWORD="$(az containerapp secret show -g "$RG" -n "$APP" --secret-name pg-password --query value -o tsv)"
else
  
  [ -f "$PW_FILE" ] || { echo "no password: run azure_canary.sh first, or reset it with 'az postgres flexible-server update --admin-password'" >&2; exit 1; }
  PG_PASSWORD="$(cat "$PW_FILE")"
fi

# The public canary signs only its own attribution cookie. This value is an
# application secret, deliberately distinct from the internal billing-gateway
# token, and is persisted across redeploys instead of rotating every revision.
ATTRIBUTION_COOKIE_SECRET=""
if [ "$APP_EXISTS" = "1" ]; then
  ATTRIBUTION_COOKIE_SECRET="$(az containerapp secret show \
    -g "$RG" -n "$APP" \
    --secret-name "$ATTRIBUTION_SECRET_NAME" \
    --query value -o tsv 2>/dev/null || true)"
fi
if [ "${#ATTRIBUTION_COOKIE_SECRET}" -lt 32 ]; then
  ATTRIBUTION_COOKIE_SECRET="$(openssl rand -hex 32)"
  case "$ATTRIBUTION_COOKIE_SECRET" in
    *[!0-9a-f]*|'')
      echo "could not generate a valid canary attribution secret" >&2
      exit 1
      ;;
  esac
  [ "${#ATTRIBUTION_COOKIE_SECRET}" -eq 64 ] || {
    echo "could not generate a 32-byte canary attribution secret" >&2
    exit 1
  }
  if [ "$APP_EXISTS" = "1" ]; then
    az containerapp secret set -g "$RG" -n "$APP" \
      --secrets "${ATTRIBUTION_SECRET_NAME}=${ATTRIBUTION_COOKIE_SECRET}" -o none
  fi
fi

# sslmode=require: Flexible Server enforces TLS, and psycopg would otherwise
# negotiate down and fail on a server that refuses plaintext.
DSN="postgresql://${PG_ADMIN}:${PG_PASSWORD}@${PG_HOST}:5432/${PG_DB}?sslmode=require"

log "deploying $APP from ${ACR_SERVER}/trusted-router:${IMAGE_TAG}"

# TR_ENVIRONMENT stays 'canary', NOT 'production' — several code paths branch on
# it, and a pipeline test must never be mistaken for a live region in logs,
# Sentry, or analytics.
COMMON_ENV=(
  "TR_ENVIRONMENT=canary"
  "TR_SERVICE_SURFACE=public"
  "TR_NEW_SIGNUPS_ENABLED=false"
  "TR_ATTRIBUTION_COOKIE_SECRET=secretref:${ATTRIBUTION_SECRET_NAME}"
  # Public renders links from capability flags; it never owns OAuth clients.
  # Keep both explicit so production validation cannot silently infer from a
  # credential that should not exist on this surface.
  "TR_GOOGLE_OAUTH_LOGIN_AVAILABLE=false"
  "TR_GITHUB_OAUTH_LOGIN_AVAILABLE=false"
  "TR_RATE_LIMIT_CLIENT_IP_MODE=untrusted"
  "TR_MAX_REQUEST_BODY_BYTES=4194304"
  "TR_MAX_IN_FLIGHT_REQUEST_BODY_BYTES=8388608"
  "TR_MAX_CONCURRENT_REQUEST_BODIES=2"
  "TR_REQUEST_BODY_READ_TIMEOUT_SECONDS=10"
  "TR_RELEASE=${IMAGE_TAG}"
  "TR_STORAGE_BACKEND=postgres"
  "TR_ENABLE_LIVE_PROVIDERS=false"
  "TR_TRUSTED_DOMAIN=trustedrouter.com"
)
RETIRED_PUBLIC_OAUTH_ENV_VARS=(
  TR_GOOGLE_CLIENT_ID
  TR_GOOGLE_CLIENT_SECRET
  TR_GOOGLE_OAUTH_REDIRECT_URL
  TR_GOOGLE_ALIAS_CREDENTIALS_JSON
  TR_GITHUB_CLIENT_ID
  TR_GITHUB_CLIENT_SECRET
  TR_GITHUB_OAUTH_REDIRECT_URL
  TR_GITHUB_ALIAS_CREDENTIALS_JSON
)

if [ "$APP_EXISTS" = "1" ]; then
  log "updating existing app"
  az containerapp update -g "$RG" -n "$APP" \
    --image "${ACR_SERVER}/trusted-router:${IMAGE_TAG}" \
    --set-env-vars "${COMMON_ENV[@]}" "TR_POSTGRES_DSN=secretref:pg-dsn" \
    --remove-env-vars "${RETIRED_PUBLIC_OAUTH_ENV_VARS[@]}" \
    --min-replicas 1 --max-replicas "${OBSERVER_MAX_REPLICAS:-2}" \
    --scale-rule-name observer-http \
    --scale-rule-type http \
    --scale-rule-http-concurrency "${OBSERVER_HTTP_CONCURRENCY:-10}" -o none
else
  log "creating app"
  az containerapp create -g "$RG" -n "$APP" \
    --environment "$APP_ENV" \
    --image "${ACR_SERVER}/trusted-router:${IMAGE_TAG}" \
    --registry-server "$ACR_SERVER" \
    --registry-username "$ACR_USER" \
    --registry-password "$ACR_PASS" \
    --secrets "pg-password=${PG_PASSWORD}" "pg-dsn=${DSN}" \
      "${ATTRIBUTION_SECRET_NAME}=${ATTRIBUTION_COOKIE_SECRET}" \
    --env-vars "${COMMON_ENV[@]}" "TR_POSTGRES_DSN=secretref:pg-dsn" \
    --target-port 8080 --ingress external \
    --min-replicas 1 --max-replicas "${OBSERVER_MAX_REPLICAS:-2}" \
    --scale-rule-name observer-http \
    --scale-rule-type http \
    --scale-rule-http-concurrency "${OBSERVER_HTTP_CONCURRENCY:-10}" \
    --cpu 0.5 --memory 1.0Gi -o none
fi

# Omission is not cleanup on Container Apps updates. Prove the serving
# template has neither old OAuth credentials nor ambiguous capability flags
# before accepting the revision as a valid public surface.
configured_env_names="$(az containerapp show -g "$RG" -n "$APP" \
  --query 'properties.template.containers[0].env[].name' -o tsv)"
configured_env_names="${configured_env_names//$'\t'/$'\n'}"
while IFS= read -r configured_env_name; do
  for retired_env_name in "${RETIRED_PUBLIC_OAUTH_ENV_VARS[@]}"; do
    if [ "$configured_env_name" = "$retired_env_name" ]; then
      echo "public canary retains forbidden OAuth env ${retired_env_name}" >&2
      exit 1
    fi
  done
done <<<"$configured_env_names"

configured_google_login_available="$(az containerapp show -g "$RG" -n "$APP" \
  --query "properties.template.containers[0].env[?name=='TR_GOOGLE_OAUTH_LOGIN_AVAILABLE'].value | [0]" -o tsv)"
configured_github_login_available="$(az containerapp show -g "$RG" -n "$APP" \
  --query "properties.template.containers[0].env[?name=='TR_GITHUB_OAUTH_LOGIN_AVAILABLE'].value | [0]" -o tsv)"
if [ "$configured_google_login_available" != "false" ] \
    || [ "$configured_github_login_available" != "false" ]; then
  echo "public canary OAuth capability verification failed: google=${configured_google_login_available:-unset} github=${configured_github_login_available:-unset}" >&2
  exit 1
fi

az containerapp revision set-mode -g "$RG" -n "$APP" --mode single -o none
active_revision_mode="$(az containerapp show -g "$RG" -n "$APP" \
  --query properties.configuration.activeRevisionsMode -o tsv)"
configured_max_replicas="$(az containerapp show -g "$RG" -n "$APP" \
  --query properties.template.scale.maxReplicas -o tsv)"
configured_http_concurrency="$(az containerapp show -g "$RG" -n "$APP" \
  --query "properties.template.scale.rules[?name=='observer-http'].http.metadata.concurrentRequests | [0]" \
  -o tsv)"
case "$active_revision_mode" in
  Single|single) mode_verified=1 ;;
  *) mode_verified=0 ;;
esac
if [ "$mode_verified" != "1" ] \
    || [ "$configured_max_replicas" != "${OBSERVER_MAX_REPLICAS:-2}" ] \
    || [ "$configured_http_concurrency" != "${OBSERVER_HTTP_CONCURRENCY:-10}" ]; then
  echo "observer scale verification failed: mode=${active_revision_mode:-unset} max=${configured_max_replicas:-unset} concurrency=${configured_http_concurrency:-unset}" >&2
  exit 1
fi

FQDN="$(az containerapp show -g "$RG" -n "$APP" --query properties.configuration.ingress.fqdn -o tsv)"
log "app URL: https://${FQDN}"
log "verify with: bash scripts/deploy/azure_canary_verify.sh"
echo "https://${FQDN}"
