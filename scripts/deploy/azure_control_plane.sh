#!/usr/bin/env bash
# Deploy the AZURE control plane: Container Apps (uaenorth) on Flexible Server.
#
# This is the Azure sibling of aws_eu_control_plane.sh, and it exists for the
# same reason that one does: a cloud is not independent until it runs its OWN
# control plane. The gateway can be perfect — attested, serving real TLS,
# verified against SEV-SNP hardware — and the cloud still has nothing to say
# about itself, because the synthetic monitor, the status page, and the
# model/provider leaderboard all live in the control plane. Azure's enclave was
# in exactly that state: healthy and invisible.
#
# THIS FILE IS THE SOURCE OF TRUTH FOR THE SERVICE ENV. The same rule the AWS
# script learned the hard way (a runtime variable set out-of-band pointed the
# monitor at the wrong host and painted a healthy cloud 50% degraded). Change
# it here or not at all.
#
# WHAT DIFFERS FROM AWS, and why:
#
#   * TR_SYNTHETIC_CANONICAL_ATTESTED=false. The AWS enclave mints a
#     self-signed cert inside the TEE, so its probes must skip CA validation
#     and verify the attestation binding instead. The Azure enclave completes
#     ACME inside the TEE and serves a publicly-trusted Let's Encrypt cert, so
#     ordinary CA validation is not only possible but stronger — it proves the
#     public chain AND the name.
#
#   * No PCR0. PCR0 is a Nitro EIF measurement. Azure's equivalent is the CCE
#     policy hash carried in SEV-SNP HOST_DATA and attested through MAA, which
#     needs its own probe shape (issuer pin + expected hostdata). Until that
#     probe exists, this deployment publishes only the components it can
#     actually measure — see components.py; a component whose probe cannot run
#     must be ABSENT, never green.
#
#   * DATABASE. Flexible Server, not Citus. docs/storage-portability/
#     multi-cloud-separation.md recommends Cosmos DB for PostgreSQL for a
#     production Azure region because Citus distributes by workspace_id, and
#     that recommendation stands for scale. This plane starts on plain
#     Flexible Server because PostgresStore passes conformance against stock
#     Postgres and the sharding behaviour is not what gates independence.
#     Re-validate on Citus with the conformance suite before trusting it with
#     production write volume.
#
# Prerequisite: scripts/deploy/azure_canary.sh has provisioned the resource
# group, Flexible Server, ACR and Container Apps environment (CANARY=tr-azure
# LOCATION=uaenorth APP_LOCATION=uaenorth).
set -euo pipefail

STACK="${STACK:-tr-azure}"
RG="${RG:-$STACK}"
LOCATION="${LOCATION:-uaenorth}"
APP="${APP:-$STACK}"
APP_ENV="${APP_ENV:-$STACK-env}"
PG_NAME="${PG_NAME:-$STACK-pg}"
PG_ADMIN="${PG_ADMIN:-tradmin}"
PG_DB="${PG_DB:-trustedrouter}"
ACR="${ACR:-$(echo "${STACK}${LOCATION}acr" | tr -cd "[:alnum:]")}"
IMAGE_TAG="${IMAGE_TAG:-azure}"
STATE_DIR="${STATE_DIR:-$HOME/.config/$STACK}"
PW_FILE="${PW_FILE:-$STATE_DIR/pgpw}"

# The attested Azure gateway this control plane fronts, and the health host the
# status page is published under.
API_BASE_URL="${API_BASE_URL:-https://api-azure.trustedrouter.com/v1}"
STATUS_HOST="${STATUS_HOST:-https://azure.trustedrouter.com}"
# Per-enclave probe target. Same reasoning as the AWS script's raw NLB names:
# connect to the group directly while SNI/Host stay api-azure.trustedrouter.com,
# so one dead region cannot hide behind a shared name. The NAME is what binds
# this endpoint to its public status component in synthetic/components.py —
# renaming one without the other silently unpublishes it.
# Both Azure enclave regions. A region missing here is not probed at all, and
# its component silently reports nothing rather than reporting down.
# southeastasia carries an explicit @public_host. The two Azure regions serve
# DIFFERENT public names, because the shared ACME cache is disabled on Azure
# (no "tr-cross-cloud-sa-key" in the bundle), so each region completes ACME for
# its own hostname only. Probing southeastasia with the canonical SNI
# api-azure.trustedrouter.com asks it for a certificate it does not hold: the
# handshake fails and a healthy region publishes as DOWN.
#
# uaenorth needs no override — it IS the canonical name. When the shared cache
# lands and both regions serve one name, drop the @suffix and this returns to
# the plain shared-name form that GCP and AWS use.
GATEWAY_REGION_TARGETS="${GATEWAY_REGION_TARGETS:-uaenorth=quill-enclave-uaenorth.uaenorth.azurecontainer.io,southeastasia=quill-enclave-southeastasia.southeastasia.azurecontainer.io@api-azure-sea.trustedrouter.com}"

