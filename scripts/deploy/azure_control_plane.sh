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
FEDERATION_HOME_BASE_URL="${FEDERATION_HOME_BASE_URL:-https://trustedrouter.com}"
DEFERRED_SETTLEMENT_ENABLED="${DEFERRED_SETTLEMENT_ENABLED:-false}"
# 120s, not 300s: the status page calls the monitor stale at 300s, so an
# interval equal to the threshold flaps in and out of a stale banner.
SYNTHETIC_INTERVAL_SECONDS="${SYNTHETIC_INTERVAL_SECONDS:-120}"
SYNTHETIC_ROTATION_COUNT="${SYNTHETIC_ROTATION_COUNT:-8}"

log() { printf '\n=== %s\n' "$*" >&2; }
exists() { "$@" >/dev/null 2>&1; }
die() { echo "[FAIL] $*" >&2; exit 1; }

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

INTERNAL_TOKEN="$(read_secret trustedrouter-internal-gateway-token TR_INTERNAL_GATEWAY_TOKEN)"
MONITOR_KEY="$(read_secret trustedrouter-synthetic-monitor-api-key TR_SYNTHETIC_MONITOR_API_KEY)"
FEDERATION_TOKEN="$(read_secret trustedrouter-federation-peer-token TR_FEDERATION_PEER_TOKEN)"
SETTLEMENT_TOKEN="$(read_secret trustedrouter-federation-settlement-token-azure-uae UNSET_ON_PURPOSE)"
[ -n "$INTERNAL_TOKEN" ] || die "no internal gateway token in $SECRETS_DIR or $KEYS_FILE"
[ -n "$MONITOR_KEY" ] || die "no synthetic monitor key: the leaderboard cannot run without it"
# The monitor key is what makes the leaderboard green: rotation calls the
# gateway as a CUSTOMER of itself. Without it there is a status page with no
# model rows, which reads as "no data" rather than "not measured".

if [ -z "$SETTLEMENT_TOKEN" ] || [ "$DEFERRED_SETTLEMENT_ENABLED" != "true" ]; then
  log "deferred settlement OFF (token present: $([ -n "$SETTLEMENT_TOKEN" ] && echo yes || echo no))"
  DEFERRED_SETTLEMENT_ENABLED="false"
fi

if exists az containerapp show -g "$RG" -n "$APP"; then
  PG_PASSWORD="$(az containerapp secret show -g "$RG" -n "$APP" --secret-name pg-password --query value -o tsv)"
else
  [ -f "$PW_FILE" ] || die "no database password at $PW_FILE — azure_canary.sh writes it"
  PG_PASSWORD="$(cat "$PW_FILE")"
fi
DSN="postgresql://${PG_ADMIN}:${PG_PASSWORD}@${PG_HOST}:5432/${PG_DB}?sslmode=require"

# BYOK envelope format ordering. FIRST, before the build, because everything
# after this line costs money or mutates a service.
#
# This control plane writes BYOK envelopes into the Flexible Server database
# that belongs to THIS cloud, and the SEV-SNP enclaves in front of it are the
# only things that ever read them back. If this build writes a format they
# cannot read, every BYOK key in this cloud stops working at the next inference
# request.
#
# docs/design/byok-aad-v2-migration.md §4.0 is where the rule lives, and it
# names its own weak point: the write-side change reaches this cloud "as an
# ordinary version bump rather than as a deliberate migration step. Nobody has
# to decide to run it." This script IS the path where nobody decided, and the
# gate in .github/workflows/deploy.yml cannot cover it — that workflow deploys
# Cloud Run and never touches Azure.
#
# Regions are enumerated from the published record rather than from
# GATEWAY_REGION_TARGETS above. UAE North and Southeast Asia run their own CCE
# policies, so they attest different hostdata and could in principle be running
# different builds; a check that read one region would be the same shape of
# mistake as a probe that measures one region — see the comment on
# GATEWAY_REGION_TARGETS.
#
# Be precise about what that buys TODAY: the checker reads the control plane's
# mirror at trustedrouter.com/trust/azure-release.json, and until this change
# that mirror dropped the upstream `regions` array entirely, so only the
# canonical endpoint was discoverable and only it got checked. The mirror fix
# ships in this same change (src/trusted_router/services/trust_release.py), but
# it takes effect for this script only once a control plane carrying it is
# serving. Until then this line checks one of two Azure regions.
#
# REPORT-ONLY TODAY, and say so plainly: check_format_ordering.py ships with
# DEFAULT_MODE = "report-only", so the command below prints its table and a
# loud "WOULD BLOCK" verdict and exits 0. THIS SCRIPT WILL CONTINUE TO DEPLOY
# EVEN WHEN THE ORDERING IS VIOLATED until that default is flipped. Read the
# gate's output before answering any deploy prompt; do not read its exit code
# as a verdict.
#
# "Exits 0" covers every path main() RETURNS from: in report-only it returns 0
# whatever happened, including a run that could not derive what this build
# writes and a run in which the script raised an Exception. Two things are not
# main() returning, and both still reach the branch below: `uv` failing to
# start the gate exits non-zero before main() runs at all, and a BaseException
# out of a probed import (SystemExit, KeyboardInterrupt) propagates through
# main()'s `except Exception`. So this script will also deploy a build whose
# written formats are UNKNOWN, printing that it knows nothing. That is the cost
# of the mode and it is stated here rather than left to be discovered; what
# catches an underivable write side before this point is quill-router's CI,
# which runs the same derivation against the real tree on every push.
#
# The reason is the ordering rule applied to the gate itself: what the gate
# reads is a generated accepted_formats.json at the released enclave commit,
# and Azure has never published a source_commit, let alone one carrying that
# file. Enforcing today would stop every Azure control-plane deploy for a
# reason nobody can clear from this repository.
#
# THE FLIP: when `uv run python -m scripts.check_format_ordering --mode
# enforcing --cloud gcp --cloud aws --cloud azure` exits 0, set
# DEFAULT_MODE = ENFORCING in scripts/check_format_ordering.py. One line, and
# it arms this script, aws_eu_control_plane.sh and deploy.yml together.
#
# There is no skip flag on purpose, and there will not be one once it is
# enforcing. Rolling back a control plane un-ships bad code; it does not
# un-write an envelope. §5: a cloud whose control plane has written even one v2
# envelope needs its enclave to keep v2 read support permanently.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
log "checking BYOK envelope format ordering against both live azure regions"
log "  (report-only until DEFAULT_MODE is flipped: read the verdict, not the exit code)"
# In report-only mode the gate's own main() returns 0 on every path it has --
# every region blocked, a write side it could not derive, an exception of its
# own -- so the ways to reach this branch today are `uv run` failing to start
# the gate at all (no interpreter, an unresolvable lockfile), a BaseException
# raised out of a probed import, and the ENFORCING flip. None of those is "the
# ordering is violated", which is why the first paragraph below asks what got
# printed before assuming it was.
if ! (cd "$REPO_ROOT" && uv run python -m scripts.check_format_ordering --cloud azure); then
  cat >&2 <<'BLOCKED'

