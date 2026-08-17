#!/usr/bin/env bash
# Deploy the AWS-EU control plane: App Runner (Paris) on Aurora DSQL.
#
# Mirrors azure_canary*.sh in shape — build locally, push, deploy, verify with
# the same cloud-agnostic scripts/deploy/verify_deployment.sh.
#
# THIS FILE IS THE SOURCE OF TRUTH FOR THE SERVICE ENV. The live service
# once carried TR_API_BASE_URL out-of-band (unversioned), pointed at its
# own App Runner URL — so the synthetic monitor probed the control plane
# for /attestation (404) and reported the EU cloud 50% trust_degraded
# while the actual enclave was healthy. Every runtime variable now lives
# here; change it here or not at all.
#
# Auth to the database is IAM: the instance role tr-eu-app holds
# dsql:DbConnectAdmin on the Paris cluster, and TR_POSTGRES_IAM_AUTH=aws-dsql
# makes PostgresStore mint a fresh token per physical connection (DSQL tokens
# expire in minutes — a static password dies within the hour). The DSN
# deliberately carries NO password.
#
# Secrets (internal gateway token, synthetic monitor API key) are wired via
# RuntimeEnvironmentSecrets from eu-west-3 Secrets Manager — NOT plaintext
# env vars. describe-service returns RuntimeEnvironmentVariables in
# cleartext to any caller with apprunner:DescribeService; it masks
# RuntimeEnvironmentSecrets.
set -euo pipefail

REGION="${REGION:-eu-west-3}"                       # Paris. Dublin is GONE.
ACCOUNT="${ACCOUNT:-330422590279}"
CLUSTER_ID="${CLUSTER_ID:-tnt642i3ofzpn5z62msacutpuu}"
ECR="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/trusted-router"
TAG="${TAG:-eu}"
SVC="${SVC:-tr-eu}"
# DSQL endpoint region is INDEPENDENT of the App Runner region.
#
# App Runner does not exist in eu-north-1, and DSQL has no endpoint in
# eu-west-1, so the second control plane runs Ireland compute against the
# STOCKHOLM endpoint of the same multi-region cluster (both peers ACTIVE,
# verified). That pairing is deliberate: it shares no region with the Paris
# service, so losing eu-west-3 entirely takes out neither the compute nor the
# database of the standby. Deriving the host from REGION would silently point
# the standby back at the region it exists to survive.
#
# The IAM token region is parsed from this hostname (see
# aws_dsql_connection_details), so naming the Stockholm endpoint is sufficient -
# nothing else needs to know.
DSQL_REGION="${DSQL_REGION:-$REGION}"
DSQL_HOST="${DSQL_HOST:-${CLUSTER_ID}.dsql.${DSQL_REGION}.on.aws}"

# The attested Nitro gateway this control plane fronts. The PCR0 pin must
# match the EIF measurement the enclave deploy published — pass it in from
# the same value quill-cloud-proxy's deploy-aws-nitro.sh pinned:
#   ATTESTATION_PCR0=<hex> bash scripts/deploy/aws_eu_control_plane.sh
API_BASE_URL="${API_BASE_URL:-https://api-aws.trustedrouter.com/v1}"
ATTESTATION_PCR0="${ATTESTATION_PCR0:?set ATTESTATION_PCR0 to the published enclave PCR0 - measurement pinning is the point of the probe}"