# Secrets resolved from the operator's own files, exactly like every other
# cloud (quill-cloud-proxy tools/quill_secret_sources.py). No cloud reads
# another cloud's secret store — that would make one cloud a hub the others
# need in order to be PROVISIONED.
KEYS_FILE="${KEYS_FILE:-$HOME/.quill_cloud_keys.private}"
SECRETS_DIR="${SECRETS_DIR:-$HOME/.quill-secrets}"

# Federation + deferred settlement. Identity federates from the GCP home plane
# (a peer token grants directory reads only); credits do not. Deferred
# settlement stays OFF until its token is present AND explicitly enabled.
# 120s, not 300s: the status page calls the monitor stale at 300s, so an
# interval equal to the threshold flaps in and out of a stale banner.
SYNTHETIC_INTERVAL_SECONDS="${SYNTHETIC_INTERVAL_SECONDS:-120}"
SYNTHETIC_ROTATION_COUNT="${SYNTHETIC_ROTATION_COUNT:-8}"
# Azure does not yet have an approved external scheduled worker. Both loops
# therefore remain in this process, and the observer service MUST remain a
# singleton: every additional replica would repeat paid inference and
# remediation. These are source-of-truth literals rather than operator knobs;
# changing ownership is a separately reviewed resource/cost migration.
OBSERVER_REMEDIATOR_IN_PROCESS_ENABLED="true"
OBSERVER_REMEDIATOR_MODE="observe"
OBSERVER_MAX_REPLICAS_EFFECTIVE=1

log() { printf '\n=== %s\n' "$*" >&2; }
exists() { "$@" >/dev/null 2>&1; }
die() { echo "[FAIL] $*" >&2; exit 1; }

case "$SYNTHETIC_INTERVAL_SECONDS" in
  ''|*[!0-9]*) die "SYNTHETIC_INTERVAL_SECONDS must be a positive integer" ;;
esac
[ "$SYNTHETIC_INTERVAL_SECONDS" -gt 0 ] \
  || die "Azure has no external synthetic owner; the in-process scheduler cannot be disabled"
[ "$OBSERVER_REMEDIATOR_IN_PROCESS_ENABLED" = "true" ] \
  || die "Azure has no external remediation owner; the in-process remediator cannot be disabled"
[ "$OBSERVER_REMEDIATOR_MODE" != "off" ] \
  || die "Azure has no external remediation owner; remediator mode cannot be off"

read_secret() {
  # A file in the secrets dir wins; otherwise the env file's own name.
  local logical="$1" env_name="$2"
  if [ -f "$SECRETS_DIR/$logical" ]; then
    tr -d '\n' < "$SECRETS_DIR/$logical"
    return 0
  fi
  python3 - "$KEYS_FILE" "$env_name" <<'PY'
import re, sys
from pathlib import Path
path, name = Path(sys.argv[1]), sys.argv[2]
if path.exists():
    for line in path.read_text().splitlines():
        m = re.match(rf"\s*(?:export\s+)?{re.escape(name)}\s*=\s*(.*)$", line)
        if m:
            raw = m.group(1).strip()
            if raw[:1] in ("'", '"') and raw[-1:] == raw[:1]:
                raw = raw[1:-1]
            sys.stdout.write(raw)
            break
PY
}

exists az containerapp env show -g "$RG" -n "$APP_ENV" \
  || die "no Container Apps environment '$APP_ENV' in '$RG' — run: CANARY=$STACK LOCATION=$LOCATION APP_LOCATION=$LOCATION bash scripts/deploy/azure_canary.sh"

PG_HOST="$(az postgres flexible-server show -g "$RG" -n "$PG_NAME" --query fullyQualifiedDomainName -o tsv)"
ACR_SERVER="$(az acr show -g "$RG" -n "$ACR" --query loginServer -o tsv)"