FIRST, look at what the gate printed above.

If there is no CLOUD/REGION table, the gate never reached an enclave and every
remedy below is about the wrong thing. Two causes:
  * it could not determine what THIS build writes -- an "algorithm" attribute
    assigned somewhere under src/trusted_router, a probed write entry point
    whose signature changed, a module that will not parse. Its banner names the
    file and the line. Fix it in quill-router; no cloud is involved.
  * uv could not start the gate at all. Nothing was checked, and nothing is
    known to be wrong with the ordering. Fix the toolchain and re-run.
While DEFAULT_MODE is report-only these two are not symmetric. The first exits
0 and CANNOT reach this branch: it prints its banner and the deploy continues.
The second is the cause that does reach it -- uv fails before the gate runs, so
there is no table above because there was no run.

OTHERWISE -- REFUSING TO DEPLOY: this build may write a BYOK envelope format an
Azure enclave cannot read. Nothing has been built or deployed.

If the block is a missing source_commit, the Azure release record cannot be
mapped to the enclave source and the answer is unknowable rather than bad.
In quill-cloud-proxy:
  python3 tools/capture-plane-measurements.py --write --keep-accepted \
      --source-commit <sha that built the RUNNING enclave>
  # then commit trust-page/, which fires publish-trust-azure.yml
--source-commit has no default: HEAD is the released build only by coincidence,
and a real-but-wrong commit makes this gate read a real file for the wrong build.

If the block is a missing accepted_formats.json, the enclave at that commit
predates the generated declaration and what it accepts was never measured. Roll
an enclave built from a commit that carries
enclave-go/internal/byokcache/accepted_formats.json.

If the block names a measurement that was never served, the record accepts a
build no region answered with. Either a region is missing from the record's
regions array or the accepted set still names a retired build.

If the block is a real format mismatch, ship and fully roll the enclave step-1
build to BOTH regions first. See docs/design/byok-aad-v2-migration.md
BLOCKED
  exit 1
fi

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

  "TR_FEDERATION_HOME_BASE_URL=${FEDERATION_HOME_BASE_URL}"
  "TR_FEDERATION_DEFERRED_SETTLEMENT_ENABLED=${DEFERRED_SETTLEMENT_ENABLED}"

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

  "TR_INTERNAL_GATEWAY_TOKEN=secretref:internal-token"
  "TR_SYNTHETIC_MONITOR_API_KEY=secretref:monitor-key"
  "TR_FEDERATION_HOME_TOKEN=secretref:federation-token"
)
SECRET_ARGS=(
  "pg-password=${PG_PASSWORD}"
  "pg-dsn=${DSN}"
  "internal-token=${INTERNAL_TOKEN}"
  "monitor-key=${MONITOR_KEY}"
  "federation-token=${FEDERATION_TOKEN}"
)
if [ "$DEFERRED_SETTLEMENT_ENABLED" = "true" ]; then
  ENV_VARS+=("TR_FEDERATION_SETTLEMENT_HOME_TOKEN=secretref:settlement-token")
  SECRET_ARGS+=("settlement-token=${SETTLEMENT_TOKEN}")
fi

if exists az containerapp show -g "$RG" -n "$APP"; then
  log "updating $APP"
  az containerapp secret set -g "$RG" -n "$APP" --secrets "${SECRET_ARGS[@]}" -o none
  az containerapp update -g "$RG" -n "$APP" \
    --image "$IMAGE_REF" --set-env-vars "${ENV_VARS[@]}" -o none
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
    --min-replicas 1 --max-replicas 3 \
    --cpu 1.0 --memory 2.0Gi -o none
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
