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

log() { printf '\n=== %s\n' "$*" >&2; }
exists() { "$@" >/dev/null 2>&1; }

PG_HOST="$(az postgres flexible-server show -g "$RG" -n "$PG_NAME" --query fullyQualifiedDomainName -o tsv)"
ACR_SERVER="$(az acr show -g "$RG" -n "$ACR" --query loginServer -o tsv)"
ACR_USER="$(az acr credential show -n "$ACR" --query username -o tsv)"
ACR_PASS="$(az acr credential show -n "$ACR" --query 'passwords[0].value' -o tsv)"

# Password source of truth: the Container App secret if the app already exists,
# otherwise the file azure_canary.sh wrote when it created the server. Never
# echoed either way.
if exists az containerapp show -g "$RG" -n "$APP"; then
  PG_PASSWORD="$(az containerapp secret show -g "$RG" -n "$APP" --secret-name pg-password --query value -o tsv)"
else
  
  [ -f "$PW_FILE" ] || { echo "no password: run azure_canary.sh first, or reset it with 'az postgres flexible-server update --admin-password'" >&2; exit 1; }
  PG_PASSWORD="$(cat "$PW_FILE")"
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
  "TR_RELEASE=${IMAGE_TAG}"
  "TR_STORAGE_BACKEND=postgres"
  "TR_ENABLE_LIVE_PROVIDERS=false"
  "TR_TRUSTED_DOMAIN=trustedrouter.com"
)

if exists az containerapp show -g "$RG" -n "$APP"; then
  log "updating existing app"
  az containerapp update -g "$RG" -n "$APP" \
    --image "${ACR_SERVER}/trusted-router:${IMAGE_TAG}" \
    --set-env-vars "${COMMON_ENV[@]}" "TR_POSTGRES_DSN=secretref:pg-dsn" -o none
else
  log "creating app"
  az containerapp create -g "$RG" -n "$APP" \
    --environment "$APP_ENV" \
    --image "${ACR_SERVER}/trusted-router:${IMAGE_TAG}" \
    --registry-server "$ACR_SERVER" \
    --registry-username "$ACR_USER" \
    --registry-password "$ACR_PASS" \
    --secrets "pg-password=${PG_PASSWORD}" "pg-dsn=${DSN}" \
    --env-vars "${COMMON_ENV[@]}" "TR_POSTGRES_DSN=secretref:pg-dsn" \
    --target-port 8080 --ingress external \
    --min-replicas 1 --max-replicas 2 \
    --cpu 0.5 --memory 1.0Gi -o none
fi

FQDN="$(az containerapp show -g "$RG" -n "$APP" --query properties.configuration.ingress.fqdn -o tsv)"
log "app URL: https://${FQDN}"
log "verify with: bash scripts/deploy/azure_canary_verify.sh"
echo "https://${FQDN}"