# PER-ENCLAVE health checks. api-aws.trustedrouter.com is an AWS Global
# Accelerator anycast record, so every unpinned probe lands on WHICHEVER
# region the accelerator prefers: with two regions behind it, one dead region
# is invisible on the status page. Each entry below makes the synthetic
# monitor connect to that region's NLB directly, producing its own status
# component ("EU West 1 Enclave (Ireland)" / "EU West 3 Enclave (Paris)").
#
# The value is name=CONNECT_HOST, and the connect host is deliberately a raw
# load-balancer hostname rather than a friendly DNS name. DO NOT "simplify"
# this into api-eu-west-1.trustedrouter.com or similar: the enclave mints its
# TLS cert INSIDE the TEE with exactly one SAN, DNS:api-aws.trustedrouter.com.
# Any other name fails hostname validation, and giving the cert more SANs
# would change the enclave image and therefore its PCR0 measurement — a far
# bigger blast radius than an observability change deserves. The probes
# instead connect to this host while SNI and the Host header stay
# api-aws.trustedrouter.com, exactly like
#   tools/verify-attestation.py --api-host X --connect-ip Y.
#
# The names (eu-west-1 / eu-west-3) are what bind each target to its public
# component in src/trusted_router/synthetic/components.py — renaming one here
# silently unpublishes its component, so change both together.
GATEWAY_REGION_TARGETS="${GATEWAY_REGION_TARGETS:-eu-west-1=quill-enclave-nlb-6ed55aa238055cfc.elb.eu-west-1.amazonaws.com,eu-west-3=quill-enclave-nlb-aa2d3be423fa9027.elb.eu-west-3.amazonaws.com}"

# Secrets Manager ARNs (eu-west-3 — App Runner requires same-region secrets).
INTERNAL_TOKEN_SECRET_ARN="${INTERNAL_TOKEN_SECRET_ARN:-$(aws secretsmanager describe-secret --region "$REGION" --secret-id quill/trustedrouter-internal-gateway-token --query ARN --output text)}"
MONITOR_KEY_SECRET_ARN="${MONITOR_KEY_SECRET_ARN:-$(aws secretsmanager describe-secret --region "$REGION" --secret-id quill/trustedrouter-synthetic-monitor-api-key --query ARN --output text)}"
# PEER side of lazy key federation: the token this plane presents to the home
# plane's resolve-key endpoint. Identity only — a GCP-issued key becomes
# known here on first use and keeps serving from cache for up to 24h of home
# outage. Credits deliberately do NOT federate; the credit-transfer tokens
# are separate secrets and remain unset.
FEDERATION_TOKEN_SECRET_ARN="${FEDERATION_TOKEN_SECRET_ARN:-$(aws secretsmanager describe-secret --region "$REGION" --secret-id quill/trustedrouter-federation-peer-token --query ARN --output text)}"
FEDERATION_HOME_BASE_URL="${FEDERATION_HOME_BASE_URL:-https://trustedrouter.com}"
# Deferred settlement, this plane's half: the token presented to home's
# apply-usage endpoint (identifies this plane as aws-eu there), and the
# master enable. OPTIONAL like the ClickHouse secret — the standby region
# must not be blocked by a secret that has not been replicated yet, and
# a missing secret simply leaves deferred settlement OFF on that service.
SETTLEMENT_TOKEN_SECRET_ARN="${SETTLEMENT_TOKEN_SECRET_ARN:-$(aws secretsmanager describe-secret --region "$REGION" --secret-id quill/trustedrouter-federation-settlement-token-aws-eu --query ARN --output text 2>/dev/null || true)}"
DEFERRED_SETTLEMENT_ENABLED="${DEFERRED_SETTLEMENT_ENABLED:-false}"
# Analytics is optional, so its secret must be too. A standby region has no
# ClickHouse node and no replica of this secret; requiring it would block the
# region whose entire job is to survive the loss of the one that has it.
CLICKHOUSE_SECRET_ARN="${CLICKHOUSE_SECRET_ARN:-$(aws secretsmanager describe-secret --region "$REGION" --secret-id quill/tr-eu-clickhouse-password --query ARN --output text 2>/dev/null || true)}"

# This cloud's OWN ClickHouse (tools/aws_eu_clickhouse.sh), private in the
# VPC. Deliberately NOT replicated from the GCP cluster: each cloud owns its
# analytics, which is also what lets the EU-only claim survive an auditor.
#
# Tables are created unqualified, so they live in `default`, not the `tr`
# database the config defaults to. Passing the wrong database yields an
# empty-but-healthy analytics path -- queries succeed against nothing.
CLICKHOUSE_URL="${CLICKHOUSE_URL:-http://172.31.10.143:8123}"
CLICKHOUSE_USER="${CLICKHOUSE_USER:-default}"
CLICKHOUSE_DATABASE="${CLICKHOUSE_DATABASE:-default}"

