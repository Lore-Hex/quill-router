#!/usr/bin/env bash
# Deploy the AWS-EU control plane: App Runner (Dublin) on Aurora DSQL.
#
# Mirrors azure_canary*.sh in shape — build locally, push, deploy, verify with
# the same cloud-agnostic scripts/deploy/verify_deployment.sh. App Runner over
# ECS+ALB for the same reason Container Apps won on Azure: an HTTPS URL with no
# cert/LB ceremony, so the e2e loop closes fast. The four-nines active/active
# topology (Dublin + Stockholm behind health-checked DNS) comes after
# reserve/settle land; this is deliberately the single-region first rung.
#
# Auth to the database is IAM: the instance role tr-eu-app holds
# dsql:DbConnectAdmin on the Dublin cluster, and TR_POSTGRES_IAM_AUTH=aws-dsql
# makes PostgresStore mint a fresh token per physical connection (DSQL tokens
# expire in minutes — a static password dies within the hour). The DSN
# deliberately carries NO password.
set -euo pipefail

REGION="${REGION:-eu-west-1}"
ACCOUNT="${ACCOUNT:-330422590279}"
CLUSTER_ID="${CLUSTER_ID:-7rt62n5uiz6xjdsoi2ahypz3cq}"
ECR="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/trusted-router"
TAG="${TAG:-eu}"
SVC="${SVC:-tr-eu}"
DSQL_HOST="${CLUSTER_ID}.dsql.${REGION}.on.aws"

log(){ printf '\n=== %s\n' "$*" >&2; }

log "building linux/amd64 image and pushing to ${ECR}:${TAG}"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com" >/dev/null
docker buildx build --platform linux/amd64 -t "${ECR}:${TAG}" --push .

CONFIG=$(cat <<JSON
{
  "ImageRepository": {
    "ImageIdentifier": "${ECR}:${TAG}",
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
        "TR_SYNTHETIC_MONITOR_REGION": "${REGION}"
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

if aws apprunner list-services --region "$REGION" --query "ServiceSummaryList[?ServiceName=='${SVC}'].ServiceArn" --output text | grep -q arn; then
  ARN=$(aws apprunner list-services --region "$REGION" --query "ServiceSummaryList[?ServiceName=='${SVC}'].ServiceArn" --output text)
  log "updating existing service $ARN"
  aws apprunner update-service --region "$REGION" --service-arn "$ARN" --source-configuration "$CONFIG" >/dev/null
else
  log "creating service $SVC"
  ARN=$(aws apprunner create-service --region "$REGION" --service-name "$SVC" \
    --source-configuration "$CONFIG" \
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
log "service: https://${URL}"
echo "https://${URL}"