OBSERVER_TOKEN="$(read_secret trustedrouter-observer-internal-token TR_OBSERVER_INTERNAL_TOKEN)"
MONITOR_KEY="$(read_secret trustedrouter-synthetic-monitor-api-key TR_SYNTHETIC_MONITOR_API_KEY)"
[ -n "$OBSERVER_TOKEN" ] || die "no observer internal token in $SECRETS_DIR or $KEYS_FILE"
[ -n "$MONITOR_KEY" ] || die "no synthetic monitor key: the leaderboard cannot run without it"
LEGACY_GATEWAY_TOKEN="$(read_secret trustedrouter-internal-gateway-token TR_INTERNAL_GATEWAY_TOKEN)"
if [ -n "$LEGACY_GATEWAY_TOKEN" ] && [ "$OBSERVER_TOKEN" = "$LEGACY_GATEWAY_TOKEN" ]; then
  die "observer internal token must differ from the billing gateway token"
fi
unset LEGACY_GATEWAY_TOKEN
# The monitor key is what makes the leaderboard green: rotation calls the
# gateway as a CUSTOMER of itself. Without it there is a status page with no
# model rows, which reads as "no data" rather than "not measured".

if exists az containerapp show -g "$RG" -n "$APP"; then
  PG_PASSWORD="$(az containerapp secret show -g "$RG" -n "$APP" --secret-name pg-password --query value -o tsv)"
else
  [ -f "$PW_FILE" ] || die "no database password at $PW_FILE — azure_canary.sh writes it"
  PG_PASSWORD="$(cat "$PW_FILE")"
fi
DSN="postgresql://${PG_ADMIN}:${PG_PASSWORD}@${PG_HOST}:5432/${PG_DB}?sslmode=require"

log "building linux/amd64 image in ACR ${ACR}"
az acr build --registry "$ACR" --platform linux/amd64 \
  --image "trusted-router:${IMAGE_TAG}" . >/dev/null

# Deploy by DIGEST, not tag. Container Apps keys "did the source change?" off
# the image reference string; with a mutable tag the string is constant, so a
# revision can come up RUNNING with new env and OLD CODE. The AWS script
# carries the same rule for the same reason — it cost a full verification
# cycle chasing a "deployed" fix that was never running.
IMAGE_DIGEST="$(az acr repository show-manifests --name "$ACR" --repository trusted-router \
  --query "[?tags[?@=='${IMAGE_TAG}']].digest | [0]" -o tsv 2>/dev/null || true)"
[ -n "$IMAGE_DIGEST" ] && [ "$IMAGE_DIGEST" != "None" ] \
  || die "could not resolve trusted-router:${IMAGE_TAG} to a digest"
IMAGE_REF="${ACR_SERVER}/trusted-router@${IMAGE_DIGEST}"
log "deploying by digest: ${IMAGE_DIGEST}"