# App Runner egress is ALL-OR-NOTHING. Switching to VPC routes every
# outbound call through the connector's ENIs, which have no public IP -- so
# an internet-gateway route gives them nothing and the service loses its
# calls to the attested gateway and the home plane. The connector therefore
# sits on PRIVATE subnets behind a per-AZ NAT (tools/aws-private-egress.sh
# in quill-cloud-proxy). One NAT per AZ, because this now carries the whole
# control plane's egress, not just analytics.
VPC_CONNECTOR_ARN="${VPC_CONNECTOR_ARN:-$(aws apprunner list-vpc-connectors --region "$REGION" \
  --query "VpcConnectors[?VpcConnectorName=='tr-eu-vpc-private' && Status=='ACTIVE'].VpcConnectorArn | [0]" \
  --output text)}"
# The analytics egress is optional. ClickHouse is a single private node in
# eu-west-3; a standby control plane in another region cannot reach it and must
# not be blocked from existing because of it. Analytics degrade to absent on the
# standby, which is correct - it is not the serving path.
if [ "${REQUIRE_VPC_EGRESS:-1}" = "1" ]; then
  [ -n "$VPC_CONNECTOR_ARN" ] && [ "$VPC_CONNECTOR_ARN" != "None" ] || {
    echo "no ACTIVE tr-eu-vpc-private connector; run tools/aws-private-egress.sh first" >&2; exit 1; }
fi

log(){ printf '\n=== %s\n' "$*" >&2; }

log "building linux/amd64 image and pushing to ${ECR}:${TAG}"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com" >/dev/null
docker buildx build --platform linux/amd64 -t "${ECR}:${TAG}" --push .

# Resolve the tag to its DIGEST and deploy by digest.
#
# App Runner keys "did the source change?" off the ImageIdentifier STRING.
# With a mutable tag that string is constant, so update-service applies new
# environment variables but does NOT re-pull the image: the service comes
# back RUNNING, the operation reports SUCCEEDED, describe-service shows the
# new env — and the OLD CODE is still serving. That happened on this very
# service and cost a full verification cycle chasing a "deployed" fix that
# was never running. A digest changes whenever the image does, so the
# update is honest by construction.
IMAGE_DIGEST=$(aws ecr describe-images --region "$REGION" --repository-name trusted-router \
  --image-ids "imageTag=${TAG}" --query 'imageDetails[0].imageDigest' --output text)
[ -n "$IMAGE_DIGEST" ] && [ "$IMAGE_DIGEST" != "None" ] || { echo "could not resolve ${TAG} to a digest" >&2; exit 1; }
IMAGE_REF="${ECR}@${IMAGE_DIGEST}"
log "deploying by digest: ${IMAGE_DIGEST}"

# Deferred settlement rides the same optional-secret shape as ClickHouse:
# no per-plane settlement token in this region means the forwarder has
# nothing to present, so the enable flag is forced off rather than shipping
# a plane that admits deferred spend it can never deliver.
if [ -n "$SETTLEMENT_TOKEN_SECRET_ARN" ] && [ "$SETTLEMENT_TOKEN_SECRET_ARN" != "None" ]; then
  SETTLEMENT_SECRET_JSON=",
        \"TR_FEDERATION_SETTLEMENT_HOME_TOKEN\": \"${SETTLEMENT_TOKEN_SECRET_ARN}\""
else
  log "no settlement token in ${REGION}: deferred settlement disabled for this service"
  SETTLEMENT_SECRET_JSON=""
  DEFERRED_SETTLEMENT_ENABLED="false"
fi

# With no ClickHouse secret in this region there is no analytics path, so the
# outbox stays off rather than enqueuing rows nothing will ever drain.
if [ -n "$CLICKHOUSE_SECRET_ARN" ] && [ "$CLICKHOUSE_SECRET_ARN" != "None" ]; then
  OUTBOX_ENABLED="true"
  CLICKHOUSE_URL_EFFECTIVE="$CLICKHOUSE_URL"
  CLICKHOUSE_SECRET_JSON=",
        \"TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD\": \"${CLICKHOUSE_SECRET_ARN}\""
