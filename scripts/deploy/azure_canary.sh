#!/usr/bin/env bash
# Azure canary: the smallest possible end-to-end TrustedRouter deployment.
#
# PURPOSE — this is a DEPLOY-PIPELINE TEST, not a production region.
#
# It exists to answer "does the image build, ship, boot, reach its own database,
# and serve its own status page on a non-GCP cloud?" quickly and cheaply, so
# that the expensive EU AWS build is not the first place we discover a broken
# assumption. Deploy here first, then AWS.
#
# DELIBERATE CHOICE OF DATABASE. docs/storage-portability/multi-cloud-separation.md
# recommends Cosmos DB for PostgreSQL (Citus) for a *production* Azure region,
# because Citus can distribute by workspace_id. This canary uses plain
# **Flexible Server, Burstable B1ms** instead — Citus has a substantial minimum
# node cost, and the thing being tested here is the pipeline, not the sharding
# behaviour. PostgresStore already passes conformance against stock Postgres, so
# this is a faithful test of the application. If Azure ever becomes a production
# region, re-validate on Citus with the conformance suite before trusting it.
#
# SCOPE. One region, no HA, no multi-AZ, no failover, no attestation, no
# inference. It is not on any uptime SLO and must never be advertised.
#
# Cost is roughly $20-25/month. Tear it down with azure_canary_teardown.sh when
# it is not earning that.
#
# Idempotent: every create is check-then-create.
set -euo pipefail

# This subscription is capacity-restricted PER SERVICE, not per region, and the
# restrictions do not agree with each other: Postgres is disallowed in
# westeurope but fine in northeurope, while ACR is the exact reverse. So the
# database and the registry/app deliberately live in DIFFERENT regions. Both are
# EU member states, which is all the canary needs — it is not the EU-residency
# deployment (that is AWS; see aws-eu-and-azure-canary.md).
#
# If a create fails with "region is currently not accepting new customers",
# that is this, not a bug. Probe another region.
LOCATION="${LOCATION:-northeurope}"          # Postgres
APP_LOCATION="${APP_LOCATION:-swedencentral}"  # ACR + Container Apps
RG="${RG:-tr-canary}"
PG_NAME="${PG_NAME:-tr-canary-pg}"
PG_ADMIN="${PG_ADMIN:-tradmin}"
PG_DB="${PG_DB:-trustedrouter}"
ACR="${ACR:-trcanaryswedencentralacr}"
APP_ENV="${APP_ENV:-tr-canary-env}"
APP="${APP:-tr-canary}"
IMAGE_TAG="${IMAGE_TAG:-canary}"
# Bootstrap password location. Fixed, not $TMPDIR — these scripts run as
# separate processes with different temp dirs.
STATE_DIR="${STATE_DIR:-$HOME/.config/tr-canary}"
PW_FILE="${PW_FILE:-$STATE_DIR/pgpw}"

log() { printf '\n=== %s\n' "$*" >&2; }
exists() { "$@" >/dev/null 2>&1; }

log "subscription: $(az account show --query name -o tsv) / location=$LOCATION"

# ------------------------------------------------------------------ group
if exists az group show -n "$RG"; then
  log "resource group $RG exists"
else
  log "creating resource group $RG"
  az group create -n "$RG" -l "$LOCATION" -o none
fi

# --------------------------------------------------------------- postgres
# Burstable B1ms is the smallest tier. Public access with a firewall rather
# than a VNet: a canary that needs a bastion to debug defeats its own purpose.
# It holds no production data — see SCOPE above.
if exists az postgres flexible-server show -g "$RG" -n "$PG_NAME"; then
  log "postgres $PG_NAME exists"
else
  log "creating postgres $PG_NAME (Standard_B1ms, 32GB)"
  # Generated here, never echoed; stored straight into the Container App as a
  # secret below and readable afterwards only via `az containerapp secret show`.
  PG_PASSWORD="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24),end="")')"
  az postgres flexible-server create \
    -g "$RG" -n "$PG_NAME" -l "$LOCATION" \
    --admin-user "$PG_ADMIN" --admin-password "$PG_PASSWORD" \
    --tier Burstable --sku-name Standard_B1ms \
    --storage-size 32 --version 16 \
    --public-access 0.0.0.0 --yes -o none
  # -n is the DATABASE name here (-d is not a flag). This was wrong AND the
  # error was swallowed by `|| true`, so the server came up without its
  # database and the app crash-looped on connect with a message that pointed
  # at the pool, not at the missing database. Never silence a create that the
  # deployment depends on.
  az postgres flexible-server db create -g "$RG" -s "$PG_NAME" -n "$PG_DB" -o none
  # Stash it so azure_canary_app.sh can build the Container App without
  # recreating the server. Once that app exists, ITS secret is the system of
  # record and this file is only a bootstrap crutch.
  #
  # A fixed path, not $TMPDIR: the two scripts routinely run as different
  # processes (and CI jobs) with different temp directories, so a TMPDIR
  # handoff silently loses the password and the second script fails with a
  # confusing "no password" error.
  mkdir -p "$STATE_DIR"
  ( umask 077 && printf '%s' "$PG_PASSWORD" > "$PW_FILE" )
fi

# Container Apps egresses from shared Azure IPs. `--public-access 0.0.0.0` at
# create time already installs the "all Azure services" rule, so this only adds
# an operator IP for debugging when TR_DEV_IP is set.
#
# NOTE the flags: -s is the SERVER and -n is the RULE. Getting that wrong (-r
# does not exist) fails, and the original `|| true` hid it completely.
if [ -n "${TR_DEV_IP:-}" ]; then
  log "allowing operator IP $TR_DEV_IP"
  az postgres flexible-server firewall-rule create \
    -g "$RG" -s "$PG_NAME" -n devbox \
    --start-ip-address "$TR_DEV_IP" --end-ip-address "$TR_DEV_IP" -o none
fi

PG_HOST="$(az postgres flexible-server show -g "$RG" -n "$PG_NAME" --query fullyQualifiedDomainName -o tsv)"
log "postgres host: $PG_HOST"

# -------------------------------------------------------------------- acr
if exists az acr show -g "$RG" -n "$ACR"; then
  log "acr $ACR exists"
else
  log "creating acr $ACR (Basic)"
  az acr create -g "$RG" -n "$ACR" -l "$APP_LOCATION" --sku Basic --admin-enabled true -o none
fi

# Built locally and pushed, NOT with `az acr build`. ACR Tasks (the cloud
# builder) is not permitted on this subscription — it fails with
# TasksOperationsNotAllowed and needs an Azure support request to enable. A
# local buildx push needs no such permission.
#
# --platform linux/amd64 is REQUIRED when building from an Apple Silicon Mac:
# Container Apps runs amd64, and an arm64 image fails at start with an
# exec-format error that looks nothing like an architecture problem.
log "building image locally and pushing to $ACR (ACR Tasks is not available here)"
ACR_SERVER="$(az acr show -n "$ACR" --query loginServer -o tsv)"
az acr login -n "$ACR" >/dev/null
docker buildx build --platform linux/amd64 \
  -t "${ACR_SERVER}/trusted-router:${IMAGE_TAG}" --push .

# --------------------------------------------------- container apps env
if exists az containerapp env show -g "$RG" -n "$APP_ENV"; then
  log "container app env exists"
else
  log "creating container app environment"
  az containerapp env create -g "$RG" -n "$APP_ENV" -l "$APP_LOCATION" -o none
fi

log "done. Deploy the app with azure_canary_app.sh (kept separate so the app can"
log "be redeployed without re-provisioning the database)."
log "  PG_HOST=$PG_HOST"