ENV_VARS=(
  "TR_ENVIRONMENT=canary"
  "TR_SERVICE_SURFACE=observer"
  "TR_NEW_SIGNUPS_ENABLED=false"
  # Container Apps cannot safely overwrite the trusted client-IP header while
  # this service is directly exposed. Ignore any caller-supplied value and use
  # the conservative aggregate application bucket; the platform scale cap is
  # the cost boundary until the documented Front Door/Private Link migration.
  "TR_RATE_LIMIT_CLIENT_IP_MODE=untrusted"
  "TR_MAX_REQUEST_BODY_BYTES=4194304"
  "TR_MAX_IN_FLIGHT_REQUEST_BODY_BYTES=8388608"
  "TR_MAX_CONCURRENT_REQUEST_BODIES=2"
  "TR_REQUEST_BODY_READ_TIMEOUT_SECONDS=10"
  "TR_RELEASE=${IMAGE_TAG}"
  "TR_STORAGE_BACKEND=postgres"
  "TR_POSTGRES_DSN=secretref:pg-dsn"
  "TR_ENABLE_LIVE_PROVIDERS=false"
  "TR_TRUSTED_DOMAIN=trustedrouter.com"

  "TR_API_BASE_URL=${API_BASE_URL}"
  "TR_PRIMARY_REGION=${LOCATION}"
  "TR_REGIONS=${LOCATION}"

  "TR_SYNTHETIC_MONITOR_REGION=${LOCATION}"
  # FALSE, unlike AWS: this enclave completes ACME inside the TEE and serves a
  # publicly-trusted cert, so ordinary CA validation applies.
  "TR_SYNTHETIC_CANONICAL_ATTESTED=false"
  "TR_SYNTHETIC_REGIONAL_PROBES_ENABLED=false"
  "TR_SYNTHETIC_GATEWAY_REGION_TARGETS=${GATEWAY_REGION_TARGETS}"
  "TR_SYNTHETIC_IMAGE_PROBE_ENABLED=false"
  "TR_SYNTHETIC_CONTROL_PLANE_HEALTH_URL=${STATUS_HOST}"
  # The authorize/settle dependency this gateway ACTUALLY has today is the GCP
  # plane (QUILL_TR_CONTROL_PLANE_BASE_URL in qcp tools/deploy-azure-aci.sh).
  # The billing probe must measure the real dependency, not the intended one.
  # Flip this and the enclave's own env together, never separately.
  "TR_SYNTHETIC_CONTROL_PLANE_BASE_URL=https://trustedrouter.com"


  # The monitor runs IN THIS PROCESS. Azure has no Cloud Scheduler and no
  # EventBridge, and Container Apps Jobs cannot carry a `python -c` argv
  # through the CLI's argument handling. More importantly, a per-cloud
  # scheduler is one more thing that stops silently: AWS once had an
  # EventBridge connection go DEAUTHORIZED on a stale token and the status
  # page simply went quiet while the app stayed healthy. In-process means the
  # monitor arrives with the deployment.
  #
  # rotation_count=8 is REAL INFERENCE and costs real money. It is also the
  # only thing that puts model/provider rows on the leaderboard: a model with
  # no sample shows no verdict at all - not green, not red, absent.
  "TR_SYNTHETIC_SCHEDULER_INTERVAL_SECONDS=${SYNTHETIC_INTERVAL_SECONDS}"
  "TR_SYNTHETIC_SCHEDULER_ROTATION_COUNT=${SYNTHETIC_ROTATION_COUNT}"
  "TR_REMEDIATOR_IN_PROCESS_ENABLED=${OBSERVER_REMEDIATOR_IN_PROCESS_ENABLED}"
  "TR_REMEDIATOR_MODE=${OBSERVER_REMEDIATOR_MODE}"

  "TR_OBSERVER_INTERNAL_TOKEN=secretref:observer-token"
  "TR_SYNTHETIC_MONITOR_API_KEY=secretref:monitor-key"
)
SECRET_ARGS=(
  "pg-password=${PG_PASSWORD}"
  "pg-dsn=${DSN}"
  "observer-token=${OBSERVER_TOKEN}"
  "monitor-key=${MONITOR_KEY}"
)
RETIRED_OBSERVER_ENV_VARS=(
  TR_INTERNAL_GATEWAY_TOKEN
  TR_FEDERATION_HOME_TOKEN
  TR_FEDERATION_SETTLEMENT_HOME_TOKEN
  TR_FEDERATION_DEFERRED_SETTLEMENT_ENABLED
  TR_FEDERATION_HOME_BASE_URL
)

if exists az containerapp show -g "$RG" -n "$APP"; then
  log "updating $APP"
  az containerapp secret set -g "$RG" -n "$APP" --secrets "${SECRET_ARGS[@]}" -o none
  az containerapp update -g "$RG" -n "$APP" \
    --image "$IMAGE_REF" --set-env-vars "${ENV_VARS[@]}" \
    --remove-env-vars "${RETIRED_OBSERVER_ENV_VARS[@]}" \
    --min-replicas 1 --max-replicas "$OBSERVER_MAX_REPLICAS_EFFECTIVE" \
    --scale-rule-name observer-http \
    --scale-rule-type http \
    --scale-rule-http-concurrency "${OBSERVER_HTTP_CONCURRENCY:-10}" -o none