else
  log "no ClickHouse secret in ${REGION}: analytics disabled for this service"
  OUTBOX_ENABLED="false"
  CLICKHOUSE_URL_EFFECTIVE=""
  CLICKHOUSE_SECRET_JSON=""
fi

CONFIG=$(cat <<JSON
{
  "ImageRepository": {
    "ImageIdentifier": "${IMAGE_REF}",
    "ImageRepositoryType": "ECR",
    "ImageConfiguration": {
      "Port": "8080",
      "RuntimeEnvironmentVariables": {
        "TR_ENVIRONMENT": "canary",
        "TR_RELEASE": "${TAG}",
        "TR_STORAGE_BACKEND": "postgres",
        "TR_POSTGRES_DSN": "host=${DSQL_HOST} port=5432 user=admin dbname=postgres sslmode=require",
        "TR_POSTGRES_IAM_AUTH": "aws-dsql",
        "TR_ENABLE_LIVE_PROVIDERS": "false",

        "TR_API_BASE_URL": "${API_BASE_URL}",
        "TR_PRIMARY_REGION": "${REGION}",
        "TR_REGIONS": "${REGION}",

        "TR_SYNTHETIC_MONITOR_REGION": "${REGION}",
        "TR_SYNTHETIC_CANONICAL_ATTESTED": "true",
        "TR_ATTESTATION_EXPECTED_PCR0": "${ATTESTATION_PCR0}",
        "TR_SYNTHETIC_REGIONAL_PROBES_ENABLED": "false",
        "TR_SYNTHETIC_GATEWAY_REGION_TARGETS": "${GATEWAY_REGION_TARGETS}",
        "TR_SYNTHETIC_IMAGE_PROBE_ENABLED": "false",
        "TR_SYNTHETIC_CONTROL_PLANE_HEALTH_URL": "https://aws.trustedrouter.com",
        "TR_SYNTHETIC_CONTROL_PLANE_BASE_URL": "https://trustedrouter.com",

        "TR_FEDERATION_HOME_BASE_URL": "${FEDERATION_HOME_BASE_URL}",
        "TR_FEDERATION_DEFERRED_SETTLEMENT_ENABLED": "${DEFERRED_SETTLEMENT_ENABLED}",

        "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED": "${OUTBOX_ENABLED}",
        "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL": "${CLICKHOUSE_URL_EFFECTIVE}",
        "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER": "${CLICKHOUSE_USER}",
        "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE": "${CLICKHOUSE_DATABASE}"
      },
      "RuntimeEnvironmentSecrets": {
        "TR_INTERNAL_GATEWAY_TOKEN": "${INTERNAL_TOKEN_SECRET_ARN}",
        "TR_SYNTHETIC_MONITOR_API_KEY": "${MONITOR_KEY_SECRET_ARN}",
        "TR_FEDERATION_HOME_TOKEN": "${FEDERATION_TOKEN_SECRET_ARN}"${SETTLEMENT_SECRET_JSON}${CLICKHOUSE_SECRET_JSON}
      }
    }
  },
  "AuthenticationConfiguration": {
    "AccessRoleArn": "arn:aws:iam::${ACCOUNT}:role/tr-eu-ecr-access"
  },
  "AutoDeploymentsEnabled": false
}
JSON
)
# TR_SYNTHETIC_CONTROL_PLANE_BASE_URL is DELIBERATELY the GCP plane for
# now: the enclave's authorize/settle dependency IS https://trustedrouter.com
# today (see quill-cloud-proxy deploy-aws-nitro.sh
# QUILL_TR_CONTROL_PLANE_BASE_URL). The billing probe must measure the
# dependency the gateway actually has, not the one we wish it had. Flip
# BOTH together when key federation makes the EU plane the authorize
# target.