else
  log "creating $APP"
  ACR_USER="$(az acr credential show -n "$ACR" --query username -o tsv)"
  ACR_PASS="$(az acr credential show -n "$ACR" --query 'passwords[0].value' -o tsv)"
  az containerapp create -g "$RG" -n "$APP" \
    --environment "$APP_ENV" \
    --image "$IMAGE_REF" \
    --registry-server "$ACR_SERVER" \
    --registry-username "$ACR_USER" \
    --registry-password "$ACR_PASS" \
    --secrets "${SECRET_ARGS[@]}" \
    --env-vars "${ENV_VARS[@]}" \
    --target-port 8080 --ingress external \
    --min-replicas 1 --max-replicas "$OBSERVER_MAX_REPLICAS_EFFECTIVE" \
    --scale-rule-name observer-http \
    --scale-rule-type http \
    --scale-rule-http-concurrency "${OBSERVER_HTTP_CONCURRENCY:-10}" \
    --cpu 1.0 --memory 2.0Gi -o none
fi

configured_env_names="$(az containerapp show -g "$RG" -n "$APP" \
  --query 'properties.template.containers[0].env[].name' -o tsv)"
configured_env_names="${configured_env_names//$'\t'/$'\n'}"
while IFS= read -r configured_env_name; do
  for retired_env_name in "${RETIRED_OBSERVER_ENV_VARS[@]}"; do
    if [ "$configured_env_name" = "$retired_env_name" ]; then
      die "observer retains forbidden legacy env ${retired_env_name}"
    fi
  done
done <<<"$configured_env_names"

# Scale limits are revision-scoped. Only switch to single mode after the
# bounded replacement revision has deployed successfully, so an old staged
# revision cannot keep serving outside this cap and a pre-existing staged
# revision cannot be promoted before the known image is ready.
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
    || [ "$configured_max_replicas" != "$OBSERVER_MAX_REPLICAS_EFFECTIVE" ] \
    || [ "$configured_http_concurrency" != "${OBSERVER_HTTP_CONCURRENCY:-10}" ]; then
  die "observer scale verification failed: mode=${active_revision_mode:-unset} max=${configured_max_replicas:-unset} concurrency=${configured_http_concurrency:-unset}"
fi

FQDN="$(az containerapp show -g "$RG" -n "$APP" --query properties.configuration.ingress.fqdn -o tsv)"
log "app URL: https://${FQDN}"

# Schema, and then PROOF that it landed.
#
# The previous version ran `az containerapp exec` and, if that failed, logged
# a NOTE and carried on. It failed — exec needs an interactive TTY, which a
# deploy pipeline does not have — and the deploy reported success against an
# EMPTY DATABASE. The app then served HTTP 200 on every page while every
# write failed as `rate_limit.store_error`, and the status page published
# five permanently-"unknown" components. That is the "reports success without
# measuring" failure this codebase keeps re-finding, so the fix is not a
# better exec: it is a CHECK that fails the deploy.
log "applying the schema (idempotent; every statement is IF NOT EXISTS)"
SCHEMA_SQL="${SCHEMA_SQL:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/src/trusted_router/storage_postgres_schema.sql}"
[ -f "$SCHEMA_SQL" ] || die "schema file not found at $SCHEMA_SQL"

if command -v psql >/dev/null 2>&1; then
  # A temporary firewall rule for THIS host only, removed on every exit path.
  MYIP="$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || true)"
  if [ -n "$MYIP" ]; then
    az postgres flexible-server firewall-rule create -g "$RG" -s "$PG_NAME" \
      --name "tmp-schema-$$" --start-ip-address "$MYIP" --end-ip-address "$MYIP" \
      -o none 2>/dev/null || true
    # shellcheck disable=SC2064
    trap "az postgres flexible-server firewall-rule delete -g '$RG' -s '$PG_NAME' --name 'tmp-schema-$$' --yes -o none 2>/dev/null || true" EXIT
    sleep 5
  fi
  PGPASSWORD="$PG_PASSWORD" psql \
    "host=${PG_HOST} port=5432 user=${PG_ADMIN} dbname=${PG_DB} sslmode=require" \
    -v ON_ERROR_STOP=1 -q -f "$SCHEMA_SQL" >/dev/null \
    || die "schema application FAILED — the app would serve 200s over an empty database"
  APPLIED=$(PGPASSWORD="$PG_PASSWORD" psql \
    "host=${PG_HOST} port=5432 user=${PG_ADMIN} dbname=${PG_DB} sslmode=require" \
    -tAc "select count(*) from information_schema.tables where table_schema='public' and table_name like 'tr\\_%'" 2>/dev/null || echo 0)
  # Assert the COUNT, not the exit code: a psql that connects and applies
  # nothing still exits 0.
  [ "${APPLIED:-0}" -ge 6 ] || die "schema check FAILED: only ${APPLIED:-0} tr_* tables present (expected >= 6)"
  log "schema verified: ${APPLIED} tr_* tables present"
else
  die "psql not found — required to apply and VERIFY the schema. Install it, or
       apply ${SCHEMA_SQL} against ${PG_HOST}/${PG_DB} yourself and re-run."
fi

log "DNS: point azure.trustedrouter.com at this app"
if command -v gcloud >/dev/null 2>&1; then
  CURRENT="$(gcloud dns record-sets describe azure.trustedrouter.com. \
    --project "${DNS_PROJECT:-quill-cloud-proxy}" --zone "${DNS_ZONE:-trustedrouter-com}" \
    --type CNAME --format 'value(rrdatas[0])' 2>/dev/null || true)"
  if [ "$CURRENT" = "${FQDN}." ]; then
    log "dns: azure.trustedrouter.com already -> ${FQDN}"
  elif [ -n "$CURRENT" ]; then
    gcloud dns record-sets update azure.trustedrouter.com. \
      --project "${DNS_PROJECT:-quill-cloud-proxy}" --zone "${DNS_ZONE:-trustedrouter-com}" \
      --type CNAME --ttl 300 --rrdatas "${FQDN}." >/dev/null
    log "dns: reconciled azure.trustedrouter.com ${CURRENT} -> ${FQDN}."
  else
    gcloud dns record-sets create azure.trustedrouter.com. \
      --project "${DNS_PROJECT:-quill-cloud-proxy}" --zone "${DNS_ZONE:-trustedrouter-com}" \
      --type CNAME --ttl 300 --rrdatas "${FQDN}." >/dev/null
    log "dns: created azure.trustedrouter.com -> ${FQDN}."
  fi
else
  log "gcloud absent: point azure.trustedrouter.com CNAME at ${FQDN} yourself"
fi

cat >&2 <<NOTE

=== next
  status page   https://azure.trustedrouter.com/status.json
  leaderboard   the first rotation pass populates model/provider rows; the
                monitor calls api-azure.trustedrouter.com as a customer of
                itself, so a red row means the ENCLAVE could not serve that
                model — which is the whole point of measuring it here.
  verify        bash scripts/deploy/verify_deployment.sh (cloud-agnostic)
NOTE

# ---------------------------------------------------------------------------
# ...and then the part that is NOT a note.
#
# Everything above provisions a control plane that serves, measures itself, and
# publishes a status page. None of it gives this cloud an operational-analytics
# pipeline: the ENV_VARS block sets no TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED,
# so settle enqueues nothing, there is no outbox to drain, and no drain. On AWS
# that same gap ran for fifteen days behind an entirely green status page
# because the only alarm is emitted by the missing process.
#
# So this deploy now ends by asking whether the CLOUD works rather than whether
# the script finished, and today, on Azure, it says no. That is the correct
# answer, and no variable this script inherits changes it: the verifier's bound
# is a constant in src/ and its URL comes from the fleet registry, and the two
# variables it reads at all -- TR_MAX_DRAIN_LAG_SECONDS and TR_STATUS_URL -- it
# reads only in order to print that they are being IGNORED. (An earlier version
# of this comment said the verifier "reads no environment variable at all",
# which was a tidier sentence and not true.) It takes no flags either.
#
# The only way to make this exit 0 is to build the pipeline. There is no
# exemption, no waiver and no registry field that excuses a stage: a cloud that
# cannot be checked is NOT VERIFIED and this script exits non-zero.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/cloud_complete_gate.sh
. "${SCRIPT_DIR}/cloud_complete_gate.sh"

require_cloud_complete azure "$(cat <<'NEXT'
The Azure app is deployed and serving. The Azure CLOUD is not complete: it has
no operational-analytics pipeline at all. To finish it:

  1. give it somewhere to drain TO (a ClickHouse this cloud owns, mirroring
     scripts/deploy/aws_eu_clickhouse.sh) — this is a COST decision, so it is
     not made by a deploy script;
  2. add to the ENV_VARS block in this file:
       TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED=true
       TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL=...  (+ user/database/password)
  3. install a drain against it, mirroring
     scripts/deploy/aws_eu_clickhouse_drain_install.sh;
  4. bash scripts/deploy/verify_cloud_complete.sh azure
NEXT
)"