# VPC egress only when there is a connector to use. A standby region has no
# private ClickHouse to reach, and forcing VPC egress there would route its
# entire outbound through a NAT it does not have - i.e. no internet at all.
if [ -n "$VPC_CONNECTOR_ARN" ] && [ "$VPC_CONNECTOR_ARN" != "None" ]; then
  NETWORK_CONFIG=$(cat <<JSON
{
  "EgressConfiguration": {
    "EgressType": "VPC",
    "VpcConnectorArn": "${VPC_CONNECTOR_ARN}"
  }
}
JSON
)
else
  NETWORK_CONFIG='{"EgressConfiguration":{"EgressType":"DEFAULT"}}'
fi

if aws apprunner list-services --region "$REGION" --query "ServiceSummaryList[?ServiceName=='${SVC}'].ServiceArn" --output text | grep -q arn; then
  ARN=$(aws apprunner list-services --region "$REGION" --query "ServiceSummaryList[?ServiceName=='${SVC}'].ServiceArn" --output text)
  log "updating existing service $ARN"
  aws apprunner update-service --region "$REGION" --service-arn "$ARN" \
    --source-configuration "$CONFIG" --network-configuration "$NETWORK_CONFIG" >/dev/null
else
  log "creating service $SVC"
  ARN=$(aws apprunner create-service --region "$REGION" --service-name "$SVC" \
    --source-configuration "$CONFIG" --network-configuration "$NETWORK_CONFIG" \
    --instance-configuration "Cpu=1024,Memory=2048,InstanceRoleArn=arn:aws:iam::${ACCOUNT}:role/tr-eu-app" \
    --health-check-configuration "Protocol=HTTP,Path=/health,Interval=10,Timeout=5,HealthyThreshold=2,UnhealthyThreshold=3" \
    --query 'Service.ServiceArn' --output text)
fi

log "waiting for RUNNING"
for _ in $(seq 1 60); do
  S=$(aws apprunner describe-service --region "$REGION" --service-arn "$ARN" --query 'Service.Status' --output text)
  [ "$S" = "RUNNING" ] && break
  [ "$S" = "CREATE_FAILED" ] || [ "$S" = "UPDATE_FAILED" ] && { echo "FAILED: $S" >&2; exit 1; }
  sleep 20
done
URL=$(aws apprunner describe-service --region "$REGION" --service-arn "$ARN" --query 'Service.ServiceUrl' --output text)

# Assert the service is SERVING the digest we just built. RUNNING +
# operation SUCCEEDED is not that assertion — see the digest note above.
RUNNING_REF=$(aws apprunner describe-service --region "$REGION" --service-arn "$ARN" \
  --query 'Service.SourceConfiguration.ImageRepository.ImageIdentifier' --output text)
if [ "$RUNNING_REF" != "$IMAGE_REF" ]; then
  echo "FAILED: service is serving ${RUNNING_REF}, expected ${IMAGE_REF}" >&2
  exit 1
fi
log "verified serving digest ${IMAGE_DIGEST}"
log "service: https://${URL}"
echo "https://${URL}"

# ---------------------------------------------------------------------------
# EventBridge synthetic cadence — versioned here for the same reason the env
# is: an unversioned rule Input is how a probe silently measures the wrong
# cloud.
#
# rotation_count=8 (the route's hard clamp) with NO rotation_models pin. The
# pin to two DeepSeek ids meant exactly two of the 448 catalogue models ever
# saw a real completion on this plane, so every other model and provider had
# zero samples and could never show a verdict on the AWS leaderboard - not
# green, not red, just absent. Unpinned rotation walks the catalogue.
#
# This IS real inference and it costs real money: 8 completions/min steady
# state, ~11.5k/day, roughly 25 samples per model per day across 448 models.
# That is the price of a leaderboard that reflects what this cloud can
# actually serve rather than what we hope it serves. Lower rotation_count to
# cut spend; the trade is slower coverage, not wrong data.
# ---------------------------------------------------------------------------
if [ "${SKIP_EVENTBRIDGE:-0}" != "1" ]; then
  log "aligning EventBridge rule tr-eu-synthetic-1min"

  # Re-authorize the connection with the CURRENT internal gateway token.
  #
  # The connection stores its own copy of the credential. When the token
  # this service reads changes (e.g. moving it from a plaintext env var
  # to Secrets Manager, as this script did), that stored copy goes stale,
  # every invocation 401s, and EventBridge flips the connection to
  # DEAUTHORIZED and stops trying. Nothing on the service side reports
  # this — the app is healthy, the rule is ENABLED, and the status page
  # simply goes quietly stale. Observed exactly that: 15/15
  # FailedInvocations with the app returning 200 to manual calls.
  #
  # Re-authorizing here binds the credential to the same token the
  # service was just deployed with, so the two cannot drift.
  CONN_TOKEN=$(aws secretsmanager get-secret-value --region "$REGION" \
    --secret-id quill/trustedrouter-internal-gateway-token --query SecretString --output text)
  aws events update-connection --region "$REGION" --name tr-eu-synthetic \
    --authorization-type API_KEY \
    --auth-parameters "ApiKeyAuthParameters={ApiKeyName=Authorization,ApiKeyValue=Bearer ${CONN_TOKEN}}" >/dev/null
  for _ in $(seq 1 15); do
    CONN_STATE=$(aws events describe-connection --region "$REGION" --name tr-eu-synthetic --query ConnectionState --output text)
    [ "$CONN_STATE" = "AUTHORIZED" ] && break
    sleep 10
  done
  [ "$CONN_STATE" = "AUTHORIZED" ] || { echo "FAILED: connection state $CONN_STATE" >&2; exit 1; }
  log "connection AUTHORIZED with current token"

  DEST_ARN=$(aws events list-api-destinations --region "$REGION" --name-prefix tr-eu-synthetic-run --query 'ApiDestinations[0].ApiDestinationArn' --output text)
  ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/tr-eu-eventbridge-invoke-par"
  TARGETS=$(python3 - "$DEST_ARN" "$ROLE_ARN" "$REGION" <<'PY'
import json
import sys

dest_arn, role_arn, region = sys.argv[1:4]
rule_input = {
    "monitor_region": region,
    "rotation_count": 8,
    # REQUIRED here: EventBridge API destinations abandon the request
    # after ~5s and a probe pass takes 10-17s, so without detach every
    # tick is a FailedInvocation (observed 15/15) even though the app
    # completes the run and returns 200. detach acknowledges in
    # milliseconds and probes in the background.
    "detach": True,
}
print(json.dumps([
    {
        "Id": "synthetic",
        "Arn": dest_arn,
        "RoleArn": role_arn,
        "Input": json.dumps(rule_input),
    }
]))
PY
)
  aws events put-rule --region "$REGION" --name tr-eu-synthetic-1min --schedule-expression 'rate(1 minute)' --state ENABLED >/dev/null
  aws events put-targets --region "$REGION" --rule tr-eu-synthetic-1min --targets "$TARGETS" >/dev/null
  log "rule aligned: rotation_count=8, full catalogue (unpinned)"
fi

# ---------------------------------------------------------------------------
# The deploy is not done until the CLOUD is done.
#
# This script is the source of truth for the service env, and the env it sets
# includes TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED — so from 2026-08-02 it was
# faithfully enqueueing operational rows into DSQL while no drain existed to
# collect them. Every part of this file succeeded. The cloud did not work.
#
# Ending here makes those the same claim. It is READ-ONLY (one public HTTPS GET
# plus a text read of this file) and provisions nothing; if it fails, the
# service that was just deployed stays deployed and the message names what is
# still missing.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if ! bash "${SCRIPT_DIR}/verify_cloud_complete.sh" aws; then
  cat >&2 <<'NEXT'
The service is deployed but the AWS cloud is not complete. Most often this is
the drain — the step that has been missed before:

  bash scripts/deploy/aws_eu_clickhouse_drain_install.sh
  bash scripts/deploy/verify_cloud_complete.sh aws

NEXT
  exit 1
fi
